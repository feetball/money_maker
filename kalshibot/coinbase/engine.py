"""CoinbaseEngine: the Coinbase spot PAPER venue's scheduler (docs/COINBASE_CONTRACT.md §10).

PAPER TRADING ONLY. Public market data in, simulated orders out (``SpotPaperBroker``); no
real order, no API key. Fully separate from the Kalshi engine: its own asyncio task, event
bus, kill switch, risk limits and database - an exception here is caught, logged and shown
in ``status()``; it never reaches the Kalshi engine.

Jobs (each isolated; network failures back off exponentially up to 5 min)
    ``products``      ``GET /products`` every ``coinbase.engine.products_refresh_s`` (1 h).
    ``stats``         24 h stats of every product (one request) every 120 s.
    ``bars``          every 5 s: for each enabled strategy whose bar closed at least
                      ``bar_delay_s`` (60 s) ago and was not evaluated yet (the last
                      evaluated bar is persisted, so a restart never re-trades a bar; a newly
                      enabled strategy acts on the latest closed bar):
                      1. candles for the strategy's universe (``history_bars`` closed bars,
                         cached and extended incrementally). If no product of the universe
                         (nor the BTC-USD reference series) has the final bar yet (Coinbase
                         can publish a bar ~60 s late) the evaluation is retried every 20 s
                         for up to 150 s, then runs with what exists. Once the bar is
                         published, products without it had no trades (Coinbase omits empty
                         buckets) and are not waited for;
                      2. ``on_bar(ctx)`` in its own daemon thread (a slow strategy never
                         stalls the event loop the Kalshi venue shares, nor asyncio's shared
                         default executor), with a timeout. A call that times out keeps its
                         thread; the strategy's later bars are skipped (and logged) until it
                         returns, so an instance never runs twice at once;
                      3. ``plan_from_view`` (the same planner as the backtester; prices = the
                         last closes, overridden by live mids of held products);
                      4. per intent, sells first: risk check (fresh book for the spread guard)
                         -> broker. ``execution="maker_then_taker"``: a post-only GTC at the
                         best bid (buys) / ask (sells); after ``maker_timeout_s`` it is
                         cancelled and the remainder is taken (IOC). A post-only order that
                         would cross goes straight to the taker path;
                      5. every intent is recorded as a signal with its decision and reason;
                         a ``bar`` event summarizes the evaluation.
                      Exceptions in one strategy never stop the loop or the other strategies.
    ``maintenance``   resting orders (fills from later public trades, expiry) every
                      ``maintenance_s`` (15 s) + the maker-timeout follow-ups.
    ``snapshot``      marks + equity snapshot + daily-loss kill-switch check every
                      ``snapshot_s`` (60 s); emits ``tick`` and ``account``.
    ``housekeeping``  hourly: prune logs/signals, thin old equity snapshots, drop idle books.

Events on ``self.bus`` (``kalshibot.engine.EventBus``, a separate instance): ``tick, signal,
order, fill, log, account, bar``; every payload carries ``"venue": "coinbase"``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import math
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from kalshibot.coinbase.marketdata import is_network_error
from kalshibot.coinbase.models import Candle, OrderBook, Product, Stats
from kalshibot.coinbase.paper import (
    VENUE,
    SpotOrder,
    SpotOrderIntent,
    SpotPortfolioView,
    f8,
    iso,
)
from kalshibot.coinbase.rebalance import plan_from_view
from kalshibot.coinbase.strategies.base import (
    SpotStrategy,
    resolve_enabled,
    resolve_strategy_params,
    strategy_config,
)
from kalshibot.engine import EventBus, jsonable

__all__ = ["STREAM_EVENT_TYPES", "VENUE", "CoinbaseEngine", "LiveSpotContext", "SpotRuntime"]

log = logging.getLogger(__name__)

STREAM_EVENT_TYPES = ("tick", "signal", "order", "fill", "log", "account", "bar")
MAX_BACKOFF_S = 300.0
LAST_BAR_KEY = "engine.last_bar."
#: at most this many ``ctx.log`` calls per strategy per bar reach the logs table
MAX_STRATEGY_LOGS_PER_BAR = 50
_ZERO = Decimal(0)


def _cb(settings: Any) -> Any:
    """``settings.coinbase`` of a full ``Settings``; a ``CoinbaseSettings`` as is."""
    return getattr(settings, "coinbase", None) or settings


def _f(x: Any) -> float | None:
    if x is None:
        return None
    if isinstance(x, Decimal):
        return f8(x)
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


# --------------------------------------------------------------------------- runtime state


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


@dataclass
class SpotRuntime:
    """One registered strategy inside the engine."""

    name: str
    cls: type[SpotStrategy]
    instance: SpotStrategy
    enabled: bool
    enabled_source: str
    params_error: str | None = None
    last_bar_at: datetime | None = None  # close of the last bar evaluated
    last_run_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    bars_run: int = 0
    last_intents: int = 0
    universe: list[str] = field(default_factory=list)
    next_try: float = 0.0  # monotonic: earliest next attempt (retry / backoff)
    failures: int = 0
    errors: int = 0  # errors recorded so far (a clean bar clears ``last_error``)
    #: the daemon thread of an ``on_bar`` call that outlived its timeout (bars are skipped
    #: until it ends, so the instance never runs twice at once)
    inflight: threading.Thread | None = None

    @property
    def granularity_s(self) -> int:
        return int(getattr(self.instance, "bar_granularity_s", 86400))


@dataclass
class _PendingMaker:
    order_id: int
    strategy: str
    intent: SpotOrderIntent  # the planner's original (taker-sized) intent
    deadline: float  # monotonic


class _NotReady(Exception):
    """The bar's data is not complete yet (retry soon)."""


#: liquid series that tells "the final bar is not published yet" from "no trades in it"
REFERENCE_PRODUCT = "BTC-USD"


def _daemon_call(fn: Callable[..., Any], *args: Any, name: str) -> tuple[asyncio.Future[Any], threading.Thread]:
    """Run ``fn(*args)`` in a new daemon thread. Strategy code may hang: a daemon thread never
    occupies asyncio's shared default executor (which also serves DNS lookups for the Kalshi
    venue's HTTP clients) and never holds up interpreter exit."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Any] = loop.create_future()

    def done(ok: bool, value: Any) -> None:
        if not fut.done():
            if ok:
                fut.set_result(value)
            else:
                fut.set_exception(value)

    def target() -> None:
        try:
            res = fn(*args)
        except BaseException as e:  # noqa: BLE001 - forwarded to the awaiting task
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(done, False, e)
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(done, True, res)

    th = threading.Thread(target=target, name=name, daemon=True)
    th.start()
    return fut, th


class LiveSpotContext:
    """:class:`~kalshibot.coinbase.strategies.base.SpotContext` for one live bar close."""

    __slots__ = ("_candles", "_log", "_portfolio", "_stats", "bar_end", "now", "params", "products")

    def __init__(self, *, now: datetime, bar_end: datetime, products: Mapping[str, Product],
                 params: Mapping[str, Any], candles: Mapping[str, list[Candle]],
                 stats: Callable[[str], Stats | None], portfolio: SpotPortfolioView,
                 log: Callable[[str, dict[str, Any]], None]) -> None:
        self.now = now
        self.bar_end = bar_end
        self.products: Mapping[str, Product] = MappingProxyType(dict(products))
        self.params: Mapping[str, Any] = MappingProxyType(dict(params))
        self._candles = {k: tuple(v) for k, v in candles.items()}
        self._stats = stats
        self._portfolio = portfolio
        self._log = log

    @property
    def portfolio(self) -> SpotPortfolioView:
        return self._portfolio

    def candles(self, product_id: str, n: int) -> list[Candle]:
        """The last ``n`` closed bars (``end <= bar_end``), oldest first (a new list)."""
        bars = self._candles.get(product_id)
        try:
            n = int(n)
        except (TypeError, ValueError):
            return []
        if not bars or n <= 0:
            return []
        return list(bars[-n:])

    def stats(self, product_id: str) -> Stats | None:
        try:
            return self._stats(product_id)
        except Exception:
            return None

    def log(self, msg: str, **data: Any) -> None:
        self._log(str(msg), data)


class _BusLogHandler(logging.Handler):
    """Streams the Coinbase broker's / risk manager's log records (which they store
    themselves) to the Coinbase bus as ``log`` events."""

    def __init__(self, engine: CoinbaseEngine) -> None:
        super().__init__(logging.INFO)
        self.engine = engine

    def emit(self, record: logging.LogRecord) -> None:
        try:
            kind = record.name.rsplit(".", 1)[-1]
            message = record.getMessage()
            args = record.args if isinstance(record.args, tuple) else ()
            if record.msg == "coinbase %s: %s" and len(args) == 2:
                kind, message = str(args[0]), str(args[1])
            elif record.msg == "coinbase risk: %s" and len(args) == 1:
                kind, message = "risk", str(args[0])
            self.engine.bus.publish("log", {
                "venue": VENUE, "id": None, "ts": iso(datetime.fromtimestamp(record.created, tz=UTC)),
                "level": record.levelname.lower(), "kind": kind, "message": message, "data": None})
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)


_LOGGERS = ("kalshibot.coinbase.broker", "kalshibot.coinbase.risk")


# --------------------------------------------------------------------------- engine


class CoinbaseEngine:
    """The Coinbase venue's trading loop. Use from the event loop that owns the broker."""

    def __init__(
        self,
        settings: Any,
        md: Any,
        broker: Any,
        risk: Any,
        store: Any,
        strategies: Mapping[str, type[SpotStrategy]] | Iterable[type[SpotStrategy]] | None = None,
        *,
        bus: EventBus | None = None,
        clock: Callable[[], datetime] | None = None,
        mono: Callable[[], float] = time.monotonic,
        bars_poll_s: float = 5.0,
        stats_s: float = 120.0,
        housekeeping_s: float = 3600.0,
        on_bar_timeout_s: float = 120.0,
        bar_wait_s: float = 150.0,
        bar_wait_frac: float = 0.05,
        bar_wait_max_s: float = 3600.0,
        bar_retry_s: float = 20.0,
        keep_rows: int = 50_000,
        equity_full_days: float = 7.0,
        equity_bucket_s: int = 3600,
    ) -> None:
        self.settings = settings
        self.cb = _cb(settings)
        self.md = md
        self.broker = broker
        self.risk = risk
        self.store = store
        self.bus = bus or EventBus()
        self.clock: Callable[[], datetime] = clock or getattr(broker, "clock", None) or (lambda: datetime.now(UTC))
        self.mono = mono
        eng = getattr(self.cb, "engine", None)
        self.bar_delay_s = float(getattr(eng, "bar_delay_s", 60))
        self.maker_timeout_s = float(getattr(eng, "maker_timeout_s", 120))
        self.intervals = {
            "products": float(getattr(eng, "products_refresh_s", 3600)),
            "stats": float(stats_s),
            "bars": float(bars_poll_s),
            "maintenance": float(getattr(eng, "maintenance_s", 15)),
            "snapshot": float(getattr(eng, "snapshot_s", 60)),
            "housekeeping": float(housekeeping_s),
        }
        self.on_bar_timeout_s = float(on_bar_timeout_s)
        self.bar_wait_s = float(bar_wait_s)
        self.bar_wait_frac = float(bar_wait_frac)
        self.bar_wait_max_s = float(bar_wait_max_s)
        self.bar_retry_s = float(bar_retry_s)
        self.keep_rows = int(keep_rows)
        self.equity_full_days = float(equity_full_days)
        self.equity_bucket_s = int(equity_bucket_s)

        self.running = False
        self.started_at: datetime | None = None
        self.last_tick_at: datetime | None = None
        self.last_bar_at: datetime | None = None
        self.tick_count = 0
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self._error_job: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._lifecycle = asyncio.Lock()
        self._exec_lock = asyncio.Lock()
        self._log_handler: _BusLogHandler | None = None
        self._pending: dict[int, _PendingMaker] = {}
        self._account_dirty = False
        self._last_account_pub = 0.0
        self.load_errors: dict[str, str] = {}

        self.runtimes: dict[str, SpotRuntime] = {}
        self._build_runtimes(strategies)
        self.jobs: dict[str, _Job] = {
            "products": _Job("products", self.intervals["products"], self._job_products),
            "stats": _Job("stats", self.intervals["stats"], self._job_stats, background=True),
            "bars": _Job("bars", self.intervals["bars"], self._job_bars, background=True),
            "maintenance": _Job("maintenance", self.intervals["maintenance"], self._job_maintenance,
                                background=True),
            "snapshot": _Job("snapshot", self.intervals["snapshot"], self._job_snapshot),
            "housekeeping": _Job("housekeeping", self.intervals["housekeeping"], self._job_housekeeping),
        }
        self._unsubscribe = broker.subscribe(self._on_broker_event) if hasattr(broker, "subscribe") else (
            lambda: None)

    # ------------------------------------------------------------------ strategies

    def _build_runtimes(self, strategies: Any) -> None:
        if strategies is None:
            try:
                from kalshibot.coinbase.strategies import LOAD_ERRORS, REGISTRY
            except Exception as e:  # a broken registry leaves the venue running without strategies
                log.exception("coinbase strategies unavailable")
                self.load_errors["kalshibot.coinbase.strategies"] = f"{type(e).__name__}: {e}"
                return
            self.load_errors.update(LOAD_ERRORS)
            items = list(REGISTRY.items())
        elif isinstance(strategies, Mapping):
            items = list(strategies.items())
        else:
            items = [(c.name, c) for c in strategies]
        for name, cls in sorted(items, key=lambda kv: kv[0]):
            try:
                self.runtimes[name] = self._make_runtime(name, cls)
            except Exception as e:
                log.exception("coinbase strategy %s failed to initialize", name)
                self.load_errors[name] = f"{type(e).__name__}: {e}"
        self._install_allocations()

    def _stored_state(self, name: str) -> dict[str, Any]:
        if self.store is None:
            return {}
        try:
            return self.store.get_strategy_state(name) or {}
        except Exception:
            log.exception("reading coinbase strategy state %s failed", name)
            return {}

    def _make_runtime(self, name: str, cls: type[SpotStrategy]) -> SpotRuntime:
        stored = self._stored_state(name)
        cfg_enabled, cfg_params = strategy_config(self.settings, name)
        enabled, source = resolve_enabled(cls, stored=stored.get("enabled"), config=cfg_enabled)
        params, err = resolve_strategy_params(cls, config_params=cfg_params, overrides=stored.get("params") or {})
        inst = cls(params)
        state = stored.get("state")
        if state not in (None, {}, []):
            try:
                inst.load_state(state)
            except Exception as e:
                log.warning("coinbase strategy %s: stored state not loaded: %s", name, e)
        rt = SpotRuntime(name=name, cls=cls, instance=inst, enabled=enabled, enabled_source=source,
                         params_error=err)
        if err:
            rt.last_error = f"params: {err} (using defaults)"
        if self.store is not None:
            with contextlib.suppress(Exception):
                v = self.store.get_kv(LAST_BAR_KEY + name)
                if v:
                    rt.last_bar_at = datetime.fromisoformat(str(v))
        return rt

    def _install_allocations(self) -> None:
        allocs: dict[str, float | None] = {}
        for name, rt in self.runtimes.items():
            entry = None
            fn = getattr(self.cb, "strategy", None)
            if callable(fn):
                with contextlib.suppress(Exception):
                    entry = fn(name)
            v = getattr(entry, "max_allocation_pct", None) if entry is not None else None
            if v is None:
                v = (getattr(rt.cls, "risk_defaults", None) or {}).get("max_allocation_pct")
            if v is not None:
                allocs[name] = float(v)
        if allocs and hasattr(self.risk, "set_strategy_allocations"):
            self.risk.set_strategy_allocations(allocs)

    def update_strategy(self, name: str, *, enabled: bool | None = None,
                        params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Dashboard edits (persisted). ``KeyError`` for an unknown strategy, ``ParamError``
        (a ``ValueError``) for invalid params."""
        rt = self.runtimes[name]
        if params is not None:
            stored = self._stored_state(name)
            _, cfg_params = strategy_config(self.settings, name)
            overrides = {**(stored.get("params") or {}), **dict(params)}
            new_params = rt.cls.resolve_params({**cfg_params, **overrides}, strict=True)
            state = None
            with contextlib.suppress(Exception):
                state = rt.instance.dump_state()
            inst = rt.cls(new_params)
            if state not in (None, {}, []):
                with contextlib.suppress(Exception):
                    inst.load_state(state)
            rt.instance = inst
            rt.params_error = None
            rt.universe = []
            if self.store is not None:
                self.store.save_strategy_state(name, params=jsonable(overrides))
            self.log("info", "strategy", f"{name}: params updated", strategy=name, params=params)
        if enabled is not None:
            changed = bool(enabled) != rt.enabled
            rt.enabled = bool(enabled)
            rt.enabled_source = "dashboard"
            rt.next_try = 0.0
            if self.store is not None:
                self.store.save_strategy_state(name, enabled=bool(enabled))
            if changed:
                self.log("info", "strategy", f"{name} {'enabled' if enabled else 'disabled'} (dashboard)",
                         strategy=name)
            self._wake.set()
        return self.strategy_json(name)

    def reset_strategies(self) -> None:
        """After an account reset: fresh strategy instances (same params), forget evaluated
        bars (the next start re-evaluates the latest closed bar) and maker follow-ups."""
        self._pending.clear()
        for name, rt in self.runtimes.items():
            with contextlib.suppress(Exception):
                rt.instance = rt.cls(dict(rt.instance.params))
            rt.last_bar_at = None
            rt.bars_run = 0
            rt.last_error = None
            rt.next_try = 0.0
            rt.failures = 0
            if self.store is not None:
                with contextlib.suppress(Exception):
                    self.store.save_strategy_state(name, state={})
                    self.store.delete_kv(LAST_BAR_KEY + name)
        self.last_bar_at = None

    def _universe(self, rt: SpotRuntime) -> list[str]:
        if rt.universe:
            return rt.universe
        products = self._products()
        if not products:
            return []
        try:
            uni = [p for p in dict.fromkeys(rt.instance.universe(MappingProxyType(dict(products)))) if p in products]
        except Exception as e:
            rt.last_error = f"universe: {type(e).__name__}: {e}"
            return []
        rt.universe = uni
        return uni

    def _products(self) -> dict[str, Product]:
        fn = getattr(self.md, "usd_products", None)
        return fn() if callable(fn) else {}

    def strategy_json(self, name: str, stats: Mapping[str, Any] | None = None) -> dict[str, Any]:
        rt = self.runtimes[name]
        if stats is None:
            stats = self._strategy_stats()
        st = stats.get(name) or {}
        inst = rt.instance
        return {
            "venue": VENUE,
            "name": name,
            "description": getattr(rt.cls, "description", "") or "",
            "experimental": bool(getattr(rt.cls, "experimental", False)),
            "enabled": rt.enabled,
            "enabled_source": rt.enabled_source,
            "params": jsonable(dict(inst.params)),
            "param_schema": jsonable(rt.cls.schema_json()),
            "bar_granularity_s": rt.granularity_s,
            "universe": list(self._universe(rt)),
            "backtestable": bool(getattr(rt.cls, "backtestable", True)),
            # extras
            "history_bars": int(getattr(inst, "history_bars", 0) or 0),
            "execution": str(getattr(inst, "execution", "taker")),
            "rebalance_band": _f(getattr(inst, "rebalance_band", None)),
            "params_error": rt.params_error,
            "stats": {
                "orders": int(st.get("orders", 0) or 0),
                "fills": int(st.get("fills", 0) or 0),
                "open_positions": int(st.get("open_positions", 0) or 0),
                "open_orders": int(st.get("open_orders", 0) or 0),
                "realized_pnl": _f(st.get("realized_pnl", _ZERO)) or 0.0,
                "unrealized_pnl": _f(st.get("unrealized_pnl", _ZERO)) or 0.0,
                "fees": _f(st.get("fees", _ZERO)) or 0.0,
                "exposure": _f(st.get("exposure", _ZERO)) or 0.0,
                "value": _f(st.get("value", _ZERO)) or 0.0,
                "trades": int(st.get("trades", 0) or 0),
                "win_rate": _f(st.get("win_rate")),
                "allocation_pct": _f(self.risk.allocation_pct(name)) if hasattr(self.risk, "allocation_pct")
                else None,
                "last_bar_at": iso(rt.last_bar_at),
                "last_run_at": iso(rt.last_run_at),
                "bars_run": rt.bars_run,
                "last_intents": rt.last_intents,
                "last_error": rt.last_error,
                "last_error_at": iso(rt.last_error_at),
            },
        }

    def _strategy_stats(self) -> Mapping[str, Any]:
        try:
            return self.broker.strategy_stats()
        except Exception:
            log.exception("coinbase strategy stats failed")
            return {}

    def strategies_json(self) -> list[dict[str, Any]]:
        stats = self._strategy_stats()
        return [self.strategy_json(n, stats) for n in sorted(self.runtimes)]

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Start the loop (idempotent)."""
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
            self.jobs["housekeeping"].next_due = now + 60
            for rt in self.runtimes.values():
                rt.next_try = 0.0
            if self._log_handler is None:
                self._log_handler = _BusLogHandler(self)
                for name in _LOGGERS:
                    logging.getLogger(name).addHandler(self._log_handler)
            states = ", ".join(f"{n} {'on' if rt.enabled else 'off'} ({rt.enabled_source})"
                               for n, rt in sorted(self.runtimes.items()))
            self.log("info", "engine", f"coinbase engine started (paper trading); strategies: {states or 'none'}")
            self._task = asyncio.create_task(self._run(stop), name="coinbase-engine")

    async def stop(self, timeout: float = 15.0) -> None:
        """Stop the loop; background jobs get a moment to finish, then are cancelled."""
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
                log.exception("coinbase engine task ended with an error")
            await self._cancel_background(grace_s=min(5.0, timeout))
            if self._task is task:
                self._task = None
                self.running = False
            if self._log_handler is not None:
                for name in _LOGGERS:
                    logging.getLogger(name).removeHandler(self._log_handler)
                self._log_handler = None
            self.log("info", "engine", "coinbase engine stopped")

    async def close(self, timeout: float = 15.0) -> None:
        await self.stop(timeout=timeout)
        with contextlib.suppress(Exception):
            self._unsubscribe()

    async def _cancel_background(self, grace_s: float = 5.0) -> None:
        tasks = [j.task for j in self.jobs.values() if j.task is not None and not j.task.done()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=grace_s)
            for t in pending:
                t.cancel()
            for t in pending:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        for job in self.jobs.values():
            job.task = None

    @property
    def is_running(self) -> bool:
        return bool(self.running and self._task is not None and not self._task.done())

    async def set_kill_switch(self, on: bool, reason: str = "manual") -> list[SpotOrder]:
        """Turn the Coinbase kill switch on/off; engaging it cancels resting BUY orders
        (sells reduce risk and may keep resting). Returns the cancelled orders."""
        was = bool(self.risk.kill_switch)
        self.risk.set_kill_switch(bool(on), reason)
        if on and not was:
            return await self._cancel_buys_for_kill_switch()
        return []

    async def _cancel_buys_for_kill_switch(self) -> list[SpotOrder]:
        why = getattr(self.risk, "kill_switch_reason", "") or "on"
        try:
            async with self._exec_lock:
                cancelled = await self.broker.cancel_all(side="buy", reason=f"kill switch: {why}")
        except Exception as e:
            log.exception("coinbase kill switch: cancelling resting buys failed")
            self.log("error", "risk", f"kill switch: cancelling resting buys failed: {type(e).__name__}: {e}")
            return []
        for o in cancelled:
            self._pending.pop(o.id, None)
        if cancelled:
            self.log("warning", "risk", f"kill switch on: cancelled {len(cancelled)} resting buy order(s)",
                     order_ids=[o.id for o in cancelled])
            self.publish_account()
        return list(cancelled)

    # ------------------------------------------------------------------ loop

    async def _run(self, stop: asyncio.Event) -> None:
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
            log.exception("coinbase engine loop crashed")
        finally:
            if self._task is None or self._task is asyncio.current_task():
                self.running = False

    async def _run_job(self, job: _Job) -> None:
        if job.background:
            if job.task is not None and not job.task.done():
                job.next_due = self.mono() + 1.0
                return
            job.next_due = self.mono() + job.interval
            job.task = asyncio.create_task(self._job_wrapper(job), name=f"coinbase-{job.name}")
            return
        await self._job_wrapper(job)

    async def _job_wrapper(self, job: _Job) -> None:
        t0 = self.mono()
        job.last_run = self.clock()
        try:
            await job.fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            job.failures += 1
            net = is_network_error(e)
            # network trouble: retry soon (even for the hourly products job), backing off to 5 min
            delay = max(1.0, min(min(job.interval, 15.0) * (2 ** min(job.failures, 8)), MAX_BACKOFF_S)) if net \
                else job.interval
            job.next_due = self.mono() + delay
            job.last_error = f"{type(e).__name__}: {e}"
            job.last_duration_s = round(self.mono() - t0, 3)
            self._set_error(f"{job.name}: {job.last_error}", job=job.name)
            if net:
                self.log("warning", "engine", f"{job.name} failed (Coinbase unreachable? {job.last_error}); "
                         f"retry in {delay:.0f}s (attempt {job.failures})", job=job.name)
            else:
                log.exception("coinbase engine job %s failed", job.name)
                self.log("error", "engine", f"{job.name} failed: {job.last_error}", job=job.name)
            return
        job.failures = 0
        job.runs += 1
        job.last_error = None
        if self._error_job == job.name:
            self.last_error = None
            self._error_job = None
        job.last_duration_s = round(self.mono() - t0, 3)
        if not job.background:
            job.next_due = t0 + job.interval if self.mono() < t0 + job.interval else self.mono() + 0.05

    def _set_error(self, msg: str, *, job: str | None = None) -> None:
        self.last_error = msg
        self.last_error_at = self.clock()
        self._error_job = job

    # ------------------------------------------------------------------ jobs

    async def _job_products(self) -> None:
        n = await self.md.refresh_products()
        for rt in self.runtimes.values():
            rt.universe = []  # recomputed against the new product list
        if n == 0:
            self.log("warning", "engine", "Coinbase returned no tradable USD products")

    async def _job_stats(self) -> None:
        fn = getattr(self.md, "refresh_stats", None)
        if callable(fn):
            await fn()

    async def _job_bars(self) -> None:
        await self.run_due_bars()

    async def _job_maintenance(self) -> None:
        fills = await self.broker.maintain()
        if fills:
            self._account_dirty = True
        await self._follow_up_makers()

    async def _job_snapshot(self) -> None:
        await self.broker.mark()
        snap = self.broker.equity_snapshot()
        acct = self.broker.account()
        was = bool(self.risk.kill_switch)
        on = self.risk.evaluate(acct)
        if on and not was:
            self.log("warning", "risk", f"coinbase kill switch tripped: {self.risk.kill_switch_reason}")
            await self._cancel_buys_for_kill_switch()
        self.tick_count += 1
        self.last_tick_at = self.clock()
        self.bus.publish("tick", {
            "venue": VENUE, "ts": iso(self.last_tick_at), "tick_count": self.tick_count,
            "products_loaded": self._products_loaded(), "equity": snap.get("equity"),
            "open_orders": acct.open_orders, "open_positions": acct.open_positions,
            "strategies_enabled": sorted(n for n, rt in self.runtimes.items() if rt.enabled),
            "coinbase_reachable": getattr(self.md, "reachable", None)})
        self.publish_account()

    async def _job_housekeeping(self) -> None:
        if self.store is not None:
            for table in ("logs", "signals"):
                n = self.store.prune(table, self.keep_rows)
                if n:
                    log.info("coinbase: pruned %d old %s rows", n, table)
            if self.equity_full_days > 0:
                cut = self.clock() - timedelta(days=self.equity_full_days)
                n = self.store.downsample_equity(cut, self.equity_bucket_s)
                if n:
                    log.info("coinbase: downsampled %d equity snapshots older than %s", n, iso(cut))
        gc = getattr(self.md, "gc", None)
        if callable(gc):
            gc()

    def _products_loaded(self) -> int:
        v = getattr(self.md, "products_loaded", 0)
        return int(v() if callable(v) else v or 0)

    # ------------------------------------------------------------------ bars

    def bar_wait_for(self, granularity_s: int) -> float:
        """How long past ``bar_delay_s`` to keep waiting for a bar's final candle.

        Coinbase sometimes publishes a closed candle minutes late. Running without it
        makes the daily strategies skip the bar ("no change"), and a day-late decision
        cost the BTC trend rule ~7 pts/yr in research - so daily bars wait up to an hour,
        hourly bars ~3 minutes."""
        return max(self.bar_wait_s, min(self.bar_wait_max_s, granularity_s * self.bar_wait_frac))

    def due_bar_end(self, rt: SpotRuntime, now: datetime | None = None) -> datetime | None:
        """The latest bar close whose ``bar_delay_s`` has passed, if ``rt`` has not evaluated it."""
        now = now or self.clock()
        g = rt.granularity_s
        ts = math.floor((now.timestamp() - self.bar_delay_s) / g) * g
        bar_end = datetime.fromtimestamp(ts, tz=UTC)
        if rt.last_bar_at is not None and rt.last_bar_at >= bar_end:
            return None
        return bar_end

    async def run_due_bars(self, now: datetime | None = None) -> list[str]:
        """Evaluate every enabled strategy with a due bar (sequentially). Returns their names."""
        if self._products_loaded() == 0:
            return []
        done: list[str] = []
        for name in sorted(self.runtimes):
            rt = self.runtimes[name]
            if not rt.enabled or self.mono() < rt.next_try:
                continue
            t = now or self.clock()
            bar_end = self.due_bar_end(rt, t)
            if bar_end is None:
                continue
            try:
                ran = await self.run_bar(rt, bar_end, now=t)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let one strategy stop the others
                log.exception("coinbase strategy %s bar failed", name)
                self._strategy_error(rt, f"bar {iso(bar_end)}: {type(e).__name__}: {e}")
                self._mark_bar_done(rt, bar_end)
                continue
            if ran:
                done.append(name)
        return done

    def _strategy_error(self, rt: SpotRuntime, msg: str, *, level: str = "error") -> None:
        rt.errors += 1
        rt.last_error = msg
        rt.last_error_at = self.clock()
        self.log(level, "strategy", f"{rt.name}: {msg}", strategy=rt.name)

    def _mark_bar_done(self, rt: SpotRuntime, bar_end: datetime) -> None:
        rt.last_bar_at = bar_end
        rt.last_run_at = self.clock()
        rt.failures = 0
        rt.next_try = 0.0
        self.last_bar_at = max(self.last_bar_at, bar_end) if self.last_bar_at else bar_end
        if self.store is not None:
            try:
                self.store.set_kv(LAST_BAR_KEY + rt.name, iso(bar_end))
            except Exception:
                log.exception("coinbase: saving the last bar of %s failed", rt.name)

    async def _load_candles(self, rt: SpotRuntime, universe: list[str], bar_end: datetime,
                            n: int) -> dict[str, list[Candle]]:
        out: dict[str, list[Candle]] = {}
        g = rt.granularity_s
        for pid in universe:
            try:
                out[pid] = await self.md.candles(pid, g, n, bar_end=bar_end)
            except Exception as e:
                if is_network_error(e):
                    raise
                log.warning("coinbase candles for %s unavailable: %s", pid, e)
        return out

    async def run_bar(self, rt: SpotRuntime, bar_end: datetime, *, now: datetime | None = None) -> bool:
        """Evaluate ``rt`` on the bar that closed at ``bar_end`` and execute its plan.

        Returns ``False`` when the evaluation was postponed (data not complete yet, or
        Coinbase unreachable - retried with backoff); raises only for unexpected errors
        (the caller records them and moves on)."""
        now = now or self.clock()
        products = self._products()
        if not products:
            return False
        err_mark = rt.errors
        universe = self._universe(rt)
        n = max(1, int(getattr(rt.instance, "history_bars", 1) or 1))
        if rt.inflight is not None and rt.inflight.is_alive():
            # the previous on_bar is still running in its thread: never run the instance twice
            self._strategy_error(rt, f"bar {iso(bar_end)} skipped: the previous on_bar is still running "
                                     f"(timed out after {self.on_bar_timeout_s:.0f}s)")
            self._mark_bar_done(rt, bar_end)
            self._publish_bar(rt, bar_end, universe, None, error=rt.last_error)
            return True
        rt.inflight = None
        try:
            candles = await self._load_candles(rt, universe, bar_end, n)
            last_needed = bar_end - timedelta(seconds=rt.granularity_s)
            missing = sorted(p for p, bars in candles.items() if bars and bars[-1].start < last_needed)
            if (missing and now < bar_end + timedelta(seconds=self.bar_delay_s + self.bar_wait_for(rt.granularity_s))
                    and not await self._bar_published(candles, rt.granularity_s, bar_end, last_needed)):
                raise _NotReady(missing)
        except _NotReady as e:
            rt.next_try = self.mono() + self.bar_retry_s
            log.info("coinbase %s: bar %s not complete yet for %d product(s); retry in %.0fs",
                     rt.name, iso(bar_end), len(e.args[0]), self.bar_retry_s)
            return False
        except Exception as e:
            if not is_network_error(e):
                raise
            rt.failures += 1
            delay = min(self.bar_retry_s * (2 ** min(rt.failures, 6)), MAX_BACKOFF_S)
            rt.next_try = self.mono() + delay
            self._strategy_error(rt, f"candles unavailable ({type(e).__name__}: {e}); retry in {delay:.0f}s",
                                 level="warning")
            return False
        rt.failures = 0
        with contextlib.suppress(Exception):
            refresh = getattr(self.md, "refresh_stats", None)
            if callable(refresh):
                await refresh()

        view = self.broker.portfolio_view(rt.name, allocation_pct=self.risk.allocation_pct(rt.name))
        logs: list[int] = [0]

        def ctx_log(msg: str, data: dict[str, Any]) -> None:
            logs[0] += 1
            if logs[0] <= MAX_STRATEGY_LOGS_PER_BAR:
                self.log("info", "strategy", f"{rt.name}: {msg}", strategy=rt.name, **data)

        ctx = LiveSpotContext(now=now, bar_end=bar_end, products=products, params=rt.instance.params,
                              candles=candles, stats=self.md.stats, portfolio=view, log=ctx_log)
        fut, thread = _daemon_call(rt.instance.on_bar, ctx, name=f"cb-strategy-{rt.name}")
        try:
            result = await asyncio.wait_for(fut, self.on_bar_timeout_s)
        except TimeoutError:
            rt.inflight = thread  # later bars are skipped until this call returns
            self._strategy_error(rt, f"on_bar timed out after {self.on_bar_timeout_s:.0f}s (bar {iso(bar_end)})")
            self._mark_bar_done(rt, bar_end)
            return True
        except Exception as e:
            log.exception("coinbase strategy %s on_bar failed", rt.name)
            self._strategy_error(rt, f"on_bar: {type(e).__name__}: {e}")
            self._mark_bar_done(rt, bar_end)
            self._publish_bar(rt, bar_end, universe, None, error=rt.last_error)
            return True

        n_intents = 0
        if result is not None:
            n_intents = await self._rebalance(rt, result, products, candles)
        rt.bars_run += 1
        rt.last_intents = n_intents
        if rt.errors == err_mark and rt.failures == 0:
            rt.last_error = None  # a clean bar clears the previous error
        self._mark_bar_done(rt, bar_end)
        self._save_state(rt)
        self._publish_bar(rt, bar_end, universe, n_intents, result_none=result is None)
        return True

    async def _bar_published(self, candles: Mapping[str, list[Candle]], granularity_s: int, bar_end: datetime,
                             last_needed: datetime) -> bool:
        """Whether Coinbase has published the bar that closed at ``bar_end``. Candle buckets
        without trades are omitted, so a product lacking the final bar may simply not have
        traded: once any series of the universe - or the liquid reference series - has the
        bar, the remaining gaps are no-trade buckets and are not waited for."""
        if any(bars and bars[-1].start >= last_needed for bars in candles.values()):
            return True
        if REFERENCE_PRODUCT in candles:
            return False
        try:
            ref = await self.md.candles(REFERENCE_PRODUCT, granularity_s, 1, bar_end=bar_end)
        except Exception as e:
            log.info("coinbase: reference candles unavailable: %s", e)
            return False
        return bool(ref) and ref[-1].start >= last_needed

    def _publish_bar(self, rt: SpotRuntime, bar_end: datetime, universe: list[str], intents: int | None, *,
                     error: str | None = None, result_none: bool = False) -> None:
        self.bus.publish("bar", {
            "venue": VENUE, "ts": iso(self.clock()), "strategy": rt.name, "bar_end": iso(bar_end),
            "granularity_s": rt.granularity_s, "products": len(universe), "intents": intents,
            "no_change": result_none, "error": error})

    def _save_state(self, rt: SpotRuntime) -> None:
        if self.store is None:
            return
        try:
            state = rt.instance.dump_state()
            if state is not None:
                self.store.save_strategy_state(rt.name, state=jsonable(state))
        except Exception as e:
            log.warning("coinbase strategy %s: state not saved: %s", rt.name, e)

    async def _prices(self, rt: SpotRuntime, view: SpotPortfolioView, candles: Mapping[str, list[Candle]],
                      needed: Iterable[str]) -> dict[str, Decimal]:
        prices: dict[str, Decimal] = {pid: bars[-1].close for pid, bars in candles.items() if bars}
        prices.update({p: m for p, m in view.mids.items() if m is not None and m > 0})
        for pid in needed:
            if pid in prices:
                continue
            try:
                book = await self.md.book(pid, max_age_s=10)
            except Exception as e:
                log.info("coinbase %s: no price for %s: %s", rt.name, pid, e)
                continue
            px = book.mid or book.best_bid
            if px is not None and px > 0:
                prices[pid] = px
        return prices

    async def _rebalance(self, rt: SpotRuntime, result: Any, products: Mapping[str, Product],
                         candles: Mapping[str, list[Candle]]) -> int:
        name = rt.name
        view = self.broker.portfolio_view(name, allocation_pct=self.risk.allocation_pct(name))
        target_ids: list[str] = []
        if isinstance(result, Mapping):
            target_ids = [str(k) for k in result]
        elif isinstance(result, Iterable) and not isinstance(result, str | bytes):
            target_ids = [str(getattr(t, "product_id", "")) for t in result]
        needed = {p for p in target_ids if p in products} | set(view.holdings)
        prices = await self._prices(rt, view, candles, needed)
        tier = getattr(self.broker, "tier", None)
        plan = plan_from_view(result, view, products, band=float(getattr(rt.instance, "rebalance_band", 0.02)),
                              min_trade_usd=self.risk.limits.min_trade_usd, strategy=name,
                              fee_rate=getattr(tier, "taker_rate", 0), prices=prices)
        for p in plan.problems:
            self.log("warning", "strategy", f"{name}: {p}", strategy=name)
        if plan.skipped:
            self.log("info", "plan", f"{name}: {len(plan.skipped)} trade(s) not planned",
                     strategy=name, skipped=plan.skipped[:20])
        maker = str(getattr(rt.instance, "execution", "taker")) == "maker_then_taker"
        for intent in plan.intents:  # sells first, then buys (planner order)
            try:
                if maker:
                    await self._execute_maker(name, intent)
                else:
                    await self._execute_taker(name, intent)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("coinbase %s: executing %s %s failed", name, intent.side, intent.product_id)
                self._signal(name, intent, "rejected", f"execution error: {type(e).__name__}: {e}")
        return len(plan.intents)

    # ------------------------------------------------------------------ execution

    async def _book(self, pid: str) -> OrderBook | None:
        try:
            return await self.md.book(pid, max_age_s=2)
        except Exception as e:
            log.info("coinbase: book for %s unavailable: %s", pid, e)
            return None

    @staticmethod
    def _order_reason(order: SpotOrder, risk_note: str = "") -> str:
        parts: list[str] = []
        if risk_note:
            parts.append(f"risk: {risk_note}")
        if order.status_reason:
            parts.append(order.status_reason)
        elif order.filled_base > 0:
            avg = order.avg_fill_price
            parts.append(f"filled {order.filled_base.normalize():f} @ {avg:.8g} (fees ${order.fees:.2f})"
                         if avg is not None else f"filled {order.filled_base.normalize():f}")
        elif order.is_open:
            parts.append(f"resting at {order.limit_price}")
        return "; ".join(parts)

    async def _execute_taker(self, strategy: str, intent: SpotOrderIntent, *, note: str = "") -> SpotOrder | None:
        book = await self._book(intent.product_id) if intent.side == "buy" else None
        async with self._exec_lock:
            view = self.broker.portfolio_view(strategy, allocation_pct=self.risk.allocation_pct(strategy))
            d = self.risk.check(intent, view, self.broker.account(), book=book)
            if not d.approved:
                self._signal(strategy, intent, "rejected", f"risk: {d.reason}")
                return None
            order = await self.broker.place_order(d.apply(intent))
        risk_note = d.reason if d.partial or d.binding_limit else ""
        reason = self._order_reason(order, risk_note)
        if note:
            reason = f"{note}; {reason}" if reason else note
        self._signal(strategy, intent, order.decision, reason, order_id=order.id)
        return order

    async def _execute_maker(self, strategy: str, intent: SpotOrderIntent) -> SpotOrder | None:
        book = await self._book(intent.product_id)
        price = None if book is None else (book.best_bid if intent.side == "buy" else book.best_ask)
        if price is None:
            return await self._execute_taker(strategy, intent, note="maker: no book; taker")
        maker = dataclasses.replace(intent, order_type="limit", tif="gtc", post_only=True, limit_price=price,
                                    expires_in_s=int(self.maker_timeout_s) + 300)
        async with self._exec_lock:
            view = self.broker.portfolio_view(strategy, allocation_pct=self.risk.allocation_pct(strategy))
            d = self.risk.check(maker, view, self.broker.account(), book=book)
            if not d.approved:
                self._signal(strategy, maker, "rejected", f"risk: {d.reason}")
                return None
            order = await self.broker.place_order(d.apply(maker))
        if order.status == "rejected" and "cross" in (order.status_reason or "").lower():
            self._signal(strategy, maker, "rejected", f"{order.status_reason}; taking instead", order_id=order.id)
            return await self._execute_taker(strategy, intent, note="maker would cross; taker")
        risk_note = d.reason if d.partial or d.binding_limit else ""
        self._signal(strategy, maker, order.decision, self._order_reason(order, risk_note), order_id=order.id)
        if order.is_open:
            self._pending[order.id] = _PendingMaker(order.id, strategy, intent, self.mono() + self.maker_timeout_s)
        return order

    async def _follow_up_makers(self) -> None:
        """Maker orders past ``maker_timeout_s``: cancel, then take the unfilled remainder."""
        now = self.mono()
        for oid, pm in list(self._pending.items()):
            o = self.broker.get_order(oid)
            if o is None:
                self._pending.pop(oid, None)
                continue
            if o.is_open:
                if now < pm.deadline:
                    continue
                try:
                    o = await self.broker.cancel_order(oid, reason="maker timeout; taking the remainder")
                except Exception as e:
                    log.warning("coinbase: cancelling maker order %s failed: %s", oid, e)
                    o = self.broker.get_order(oid)
                    if o is None or o.is_open:
                        continue
            self._pending.pop(oid, None)
            if o.status == "filled":
                continue
            intent = pm.intent
            if intent.side == "buy":
                spent = o.filled_quote + o.fees
                if intent.quote_size is None:
                    continue
                rest = (intent.quote_size - spent).quantize(Decimal("0.01"))
                if rest < Decimal(str(self.risk.limits.min_trade_usd)):
                    continue
                follow = dataclasses.replace(intent, quote_size=rest)
            else:
                if intent.base_size is None:
                    continue
                rest_b = intent.base_size - o.filled_base
                if rest_b <= 0:
                    continue
                follow = dataclasses.replace(intent, base_size=rest_b)
            follow = dataclasses.replace(follow, order_type="market", tif="ioc", post_only=False, limit_price=None,
                                         expires_in_s=None)
            try:
                await self._execute_taker(pm.strategy, follow, note=f"taker after maker order {oid} timed out")
            except Exception as e:
                log.exception("coinbase: taker follow-up for maker order %s failed", oid)
                self._signal(pm.strategy, follow, "rejected", f"execution error: {type(e).__name__}: {e}")

    # ------------------------------------------------------------------ signals / events / logging

    def _signal(self, strategy: str, intent: Any, decision: str, reason: str, *,
                order_id: int | None = None) -> dict[str, Any]:
        def g(name: str) -> Any:
            return intent.get(name) if isinstance(intent, Mapping) else getattr(intent, name, None)

        row: dict[str, Any] = {
            "ts": self.clock(), "strategy": strategy, "product_id": str(g("product_id") or ""), "side": g("side"),
            "target_weight": g("target_weight"), "quote_size": g("quote_size"), "base_size": g("base_size"),
            "limit_price": g("limit_price"), "expected_edge_bps": g("expected_edge_bps"),
            "reason": str(g("reason") or ""), "decision": decision, "decision_reason": reason, "order_id": order_id,
        }
        sid = None
        if self.store is not None:
            try:
                sid = self.store.insert_signal(**row)
            except Exception as e:
                log.warning("failed to store coinbase signal: %s", e)
        payload = self.signal_json({**row, "id": sid})
        self.bus.publish("signal", payload)
        return payload

    @staticmethod
    def signal_json(row: Mapping[str, Any]) -> dict[str, Any]:
        """``GET /api/coinbase/signals`` row shape."""
        ts = row.get("ts")
        return {
            "venue": VENUE,
            "id": row.get("id"),
            "ts": iso(ts) if isinstance(ts, datetime) else ts,
            "strategy": row.get("strategy") or "",
            "product_id": row.get("product_id") or "",
            "side": row.get("side"),
            "target_weight": _f(row.get("target_weight")),
            "quote_size": _f(row.get("quote_size")),
            "base_size": _f(row.get("base_size")),
            "limit_price": _f(row.get("limit_price")),
            "expected_edge_bps": _f(row.get("expected_edge_bps")),
            "reason": row.get("reason") or "",
            "decision": row.get("decision") or "",
            "decision_reason": row.get("decision_reason") or "",
            "order_id": row.get("order_id"),
        }

    def _on_broker_event(self, kind: str, obj: Any) -> None:
        try:
            payload = obj.to_json()
            payload.setdefault("venue", VENUE)
            self.bus.publish(kind, payload)
        except Exception:
            log.exception("publishing coinbase broker %s event failed", kind)
        if kind == "fill":
            self._account_dirty = True

    def publish_account(self) -> None:
        self._account_dirty = False
        self._last_account_pub = self.mono()
        try:
            self.bus.publish("account", self.broker.account().to_json())
        except Exception:
            log.exception("publishing coinbase account failed")

    def log(self, level: str, kind: str, message: str, **data: Any) -> None:
        """Log to python logging, the Coinbase store (``/api/coinbase/logs``) and the bus."""
        log.log(getattr(logging, level.upper(), logging.INFO), "coinbase %s: %s", kind, message)
        now = self.clock()
        rid = None
        clean = jsonable({"venue": VENUE, **data}) if data else None
        if self.store is not None:
            try:
                rid = self.store.insert_log(level, kind, message, clean, ts=now)
            except Exception:
                log.exception("failed to write coinbase log row")
        self.bus.publish("log", {"venue": VENUE, "id": rid, "ts": iso(now), "level": level, "kind": kind,
                                 "message": message, "data": clean})

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        """``engine`` block of ``GET /api/coinbase/status`` (+ extras)."""
        mono = self.mono()
        md_status: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            md_status = jsonable(self.md.status())
        return {
            "running": self.is_running,
            "started_at": iso(self.started_at),
            "last_tick_at": iso(self.last_tick_at),
            "last_bar_at": iso(self.last_bar_at),
            "tick_count": self.tick_count,
            "products_loaded": self._products_loaded(),
            "last_error": self.last_error,
            "last_error_at": iso(self.last_error_at),
            "kill_switch": bool(self.risk.kill_switch),
            "kill_switch_reason": getattr(self.risk, "kill_switch_reason", None) or None,
            "coinbase_reachable": getattr(self.md, "reachable", None),
            "strategies_enabled": sorted(n for n, rt in self.runtimes.items() if rt.enabled),
            # extras
            "strategies": sorted(self.runtimes),
            "strategy_load_errors": dict(self.load_errors),
            "pending_maker_orders": len(self._pending),
            "bar_delay_s": self.bar_delay_s,
            "marketdata": md_status,
            "jobs": {
                j.name: {
                    "interval_s": j.interval, "runs": j.runs, "failures": j.failures,
                    "last_run": iso(j.last_run), "last_duration_s": j.last_duration_s,
                    "last_error": j.last_error,
                    "next_in_s": round(max(0.0, j.next_due - mono), 1) if self.running else None,
                } for j in self.jobs.values()
            },
        }
