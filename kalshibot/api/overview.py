"""``GET /api/overview``: both PAPER venues side by side (docs/COINBASE_CONTRACT.md §13).

PAPER TRADING ONLY - read-only. Mounted by ``kalshibot.api.server`` before the
``/api/{rest:path}`` catch-all. Each venue block is built independently: a Coinbase venue
that is disabled, failed to start or raises here is reported as ``available: false`` with
its ``unavailable_reason`` and never affects the Kalshi block (and vice versa).

Response::

    {generated_at,
     venues: {kalshi:   {venue, label: "KALSHI · prediction markets", available, unavailable_reason,
                         engine_running, kill_switch, starting_balance, equity, cash, total_pnl,
                         total_return_pct, todays_pnl, open_positions, fees_paid, last_error,
                         last_error_at, last_tick_at},
              coinbase: {venue, label: "COINBASE · crypto spot", available, unavailable_reason, ...same}},
     combined: {starting_balance, equity, total_pnl, total_return_pct,
                note: "Sum of two separate paper accounts", venues_included: [...]},
     equity_series: {kalshi: [{ts, equity}], coinbase: [{ts, equity}]}}

``?range=1d|7d|30d|all`` (default ``all``) bounds the equity series, each thinned to at most
``?max_points`` (default 500) points plus the live value. Money fields of an unavailable
venue are ``null`` (never a misleading 0), and the combined figures sum the available venues
only.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Query, Request

__all__ = ["COINBASE_LABEL", "COMBINED_NOTE", "KALSHI_LABEL", "overview_payload", "router"]

log = logging.getLogger(__name__)

router = APIRouter()

KALSHI_LABEL = "KALSHI · prediction markets"
COINBASE_LABEL = "COINBASE · crypto spot"
COMBINED_NOTE = "Sum of two separate paper accounts"
RANGES = {"1d": timedelta(days=1), "7d": timedelta(days=7), "30d": timedelta(days=30), "all": None}
MONEY = ("starting_balance", "equity", "cash", "total_pnl", "total_return_pct", "todays_pnl", "open_positions",
         "fees_paid")


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _num(x: Any) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, 8) if math.isfinite(v) else None


def _blank(venue: str, label: str, reason: str) -> dict[str, Any]:
    out: dict[str, Any] = {"venue": venue, "label": label, "available": False, "unavailable_reason": reason,
                           "engine_running": False, "kill_switch": False}
    out.update({k: None for k in MONEY})
    out.update(last_error=None, last_error_at=None, last_tick_at=None)
    return out


def _block(venue: str, label: str, acct: Mapping[str, Any], engine: Mapping[str, Any], kill_switch: bool,
           *, last_tick: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {"venue": venue, "label": label, "available": True, "unavailable_reason": None,
                           "engine_running": bool(engine.get("running")), "kill_switch": bool(kill_switch)}
    for k in MONEY:
        v = acct.get(k)
        out[k] = int(v) if k == "open_positions" and v is not None else _num(v)
    out.update(last_error=engine.get("last_error"), last_error_at=engine.get("last_error_at"),
               last_tick_at=last_tick if last_tick is not None else engine.get("last_tick_at"))
    return out


def _series(store: Any, since: datetime | None, max_points: int, live: tuple[datetime, Any] | None) -> list[dict]:
    rows = store.list_equity(since=since, max_points=max_points)
    out = [{"ts": _iso(r["ts"]) if isinstance(r["ts"], datetime) else r["ts"], "equity": _num(r["equity"])}
           for r in rows]
    if live is not None:
        out.append({"ts": _iso(live[0]), "equity": _num(live[1])})
    return [p for p in out if p["ts"] and p["equity"] is not None]


def kalshi_part(svc: Any, since: datetime | None, max_points: int) -> tuple[dict[str, Any], list[dict]]:
    if svc is None:
        return _blank("kalshi", KALSHI_LABEL, "Kalshi services not started"), []
    try:
        a = svc.broker.account()
        eng = svc.engine.status()
        block = _block("kalshi", KALSHI_LABEL, a.to_json(), eng, bool(svc.risk.kill_switch))
    except Exception as e:
        log.exception("overview: Kalshi block failed")
        return _blank("kalshi", KALSHI_LABEL, f"{type(e).__name__}: {e}"), []
    try:
        series = _series(svc.store, since, max_points, (a.ts, a.equity))
    except Exception:
        log.exception("overview: Kalshi equity series failed")
        series = []
    return block, series


def coinbase_part(cb: Any, error: str | None, since: datetime | None,
                  max_points: int) -> tuple[dict[str, Any], list[dict]]:
    if cb is None:
        blank = _blank("coinbase", COINBASE_LABEL, error or "Coinbase venue not started")
        blank.update(last_bar_at=None, coinbase_reachable=None)
        return blank, []
    try:
        a = cb.broker.account()
        eng = cb.engine.status()
        block = _block("coinbase", COINBASE_LABEL, a.to_json(), eng, bool(cb.risk.kill_switch))
        # last_tick_at = the 60 s marks/snapshot tick; last_bar_at = the last strategy bar close
        block["last_bar_at"] = eng.get("last_bar_at")
        block["coinbase_reachable"] = eng.get("coinbase_reachable")
    except Exception as e:
        log.exception("overview: Coinbase block failed")
        blank = _blank("coinbase", COINBASE_LABEL, f"{type(e).__name__}: {e}")
        blank.update(last_bar_at=None, coinbase_reachable=None)
        return blank, []
    try:
        series = _series(cb.store, since, max_points, (a.ts, a.equity))
    except Exception:
        log.exception("overview: Coinbase equity series failed")
        series = []
    return block, series


def _combined(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    avail = [b for b in blocks if b["available"]]

    def total(k: str) -> float | None:
        vals = [b[k] for b in avail]
        if not vals or any(v is None for v in vals):
            return None
        return round(float(sum(Decimal(str(v)) for v in vals)), 8)

    start, equity, pnl = total("starting_balance"), total("equity"), total("total_pnl")
    return {"starting_balance": start, "equity": equity, "total_pnl": pnl,
            "total_return_pct": round(pnl / start * 100, 8) if pnl is not None and start else None,
            "note": COMBINED_NOTE, "venues_included": [b["venue"] for b in avail]}


def overview_payload(app_state: Any, *, range: str = "all", max_points: int = 500) -> dict[str, Any]:
    now = datetime.now(UTC)
    span = RANGES.get(range)
    since = now - span if span is not None else None
    kb, ks = kalshi_part(getattr(app_state, "svc", None), since, max_points)
    cb, cs = coinbase_part(getattr(app_state, "cb", None), getattr(app_state, "cb_error", None), since, max_points)
    return {"generated_at": _iso(now), "venues": {"kalshi": kb, "coinbase": cb}, "combined": _combined([kb, cb]),
            "equity_series": {"kalshi": ks, "coinbase": cs}}


@router.get("/api/overview")
async def get_overview(request: Request, range: Literal["1d", "7d", "30d", "all"] = "all",
                       max_points: int = Query(500, ge=10, le=5000)) -> dict[str, Any]:
    return overview_payload(request.app.state, range=range, max_points=max_points)
