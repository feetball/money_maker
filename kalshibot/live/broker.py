"""LiveBroker: sends the engine's orders to Kalshi (REAL MONEY).

It is a :class:`~kalshibot.paper.broker.PaperBroker` whose fills come from the exchange, not
the simulator. Everything downstream of a fill is shared with paper trading and unchanged:
per-strategy positions with Kalshi netting, marks from public books, settlement from market
results, equity snapshots, analytics, the dashboard and the risk manager.

Order flow (:meth:`LiveBroker.place_order`):

1. Local checks: the intent is valid, the market is active and before close, trading is not
   paused, the price is on the tick grid (all from the paper broker), plus the live hard caps
   ``live.max_order_contracts`` and ``live.max_order_cost``, and enough ledger cash.
2. The order is written to the database as ``open`` ("submitting") with its cash reserved and a
   unique ``client_order_id`` **before** anything is sent, so a crash can never lose a real order.
3. ``POST /portfolio/events/orders``. Taker orders (``tif="ioc"``) go out as
   ``live.taker_time_in_force`` (default ``fill_or_kill``, see below); resting ones as
   ``good_till_canceled`` with ``expiration_time``. A definitive error (4xx) rejects the order.
   A timeout/5xx leaves it ``open``; the next resting-order pass finds it by ``client_order_id``.
4. The order's fills are read from ``GET /portfolio/fills?order_id=`` and booked at the
   exchange's actual prices and fees.

**Fractional fills.** Kalshi matches in hundredths of a contract, so one order can fill
2.5 + 47.5. The ledger counts whole contracts: fills are booked as the order's running total
rounds down to a new whole number, at the volume-weighted price and fee of what was not booked yet,
so whole-contract totals book exactly. An order that ends on a fractional total
leaves a fraction of a contract unbooked; it is logged and kept in ``live.residue`` (shown in
:meth:`live_status`). ``fill_or_kill`` taker orders (the default) always fill a whole count or
nothing, so they never leave residue.

**Baskets** are rejected: Kalshi has no atomic multi-market order, and sending the legs one by
one can leave an unhedged position.

**Cash.** The ledger's cash is the bot's view. :meth:`reconcile` reads the exchange balance and
positions and reports the differences; it never changes the ledger by itself. Deposits and
withdrawals are booked with :meth:`sync_cash_to_exchange` (moves the starting balance too, so
P&L is unaffected).
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from types import SimpleNamespace
from typing import Any

from kalshibot.fees import trading_fee
from kalshibot.kalshi.client import KalshiAPIError, KalshiNotFound
from kalshibot.kalshi.trading import KalshiTradingClient, OrderSubmitUnknown
from kalshibot.money import ZERO, D
from kalshibot.paper.broker import STORE_ERRORS, PaperBroker, _Batch, _copy_order, _Meta
from kalshibot.paper.models import Fill, Order, Settlement, iso, parse_iso, q6

__all__ = ["LiveBroker"]

log = logging.getLogger(__name__)

#: an order whose submission outcome is unknown is given up (never reached Kalshi) after this
SUBMIT_GRACE_S = 90.0
#: fill reports can lag the order response; retry delays (s) before leaving it to the next pass
FILL_RETRY_DELAYS = (0.25, 0.5, 1.0)
#: exchange order states that are final
FINAL_X_STATUSES = frozenset({"canceled", "cancelled", "executed"})
FRACTION = Decimal("0.01")


def _floor_int(x: Decimal) -> int:
    return int(x.to_integral_value(rounding=ROUND_FLOOR))


def _dz(x: Any) -> Decimal:
    if x is None or x == "":
        return ZERO
    return D(x)


class LiveBroker(PaperBroker):
    """Real-money broker. ``trader`` is an authenticated :class:`KalshiTradingClient`."""

    mode = "live"

    def __init__(self, marketdata: Any, store: Any, trader: KalshiTradingClient, *, settings: Any = None,
                 clock: Any = None, sleep: Any = None, **kw: Any) -> None:
        live = getattr(settings, "live", None)
        self.trader = trader
        self.environment = str(getattr(live, "environment", "prod"))
        self.max_order_contracts = int(getattr(live, "max_order_contracts", 100))
        self.max_order_cost = D(getattr(live, "max_order_cost", 100))
        tif = str(getattr(live, "taker_time_in_force", "fill_or_kill"))
        self.taker_tif = "fok" if tif in ("fok", "fill_or_kill") else "ioc"
        self.client_prefix = str(getattr(live, "client_order_prefix", "kb"))
        self._residue: dict[str, dict[str, Decimal]] = {}
        #: last :meth:`reconcile` result (exchange balance, drift, position mismatches)
        self.exchange: dict[str, Any] = {"checked_at": None, "error": None}
        #: orders are refused (and the engine may not start) until :meth:`start` succeeded
        self.ready = False
        self.blocked_reason: str | None = "not started"
        #: where the key came from: "config" (config.yaml / env), "dashboard", or None
        self.credentials_source: str | None = None
        super().__init__(marketdata, store, settings=settings, clock=clock, sleep=sleep, taker_latency_s=0.0, **kw)

    # ------------------------------------------------------------------ state

    def _init_state(self, starting_balance: Decimal) -> None:
        super()._init_state(starting_balance)
        self._residue = {}

    def _load(self, default_start: Decimal) -> None:
        super()._load(default_start)
        if self.store is not None:
            raw = self.store.get_kv("live.residue", {}) or {}
            self._residue = {t: {s: D(q) for s, q in sides.items()} for t, sides in raw.items()}
            self._persisted["live.residue"] = self._residue_state()

    def _residue_state(self) -> dict[str, dict[str, str]]:
        return {t: {s: str(q) for s, q in sorted(sides.items()) if q} for t, sides in sorted(self._residue.items())
                if any(sides.values())}

    def _state_values(self) -> dict[str, Any]:
        out = super()._state_values()
        out["live.residue"] = self._residue_state()
        return out

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _xs(order: Order) -> dict[str, str]:
        """Live bookkeeping kept in ``order.fee_state`` (persisted with the order)."""
        return order.fee_state

    def exchange_order_id(self, order: Order) -> str | None:
        return self._xs(order).get("x_id") or None

    def not_before(self, decided_at: datetime | None) -> datetime | None:
        return None  # real orders have real latency

    def _live_guard(self, order: Order) -> str | None:
        if order.count > self.max_order_contracts:
            return f"live cap: {order.count} contracts > live.max_order_contracts {self.max_order_contracts}"
        cost = order.buy_limit * order.count
        if cost > self.max_order_cost:
            return f"live cap: order cost ${cost} > live.max_order_cost ${self.max_order_cost}"
        return None

    def _need_cash(self, order: Order, fee_type: str, mult: Decimal) -> Decimal:
        """Cash to hold while the order is live: principal for the contracts that open a
        position plus the taker fee at the limit (closing contracts free $1 per pair)."""
        opening = order.count - min(order.count, self._closable(order))
        fee = trading_fee(order.buy_limit, order.count, is_taker=True, fee_type=fee_type,
                          fee_multiplier=mult, precision=self.precision)
        return max(ZERO, opening * order.buy_limit + fee)

    def _new_client_id(self, order: Order) -> str:
        return f"{self.client_prefix}-{order.id}-{secrets.token_hex(4)}"

    # ------------------------------------------------------------------ placement

    async def place_order(self, intent: Any, *, count: int | None = None,
                          decided_at: datetime | None = None) -> Order:
        order = self._new_order(intent, count, self._now())
        pristine = _copy_order(order)
        meta: _Meta | str | None = None
        if order.status != "rejected":
            meta = (f"live trading is not ready: {self.blocked_reason}" if not self.ready
                    else self._live_guard(order) or await self._meta(order))
        async with self._lock:
            try:
                with self._atomic():
                    batch = _Batch(new_orders={order.id})
                    now = self._now()
                    if order.status != "rejected":
                        if isinstance(meta, str) or meta is None:
                            self._set_rejected(order, meta or "not prepared", now)
                        else:
                            need = self._need_cash(order, meta.fee_type, meta.fee_mult)
                            if need > self.cash:
                                self._set_rejected(order, f"insufficient cash: need {need}, free {self.cash}", now)
                            else:
                                order.event_ticker = meta.market.event_ticker
                                order.created_at = order.updated_at = now
                                order.reserved = need
                                self.cash -= need
                                order.status = "open"
                                order.status_reason = "submitting to Kalshi"
                                order.fee_state = {
                                    "x_coid": self._new_client_id(order),
                                    "x_tif": self.taker_tif if order.tif == "ioc" else "gtc",
                                    "x_submitted_at": iso(now) or "",
                                    "x_fee_type": meta.fee_type, "x_fee_mult": str(meta.fee_mult),
                                    "x_filled": "0", "x_cost": "0", "x_fee": "0",
                                    "x_booked_n": "0", "x_booked_cost": "0", "x_booked_fee": "0",
                                    "x_seen": "",
                                }
                    batch.orders[order.id] = order
                    self._commit(batch)
            except STORE_ERRORS as e:
                self._not_recorded([pristine], e)
                return pristine
        if order.status == "rejected":
            self._log("info", "order", f"order {order.id} {order.ticker} rejected: {order.status_reason}",
                      order_id=order.id, strategy=order.strategy)
            return order
        return await self._submit(order)

    async def _submit(self, order: Order) -> Order:
        xs = self._xs(order)
        tif = xs["x_tif"]
        exp_ts = int(order.expires_at.timestamp()) if tif == "gtc" and order.expires_at is not None else None
        try:
            resp = await self.trader.create_order(
                ticker=order.ticker, side=order.side, action=order.action, count=order.count,
                limit_price=order.limit_price, tif=tif, client_order_id=xs["x_coid"], expiration_ts=exp_ts)
        except OrderSubmitUnknown as e:
            self._log("warning", "order", f"order {order.id} {order.ticker}: submission outcome unknown ({e}); "
                      "resolving by client_order_id", order_id=order.id, strategy=order.strategy)
            await self._update(order, status_reason=f"submission outcome unknown: {e.message}")
            return order
        except KalshiAPIError as e:
            async with self._lock:
                with self._atomic():
                    batch = _Batch()
                    self._finish(order, "rejected", f"Kalshi rejected the order: {e.message or e}", self._now(), batch)
                    self._commit(batch)
            self._log("warning", "order", f"order {order.id} {order.ticker} rejected by Kalshi: {e}",
                      order_id=order.id, strategy=order.strategy)
            return order
        except Exception as e:  # programming/transport surprise: treat as unknown, never resend
            log.exception("order %s: unexpected submit error", order.id)
            await self._update(order, status_reason=f"submission outcome unknown: {type(e).__name__}: {e}")
            return order
        xid = str(resp.get("order_id") or "")
        filled = _dz(resp.get("fill_count"))
        remaining = _dz(resp.get("remaining_count"))
        final = tif != "gtc" or remaining <= 0
        await self._update(order, x_id=xid, status_reason="resting on Kalshi" if not final else "")
        self._log("info", "order", f"order {order.id} {order.ticker} sent to Kalshi as {xid}: "
                  f"{order.action} {order.count} {order.side} @ {order.limit_price} ({tif}); filled {filled}",
                  order_id=order.id, strategy=order.strategy, exchange_order_id=xid)
        await self._sync(order, x_filled=filled, x_final=final, retry_fills=True)
        return order

    async def _update(self, order: Order, *, status_reason: str | None = None, x_id: str | None = None) -> None:
        async with self._lock:
            with self._atomic():
                batch = _Batch()
                if x_id is not None:
                    order.fee_state = {**order.fee_state, "x_id": x_id}
                if status_reason is not None:
                    order.status_reason = status_reason
                order.updated_at = self._now()
                batch.orders[order.id] = order
                self._commit(batch)

    async def place_basket(self, intents: Iterable[Any], *, all_or_none: bool = True,
                           counts: Sequence[int | None] | None = None,
                           decided_at: datetime | None = None) -> list[Order]:
        intents = list(intents)
        counts = list(counts) if counts is not None else [None] * len(intents)
        if not all_or_none:
            return [await self.place_order(i, count=c) for i, c in zip(intents, counts, strict=True)]
        now = self._now()
        orders = [self._new_order(i, c, now) for i, c in zip(intents, counts, strict=True)]
        async with self._lock:
            with self._atomic():
                batch = _Batch(new_orders={o.id for o in orders})
                for o in orders:
                    if o.status != "rejected":
                        self._set_rejected(o, "baskets are not supported in live trading "
                                              "(Kalshi has no atomic multi-leg order)", now)
                    batch.orders[o.id] = o
                self._commit(batch)
        return orders

    # ------------------------------------------------------------------ sync with the exchange

    async def _fetch_fills(self, xid: str, want: Decimal | None, retry: bool) -> list[dict[str, Any]]:
        fills = await self.trader.get_fills(order_id=xid)
        if not retry or want is None:
            return fills
        for delay in FILL_RETRY_DELAYS:
            if sum((_dz(f.get("count_fp")) for f in fills), ZERO) >= want:
                break
            await self._sleep(delay)
            fills = await self.trader.get_fills(order_id=xid)
        return fills

    async def _sync(self, order: Order, *, x_filled: Decimal | None = None, x_final: bool | None = None,
                    x_status: str = "", retry_fills: bool = False) -> list[Fill]:
        """Bring one order up to date with the exchange: book new fills, finish it when the
        exchange is done with it and every reported fill has been booked."""
        xid = self.exchange_order_id(order)
        if not xid:
            return []
        if x_filled is None or x_final is None:
            try:
                xo = await self.trader.get_order(xid)
            except KalshiNotFound:
                xo = {}
            except Exception as e:
                log.warning("order %s (%s): status read failed: %s", order.id, xid, e)
                return []
            x_status = str(xo.get("status") or "")
            x_filled = _dz(xo.get("fill_count_fp")) if xo else ZERO
            x_final = (x_status in FINAL_X_STATUSES) if xo else True
        fills: list[dict[str, Any]] = []
        if x_filled > _dz(self._xs(order).get("x_filled")):
            try:
                fills = await self._fetch_fills(xid, x_filled, retry_fills)
            except Exception as e:
                log.warning("order %s (%s): fills read failed: %s", order.id, xid, e)
                return []
        async with self._lock:
            with self._atomic():
                batch = _Batch()
                now = self._now()
                cur = self._open.get(order.id)
                if cur is None:
                    return []
                self._book(cur, fills, batch)
                seen = _dz(self._xs(cur).get("x_filled"))
                if x_final and seen >= x_filled:
                    self._finalize(cur, now, batch, x_status)
                elif x_final:
                    cur.status_reason = f"done on Kalshi; waiting for fill reports ({seen}/{x_filled})"
                    batch.orders[cur.id] = cur
                self._commit(batch)
                return list(batch.fills)

    def _book(self, order: Order, fills: Sequence[Mapping[str, Any]], batch: _Batch) -> None:
        """Book exchange fills not booked yet (whole contracts; see the module docstring)."""
        xs = dict(order.fee_state)
        seen_ids = set(filter(None, xs.get("x_seen", "").split(",")))
        filled, cost, fee = _dz(xs.get("x_filled")), _dz(xs.get("x_cost")), _dz(xs.get("x_fee"))
        bn, bcost, bfee = int(xs.get("x_booked_n") or 0), _dz(xs.get("x_booked_cost")), _dz(xs.get("x_booked_fee"))
        fee_type, mult = xs.get("x_fee_type", "quadratic"), _dz(xs.get("x_fee_mult") or "1")
        for f in sorted(fills, key=lambda f: (str(f.get("created_time") or ""), str(f.get("fill_id") or ""))):
            fid = str(f.get("fill_id") or f.get("trade_id") or "")
            if not fid or fid in seen_ids:
                continue
            seen_ids.add(fid)
            n = _dz(f.get("count_fp"))
            side = str(f.get("outcome_side") or "")
            if side and side != order.buy_side:
                log.error("order %s: fill %s is on %s, the order buys %s", order.id, fid, side, order.buy_side)
            price = _dz(f.get("yes_price_dollars") if order.buy_side == "yes" else f.get("no_price_dollars"))
            filled += n
            cost += n * price
            fee += _dz(f.get("fee_cost"))
            target = min(_floor_int(filled), order.count)
            delta = target - bn
            if delta <= 0:
                continue
            unbooked = filled - bn
            avg = q6((cost - bcost) / unbooked)
            fee_part = q6((fee - bfee) * delta / unbooked)
            ts = parse_iso(f.get("created_time")) or self._now()
            old_reserved = order.reserved
            self._apply_fill(order, avg, delta, SimpleNamespace(net_fee=fee_part),  # type: ignore[arg-type]
                             is_taker=bool(f.get("is_taker", True)), ts=ts, batch=batch)
            order.reserved = min(old_reserved, self._reserve_amount(order.remaining, order.buy_limit, fee_type, mult))
            self.cash += old_reserved - order.reserved
            bn, bcost, bfee = target, bcost + avg * delta, bfee + fee_part
        xs.update({"x_seen": ",".join(sorted(seen_ids)), "x_filled": str(filled), "x_cost": str(cost),
                   "x_fee": str(fee), "x_booked_n": str(bn), "x_booked_cost": str(bcost), "x_booked_fee": str(bfee)})
        order.fee_state = xs
        if order.status == "open" and order.filled_count:
            order.status = "partially_filled"
        batch.orders[order.id] = order

    def _finalize(self, order: Order, now: datetime, batch: _Batch, x_status: str = "") -> None:
        xs = order.fee_state
        left = _dz(xs.get("x_filled")) - int(xs.get("x_booked_n") or 0)
        if left > 0:
            # a fraction of a contract that the whole-contract ledger cannot hold
            sides = self._residue.setdefault(order.ticker, {})
            sides[order.buy_side] = sides.get(order.buy_side, ZERO) + left
            unbooked_fee = _dz(xs.get("x_fee")) - _dz(xs.get("x_booked_fee"))
            unbooked_cost = _dz(xs.get("x_cost")) - _dz(xs.get("x_booked_cost"))
            self.cash -= unbooked_cost + unbooked_fee  # the money really left the account
            self._log("warning", "order", f"order {order.id} {order.ticker}: {left} {order.buy_side} contract(s) "
                      "filled as a fraction and are not in the ledger (see live.residue)",
                      order_id=order.id, strategy=order.strategy)
        if order.filled_count >= order.count:
            status, reason = "filled", ""
        elif order.tif == "gtc" and order.expires_at is not None and now >= order.expires_at:
            status, reason = "expired", "expired on Kalshi"
        elif order.tif == "gtc":
            status, reason = "cancelled", xs.get("x_cancel_reason") or "cancelled on Kalshi"
        elif order.filled_count == 0:
            status, reason = "cancelled", f"{xs.get('x_tif', 'ioc')}: no fill at or better than the limit"
        else:
            status, reason = "cancelled", f"{xs.get('x_tif', 'ioc')}: {order.remaining} unfilled (remainder cancelled)"
        self._finish(order, status, reason, now, batch)

    async def _resolve_unknown(self, order: Order) -> None:
        """An order sent without a known outcome: find it by client_order_id, or give up on it."""
        xs = self._xs(order)
        coid = xs.get("x_coid", "")
        submitted = parse_iso(xs.get("x_submitted_at")) or order.created_at or self._now()
        try:
            found = [o for o in await self.trader.get_orders(
                ticker=order.ticker, min_ts=int(submitted.timestamp()) - 120) if o.get("client_order_id") == coid]
        except Exception as e:
            log.warning("order %s: lookup by client_order_id failed: %s", order.id, e)
            return
        if found:
            await self._update(order, x_id=str(found[0].get("order_id")), status_reason="")
            xo = found[0]
            await self._sync(order, x_filled=_dz(xo.get("fill_count_fp")),
                             x_final=str(xo.get("status") or "") in FINAL_X_STATUSES, x_status=str(xo.get("status")))
            return
        if (self._now() - submitted).total_seconds() < SUBMIT_GRACE_S:
            return
        async with self._lock:
            with self._atomic():
                batch = _Batch()
                cur = self._open.get(order.id)
                if cur is not None:
                    self._finish(cur, "rejected", "never reached Kalshi (no order with its client_order_id)",
                                 self._now(), batch)
                    self._commit(batch)

    async def process_resting_orders(self, tickers: Iterable[str] | None = None, *,
                                     max_trade_polls: int | None = None) -> list[Fill]:
        """Sync every open order with the exchange (fills, cancels, expiries)."""
        if self.trader.signer is None:
            return []
        async with self._poll_lock:
            want = set(tickers) if tickers is not None else None
            orders = [o for o in self.open_orders() if want is None or o.ticker in want]
            out: list[Fill] = []
            for o in orders:
                if not self.exchange_order_id(o):
                    await self._resolve_unknown(o)
                else:
                    out.extend(await self._sync(o))
            return out

    # ------------------------------------------------------------------ cancels

    async def _cancel_on_exchange(self, order: Order, reason: str) -> Order:
        xid = self.exchange_order_id(order)
        if not xid:
            self._log("warning", "order", f"order {order.id}: cannot cancel yet (submission outcome unknown)",
                      order_id=order.id)
            return order
        try:
            await self.trader.cancel_order(xid, order.ticker)
        except KalshiNotFound:
            pass  # already executed/cancelled: the sync below books what happened
        except Exception as e:
            self._log("warning", "order", f"order {order.id}: cancel on Kalshi failed: {e}", order_id=order.id)
            return order
        order.fee_state = {**order.fee_state, "x_cancel_reason": reason}  # persisted by the sync's commit
        await self._sync(order)
        return self.get_order(order.id) or order

    async def cancel_order(self, order_id: int, reason: str = "cancelled by user") -> Order:
        o = self._open.get(order_id)
        if o is None:
            stored = self.store.get_order(order_id) if self.store is not None else None
            if stored is None:
                raise KeyError(order_id)
            return stored
        return await self._cancel_on_exchange(o, reason)

    async def cancel_orders(self, order_ids: Iterable[int], *, reason: str = "cancelled by strategy",
                            reasons: Mapping[int, str] | None = None, strategy: str | None = None,
                            sync: bool = True) -> list[Order]:
        out = []
        for i in dict.fromkeys(order_ids):
            o = self._open.get(i)
            if o is None or (strategy is not None and o.strategy != strategy):
                continue
            out.append(await self._cancel_on_exchange(o, (reasons or {}).get(i) or reason))
        return out

    async def cancel_all(self, *, ticker: str | None = None, strategy: str | None = None,
                         reason: str = "cancelled") -> list[Order]:
        ids = [o.id for o in self.open_orders(ticker=ticker, strategy=strategy)]
        return await self.cancel_orders(ids, reason=reason)

    # ------------------------------------------------------------------ account

    async def start(self) -> bool:
        """Check the credentials, adopt the exchange balance for a brand-new ledger, resolve
        orders left open by a previous run and reconcile. Never raises: on a missing/rejected
        key or an unreachable API the broker stays not ready (:attr:`blocked_reason`)."""
        self.ready = False
        if self.trader.signer is None:
            self.blocked_reason = f"no Kalshi {self.environment} API key yet"
            self._log("warning", "live", self.blocked_reason)
            return False
        try:
            await self._start()
        except Exception as e:
            self.blocked_reason = f"Kalshi refused or could not be reached: {e}"
            self._log("error", "live", f"live trading not ready: {self.blocked_reason}")
            return False
        self.ready, self.blocked_reason = True, None
        return True

    async def set_signer(self, signer: Any, source: str) -> bool:
        """Swap in a new key (dashboard) and re-run :meth:`start`."""
        self.trader.signer = signer
        self.credentials_source = source
        return await self.start()

    async def _start(self) -> None:
        balance = await self.trader.balance_dollars()
        # nothing ever traded (rejected orders, e.g. while locked, don't count)
        fresh = not self._positions and not self._open and (self.store is None or self.store.max_id("fills") == 0)
        if fresh and balance != self.starting_balance:
            await super().reset(balance)
            self._log("info", "account", f"live ledger started at the Kalshi balance ${balance}")
        await self.process_resting_orders()
        await self.reconcile()

    async def reconcile(self) -> dict[str, Any]:
        """Compare the ledger with the exchange (balance, positions). Never changes the ledger."""
        if self.trader.signer is None:
            return self.exchange
        now = self._now()
        try:
            bal = await self.trader.get_balance()
            positions = await self.trader.get_positions()
        except Exception as e:
            self.exchange = {**self.exchange, "checked_at": iso(now), "error": f"{type(e).__name__}: {e}"}
            self._log("warning", "live", f"reconcile failed: {e}")
            return self.exchange
        balance = D(bal["balance_dollars"]) if bal.get("balance_dollars") else D(bal.get("balance", 0)) / 100
        portfolio = D(bal.get("portfolio_value", 0)) / 100
        # Kalshi's ``balance`` is the *available* balance (resting-order collateral excluded), so it
        # compares with free cash; our fee budget on resting orders can make a cents-sized gap
        ledger_cash = self.cash + self.reserved_profit
        ledger_net: dict[str, Decimal] = {}
        for p in self.positions():
            ledger_net[p.ticker] = ledger_net.get(p.ticker, ZERO) + (p.count if p.side == "yes" else -p.count)
        for t, sides in self._residue.items():
            ledger_net[t] = ledger_net.get(t, ZERO) + sides.get("yes", ZERO) - sides.get("no", ZERO)
        x_net = {str(p.get("ticker")): _dz(p.get("position_fp")) for p in positions}
        mismatches = []
        for t in sorted(set(ledger_net) | set(x_net)):
            a, b = ledger_net.get(t, ZERO), x_net.get(t, ZERO)
            if abs(a - b) >= FRACTION:
                mismatches.append({"ticker": t, "ledger": float(a), "exchange": float(b)})
        drift = balance - ledger_cash
        self.exchange = {
            "checked_at": iso(now), "error": None, "environment": self.environment,
            "balance": float(balance), "portfolio_value": float(portfolio),
            "ledger_cash": float(ledger_cash), "cash_drift": float(drift),
            "position_mismatches": mismatches, "residue": {t: {s: float(q) for s, q in v.items()}
                                                           for t, v in self._residue.items() if any(v.values())},
        }
        if mismatches or abs(drift) >= 1:
            self._log("warning", "live", f"reconcile: cash drift ${drift}, {len(mismatches)} position mismatch(es)",
                      mismatches=mismatches[:20])
        return self.exchange

    def live_status(self) -> dict[str, Any]:
        return {"mode": self.mode, "environment": self.environment, "ready": self.ready,
                "blocked_reason": self.blocked_reason, "credentials_source": self.credentials_source,
                "max_order_contracts": self.max_order_contracts,
                "max_order_cost": float(self.max_order_cost), "taker_time_in_force": self.taker_tif,
                "trader_requests": self.trader.request_count, "trader_errors": self.trader.error_count,
                "exchange": self.exchange}

    async def sync_cash_to_exchange(self) -> Decimal:
        """Book the difference between the exchange balance and the ledger's cash as a
        deposit/withdrawal: the starting balance moves with it, so P&L is unchanged. A deposit
        goes to tradeable cash. A withdrawal (taking profits on kalshi.com) comes out of
        ``reserved_profit`` first, then cash, so the bot keeps trading the same stake.
        Refused while orders are open."""
        if self._open:
            raise ValueError("cancel or wait for open orders before syncing cash")
        balance = await self.trader.balance_dollars()
        async with self._lock:
            with self._atomic():
                delta = balance - (self.cash + self.reserved_profit)
                from_reserve = min(self.reserved_profit, -delta) if delta < 0 else ZERO
                self.reserved_profit -= from_reserve
                self.cash += delta + from_reserve
                self.starting_balance += delta
                if self.store is not None:
                    with self.store.transaction():
                        written = self._write_state()
                    self._persisted.update(written)
        self._log("info", "account", f"ledger cash synced to the Kalshi balance: {delta:+} booked as a transfer")
        await self.reconcile()
        return delta

    async def reset(self, starting_balance: Any = None) -> Any:
        """Start a new live ledger at the current Kalshi balance (refused while the ledger
        holds positions or open orders; ``starting_balance`` is ignored)."""
        if self._open or self.positions():
            raise ValueError("the live ledger still has open orders or positions; close them first")
        balance = await self.trader.balance_dollars()
        acct = await super().reset(balance)
        self._residue = {}
        return acct
