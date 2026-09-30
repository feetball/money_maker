"""FastAPI app: REST + SSE for the dashboard (ARCHITECTURE.md §12), serves ``frontend/dist``.

PAPER TRADING ONLY. Every endpoint reads or changes the *simulated* account; nothing here
can place a real order.

* ``create_app(settings)`` builds the whole stack (store, public Kalshi client, market
  data, paper broker, risk manager, feeds, engine) in the app lifespan and starts the
  engine when ``engine.autostart`` is true. Tests pass ``services=`` (fakes) instead.
* Errors are ``{"detail": str}`` with a 4xx/5xx status (validation errors are flattened
  to one string). Unknown ``/api/...`` paths are JSON 404s, never the SPA.
* ``GET /api/stream`` is Server-Sent Events: ``event: <type>\\ndata: <json>\\nid: <n>\\n\\n``
  for ``tick, signal, order, fill, settlement, log, account``. Ids increase strictly (also
  across restarts). ``?replay=N`` first re-sends the last N buffered events (up to 500),
  ``Last-Event-ID`` the buffered events after that id; then an ``account`` greeting (id-less
  after a replay, else carrying the latest event id), then live events, and a
  ``: keepalive`` comment every 15 s. ``?max_events=N`` /
  ``?duration=S`` end the stream early (tests, curl).
* ``frontend/dist`` (if built; checked per request) is served at ``/`` with an
  ``index.html`` fallback for client-side routes; otherwise a small placeholder page
  explains how to build it.
* Backtests: ``POST /api/backtests`` runs ``kalshibot.backtest.runner.run_backtest`` in a
  background daemon thread when that module is installed, else answers 501.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import importlib
import inspect
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from kalshibot import __version__
from kalshibot.analytics import (
    DEFAULT_MAX_DRAWDOWN_PCT,
    DEFAULT_MIN_TRADES,
    compute_analytics,
    with_equity_drawdown,
)
from kalshibot.api.schemas import (
    Account,
    AccountResetRequest,
    AnalyticsResponse,
    BacktestCreateRequest,
    BacktestCreateResponse,
    BacktestDetail,
    BacktestSummary,
    EquityPoint,
    FillOut,
    KillSwitchRequest,
    LogOut,
    MarketRow,
    OrderOut,
    PositionOut,
    RiskResponse,
    SettlementOut,
    SignalOut,
    StatusResponse,
    StrategyOut,
    StrategyPatch,
)
from kalshibot.config import DEFAULT_MIN_TRADES_BY_STRATEGY, Settings, load_settings
from kalshibot.engine import STREAM_EVENT_TYPES, Engine, EventBus, jsonable
from kalshibot.feeds import FeedRegistry, build_feeds
from kalshibot.kalshi.client import KalshiClient
from kalshibot.kalshi.models import series_from_event_ticker
from kalshibot.marketdata import MarketDataService, display_title, market_url
from kalshibot.money import D, f4
from kalshibot.paper.broker import PaperBroker
from kalshibot.paper.models import iso
from kalshibot.risk import RiskManager
from kalshibot.store import Store
from kalshibot.strategies import LOAD_ERRORS
from kalshibot.strategies.base import ParamError, coerce_params

__all__ = ["DIST", "AppServices", "build_services", "create_app"]

log = logging.getLogger(__name__)

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
RANGES = {"1d": timedelta(days=1), "7d": timedelta(days=7), "30d": timedelta(days=30), "all": None}
ORDER_STATUSES = ("open", "all", "filled", "partially_filled", "cancelled", "expired", "rejected")
MAX_BT_TRADES = 5000
MAX_BT_POINTS = 2000
KEEPALIVE_S = 15.0


# --------------------------------------------------------------------------- services


@dataclass
class AppServices:
    """Everything the API needs (built by :func:`build_services` or injected by tests)."""

    settings: Settings
    store: Store
    client: Any
    md: MarketDataService
    broker: PaperBroker
    risk: RiskManager
    engine: Engine
    feeds: FeedRegistry
    bus: EventBus
    owns_resources: bool = True
    backtests: dict[int, asyncio.Task[Any]] = field(default_factory=dict)
    title_miss: dict[str, float] = field(default_factory=dict)
    title_task: asyncio.Task[Any] | None = None
    analytics_cache: tuple[Any, dict[str, Any]] | None = None
    equity_dd_cache: tuple[Any, tuple[float, float]] | None = None

    async def aclose(self) -> None:
        await self.engine.close()
        for t in list(self.backtests.values()):
            t.cancel()
        if self.title_task is not None:
            self.title_task.cancel()
        if self.owns_resources:
            with contextlib.suppress(Exception):
                await self.client.aclose()
            with contextlib.suppress(Exception):
                await self.feeds.aclose()
            with contextlib.suppress(Exception):
                self.store.close()


def build_services(
    settings: Settings,
    *,
    client: Any = None,
    store: Store | None = None,
    strategies: Any = None,
    feeds: FeedRegistry | None = None,
    clock: Callable[[], datetime] | None = None,
) -> AppServices:
    """Wire the production stack (public client, market data, paper broker, risk, engine)."""
    # the single-writer lock: a second `serve` or a CLI `reset` on this database fails fast
    store = store or Store(settings.storage.path, exclusive=True)
    client = client or KalshiClient(settings.kalshi.base_url, max_rps=settings.kalshi.max_rps,
                                    timeout=settings.kalshi.timeout)
    md = MarketDataService(client, settings, clock=clock)
    broker = PaperBroker(md, store, settings=settings, clock=clock)
    risk = RiskManager(settings, store=store, clock=clock)
    # kalshi_settled shares the engine's client, so its requests count against kalshi.max_rps
    feeds = feeds if feeds is not None else build_feeds(settings, kalshi_client=client)
    bus = EventBus()
    engine = Engine(settings, client, md, broker, risk, store, strategies, feeds=feeds, bus=bus, clock=clock)
    return AppServices(settings=settings, store=store, client=client, md=md, broker=broker, risk=risk,
                       engine=engine, feeds=feeds, bus=bus)


def _svc(request: Request) -> AppServices:
    return request.app.state.svc


def _section(settings: Any, name: str) -> Mapping[str, Any]:
    v = getattr(settings, name, None)
    if v is None:
        v = (getattr(settings, "model_extra", None) or {}).get(name)
    if isinstance(v, Mapping):
        return v
    if hasattr(v, "model_dump"):
        return v.model_dump()
    return {}


# --------------------------------------------------------------------------- helpers


def _now(svc: AppServices) -> datetime:
    return svc.broker.clock()


def status_payload(svc: AppServices) -> dict[str, Any]:
    return {
        "mode": "paper",
        "engine": svc.engine.status(),
        "exchange": {"trading_active": svc.md.trading_active, "error": svc.md.exchange_error},
        "server_time": iso(datetime.now(UTC)),
        "version": __version__,
        "marketdata": jsonable(svc.md.status()),
        "feeds": jsonable(svc.feeds.status()),
        "strategy_load_errors": dict(LOAD_ERRORS),
    }


def _title(svc: AppServices, ticker: str) -> str:
    return display_title(svc.md.known_market(ticker))


def _want_titles(svc: AppServices, tickers: Iterable[str]) -> list[str]:
    now = time.monotonic()
    out = []
    for t in dict.fromkeys(tickers):
        if t and svc.md.known_market(t) is None and now - svc.title_miss.get(t, -1e9) > 60:
            out.append(t)
    return out


async def _fetch_titles(svc: AppServices, tickers: list[str]) -> None:
    now = time.monotonic()
    for t in tickers:
        svc.title_miss[t] = now
    try:
        await svc.md.refresh_markets(tickers[:100])
    except Exception as e:
        log.info("title lookup failed: %s", e)


async def _ensure_titles(svc: AppServices, tickers: Iterable[str], *, wait: bool) -> None:
    """Make market snapshots (titles, close times) available: wait briefly, or fetch in the background."""
    missing = _want_titles(svc, tickers)
    if not missing:
        return
    if wait:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(_fetch_titles(svc, missing), 5)
        return
    if svc.title_task is None or svc.title_task.done():
        svc.title_task = asyncio.create_task(_fetch_titles(svc, missing))


def _with_title(svc: AppServices, d: dict[str, Any]) -> dict[str, Any]:
    d["title"] = _title(svc, d.get("ticker", ""))
    return d


def _market_row(svc: AppServices, m: Any) -> dict[str, Any]:
    return {
        "ticker": m.ticker,
        "event_ticker": m.event_ticker,
        "series_ticker": m.series_ticker,
        "title": display_title(m),
        "category": svc.md.category(m),
        "status": m.status,
        "yes_bid": f4(m.yes_bid),
        "yes_ask": f4(m.yes_ask),
        "no_bid": f4(m.no_bid),
        "no_ask": f4(m.no_ask),
        "spread": f4(m.spread),
        "last_price": f4(m.last_price),
        "volume_24h": float(m.volume_24h),
        "volume": float(m.volume),
        "open_interest": float(m.open_interest),
        "close_time": iso(m.close_time),
        "url": market_url(m.series_ticker),
    }


def _fmt_ts(x: Any) -> Any:
    return iso(x) if isinstance(x, datetime) else x


def _bt_summary(r: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"], "strategy": r["strategy"], "params": r.get("params") or {},
        "start": r.get("start"), "end": r.get("end"), "status": r["status"],
        "created_at": _fmt_ts(r.get("created_at")), "metrics": r.get("metrics"),
        "error": r.get("error"), "finished_at": _fmt_ts(r.get("finished_at")),
        "starting_balance": f4(r.get("starting_balance")) if r.get("starting_balance") is not None else None,
    }


# --------------------------------------------------------------------------- backtests


def resolve_backtest_runner() -> tuple[Callable[..., Any] | None, str | None]:
    """``kalshibot.backtest.runner.run_backtest`` if importable, else ``(None, why)``."""
    try:
        mod = importlib.import_module("kalshibot.backtest.runner")
    except ModuleNotFoundError as e:
        return None, f"backtest runner not installed (kalshibot.backtest.runner: {e})"
    except Exception as e:
        return None, f"backtest runner failed to import: {type(e).__name__}: {e}"
    fn = getattr(mod, "run_backtest", None)
    if not callable(fn):
        return None, "kalshibot.backtest.runner has no run_backtest()"
    return fn, None


def _call_kwargs(fn: Callable[..., Any], cand: Mapping[str, Any]) -> dict[str, Any]:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return dict(cand)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return {k: v for k, v in cand.items() if k not in ("strategy_cls",) or "strategy_cls" in sig.parameters}
    return {k: v for k, v in cand.items() if k in sig.parameters}


def _daemon_call(fn: Callable[..., Any], kwargs: Mapping[str, Any]) -> asyncio.Future[Any]:
    """Run ``fn(**kwargs)`` in a daemon thread (never blocks interpreter shutdown)."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Any] = loop.create_future()

    def done(ok: bool, value: Any) -> None:
        if fut.done():
            return
        if ok:
            fut.set_result(value)
        else:
            fut.set_exception(value)

    def target() -> None:
        try:
            res = fn(**kwargs)
        except BaseException as e:  # noqa: BLE001 - forwarded to the awaiting task
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(done, False, e)
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(done, True, res)

    threading.Thread(target=target, name="kalshibot-backtest", daemon=True).start()
    return fut


def _thin(points: list[Any], n: int) -> list[Any]:
    if len(points) <= n:
        return points
    step = len(points) / n
    idx = sorted({min(len(points) - 1, int(i * step)) for i in range(n)} | {len(points) - 1})
    return [points[i] for i in idx]


def normalize_backtest_result(res: Any) -> dict[str, Any]:
    """Runner output -> ``{metrics, equity_curve, trades, by_month}`` (JSON-safe, size-capped)."""
    if hasattr(res, "to_json") and callable(res.to_json):
        res = res.to_json()
    elif dataclasses.is_dataclass(res) and not isinstance(res, type):
        res = dataclasses.asdict(res)
    elif hasattr(res, "model_dump"):
        res = res.model_dump()
    if not isinstance(res, Mapping):
        res = {"metrics": {"result": res}}
    metrics = res.get("metrics")
    if not isinstance(metrics, Mapping):
        metrics = {k: v for k, v in res.items() if k not in ("equity_curve", "trades", "by_month")}
    curve = list(res.get("equity_curve") or [])
    trades = list(res.get("trades") or [])
    by_month = list(res.get("by_month") or [])
    metrics = dict(metrics)
    if len(trades) > MAX_BT_TRADES:
        metrics["trades_total"] = len(trades)
        metrics["trades_truncated"] = True
        trades = trades[-MAX_BT_TRADES:]
    return {"metrics": jsonable(metrics), "equity_curve": jsonable(_thin(curve, MAX_BT_POINTS)),
            "trades": jsonable(trades), "by_month": jsonable(by_month)}


async def _run_backtest_task(svc: AppServices, bt_id: int, fn: Callable[..., Any], kwargs: dict[str, Any]) -> None:
    try:
        if inspect.iscoroutinefunction(fn):
            res = await fn(**kwargs)
        else:
            res = await _daemon_call(fn, kwargs)
            if inspect.isawaitable(res):
                res = await res
        out = normalize_backtest_result(res)
        svc.store.update_backtest(bt_id, status="done", finished_at=datetime.now(UTC), **out)
        svc.engine.log("info", "backtest", f"backtest {bt_id} finished", backtest_id=bt_id)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            svc.store.update_backtest(bt_id, status="failed", error="cancelled (server shutting down)",
                                      finished_at=datetime.now(UTC))
        raise
    except Exception as e:
        log.exception("backtest %s failed", bt_id)
        svc.store.update_backtest(bt_id, status="failed", error=f"{type(e).__name__}: {e}",
                                  finished_at=datetime.now(UTC))
        svc.engine.log("error", "backtest", f"backtest {bt_id} failed: {type(e).__name__}: {e}",
                       backtest_id=bt_id)
    finally:
        svc.backtests.pop(bt_id, None)


def _fail_interrupted_backtests(store: Store) -> None:
    for r in store.list_backtests(limit=None):
        if r["status"] in ("running", "queued"):
            store.update_backtest(r["id"], status="failed", error="interrupted (server restarted)",
                                  finished_at=datetime.now(UTC))


# --------------------------------------------------------------------------- SSE


def sse(event: str, data: Any, event_id: int | None = None) -> str:
    """One SSE message. The ``id:`` line (when given) follows ``data:``; field order inside
    a message does not matter to EventSource."""
    payload = json.dumps(jsonable(data), separators=(",", ":"), allow_nan=False, default=str)
    tail = f"id: {event_id}\n" if event_id is not None else ""
    return f"event: {event}\ndata: {payload}\n{tail}\n"


def _int_header(v: str | None) -> int | None:
    try:
        return int(v) if v is not None and v.strip() else None
    except ValueError:
        return None


# --------------------------------------------------------------------------- app


def create_app(
    settings: Settings | None = None,
    *,
    services: AppServices | None = None,
    autostart: bool | None = None,
    frontend_dist: Path | None = None,
    coinbase_services: Any = None,
    build_coinbase: bool | None = None,
    coinbase_autostart: bool | None = None,
) -> FastAPI:
    """The ASGI app. ``services`` (tests) skips building the production stack.

    Coinbase venue (docs/COINBASE_CONTRACT.md §13): ``coinbase_services`` injects a built
    :class:`kalshibot.coinbase.services.CoinbaseServices`; otherwise it is built in the
    lifespan when ``build_coinbase`` (default: only for the production stack, i.e. when
    ``services`` is not injected). Its engine starts per ``coinbase.engine.autostart``
    unless ``coinbase_autostart`` says otherwise."""
    settings = settings or (services.settings if services is not None else load_settings())
    dist = DIST if frontend_dist is None else frontend_dist

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        svc = services or build_services(settings)
        app.state.svc = svc
        svc.bus.bind(asyncio.get_running_loop())
        with contextlib.suppress(Exception):
            _fail_interrupted_backtests(svc.store)
        app.state.stopping = False
        start = settings.engine.autostart if autostart is None else autostart
        if start:
            await svc.engine.start()
        # Coinbase venue: built and started in isolation - any failure leaves cb=None
        # (503 on /api/coinbase/*) and never touches the Kalshi venue
        want_cb = build_coinbase if build_coinbase is not None else services is None
        await _start_coinbase(app, settings, coinbase_services, build=want_cb, autostart=coinbase_autostart)
        try:
            yield
        finally:
            app.state.stopping = True
            # side by side: a slow Coinbase shutdown (outage, hung job) must never eat the
            # Kalshi venue's share of the stop budget (uvicorn 5 s + docker's 30 s grace)
            results = await asyncio.gather(_stop_coinbase(app), svc.aclose(), return_exceptions=True)
            for r in results:
                if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
                    log.error("shutdown step failed: %s", r, exc_info=r)

    app = FastAPI(title="kalshibot (paper trading)", version=__version__, lifespan=lifespan,
                  description="Kalshi paper-trading bot API. PAPER TRADING ONLY: no real orders.")

    # -- errors -----------------------------------------------------------------------

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        parts = []
        for e in exc.errors():
            loc = ".".join(str(p) for p in e.get("loc", ()) if p not in ("body", "query", "path"))
            parts.append(f"{loc}: {e.get('msg')}" if loc else str(e.get("msg")))
        return JSONResponse({"detail": "; ".join(parts) or "invalid request"}, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled API error")
        return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)

    # -- status / engine --------------------------------------------------------------

    @app.get("/api/status", response_model=StatusResponse)
    async def get_status(request: Request) -> dict[str, Any]:
        return status_payload(_svc(request))

    @app.post("/api/engine/start", response_model=StatusResponse)
    async def engine_start(request: Request) -> dict[str, Any]:
        svc = _svc(request)
        await svc.engine.start()
        return status_payload(svc)

    @app.post("/api/engine/stop", response_model=StatusResponse)
    async def engine_stop(request: Request) -> dict[str, Any]:
        svc = _svc(request)
        await svc.engine.stop()
        return status_payload(svc)

    @app.post("/api/engine/kill-switch", response_model=StatusResponse)
    async def engine_kill_switch(request: Request, body: KillSwitchRequest) -> dict[str, Any]:
        svc = _svc(request)
        cancelled = await svc.engine.set_kill_switch(body.on, body.reason or "manual (dashboard)")
        svc.engine.log("warning" if body.on else "info", "risk", f"kill switch {'ON' if body.on else 'off'} (API)",
                       cancelled_orders=len(cancelled))
        return status_payload(svc)

    # -- account ----------------------------------------------------------------------

    @app.get("/api/account", response_model=Account)
    async def get_account(request: Request) -> dict[str, Any]:
        return _svc(request).broker.account().to_json()

    @app.post("/api/account/reset", response_model=Account)
    async def reset_account(request: Request,
                            body: Annotated[AccountResetRequest | None, Body()] = None) -> dict[str, Any]:
        svc = _svc(request)
        await svc.engine.stop()
        acct = await svc.broker.reset(body.starting_balance if body is not None else None)
        svc.risk.reset()
        svc.engine.reset_strategies()
        svc.analytics_cache = None
        svc.equity_dd_cache = None
        svc.engine.log("warning", "account", f"paper account reset to ${acct.starting_balance} (engine stopped)")
        svc.engine.publish_account()
        return acct.to_json()

    @app.get("/api/equity", response_model=list[EquityPoint])
    async def get_equity(request: Request, range: Literal["1d", "7d", "30d", "all"] = "all"
                         ) -> list[dict[str, Any]]:
        svc = _svc(request)
        span = RANGES[range]
        now = _now(svc)
        rows = svc.store.list_equity(since=now - span if span else None, max_points=1000)
        out = [{"ts": iso(r["ts"]), "equity": f4(r["equity"]), "equity_mid": f4(r["equity_mid"]),
                "cash": f4(r["cash"]), "realized_pnl": f4(r["realized_pnl"]),
                "unrealized_pnl": f4(r["unrealized_pnl"]), "reserved_cash": f4(r["reserved_cash"]),
                "positions_value": f4(r["positions_value"])} for r in rows]
        a = svc.broker.account()
        out.append({"ts": iso(a.ts), "equity": f4(a.equity), "equity_mid": f4(a.equity_mid), "cash": f4(a.cash),
                    "realized_pnl": f4(a.realized_pnl), "unrealized_pnl": f4(a.unrealized_pnl),
                    "reserved_cash": f4(a.reserved_cash), "positions_value": f4(a.positions_liquidation_value),
                    "live": True})
        return out

    # -- portfolio --------------------------------------------------------------------

    @app.get("/api/positions", response_model=list[PositionOut])
    async def get_positions(request: Request) -> list[dict[str, Any]]:
        svc = _svc(request)
        rows = svc.broker.positions_json()
        await _ensure_titles(svc, (r["ticker"] for r in rows), wait=True)
        for r in rows:
            m = svc.md.known_market(r["ticker"])
            r["title"] = display_title(m)
            r["close_time"] = iso(m.close_time) if m is not None else None
            r["url"] = market_url(m.series_ticker if m is not None else series_from_event_ticker(r["event_ticker"]))
        return rows

    @app.get("/api/orders", response_model=list[OrderOut])
    async def get_orders(request: Request, status: str = "open", limit: int = Query(200, ge=1, le=5000)
                         ) -> list[dict[str, Any]]:
        svc = _svc(request)
        if status not in ORDER_STATUSES:
            raise HTTPException(422, f"status must be one of {', '.join(ORDER_STATUSES)}")
        if status == "open":
            orders = sorted(svc.broker.open_orders(), key=lambda o: o.id, reverse=True)[:limit]
        else:
            orders = svc.store.list_orders(status, limit=limit)
        await _ensure_titles(svc, (o.ticker for o in orders), wait=False)
        return [_with_title(svc, o.to_json()) for o in orders]

    @app.post("/api/orders/{order_id}/cancel", response_model=OrderOut)
    async def cancel_order(request: Request, order_id: int) -> dict[str, Any]:
        svc = _svc(request)
        existing = svc.broker.get_order(order_id)
        if existing is None:
            raise HTTPException(404, f"order {order_id} not found")
        if not existing.is_open:
            raise HTTPException(409, f"order {order_id} is {existing.status}, not open")
        try:
            o = await svc.broker.cancel_order(order_id, reason="cancelled via API")
        except KeyError:
            raise HTTPException(404, f"order {order_id} not found") from None
        return _with_title(svc, o.to_json())

    @app.get("/api/fills", response_model=list[FillOut])
    async def get_fills(request: Request, limit: int = Query(200, ge=1, le=5000)) -> list[dict[str, Any]]:
        svc = _svc(request)
        fills = svc.store.list_fills(limit=limit)
        await _ensure_titles(svc, (f.ticker for f in fills), wait=False)
        return [_with_title(svc, f.to_json()) for f in fills]

    @app.get("/api/settlements", response_model=list[SettlementOut])
    async def get_settlements(request: Request, limit: int = Query(200, ge=1, le=5000)) -> list[dict[str, Any]]:
        svc = _svc(request)
        rows = svc.store.list_settlements(limit=limit)
        await _ensure_titles(svc, (s.ticker for s in rows), wait=False)
        return [_with_title(svc, s.to_json()) for s in rows]

    # -- strategies ------------------------------------------------------------------

    @app.get("/api/strategies", response_model=list[StrategyOut])
    async def get_strategies(request: Request) -> list[dict[str, Any]]:
        return _svc(request).engine.strategies_json()

    @app.patch("/api/strategies/{name}", response_model=StrategyOut)
    async def patch_strategy(request: Request, name: str, body: StrategyPatch) -> dict[str, Any]:
        svc = _svc(request)
        if name not in svc.engine.runtimes:
            raise HTTPException(404, f"unknown strategy {name!r}")
        try:
            svc.engine.update_strategy(name, enabled=body.enabled, params=body.params)
        except ParamError as e:
            raise HTTPException(422, str(e)) from None
        return svc.engine.strategy_json(name)

    # -- risk -------------------------------------------------------------------------

    def risk_payload(svc: AppServices) -> dict[str, Any]:
        titles = {t: ev.title for t, ev in svc.md.events.items() if ev.title}
        return svc.risk.to_json(svc.broker.portfolio(), titles=titles)

    @app.get("/api/risk", response_model=RiskResponse)
    async def get_risk(request: Request) -> dict[str, Any]:
        return risk_payload(_svc(request))

    @app.patch("/api/risk", response_model=RiskResponse)
    async def patch_risk(request: Request, body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        svc = _svc(request)
        patch = dict(body)
        ks = patch.pop("kill_switch", None)
        patch.pop("kill_switch_reason", None)
        if patch:
            try:
                svc.risk.update_limits(patch)
            except ValidationError as e:
                msg = "; ".join(f"{'.'.join(str(p) for p in er['loc'])}: {er['msg']}" for er in e.errors())
                raise HTTPException(422, msg) from None
            except ValueError as e:
                raise HTTPException(422, str(e)) from None
            svc.engine.log("info", "risk", f"risk limits updated: {patch}")
        if ks is not None:
            await svc.engine.set_kill_switch(bool(ks), "manual (dashboard)")
        return risk_payload(svc)

    # -- feeds ------------------------------------------------------------------------

    @app.get("/api/signals", response_model=list[SignalOut])
    async def get_signals(request: Request, limit: int = Query(200, ge=1, le=5000),
                          strategy: str | None = None, decision: str | None = None) -> list[dict[str, Any]]:
        svc = _svc(request)
        rows = svc.store.list_signals(limit=limit, strategy=strategy, decision=decision)
        return [Engine.signal_json(r) for r in rows]

    @app.get("/api/logs", response_model=list[LogOut])
    async def get_logs(request: Request, limit: int = Query(200, ge=1, le=5000), level: str | None = None,
                       kind: str | None = None) -> list[dict[str, Any]]:
        svc = _svc(request)
        rows = svc.store.list_logs(limit=limit, level=level, kind=kind)
        return [{"id": r["id"], "ts": iso(r["ts"]), "level": r["level"], "kind": r["kind"],
                 "message": r["message"], "data": r["data"]} for r in rows]

    @app.get("/api/markets", response_model=list[MarketRow])
    async def get_markets(request: Request, search: str = "", category: str = "",
                          sort: Literal["volume_24h", "close_time", "spread"] = "volume_24h",
                          limit: int = Query(100, ge=1, le=2000)) -> list[dict[str, Any]]:
        svc = _svc(request)
        return [_market_row(svc, m) for m in svc.md.search(search=search, category=category, sort=sort,
                                                            limit=limit)]

    # -- analytics --------------------------------------------------------------------

    @app.get("/api/analytics", response_model=AnalyticsResponse)
    async def get_analytics(request: Request) -> dict[str, Any]:
        svc = _svc(request)
        cfg = _section(svc.settings, "analytics")
        min_trades = int(cfg.get("min_settled_trades", DEFAULT_MIN_TRADES))
        max_dd = float(cfg.get("max_drawdown_pct", DEFAULT_MAX_DRAWDOWN_PCT))
        by_strat = cfg.get("min_settled_trades_by_strategy")
        mins = {**DEFAULT_MIN_TRADES_BY_STRATEGY,
                **{str(k): int(v) for k, v in (by_strat.items() if isinstance(by_strat, Mapping) else ())}}
        starting_balance = svc.broker.starting_balance
        # each strategy's drawdown is measured against its own allocation (risk limits)
        names = set(svc.engine.runtimes) | set(getattr(svc.risk, "strategy_limits", {}) or {})
        capital = {n: float(starting_balance) * svc.risk.allocation_pct(n) / 100 for n in sorted(names)}
        # cheap change detection first: the heavy reads + bootstrap run only when the ledger
        # changed, in a worker thread with its own read-only connection (never on the loop)
        n_settled, last_settlement, last_equity = svc.store.ledger_version()
        key = (n_settled, last_settlement, str(starting_balance), min_trades, max_dd,
               tuple(sorted(mins.items())), tuple(sorted(capital.items())))

        def compute() -> dict[str, Any]:
            with svc.store.reader() as r:
                rows = r.list_settlements(limit=None)
            return compute_analytics(rows, starting_balance=starting_balance, min_settled_trades=min_trades,
                                     max_drawdown_pct=max_dd, min_settled_trades_by_strategy=mins,
                                     strategy_capital=capital)

        def equity_dd() -> tuple[float, float]:
            with svc.store.reader() as r:
                return r.equity_drawdown()

        if svc.analytics_cache is None or svc.analytics_cache[0] != key:
            svc.analytics_cache = (key, await run_in_threadpool(compute))
        if svc.equity_dd_cache is None or svc.equity_dd_cache[0] != last_equity:
            svc.equity_dd_cache = (last_equity, await run_in_threadpool(equity_dd))
        dd_usd, dd_pct = svc.equity_dd_cache[1]
        # the broker also tracks the peak/drawdown of every snapshot ever taken (survives
        # the downsampling of old snapshots)
        dd_pct = max(dd_pct, float(svc.broker.account().max_drawdown_pct))
        return with_equity_drawdown(svc.analytics_cache[1], dd_usd, dd_pct, min_settled_trades=min_trades,
                                    max_drawdown_pct=max_dd)

    # -- backtests --------------------------------------------------------------------

    @app.get("/api/backtests", response_model=list[BacktestSummary])
    async def list_backtests(request: Request, limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return [_bt_summary(r) for r in _svc(request).store.list_backtests(limit=limit)]

    @app.post("/api/backtests", response_model=BacktestCreateResponse, status_code=202)
    async def create_backtest(request: Request, body: BacktestCreateRequest) -> dict[str, Any]:
        svc = _svc(request)
        fn, why = resolve_backtest_runner()
        if fn is None:
            raise HTTPException(501, f"Backtests are unavailable: {why}")
        rt = svc.engine.runtimes.get(body.strategy)
        if rt is None:
            raise HTTPException(404, f"unknown strategy {body.strategy!r}")
        if not rt.cls.backtestable:
            raise HTTPException(422, f"strategy {body.strategy!r} is not backtestable")
        try:
            coerce_params(rt.cls.param_schema, body.params or {}, strict=True)
        except ParamError as e:
            raise HTTPException(422, str(e)) from None
        params = rt.cls.resolve_params({**(body.params or {})})
        start_bal = body.starting_balance if body.starting_balance is not None else float(
            svc.settings.account.starting_balance)
        bt_id = svc.store.create_backtest(body.strategy, jsonable(params), start=body.start, end=body.end,
                                          starting_balance=D(str(start_bal)))
        kwargs = _call_kwargs(fn, {
            "strategy": body.strategy, "strategy_cls": rt.cls, "params": params, "start": body.start,
            "end": body.end, "starting_balance": start_bal, "settings": svc.settings})
        svc.backtests[bt_id] = asyncio.create_task(_run_backtest_task(svc, bt_id, fn, kwargs),
                                                   name=f"kalshibot-backtest-{bt_id}")
        svc.engine.log("info", "backtest", f"backtest {bt_id} started: {body.strategy}", backtest_id=bt_id)
        return {"id": bt_id, "status": "running"}

    @app.get("/api/backtests/{bt_id}", response_model=BacktestDetail)
    async def get_backtest(request: Request, bt_id: int) -> dict[str, Any]:
        r = _svc(request).store.get_backtest(bt_id)
        if r is None:
            raise HTTPException(404, f"backtest {bt_id} not found")
        out = _bt_summary(r)
        out.update(error=r.get("error"), equity_curve=r.get("equity_curve") or [], trades=r.get("trades") or [],
                   by_month=r.get("by_month") or [])
        return out

    # -- stream -----------------------------------------------------------------------

    @app.get("/api/stream")
    async def stream(request: Request, max_events: int | None = Query(None, ge=1),
                     duration: float | None = Query(None, gt=0),
                     replay: int | None = Query(None, ge=0)) -> StreamingResponse:
        """SSE. Order on the wire: ``retry``, the replayed backlog (``?replay=N`` = the last N
        buffered events, and/or those after the ``Last-Event-ID`` header; each with its
        ``id:``), the ``account`` greeting, then live events (each with its ``id:``).

        After a replay request the greeting has no ``id:`` (it marks the end of the backlog);
        on a plain connection it carries the id of the latest event, so a client that has
        seen no event yet can still resume from here with ``?replay`` / ``Last-Event-ID``."""
        svc = _svc(request)
        after = _int_header(request.headers.get("last-event-id"))

        async def gen() -> AsyncIterator[str]:
            deadline = time.monotonic() + duration if duration else None
            last_out = time.monotonic()
            sent = 0
            # subscribe and snapshot the replay buffer in the same loop step (no await in
            # between): the backlog and the live queue neither overlap nor leave a gap. Done
            # inside the generator so the finally below always unsubscribes.
            q = svc.bus.subscribe(with_ids=True)
            try:
                backlog: list[tuple[int, str, Any]] = []
                if replay is not None or after is not None:
                    backlog = svc.bus.replay(after=after, last=replay)
                high = backlog[-1][0] if backlog else 0
                greet_id = svc.bus.last_id if replay is None and after is None else None
                yield "retry: 3000\n\n"
                for eid, typ, data in backlog:
                    if max_events is not None and sent >= max_events:
                        return
                    if typ in STREAM_EVENT_TYPES:
                        yield sse(typ, data, eid)
                        sent += 1
                if max_events is not None and sent >= max_events:
                    return
                yield sse("account", svc.broker.account().to_json(), greet_id)
                sent += 1
                while max_events is None or sent < max_events:
                    if getattr(request.app.state, "stopping", False):
                        break  # server shutting down: end the stream so shutdown is not held up
                    timeout = 1.0
                    if deadline is not None:
                        timeout = min(timeout, deadline - time.monotonic())
                        if timeout <= 0:
                            break
                    try:
                        eid, typ, data = await asyncio.wait_for(q.get(), timeout)
                    except TimeoutError:
                        if time.monotonic() - last_out >= KEEPALIVE_S:
                            if await request.is_disconnected():
                                break
                            last_out = time.monotonic()
                            yield ": keepalive\n\n"
                        continue
                    if typ not in STREAM_EVENT_TYPES or eid <= high:
                        continue
                    last_out = time.monotonic()
                    yield sse(typ, data, eid)
                    sent += 1
            finally:
                svc.bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # -- Coinbase venue + overview (before the /api catch-all; contract §13) ------------

    _mount_coinbase(app)

    # -- anything else under /api is a JSON 404 ----------------------------------------

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"], include_in_schema=False)
    async def api_not_found(rest: str) -> JSONResponse:
        return JSONResponse({"detail": f"Not Found: /api/{rest}"}, status_code=404)

    # -- frontend ---------------------------------------------------------------------
    # Checked per request, so `npm run build` while the server runs is picked up without a
    # restart. Real files under dist/ are served as-is (content-hashed /assets/* cached
    # for a year); every other non-/api GET gets index.html (client-side routes, deep
    # links); a missing /assets/* file is a 404, never index.html.

    root = dist.resolve()

    @app.get("/{path:path}", include_in_schema=False, response_model=None)
    async def spa(path: str) -> FileResponse | HTMLResponse:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "Not Found")
        index = dist / "index.html"
        if not index.is_file():
            return HTMLResponse(PLACEHOLDER_HTML, headers={"Cache-Control": "no-cache"})
        if path:
            f = (dist / path).resolve()
            if f.is_file() and root in f.parents:
                immutable = path.startswith("assets/")
                return FileResponse(f, headers={
                    "Cache-Control": "public, max-age=31536000, immutable" if immutable else "no-cache"})
            if path.startswith("assets/"):
                raise HTTPException(404, "Not Found")
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app


# --------------------------------------------------------------------------- Coinbase venue
# Additive integration of the separate Coinbase spot PAPER venue (docs/COINBASE_CONTRACT.md
# §13). Everything is wrapped so an import error, a bad config or a Coinbase outage can only
# make /api/coinbase/* answer 503 - the Kalshi routes, engine and lifespan are unaffected.

COINBASE_BUILD_TIMEOUT_S = 30.0
#: cap on the Coinbase venue's shutdown (it runs next to Kalshi's, never before it)
COINBASE_STOP_TIMEOUT_S = 10.0


async def _start_coinbase(app: FastAPI, settings: Any, injected: Any, *, build: bool,
                          autostart: bool | None) -> None:
    app.state.cb = None
    app.state.cb_error = None
    cb = injected
    try:
        if cb is None:
            if not build:
                app.state.cb_error = "not started (Coinbase venue not built for this app)"
                return
            from kalshibot.coinbase.services import build_coinbase_services

            cb = await asyncio.wait_for(build_coinbase_services(settings), COINBASE_BUILD_TIMEOUT_S)
        cb.bind(asyncio.get_running_loop())
        app.state.cb = cb
    except Exception as e:
        app.state.cb = None
        app.state.cb_error = str(e) or type(e).__name__
        log.warning("Coinbase venue unavailable (Kalshi unaffected): %s", app.state.cb_error)
        return
    try:
        start = cb.autostart if autostart is None else autostart
        if start:
            await cb.engine.start()
    except Exception as e:  # the venue stays browsable; its engine can be started from the UI
        log.exception("Coinbase engine failed to start (Kalshi unaffected)")
        with contextlib.suppress(Exception):
            cb.engine.log("error", "engine", f"engine failed to start: {type(e).__name__}: {e}")


async def _stop_coinbase(app: FastAPI) -> None:
    cb = getattr(app.state, "cb", None)
    if cb is None:
        return
    try:
        await asyncio.wait_for(cb.aclose(), COINBASE_STOP_TIMEOUT_S)
    except Exception:
        log.exception("Coinbase venue shutdown failed (Kalshi shutdown continues)")


def _mount_coinbase(app: FastAPI) -> None:
    """``/api/coinbase/*`` and ``/api/overview``; each falls back to a stub if its module fails
    to import (``/api/coinbase/*`` -> 503 with the import error)."""
    try:
        from kalshibot.coinbase.api import router as coinbase_router

        app.include_router(coinbase_router, prefix="/api/coinbase")
    except Exception as e:
        log.exception("Coinbase API routes unavailable (Kalshi unaffected)")
        why = f"Coinbase API failed to import: {type(e).__name__}: {e}"

        @app.api_route("/api/coinbase/{rest:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
                       include_in_schema=False)
        async def coinbase_unavailable(rest: str) -> JSONResponse:
            return JSONResponse({"detail": f"coinbase venue unavailable: {why}"}, status_code=503)
    try:
        from kalshibot.api.overview import router as overview_router

        app.include_router(overview_router)
    except Exception:
        log.exception("GET /api/overview unavailable")


PLACEHOLDER_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>kalshibot (paper)</title>
<style>
:root{color-scheme:light dark;--bg:#0f1115;--fg:#e6e6e6;--mut:#9aa3ad;--acc:#f5b301}
@media (prefers-color-scheme: light){:root{--bg:#fafafa;--fg:#1b1d21;--mut:#5b6470;--acc:#9a6b00}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0;padding:32px 16px}
main{max-width:640px;margin:0 auto}.badge{display:inline-block;border:1px solid var(--acc);color:var(--acc);
padding:2px 8px;border-radius:4px;font-weight:600;letter-spacing:.05em}code{font-size:13px}
a{color:inherit}p{color:var(--mut)}
</style></head><body><main>
<p class="badge">PAPER TRADING</p>
<h1>kalshibot API is running</h1>
<p>The dashboard has not been built. Build it with <code>cd frontend &amp;&amp; npm install &amp;&amp;
npm run build</code> and reload this page.</p>
<p>Meanwhile: <a href="/api/status">/api/status</a> &middot; <a href="/api/account">/api/account</a> &middot;
<a href="/api/markets?limit=20">/api/markets</a> &middot; <a href="/docs">/docs</a></p>
</main></body></html>
"""
