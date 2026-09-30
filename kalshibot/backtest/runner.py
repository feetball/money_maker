"""Backtest runner (ARCHITECTURE.md §10): replays history through the **same** ``Strategy`` classes.

``run_backtest(strategy, params, start, end, starting_balance, settings, **options)`` builds a
:class:`BacktestContext` (a ``StrategyContext``) at walk-forward snapshot times and routes every
intent through the real :class:`~kalshibot.risk.RiskManager` (optional) and the real
:class:`~kalshibot.paper.broker.PaperBroker` running on :class:`~kalshibot.backtest.data.ReplayMarketData`
(``kalshibot.paper.sim`` market data driven by a manual clock). Fills, fees (``fees.py``, the
broker's per-order rounding), positions and settlement are therefore the paper account's own code.

Data (``data`` option, :mod:`kalshibot.backtest.data`)
    ``hourly`` - research/data hourly candles (Adapter A; needs pyarrow), UTC hour grid;
    ``minute`` - research/crypto_fv 1-minute candles + Coinbase spot (Adapter B), UTC minute grid;
    ``auto`` (default) - ``minute`` when every series of the strategy's ``UniverseSpec.series_tickers``
    has minute data, else ``hourly``.

Clock and look-ahead
    At each grid time ``t`` the context shows the strategy's universe (its ``UniverseSpec``, as
    the engine's ``markets_for``) quoted from the last candle that **ended** at or before ``t``,
    synthetic books from the same candle, and the replay feeds (``ctx.feeds``) clocked at ``t``.
    Settlements are applied at each market's recorded ``settlement_ts`` (the clock never moves
    backwards). If a tick produced order intents, the strategy ticks again every
    ``tick_interval_s`` (its own, else ``engine.tick_s``) until a tick produces none or the next
    data time is reached, like the live cadence (e.g. intents over ``max_intents_per_tick`` wait
    for the next tick).

Execution (``fill`` option)
    ``same``     - orders reach the book at the decision time ``t`` (IOC at the strategy's limit);
                   for BTC15M this is the live-equivalent replay (live decides and sends the IOC
                   on the same fresh book);
    ``next``     - orders reach the book ``latency_s`` (60) later, at the strategy's limit: an ask
                   that moved above the limit leaves the IOC unfilled;
    ``next_ask`` - the crypto research convention: fill at the ask ``latency_s`` later whatever it
                   is (the IOC limit is lifted to the top of the price grid; risk still checks the
                   strategy's own limit and count);
    ``auto``     - the dataset's research convention: ``same`` for hourly, ``next_ask`` for minute.
    With a latency the risk check runs at the **decision** time ``t`` (market, book, portfolio
    and kill switch as of ``t``, like the live engine), and the approved orders are queued as
    timed events executed at ``t + latency_s`` in clock order with the settlements: the clock
    never runs ahead of the tick being evaluated, so ``ctx`` (books, feeds, portfolio) never
    shows the future, whatever the latency (also when it exceeds the data step).
    Books have ``book_size`` (250) contracts per level (candles carry no depth). Resting (GTC)
    orders can only fill when a later book crosses them (no trade tape); cancels are ignored.

Other options: ``risk`` (True: the live RiskManager with ``settings.risk`` and the strategy's own
limits, :func:`kalshibot.risk.strategy_limits`; a tripped kill switch is released at the next UTC
day, like ``risk.kill_switch_auto_release`` live),
``max_intents_per_tick`` (50, as the engine), ``snapshot_s`` (3600, equity-curve spacing),
``book_size``, ``latency_s``, ``data_dir`` (default ``research/``), ``universe`` (hourly:
``research`` | ``all``), ``categories`` (hourly data subset), ``n_boot`` (2000), ``seed``. Defaults
can also come from an optional ``backtest:`` config section; keyword arguments win.

Positions still open at ``end`` are held to their recorded settlement (the equity curve runs
until the last one settles); ``metrics.details.settled_after_end`` counts them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import heapq
import logging
import math
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import numpy as np

from kalshibot.analytics import bootstrap_ratio_ci, drawdown
from kalshibot.backtest.data import (
    BacktestDataError,
    ReplayDataset,
    ReplayMarketData,
    load_dataset,
    minute_series_available,
)
from kalshibot.feeds import FeedRegistry
from kalshibot.fees import trading_fee
from kalshibot.kalshi.models import Market, Orderbook, Series
from kalshibot.money import ONE, D, max_valid_price, min_valid_price
from kalshibot.paper.broker import PaperBroker
from kalshibot.paper.models import Fill, Order, PortfolioView, Settlement
from kalshibot.paper.sim import ManualClock
from kalshibot.risk import RiskManager, strategy_limits
from kalshibot.strategies.base import CancelIntent, OrderIntent, Strategy, UniverseSpec, intents_list

__all__ = ["DEFAULT_OPTIONS", "FILL_MODES", "BacktestContext", "BacktestResult", "run_backtest",
           "run_backtest_async"]

log = logging.getLogger(__name__)

FILL_MODES = ("auto", "same", "next", "next_ask")
_NUM = re.compile(r"[-+]?\$?\d+(?:\.\d+)?")

DEFAULT_OPTIONS: dict[str, Any] = {
    "data": "auto",
    "data_dir": None,
    "fill": "auto",
    "latency_s": 60,
    "book_size": 250,
    "risk": True,
    "universe": "research",
    "categories": None,
    "max_intents_per_tick": 50,
    "snapshot_s": 3600,
    "n_boot": 2000,
    "seed": 0,
    "force": False,
}


# --------------------------------------------------------------------------- helpers


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=UTC)


def _parse_when(value: Any, *, end: bool = False) -> datetime | None:
    """``YYYY-MM-DD`` (a whole UTC day: ``end`` dates are inclusive), ISO datetime, or datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        d = datetime(value.year, value.month, value.day, tzinfo=UTC)
        return d + timedelta(days=1) if end else d
    s = str(value).strip()
    if len(s) == 10:
        try:
            d = datetime.fromisoformat(s).replace(tzinfo=UTC)
        except ValueError as e:
            raise ValueError(f"invalid date {value!r} (use YYYY-MM-DD)") from e
        return d + timedelta(days=1) if end else d
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"invalid date/time {value!r}") from e
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _f(x: Any, nd: int = 6) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if math.isfinite(v) else None


def _options(settings: Any, overrides: Mapping[str, Any]) -> dict[str, Any]:
    opts = dict(DEFAULT_OPTIONS)
    section = getattr(settings, "backtest", None) if settings is not None else None
    if section is None and settings is not None:
        section = (getattr(settings, "model_extra", None) or {}).get("backtest")
    if section is not None and not isinstance(section, Mapping):
        section = section.model_dump() if hasattr(section, "model_dump") else vars(section)
    for k, v in dict(section or {}).items():
        if k in opts:
            opts[k] = v
    unknown = sorted(set(overrides) - set(opts))
    if unknown:
        raise ValueError(f"unknown backtest option(s) {unknown} (known: {sorted(opts)})")
    opts.update(overrides)
    if opts["fill"] not in FILL_MODES:
        raise ValueError(f"fill must be one of {FILL_MODES}, got {opts['fill']!r}")
    for k in ("latency_s", "book_size", "max_intents_per_tick", "snapshot_s", "n_boot", "seed"):
        opts[k] = int(float(opts[k]))
    if opts["book_size"] <= 0 or opts["max_intents_per_tick"] <= 0 or opts["snapshot_s"] <= 0:
        raise ValueError("book_size, max_intents_per_tick and snapshot_s must be positive")
    if opts["latency_s"] < 0:
        raise ValueError("latency_s must be >= 0")
    for k in ("risk", "force"):
        v = opts[k]
        opts[k] = v.strip().lower() in ("1", "true", "yes", "on") if isinstance(v, str) else bool(v)
    if isinstance(opts["categories"], str):
        opts["categories"] = [c.strip() for c in opts["categories"].split(",") if c.strip()]
    return opts


class _QuietBroker(PaperBroker):
    """The real broker; its per-event log lines are counted instead of logged (a replay has thousands)."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        self.log_counts: Counter[str] = Counter()
        super().__init__(*a, **kw)

    def _log(self, level: str, kind: str, message: str, **data: Any) -> None:
        self.log_counts[f"{level}:{kind}"] += 1
        if level in ("error", "critical"):
            log.warning("backtest broker %s: %s", kind, message)


class _QuietRisk(RiskManager):
    def _log(self, level: str, message: str, **data: Any) -> None:
        pass


# --------------------------------------------------------------------------- context


class BacktestContext:
    """:class:`~kalshibot.strategies.base.StrategyContext` over the replay at ``now``."""

    def __init__(self, run: _Replay, now: datetime, markets: Mapping[str, Market]) -> None:
        self._run = run
        self.strategy = run.name
        self.params = dict(run.strategy.params)
        self.now = now
        self.markets = markets
        self.events: Mapping[str, Any] = {}
        self.feeds = run.feeds
        self.cancels: list[CancelIntent] = []
        self._portfolio: PortfolioView | None = None

    @property
    def portfolio(self) -> PortfolioView:
        if self._portfolio is None:
            self._portfolio = self._run.broker.portfolio()
        return self._portfolio

    async def series(self, series_ticker: str) -> Series:
        return await self._run.md.series(series_ticker)

    def _ts(self) -> int:
        return int(self.now.timestamp())

    async def orderbook(self, ticker: str, max_age_s: float | None = None) -> Orderbook:
        """The synthetic book at ``now`` (``max_age_s`` is accepted and ignored: always current)."""
        i = self._run.ds.index.get(ticker)
        if i is None:
            return await self._run.md.orderbook(ticker)  # raises not found
        return self._run.md.book_at(i, self._ts())

    async def orderbooks(self, tickers: Iterable[str]) -> dict[str, Orderbook]:
        out: dict[str, Orderbook] = {}
        for t in dict.fromkeys(tickers):
            i = self._run.ds.index.get(t)
            if i is not None:
                out[t] = self._run.md.book_at(i, self._ts())
        return out

    async def market(self, ticker: str, fresh: bool = False) -> Market:
        i = self._run.ds.index.get(ticker)
        if i is None:
            return await self._run.md.market(ticker)  # raises not found
        return self._run.md.market_at(i, self._ts())

    async def event(self, event_ticker: str) -> None:
        return None

    def fee_params(self, market: Market) -> tuple[str, Decimal]:
        return self._run.md.fee_params(market)

    def fee(self, market: Market, price: Any, count: Any, is_taker: bool = True) -> Decimal:
        ft, mult = self.fee_params(market)
        return trading_fee(D(price), D(count), is_taker=is_taker, fee_type=ft, fee_multiplier=mult,
                           precision=self._run.broker.precision)

    def log(self, msg: str, **data: Any) -> None:
        self._run.note_log(msg, data)

    def cancel(self, order_id: Any = None, *, ticker: str | None = None, reason: str = "") -> CancelIntent:
        c = CancelIntent(order_id=getattr(order_id, "id", order_id), ticker=ticker, reason=reason,
                         strategy=self.strategy)
        self.cancels.append(c)
        return c


# --------------------------------------------------------------------------- result


@dataclasses.dataclass
class BacktestResult:
    """What ``POST /api/backtests`` stores (``to_json``): metrics, equity curve, trades, by month."""

    metrics: dict[str, Any]
    equity_curve: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    by_month: list[dict[str, Any]]
    signals: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"metrics": self.metrics, "equity_curve": self.equity_curve, "trades": self.trades,
                "by_month": self.by_month}


# --------------------------------------------------------------------------- the replay


class _Replay:
    def __init__(self, strategy: Strategy, ds: ReplayDataset, *, start: int, end: int, starting_balance: Decimal,
                 settings: Any, opts: Mapping[str, Any]) -> None:
        self.strategy = strategy
        self.name = strategy.name or type(strategy).__name__
        self.ds = ds
        self.opts = dict(opts)
        self.start, self.end = start, end
        self.step = ds.step_s
        fill = opts["fill"]
        self.fill = ds.default_fill if fill == "auto" else fill
        self.latency = int(opts["latency_s"]) if self.fill in ("next", "next_ask") else 0
        self.clock = ManualClock(_dt(start))
        self.md = ReplayMarketData(ds, self.clock, book_size=opts["book_size"])
        self.broker = _QuietBroker(self.md, None, settings=settings, starting_balance=starting_balance,
                                   clock=self.clock, log_to_store=False, max_trade_polls_per_pass=0)
        self.risk: RiskManager | None = _QuietRisk(settings, store=None, clock=self.clock) if opts["risk"] else None
        if self.risk is not None:  # the strategy's own allocation / daily loss limit, as live
            self.risk.set_strategy_limits(strategy_limits(settings, {self.name: type(strategy)}))
        self.feeds = ds.make_feeds(self.clock) if ds.make_feeds is not None else FeedRegistry()
        self.spec: UniverseSpec = strategy.universe()
        eng = getattr(settings, "engine", None)
        tick = getattr(strategy, "tick_interval_s", None) or getattr(eng, "tick_s", 30) or 30
        self.tick_s = max(1, int(math.ceil(float(tick))))
        self.max_intents = int(opts["max_intents_per_tick"])
        # bookkeeping
        self.heap: list[tuple[int, str]] = []
        self.scheduled: set[str] = set()
        #: orders decided at t, executed at t + latency: (te, seq, [planned order/basket])
        self.pending: list[tuple[int, int, list[tuple[Any, ...]]]] = []
        self._seq = 0
        self.entries: dict[tuple[str, str], dict[str, Any]] = {}
        self.orders: dict[int, Order] = {}
        self.trades: list[dict[str, Any]] = []
        self.curve: list[dict[str, Any]] = []
        self.stats: Counter[str] = Counter()
        self.reasons: Counter[str] = Counter()
        self.logs: Counter[str] = Counter()
        self.log_samples: list[str] = []
        self.signals: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.kill_day: str | None = None
        self.kill_days = 0
        self.broker.subscribe(self._on_event)

    # -- clock / settlement ---------------------------------------------------------------

    @property
    def now(self) -> int:
        return int(self.clock.now.timestamp())

    def _set(self, ts: int) -> None:
        if ts > self.now:
            self.clock.set(_dt(ts))

    async def advance_to(self, target: int) -> None:
        """Apply every settlement and queued execution due by ``target``, each at its own time and
        in clock order (a settlement first on a tie), then move the clock there."""
        while True:
            s_ts = self.heap[0][0] if self.heap and self.heap[0][0] <= target else None
            e_ts = self.pending[0][0] if self.pending and self.pending[0][0] <= target else None
            if s_ts is None and e_ts is None:
                break
            if s_ts is not None and (e_ts is None or s_ts <= e_ts):
                ts, ticker = heapq.heappop(self.heap)
                self.scheduled.discard(ticker)
                self._set(ts)
                i = self.ds.index[ticker]
                await self.broker.settle_market(self.md.market_at(i, max(ts, self.now)))
            else:
                te, _, planned = heapq.heappop(self.pending)
                self._set(te)
                await self._execute_planned(planned)
        self._set(target)

    def _schedule(self, ticker: str) -> None:
        if ticker in self.scheduled:
            return
        i = self.ds.index.get(ticker)
        if i is None:
            return
        self.scheduled.add(ticker)
        heapq.heappush(self.heap, (int(self.ds.settle_ts[i]), ticker))

    # -- broker events -> trades --------------------------------------------------------------

    def _on_event(self, kind: str, obj: Any) -> None:
        if kind == "order":
            self.orders[obj.id] = obj
        elif kind == "fill":
            f: Fill = obj
            self._schedule(f.ticker)
            e = self.entries.setdefault((f.strategy, f.ticker), {"orders": [], "ts": f.ts})
            if f.order_id not in e["orders"]:
                e["orders"].append(f.order_id)
            if f.strategy == self.name:
                try:
                    self.strategy.on_fill(f)
                except Exception as ex:  # a hook must not stop the replay (engine behaviour)
                    self._error(f"on_fill: {type(ex).__name__}: {ex}")
        elif kind == "settlement":
            s: Settlement = obj
            self._trade(s)
            if s.strategy == self.name:
                try:
                    self.strategy.on_settlement(s)
                except Exception as ex:
                    self._error(f"on_settlement: {type(ex).__name__}: {ex}")

    def _trade(self, s: Settlement) -> None:
        e = self.entries.get((s.strategy, s.ticker)) or {"orders": [], "ts": s.opened_at}
        order = self.orders.get(e["orders"][0]) if e["orders"] else None
        i = self.ds.index.get(s.ticker)
        n = int(s.count)
        self.trades.append({
            "ts": _iso(s.opened_at or e.get("ts")),
            "ticker": s.ticker,
            "event_ticker": s.event_ticker,
            "series_ticker": self.ds.series_of[i] if i is not None else "",
            "category": self.ds.category(i) if i is not None else "",
            "side": s.side,
            "count": n,
            "price": _f(s.cost_basis / n) if n else None,
            "fee": _f(s.fees),
            "result": s.result,
            "payout": _f(s.payout),
            "pnl": _f(s.pnl),
            "pnl_per_contract": _f(s.pnl / n) if n else None,
            "settled_at": _iso(s.ts),
            "kind": s.kind,
            "fair_value": _f(s.fair_value),
            "expected_edge": _f(s.expected_edge / n) if s.expected_edge is not None and n else None,
            "reason": order.reason if order is not None else "",
            "order_ids": list(e["orders"]),
        })
        if s.kind == "settlement":
            self.entries.pop((s.strategy, s.ticker), None)

    # -- logging ------------------------------------------------------------------------------

    def note_log(self, msg: str, data: Mapping[str, Any]) -> None:
        key = str(data.get("skip") or data.get("kind") or "log")
        self.logs[key] += 1
        if len(self.log_samples) < 50:
            self.log_samples.append(f"{_iso(self.clock.now)} {msg}")

    def _error(self, msg: str) -> None:
        self.stats["strategy_errors"] += 1
        if len(self.errors) < 20:
            self.errors.append(f"{_iso(self.clock.now)} {msg}")

    def _signal(self, intent: Any, decision: str, why: str, order: Order | None = None) -> None:
        self.stats[f"signals_{decision}"] += 1
        if why and decision != "executed":
            self.reasons[_NUM.sub("#", why)[:100]] += 1  # grouped: numbers masked
        if len(self.signals) < 2000:
            self.signals.append({
                "ts": _iso(self.clock.now), "ticker": getattr(intent, "ticker", ""),
                "side": getattr(intent, "side", ""),
                "count": getattr(intent, "count", None), "limit_price": _f(getattr(intent, "limit_price", None)),
                "decision": decision, "decision_reason": why, "order_id": order.id if order is not None else None,
                "filled": order.filled_count if order is not None else 0,
                "avg_fill_price": _f(order.avg_fill_price) if order is not None else None})

    # -- ticking --------------------------------------------------------------------------

    def _release_kill_switch(self) -> None:
        """A kill switch tripped on an earlier UTC day is released (nobody can do it by hand)."""
        if self.risk is not None and self.risk.kill_switch and self.kill_day is not None \
                and self.clock.now.date().isoformat() != self.kill_day:
            self.risk.set_kill_switch(False, "backtest: new UTC day")
            self.kill_day = None

    def _note_kill_switch(self) -> None:
        if self.risk is None:
            return
        if not self.risk.kill_switch:  # released (also by risk.kill_switch_auto_release)
            self.kill_day = None
        elif self.kill_day is None:
            self.kill_day = self.clock.now.date().isoformat()
            self.kill_days += 1

    async def tick(self, t: int) -> tuple[int, int]:
        """One strategy tick at ``t``; returns (order intents, execution time)."""
        markets = self.md.snapshot(self.spec, t)
        if not markets:
            return 0, t
        self.stats["ticks"] += 1
        ctx = BacktestContext(self, _dt(t), markets)
        try:
            items = intents_list(await self.strategy.on_tick(ctx)) + list(ctx.cancels)
        except Exception as e:
            self._error(f"on_tick: {type(e).__name__}: {e}")
            log.debug("backtest on_tick failed", exc_info=True)
            return 0, t
        cancels = [x for x in items if isinstance(x, CancelIntent)]
        if cancels:
            self.stats["cancels_ignored"] += len(cancels)
        intents = [x for x in items if not isinstance(x, CancelIntent)]
        if not intents:
            return 0, t
        intents = self._cap(intents)
        if self.latency <= 0:
            await self.execute(intents, t)
            return len(intents), t
        te = t + self.latency  # decided (risk-checked) now, executed at te in clock order
        planned = self._decide(intents, t)
        if planned:
            self._seq += 1
            heapq.heappush(self.pending, (te, self._seq, planned))
        return len(intents), te

    def _cap(self, intents: list[Any]) -> list[Any]:
        if len(intents) <= self.max_intents:
            return intents
        groups: dict[str, list[Any]] = {}
        for i, raw in enumerate(intents):
            gid = getattr(raw, "group_id", None) if not isinstance(raw, Mapping) else raw.get("group_id")
            groups.setdefault(f"g:{gid}" if gid else f"s:{i}", []).append(raw)
        kept: list[Any] = []
        for legs in groups.values():
            if len(kept) + len(legs) > self.max_intents:
                break
            kept.extend(legs)
        self.stats["intents_over_cap"] += len(intents) - len(kept)
        return kept

    # -- execution --------------------------------------------------------------------------

    def _normalize(self, raw: Any) -> tuple[OrderIntent | None, str]:
        if isinstance(raw, OrderIntent):
            intent = raw
        elif isinstance(raw, Mapping):
            try:
                intent = OrderIntent(**{k: v for k, v in raw.items() if k in OrderIntent.__dataclass_fields__})
            except TypeError as e:
                return None, f"invalid intent: {e}"
        else:
            return None, f"invalid intent object {type(raw).__name__}"
        if not intent.strategy:
            intent.strategy = self.name
        elif intent.strategy != self.name:
            return intent, f"intent.strategy {intent.strategy!r} does not match {self.name!r}"
        problems = intent.problems()
        if problems:
            return intent, "invalid intent: " + "; ".join(problems)
        if intent.replaces is not None:
            return intent, "cancel/replace is not supported in backtests"
        if intent.ticker not in self.ds.index:
            return intent, f"unknown market {intent.ticker}"
        return intent, ""

    def _aggressive(self, m: Market, intent: OrderIntent) -> Decimal:
        """Limit that takes whatever the book offers (``next_ask``): the top of the grid for the side bought."""
        top_yes = max_valid_price(m.price_ranges)
        top_no = ONE - min_valid_price(m.price_ranges)
        buy_top = top_yes if intent.buy_side == "yes" else top_no
        return buy_top if intent.action == "buy" else ONE - buy_top

    async def execute(self, raws: list[Any], te: int) -> None:
        await self.advance_to(te)
        groups: dict[str, list[Any]] = {}
        for i, raw in enumerate(raws):
            gid = getattr(raw, "group_id", None) if not isinstance(raw, Mapping) else raw.get("group_id")
            groups.setdefault(f"g:{gid}" if gid else f"s:{i}", []).append(raw)
        for key, legs in groups.items():
            if key.startswith("g:"):
                await self._basket(legs)
            else:
                await self._one(legs[0])

    async def _one(self, raw: Any) -> None:
        intent, problem = self._normalize(raw)
        if intent is None or problem:
            self._signal(intent or raw, "rejected", problem)
            return
        i = self.ds.index[intent.ticker]
        m = self.md.market_at(i)
        count = intent.count
        risk_note = ""
        if self.risk is not None:
            self._release_kill_switch()
            d = self.risk.check(intent, m, self.broker.portfolio(), book=self.md.book_at(i))
            self._note_kill_switch()
            if d.approved_count <= 0:
                self.stats["risk_rejected"] += 1
                self._signal(intent, "rejected", f"risk: {d.reason}")
                return
            if d.approved_count < count:
                self.stats["risk_trimmed"] += 1
                risk_note = f"risk: {d.reason}"
            count = d.approved_count
        await self._place_one(intent, count, risk_note)

    async def _place_one(self, intent: OrderIntent, count: int, risk_note: str) -> None:
        place = intent
        if self.fill == "next_ask" and intent.tif == "ioc":
            m = self.md.market_at(self.ds.index[intent.ticker])
            place = dataclasses.replace(intent, limit_price=self._aggressive(m, intent))
        order = await self.broker.place_order(place, count=count)
        self.orders[order.id] = order
        decision = order.decision
        if risk_note and decision == "executed":
            decision = "partial"
        why = "; ".join(x for x in (risk_note, order.status_reason if decision != "executed" else "") if x)
        self._signal(intent, decision, why, order)

    async def _basket(self, raws: list[Any]) -> None:
        legs: list[OrderIntent] = []
        for raw in raws:
            intent, problem = self._normalize(raw)
            if intent is None or problem:
                for r in raws:
                    self._signal(r, "rejected", f"basket rejected: {problem or 'invalid intent'}")
                return
            legs.append(intent)
        counts = [leg.count for leg in legs]
        if self.risk is not None:
            self._release_kill_switch()
            markets = [self.md.market_at(self.ds.index[leg.ticker]) for leg in legs]
            books = [self.md.book_at(self.ds.index[leg.ticker]) for leg in legs]
            ds = self.risk.check_basket(legs, markets, self.broker.portfolio(), books=books)
            self._note_kill_switch()
            if any(d.approved_count < leg.count for leg, d in zip(legs, ds, strict=True)):
                self.stats["risk_rejected"] += len(legs)
                for leg in legs:
                    self._signal(leg, "rejected", "risk: basket trimmed (all-or-none)")
                return
            counts = [d.approved_count for d in ds]
        await self._place_basket(legs, counts)

    async def _place_basket(self, legs: list[OrderIntent], counts: list[int]) -> None:
        orders = await self.broker.place_basket(legs, all_or_none=True, counts=counts)
        for leg, o in zip(legs, orders, strict=True):
            self.orders[o.id] = o
            if self.risk is not None and o.status != "rejected":
                self.risk.record_order(strategy=leg.strategy)  # as the engine: placed legs count
            self._signal(leg, o.decision, o.status_reason if o.decision != "executed" else "", o)

    # -- decide now, execute later (latency) ------------------------------------------------------

    def _pending_view(self) -> Any:
        """The portfolio at the decision time plus the orders decided earlier and not yet executed
        (as resting exposure), so every risk limit sees them - as it would see placed orders live."""
        pf = self.broker.portfolio()
        extra: list[Any] = []
        cash = pf.cash
        for _, _, planned in self.pending:
            for item in planned:
                legs = item[1] if item[0] == "basket" else [item[1]]
                counts = item[2] if item[0] == "basket" else [item[2]]
                for leg, n in zip(legs, counts, strict=True):
                    cost = self._cost_bound(leg) * n
                    extra.append(self._synthetic(leg, n, cost))
                    cash -= cost
        if not extra:
            return pf
        return dataclasses.replace(pf, cash=cash, open_orders=tuple(pf.open_orders) + tuple(extra))

    def _synthetic(self, leg: OrderIntent, n: int, cost: Decimal) -> Any:
        i = self.ds.index.get(leg.ticker)
        return SimpleNamespace(ticker=leg.ticker, event_ticker=self.ds.event_of[i] if i is not None else "",
                               strategy=leg.strategy, side=leg.side, action=leg.action, count=n, filled_count=0,
                               status="open", reserved=cost)

    @staticmethod
    def _cost_bound(leg: OrderIntent) -> Decimal:
        bp = leg.buy_price
        return bp + Decimal("0.07") * bp * (ONE - bp)

    def _decide(self, raws: list[Any], t: int) -> list[tuple[Any, ...]]:
        """Risk-check this tick's intents at the decision time ``t`` (market, book, portfolio as of
        ``t``; each intent sees the ones approved before it). Returns what to execute later."""
        groups: dict[str, list[Any]] = {}
        for i, raw in enumerate(raws):
            gid = getattr(raw, "group_id", None) if not isinstance(raw, Mapping) else raw.get("group_id")
            groups.setdefault(f"g:{gid}" if gid else f"s:{i}", []).append(raw)
        planned: list[tuple[Any, ...]] = []
        now = _dt(t)
        view = self._pending_view() if self.risk is not None else None
        for key, legs_raw in groups.items():
            legs: list[OrderIntent] = []
            bad = ""
            for raw in legs_raw:
                intent, problem = self._normalize(raw)
                if intent is None or problem:
                    bad = problem or "invalid intent"
                    if not key.startswith("g:"):
                        self._signal(intent or raw, "rejected", problem)
                    break
                legs.append(intent)
            if bad:
                if key.startswith("g:"):
                    for r in legs_raw:
                        self._signal(r, "rejected", f"basket rejected: {bad}")
                continue
            idx = [self.ds.index[leg.ticker] for leg in legs]
            counts = [leg.count for leg in legs]
            note = ""
            if self.risk is not None:
                self._release_kill_switch()
                markets = [self.md.market_at(i, t) for i in idx]
                books = [self.md.book_at(i, t) for i in idx]
                if key.startswith("g:"):
                    ds = self.risk.check_basket(legs, markets, view, books=books, now=now)
                    self._note_kill_switch()
                    if any(d.approved_count < leg.count for leg, d in zip(legs, ds, strict=True)):
                        self.stats["risk_rejected"] += len(legs)
                        for leg in legs:
                            self._signal(leg, "rejected", "risk: basket trimmed (all-or-none)")
                        continue
                    for leg in legs:
                        self.risk.record_order(now, leg.strategy)
                    counts = [d.approved_count for d in ds]
                else:
                    d = self.risk.check(legs[0], markets[0], view, book=books[0], now=now)
                    self._note_kill_switch()
                    if d.approved_count <= 0:
                        self.stats["risk_rejected"] += 1
                        self._signal(legs[0], "rejected", f"risk: {d.reason}")
                        continue
                    if d.approved_count < counts[0]:
                        self.stats["risk_trimmed"] += 1
                        note = f"risk: {d.reason}"
                    counts = [d.approved_count]
                extra = [self._synthetic(leg, n, self._cost_bound(leg) * n) for leg, n in zip(legs, counts, strict=True)]
                view = dataclasses.replace(view, cash=view.cash - sum((x.reserved for x in extra), Decimal(0)),
                                           open_orders=tuple(view.open_orders) + tuple(extra))
            if key.startswith("g:"):
                planned.append(("basket", legs, counts))
            else:
                planned.append(("one", legs[0], counts[0], note))
        return planned

    async def _execute_planned(self, planned: list[tuple[Any, ...]]) -> None:
        for item in planned:
            if item[0] == "basket":
                await self._place_basket(item[1], item[2])
            else:
                await self._place_one(item[1], item[2], item[3])

    # -- equity ---------------------------------------------------------------------------------

    async def snapshot_equity(self) -> None:
        if self.broker.positions():
            await self.broker.refresh_marks()
        a = self.broker.account()
        self.curve.append({"ts": _iso(self.clock.now), "equity": _f(a.equity, 4), "cash": _f(a.cash, 4),
                           "realized_pnl": _f(a.realized_pnl, 4), "open_positions": a.open_positions})

    async def resting(self) -> None:
        if self.broker.open_orders():
            await self.broker.process_resting_orders()

    # -- main loop ------------------------------------------------------------------------------

    async def run(self, progress: Callable[[float], None] | None = None) -> None:
        step, snap = self.step, int(self.opts["snapshot_s"])
        t = -(-self.start // step) * step
        next_snap = (t // snap + 1) * snap
        await self.snapshot_equity()
        last_forget = t
        span = max(1, self.end - t)
        t0 = t
        while t < self.end:
            await self.advance_to(t)
            await self.resting()
            n, te = await self.tick(t)
            tick_t = t
            while n:  # live cadence: tick again tick_interval_s later while the strategy keeps sending
                nxt = max(tick_t + self.tick_s, te)
                if nxt >= t + step or nxt >= self.end:
                    break
                await self.advance_to(nxt)
                tick_t = nxt
                n, te = await self.tick(nxt)
            if t >= next_snap:
                await self.snapshot_equity()
                while next_snap <= t:
                    next_snap += snap
            if t - last_forget >= 86400:
                self.md.forget(t)
                last_forget = t
                if progress is not None:
                    progress((t - t0) / span)
            t += step
        if self.pending:  # orders decided before the end still reach the book
            await self.advance_to(max(te for te, _, _ in self.pending))
        # hold to settlement: settle what is still open at its recorded time
        self.stats["open_at_end"] = len(self.broker.positions())
        self.stats["settled_after_end"] = len(self.scheduled)
        if self.heap:
            await self.advance_to(max(ts for ts, _ in self.heap))
        await self.snapshot_equity()


# --------------------------------------------------------------------------- metrics


def _sharpe(curve: list[dict[str, Any]]) -> float | None:
    """Annualized mean/stdev of daily equity changes (last equity point of each UTC day)."""
    days: dict[str, float] = {}
    for p in curve:
        if p.get("equity") is not None and p.get("ts"):
            days[str(p["ts"])[:10]] = float(p["equity"])
    vals = [days[k] for k in sorted(days)]
    if len(vals) < 3:
        return None
    d = np.diff(np.asarray(vals, dtype=float))
    sd = float(d.std(ddof=1))
    return round(float(d.mean()) / sd * math.sqrt(365), 4) if sd > 0 else None


def compute_metrics(trades: list[dict[str, Any]], curve: list[dict[str, Any]], *, starting_balance: float,
                    final_equity: float, fees: float, n_boot: int = 2000, seed: int = 0
                    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(metrics, by_month) from settled trade rows and the equity curve."""
    t = [x for x in trades if x.get("count")]
    n = len(t)
    pnl = np.asarray([x["pnl"] for x in t], dtype=float)
    cnt = np.asarray([x["count"] for x in t], dtype=float)
    ev_cl = [x.get("event_ticker") or x["ticker"] for x in t]
    contracts = float(cnt.sum())
    m: dict[str, Any] = {
        "total_pnl": round(final_equity - starting_balance, 4),
        "total_return_pct": round((final_equity - starting_balance) / starting_balance * 100, 4)
        if starting_balance else None,
        "final_equity": round(final_equity, 4),
        "n_trades": n,
        "contracts": int(contracts),
        "events": len(set(ev_cl)),
        "fees": round(fees, 4),
        "realized_pnl": round(float(pnl.sum()), 4),
    }
    if n:
        per = pnl / cnt
        lo, hi = bootstrap_ratio_ci(pnl, cnt, ev_cl, n_boot=n_boot, seed=seed)
        tlo, thi = bootstrap_ratio_ci(per, None, ev_cl, n_boot=n_boot, seed=seed)
        edge = [(x["expected_edge"], x["count"]) for x in t if x.get("expected_edge") is not None]
        m.update({
            "ev_per_contract": round(float(pnl.sum() / contracts), 6),
            "ev_ci_low": _f(lo), "ev_ci_high": _f(hi),
            "ev_per_trade": round(float(per.mean()), 6),
            "ev_per_trade_ci": [_f(tlo), _f(thi)],
            "hit_rate": round(float((pnl > 0).mean()), 6),
            "avg_entry_price": round(float(sum(x["price"] * x["count"] for x in t) / contracts), 6),
            "expected_edge_per_contract": round(sum(e * c for e, c in edge) / sum(c for _, c in edge), 6)
            if edge else None,
        })
    else:
        m.update({"ev_per_contract": None, "ev_ci_low": None, "ev_ci_high": None, "hit_rate": None})
    dd_usd, dd_pct = drawdown([p["equity"] for p in curve if p.get("equity") is not None])
    m["max_drawdown"] = round(dd_usd, 4)
    m["max_drawdown_pct"] = round(dd_pct, 4)
    m["sharpe"] = _sharpe(curve)
    months: dict[str, list[dict[str, Any]]] = {}
    for x in t:
        months.setdefault(str(x.get("ts") or "")[:7], []).append(x)
    by_month = []
    for k in sorted(months):
        rows = months[k]
        p = sum(r["pnl"] for r in rows)
        c = sum(r["count"] for r in rows)
        by_month.append({"month": k, "pnl": round(p, 4), "trades": len(rows), "contracts": c,
                         "win_rate": round(sum(r["pnl"] > 0 for r in rows) / len(rows), 4),
                         "ev_per_contract": round(p / c, 6) if c else None})
    return m, by_month


# --------------------------------------------------------------------------- entry points


def _resolve_strategy(strategy: str | None, strategy_cls: type[Strategy] | None) -> type[Strategy]:
    if strategy_cls is not None:
        return strategy_cls
    from kalshibot.strategies import REGISTRY

    if not strategy or strategy not in REGISTRY:
        raise ValueError(f"unknown strategy {strategy!r}; known: {', '.join(sorted(REGISTRY)) or '(none)'}")
    return REGISTRY[strategy]


def _choose_data(spec: UniverseSpec, opts: Mapping[str, Any]) -> str:
    kind = str(opts["data"] or "auto")
    if kind != "auto":
        return kind
    if spec.series_tickers and not spec.max_days_to_close and minute_series_available(spec.series_tickers,
                                                                                         opts["data_dir"]):
        return "minute"
    return "hourly"


async def run_backtest_async(
    strategy: str | None = None,
    params: Mapping[str, Any] | None = None,
    start: Any = None,
    end: Any = None,
    starting_balance: float | None = None,
    settings: Any = None,
    *,
    strategy_cls: type[Strategy] | None = None,
    dataset: ReplayDataset | None = None,
    progress: Callable[[float], None] | None = None,
    **options: Any,
) -> BacktestResult:
    """See the module docstring. ``dataset`` reuses an already loaded :class:`ReplayDataset`."""
    wall = time.monotonic()
    opts = _options(settings, options)
    cls = _resolve_strategy(strategy, strategy_cls)
    if not getattr(cls, "backtestable", False) and not opts["force"]:
        raise ValueError(f"strategy {cls.name or cls.__name__!r} is not backtestable (force=True overrides)")
    strat = cls(params)
    spec = strat.universe()
    kind = dataset.kind if dataset is not None else _choose_data(spec, opts)
    ds = dataset or load_dataset(kind, series=spec.series_tickers, data_dir=opts["data_dir"],
                                 universe=str(opts["universe"]), categories=opts["categories"])
    if ds.n == 0:
        raise BacktestDataError("the dataset has no markets")
    start_dt = _parse_when(start)
    end_dt = _parse_when(end, end=True)
    s = int(start_dt.timestamp()) if start_dt else ds.first_ts
    e = int(end_dt.timestamp()) if end_dt else ds.last_ts + ds.step_s
    s, e = max(s, ds.first_ts - ds.step_s), min(e, ds.last_ts + ds.step_s)
    if e <= s:
        raise ValueError(f"empty period: data covers {_iso(_dt(ds.first_ts))} .. {_iso(_dt(ds.last_ts))}")
    acct = getattr(settings, "account", None)
    bal = D(starting_balance if starting_balance is not None else getattr(acct, "starting_balance", 1000))
    run = _Replay(strat, ds, start=s, end=e, starting_balance=bal, settings=settings, opts=opts)
    await run.run(progress)
    a = run.broker.account()
    metrics, by_month = compute_metrics(run.trades, run.curve, starting_balance=float(bal),
                                        final_equity=float(a.equity), fees=float(a.fees_paid),
                                        n_boot=opts["n_boot"], seed=opts["seed"])
    st = run.stats
    metrics.update({
        "data": ds.kind,
        "fill_mode": run.fill,
        "signals": sum(v for k, v in st.items() if k.startswith("signals_")),
        "unfilled_signals": st["signals_unfilled"],
        "risk_rejected": st["risk_rejected"],
    })
    metrics["details"] = {
        "strategy": run.name,
        "realized_pnl": metrics.pop("realized_pnl", None),
        "ev_per_trade_ci": metrics.pop("ev_per_trade_ci", None),
        "params": {k: v for k, v in strat.params.items()},
        "period": {"start": _iso(_dt(s)), "end": _iso(_dt(e))},
        "options": {k: v for k, v in opts.items() if k != "data_dir"} | {"fill": run.fill,
                                                                          "latency_s": run.latency},
        "dataset": ds.describe(),
        "starting_balance": float(bal),
        "ticks": st["ticks"],
        "tick_interval_s": run.tick_s,
        "signals": {k.removeprefix("signals_"): v for k, v in st.items() if k.startswith("signals_")},
        "risk_trimmed": st["risk_trimmed"],
        "intents_over_cap": st["intents_over_cap"],
        "cancels_ignored": st["cancels_ignored"],
        "strategy_errors": st["strategy_errors"],
        "errors": run.errors,
        "open_at_end": st["open_at_end"],
        "settled_after_end": st["settled_after_end"],
        "kill_switch_days": run.kill_days,
        "rejection_reasons": dict(run.reasons.most_common(15)),
        "strategy_logs": dict(run.logs.most_common(20)),
        "strategy_log_samples": run.log_samples[:20],
        "broker_logs": dict(run.broker.log_counts),
        "elapsed_s": round(time.monotonic() - wall, 2),
    }
    trades = sorted(run.trades, key=lambda x: (x.get("ts") or "", x["ticker"]))
    return BacktestResult(metrics=metrics, equity_curve=run.curve, trades=trades, by_month=by_month,
                          signals=run.signals)


def run_backtest(
    strategy: str | None = None,
    params: Mapping[str, Any] | None = None,
    start: Any = None,
    end: Any = None,
    starting_balance: float | None = None,
    settings: Any = None,
    *,
    strategy_cls: type[Strategy] | None = None,
    dataset: ReplayDataset | None = None,
    progress: Callable[[float], None] | None = None,
    **options: Any,
) -> BacktestResult:
    """Synchronous entry point (the API runs it in a worker thread; the CLI calls it directly)."""
    return asyncio.run(run_backtest_async(strategy, params, start, end, starting_balance, settings,
                                          strategy_cls=strategy_cls, dataset=dataset, progress=progress,
                                          **options))
