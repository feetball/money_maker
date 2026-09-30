"""``/api/coinbase/*`` REST + SSE routes of the Coinbase spot PAPER venue (contract §13).

PAPER TRADING ONLY: every route reads or changes the *simulated* Coinbase account; nothing
here can place a real order. Mounted by ``kalshibot.api.server`` with
``app.include_router(router, prefix="/api/coinbase")``. The venue's services live in
``app.state.cb`` (a :class:`~kalshibot.coinbase.services.CoinbaseServices`); when it is
``None`` (disabled, bad config, failed to build) every route - including unknown
``/api/coinbase/...`` paths and the stream - answers
``503 {"detail": "coinbase venue unavailable: <app.state.cb_error>"}``.

Every top-level JSON object (and every list row) carries ``"venue": "coinbase"``. Numbers
are floats with up to 8 decimals; timestamps ISO-8601 UTC with ``Z``.

``GET /stream`` is Server-Sent Events like ``/api/stream`` (``retry``, ``?replay=N`` /
``Last-Event-ID`` backlog, an ``account`` greeting, live events with ``id:``, ``: keepalive``
every 15 s, ``?max_events`` / ``?duration`` for curl and tests) over the venue's own bus:
types ``tick, signal, order, fill, log, account, bar``.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import math
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from kalshibot.coinbase.engine import STREAM_EVENT_TYPES, CoinbaseEngine
from kalshibot.coinbase.paper import VENUE, f8, iso, product_url
from kalshibot.engine import jsonable

__all__ = ["router", "status_payload"]

log = logging.getLogger(__name__)



def _require_available(request: Request) -> None:
    """Router dependency: 503 before any body/query validation while the venue is unavailable."""
    cb_services(request)


router = APIRouter(dependencies=[Depends(_require_available)])

RANGES = {"1d": timedelta(days=1), "7d": timedelta(days=7), "30d": timedelta(days=30), "all": None}
ORDER_STATUSES = ("open", "all", "filled", "partially_filled", "cancelled", "expired", "rejected")
KEEPALIVE_S = 15.0
FETCH_TIMEOUT_S = 10.0
ANALYTICS_TTL_S = 30.0
#: readiness gates (paper evidence before anyone should even think about real money)
READY_MIN_TRADES = 30
READY_MIN_DAYS = 30
READY_MAX_DRAWDOWN_PCT = 25.0


# --------------------------------------------------------------------------- request bodies


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KillSwitchBody(_In):
    on: bool
    reason: str | None = None


class ResetBody(_In):
    starting_balance: float | None = Field(default=None, gt=0, le=1e9)


class StrategyPatchBody(_In):
    enabled: bool | None = None
    params: dict[str, Any] | None = None


class BacktestBody(_In):
    strategy: str = Field(min_length=1)
    params: dict[str, Any] | None = None
    start: str | None = None
    end: str | None = None
    starting_balance: float | None = Field(default=None, gt=0, le=1e9)
    fee_tier: str | None = None
    slippage: str | float | None = None


# --------------------------------------------------------------------------- helpers


def cb_services(request: Request) -> Any:
    """``app.state.cb`` or 503 with the reason the venue is unavailable."""
    cb = getattr(request.app.state, "cb", None)
    if cb is None:
        reason = getattr(request.app.state, "cb_error", None) or "not started"
        raise HTTPException(503, f"coinbase venue unavailable: {reason}")
    return cb


def _num(x: Any) -> float | None:
    if x is None:
        return None
    if isinstance(x, Decimal):
        return f8(x) if x.is_finite() else None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _fee_tiers() -> list[dict[str, Any]]:
    with contextlib.suppress(Exception):
        from kalshibot.coinbase.fees import FEE_TIERS

        return [t.as_dict() for t in FEE_TIERS.values()]
    return []


def status_payload(cb: Any) -> dict[str, Any]:
    tier: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        tier = cb.broker.fee_tier_json()
    return {
        "venue": VENUE,
        "mode": "paper",
        "engine": cb.engine.status(),
        "fee_tier": tier,
        "server_time": iso(datetime.now(UTC)),
        # extras
        "fee_tiers": _fee_tiers(),
        "starting_balance": _num(getattr(cb.broker, "starting_balance", None)),
    }


def _alloc_equity(cb: Any) -> dict[str, Decimal]:
    eq = cb.broker.account().equity
    names = {p.strategy for p in cb.broker.positions()} | set(getattr(cb.engine, "runtimes", {}))
    return {n: eq * Decimal(str(cb.risk.allocation_pct(n))) / 100 for n in names}


def _order_json(o: Any) -> dict[str, Any]:
    d = o.to_json()
    d.setdefault("venue", VENUE)
    return d


# --------------------------------------------------------------------------- status / engine


@router.get("/status")
async def get_status(request: Request) -> dict[str, Any]:
    return status_payload(cb_services(request))


@router.post("/engine/start")
async def engine_start(request: Request) -> dict[str, Any]:
    cb = cb_services(request)
    await cb.engine.start()
    return status_payload(cb)


@router.post("/engine/stop")
async def engine_stop(request: Request) -> dict[str, Any]:
    cb = cb_services(request)
    await cb.engine.stop()
    return status_payload(cb)


@router.post("/engine/kill-switch")
async def engine_kill_switch(request: Request, body: KillSwitchBody) -> dict[str, Any]:
    cb = cb_services(request)
    cancelled = await cb.engine.set_kill_switch(body.on, body.reason or "manual (dashboard)")
    cb.engine.log("warning" if body.on else "info", "risk",
                  f"coinbase kill switch {'ON' if body.on else 'off'} (API)", cancelled_orders=len(cancelled))
    return status_payload(cb)


# --------------------------------------------------------------------------- account


@router.get("/account")
async def get_account(request: Request) -> dict[str, Any]:
    return cb_services(request).broker.account().to_json()


@router.post("/account/reset")
async def reset_account(request: Request, body: Annotated[ResetBody | None, Body()] = None) -> dict[str, Any]:
    cb = cb_services(request)
    await cb.engine.stop()
    start = body.starting_balance if body is not None and body.starting_balance is not None else None
    acct = cb.broker.reset(Decimal(str(start)) if start is not None else None)
    cb.risk.reset()
    cb.engine.reset_strategies()
    cb.cache.clear()
    cb.engine.log("warning", "account", f"coinbase paper account reset to ${acct.starting_balance} (engine stopped)")
    cb.engine.publish_account()
    return acct.to_json()


def _equity_row(r: Mapping[str, Any]) -> dict[str, Any]:
    return {"venue": VENUE, "ts": iso(r["ts"]), "equity": _num(r["equity"]), "equity_mid": _num(r.get("equity_mid")),
            "cash": _num(r.get("cash")), "realized_pnl": _num(r.get("realized_pnl")),
            "unrealized_pnl": _num(r.get("unrealized_pnl")), "reserved_cash": _num(r.get("reserved_cash")),
            "positions_value": _num(r.get("positions_value"))}


@router.get("/equity")
async def get_equity(request: Request, range: Literal["1d", "7d", "30d", "all"] = "all") -> list[dict[str, Any]]:
    cb = cb_services(request)
    span = RANGES[range]
    now = cb.broker.clock()
    rows = cb.store.list_equity(since=now - span if span else None, max_points=1000)
    out = [_equity_row(r) for r in rows]
    a = cb.broker.account()
    out.append({"venue": VENUE, "ts": iso(a.ts), "equity": _num(a.equity), "equity_mid": _num(a.equity_mid),
                "cash": _num(a.cash), "realized_pnl": _num(a.realized_pnl), "unrealized_pnl": _num(a.unrealized_pnl),
                "reserved_cash": _num(a.reserved_cash), "positions_value": _num(a.positions_liquidation_value),
                "live": True})
    return out


# --------------------------------------------------------------------------- portfolio


@router.get("/positions")
async def get_positions(request: Request) -> list[dict[str, Any]]:
    cb = cb_services(request)
    return cb.broker.positions_json(alloc_equity=_alloc_equity(cb))


@router.get("/orders")
async def get_orders(request: Request, status: str = "open",
                     limit: int = Query(200, ge=1, le=5000)) -> list[dict[str, Any]]:
    cb = cb_services(request)
    if status not in ORDER_STATUSES:
        raise HTTPException(422, f"status must be one of {', '.join(ORDER_STATUSES)}")
    if status == "open":
        orders = sorted(cb.broker.open_orders(), key=lambda o: o.id, reverse=True)[:limit]
    else:
        orders = cb.store.list_orders(status, limit=limit)
    return [_order_json(o) for o in orders]


@router.post("/orders/{order_id}/cancel")
async def cancel_order(request: Request, order_id: int) -> dict[str, Any]:
    from kalshibot.coinbase.broker import OrderNotFoundError, OrderNotOpenError

    cb = cb_services(request)
    try:
        o = await cb.broker.cancel_order(order_id, reason="cancelled via API")
    except OrderNotFoundError:
        raise HTTPException(404, f"order {order_id} not found") from None
    except OrderNotOpenError as e:
        status = getattr(getattr(e, "order", None), "status", "not open")
        raise HTTPException(409, f"order {order_id} is {status}, not open") from None
    cb.engine.log("info", "order", f"order {order_id} cancelled via API", order_id=order_id)
    return _order_json(o)


@router.get("/fills")
async def get_fills(request: Request, limit: int = Query(200, ge=1, le=5000)) -> list[dict[str, Any]]:
    cb = cb_services(request)
    return [f.to_json() for f in cb.store.list_fills(limit=limit)]


# --------------------------------------------------------------------------- strategies


@router.get("/strategies")
async def get_strategies(request: Request) -> list[dict[str, Any]]:
    return cb_services(request).engine.strategies_json()


@router.patch("/strategies/{name}")
async def patch_strategy(request: Request, name: str, body: StrategyPatchBody) -> dict[str, Any]:
    from kalshibot.coinbase.strategies.base import ParamError

    cb = cb_services(request)
    if name not in cb.engine.runtimes:
        raise HTTPException(404, f"unknown coinbase strategy {name!r}")
    try:
        return cb.engine.update_strategy(name, enabled=body.enabled, params=body.params)
    except ParamError as e:
        raise HTTPException(422, str(e)) from None


# --------------------------------------------------------------------------- risk


def risk_payload(cb: Any) -> dict[str, Any]:
    acct = cb.broker.account()
    out = cb.risk.to_json(cb.broker.portfolio_view(None), acct)
    out.setdefault("venue", VENUE)
    eq = acct.equity
    util = out.get("utilization") or {}
    lim = cb.risk.limits
    # extras for the UI: each row's limit and exposure as % of Coinbase equity
    for row in util.get("by_product") or ():
        row["limit_pct"] = _num(lim.max_position_pct_per_product)
        row["equity_pct"] = _num(Decimal(str(row.get("exposure") or 0)) / eq * 100) if eq > 0 else None
    for row in util.get("by_strategy") or ():
        row["limit_pct"] = _num(row.get("allocation_pct"))
        row["equity_pct"] = _num(Decimal(str(row.get("exposure") or 0)) / eq * 100) if eq > 0 else None
    return out


@router.get("/risk")
async def get_risk(request: Request) -> dict[str, Any]:
    return risk_payload(cb_services(request))


@router.patch("/risk")
async def patch_risk(request: Request, body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    cb = cb_services(request)
    patch = dict(body)
    ks = patch.pop("kill_switch", None)
    patch.pop("kill_switch_reason", None)
    if patch:
        try:
            cb.risk.update_limits(patch)
        except ValidationError as e:
            msg = "; ".join(f"{'.'.join(str(p) for p in er['loc'])}: {er['msg']}" for er in e.errors())
            raise HTTPException(422, msg) from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        cb.engine.log("info", "risk", f"coinbase risk limits updated: {patch}")
    if ks is not None:
        await cb.engine.set_kill_switch(bool(ks), "manual (dashboard)")
    return risk_payload(cb)


# --------------------------------------------------------------------------- feeds


@router.get("/signals")
async def get_signals(request: Request, limit: int = Query(200, ge=1, le=5000), strategy: str | None = None,
                      decision: str | None = None) -> list[dict[str, Any]]:
    cb = cb_services(request)
    return [CoinbaseEngine.signal_json(r) for r in cb.store.list_signals(limit=limit, strategy=strategy,
                                                                          decision=decision)]


@router.get("/logs")
async def get_logs(request: Request, limit: int = Query(200, ge=1, le=5000), level: str | None = None,
                   kind: str | None = None) -> list[dict[str, Any]]:
    cb = cb_services(request)
    return [{"venue": VENUE, "id": r["id"], "ts": iso(r["ts"]), "level": r["level"], "kind": r["kind"],
             "message": r["message"], "data": r["data"]} for r in cb.store.list_logs(limit=limit, level=level,
                                                                                      kind=kind)]


# --------------------------------------------------------------------------- products


def _product_row(md: Any, p: Any) -> dict[str, Any]:
    st = md.stats(p.product_id)
    q = md.quote(p.product_id)
    bid = q[1] if q else None
    ask = q[2] if q else None
    last = st.last if st is not None else None
    opn = st.open if st is not None else None
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    price = last if last is not None else mid
    spread = (ask - bid) / mid * 10_000 if mid is not None and mid > 0 else None  # type: ignore[operator]
    vol = st.volume_24h if st is not None else None
    vol_usd = vol * last if vol is not None and last is not None else None
    change = (last - opn) / opn * 100 if last is not None and opn else None
    return {
        "venue": VENUE, "product_id": p.product_id, "base_currency": p.base_currency,
        "price": _num(price), "bid": _num(bid), "ask": _num(ask), "spread_bps": _num(spread),
        "change_24h_pct": _num(change), "volume_24h_usd": _num(vol_usd), "tradable": bool(p.tradable),
        "url": product_url(p.product_id),
        # extras
        "display_name": p.display_name, "status": p.status, "limit_only": p.limit_only, "post_only": p.post_only,
        "min_market_funds": _num(p.min_market_funds), "base_increment": _num(p.base_increment),
        "quote_increment": _num(p.quote_increment),
        "quote_as_of": iso(q[0]) if q else None,
    }


@router.get("/products")
async def get_products(request: Request, search: str = "", sort: Literal["volume", "spread", "change"] = "volume",
                       limit: int = Query(100, ge=1, le=2000)) -> list[dict[str, Any]]:
    cb = cb_services(request)
    md = cb.md
    if not md.usd_products(tradable_only=False):
        if md.reachable is False:  # known outage: fail fast instead of waiting on retries
            raise HTTPException(502, f"Coinbase public API unreachable, no products cached yet: {md.last_error}")
        try:
            await asyncio.wait_for(md.refresh_products(), FETCH_TIMEOUT_S)
        except Exception as e:
            raise HTTPException(502, f"Coinbase public API unreachable, no products cached yet: "
                                     f"{type(e).__name__}: {e}") from None
    with contextlib.suppress(Exception):
        await asyncio.wait_for(md.refresh_stats(), FETCH_TIMEOUT_S)
    q = search.strip().lower()
    rows = []
    for p in md.usd_products(tradable_only=False).values():
        if p.status == "delisted":
            continue
        if q and q not in p.product_id.lower() and q not in p.base_currency.lower() and \
                q not in (p.display_name or "").lower():
            continue
        rows.append(_product_row(md, p))

    def key(r: dict[str, Any]) -> tuple[Any, ...]:
        if sort == "spread":
            v = r["spread_bps"]
            return (v is None, v if v is not None else 0.0, r["product_id"])
        if sort == "change":
            v = r["change_24h_pct"]
            return (v is None, -(v or 0.0), r["product_id"])
        v = r["volume_24h_usd"]
        return (v is None, -(v or 0.0), r["product_id"])

    rows.sort(key=key)
    rows = rows[:limit]
    # bid/ask come from books already seen; fetch cheap level-1 books for the top rows in
    # the background, so the next refresh shows them (never delays this response)
    md.refresh_quotes_soon([r["product_id"] for r in rows[:20] if r["tradable"]], max_age_s=60.0, max_requests=10)
    return rows


# --------------------------------------------------------------------------- analytics


def _daily_returns(rows: list[Mapping[str, Any]]) -> list[float]:
    by_day: dict[str, float] = {}
    for r in rows:
        ts = r["ts"]
        by_day[ts.date().isoformat() if isinstance(ts, datetime) else str(ts)[:10]] = float(r["equity"])
    vals = [by_day[k] for k in sorted(by_day)]
    return [b / a - 1 for a, b in itertools.pairwise(vals) if a > 0]


def _sharpe(rets: list[float]) -> float | None:
    if len(rets) < 5:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    return round(mean / sd * math.sqrt(365), 4) if sd > 0 else None


async def _btc_benchmark(cb: Any, since: datetime | None) -> float | None:
    """BTC-USD buy-and-hold return (%) from ``since`` to now (cached ~5 min)."""
    if since is None:
        return None
    key = "btc_bench"
    hit = cb.cache.get(key)
    if hit is not None and hit[0] == since and time.monotonic() - hit[1] < 300:
        return hit[2]
    try:
        start = since.replace(minute=0, second=0, microsecond=0)
        bars = await asyncio.wait_for(cb.md.client.get_candles("BTC-USD", 3600, start, start), FETCH_TIMEOUT_S)
        if not bars:
            return None
        p0 = bars[0].open
        with contextlib.suppress(Exception):
            await asyncio.wait_for(cb.md.refresh_stats(), FETCH_TIMEOUT_S)
        st = cb.md.stats("BTC-USD")
        p1 = st.last if st is not None else None
        if p1 is None:
            book = await asyncio.wait_for(cb.md.book("BTC-USD", max_age_s=30), FETCH_TIMEOUT_S)
            p1 = book.mid
        val = round(float((p1 / p0 - 1) * 100), 4) if p0 and p1 else None
    except Exception as e:
        log.info("coinbase analytics: BTC benchmark unavailable: %s", e)
        return None
    cb.cache[key] = (since, time.monotonic(), val)
    return val


def _compute_ledger(cb: Any) -> dict[str, Any]:
    """Heavy reads (worker thread, read-only connection)."""
    with cb.store.reader() as r:
        eq_rows = r.list_equity()
        fills = r.list_fills(limit=None)
        _, dd_pct = r.equity_drawdown()
    notional: dict[str, Decimal] = {}
    for f in fills:
        notional[f.strategy] = notional.get(f.strategy, Decimal(0)) + f.notional
    return {"rets": _daily_returns(eq_rows), "first": eq_rows[0]["ts"] if eq_rows else None,
            "avg_equity": (sum((float(x["equity"]) for x in eq_rows), 0.0) / len(eq_rows)) if eq_rows else None,
            "notional": notional, "dd_pct": dd_pct, "n_fills": len(fills)}


@router.get("/analytics")
async def get_analytics(request: Request) -> dict[str, Any]:
    cb = cb_services(request)
    acct = cb.broker.account()
    hit = cb.cache.get("ledger")
    if hit is None or time.monotonic() - hit[0] > ANALYTICS_TTL_S or hit[1] != acct.fills:
        cb.cache["ledger"] = (time.monotonic(), acct.fills, await run_in_threadpool(_compute_ledger, cb))
    led = cb.cache["ledger"][2]
    start = acct.starting_balance
    total_notional = sum(led["notional"].values(), Decimal(0))
    avg_eq = led["avg_equity"] or float(start) or None
    max_dd = max(float(acct.max_drawdown_pct), float(led["dd_pct"] or 0))
    overall = {
        "trades": acct.trades, "total_pnl": _num(acct.total_pnl), "return_pct": _num(acct.total_return_pct),
        "sharpe": _sharpe(led["rets"]), "max_drawdown_pct": round(max_dd, 4), "fees": _num(acct.fees_paid),
        "turnover": round(float(total_notional) / avg_eq, 4) if avg_eq else None,
        # extras
        "win_rate": acct.win_rate, "realized_pnl": _num(acct.realized_pnl), "unrealized_pnl": _num(acct.unrealized_pnl),
        "fills": acct.fills, "traded_notional": _num(total_notional), "equity": _num(acct.equity),
    }
    by_strategy: dict[str, Any] = {}
    stats = cb.broker.strategy_stats()
    for name in sorted(set(stats) | set(cb.engine.runtimes)):
        st = stats.get(name) or {}
        capital = start * Decimal(str(cb.risk.allocation_pct(name))) / 100
        realized = st.get("realized_pnl", Decimal(0)) or Decimal(0)
        unreal = st.get("unrealized_pnl", Decimal(0)) or Decimal(0)
        total = realized + unreal
        n = led["notional"].get(name, Decimal(0))
        by_strategy[name or "-"] = {
            "trades": int(st.get("trades", 0) or 0), "total_pnl": _num(total),
            "return_pct": _num(total / capital * 100) if capital > 0 else None, "sharpe": None,
            "max_drawdown_pct": None, "fees": _num(st.get("fees", Decimal(0))),
            "turnover": _num(n / capital) if capital > 0 else None,
            "realized_pnl": _num(realized), "unrealized_pnl": _num(unreal), "win_rate": _num(st.get("win_rate")),
            "open_positions": int(st.get("open_positions", 0) or 0), "allocation_pct": _num(cb.risk.allocation_pct(name)),
        }
    since = led["first"]
    btc = await _btc_benchmark(cb, since)
    reasons: list[str] = []
    days = (cb.broker.clock() - since).total_seconds() / 86400 if since is not None else 0.0
    if acct.trades < READY_MIN_TRADES:
        reasons.append(f"only {acct.trades} closed trades (need {READY_MIN_TRADES})")
    if days < READY_MIN_DAYS:
        reasons.append(f"only {days:.1f} days of paper history (need {READY_MIN_DAYS})")
    if acct.total_pnl <= 0:
        reasons.append("total P&L after fees is not positive")
    if max_dd > READY_MAX_DRAWDOWN_PCT:
        reasons.append(f"max drawdown {max_dd:.1f}% exceeds {READY_MAX_DRAWDOWN_PCT:g}%")
    if btc is not None and float(acct.total_return_pct) <= btc:
        reasons.append(f"does not beat BTC buy-and-hold ({btc:+.2f}% over the same period)")
    return {
        "venue": VENUE, "overall": overall, "by_strategy": by_strategy,
        "benchmark": {"btc_buy_hold_return_pct": btc, "since": iso(since) if isinstance(since, datetime) else None},
        "readiness": {"ready": not reasons, "reasons": reasons},
    }


# --------------------------------------------------------------------------- backtests


def _strip_details(metrics: Any) -> Any:
    if isinstance(metrics, Mapping):
        return {k: v for k, v in metrics.items() if k != "details"}
    return metrics


def _bt_summary(r: Mapping[str, Any], *, full_metrics: bool = False) -> dict[str, Any]:
    sb = r.get("starting_balance")
    return {
        "venue": VENUE, "id": r["id"], "strategy": r["strategy"], "params": r.get("params") or {},
        "start": r.get("start"), "end": r.get("end"), "status": r["status"],
        "created_at": iso(r["created_at"]) if isinstance(r.get("created_at"), datetime) else r.get("created_at"),
        "finished_at": iso(r["finished_at"]) if isinstance(r.get("finished_at"), datetime) else r.get("finished_at"),
        "metrics": r.get("metrics") if full_metrics else _strip_details(r.get("metrics")),
        "error": r.get("error"), "fee_tier": r.get("fee_tier"),
        "starting_balance": _num(sb) if sb is not None else None,
    }


def _strategy_cls(cb: Any, name: str) -> Any:
    rt = cb.engine.runtimes.get(name)
    if rt is not None:
        return rt.cls
    with contextlib.suppress(Exception):
        from kalshibot.coinbase.strategies import REGISTRY

        return REGISTRY.get(name)
    return None


def _daemon_call(fn: Callable[..., Any], kwargs: Mapping[str, Any]) -> asyncio.Future[Any]:
    """Run ``fn(**kwargs)`` in a daemon thread (never blocks the loop or interpreter shutdown)."""
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

    threading.Thread(target=target, name="coinbase-backtest", daemon=True).start()
    return fut


def _store_result(res: Mapping[str, Any]) -> dict[str, Any]:
    """``run_spot_backtest`` output -> ``SpotStore.update_backtest`` fields. The decision
    samples, universe and granularity ride along in the ``benchmarks`` JSON column."""
    bm = dict(res.get("benchmarks") or {})
    bm["_signals"] = list(res.get("signals") or [])
    bm["_granularity_s"] = res.get("granularity_s")
    return {"metrics": jsonable(res.get("metrics") or {}), "equity_curve": jsonable(res.get("equity_curve") or []),
            "benchmarks": jsonable(bm), "trades": jsonable(res.get("trades") or []),
            "by_year": jsonable(res.get("by_year") or []), "by_month": jsonable(res.get("by_month") or [])}


async def _run_backtest(cb: Any, bt_id: int, kwargs: dict[str, Any]) -> None:
    try:
        from kalshibot.coinbase.backtest import run_spot_backtest

        res = await _daemon_call(run_spot_backtest, kwargs)
        cb.store.update_backtest(bt_id, status="done", finished_at=datetime.now(UTC), **_store_result(res))
        cb.engine.log("info", "backtest", f"coinbase backtest {bt_id} finished", backtest_id=bt_id)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            cb.store.update_backtest(bt_id, status="failed", error="cancelled (server shutting down)",
                                     finished_at=datetime.now(UTC))
        raise
    except Exception as e:
        log.info("coinbase backtest %s failed: %s", bt_id, e)
        with contextlib.suppress(Exception):
            cb.store.update_backtest(bt_id, status="failed", error=f"{type(e).__name__}: {e}",
                                     finished_at=datetime.now(UTC))
        cb.engine.log("error", "backtest", f"coinbase backtest {bt_id} failed: {type(e).__name__}: {e}",
                      backtest_id=bt_id)
    finally:
        cb.backtests.pop(bt_id, None)


@router.get("/backtests")
async def list_backtests(request: Request, limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
    cb = cb_services(request)
    return [_bt_summary(r) for r in cb.store.list_backtests(limit=limit)]


@router.post("/backtests", status_code=202)
async def create_backtest(request: Request, body: BacktestBody) -> dict[str, Any]:
    from kalshibot.coinbase.fees import get_tier
    from kalshibot.coinbase.strategies.base import ParamError

    cb = cb_services(request)
    cls = _strategy_cls(cb, body.strategy)
    if cls is None:
        raise HTTPException(404, f"unknown coinbase strategy {body.strategy!r}")
    if not getattr(cls, "backtestable", True):
        raise HTTPException(422, f"coinbase strategy {body.strategy!r} is not backtestable")
    try:
        params = cls.resolve_params(body.params or {}, strict=True)
    except ParamError as e:
        raise HTTPException(422, str(e)) from None
    tier = None
    if body.fee_tier:
        try:
            tier = get_tier(body.fee_tier)
        except KeyError as e:
            raise HTTPException(422, str(e.args[0] if e.args else e)) from None
    else:
        with contextlib.suppress(Exception):
            tier = cb.broker.tier
    start_bal = body.starting_balance if body.starting_balance is not None else float(cb.broker.starting_balance)
    bt_id = cb.store.create_backtest(body.strategy, jsonable(params), start=body.start, end=body.end,
                                     starting_balance=Decimal(str(start_bal)),
                                     fee_tier=tier.name if tier is not None else None)
    kwargs: dict[str, Any] = {"strategy_cls": cls, "params": params, "start": body.start, "end": body.end,
                              "starting_balance": start_bal, "fee_tier": tier, "settings": cb.settings}
    if body.slippage is not None:
        kwargs["slippage"] = body.slippage
    cb.backtests[bt_id] = asyncio.create_task(_run_backtest(cb, bt_id, kwargs), name=f"coinbase-backtest-{bt_id}")
    cb.engine.log("info", "backtest", f"coinbase backtest {bt_id} started: {body.strategy}", backtest_id=bt_id)
    return {"venue": VENUE, "id": bt_id, "status": "running"}


@router.get("/backtests/{bt_id}")
async def get_backtest(request: Request, bt_id: int) -> dict[str, Any]:
    cb = cb_services(request)
    r = cb.store.get_backtest(bt_id)
    if r is None:
        raise HTTPException(404, f"backtest {bt_id} not found")
    out = _bt_summary(r, full_metrics=True)
    bm = dict(r.get("benchmarks") or {})
    signals = bm.pop("_signals", None) or []
    gran = bm.pop("_granularity_s", None)
    metrics = r.get("metrics") if isinstance(r.get("metrics"), Mapping) else {}
    details = metrics.get("details") if isinstance(metrics.get("details"), Mapping) else {}
    bench_metrics = metrics.get("benchmarks") if isinstance(metrics.get("benchmarks"), Mapping) else {}
    out.update(
        equity_curve=r.get("equity_curve") or [],
        benchmarks={"btc": bm.get("btc") or [], "equal_weight": bm.get("equal_weight") or []},
        benchmark_metrics={"btc": bench_metrics.get("btc"), "equal_weight": bench_metrics.get("equal_weight")},
        trades=r.get("trades") or [], by_year=r.get("by_year") or [], by_month=r.get("by_month") or [],
        signals=[{"venue": VENUE, "id": None, "strategy": r["strategy"], "order_id": None, **s}
                 for s in signals if isinstance(s, Mapping)],
        universe=list(details.get("universe") or []), granularity_s=gran or details.get("granularity_s"),
    )
    return out


# --------------------------------------------------------------------------- stream


def sse(event: str, data: Any, event_id: int | None = None) -> str:
    payload = json.dumps(jsonable(data), separators=(",", ":"), allow_nan=False, default=str)
    tail = f"id: {event_id}\n" if event_id is not None else ""
    return f"event: {event}\ndata: {payload}\n{tail}\n"


def _int_header(v: str | None) -> int | None:
    try:
        return int(v) if v is not None and v.strip() else None
    except ValueError:
        return None


@router.get("/stream")
async def stream(request: Request, max_events: int | None = Query(None, ge=1),
                 duration: float | None = Query(None, gt=0),
                 replay: int | None = Query(None, ge=0)) -> StreamingResponse:
    """SSE like ``/api/stream`` over the Coinbase venue's own bus (see the module doc)."""
    cb = cb_services(request)
    bus = cb.bus
    after = _int_header(request.headers.get("last-event-id"))

    async def gen() -> AsyncIterator[str]:
        deadline = time.monotonic() + duration if duration else None
        last_out = time.monotonic()
        sent = 0
        q = bus.subscribe(with_ids=True)  # subscribe + snapshot the backlog in one loop step
        try:
            backlog: list[tuple[int, str, Any]] = []
            if replay is not None or after is not None:
                backlog = bus.replay(after=after, last=replay)
            high = backlog[-1][0] if backlog else 0
            greet_id = bus.last_id if replay is None and after is None else None
            yield "retry: 3000\n\n"
            for eid, typ, data in backlog:
                if max_events is not None and sent >= max_events:
                    return
                if typ in STREAM_EVENT_TYPES:
                    yield sse(typ, data, eid)
                    sent += 1
            if max_events is not None and sent >= max_events:
                return
            yield sse("account", cb.broker.account().to_json(), greet_id)
            sent += 1
            while max_events is None or sent < max_events:
                if getattr(request.app.state, "stopping", False) or cb.closed:
                    break
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
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------- unknown paths


@router.api_route("/{rest:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"], include_in_schema=False)
async def coinbase_not_found(request: Request, rest: str) -> JSONResponse:
    cb_services(request)  # 503 while the venue is unavailable
    return JSONResponse({"detail": f"Not Found: /api/coinbase/{rest}"}, status_code=404)
