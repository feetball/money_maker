"""Engine (ARCHITECTURE.md §9): the paper-trading loop.

``Engine(settings, client, marketdata, broker, risk, store, strategies)`` with
``start()``, ``stop()`` and ``status()``. One asyncio task runs a small scheduler; every
interval comes from ``settings.engine``:

=====================  ====================  =================================================
job                    default interval      what it does
=====================  ====================  =================================================
``universe``           ``universe_refresh_s`` refresh the market universe (union of enabled
                       (120 s)               strategies' specs) + prefetch series; runs as a
                                             child task so a slow scan never blocks trading
``series``             ``UniverseSpec.        series-scoped refresh (one request per series) of
                       refresh_s`` (>= 15 s)  specs that ask for it: newly listed markets of
                                             short-lived series appear promptly (child task)
``exchange``           30 s                  ``GET /exchange/status`` -> broker; ticks are
                                             skipped while trading is paused or Kalshi is down
``tick``               per strategy:         starts each enabled strategy's tick on its own
                       ``tick_interval_s``   grid (``on_tick(ctx)`` -> cancels -> risk check ->
                       or ``tick_s`` (30 s)  paper broker; every intent is recorded as a signal)
``orders``             ``order_poll_s``      maker-fill simulation + expiries (child task; at
                                             most ``paper.max_trade_polls_per_pass`` trade-tape
                                             reads per pass, least recently read first)
``settlement``         ``settlement_poll_s`` batch-refresh held markets, settle finalized ones
``snapshot``           ``snapshot_s``        marks + equity snapshot, daily-loss kill switch
``preclose``           1 s                   one extra mark of a held market in its last
                                             ``preclose_mark_s`` (5 s) before close (no request
                                             otherwise); the mark then freezes until the result
``postclose``          10 s                  settlement poll while a held market is in its first
                                             10 min after close (no request otherwise)
``housekeeping``       1 h                   prune old logs/signals (disk hygiene; ``engine.keep_log_rows``)
=====================  ====================  =================================================

Maintenance jobs run one at a time. Strategy ticks do not: the ``tick`` job only starts
them (see :meth:`Engine.dispatch`). Each strategy ticks on a fixed grid of its own
``tick_interval_s`` (class or instance attribute, >= 1 s; default ``engine.tick_s``), so
ticks never drift; a slot that comes while the strategy's previous tick is still running is
skipped (counted in ``skipped_ticks``), never queued. Strategies tick concurrently - a slow
strategy cannot delay another's window - while their risk checks and placements are
serialized by one execution lock. A failing job is logged and retried; on network-type
errors (Kalshi unreachable, 429/5xx) its interval backs off exponentially up to 5 minutes
(the exchange check: at most 60 s, and as soon as any other Kalshi request succeeds while
ticks are paused, the next tick re-checks immediately). An exception (or timeout) in one
strategy's tick is caught, logged and recorded on that strategy; it never affects the
others or the loop. ``start``/``stop`` are serialized, so a Start arriving while a Stop is
still cleaning up can never leave an untracked second loop running.

Order flow per strategy tick: first the strategy's cancels (``CancelIntent`` items and the
orders named by ``OrderIntent.replaces``; each market is synced with the trade tape before
its orders are cancelled). Then, under the execution lock, its order intents: the markets
and books of all of them are fetched in one batch first (``/markets?tickers=`` and
``/markets/orderbooks``), so the risk checks and the broker's own fresh-book reads are cache
hits; intents sharing a ``group_id`` are an all-or-none basket, risk-checked together
(:meth:`~kalshibot.risk.RiskManager.check_basket`, any trimmed leg rejects it) and placed
with ``place_basket(all_or_none=True)``; at most ``max_intents_per_tick`` (50) intents per
tick, a basket kept or dropped whole. Housekeeping also downsamples equity snapshots older
than ``equity_full_days`` (7) to the extremes of each hour.

Events (``tick``, ``signal``, ``order``, ``fill``, ``settlement``, ``log``, ``account``) are
published to an in-process :class:`EventBus` that the API streams over SSE.

Strategy parameters are the class ``default_params`` <- config ``strategies.<name>.params``
<- runtime overrides saved in the store (``PATCH /api/strategies/{name}``). ``enabled``:
the dashboard toggle saved in the store if set, else ``strategies.<name>.enabled`` in the
config if set, else the class's ``enabled_by_default`` (``enabled_source`` in
``/api/strategies``: "dashboard" / "config" / "default").

Orders go to the broker: :class:`~kalshibot.paper.broker.PaperBroker` (paper, the default) or
:class:`~kalshibot.live.broker.LiveBroker` (``live.enabled``: real orders on Kalshi). In live mode
the snapshot job also reconciles the ledger with the exchange balance and positions.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx

from kalshibot.fees import resolve_fee_params, trading_fee
from kalshibot.kalshi.client import KalshiAPIError, KalshiNotFound
from kalshibot.marketdata import display_title
from kalshibot.money import D, f4
from kalshibot.paper.broker import FALLBACK_FEE_PARAMS
from kalshibot.paper.models import iso
from kalshibot.strategies.base import (
    CancelIntent,
    OrderIntent,
    ParamError,
    Strategy,
    UniverseSpec,
    coerce_params,
    intents_list,
)

if TYPE_CHECKING:
    from kalshibot.feeds import FeedRegistry
    from kalshibot.kalshi.models import Event, Market, Orderbook, Series
    from kalshibot.marketdata import MarketDataService
    from kalshibot.paper.broker import PaperBroker
    from kalshibot.paper.models import Order, PortfolioView
    from kalshibot.risk import RiskManager
    from kalshibot.store import Store

__all__ = ["STREAM_EVENT_TYPES", "Engine", "EngineContext", "EventBus", "StrategyRuntime", "jsonable"]

log = logging.getLogger(__name__)

STREAM_EVENT_TYPES = ("tick", "signal", "order", "fill", "settlement", "log", "account")
MAX_BACKOFF_S = 300.0
#: The exchange check decides whether ticks run: never back it off further than this.
MAX_EXCHANGE_BACKOFF_S = 60.0
#: Floor for a strategy's declared ``tick_interval_s``.
MIN_TICK_INTERVAL_S = 1.0
#: Floor for ``UniverseSpec.refresh_s`` (``/markets`` lists are CDN-cached for 15 s).
MIN_SERIES_REFRESH_S = 15.0
#: Skipped-tick warnings are logged at most this often per strategy.
SKIP_WARN_EVERY_S = 300.0
#: ``Engine.health()``: no completed tick for ``max(this, 4 x tick_s)`` seconds is unhealthy.
HEALTH_MIN_TICK_AGE_S = 120.0


# --------------------------------------------------------------------------- JSON helpers


def jsonable(x: Any) -> Any:
    """Plain-JSON form: Decimal -> float (4 dp), datetime -> ISO Z, NaN/inf -> None."""
    if x is None or isinstance(x, bool | int | str):
        return x
    if isinstance(x, Decimal):
        return f4(x) if x.is_finite() else None
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, datetime):
        return iso(x)
    if isinstance(x, Mapping):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, list | tuple | set | frozenset):
        return [jsonable(v) for v in x]
    to_json = getattr(x, "to_json", None)
    if callable(to_json):
        return jsonable(to_json())
    return str(x)


# --------------------------------------------------------------------------- event bus


class EventBus:
    """In-process pub/sub. ``publish`` is thread-safe; subscribers get bounded queues
    (a slow consumer loses its oldest events, never blocks the engine).

    Every delivered event gets an id, strictly increasing for the life of the process and
    across restarts (the sequence starts at the process start time in epoch milliseconds),
    so the SSE stream can send ``id:`` lines and replay what a reconnecting client missed.
    ``recent`` keeps the last ``history`` events as ``(id, type, data)``.
    """

    def __init__(self, maxsize: int = 1000, history: int = 500) -> None:
        self.maxsize = maxsize
        #: queue -> whether its items carry the event id (``(id, type, data)``) or not (``(type, data)``)
        self._subs: dict[asyncio.Queue[Any], bool] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self.recent: deque[tuple[int, str, Any]] = deque(maxlen=history)
        self.published = 0
        self._seq0 = int(time.time() * 1000)

    @property
    def history(self) -> int:
        return self.recent.maxlen or 0

    @property
    def last_id(self) -> int:
        """Id of the most recent event (the base of the sequence if nothing was published yet)."""
        return self._seq0 + self.published

    def bind(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop or asyncio.get_running_loop()

    def subscribe(self, maxsize: int | None = None, *, with_ids: bool = False) -> asyncio.Queue[Any]:
        """A new subscriber queue; ``with_ids`` makes its items ``(id, type, data)``."""
        if self._loop is None:
            with contextlib.suppress(RuntimeError):
                self._loop = asyncio.get_running_loop()
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize or self.maxsize)
        self._subs[q] = with_ids
        return q

    def unsubscribe(self, q: asyncio.Queue[Any]) -> None:
        self._subs.pop(q, None)

    def replay(self, *, after: int | None = None, last: int | None = None) -> list[tuple[int, str, Any]]:
        """Buffered events (oldest first): those with id > ``after`` and/or the last ``last`` ones."""
        evs = list(self.recent)
        if after is not None:
            evs = [e for e in evs if e[0] > after]
        if last is not None:
            evs = evs[-last:] if last > 0 else []
        return evs

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def publish(self, type_: str, data: Any) -> None:
        ev = (type_, jsonable(data))
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        loop = self._loop
        if loop is not None and running is not loop:
            if loop.is_closed():
                return
            loop.call_soon_threadsafe(self._deliver, ev)
        else:
            self._deliver(ev)

    def _deliver(self, ev: tuple[str, Any]) -> None:
        self.published += 1
        full = (self._seq0 + self.published, *ev)
        self.recent.append(full)
        for q, with_ids in list(self._subs.items()):
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(full if with_ids else ev)


class _BusLogHandler(logging.Handler):
    """Streams broker/risk log records (which they already store) to the bus as ``log``."""

    def __init__(self, engine: Engine) -> None:
        super().__init__(logging.INFO)
        self.engine = engine

    def emit(self, record: logging.LogRecord) -> None:
        try:
            kind = record.name.rsplit(".", 1)[-1]
            message = record.getMessage()
            if record.msg == "%s: %s" and isinstance(record.args, tuple) and len(record.args) == 2:
                kind, message = str(record.args[0]), str(record.args[1])
            elif record.msg == "risk: %s" and isinstance(record.args, tuple) and len(record.args) == 1:
                kind, message = "risk", str(record.args[0])
            self.engine.bus.publish("log", {
                "id": None, "ts": iso(datetime.fromtimestamp(record.created, tz=UTC)),
                "level": record.levelname.lower(), "kind": kind, "message": message, "data": None})
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)


# --------------------------------------------------------------------------- runtime state


@dataclass
class StrategyRuntime:
    name: str
    cls: type[Strategy]
    instance: Strategy
    enabled: bool = False
    #: where ``enabled`` came from: "dashboard" (stored toggle), "config" or "default" (the class's
    #: ``enabled_by_default``)
    enabled_source: str = "default"
    overrides: dict[str, Any] = field(default_factory=dict)  # runtime param overrides (store)
    last_tick_at: datetime | None = None
    #: end of the last ``on_tick`` that returned without raising (``last_tick_at`` also advances on a
    #: failed tick, so it cannot tell a working strategy from one that raises every time)
    last_ok_at: datetime | None = None
    #: when the dashboard last switched the strategy on (the health check's grace anchor)
    enabled_at: datetime | None = None
    last_duration_ms: float | None = None
    last_error: str | None = None
    ticks: int = 0
    errors: int = 0
    intents: int = 0
    last_state: Any = None
    cancels: int = 0  # resting orders cancelled on the strategy's request
    # scheduling (monotonic seconds): each strategy ticks on its own fixed grid
    next_due: float = 0.0  # next slot (0 = as soon as possible)
    task: asyncio.Task[Any] | None = None  # the running scheduled tick
    skipped_ticks: int = 0  # slots skipped: previous tick still running, or the engine was late
    last_lag_ms: float | None = None  # how late the last scheduled tick started after its slot
    skip_warned_at: float | None = None

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def spec(self) -> UniverseSpec:
        try:
            spec = self.instance.universe()
        except Exception as e:
            log.error("strategy %s: universe() failed: %s", self.name, e)
            return UniverseSpec()
        return spec if isinstance(spec, UniverseSpec) else UniverseSpec()


@dataclass
class _Job:
    name: str
    interval: float
    fn: Callable[[], Awaitable[Any]]
    background: bool = False
    next_due: float = 0.0
    failures: int = 0
    runs: int = 0
    last_run: datetime | None = None
    last_duration_s: float | None = None
    last_error: str | None = None
    task: asyncio.Task[Any] | None = None
    #: next due time after a successful run (default: ``interval`` after the start)
    schedule: Callable[[float], float] | None = None


class _Postpone(Exception):
    """Raised by a job to be retried after ``delay`` seconds without counting a failure."""

    def __init__(self, delay: float, why: str = "") -> None:
        super().__init__(why)
        self.delay = delay


def is_network_error(e: BaseException) -> bool:
    """Kalshi unreachable / throttled / 5xx (worth backing off), vs a logic error."""
    if isinstance(e, KalshiNotFound):
        return False
    if isinstance(e, KalshiAPIError):
        return e.status is None or e.status == 429 or e.status >= 500
    return isinstance(e, httpx.HTTPError | OSError | TimeoutError)


def _field(x: Any, name: str, default: Any = None) -> Any:
    """``x[name]`` for a mapping intent, else ``x.name``."""
    if isinstance(x, Mapping):
        return x.get(name, default)
    return getattr(x, name, default)


def _as_int(x: Any) -> int | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        return int(x)
    except (TypeError, ValueError, OverflowError):
        return None


# --------------------------------------------------------------------------- context


class EngineContext:
    """:class:`~kalshibot.strategies.base.StrategyContext` backed by market data + the broker."""

    def __init__(self, engine: Engine, rt: StrategyRuntime, now: datetime, markets: Mapping[str, Market],
                 portfolio: PortfolioView) -> None:
        self._engine = engine
        self._md = engine.md
        self.strategy = rt.name
        self.params = dict(rt.instance.params)
        self.now = now
        self.markets = markets
        self.events: Mapping[str, Event] = engine.md.events
        self.portfolio = portfolio
        self.feeds = engine.feeds
        self.book_max_age_s = engine.ctx_book_max_age_s
        self.cancels: list[CancelIntent] = []  # queued by ``cancel``; applied before the tick's orders

    def clock(self) -> datetime:
        """The engine's real clock, read now. ``now`` is frozen at the start of the tick, so a
        strategy whose timing matters re-reads this after its network calls (a decision whose
        reads took 20 s is 20 s later than ``now`` says)."""
        return self._engine.clock()

    async def series(self, series_ticker: str) -> Series:
        return await self._md.series(series_ticker)

    async def orderbook(self, ticker: str, max_age_s: float | None = None) -> Orderbook:
        """Order book at most ``max_age_s`` old (default ``ctx_book_max_age_s``, 5 s; 0 = fetch now)."""
        age = self.book_max_age_s if max_age_s is None else max(0.0, float(max_age_s))
        return await self._md.orderbook(ticker, max_age_s=age)

    async def orderbooks(self, tickers: Iterable[str]) -> dict[str, Orderbook]:
        return await self._md.orderbooks(tickers, max_age_s=self.book_max_age_s)

    async def event(self, event_ticker: str) -> Event | None:
        return await self._md.event(event_ticker)

    async def market(self, ticker: str, fresh: bool = False) -> Market:
        return await self._md.market(ticker, fresh=fresh)

    def fee_params(self, market: Market) -> tuple[str, Decimal]:
        """(fee_type, multiplier) in effect at ``now`` from cached series/event data and the
        scheduled fee changes (conservative fallback when the series is not cached)."""
        cached = getattr(self._md, "fee_params_cached", None)
        res = cached(market, self.now) if callable(cached) else None
        if res is not None:
            return res
        event = self._md.events.get(market.event_ticker)
        series = self._md.cached_series(market.series_ticker)
        if series is None:
            ft, m = FALLBACK_FEE_PARAMS
            return resolve_fee_params(_FeeStub(ft, m), event)
        return resolve_fee_params(series, event)

    def fee(self, market: Market, price: Any, count: Any, is_taker: bool = True) -> Decimal:
        """Kalshi fee in dollars for ``count`` contracts at ``price`` (single execution)."""
        ft, mult = self.fee_params(market)
        return trading_fee(D(price), D(count), is_taker=is_taker, fee_type=ft, fee_multiplier=mult,
                           precision=self._engine.broker.precision)

    def log(self, msg: str, **data: Any) -> None:
        self._engine.log("info", "strategy", f"[{self.strategy}] {msg}", strategy=self.strategy, **data)

    def cancel(self, order_id: Any = None, *, ticker: str | None = None, reason: str = "") -> CancelIntent:
        """Queue a cancel of this strategy's resting order ``order_id`` (an id or an ``Order``),
        or of all its resting orders in ``ticker`` - same as returning a :class:`CancelIntent`."""
        oid = getattr(order_id, "id", order_id)
        c = CancelIntent(order_id=oid, ticker=ticker, reason=reason, strategy=self.strategy)
        self.cancels.append(c)
        return c


@dataclass(frozen=True)
class _FeeStub:
    fee_type: str
    fee_multiplier: Decimal


# --------------------------------------------------------------------------- engine


class Engine:
    """The trading loop. Use from the event loop that owns the broker."""

    def __init__(
        self,
        settings: Any,
        client: Any,
        marketdata: MarketDataService,
        broker: PaperBroker,
        risk: RiskManager,
        store: Store | None,
        strategies: Mapping[str, type[Strategy] | Strategy] | Iterable[Strategy] | None = None,
        *,
        feeds: FeedRegistry | None = None,
        bus: EventBus | None = None,
        clock: Callable[[], datetime] | None = None,
        mono: Callable[[], float] = time.monotonic,
        exchange_poll_s: float = 30.0,
        housekeeping_s: float = 3600.0,
        preclose_mark_s: float = 5.0,
        postclose_poll_s: float = 10.0,
        postclose_window_s: float = 600.0,
        tick_timeout_s: float | None = None,
        max_intents_per_tick: int = 50,
        ctx_book_max_age_s: float = 5.0,
        series_prefetch: int = 25,
        keep_rows: int | None = None,
        equity_full_days: float = 7.0,
        equity_bucket_s: int = 3600,
    ) -> None:
        from kalshibot.feeds import FeedRegistry

        self.settings = settings
        self.client = client
        self.md = marketdata
        self.broker = broker
        self.risk = risk
        self.store = store
        self.feeds = feeds if feeds is not None else FeedRegistry()
        self.bus = bus or EventBus()
        self.clock = clock or broker.clock
        self.mono = mono
        eng = getattr(settings, "engine", None)
        self.intervals = {
            "universe": float(getattr(eng, "universe_refresh_s", 120)),
            "exchange": float(exchange_poll_s),
            "tick": float(getattr(eng, "tick_s", 30)),
            "orders": float(getattr(eng, "order_poll_s", 15)),
            "settlement": float(getattr(eng, "settlement_poll_s", 60)),
            "snapshot": float(getattr(eng, "snapshot_s", 60)),
            "housekeeping": float(housekeeping_s),
        }
        #: held markets get one extra mark refresh in their last N seconds before close (0 = off)
        self.preclose_mark_s = float(preclose_mark_s)
        #: held markets are polled for settlement every ``postclose_poll_s`` during their first
        #: ``postclose_window_s`` after close (short-dated markets settle within seconds)
        self.postclose_window_s = float(postclose_window_s)
        self.tick_timeout_s = float(tick_timeout_s if tick_timeout_s is not None
                                    else max(5.0, self.intervals["tick"] * 4))
        self.max_intents_per_tick = int(max_intents_per_tick)
        self.ctx_book_max_age_s = float(ctx_book_max_age_s)
        self.series_prefetch = int(series_prefetch)
        #: rows kept in logs/signals by the housekeeping job (``engine.keep_log_rows``; 0 = keep all)
        self.keep_rows = int(keep_rows if keep_rows is not None else getattr(eng, "keep_log_rows", 50_000))
        self.equity_full_days = float(equity_full_days)
        self.equity_bucket_s = int(equity_bucket_s)

        self.running = False
        self.started_at: datetime | None = None
        self.last_tick_at: datetime | None = None
        self.tick_count = 0
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.kalshi_down = False
        self.trading_paused = False
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._account_dirty = False
        self._last_account_pub = 0.0
        self._log_handler: _BusLogHandler | None = None
        self._force_universe = False
        self._error_job: str | None = None
        self._lifecycle = asyncio.Lock()  # serializes start()/stop()
        self._equity_cut: datetime | None = None  # last equity downsampling cut-off
        self._down_mark = 0  # client.success_count when Kalshi went down
        #: serializes risk check + placement across concurrently ticking strategies
        self._exec_lock = asyncio.Lock()
        self._rounds: set[asyncio.Task[Any]] = set()  # scheduled tick rounds in flight
        self._tick_gated = False  # last tick job found Kalshi down / trading paused
        self._heartbeat_due = 0.0  # no strategy enabled: bare ticks every ``tick_s``
        self._preclose_done: dict[str, datetime] = {}  # ticker -> close time already sampled

        self.runtimes: dict[str, StrategyRuntime] = {}
        self._build_runtimes(strategies)
        self._install_strategy_limits()
        self.jobs: dict[str, _Job] = {
            "universe": _Job("universe", self.intervals["universe"], self._job_universe, background=True),
            "series": _Job("series", self.intervals["universe"], self._job_series, background=True),
            "exchange": _Job("exchange", self.intervals["exchange"], self._job_exchange),
            "tick": _Job("tick", self.intervals["tick"], self._job_tick, schedule=self._next_tick_due),
            "orders": _Job("orders", self.intervals["orders"], self._job_orders, background=True),
            "settlement": _Job("settlement", self.intervals["settlement"], self._job_settlement),
            "snapshot": _Job("snapshot", self.intervals["snapshot"], self._job_snapshot),
            "preclose": _Job("preclose", 1.0, self._job_preclose),
            "postclose": _Job("postclose", float(postclose_poll_s), self._job_postclose),
            "housekeeping": _Job("housekeeping", self.intervals["housekeeping"], self._job_housekeeping),
        }
        self._unsubscribe = broker.subscribe(self._on_broker_event)
        self._sync_specs()

    # ------------------------------------------------------------------ strategies

    def _build_runtimes(self, strategies: Any) -> None:
        if strategies is None:
            from kalshibot.strategies import REGISTRY

            strategies = REGISTRY
        items: list[tuple[str, type[Strategy], Strategy | None]] = []
        if isinstance(strategies, Mapping):
            for name, v in strategies.items():
                if isinstance(v, Strategy):
                    items.append((name, type(v), v))
                else:
                    items.append((name, v, None))
        else:
            for inst in strategies:
                items.append((inst.name, type(inst), inst))
        for name, cls, inst in sorted(items, key=lambda x: x[0]):
            try:
                self.runtimes[name] = self._make_runtime(name, cls, inst)
            except Exception as e:
                log.exception("strategy %s could not be created", name)
                self.log("error", "strategy", f"strategy {name} could not be created: {e}", strategy=name)

    def _install_strategy_limits(self) -> None:
        """Per-strategy allocation / daily-loss limits (class ``risk_defaults`` <- config) -> risk."""
        setter = getattr(self.risk, "set_strategy_limits", None)
        if not callable(setter):
            return
        from kalshibot.risk import strategy_limits

        try:
            setter(strategy_limits(self.settings, {n: rt.cls for n, rt in self.runtimes.items()}))
        except Exception as e:  # never keep the engine from starting
            log.exception("per-strategy risk limits could not be installed")
            self.log("error", "risk", f"per-strategy risk limits could not be installed: {e}")

    def _config_params(self, name: str) -> dict[str, Any]:
        s = self.settings.strategy(name) if hasattr(self.settings, "strategy") else None
        return dict(getattr(s, "params", None) or {})

    def _config_enabled(self, name: str, cls: type[Strategy]) -> tuple[bool, str]:
        """``strategies.<name>.enabled`` from the config when set, else the class's
        ``enabled_by_default``; with the source ("config" / "default")."""
        s = self.settings.strategy(name) if hasattr(self.settings, "strategy") else None
        v = getattr(s, "enabled", None)
        if v is not None:
            return bool(v), "config"
        return bool(getattr(cls, "enabled_by_default", False)), "default"

    def _make_runtime(self, name: str, cls: type[Strategy], inst: Strategy | None) -> StrategyRuntime:
        st = self.store.get_strategy_state(name) if self.store is not None else None
        overrides = dict((st or {}).get("params") or {})
        if st and st.get("enabled") is not None:  # the dashboard toggle beats the config
            enabled, source = bool(st["enabled"]), "dashboard"
        else:
            enabled, source = self._config_enabled(name, cls)
        if inst is None:
            merged = {**self._config_params(name), **overrides}
            try:
                params = cls.resolve_params(merged)
            except ParamError as e:
                self.log("error", "strategy", f"strategy {name}: invalid parameters ({e}); using defaults",
                         strategy=name)
                params = cls.resolve_params({})
            inst = cls(params)
        rt = StrategyRuntime(name=name, cls=cls, instance=inst, enabled=bool(enabled), enabled_source=source,
                             overrides=overrides)
        state = (st or {}).get("state")
        if state is not None:
            try:
                inst.load_state(state)
                rt.last_state = state
            except Exception as e:
                self.log("warning", "strategy", f"strategy {name}: load_state failed: {e}", strategy=name)
        return rt

    def _sync_specs(self) -> bool:
        specs = {n: rt.spec() for n, rt in self.runtimes.items() if rt.enabled}
        jobs = getattr(self, "jobs", None)
        if jobs:  # cadences follow the enabled strategies
            jobs["tick"].interval = self._min_tick_interval()
            _, fast = self._fast_series(specs)
            jobs["series"].interval = fast if fast is not None else self.intervals["universe"]
        return self.md.set_specs(specs)

    def strategy_interval(self, rt: StrategyRuntime) -> float:
        """Seconds between ``rt``'s ticks: its ``tick_interval_s`` (>= 1 s), else ``engine.tick_s``."""
        v: Any = getattr(rt.instance, "tick_interval_s", None)
        try:
            v = float(v) if v is not None else None
        except (TypeError, ValueError):
            v = None
        if v is None or not math.isfinite(v) or v <= 0:
            return self.intervals["tick"]
        return max(MIN_TICK_INTERVAL_S, v)

    def _min_tick_interval(self) -> float:
        ivs = [self.strategy_interval(rt) for rt in self.runtimes.values() if rt.enabled]
        return min(ivs) if ivs else self.intervals["tick"]

    @staticmethod
    def _fast_series(specs: Mapping[str, UniverseSpec]) -> tuple[list[str], float | None]:
        """Series with a ``refresh_s`` of their own, and the shortest such interval (>= 15 s)."""
        series: set[str] = set()
        ivs: list[float] = []
        for spec in specs.values():
            r = getattr(spec, "refresh_s", None)
            if r and spec.series_tickers:
                series.update(spec.series_tickers)
                ivs.append(max(MIN_SERIES_REFRESH_S, float(r)))
        return sorted(series), (min(ivs) if ivs else None)

    def update_strategy(self, name: str, *, enabled: bool | None = None,
                        params: Mapping[str, Any] | None = None) -> StrategyRuntime:
        """Apply ``PATCH /api/strategies/{name}``: validate, persist, re-create the instance.

        Raises ``KeyError`` (unknown strategy) or :class:`ParamError` (invalid params).
        """
        rt = self.runtimes[name]
        new_overrides = dict(rt.overrides)
        if params is not None:
            checked = coerce_params(rt.cls.param_schema, params, strict=True)
            new_overrides.update(checked)
            merged = {**self._config_params(name), **new_overrides}
            new_params = rt.cls.resolve_params(merged, strict=False)
            state = None
            try:
                state = rt.instance.dump_state()
            except Exception:
                state = None
            inst = rt.cls(new_params)
            if state is not None:
                with contextlib.suppress(Exception):
                    inst.load_state(state)
            rt.instance = inst
            rt.overrides = new_overrides
        if enabled is not None:
            if enabled and not rt.enabled:
                rt.enabled_at = self.clock()
                rt.next_due = 0.0  # tick as soon as possible
                tick = self.jobs.get("tick")
                if tick is not None:
                    tick.next_due = self.mono()
                    self._wake.set()
            rt.enabled = bool(enabled)
            rt.enabled_source = "dashboard"
        if self.store is not None:
            self.store.save_strategy_state(name, enabled=rt.enabled if enabled is not None else None,
                                           params=new_overrides if params is not None else None)
        changes = []
        if enabled is not None:
            changes.append("enabled" if enabled else "disabled")
        if params is not None:
            changes.append(f"params {dict(params)}")
        self.log("info", "strategy", f"strategy {name}: {', '.join(changes) or 'no change'}", strategy=name)
        if self._sync_specs():
            self.request_universe_refresh()
        return rt

    def reset_strategies(self) -> None:
        """Re-create every strategy instance (fresh in-memory state), e.g. after an account reset."""
        for name, rt in list(self.runtimes.items()):
            params = rt.cls.resolve_params({**self._config_params(name), **rt.overrides})
            rt.instance = rt.cls(params)
            rt.last_state = None
            rt.last_error = None
            if self.store is not None:
                self.store.save_strategy_state(name, state={})

    def strategy_json(self, name: str, stats: Mapping[str, Any] | None = None) -> dict[str, Any]:
        rt = self.runtimes[name]
        if stats is None:
            stats = self.broker.strategy_stats().get(name, {})
        spec = rt.spec()
        st = {k: stats.get(k) for k in ("orders", "fills", "open_positions", "settled", "realized_pnl",
                                         "unrealized_pnl", "fees", "win_rate", "exposure")}
        for k in ("orders", "fills", "open_positions", "settled"):
            st[k] = int(st[k] or 0)
        for k in ("realized_pnl", "unrealized_pnl", "fees", "exposure"):
            st[k] = f4(D(st[k] or 0))
        return {
            "name": name,
            "description": rt.cls.description,
            "enabled": rt.enabled,
            "enabled_source": rt.enabled_source,
            "params": jsonable(rt.instance.params),
            "param_schema": jsonable(rt.cls.schema_json()),
            "backtestable": bool(rt.cls.backtestable),
            "experimental": bool(getattr(rt.cls, "experimental", False)),
            "risk_limits": self._risk_limits_json(name),
            "stats": st,
            "last_tick_at": iso(rt.last_tick_at),
            "last_error": rt.last_error,
            "ticks": rt.ticks,
            "errors": rt.errors,
            "intents": rt.intents,
            "cancels": rt.cancels,
            "tick_interval_s": self.strategy_interval(rt),
            "skipped_ticks": rt.skipped_ticks,
            "last_tick_lag_ms": rt.last_lag_ms,
            "last_duration_ms": rt.last_duration_ms,
            "universe": {"max_days_to_close": spec.max_days_to_close, "series_tickers": spec.series_tickers},
            "universe_size": len(self.md.markets_for(spec)) if rt.enabled else 0,
        }

    def _risk_limits_json(self, name: str) -> dict[str, Any]:
        alloc = getattr(self.risk, "allocation_pct", None)
        loss = getattr(self.risk, "strategy_loss_limit", None)
        paused = getattr(self.risk, "strategy_paused", None)
        out: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            out["max_allocation_pct"] = alloc(name) if callable(alloc) else None
            lim = loss(name) if callable(loss) else None
            out["daily_loss_limit"] = f4(lim) if lim else None
            out["paused"] = paused(name) if callable(paused) else None
        return out

    def strategies_json(self) -> list[dict[str, Any]]:
        stats = self.broker.strategy_stats()
        return [self.strategy_json(n, stats.get(n, {})) for n in sorted(self.runtimes)]

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Start the loop (idempotent; waits for a stop in progress to finish first)."""
        async with self._lifecycle:
            if self._task is not None and not self._task.done():
                return
            self.bus.bind(asyncio.get_running_loop())
            stop = asyncio.Event()
            self._stop = stop
            self._wake = asyncio.Event()
            self.running = True
            self.started_at = self.clock()
            now = self.mono()
            for job in self.jobs.values():
                job.next_due = now
                job.failures = 0
            for rt in self.runtimes.values():
                rt.next_due = 0.0
            self._heartbeat_due = 0.0
            # housekeeping does not need to run at startup
            self.jobs["housekeeping"].next_due = now + 60
            if self._log_handler is None:
                self._log_handler = _BusLogHandler(self)
                for name in ("kalshibot.paper", "kalshibot.risk"):
                    logging.getLogger(name).addHandler(self._log_handler)
            self._sync_specs()
            states = ", ".join(f"{n} {'on' if rt.enabled else 'off'} ({rt.enabled_source})"
                               for n, rt in sorted(self.runtimes.items()))
            self.log("info", "engine", f"engine started (paper trading); strategies: {states or 'none'}")
            self._task = asyncio.create_task(self._run(stop), name="kalshibot-engine")

    async def stop(self, timeout: float = 15.0) -> None:
        """Stop the loop and wait for the current job to finish (then cancel)."""
        async with self._lifecycle:
            task = self._task
            if task is None:
                self.running = False
                return
            self._stop.set()
            self._wake.set()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            except asyncio.CancelledError:
                task.cancel()
                raise
            except Exception:
                log.exception("engine task ended with an error")
            await self._cancel_background()
            if self._task is task:
                self._task = None
                self.running = False
            if self._log_handler is not None:
                for name in ("kalshibot.paper", "kalshibot.risk"):
                    logging.getLogger(name).removeHandler(self._log_handler)
                self._log_handler = None
            self.log("info", "engine", "engine stopped")

    async def close(self) -> None:
        await self.stop()
        with contextlib.suppress(Exception):
            self._unsubscribe()

    async def _cancel_background(self, grace_s: float = 5.0) -> None:
        # strategy ticks in flight get a moment to finish placing their orders
        ticks = [t for t in [rt.task for rt in self.runtimes.values()] + list(self._rounds)
                 if t is not None and not t.done()]
        if ticks:
            _, pending = await asyncio.wait(ticks, timeout=grace_s)
            for t in pending:
                t.cancel()
            for t in pending:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        for rt in self.runtimes.values():
            rt.task = None
        for job in self.jobs.values():
            if job.task is not None and not job.task.done():
                job.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await job.task
            job.task = None

    async def set_kill_switch(self, on: bool, reason: str = "manual") -> list[Any]:
        """Turn the risk kill switch on or off (API / dashboard).

        Engaging it (off -> on) also cancels every resting paper order: they are entries
        waiting to fill, and the kill switch blocks new entries (ARCHITECTURE.md §8).
        Returns the cancelled orders.
        """
        was = bool(self.risk.kill_switch)
        self.risk.set_kill_switch(bool(on), reason)
        if on and not was:
            return await self._cancel_resting_for_kill_switch()
        return []

    async def _cancel_resting_for_kill_switch(self) -> list[Any]:
        why = self.risk.kill_switch_reason or "on"
        try:
            async with self._exec_lock:  # after any placement in flight (it passed risk before the switch)
                cancelled = await self.broker.cancel_all(reason=f"kill switch: {why}")
        except Exception as e:
            log.exception("kill switch: cancelling resting orders failed")
            self.log("error", "risk", f"kill switch: cancelling resting orders failed: {type(e).__name__}: {e}")
            return []
        if cancelled:
            self.log("warning", "risk", f"kill switch on: cancelled {len(cancelled)} resting order(s)",
                     order_ids=[o.id for o in cancelled])
            self.publish_account()
        return cancelled

    def request_universe_refresh(self) -> None:
        """Refresh the universe soon, bypassing the 60 s minimum (e.g. a strategy was enabled)."""
        self._force_universe = True
        job = self.jobs.get("universe")
        if job is not None:
            job.next_due = self.mono()
            self._wake.set()

    async def _run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or self._stop  # each run owns its stop event
        try:
            while not stop.is_set():
                now = self.mono()
                for job in self.jobs.values():
                    if stop.is_set():
                        break
                    if now >= job.next_due:
                        await self._run_job(job)
                        now = self.mono()
                if self._account_dirty and self.mono() - self._last_account_pub >= 2:
                    self.publish_account()
                nxt = min(j.next_due for j in self.jobs.values())
                delay = min(max(0.05, nxt - self.mono()), 1.0)
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), delay)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # the scheduler itself must never die silently
            self._set_error(f"engine loop crashed: {type(e).__name__}: {e}")
            log.exception("engine loop crashed")
        finally:
            if self._task is None or self._task is asyncio.current_task():
                self.running = False

    async def _run_job(self, job: _Job) -> None:
        if job.background:
            if job.task is not None and not job.task.done():
                job.next_due = self.mono() + 1.0
                return
            job.next_due = self.mono() + job.interval
            job.task = asyncio.create_task(self._job_wrapper(job), name=f"kalshibot-{job.name}")
            return
        await self._job_wrapper(job)

    async def _job_wrapper(self, job: _Job) -> None:
        t0 = self.mono()
        job.last_run = self.clock()
        try:
            await job.fn()
        except asyncio.CancelledError:
            raise
        except _Postpone as p:
            job.next_due = self.mono() + p.delay
            return
        except Exception as e:
            job.failures += 1
            net = is_network_error(e)
            cap = MAX_EXCHANGE_BACKOFF_S if job.name == "exchange" else MAX_BACKOFF_S
            delay = min(job.interval * (2 ** min(job.failures, 8)), max(cap, job.interval)) if net else job.interval
            job.next_due = self.mono() + max(delay, 1.0)
            job.last_error = f"{type(e).__name__}: {e}"
            job.last_duration_s = round(self.mono() - t0, 3)
            self._set_error(f"{job.name}: {job.last_error}", job=job.name)
            if net:
                self.log("warning", "engine", f"{job.name} failed ({job.last_error}); "
                         f"retry in {delay:.0f}s (attempt {job.failures})", job=job.name)
            else:
                log.exception("engine job %s failed", job.name)
                self.log("error", "engine", f"{job.name} failed: {job.last_error}", job=job.name)
            return
        job.failures = 0
        job.runs += 1
        job.last_error = None
        if self._error_job == job.name:  # the job that set last_error recovered
            self.last_error = None
            self._error_job = None
        job.last_duration_s = round(self.mono() - t0, 3)
        if job.schedule is not None:
            job.next_due = job.schedule(t0)
        elif not job.background:
            job.next_due = t0 + job.interval if self.mono() < t0 + job.interval else self.mono() + 0.05

    def _set_error(self, msg: str, *, job: str | None = None) -> None:
        self.last_error = msg
        self.last_error_at = self.clock()
        self._error_job = job

    # ------------------------------------------------------------------ jobs

    async def _job_universe(self) -> None:
        self._sync_specs()
        force = self._force_universe
        refreshed = await self.md.refresh_universe(force=force)
        if force:
            if refreshed:
                self._force_universe = False
            else:  # a refresh with the same specs just finished: retry when the guard allows
                wait = getattr(self.md, "force_refresh_wait_s", None)
                raise _Postpone(max(0.5, float(wait()) if callable(wait) else 5.0), "forced refresh deferred")
        if refreshed and self.md.last_error:
            self.log("warning", "marketdata", f"universe refresh partial: {self.md.last_error}")
        if refreshed:
            await self.md.prefetch_series(self.series_prefetch)
        refresh_fees = getattr(self.md, "refresh_fee_schedule", None)
        if callable(refresh_fees):
            await refresh_fees()

    def _success_count(self) -> int:
        return int(getattr(self.client, "success_count", 0) or 0)

    async def _job_exchange(self, fresh: bool = False) -> None:
        st = await (self.md.exchange_status(max_age_s=0) if fresh else self.md.exchange_status())
        err = getattr(self.md, "exchange_error", None)
        if err:
            if not self.kalshi_down:
                self.log("warning", "engine", f"Kalshi unreachable ({err}); strategy ticks paused")
                self._down_mark = self._success_count()
            self.kalshi_down = True
            raise KalshiAPIError(None, err, "/exchange/status")
        if self.kalshi_down:
            self.log("info", "engine", "Kalshi reachable again; resuming strategy ticks")
        self.kalshi_down = False
        self.broker.set_exchange_status(st)
        paused = isinstance(st, Mapping) and (st.get("trading_active") is False or st.get("exchange_active") is False)
        if paused != self.trading_paused:
            self.log("warning" if paused else "info", "engine",
                     "exchange trading paused; strategy ticks skipped" if paused else "exchange trading active")
        self.trading_paused = paused

    async def _job_tick(self) -> None:
        """Gate (first universe refresh, Kalshi reachable, trading active), then start the
        ticks of the strategies whose slot has come (non-blocking; see :meth:`dispatch`)."""
        self._tick_gated = False
        if self.md.refresh_count == 0 and self.jobs["universe"].task is not None \
                and not self.jobs["universe"].task.done():
            raise _Postpone(1.0, "waiting for the first universe refresh")
        if self.kalshi_down and self._success_count() > self._down_mark:
            # another Kalshi request succeeded since the outage began: re-check right away
            # instead of waiting for the backed-off exchange job
            job = self.jobs["exchange"]
            self._down_mark = self._success_count()  # one re-check per new success
            with contextlib.suppress(Exception):
                await self._job_exchange(fresh=True)
                job.failures = 0
                job.last_error = None
                job.next_due = self.mono() + job.interval
                if self._error_job == "exchange":
                    self.last_error = None
                    self._error_job = None
        if self.kalshi_down or self.trading_paused:
            self._tick_gated = True
            return
        if not any(rt.enabled for rt in self.runtimes.values()):
            if self.mono() >= self._heartbeat_due:  # nothing to run: keep the tick heartbeat
                self._heartbeat_due = self.mono() + self.intervals["tick"]
                await self.tick()
            return
        self.dispatch()

    def _next_tick_due(self, t0: float) -> float:
        """When the tick job must look again: the earliest strategy slot (1 s while gated)."""
        mono = self.mono()
        if self._tick_gated:
            return mono + 1.0
        slots = [rt.next_due for rt in self.runtimes.values() if rt.enabled]
        nxt = min(slots) if slots else self._heartbeat_due
        return max(nxt, mono + 0.05)

    def dispatch(self) -> list[str]:
        """Start a tick task for every enabled strategy whose slot is due; returns their names.

        Every strategy has its own fixed grid of :meth:`strategy_interval` seconds (anchored
        at its first tick; no drift). A late start runs the latest due slot once and counts
        the earlier ones as skipped; a slot that comes while the strategy's previous tick is
        still running is skipped too - never queued. Strategies tick concurrently (a slow
        one never delays another); their risk checks and placements are serialized by the
        execution lock. The strategies started together form one round: when all of them
        finished, ``tick_count`` advances and a ``tick`` event is published.
        """
        mono = self.mono()
        due: list[StrategyRuntime] = []
        for name in sorted(self.runtimes):
            rt = self.runtimes[name]
            if not rt.enabled or rt.next_due > mono:
                continue
            iv = self.strategy_interval(rt)
            if rt.next_due <= 0:
                slot, missed = mono, 0
            else:
                missed = int((mono - rt.next_due) // iv)
                slot = rt.next_due + missed * iv  # the latest slot not after now
            rt.next_due = slot + iv
            busy = rt.busy
            skipped = missed + (1 if busy else 0)
            if skipped:
                rt.skipped_ticks += skipped
                if rt.skip_warned_at is None or mono - rt.skip_warned_at >= SKIP_WARN_EVERY_S:
                    rt.skip_warned_at = mono
                    why = "its previous tick is still running" if busy else "the engine was late"
                    self.log("warning", "strategy", f"strategy {rt.name}: skipped {skipped} tick slot(s) "
                             f"({why}; interval {iv:g}s, {rt.skipped_ticks} skipped so far)", strategy=rt.name)
            if busy:
                continue
            rt.last_lag_ms = round((mono - slot) * 1000, 1)
            due.append(rt)
        if not due:
            return []
        now = self.clock()
        tasks = []
        for rt in due:
            rt.task = asyncio.create_task(self._tick_strategy(rt, now), name=f"kalshibot-tick-{rt.name}")
            tasks.append(rt.task)
        names = [rt.name for rt in due]
        rnd = asyncio.create_task(self._finish_round(tasks, names, now, mono), name="kalshibot-tick-round")
        self._rounds.add(rnd)
        rnd.add_done_callback(self._rounds.discard)
        return names

    async def _finish_round(self, tasks: list[asyncio.Task[Any]], names: list[str], now: datetime,
                            t0: float) -> dict[str, Any]:
        res = await asyncio.gather(*tasks, return_exceptions=True)
        total = sum(r for r in res if isinstance(r, int) and not isinstance(r, bool))
        return self._tick_done(now, t0, total, names)

    def _tick_done(self, now: datetime, t0: float, total: int, names: list[str]) -> dict[str, Any]:
        self.tick_count += 1
        self.last_tick_at = now
        info = {"ts": iso(now), "tick_count": self.tick_count, "universe_size": self.md.universe_size,
                "duration_ms": round((self.mono() - t0) * 1000, 1), "intents": total, "strategies": names}
        self.bus.publish("tick", info)
        self.publish_account()
        return info

    async def tick(self) -> dict[str, Any]:
        """One tick of every enabled strategy, one after the other, now (tests / manual use;
        the running engine schedules each strategy on its own grid via :meth:`dispatch`)."""
        t0 = self.mono()
        refresh_fees = getattr(self.md, "refresh_fee_schedule", None)
        if callable(refresh_fees):
            await refresh_fees()  # no request unless the fee schedule is due (every 30 min)
        now = self.clock()
        total = 0
        ran = []
        for name in sorted(self.runtimes):
            rt = self.runtimes[name]
            if not rt.enabled:
                continue
            ran.append(name)
            total += await self._tick_strategy(rt, now)
        return self._tick_done(now, t0, total, ran)

    async def _tick_strategy(self, rt: StrategyRuntime, now: datetime) -> int:
        t0 = self.mono()
        ctx: EngineContext | None = None
        try:
            markets = self.md.markets_for(rt.spec(), now)
            ctx = EngineContext(self, rt, now, markets, self.broker.portfolio())
            result = await asyncio.wait_for(rt.instance.on_tick(ctx), self.tick_timeout_s)
            decided_at = self.clock()  # the orders leave now: they meet only books from after this
            items = intents_list(result) + list(ctx.cancels)
        except TimeoutError:
            rt.errors += 1
            rt.last_error = f"on_tick timed out after {self.tick_timeout_s:g}s"
            self.log("error", "strategy", f"strategy {rt.name}: {rt.last_error}", strategy=rt.name)
            return 0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            rt.errors += 1
            rt.last_error = f"{type(e).__name__}: {e}"
            log.exception("strategy %s on_tick failed", rt.name)
            self.log("error", "strategy", f"strategy {rt.name} on_tick failed: {rt.last_error}", strategy=rt.name)
            return 0
        finally:
            rt.ticks += 1
            rt.last_tick_at = now
            rt.last_duration_ms = round((self.mono() - t0) * 1000, 1)
        rt.last_error = None
        rt.last_ok_at = now
        cancels = [x for x in items if isinstance(x, CancelIntent)]
        intents = self._cap_intents(rt, [x for x in items if not isinstance(x, CancelIntent)])
        rt.intents += len(intents)
        try:
            await self.execute_intents(rt.name, [*cancels, *intents], seen=ctx.portfolio if ctx else None,
                                       decided_at=decided_at)
        finally:
            self._save_state(rt)
        return len(intents)

    def _cap_intents(self, rt: StrategyRuntime, intents: list[Any]) -> list[Any]:
        """At most ``max_intents_per_tick`` order intents; a basket (``group_id``) is kept or
        dropped whole, never split."""
        if len(intents) <= self.max_intents_per_tick:
            return intents
        groups: dict[str, list[Any]] = {}
        for i, raw in enumerate(intents):
            gid = _field(raw, "group_id")
            groups.setdefault(f"group:{gid}" if gid else f"single:{i}", []).append(raw)
        kept: list[Any] = []
        for legs in groups.values():
            if len(kept) + len(legs) > self.max_intents_per_tick:
                break
            kept.extend(legs)
        self.log("warning", "strategy", f"strategy {rt.name}: {len(intents) - len(kept)} intents over the "
                 f"max_intents_per_tick limit ({self.max_intents_per_tick}) were dropped", strategy=rt.name)
        return kept

    def _save_state(self, rt: StrategyRuntime) -> None:
        if self.store is None:
            return
        try:
            state = rt.instance.dump_state()
        except Exception as e:
            self.log("warning", "strategy", f"strategy {rt.name}: dump_state failed: {e}", strategy=rt.name)
            return
        if state is not None and state != rt.last_state:
            try:
                self.store.save_strategy_state(rt.name, state=jsonable(state))
                rt.last_state = state
            except Exception as e:
                self.log("warning", "strategy", f"strategy {rt.name}: saving state failed: {e}", strategy=rt.name)

    async def _job_orders(self) -> None:
        if not self.broker.open_orders():
            return
        fills = await self.broker.process_resting_orders()
        if fills:
            self._account_dirty = True

    async def _job_series(self) -> None:
        """Series-scoped universe refresh for specs with ``refresh_s`` (newly listed markets of
        short-lived series, e.g. 15-minute crypto windows): one request per series."""
        specs = {n: rt.spec() for n, rt in self.runtimes.items() if rt.enabled}
        series, _ = self._fast_series(specs)
        refresh = getattr(self.md, "refresh_series", None)
        if not series or not callable(refresh) or self.md.refresh_count == 0:
            return  # nothing asked for, or the first full refresh has not happened yet
        await refresh(series)

    async def _job_preclose(self) -> None:
        """One extra mark of each held market in its last ``preclose_mark_s`` seconds before
        close, so the mark that freezes at close (until the result) is a recent pre-close one.
        No request unless a held market is about to close."""
        if self.preclose_mark_s <= 0:
            return
        held = {p.ticker for p in self.broker.positions()}
        for t in [t for t in self._preclose_done if t not in held]:
            del self._preclose_done[t]
        if not held:
            return
        now = self.clock()
        due: list[str] = []
        for t in sorted(held):
            m = self.md.known_market(t)
            close = m.close_time if m is not None else None
            if close is None or self._preclose_done.get(t) == close:
                continue
            if 0 < (close - now).total_seconds() <= self.preclose_mark_s:
                self._preclose_done[t] = close
                due.append(t)
        if due:
            await self.md.orderbooks(due, max_age_s=0.5)
            await self.broker.refresh_marks(due)

    async def _job_postclose(self) -> None:
        """Settlement poll for held markets in their first ``postclose_window_s`` after close
        (e.g. KXBTC15M is finalized ~6 s after close), on top of ``settlement_poll_s``: the
        payout lands (and the cash is free again) within seconds. No request otherwise."""
        if self.postclose_window_s <= 0:
            return
        now = self.clock()
        for t in sorted({p.ticker for p in self.broker.positions()}):
            m = self.md.known_market(t)
            close = m.close_time if m is not None else None
            if close is not None and 0 <= (now - close).total_seconds() <= self.postclose_window_s:
                await self._job_settlement()
                return

    async def _job_settlement(self) -> None:
        held = sorted({p.ticker for p in self.broker.positions()})
        if not held:
            return
        try:
            await self.md.refresh_markets(held)
        except Exception as e:  # the broker falls back to single-market fetches
            if is_network_error(e):
                raise
            log.warning("batch market refresh failed: %s", e)
        settled = await self.broker.check_settlements()
        if settled:
            self._account_dirty = True

    async def _job_snapshot(self) -> None:
        held = sorted({p.ticker for p in self.broker.positions()})
        if held:
            await self.md.orderbooks(held, max_age_s=self.broker.mark_max_age_s)
        await self.broker.record_equity_snapshot(refresh=True)
        reconcile = getattr(self.broker, "reconcile", None)
        if reconcile is not None:  # live: compare the ledger with the exchange (report only)
            try:
                await reconcile()
            except Exception as e:
                log.warning("live reconcile failed: %s", e)
        was = self.risk.kill_switch
        on = self.risk.evaluate(self.broker.portfolio())
        if on and not was:
            self.log("warning", "risk", f"kill switch tripped: {self.risk.kill_switch_reason}")
            await self._cancel_resting_for_kill_switch()
        self.publish_account()

    async def _job_housekeeping(self) -> None:
        if self.store is None:
            return
        for table in ("logs", "signals") if self.keep_rows > 0 else ():  # 0 = never prune
            n = self.store.prune(table, self.keep_rows)
            if n:
                log.info("pruned %d old %s rows", n, table)
        if self.equity_full_days > 0:
            cut = self.clock() - timedelta(days=self.equity_full_days)
            prev = self._equity_cut  # after the first pass only the newly aged rows are scanned
            lo = prev - timedelta(seconds=self.equity_bucket_s) if prev is not None else None
            n = self.store.downsample_equity(cut, self.equity_bucket_s, newer_than=lo)
            self._equity_cut = cut
            if n:
                log.info("downsampled equity snapshots older than %s: %d rows removed", iso(cut), n)

    # ------------------------------------------------------------------ intents -> risk -> broker

    def _normalize(self, strategy: str, raw: Any) -> tuple[OrderIntent | None, str]:
        if isinstance(raw, OrderIntent):
            intent = raw
        elif isinstance(raw, Mapping):
            try:
                intent = OrderIntent(**{k: v for k, v in raw.items()
                                        if k in OrderIntent.__dataclass_fields__})
            except TypeError as e:
                return None, f"invalid intent: {e}"
        else:
            return None, f"invalid intent object {type(raw).__name__}"
        if not intent.strategy:
            intent.strategy = strategy
        elif intent.strategy != strategy:
            return intent, f"intent.strategy {intent.strategy!r} does not match {strategy!r}"
        problems = intent.problems()
        if problems:
            return intent, "invalid intent: " + "; ".join(problems)
        return intent, ""

    async def execute_intents(self, strategy: str, raw_intents: Iterable[Any], *,
                              seen: PortfolioView | None = None,
                              decided_at: datetime | None = None) -> list[dict[str, Any]]:
        """Apply one tick's output; returns the signal rows of its order intents.

        1. :class:`CancelIntent` items and the orders named by ``OrderIntent.replaces`` are
           cancelled first (only the strategy's own open orders; each market is first synced
           with the trade tape, so a cancel can come back filled - see
           :meth:`PaperBroker.cancel_orders`).
        2. Order intents: intents sharing a ``group_id`` form an all-or-none basket
           (:meth:`_execute_basket`, even a single leg), the rest go one by one. Each risk
           check + placement holds the execution lock, so it never interleaves with another
           strategy's (limits see every earlier order), yet a strategy with many intents
           delays another's order by at most one placement.

        ``seen`` is the portfolio snapshot the strategy decided on: a replacement's count is
        reduced by what the replaced order filled since then. ``decided_at`` (set by the tick):
        the orders reach the paper exchange ``paper.taker_latency_s`` later and walk only books
        received from then on (:meth:`PaperBroker.place_order`); the books are fetched in one
        batch at that moment.
        """
        raws = list(raw_intents)
        cancels = [r for r in raws if isinstance(r, CancelIntent)]
        raws = [r for r in raws if not isinstance(r, CancelIntent)]
        replaced = await self._apply_cancels(strategy, cancels, raws, seen)
        if not raws:
            return []
        groups: dict[str, list[Any]] = {}  # insertion-ordered: singles keep their position
        for i, raw in enumerate(raws):
            gid = _field(raw, "group_id")
            groups.setdefault(f"group:{gid}" if gid else f"single:{i}", []).append(raw)
        not_before = self.broker.not_before(decided_at) if decided_at is not None else None
        await self._prime([str(_field(r, "ticker", "") or "") for r in raws], not_before=not_before)
        await self.broker.wait_until(not_before)  # outside the execution lock
        out: list[dict[str, Any]] = []
        for key, legs in groups.items():
            try:
                if key.startswith("group:"):
                    out.extend(await self._execute_basket(strategy, legs, decided_at=decided_at))
                else:
                    out.append(await self._execute_one(strategy, legs[0], replaced, decided_at=decided_at))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let one intent break the tick
                log.exception("executing intent(s) for %s failed", strategy)
                self.log("error", "engine", f"executing {strategy} intent failed: {type(e).__name__}: {e}",
                         strategy=strategy)
        return out

    async def _apply_cancels(self, strategy: str, cancels: list[CancelIntent], raws: list[Any],
                             seen: PortfolioView | None) -> dict[int, tuple[Order, int]]:
        """Cancel what the tick asked for; returns ``{order_id: (final order, filled_count the
        strategy saw)}`` for every order it tried to cancel (explicitly or via ``replaces``)."""
        rt = self.runtimes.get(strategy)
        reasons: dict[int, str] = {}
        ignored: list[str] = []

        def mine(oid: int) -> Order | None:
            o = self.broker.get_order(oid)
            return o if o is not None and o.strategy == strategy else None

        for c in cancels:
            problems = c.problems()
            if c.strategy and c.strategy != strategy:
                problems.append(f"cancel.strategy {c.strategy!r} does not match {strategy!r}")
            if problems:
                ignored.append("; ".join(problems))
                continue
            why = c.reason or "cancelled by strategy"
            if c.order_id is not None:
                o = mine(c.order_id)
                if o is None:
                    ignored.append(f"order {c.order_id} is not an order of {strategy}")
                elif o.is_open and (not c.ticker or o.ticker == c.ticker):
                    reasons.setdefault(o.id, why)
            elif c.ticker:
                for o in self.broker.open_orders(ticker=c.ticker, strategy=strategy):
                    reasons.setdefault(o.id, why)
        for raw in raws:
            rid = _as_int(_field(raw, "replaces"))
            if rid is not None:
                o = mine(rid)
                if o is not None and o.is_open:
                    text = str(_field(raw, "reason", "") or "")
                    reasons.setdefault(o.id, f"replaced: {text}" if text else "replaced by a new order")
        if ignored:
            self.log("warning", "strategy", f"strategy {strategy}: cancel ignored ({'; '.join(ignored)})",
                     strategy=strategy)
        if not reasons:
            return {}
        seen_filled = {o.id: o.filled_count for o in (seen.open_orders if seen is not None else ())}
        for oid in reasons:
            if oid not in seen_filled:
                o = self.broker.get_order(oid)
                seen_filled[oid] = o.filled_count if o is not None else 0
        orders = await self.broker.cancel_orders(sorted(reasons), reasons=reasons, strategy=strategy, sync=True)
        done = [o for o in orders if o.status == "cancelled"]
        other = [o for o in orders if o.status != "cancelled"]
        if rt is not None:
            rt.cancels += len(done)
        if orders:
            msg = f"strategy {strategy}: cancelled {len(done)} resting order(s)"
            if other:
                msg += "; " + ", ".join(f"order {o.id} was {o.status} first" for o in other)
            self.log("info", "strategy", msg, strategy=strategy, order_ids=[o.id for o in done])
            self._account_dirty = True
        return {o.id: (o, seen_filled.get(o.id, 0)) for o in orders}

    async def _prime(self, tickers: list[str], *, not_before: datetime | None = None) -> None:
        """Fetch the markets (fresh) and books of many tickers in batches, so per-order reads
        (risk check, the broker's fresh-market and fresh-book reads) are cache hits. With
        ``not_before`` (the orders' arrival) the books are fetched only from that moment on."""
        tickers = [t for t in dict.fromkeys(tickers) if t]
        if len(tickers) < 2:
            return
        refresh = getattr(self.md, "refresh_markets", None)
        if callable(refresh):
            try:
                await refresh(tickers)
            except Exception as e:
                log.warning("batched market refresh for intents failed: %s", e)
        books = getattr(self.md, "orderbooks", None)
        if callable(books):
            try:
                if not_before is not None:
                    await self.broker.wait_until(not_before)
                    await books(tickers, max_age_s=0)  # received after the orders' arrival
                else:
                    await books(tickers, max_age_s=self.broker.book_max_age_s)
            except Exception as e:
                log.warning("batched order books for intents failed: %s", e)

    async def _market_for(self, ticker: str) -> Market:
        m = self.md.markets.get(ticker)
        return m if m is not None else await self.md.market(ticker)

    async def _execute_one(self, strategy: str, raw: Any,
                           replaced: Mapping[int, tuple[Order, int]] | None = None, *,
                           decided_at: datetime | None = None) -> dict[str, Any]:
        intent, problem = self._normalize(strategy, raw)
        if intent is None or problem:
            return self._signal(strategy, intent or raw, None, "rejected", problem)
        note = ""
        if intent.replaces is not None:
            old, seen_filled = (replaced or {}).get(intent.replaces, (None, 0))
            if old is None:
                return self._signal(strategy, intent, None, "rejected",
                                    f"replaces order {intent.replaces}, which is not an open order of {strategy}")
            if old.status != "cancelled":
                return self._signal(strategy, intent, None, "rejected",
                                    f"replaced order {old.id} was {old.status} before the cancel; not replaced")
            filled = old.filled_count - seen_filled
            if filled > 0:
                if filled >= intent.count:
                    return self._signal(strategy, intent, None, "rejected", f"replaced order {old.id} filled "
                                        f"{filled} more before the cancel; nothing left to replace")
                intent = dataclasses.replace(intent, count=intent.count - filled)
                note = f"count reduced by {filled} (filled on replaced order {old.id} before the cancel)"
        try:
            market = await self._market_for(intent.ticker)
        except Exception as e:
            return self._signal(strategy, intent, None, "rejected", f"market unavailable: {e}")
        book: Orderbook | None = None
        try:
            book = await self.md.orderbook(intent.ticker, max_age_s=self.broker.book_max_age_s)
        except Exception as e:
            log.warning("book for %s unavailable for the risk check: %s", intent.ticker, e)
        async with self._exec_lock:  # risk check + placement: atomic w.r.t. other strategies
            decision = self.risk.check(intent, market, self.broker.portfolio(), book=book)
            if decision.approved_count <= 0:
                return self._signal(strategy, intent, market, "rejected", f"risk: {decision.reason}")
            order = await self.broker.place_order(intent, count=decision.approved_count, decided_at=decided_at)
        verdict, why = self._order_outcome(order, intent.count, decision)
        if note:
            why = f"{note}; {why}" if why else note
        return self._signal(strategy, intent, market, verdict, why, order_id=order.id)

    async def _execute_basket(self, strategy: str, raws: list[Any], *,
                              decided_at: datetime | None = None) -> list[dict[str, Any]]:
        """All-or-none: risk-checked together (:meth:`RiskManager.check_basket`; any trimmed
        leg rejects the basket) and placed with ``place_basket(all_or_none=True)``."""
        legs: list[OrderIntent] = []
        targets: list[Any] = []  # what each raw leg's signal row describes
        problems: list[str] = []
        for raw in raws:
            intent, problem = self._normalize(strategy, raw)
            targets.append(intent if intent is not None else raw)
            if intent is None or problem:
                problems.append(problem or "invalid intent")
            elif intent.replaces is not None:
                problems.append(f"{intent.ticker}: replaces is not supported for basket legs")
            if intent is not None:
                legs.append(intent)
        if problems:
            why = "basket rejected: " + "; ".join(problems)
            return [self._signal(strategy, t, None, "rejected", why) for t in targets]
        markets: list[Market] = []
        for leg in legs:
            try:
                markets.append(await self._market_for(leg.ticker))
            except Exception as e:
                why = f"basket rejected: market {leg.ticker} unavailable: {e}"
                return [self._signal(strategy, lg, None, "rejected", why) for lg in legs]
        books = await self.md.orderbooks([leg.ticker for leg in legs], max_age_s=self.broker.book_max_age_s)
        async with self._exec_lock:  # risk check + placement: atomic w.r.t. other strategies
            # legs are checked cumulatively: each sees the exposure and cash of the legs before it
            decisions = self.risk.check_basket(legs, markets, self.broker.portfolio(),
                                               books=[books.get(leg.ticker) for leg in legs])
            short = [(leg, d) for leg, d in zip(legs, decisions, strict=True) if d.approved_count < leg.count]
            if short:
                why = "basket rejected (all-or-none): " + "; ".join(
                    f"{leg.ticker}: risk approved {d.approved_count}/{leg.count} ({d.reason})" for leg, d in short)
                return [self._signal(strategy, leg, m, "rejected", why)
                        for leg, m in zip(legs, markets, strict=True)]
            orders = await self.broker.place_basket(legs, all_or_none=True,
                                                    counts=[d.approved_count for d in decisions],
                                                    decided_at=decided_at)
        out = []
        for leg, m, d, o in zip(legs, markets, decisions, orders, strict=True):
            if o.status != "rejected":
                self.risk.record_order(strategy=strategy)
            verdict, why = self._order_outcome(o, leg.count, d)
            out.append(self._signal(strategy, leg, m, verdict, why, order_id=o.id))
        return out

    @staticmethod
    def _order_outcome(order: Any, requested: int, decision: Any) -> tuple[str, str]:
        verdict = order.decision
        parts: list[str] = []
        if decision.approved_count < requested:
            parts.append(f"risk: {decision.reason}")
            if verdict == "executed":
                verdict = "partial"
        if order.status == "rejected":
            parts.append(order.status_reason or "rejected by broker")
        else:
            if order.filled_count > 0:
                avg = f" @ {order.avg_fill_price:.4f}" if order.avg_fill_price is not None else ""
                parts.append(f"filled {order.filled_count}/{order.count}{avg}, fees ${order.fees:.2f}")
            if order.is_open:
                qa = f", queue ahead {order.queue_ahead:g}" if order.queue_ahead is not None else ""
                parts.append(f"resting {order.remaining} @ {order.limit_price}{qa}")
            elif order.filled_count < order.count:
                parts.append(order.status_reason or f"{order.count - order.filled_count} unfilled ({order.status})")
        return verdict, "; ".join(p for p in parts if p)

    def _signal(self, strategy: str, intent: Any, market: Market | None, decision: str, reason: str,
                *, order_id: int | None = None) -> dict[str, Any]:
        def g(name: str, default: Any = None) -> Any:
            if isinstance(intent, Mapping):
                return intent.get(name, default)
            return getattr(intent, name, default)

        ticker = str(g("ticker", "") or "")
        if market is None and ticker:
            market = self.md.known_market(ticker)
        lp = g("limit_price")
        ee = g("expected_edge")
        cnt = g("count")
        row: dict[str, Any] = {
            "ts": self.clock(), "strategy": strategy, "ticker": ticker, "title": display_title(market),
            "side": g("side"), "action": g("action", "buy"),
            "count": cnt if isinstance(cnt, int) and not isinstance(cnt, bool) else None,
            "limit_price": lp if isinstance(lp, Decimal) else None,
            "fair_value": g("fair_value"), "expected_edge": ee if isinstance(ee, Decimal) else None,
            "reason": str(g("reason", "") or ""), "decision": decision, "decision_reason": reason,
            "order_id": order_id, "group_id": g("group_id"),
        }
        sid = None
        if self.store is not None:
            try:
                sid = self.store.insert_signal(**row)
            except Exception as e:
                log.warning("failed to store signal: %s", e)
        payload = self.signal_json({**row, "id": sid})
        self.bus.publish("signal", payload)
        return payload

    @staticmethod
    def signal_json(row: Mapping[str, Any]) -> dict[str, Any]:
        """``GET /api/signals`` row shape."""
        return {
            "id": row.get("id"),
            "ts": iso(row["ts"]) if isinstance(row.get("ts"), datetime) else row.get("ts"),
            "strategy": row.get("strategy") or "",
            "ticker": row.get("ticker") or "",
            "title": row.get("title") or "",
            "side": row.get("side"),
            "action": row.get("action"),
            "count": row.get("count"),
            "limit_price": jsonable(row.get("limit_price")),
            "fair_value": jsonable(row.get("fair_value")),
            "expected_edge": jsonable(row.get("expected_edge")),
            "reason": row.get("reason") or "",
            "decision": row.get("decision") or "",
            "decision_reason": row.get("decision_reason") or "",
            "order_id": row.get("order_id"),
            "group_id": row.get("group_id"),
        }

    # ------------------------------------------------------------------ events / logging

    def title(self, ticker: str) -> str:
        return display_title(self.md.known_market(ticker))

    def _on_broker_event(self, kind: str, obj: Any) -> None:
        try:
            payload = obj.to_json()
            payload["title"] = self.title(getattr(obj, "ticker", ""))
            self.bus.publish(kind, payload)
        except Exception:
            log.exception("publishing broker %s event failed", kind)
        if kind in ("fill", "settlement"):
            self._account_dirty = True
            rt = self.runtimes.get(getattr(obj, "strategy", ""))
            if rt is not None:
                hook = rt.instance.on_fill if kind == "fill" else rt.instance.on_settlement
                try:
                    hook(obj)
                except Exception as e:
                    log.exception("strategy %s %s hook failed", rt.name, kind)
                    rt.last_error = f"on_{kind}: {type(e).__name__}: {e}"

    def publish_account(self) -> None:
        self._account_dirty = False
        self._last_account_pub = self.mono()
        try:
            self.bus.publish("account", self.broker.account().to_json())
        except Exception:
            log.exception("publishing account failed")

    def log(self, level: str, kind: str, message: str, **data: Any) -> None:
        """Log to python logging, the store (``/api/logs``) and the bus (``log`` event)."""
        log.log(getattr(logging, level.upper(), logging.INFO), "%s: %s", kind, message)
        now = self.clock()
        rid = None
        clean = jsonable(data) if data else None
        if self.store is not None:
            try:
                rid = self.store.insert_log(level, kind, message, clean, ts=now)
            except Exception:
                log.exception("failed to write log row")
        self.bus.publish("log", {"id": rid, "ts": iso(now), "level": level, "kind": kind, "message": message,
                                 "data": clean})

    # ------------------------------------------------------------------ health

    def health(self) -> dict[str, Any]:
        """Is the engine alive and deciding? (``GET /api/health``; ``ok`` False = answer 503 with ``reason``).

        Unhealthy: the engine is stopped or its task died; Kalshi is unreachable; the engine has not
        completed a tick within ``max(120 s, 4 x engine.tick_s)`` of its last tick or its (re)start; or
        an enabled strategy's ``on_tick`` has raised on every tick for that long (no clean tick since
        it was switched on or the engine started). ``/api/status`` answers 200 in all of these cases,
        and a strategy that raises every tick still advances its ``last_tick_at``.

        A scheduled exchange pause is healthy but ``gated="trading_paused"``: no strategy ticks run
        meanwhile, so the tick and strategy checks are skipped."""
        now = self.clock()
        limit = max(HEALTH_MIN_TICK_AGE_S, 4 * self.intervals["tick"])
        out: dict[str, Any] = {"ok": True, "reason": None, "gated": None, "max_tick_age_s": limit,
                               "last_tick_at": iso(self.last_tick_at), "tick_age_s": None,
                               "failing_strategies": []}

        def bad(reason: str) -> dict[str, Any]:
            out.update(ok=False, reason=reason)
            return out

        task = self._task
        if task is not None and task.done():
            return bad(f"the engine task died ({self.last_error or 'no error recorded'})")
        if task is None or not self.running:
            return bad("the engine is stopped")
        if self.kalshi_down:
            return bad(f"Kalshi is unreachable ({getattr(self.md, 'exchange_error', None) or self.last_error})")
        if self.trading_paused:
            out["gated"] = "trading_paused"
            return out
        marks = [t for t in (self.started_at, self.last_tick_at) if t is not None]
        age = (now - max(marks, default=now)).total_seconds()
        out["tick_age_s"] = round(age, 1)
        if age > limit:
            return bad(f"the engine has not ticked for {age:.0f}s (limit {limit:.0f}s)")
        for name in sorted(self.runtimes):
            rt = self.runtimes[name]
            if not rt.enabled or rt.last_error is None:
                continue
            since = max((t for t in (rt.last_ok_at, rt.enabled_at, self.started_at) if t is not None),
                        default=now)
            failing_s = (now - since).total_seconds()
            if failing_s > limit:
                out["failing_strategies"].append({"name": name, "failing_for_s": round(failing_s, 1),
                                                  "last_error": rt.last_error})
        if out["failing_strategies"]:
            f = out["failing_strategies"][0]
            names = ", ".join(x["name"] for x in out["failing_strategies"])
            return bad(f"strategy {names}: on_tick has raised on every tick for {f['failing_for_s']:.0f}s "
                       f"(limit {limit:.0f}s; {f['name']}: {f['last_error']})")
        return out

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        """``engine`` block of ``GET /api/status`` (+ extras)."""
        mono = self.mono()
        return {
            "running": bool(self.running and self._task is not None and not self._task.done()),
            "started_at": iso(self.started_at),
            "last_tick_at": iso(self.last_tick_at),
            "tick_count": self.tick_count,
            "universe_size": self.md.universe_size,
            "last_error": self.last_error,
            "kill_switch": bool(self.risk.kill_switch),
            "kill_switch_reason": self.risk.kill_switch_reason or None,
            "last_error_at": iso(self.last_error_at),
            "kalshi_reachable": not self.kalshi_down,
            "trading_paused": self.trading_paused,
            "universe": {
                "last_refresh": iso(self.md.last_refresh),
                "last_refresh_duration_s": self.md.last_refresh_duration_s,
                "last_error": self.md.last_error,
                "truncated": list(self.md.truncated),
            },
            "strategies_enabled": sorted(n for n, rt in self.runtimes.items() if rt.enabled),
            "jobs": {
                j.name: {
                    "interval_s": j.interval, "runs": j.runs, "failures": j.failures,
                    "last_run": iso(j.last_run), "last_duration_s": j.last_duration_s,
                    "last_error": j.last_error,
                    "next_in_s": round(max(0.0, j.next_due - mono), 1) if self.running else None,
                } for j in self.jobs.values()
            },
        }
