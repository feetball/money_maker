"""SpotRiskManager: pre-trade limits and kill switch for the Coinbase venue (contract §11) - PAPER ONLY.

``SpotRiskManager(settings).check(intent, portfolio, account) -> RiskDecision``

``portfolio`` is the broker's :class:`~kalshibot.coinbase.paper.SpotPortfolioView` for the
intent's strategy (its ``all_positions`` / ``all_open_orders`` give the account-wide
picture); ``account`` the broker's :class:`~kalshibot.coinbase.paper.SpotAccountState`.

**Sells always pass** (they reduce risk): the kill switch, the spread guard, the order-rate
limit and every dollar limit are skipped. A sell larger than what the strategy holds (net of
its resting sells) is trimmed to that quantity; nothing held -> rejected.

**Buys** are checked in this order (the decision names the limit that bound it):

=================================  =========================================================
``max_orders_per_minute``          buys approved in the trailing 60 s, per strategy (sells are
                                   not counted, so a rebalance's sells never starve its buys)
``daily_loss_limit``               today's P&L <= -limit trips the kill switch
kill switch                        no buys while on (manual trips are sticky; a daily-loss
                                   trip is released at the next UTC day unless
                                   ``kill_switch_auto_release: false``)
``max_spread_bps``                 book spread (``book`` / ``spread_bps``) above -> rejected;
                                   not checked when the caller gives neither
``min_trade_usd``                  requested (and approved) cost below -> rejected
``max_position_pct_per_product``   product exposure (all strategies) + cost <= pct% of equity
``max_total_exposure_pct``         total exposure + cost <= pct% of equity
``max_strategy_allocation_pct``    the strategy's exposure + cost <= pct% of equity; pct = the
                                   strategy's own ``max_allocation_pct`` when set
``min_cash_reserve``               free cash - cost >= reserve
=================================  =========================================================

Dollar limits **size the buy down** (``approved_quote`` for a ``quote_size`` buy,
``approved_base`` for a base-sized limit buy) rather than rejecting, unless what is left is
below ``min_trade_usd``. *Exposure* = liquidation value of holdings (cost basis while
unmarked) + USD reserved by resting buys. *Cost* of a buy = ``quote_size`` (fee included),
or ``base_size x price x (1 + taker_rate)``.

The kill switch, its reason and runtime limit overrides persist in the Coinbase store
(``kv['risk.kill_switch']``, ``risk_limits``); nothing is shared with the Kalshi venue.
"""

from __future__ import annotations

import dataclasses
import logging
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Any

from kalshibot.coinbase.config import CoinbaseRiskSettings
from kalshibot.coinbase.fees import DEFAULT_TIER, FeeTier
from kalshibot.coinbase.paper import VENUE, f8, spot_exposure
from kalshibot.money import ZERO, D

if TYPE_CHECKING:
    from kalshibot.coinbase.store import SpotStore

__all__ = ["KILL_SWITCH_KEY", "LIMIT_FIELDS", "RiskDecision", "SpotRiskManager"]

log = logging.getLogger(__name__)

KILL_SWITCH_KEY = "risk.kill_switch"
#: The limits of contract §11 (+ ``kill_switch_auto_release``), in display order.
LIMIT_FIELDS = ("max_position_pct_per_product", "max_total_exposure_pct", "max_strategy_allocation_pct",
                "min_cash_reserve", "max_orders_per_minute", "daily_loss_limit", "max_spread_bps", "min_trade_usd",
                "kill_switch_auto_release")
_CENT = Decimal("0.01")
_Q8 = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Outcome of :meth:`SpotRiskManager.check`. Exactly one of ``approved_quote`` (buys by
    ``quote_size``) / ``approved_base`` (sells and base-sized buys) is set; 0 = rejected."""

    approved_quote: Decimal | None = None
    approved_base: Decimal | None = None
    reason: str = ""
    requested_quote: Decimal | None = None
    requested_base: Decimal | None = None
    binding_limit: str | None = None  # limit that reduced / rejected the order
    reduces_risk: bool = False  # a sell

    @property
    def approved(self) -> bool:
        v = self.approved_quote if self.approved_quote is not None else self.approved_base
        return v is not None and v > 0

    @property
    def partial(self) -> bool:
        if not self.approved:
            return False
        if self.approved_quote is not None and self.requested_quote is not None:
            return self.approved_quote < self.requested_quote
        if self.approved_base is not None and self.requested_base is not None:
            return self.approved_base < self.requested_base
        return False

    def apply(self, intent: Any) -> Any:
        """A copy of ``intent`` sized to the approved amount (dataclass intents)."""
        changes: dict[str, Any] = {}
        if self.approved_quote is not None:
            changes["quote_size"] = self.approved_quote
        if self.approved_base is not None:
            changes["base_size"] = self.approved_base
        if dataclasses.is_dataclass(intent) and not isinstance(intent, type):
            return dataclasses.replace(intent, **changes)
        for k, v in changes.items():
            setattr(intent, k, v)
        return intent


def _get(obj: Any, name: str, default: Any = None) -> Any:
    v = getattr(obj, name, None)
    if v is None and isinstance(obj, Mapping):
        v = obj.get(name)
    return default if v is None else v


def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        d = D(v)
    except (TypeError, ValueError, ArithmeticError):
        return None
    return d if d.is_finite() else None


def _risk_base(settings: Any) -> CoinbaseRiskSettings:
    if settings is None:
        return CoinbaseRiskSettings()
    if isinstance(settings, CoinbaseRiskSettings):
        return settings
    if isinstance(settings, Mapping):
        return CoinbaseRiskSettings.model_validate(dict(settings))
    cb = getattr(settings, "coinbase", None) or settings
    r = getattr(cb, "risk", None)
    return r if isinstance(r, CoinbaseRiskSettings) else CoinbaseRiskSettings()


class SpotRiskManager:
    """Pre-trade checks for Coinbase paper orders. ``settings``: ``CoinbaseSettings``, a full
    ``Settings`` (its ``coinbase`` section), ``CoinbaseRiskSettings`` or a mapping of limits."""

    def __init__(self, settings: Any = None, *, store: SpotStore | None = None,
                 clock: Callable[[], datetime] | None = None, fee_tier: FeeTier | None = None) -> None:
        self.base = _risk_base(settings)
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        cb = getattr(settings, "coinbase", None) or settings
        tier_fn = getattr(cb, "tier", None)
        tier = fee_tier or (tier_fn() if callable(tier_fn) else None)
        self.taker_rate: Decimal = tier.taker_rate if isinstance(tier, FeeTier) else DEFAULT_TIER.taker_rate
        overrides = store.get_risk_limits() if store is not None else {}
        self.limits: CoinbaseRiskSettings = self._merge(overrides)
        self._order_times: dict[str, deque[datetime]] = {}
        #: per-strategy allocation (% of Coinbase equity); unset -> max_strategy_allocation_pct
        self.strategy_allocations: dict[str, float] = {}
        strategies = getattr(cb, "strategies", None)
        if isinstance(strategies, Mapping):
            for name, s in strategies.items():
                pct = _get(s, "max_allocation_pct")
                if pct is not None:
                    self.strategy_allocations[str(name)] = float(pct)
        ks = store.get_kv(KILL_SWITCH_KEY) if store is not None else None
        ks = ks if isinstance(ks, Mapping) else {}
        self.kill_switch: bool = bool(ks.get("on"))
        self.kill_switch_reason: str = str(ks.get("reason") or "") if self.kill_switch else ""
        self.kill_switch_auto: bool = bool(self.kill_switch and ks.get("auto"))
        self.kill_switch_day: str | None = (str(ks.get("ts") or "")[:10] or None) if self.kill_switch else None

    # ------------------------------------------------------------------ limits

    def _merge(self, overrides: Mapping[str, Any]) -> CoinbaseRiskSettings:
        """Config limits + stored runtime overrides. An override that no longer validates
        (edited database, changed bounds) is skipped with a warning rather than failing the
        venue's startup."""
        data = self.base.model_dump()
        for k, v in overrides.items():
            if k not in LIMIT_FIELDS:
                continue
            trial = {**data, k: v}
            try:
                CoinbaseRiskSettings.model_validate(trial)
            except Exception as e:  # pydantic.ValidationError, bad JSON types
                log.warning("coinbase risk: ignoring invalid stored override %s=%r: %s", k, v, e)
                continue
            data = trial
        return CoinbaseRiskSettings.model_validate(data)

    @property
    def auto_release(self) -> bool:
        return bool(getattr(self.limits, "kill_switch_auto_release", True))

    def update_limits(self, patch: Mapping[str, Any]) -> CoinbaseRiskSettings:
        """Validate and apply a partial update (``PATCH /api/coinbase/risk``); persisted as
        overrides. Unknown keys -> ``ValueError``; bad values -> ``pydantic.ValidationError``."""
        unknown = set(patch) - set(LIMIT_FIELDS)
        if unknown:
            raise ValueError(f"unknown coinbase risk limits: {sorted(unknown)}")
        if "kill_switch_auto_release" in patch and not isinstance(patch["kill_switch_auto_release"], bool):
            raise ValueError("kill_switch_auto_release must be true or false")
        data = self.limits.model_dump()
        data.update(patch)
        new = CoinbaseRiskSettings.model_validate(data)
        self.limits = new
        if self.store is not None:
            dumped = new.model_dump(mode="json")
            self.store.save_risk_limits({k: dumped.get(k, patch[k]) for k in patch})
        return new

    def limits_json(self) -> dict[str, Any]:
        d = self.limits.model_dump(mode="json")
        out = {k: d[k] for k in LIMIT_FIELDS if k in d}
        out["kill_switch_auto_release"] = self.auto_release
        return out

    def set_strategy_allocations(self, allocations: Mapping[str, float | None]) -> None:
        """Install per-strategy allocations in % of equity (``None`` removes one)."""
        for name, pct in allocations.items():
            if pct is None:
                self.strategy_allocations.pop(name, None)
            else:
                self.strategy_allocations[name] = float(pct)

    def allocation_pct(self, strategy: str) -> float:
        """The strategy's exposure cap in % of equity (its own, else the account-wide fallback)."""
        own = self.strategy_allocations.get(strategy)
        return float(own) if own is not None else float(self.limits.max_strategy_allocation_pct)

    # ------------------------------------------------------------------ kill switch

    def _now(self) -> datetime:
        n = self.clock()
        return n if n.tzinfo else n.replace(tzinfo=UTC)

    def set_kill_switch(self, on: bool, reason: str = "manual", *, auto: bool = False,
                        now: datetime | None = None) -> None:
        """Turn the kill switch on/off (persisted). ``auto``: tripped by the daily loss limit.
        The caller (engine) cancels resting buy orders when it engages."""
        now = now or self._now()
        changed = bool(on) != self.kill_switch
        self.kill_switch = bool(on)
        self.kill_switch_reason = reason if on else ""
        self.kill_switch_auto = bool(on and auto)
        self.kill_switch_day = now.astimezone(UTC).date().isoformat() if on else None
        if self.store is not None:
            self.store.set_kv(KILL_SWITCH_KEY, {"on": self.kill_switch, "reason": self.kill_switch_reason,
                                                "ts": now.isoformat(), "auto": self.kill_switch_auto})
        if changed:
            self._log("warning" if on else "info", f"coinbase kill switch {'ON' if on else 'off'}: {reason}")

    @staticmethod
    def _daily_pnl(account: Any, portfolio: Any = None) -> Decimal:
        for src in (account, portfolio):
            if src is None:
                continue
            v = _get(src, "todays_pnl")
            if v is None:
                v = _get(src, "daily_pnl")
            if v is not None:
                return D(v)
            eq, start = _get(src, "equity"), _get(src, "day_start_equity")
            if eq is not None and start is not None:
                return D(eq) - D(start)
        return ZERO

    def evaluate(self, account: Any, now: datetime | None = None, *, portfolio: Any = None) -> bool:
        """Release a daily-loss trip from an earlier UTC day (``kill_switch_auto_release``), then
        trip the kill switch if today's P&L breaches ``daily_loss_limit``. Returns the state."""
        now = now or self._now()
        today = now.astimezone(UTC).date().isoformat()
        if (self.kill_switch and self.kill_switch_auto and self.auto_release
                and self.kill_switch_day is not None and today != self.kill_switch_day):
            self.set_kill_switch(False, f"daily loss limit trip of {self.kill_switch_day} released at the new UTC day",
                                 now=now)
        limit = D(self.limits.daily_loss_limit)
        if limit > 0 and not self.kill_switch:
            pnl = self._daily_pnl(account, portfolio)
            if pnl <= -limit:
                self.set_kill_switch(True, f"daily loss limit: today's P&L {pnl:.2f} <= -{limit}", auto=True, now=now)
        return self.kill_switch

    # ------------------------------------------------------------------ order rate

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=60)
        for k in list(self._order_times):
            q = self._order_times[k]
            while q and q[0] <= cutoff:
                q.popleft()
            if not q:
                del self._order_times[k]

    def orders_last_minute(self, now: datetime | None = None, *, strategy: str | None = None) -> int:
        """Buys approved in the trailing 60 s: of ``strategy``, or of all strategies."""
        self._prune(now or self._now())
        if strategy is not None:
            return len(self._order_times.get(strategy, ()))
        return sum(len(q) for q in self._order_times.values())

    def record_order(self, ts: datetime | None = None, strategy: str = "") -> None:
        self._order_times.setdefault(str(strategy or ""), deque()).append(ts or self._now())

    # ------------------------------------------------------------------ logging

    def _log(self, level: str, message: str, **data: Any) -> None:
        log.log(getattr(logging, level.upper(), logging.INFO), "coinbase risk: %s", message)
        if self.store is not None:
            try:
                self.store.insert_log(level, "risk", message, {"venue": VENUE, **data} if data else {"venue": VENUE},
                                      ts=self._now())
            except Exception:  # pragma: no cover - logging must not fail trading
                log.exception("failed to write coinbase risk log")

    # ------------------------------------------------------------------ the check

    @staticmethod
    def _spread_bps(book: Any, spread_bps: Any) -> Decimal | None:
        if spread_bps is not None:
            return _dec(spread_bps)
        if book is not None:
            return _dec(_get(book, "spread_bps"))
        return None

    @staticmethod
    def _buy_price(intent: Any, book: Any, portfolio: Any, price: Any) -> Decimal | None:
        for v in (_get(intent, "limit_price"), price, _get(book, "best_ask") if book is not None else None):
            d = _dec(v)
            if d is not None and d > 0:
                return d
        pid = str(_get(intent, "product_id", ""))
        for name in ("best_asks", "mids"):
            m = _get(portfolio, name)
            if isinstance(m, Mapping) and m.get(pid) is not None:
                return D(m[pid])
        return None

    @staticmethod
    def _positions(portfolio: Any) -> list[Any]:
        return list(_get(portfolio, "all_positions") or _get(portfolio, "positions") or ())

    @staticmethod
    def _orders(portfolio: Any) -> list[Any]:
        return list(_get(portfolio, "all_open_orders") or _get(portfolio, "open_orders") or ())

    def _exposure(self, portfolio: Any, **flt: Any) -> Decimal:
        return spot_exposure(self._positions(portfolio), self._orders(portfolio), **flt)

    def check(self, intent: Any, portfolio: Any, account: Any = None, *, book: Any = None,
              spread_bps: Any = None, price: Any = None, now: datetime | None = None,
              record: bool = True) -> RiskDecision:
        """Approve (possibly less of) ``intent``; see the module docstring. ``book`` (an
        ``OrderBook``) or ``spread_bps`` enables the spread guard; ``price`` sizes a base-sized
        buy without a limit. With ``record`` an approved buy counts toward the order rate."""
        now = now or self._now()
        lim = self.limits
        side = str(_get(intent, "side", "")).lower()
        strategy = str(_get(intent, "strategy", ""))
        pid = str(_get(intent, "product_id", ""))
        q_req = _dec(_get(intent, "quote_size"))
        b_req = _dec(_get(intent, "base_size"))
        account = account if account is not None else portfolio

        def done(*, quote: Decimal | None = None, base: Decimal | None = None, reason: str,
                 binding: str | None = None, sell: bool = False) -> RiskDecision:
            d = RiskDecision(approved_quote=quote, approved_base=base, reason=reason, requested_quote=q_req,
                             requested_base=b_req, binding_limit=binding, reduces_risk=sell)
            if d.approved and record and not sell:  # sells never use up the buy budget
                self.record_order(now, strategy)
            if binding is not None:
                self._log("info", f"{strategy or '-'} {side} {pid}: {reason}", product_id=pid, strategy=strategy,
                          limit=binding, approved=d.approved)
            return d

        if side not in ("buy", "sell"):
            return done(quote=ZERO if q_req is not None else None, base=ZERO if q_req is None else None,
                        reason=f"side must be 'buy' or 'sell', not {side!r}", binding="side")

        # ---- sells reduce risk: always allowed (kill switch included), trimmed to what is held
        if side == "sell":
            if b_req is None or b_req <= 0:
                return done(base=ZERO, reason="sells need a positive base_size", binding="base_size", sell=True)
            held = sum((D(_get(p, "quantity", ZERO)) for p in self._positions(portfolio)
                        if _get(p, "product_id") == pid and _get(p, "strategy", "") == strategy), ZERO)
            committed = sum((D(_get(o, "remaining_base", ZERO)) for o in self._orders(portfolio)
                             if _get(o, "product_id") == pid and _get(o, "strategy", "") == strategy
                             and _get(o, "side") == "sell"), ZERO)
            avail = max(ZERO, held - committed)
            if avail <= 0:
                return done(base=ZERO, reason=f"nothing to sell: {strategy or '-'} holds no free {pid}",
                            binding="holdings", sell=True)
            note = " (kill switch on: sells still allowed)" if self.kill_switch else ""
            if b_req > avail:
                return done(base=avail, reason=f"trimmed sell {b_req} -> {avail} (held, net of resting sells){note}",
                            binding="holdings", sell=True)
            return done(base=b_req, reason=f"ok (sell reduces risk){note}", sell=True)

        # ---- buys
        if (q_req is None) == (b_req is None) or (q_req is not None and q_req <= 0) or (
                b_req is not None and b_req <= 0):
            return done(quote=ZERO if q_req is not None else None, base=ZERO if q_req is None else None,
                        reason="buys need exactly one positive quote_size or base_size", binding="size")

        def reject(reason: str, binding: str) -> RiskDecision:
            return done(quote=ZERO if q_req is not None else None, base=ZERO if b_req is not None else None,
                        reason=reason, binding=binding)

        if lim.max_orders_per_minute > 0 and \
                self.orders_last_minute(now, strategy=strategy) >= lim.max_orders_per_minute:
            return reject(f"max_orders_per_minute reached ({lim.max_orders_per_minute} for {strategy or '-'})",
                          "max_orders_per_minute")
        self.evaluate(account, now, portfolio=portfolio)
        if self.kill_switch:
            return reject(f"kill switch on ({self.kill_switch_reason or 'manual'}); no buys", "kill_switch")
        spread = self._spread_bps(book, spread_bps)
        if spread is not None and D(lim.max_spread_bps) > 0 and spread > D(lim.max_spread_bps):
            return reject(f"spread {spread:.1f} bps > max_spread_bps {lim.max_spread_bps:g}", "max_spread_bps")

        per_base: Decimal | None = None  # cost per unit of base (price + taker fee) for base-sized buys
        if q_req is not None:
            cost = q_req
        else:
            px = self._buy_price(intent, book, portfolio, price)
            if px is None:
                return reject("no price to value a base-sized buy (give limit_price, book or price)", "price")
            per_base = px * (1 + self.taker_rate)
            cost = b_req * per_base  # type: ignore[operator]
        min_usd = D(lim.min_trade_usd)
        if cost < min_usd:
            return reject(f"order ${cost:.2f} is below min_trade_usd ${min_usd}", "min_trade_usd")

        equity = D(_get(account, "equity", None) if _get(account, "equity", None) is not None
                   else _get(portfolio, "equity", ZERO))
        cash = D(_get(account, "cash", None) if _get(account, "cash", None) is not None
                 else _get(portfolio, "cash", ZERO))
        alloc = self.allocation_pct(strategy)
        headroom: dict[str, Decimal] = {
            "max_position_pct_per_product": (D(lim.max_position_pct_per_product) / 100 * equity
                                             - self._exposure(portfolio, product_id=pid)),
            "max_total_exposure_pct": D(lim.max_total_exposure_pct) / 100 * equity - self._exposure(portfolio),
            "max_strategy_allocation_pct": D(str(alloc)) / 100 * equity - self._exposure(portfolio, strategy=strategy),
            "min_cash_reserve": cash - D(lim.min_cash_reserve),
        }
        allowed, binding = cost, None
        for name, room in headroom.items():
            if room < allowed:
                allowed, binding = max(room, ZERO), name
        if binding is None:
            return done(quote=q_req, base=b_req, reason="ok")
        what = binding if binding != "max_strategy_allocation_pct" else f"{binding} ({alloc:g}% for {strategy or '-'})"
        allowed = allowed.quantize(_CENT, rounding=ROUND_FLOOR)
        if allowed < min_usd or allowed <= 0:
            return reject(f"{what}: headroom ${max(headroom[binding], ZERO):.2f} is below min_trade_usd ${min_usd}",
                          binding)
        if q_req is not None:
            return done(quote=allowed, reason=f"reduced ${q_req} -> ${allowed} by {what}", binding=binding)
        assert per_base is not None
        base = (allowed / per_base).quantize(_Q8, rounding=ROUND_FLOOR)
        if base <= 0:
            return reject(f"{what}: no headroom", binding)
        return done(base=base, reason=f"reduced {b_req} -> {base} by {what} (${allowed} headroom)", binding=binding)

    # ------------------------------------------------------------------ reporting

    def utilization(self, portfolio: Any, account: Any = None) -> dict[str, Any]:
        """``utilization`` block of ``GET /api/coinbase/risk`` (floats). ``portfolio`` should be
        the whole-account view (``broker.portfolio_view(None)``)."""
        lim = self.limits
        account = account if account is not None else portfolio
        equity = D(_get(account, "equity", ZERO))
        total = self._exposure(portfolio)
        products: set[str] = set()
        strategies: set[str] = set()
        for x in [*[p for p in self._positions(portfolio) if D(_get(p, "quantity", ZERO)) > 0],
                  *self._orders(portfolio)]:
            products.add(str(_get(x, "product_id", "")))
            strategies.add(str(_get(x, "strategy", "")))
        strategies |= set(self.strategy_allocations)

        def row(key: str, exposure: Decimal, limit: Decimal, **extra: Any) -> dict[str, Any]:
            return {"key": key, "exposure": f8(exposure), "limit": f8(limit),
                    "pct": f8(exposure / limit * 100) if limit > 0 else None, **extra}

        by_product = [row(p, self._exposure(portfolio, product_id=p),
                          D(lim.max_position_pct_per_product) / 100 * equity, product_id=p) for p in products]
        by_strategy = [row(s, self._exposure(portfolio, strategy=s), D(str(self.allocation_pct(s))) / 100 * equity,
                           strategy=s, allocation_pct=self.allocation_pct(s),
                           orders_last_minute=self.orders_last_minute(strategy=s)) for s in strategies]
        return {
            "total_exposure": f8(total),
            "total_exposure_pct": f8(total / equity * 100) if equity > 0 else 0.0,
            "by_product": sorted(by_product, key=lambda r: (-(r["exposure"] or 0), r["key"])),
            "by_strategy": sorted(by_strategy, key=lambda r: (-(r["exposure"] or 0), r["key"])),
            "orders_last_minute": self.orders_last_minute(),
            "daily_pnl": f8(self._daily_pnl(account, portfolio)),
        }

    def to_json(self, portfolio: Any, account: Any = None) -> dict[str, Any]:
        """Full ``GET /api/coinbase/risk`` payload."""
        return {"venue": VENUE, "limits": self.limits_json(), "utilization": self.utilization(portfolio, account),
                "kill_switch": self.kill_switch, "kill_switch_reason": self.kill_switch_reason}

    def reset(self) -> None:
        """Forget the order-rate window (e.g. after an account reset). Keeps the kill switch."""
        self._order_times.clear()
