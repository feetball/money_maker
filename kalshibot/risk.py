"""Risk manager (ARCHITECTURE.md §8): pre-trade limits, kill switch, Kelly sizing.

``RiskManager(settings.risk).check(intent, market, portfolio) -> RiskDecision``

Every limit from ``RiskSettings`` is enforced. Dollar limits size the order down (the
decision says which limit bound it) rather than rejecting outright:

======================================  ==================================================
``max_position_cost_per_market``        exposure in the ticker (all strategies) + new cost
``max_exposure_per_event``              exposure across the event's markets + new cost
``max_total_exposure_pct``              total exposure + new cost <= pct% of equity
``max_strategy_allocation_pct``         the strategy's exposure + new cost <= pct% of equity;
                                        pct = the strategy's own ``max_allocation_pct``
                                        (:class:`StrategyLimits`) when set, else this value
``min_cash_reserve``                    free cash - new cost >= reserve
``max_orders_per_minute``               orders approved in the trailing 60 s, **per
                                        strategy** (one strategy's burst never uses up
                                        another's budget)
``daily_loss_limit``                    equity - day-start equity <= -limit trips the kill switch
``min_seconds_to_close``                no entries when close_time - now < N s
``max_spread``                          no entries when YES ask - bid > max (or one-sided)
strategy ``daily_loss_limit``           the strategy's P&L today (``PortfolioView.
                                        strategy_daily_pnl``) <= -limit pauses **only that
                                        strategy's** entries until the next UTC day
======================================  ==================================================

*Exposure* = cost basis of open positions + cash reserved by open orders.
*New cost* per contract = limit price + a taker-fee bound ``0.07 * P * (1 - P)``.

Entries vs exits: contracts that **close** the strategy's existing opposite position (Kalshi
netting; ``sell`` included) are exits. Exits bypass the kill switch, close-time, spread,
exposure and cash limits (they reduce risk); only ``max_orders_per_minute`` applies. The
closable amount is the position **minus** what the strategy's resting orders on that market
already commit to closing it, so repeated resting exits cannot add up to a new, unchecked
opposite position.

Baskets: :meth:`RiskManager.check_basket` checks legs in order against a view that already
contains the earlier approved legs (their cost as reserved exposure, cash reduced), so every
dollar limit applies to the basket as a whole.

The kill switch blocks new entries until turned off with :meth:`RiskManager.set_kill_switch`.
A manual trip is sticky. A trip caused by ``daily_loss_limit`` is released at the next UTC day
when ``kill_switch_auto_release`` is on (the default; the backtester assumes the same). Its state,
the per-strategy pauses and runtime limit overrides persist in the store
(``kv['risk.kill_switch']``, ``kv['risk.strategy_paused']``, ``risk_limits``).

Per-strategy limits (:class:`StrategyLimits`: ``max_allocation_pct``, ``daily_loss_limit``) come
from :func:`strategy_limits` - the strategy class's ``risk_defaults`` overridden by
``strategies.<name>`` in the config - and are installed with :meth:`RiskManager.set_strategy_limits`
(the engine and the backtester do this), so an experimental strategy can never crowd out the
primary one.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from kalshibot.config import RiskSettings
from kalshibot.money import ONE, ZERO, D, f4
from kalshibot.paper.models import OPEN_ORDER_STATUSES, portfolio_exposure

if TYPE_CHECKING:
    from kalshibot.store import Store

__all__ = ["TAKER_FEE_BOUND", "RiskDecision", "RiskManager", "StrategyLimits", "kelly_count", "strategy_limits"]

log = logging.getLogger(__name__)

#: Upper bound of the per-contract taker fee coefficient (fee_multiplier <= 1 on Kalshi today).
TAKER_FEE_BOUND = Decimal("0.07")
KILL_SWITCH_KEY = "risk.kill_switch"
PAUSED_KEY = "risk.strategy_paused"


@dataclass(frozen=True, slots=True)
class StrategyLimits:
    """Per-strategy risk limits (``None`` = not set: the account-wide value applies)."""

    max_allocation_pct: float | None = None
    daily_loss_limit: Decimal | None = None  # dollars; 0 = off


def strategy_limits(settings: Any, classes: Mapping[str, Any]) -> dict[str, StrategyLimits]:
    """``{name: StrategyLimits}`` for the strategies in ``classes`` (name -> class or instance):
    the class's ``risk_defaults`` overridden by ``strategies.<name>.max_allocation_pct`` /
    ``daily_loss_limit`` in ``settings`` (a :class:`~kalshibot.config.Settings`)."""
    out: dict[str, StrategyLimits] = {}
    for name, cls in classes.items():
        defaults = dict(getattr(cls, "risk_defaults", None) or {})
        cfg = settings.strategy(name) if callable(getattr(settings, "strategy", None)) else None
        alloc = getattr(cfg, "max_allocation_pct", None)
        loss = getattr(cfg, "daily_loss_limit", None)
        if alloc is None:
            alloc = defaults.get("max_allocation_pct")
        if loss is None:
            loss = defaults.get("daily_loss_limit")
        out[name] = StrategyLimits(max_allocation_pct=float(alloc) if alloc is not None else None,
                                   daily_loss_limit=D(loss) if loss is not None else None)
    return out


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved_count: int
    reason: str = ""
    requested_count: int = 0
    closing_count: int = 0  # part of approved_count that closes an existing position
    binding_limit: str | None = None  # limit that reduced/rejected the order

    @property
    def approved(self) -> bool:
        return self.approved_count > 0

    @property
    def partial(self) -> bool:
        return 0 < self.approved_count < self.requested_count


def kelly_count(p: float | Decimal, price: float | Decimal, equity: float | Decimal, kelly_fraction: float | Decimal,
                cap: int | None = None, *, fee: float | Decimal = 0) -> int:
    """Contracts to buy at ``price`` given win probability ``p`` (binary contract paying $1).

    Full Kelly for a binary contract is ``f* = (p - price) / (1 - price)``; we stake
    ``kelly_fraction * f* * equity`` dollars and buy ``floor(stake / price)`` contracts,
    capped at ``cap``. ``fee`` (per contract) is added to the price in both places.
    Returns 0 when there is no edge or inputs are degenerate.
    """
    pr = D(price) + D(fee)
    pp, eq, kf = D(p), D(equity), D(kelly_fraction)
    if not (ZERO < pr < ONE) or not (ZERO <= pp <= ONE) or eq <= 0 or kf <= 0:
        return 0
    f_star = (pp - pr) / (ONE - pr)
    if f_star <= 0:
        return 0
    n = int((kf * f_star * eq) / pr)  # floor (positive)
    if cap is not None:
        n = min(n, int(cap))
    return max(n, 0)


def _get(obj: Any, name: str, default: Any = None) -> Any:
    v = getattr(obj, name, None)
    if v is None and isinstance(obj, Mapping):
        v = obj.get(name)
    return default if v is None else v


def _buy_side(x: Any) -> str:
    side = str(_get(x, "side", "yes"))
    return side if str(_get(x, "action", "buy")) == "buy" else ("no" if side == "yes" else "yes")


def _remaining(o: Any) -> int:
    try:
        return max(0, int(_get(o, "count", 0)) - int(_get(o, "filled_count", 0)))
    except (TypeError, ValueError):
        return 0


class RiskManager:
    """Pre-trade risk checks. ``settings`` may be ``Settings`` or ``RiskSettings``."""

    def __init__(self, settings: Any = None, *, store: Store | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        base = getattr(settings, "risk", settings)
        self.base: RiskSettings = base if isinstance(base, RiskSettings) else RiskSettings()
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        overrides = store.get_risk_limits() if store is not None else {}
        self.limits: RiskSettings = self._merge(overrides)
        self._order_times: dict[str, deque[datetime]] = {}  # strategy -> approved order times
        ks = store.get_kv(KILL_SWITCH_KEY) if store is not None else None
        self.kill_switch: bool = bool(ks and ks.get("on"))
        self.kill_switch_reason: str = (ks or {}).get("reason", "") if self.kill_switch else ""
        #: tripped by the daily loss limit (released at the next UTC day with auto release)
        self.kill_switch_auto: bool = bool(self.kill_switch and (ks or {}).get("auto"))
        self.kill_switch_day: str | None = (str((ks or {}).get("ts") or "")[:10] or None) if self.kill_switch else None
        self.strategy_limits: dict[str, StrategyLimits] = {}
        cfg = getattr(settings, "strategies", None)
        if isinstance(cfg, Mapping):  # explicit config values (class defaults: set_strategy_limits)
            self.strategy_limits = {k: v for k, v in strategy_limits(settings, {n: None for n in cfg}).items()
                                    if v != StrategyLimits()}
        paused = store.get_kv(PAUSED_KEY) if store is not None else None
        #: strategy -> (UTC day, reason): entries blocked until that day is over
        self._paused: dict[str, tuple[str, str]] = {
            str(k): (str(v[0]), str(v[1])) for k, v in (paused or {}).items()
            if isinstance(v, list | tuple) and len(v) >= 2}

    # ------------------------------------------------------------------ limits

    def _merge(self, overrides: Mapping[str, Any]) -> RiskSettings:
        data = self.base.model_dump()
        data.update({k: v for k, v in overrides.items() if k in RiskSettings.model_fields})
        return RiskSettings.model_validate(data)

    def update_limits(self, patch: Mapping[str, Any]) -> RiskSettings:
        """Validate and apply a partial update (``PATCH /api/risk``); persisted as overrides."""
        unknown = set(patch) - set(RiskSettings.model_fields)
        if unknown:
            raise ValueError(f"unknown risk limits: {sorted(unknown)}")
        data = self.limits.model_dump()
        data.update(patch)
        new = RiskSettings.model_validate(data)  # raises pydantic.ValidationError on bad values
        self.limits = new
        if self.store is not None:
            dumped = new.model_dump(mode="json")
            self.store.save_risk_limits({k: dumped[k] for k in patch})
        return new

    def limits_json(self) -> dict[str, Any]:
        return self.limits.model_dump(mode="json")

    def set_strategy_limits(self, limits: Mapping[str, StrategyLimits]) -> None:
        """Install per-strategy limits (replaces the entries of the strategies given)."""
        self.strategy_limits.update(dict(limits))

    def allocation_pct(self, strategy: str) -> float:
        """The strategy's exposure cap in % of equity (its own, else the account-wide fallback)."""
        sl = self.strategy_limits.get(strategy)
        if sl is not None and sl.max_allocation_pct is not None:
            return float(sl.max_allocation_pct)
        return float(self.limits.max_strategy_allocation_pct)

    def strategy_loss_limit(self, strategy: str) -> Decimal:
        sl = self.strategy_limits.get(strategy)
        return D(sl.daily_loss_limit) if sl is not None and sl.daily_loss_limit is not None else ZERO

    # ------------------------------------------------------------------ kill switch

    def set_kill_switch(self, on: bool, reason: str = "manual", *, auto: bool = False,
                        now: datetime | None = None) -> None:
        """Turn the kill switch on/off. ``auto``: tripped by the daily loss limit (released at the
        next UTC day when ``kill_switch_auto_release`` is on); manual trips stay on."""
        now = now or self._now()
        changed = on != self.kill_switch
        self.kill_switch = bool(on)
        self.kill_switch_reason = reason if on else ""
        self.kill_switch_auto = bool(on and auto)
        self.kill_switch_day = now.astimezone(UTC).date().isoformat() if on else None
        if self.store is not None:
            self.store.set_kv(KILL_SWITCH_KEY, {"on": self.kill_switch, "reason": self.kill_switch_reason,
                                                "ts": now.isoformat(), "auto": self.kill_switch_auto})
        if changed:
            self._log("warning" if on else "info", f"kill switch {'ON' if on else 'off'}: {reason}")

    def evaluate(self, portfolio: Any, now: datetime | None = None) -> bool:
        """Release a daily-loss trip from an earlier UTC day (``kill_switch_auto_release``), then trip
        the kill switch if today's P&L breaches ``daily_loss_limit``. Returns the switch state."""
        now = now or self._now()
        if (self.kill_switch and self.kill_switch_auto and self.limits.kill_switch_auto_release
                and self.kill_switch_day is not None and now.astimezone(UTC).date().isoformat() != self.kill_switch_day):
            self.set_kill_switch(False, f"daily loss limit trip of {self.kill_switch_day} released at the new UTC day",
                                 now=now)
        limit = D(self.limits.daily_loss_limit)
        if limit > 0 and not self.kill_switch:
            pnl = self._daily_pnl(portfolio)
            if pnl <= -limit:
                self.set_kill_switch(True, f"daily loss limit: today's P&L {pnl:.2f} <= -{limit}", auto=True, now=now)
        return self.kill_switch

    # ------------------------------------------------------------------ per-strategy pause

    @staticmethod
    def _strategy_daily_pnl(portfolio: Any, strategy: str) -> Decimal | None:
        m = _get(portfolio, "strategy_daily_pnl")
        if not isinstance(m, Mapping):
            return None
        v = m.get(strategy)
        return D(v) if v is not None else ZERO

    def _save_paused(self) -> None:
        if self.store is not None:
            self.store.set_kv(PAUSED_KEY, {k: list(v) for k, v in sorted(self._paused.items())})

    def strategy_paused(self, strategy: str, portfolio: Any = None, now: datetime | None = None) -> str | None:
        """Why ``strategy``'s entries are paused today (``None`` = not paused). A pause from an
        earlier UTC day is released; with ``portfolio``, a breach of the strategy's
        ``daily_loss_limit`` starts one."""
        now = now or self._now()
        day = now.astimezone(UTC).date().isoformat()
        hit = self._paused.get(strategy)
        if hit is not None and hit[0] != day:
            del self._paused[strategy]
            self._save_paused()
            self._log("info", f"{strategy}: daily loss pause of {hit[0]} released at the new UTC day",
                      strategy=strategy)
            hit = None
        if hit is None and portfolio is not None:
            limit = self.strategy_loss_limit(strategy)
            pnl = self._strategy_daily_pnl(portfolio, strategy) if limit > 0 else None
            if pnl is not None and pnl <= -limit:
                hit = (day, f"{strategy} daily loss limit: its P&L today {pnl:.2f} <= -{limit}; "
                            "its entries are paused until the next UTC day")
                self._paused[strategy] = hit
                self._save_paused()
                self._log("warning", hit[1], strategy=strategy)
        return hit[1] if hit is not None else None

    def resume_strategy(self, strategy: str) -> None:
        """Lift a strategy's daily-loss pause by hand."""
        if self._paused.pop(strategy, None) is not None:
            self._save_paused()

    # ------------------------------------------------------------------ rate limit

    def _now(self) -> datetime:
        n = self.clock()
        return n if n.tzinfo else n.replace(tzinfo=UTC)

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=60)
        for k in list(self._order_times):
            q = self._order_times[k]
            while q and q[0] <= cutoff:
                q.popleft()
            if not q:
                del self._order_times[k]

    def orders_last_minute(self, now: datetime | None = None, *, strategy: str | None = None) -> int:
        """Orders approved in the trailing 60 s: of ``strategy``, or of all strategies (``None``)."""
        self._prune(now or self._now())
        if strategy is not None:
            return len(self._order_times.get(strategy, ()))
        return sum(len(q) for q in self._order_times.values())

    def record_order(self, ts: datetime | None = None, strategy: str = "") -> None:
        self._order_times.setdefault(str(strategy or ""), deque()).append(ts or self._now())

    def _unrecord(self, strategy: str) -> None:
        q = self._order_times.get(str(strategy or ""))
        if q:
            q.pop()

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _daily_pnl(portfolio: Any) -> Decimal:
        v = _get(portfolio, "daily_pnl")
        if v is not None:
            return D(v)
        eq, start = _get(portfolio, "equity"), _get(portfolio, "day_start_equity")
        return D(eq) - D(start) if eq is not None and start is not None else ZERO

    @staticmethod
    def _positions(portfolio: Any) -> list[Any]:
        return list(_get(portfolio, "positions", ()) or ())

    @staticmethod
    def _orders(portfolio: Any) -> list[Any]:
        return list(_get(portfolio, "open_orders", ()) or ())

    def _exposure(self, portfolio: Any, **flt: Any) -> Decimal:
        return portfolio_exposure(self._positions(portfolio), self._orders(portfolio), **flt)

    def _log(self, level: str, message: str, **data: Any) -> None:
        log.log(getattr(logging, level.upper(), logging.INFO), "risk: %s", message)
        if self.store is not None:
            try:
                self.store.insert_log(level, "risk", message, data or None, ts=self._now())
            except Exception:  # pragma: no cover
                log.exception("failed to write risk log")

    # ------------------------------------------------------------------ the check

    def check(self, intent: Any, market: Any, portfolio: Any, *, book: Any = None, now: datetime | None = None,
              record: bool = True) -> RiskDecision:
        """Approve up to ``intent.count`` contracts.

        ``market`` is a :class:`~kalshibot.kalshi.models.Market` (``close_time``, ``spread``,
        ``event_ticker``); ``portfolio`` a ``PortfolioView`` (or anything with ``equity``,
        ``cash``, ``positions``, ``open_orders``, ``daily_pnl``). If ``book`` (an
        ``Orderbook``) is given its spread is used instead of the market snapshot's.
        With ``record`` (default) an approved order counts toward ``max_orders_per_minute``.
        """
        now = now or self._now()
        lim = self.limits
        try:
            requested = int(D(_get(intent, "count", 0)))
        except Exception:
            requested = 0
        ticker = str(_get(intent, "ticker", ""))
        strategy = str(_get(intent, "strategy", ""))
        side = str(_get(intent, "side", "yes"))
        action = str(_get(intent, "action", "buy"))
        buy_side = side if action == "buy" else ("no" if side == "yes" else "yes")
        try:
            limit_price = D(_get(intent, "limit_price"))
        except Exception:
            limit_price = None

        def done(n: int, reason: str, closing: int = 0, binding: str | None = None) -> RiskDecision:
            d = RiskDecision(approved_count=n, reason=reason, requested_count=requested, closing_count=closing,
                             binding_limit=binding)
            if n > 0 and record:
                self.record_order(now, strategy)
            if n < requested:
                self._log("info", f"{strategy or '-'} {ticker}: {reason}", ticker=ticker, strategy=strategy,
                          requested=requested, approved=n, limit=binding)
            return d

        if requested <= 0:
            return done(0, "count must be a positive whole number", binding="count")
        if limit_price is None or not (ZERO < limit_price < ONE):
            return done(0, "limit_price must be strictly inside (0, 1)", binding="limit_price")
        buy_price = limit_price if action == "buy" else ONE - limit_price

        # orders per minute, per strategy (applies to exits too)
        if lim.max_orders_per_minute > 0 and \
                self.orders_last_minute(now, strategy=strategy) >= lim.max_orders_per_minute:
            return done(0, f"max_orders_per_minute reached ({lim.max_orders_per_minute} for {strategy or '-'})",
                        binding="max_orders_per_minute")

        # exits: contracts that close this strategy's opposite position, net of what its
        # resting orders on this market already commit to closing it
        closable = 0
        for p in self._positions(portfolio):
            if (_get(p, "ticker") == ticker and _get(p, "strategy", "") == strategy
                    and int(_get(p, "count", 0)) > 0 and _get(p, "side") != buy_side):
                closable += int(_get(p, "count", 0))
        if closable:
            for o in self._orders(portfolio):
                if (_get(o, "ticker") == ticker and _get(o, "strategy", "") == strategy
                        and _buy_side(o) == buy_side and str(_get(o, "status", "open")) in OPEN_ORDER_STATUSES):
                    closable -= _remaining(o)
            closable = max(0, closable)
        closing = min(requested, closable)
        entry = requested - closing

        self.evaluate(portfolio, now)
        if entry == 0:
            return done(closing, "ok (closes existing position)", closing)

        block: tuple[str, str] | None = None
        paused = None if self.kill_switch else self.strategy_paused(strategy, portfolio, now)
        if self.kill_switch:
            block = ("kill_switch", f"kill switch on ({self.kill_switch_reason or 'manual'}); no new entries")
        elif paused is not None:
            block = ("strategy_daily_loss_limit", paused)
        else:
            close_time = _get(market, "close_time")
            if close_time is not None and lim.min_seconds_to_close > 0:
                left = (close_time - now).total_seconds()
                if left < lim.min_seconds_to_close:
                    block = ("min_seconds_to_close",
                             f"{left:.0f}s to close < min_seconds_to_close {lim.min_seconds_to_close:g}")
            if block is None:
                spread = _get(book, "spread") if book is not None else _get(market, "spread")
                if spread is None:
                    block = ("max_spread", "no two-sided quote (spread unknown)")
                elif D(spread) > D(lim.max_spread):
                    block = ("max_spread", f"spread {D(spread)} > max_spread {lim.max_spread}")
        if block is not None:
            reason = block[1] + (f"; approved {closing} closing contracts only" if closing else "")
            return done(closing, reason, closing, block[0])

        per = buy_price + TAKER_FEE_BOUND * buy_price * (ONE - buy_price)
        equity = D(_get(portfolio, "equity", ZERO))
        cash = D(_get(portfolio, "cash", ZERO))
        event_ticker = _get(market, "event_ticker") or ""
        headroom: dict[str, Decimal] = {
            "max_position_cost_per_market": (D(lim.max_position_cost_per_market)
                                             - self._exposure(portfolio, ticker=ticker)),
            "max_total_exposure_pct": D(lim.max_total_exposure_pct) / 100 * equity - self._exposure(portfolio),
            "max_strategy_allocation_pct": (D(str(self.allocation_pct(strategy))) / 100 * equity
                                            - self._exposure(portfolio, strategy=strategy)),
            "min_cash_reserve": cash - D(lim.min_cash_reserve),
        }
        if event_ticker:
            headroom["max_exposure_per_event"] = (D(lim.max_exposure_per_event)
                                                  - self._exposure(portfolio, event_ticker=event_ticker))
        allowed = entry
        binding: str | None = None
        for name, room in headroom.items():
            n = max(0, math.floor(room / per)) if room > 0 else 0
            if n < allowed:
                allowed, binding = n, name
        approved = closing + allowed
        if binding is None:
            return done(approved, "ok", closing)
        room = headroom[binding]
        what = binding
        if binding == "max_strategy_allocation_pct":
            what = f"{binding} ({self.allocation_pct(strategy):g}% for {strategy or '-'})"
        if approved == 0:
            reason = f"{what}: no headroom (${max(room, ZERO):.2f} left, ${per:.4f}/contract)"
        else:
            reason = f"reduced {requested} -> {approved} by {what} (${room:.2f} headroom)"
        return done(approved, reason, closing, binding)

    def check_basket(self, intents: Sequence[Any], markets: Sequence[Any], portfolio: Any, *,
                     books: Sequence[Any] | None = None, now: datetime | None = None) -> list[RiskDecision]:
        """Check basket legs in order, each against the portfolio **plus the legs approved
        before it** (their cost - price + taker-fee bound - as reserved exposure of a resting
        order, cash reduced; closing legs as pending closes), so per-market, per-event,
        strategy, total-exposure and cash limits hold for the basket as a whole. Nothing is
        recorded toward ``max_orders_per_minute`` (record the orders actually placed), but the
        legs do count against it within the basket."""
        now = now or self._now()
        orders = list(self._orders(portfolio))
        cash = D(_get(portfolio, "cash", ZERO))
        base = {k: _get(portfolio, k) for k in ("equity", "day_start_equity")}
        daily = self._daily_pnl(portfolio)
        out: list[RiskDecision] = []
        recorded: list[str] = []
        extra = {"strategy_daily_pnl": _get(portfolio, "strategy_daily_pnl")}
        try:
            for i, (intent, market) in enumerate(zip(intents, markets, strict=True)):
                view = SimpleNamespace(cash=cash, positions=tuple(self._positions(portfolio)),
                                       open_orders=tuple(orders), daily_pnl=daily, **base, **extra)
                d = self.check(intent, market, view, book=books[i] if books is not None else None, now=now,
                               record=True)
                out.append(d)
                if d.approved_count <= 0:
                    continue
                recorded.append(str(_get(intent, "strategy", "")))
                try:
                    lp = D(_get(intent, "limit_price"))
                except Exception:
                    continue
                buy_price = lp if str(_get(intent, "action", "buy")) == "buy" else ONE - lp
                per = buy_price + TAKER_FEE_BOUND * buy_price * (ONE - buy_price)
                cost = per * (d.approved_count - d.closing_count)
                orders.append(SimpleNamespace(
                    ticker=str(_get(intent, "ticker", "")), event_ticker=_get(market, "event_ticker") or "",
                    strategy=str(_get(intent, "strategy", "")), side=str(_get(intent, "side", "yes")),
                    action=str(_get(intent, "action", "buy")), count=d.approved_count, filled_count=0,
                    status="open", reserved=cost))
                cash -= cost
        finally:
            for strategy in reversed(recorded):
                self._unrecord(strategy)  # dry run: the caller records what it actually places
        return out

    # ------------------------------------------------------------------ reporting

    def utilization(self, portfolio: Any, *, titles: Mapping[str, str] | None = None) -> dict[str, Any]:
        """``utilization`` block of ``GET /api/risk`` (floats, 4 dp)."""
        lim = self.limits
        equity = D(_get(portfolio, "equity", ZERO))
        total = self._exposure(portfolio)
        events: set[str] = set()
        strategies: set[str] = set()
        live = [p for p in self._positions(portfolio) if int(_get(p, "count", 0)) > 0]
        for x in [*live, *self._orders(portfolio)]:
            if _get(x, "event_ticker"):
                events.add(_get(x, "event_ticker"))
            strategies.add(_get(x, "strategy", ""))

        def row(key: str, exposure: Decimal, limit: Decimal | None, **extra: Any) -> dict[str, Any]:
            return {"key": key, "exposure": f4(exposure), "limit": f4(limit) if limit is not None else None,
                    "pct": f4(exposure / limit * 100) if limit else None,
                    "title": (titles or {}).get(key), **extra}

        strategies |= set(self._paused)
        daily = _get(portfolio, "strategy_daily_pnl")
        daily = daily if isinstance(daily, Mapping) else {}

        def strat_row(s: str) -> dict[str, Any]:
            loss = self.strategy_loss_limit(s)
            return row(s, self._exposure(portfolio, strategy=s), D(str(self.allocation_pct(s))) / 100 * equity,
                       strategy=s, allocation_pct=self.allocation_pct(s),
                       orders_last_minute=self.orders_last_minute(strategy=s),
                       daily_pnl=f4(D(daily.get(s, ZERO))) if daily else None,
                       daily_loss_limit=f4(loss) if loss > 0 else None,
                       paused=self._paused.get(s, (None, None))[1])

        return {
            "total_exposure": f4(total),
            "total_exposure_pct": f4(total / equity * 100) if equity > 0 else 0.0,
            "by_event": sorted((row(e, self._exposure(portfolio, event_ticker=e), D(lim.max_exposure_per_event),
                                    event_ticker=e) for e in events), key=lambda r: -r["exposure"]),
            "by_strategy": sorted((strat_row(s) for s in strategies), key=lambda r: (-r["exposure"], r["key"])),
            "orders_last_minute": self.orders_last_minute(),
            "daily_pnl": f4(self._daily_pnl(portfolio)),
        }

    def to_json(self, portfolio: Any, **kw: Any) -> dict[str, Any]:
        """Full ``GET /api/risk`` payload."""
        return {"limits": self.limits_json(), "utilization": self.utilization(portfolio, **kw),
                "kill_switch": self.kill_switch, "kill_switch_reason": self.kill_switch_reason}

    def reset(self) -> None:
        """Forget the order-rate window and the per-strategy daily-loss pauses (e.g. after an
        account reset: the P&L they came from is gone). Does not touch the kill switch."""
        self._order_times.clear()
        if self._paused:
            self._paused.clear()
            self._save_paused()
