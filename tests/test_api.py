"""REST/SSE API: every ARCHITECTURE.md §12 endpoint with the contract's field names (fakes, no network)."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest
from conftest import BoomStrategy, DummyStrategy, FakeKalshiClient, standard_market
from fastapi.testclient import TestClient

from kalshibot.api.server import AppServices, build_services, create_app
from kalshibot.config import Settings
from kalshibot.feeds import FeedRegistry
from kalshibot.money import D
from kalshibot.strategies.base import OrderIntent

A, B, C = "KXTEST-26SEP27-A", "KXTEST-26SEP27-B", "KXTEST-26SEP27-C"

STATUS_ENGINE = {"running", "started_at", "last_tick_at", "tick_count", "universe_size", "last_error", "kill_switch"}
ACCOUNT = {"starting_balance", "cash", "reserved_cash", "positions_liquidation_value", "positions_mid_value",
           "equity", "equity_mid", "realized_pnl", "unrealized_pnl", "fees_paid", "total_pnl", "total_return_pct",
           "todays_pnl", "max_drawdown_pct", "open_positions", "open_orders", "settled_trades", "win_rate"}
EQUITY = {"ts", "equity", "equity_mid", "cash", "realized_pnl", "unrealized_pnl"}
POSITION = {"ticker", "title", "event_ticker", "side", "count", "avg_price", "cost_basis", "mark_price",
            "liquidation_value", "unrealized_pnl", "fair_value", "expected_edge_total", "strategy", "opened_at",
            "close_time", "yes_bid", "yes_ask", "url"}
ORDER = {"id", "ticker", "side", "action", "count", "filled_count", "limit_price", "avg_fill_price", "tif", "status",
         "strategy", "reason", "expected_edge", "fair_value", "group_id", "queue_ahead", "created_at", "updated_at",
         "expires_at", "fees", "title"}
FILL = {"id", "order_id", "ticker", "side", "action", "count", "price", "fee", "is_taker", "ts", "strategy", "title"}
SETTLEMENT = {"id", "ticker", "result", "side", "count", "payout", "cost_basis", "pnl", "ts", "strategy", "title"}
STRATEGY = {"name", "description", "enabled", "params", "param_schema", "backtestable", "stats"}
STATS = {"orders", "fills", "open_positions", "settled", "realized_pnl", "unrealized_pnl", "fees", "win_rate",
         "exposure"}
UTIL = {"total_exposure", "total_exposure_pct", "by_event", "by_strategy", "orders_last_minute", "daily_pnl"}
SIGNAL = {"ts", "strategy", "ticker", "title", "side", "count", "limit_price", "fair_value", "expected_edge",
          "reason", "decision", "decision_reason"}
LOG = {"ts", "level", "kind", "message", "data"}
MARKET = {"ticker", "event_ticker", "title", "category", "yes_bid", "yes_ask", "spread", "last_price", "volume_24h",
          "open_interest", "close_time", "url"}
AN_STATS = {"count", "contracts", "total_pnl", "mean_pnl_per_contract", "mean_pnl_per_trade", "ci_low", "ci_high",
            "ci_basis", "expected_edge_total", "realized_pnl", "brier", "win_rate", "max_drawdown",
            "max_drawdown_pct", "readiness"}
BT_SUMMARY = {"id", "strategy", "params", "start", "end", "status", "created_at", "metrics"}
BT_DETAIL = {"id", "strategy", "params", "status", "error", "metrics", "equity_curve", "trades", "by_month"}


def has(d: dict[str, Any], keys: set[str]) -> None:
    missing = keys - set(d)
    assert not missing, f"missing keys: {sorted(missing)}"


async def populate(svc: AppServices, fc: FakeKalshiClient) -> None:
    for t in (A, B, C):
        standard_market(fc, t, volume_24h=10)
    fc.set_event("KXTEST-26SEP27", title="Test event")
    svc.engine.update_strategy("dummy", enabled=True)
    await svc.md.refresh_universe(force=True)
    await svc.engine.tick()  # buys A, B, C (2 each) at 0.45
    fc.update_market(B, status="finalized", result="yes", settlement_value_dollars="1.0000")
    await svc.engine._job_settlement()
    await svc.broker.place_order(OrderIntent(ticker=C, side="yes", count=3, limit_price=D("0.41"), tif="gtc",
                                             strategy="dummy", reason="rest"))
    await svc.engine._job_snapshot()
    svc.engine.log("info", "test", "hello", x=1)


def make(settings: Settings, tmp_path: Path, *, populate_state: bool = True,
         dist: Path | None = None) -> tuple[TestClient, AppServices, FakeKalshiClient]:
    fc = FakeKalshiClient()
    svc = build_services(settings, client=fc, strategies={"dummy": DummyStrategy, "boom": BoomStrategy},
                         feeds=FeedRegistry())
    svc.md.scanner_days_to_close = 0
    if populate_state:
        asyncio.run(populate(svc, fc))
    app = create_app(settings, services=svc, autostart=False, frontend_dist=dist or tmp_path / "nodist")
    return TestClient(app), svc, fc


@pytest.fixture
def api(settings: Settings, tmp_path: Path) -> Any:
    client, svc, fc = make(settings, tmp_path)
    with client:
        yield client, svc, fc


def test_status_and_engine_controls(api: Any) -> None:
    c, svc, _ = api
    r = c.get("/api/status")
    assert r.status_code == 200
    d = r.json()
    assert d["mode"] == "paper" and set(d["exchange"]) >= {"trading_active"}
    has(d["engine"], STATUS_ENGINE)
    assert d["engine"]["running"] is False and d["engine"]["tick_count"] == 1 and d["engine"]["universe_size"] == 2
    assert d["server_time"].endswith("Z")
    r = c.post("/api/engine/start")
    assert r.status_code == 200 and r.json()["engine"]["running"] is True
    r = c.post("/api/engine/stop")
    assert r.status_code == 200 and r.json()["engine"]["running"] is False
    r = c.post("/api/engine/kill-switch", json={"on": True})
    assert r.status_code == 200 and r.json()["engine"]["kill_switch"] is True
    assert r.json()["engine"]["kill_switch_reason"]
    assert c.post("/api/engine/kill-switch", json={"on": False}).json()["engine"]["kill_switch"] is False
    r = c.post("/api/engine/kill-switch", json={})
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)


def test_account_and_equity(api: Any) -> None:
    c, svc, _ = api
    d = c.get("/api/account").json()
    has(d, ACCOUNT)
    assert d["starting_balance"] == 1000 and d["open_positions"] == 2 and d["settled_trades"] == 1
    assert d["open_orders"] == 1 and d["reserved_cash"] > 0 and d["win_rate"] == 1.0
    for rng in ("1d", "7d", "30d", "all"):
        pts = c.get(f"/api/equity?range={rng}").json()
        assert len(pts) >= 2
        for p in pts:
            has(p, EQUITY)
            assert p["ts"].endswith("Z")
    assert c.get("/api/equity?range=2y").status_code == 422


def test_positions(api: Any) -> None:
    c, *_ = api
    rows = c.get("/api/positions").json()
    assert {r["ticker"] for r in rows} == {A, C}
    for r in rows:
        has(r, POSITION)
        assert r["title"] == f"Title of {r['ticker']}" and r["url"] == "https://kalshi.com/markets/kxtest"
        assert r["count"] == 2 and r["avg_price"] == 0.45 and r["mark_price"] == 0.4 and r["close_time"]
        assert r["strategy"] == "dummy" and r["yes_bid"] == 0.4 and r["yes_ask"] == 0.45


def test_orders_and_cancel(api: Any) -> None:
    c, svc, _ = api
    open_ = c.get("/api/orders").json()
    assert len(open_) == 1 and open_[0]["status"] == "open" and open_[0]["tif"] == "gtc"
    has(open_[0], ORDER)
    everything = c.get("/api/orders?status=all&limit=200").json()
    assert len(everything) == 4
    for o in everything:
        has(o, ORDER)
        assert o["title"]
    assert len(c.get("/api/orders?status=filled").json()) == 3
    assert c.get("/api/orders?status=weird").status_code == 422
    oid = open_[0]["id"]
    r = c.post(f"/api/orders/{oid}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    has(r.json(), ORDER)
    r = c.post(f"/api/orders/{oid}/cancel")
    assert r.status_code == 409 and "not open" in r.json()["detail"]
    r = c.post("/api/orders/99999/cancel")
    assert r.status_code == 404 and isinstance(r.json()["detail"], str)
    assert c.get("/api/account").json()["reserved_cash"] == 0


def test_fills_and_settlements(api: Any) -> None:
    c, *_ = api
    fills = c.get("/api/fills?limit=200").json()
    assert len(fills) == 3
    for f in fills:
        has(f, FILL)
        assert f["is_taker"] is True and f["price"] == 0.45
    sts = c.get("/api/settlements").json()
    assert len(sts) == 1
    has(sts[0], SETTLEMENT)
    assert sts[0]["ticker"] == B and sts[0]["result"] == "yes" and sts[0]["payout"] == 2.0
    assert sts[0]["kind"] == "settlement" and sts[0]["title"] == f"Title of {B}"
    assert c.get("/api/fills?limit=0").status_code == 422


def test_strategies_list_and_patch(api: Any) -> None:
    c, svc, _ = api
    rows = c.get("/api/strategies").json()
    assert [r["name"] for r in rows] == ["boom", "dummy"]
    for r in rows:
        has(r, STRATEGY)
        has(r["stats"], STATS)
    dummy = rows[1]
    assert dummy["enabled"] and dummy["backtestable"] and dummy["params"]["max_price"] == 0.6
    assert dummy["param_schema"]["count"]["type"] == "int"
    assert dummy["stats"]["fills"] == 3 and dummy["stats"]["settled"] == 1 and dummy["stats"]["open_positions"] == 2
    r = c.patch("/api/strategies/dummy", json={"enabled": False, "params": {"count": 4}})
    assert r.status_code == 200
    has(r.json(), STRATEGY)
    assert r.json()["enabled"] is False and r.json()["params"]["count"] == 4
    assert svc.store.get_strategy_state("dummy")["params"] == {"count": 4}
    r = c.patch("/api/strategies/dummy", json={"params": {"count": 1000}})
    assert r.status_code == 422 and "maximum" in r.json()["detail"]
    assert c.patch("/api/strategies/dummy", json={"params": {"nope": 1}}).status_code == 422
    assert c.patch("/api/strategies/dummy", json={"bogus": True}).status_code == 422
    assert c.patch("/api/strategies/nope", json={"enabled": True}).status_code == 404


def test_risk_get_and_patch(api: Any) -> None:
    c, svc, _ = api
    d = c.get("/api/risk").json()
    assert set(d) >= {"limits", "utilization", "kill_switch"}
    has(d["utilization"], UTIL)
    assert d["limits"]["max_spread"] == 0.1 and d["utilization"]["total_exposure"] > 0
    assert d["utilization"]["by_event"][0]["title"] == "Test event"
    r = c.patch("/api/risk", json={"max_spread": 0.05, "max_orders_per_minute": 10})
    assert r.status_code == 200
    assert r.json()["limits"]["max_spread"] == 0.05 and r.json()["limits"]["max_orders_per_minute"] == 10
    assert svc.store.get_risk_limits()["max_orders_per_minute"] == 10
    r = c.patch("/api/risk", json={"max_total_exposure_pct": 500})
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)
    assert c.patch("/api/risk", json={"not_a_limit": 1}).status_code == 422
    assert c.patch("/api/risk", json={"kill_switch": True}).json()["kill_switch"] is True


def test_signals_and_logs(api: Any) -> None:
    c, *_ = api
    sig = c.get("/api/signals?limit=200").json()
    assert len(sig) == 3
    for s in sig:
        has(s, SIGNAL)
        assert s["decision"] == "executed" and s["limit_price"] == 0.45 and s["fair_value"] == 0.7
        assert s["expected_edge"] is not None and s["title"].startswith("Title of")
    logs = c.get("/api/logs?limit=50").json()
    assert logs
    for row in logs:
        has(row, LOG)
    hello = [row for row in logs if row["message"] == "hello"]
    assert hello and hello[0]["data"] == {"x": 1} and hello[0]["kind"] == "test"


def test_markets(api: Any) -> None:
    c, svc, fc = api
    rows = c.get("/api/markets").json()
    assert {r["ticker"] for r in rows} == {A, C}  # B finalized -> left the universe
    for r in rows:
        has(r, MARKET)
        assert r["yes_bid"] == 0.4 and r["yes_ask"] == 0.45 and r["spread"] == 0.05
        assert r["category"] == "Testing"  # from the series fetched for fees
        assert r["url"] == "https://kalshi.com/markets/kxtest"
    assert [r["ticker"] for r in c.get("/api/markets?search=-c").json()] == [C]
    assert c.get("/api/markets?category=nope").json() == []
    assert len(c.get("/api/markets?sort=close_time&limit=1").json()) == 1
    assert len(c.get("/api/markets?sort=spread").json()) == 2
    assert c.get("/api/markets?sort=bogus").status_code == 422


def test_analytics(api: Any) -> None:
    c, *_ = api
    d = c.get("/api/analytics").json()
    assert set(d) >= {"overall", "by_strategy", "calibration", "readiness"}
    has(d["overall"], AN_STATS)
    assert d["overall"]["count"] == 1 and d["overall"]["total_pnl"] > 0
    has(d["by_strategy"]["dummy"], AN_STATS)
    assert d["calibration"] and set(d["calibration"][0]) >= {"bucket", "n", "mean_fair_value", "realized_rate"}
    assert d["readiness"]["ready"] is False and d["readiness"]["reasons"]
    assert c.get("/api/analytics").json() == d  # cached / deterministic


def test_backtests_unavailable_is_501(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    c, *_ = api
    monkeypatch.setitem(sys.modules, "kalshibot.backtest.runner", None)
    r = c.post("/api/backtests", json={"strategy": "dummy"})
    assert r.status_code == 501 and "unavailable" in r.json()["detail"]
    assert c.get("/api/backtests").json() == []
    assert c.get("/api/backtests/1").status_code == 404


def test_backtests_run_in_background(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    c, svc, _ = api
    seen: dict[str, Any] = {}

    def run_backtest(strategy: str, params: dict, start: str | None = None, end: str | None = None,
                     starting_balance: float = 1000.0) -> dict[str, Any]:
        seen.update(strategy=strategy, params=params, start=start, end=end, starting_balance=starting_balance)
        time.sleep(0.05)
        return {"metrics": {"total_pnl": D("12.5"), "n_trades": 3},
                "equity_curve": [{"ts": "2026-01-01T00:00:00Z", "equity": 1000}],
                "trades": [{"ticker": "X", "pnl": 1.0}], "by_month": [{"month": "2026-01", "pnl": 12.5}]}

    def run_fail(strategy: str) -> None:
        raise RuntimeError("no data")

    mod = types.ModuleType("kalshibot.backtest.runner")
    mod.run_backtest = run_backtest  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kalshibot.backtest.runner", mod)
    r = c.post("/api/backtests", json={"strategy": "dummy", "params": {"count": 3}, "start": "2026-01-01",
                                       "end": "2026-02-01", "starting_balance": 500})
    assert r.status_code == 202 and r.json()["status"] == "running"
    bt_id = r.json()["id"]
    detail = {}
    for _ in range(100):
        detail = c.get(f"/api/backtests/{bt_id}").json()
        if detail["status"] != "running":
            break
        time.sleep(0.02)
    has(detail, BT_DETAIL)
    assert detail["status"] == "done" and detail["error"] is None
    assert detail["metrics"] == {"total_pnl": 12.5, "n_trades": 3} and detail["trades"][0]["ticker"] == "X"
    assert detail["equity_curve"][0]["equity"] == 1000 and detail["by_month"][0]["month"] == "2026-01"
    assert seen == {"strategy": "dummy", "params": {**DummyStrategy.default_params, "count": 3},
                    "start": "2026-01-01", "end": "2026-02-01", "starting_balance": 500.0}
    rows = c.get("/api/backtests").json()
    has(rows[0], BT_SUMMARY)
    assert rows[0]["start"] == "2026-01-01" and rows[0]["metrics"]["n_trades"] == 3
    # validation
    assert c.post("/api/backtests", json={"strategy": "nope"}).status_code == 404
    assert c.post("/api/backtests", json={"strategy": "boom"}).status_code == 422  # not backtestable
    assert c.post("/api/backtests", json={"strategy": "dummy", "params": {"count": -1}}).status_code == 422
    # a failing runner marks the run failed
    mod.run_backtest = run_fail  # type: ignore[attr-defined]
    bt2 = c.post("/api/backtests", json={"strategy": "dummy"}).json()["id"]
    for _ in range(100):
        d2 = c.get(f"/api/backtests/{bt2}").json()
        if d2["status"] != "running":
            break
        time.sleep(0.02)
    assert d2["status"] == "failed" and "no data" in d2["error"]


def test_account_reset_stops_engine_and_wipes(api: Any) -> None:
    c, svc, _ = api
    c.post("/api/engine/start")
    r = c.post("/api/account/reset", json={"starting_balance": 250})
    assert r.status_code == 200
    d = r.json()
    has(d, ACCOUNT)
    assert d["starting_balance"] == 250 and d["cash"] == 250 and d["open_positions"] == 0
    assert c.get("/api/status").json()["engine"]["running"] is False
    assert c.get("/api/orders?status=all").json() == [] and c.get("/api/signals").json() == []
    assert c.post("/api/account/reset", json={"starting_balance": -5}).status_code == 422
    assert c.post("/api/account/reset").json()["starting_balance"] == 250  # body optional


def test_unknown_api_path_is_json_404(api: Any) -> None:
    c, *_ = api
    for path in ("/api/nope", "/api/", "/api/strategies/x/y"):
        r = c.get(path)
        assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
        assert isinstance(r.json()["detail"], str)
    r = c.get("/some/deep/link")
    assert r.status_code == 200 and "PAPER TRADING" in r.text  # placeholder page (no dist)


def test_stream_sse(api: Any) -> None:
    c, svc, _ = api
    stop = threading.Event()

    def publisher() -> None:
        while not stop.is_set():
            svc.bus.publish("tick", {"tick_count": 42})
            svc.bus.publish("bogus", {"x": 1})  # unknown types are not streamed
            time.sleep(0.02)

    th = threading.Thread(target=publisher, daemon=True)
    th.start()
    try:
        with c.stream("GET", "/api/stream?max_events=3&duration=3") as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            assert r.headers["cache-control"] == "no-cache"
            body = "".join(r.iter_text())
    finally:
        stop.set()
        th.join()
    events = [blk for blk in body.split("\n\n") if blk.startswith("event:")]
    assert len(events) == 3
    first = events[0].split("\n")
    assert first[0] == "event: account"
    has(json.loads(first[1][len("data: "):]), ACCOUNT)
    ticks = [e for e in events[1:] if e.startswith("event: tick")]
    assert ticks and json.loads(ticks[0].split("\n")[1][6:]) == {"tick_count": 42}
    assert "bogus" not in body


def test_spa_served_from_dist(settings: Settings, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>app</title>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (dist / "favicon.svg").write_text("<svg/>")
    client, _, _ = make(settings, tmp_path, populate_state=False, dist=dist)
    with client as c:
        assert "<title>app</title>" in c.get("/").text
        assert "<title>app</title>" in c.get("/backtests/42").text
        assert c.get("/assets/app.js").text == "console.log(1)"
        assert c.get("/favicon.svg").text == "<svg/>"
        r = c.get("/api/definitely-not")
        assert r.status_code == 404 and r.json()["detail"]
        assert c.get("/api/status").json()["mode"] == "paper"


def test_lifespan_autostart_and_shutdown(settings: Settings, tmp_path: Path) -> None:
    fc = FakeKalshiClient()
    svc = build_services(settings, client=fc, strategies={"dummy": DummyStrategy}, feeds=FeedRegistry())
    app = create_app(settings, services=svc, autostart=True, frontend_dist=tmp_path / "nodist")
    with TestClient(app) as c:
        assert c.get("/api/status").json()["engine"]["running"] is True
    assert svc.engine.running is False and fc.closed
