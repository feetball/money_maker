"""``/api/coinbase/*`` (contract §13): every endpoint with the contract's field names, errors,
SSE with replay, 503 while the venue is unavailable. Fakes only - no network. PAPER ONLY."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeKalshiClient
from fastapi.testclient import TestClient
from test_cb_engine import (
    NOW,
    PID,
    PID2,
    BoomStrategy,
    HoldStrategy,
    full_settings,
    make_services,
)

from kalshibot.api.server import AppServices, build_services, create_app
from kalshibot.coinbase.paper import SpotOrderIntent
from kalshibot.coinbase.services import CoinbaseServices
from kalshibot.feeds import FeedRegistry

STATUS_ENGINE = {"running", "started_at", "last_tick_at", "last_bar_at", "tick_count", "products_loaded",
                 "last_error", "last_error_at", "kill_switch", "kill_switch_reason", "coinbase_reachable",
                 "strategies_enabled"}
ACCOUNT = {"venue", "starting_balance", "cash", "reserved_cash", "positions_liquidation_value",
           "positions_mid_value", "equity", "equity_mid", "realized_pnl", "unrealized_pnl", "fees_paid", "total_pnl",
           "total_return_pct", "todays_pnl", "max_drawdown_pct", "open_positions", "open_orders", "trades",
           "win_rate"}
EQUITY = {"ts", "equity", "equity_mid", "cash", "realized_pnl", "unrealized_pnl"}
POSITION = {"venue", "product_id", "base_currency", "strategy", "quantity", "avg_cost", "cost_basis", "mark_price",
            "best_bid", "liquidation_value", "mid_value", "unrealized_pnl", "unrealized_pnl_pct", "realized_pnl",
            "fees_paid", "weight_of_strategy", "opened_at", "url"}
ORDER = {"venue", "id", "product_id", "side", "order_type", "tif", "post_only", "quote_size", "base_size",
         "limit_price", "filled_base", "filled_quote", "avg_fill_price", "fees", "status", "strategy", "reason",
         "created_at", "updated_at", "expires_at"}
FILL = {"venue", "id", "order_id", "product_id", "side", "base_size", "price", "notional", "fee", "fee_rate",
        "is_taker", "ts", "strategy"}
STRATEGY = {"venue", "name", "description", "experimental", "enabled", "enabled_source", "params", "param_schema",
            "bar_granularity_s", "universe", "backtestable", "stats"}
STRAT_STATS = {"orders", "fills", "open_positions", "realized_pnl", "unrealized_pnl", "fees", "exposure",
               "allocation_pct", "last_bar_at", "last_error"}
RISK_LIMITS = {"max_position_pct_per_product", "max_total_exposure_pct", "max_strategy_allocation_pct",
               "min_cash_reserve", "max_orders_per_minute", "daily_loss_limit", "max_spread_bps", "min_trade_usd"}
UTIL = {"total_exposure", "total_exposure_pct", "by_product", "by_strategy", "orders_last_minute", "daily_pnl"}
SIGNAL = {"venue", "id", "ts", "strategy", "product_id", "side", "target_weight", "quote_size", "base_size",
          "limit_price", "expected_edge_bps", "reason", "decision", "decision_reason", "order_id"}
LOG = {"venue", "id", "ts", "level", "kind", "message", "data"}
PRODUCT = {"venue", "product_id", "base_currency", "price", "bid", "ask", "spread_bps", "change_24h_pct",
           "volume_24h_usd", "tradable", "url"}
AN_OVERALL = {"trades", "total_pnl", "return_pct", "sharpe", "max_drawdown_pct", "fees", "turnover"}


def has(d: dict[str, Any], keys: set[str]) -> None:
    missing = keys - set(d)
    assert not missing, f"missing keys: {sorted(missing)}"


def kalshi_services(tmp_path: Path, settings: Any) -> AppServices:
    return build_services(settings, client=FakeKalshiClient(), strategies={}, feeds=FeedRegistry())


def make_app(tmp_path: Path, cb: CoinbaseServices | None = None, **kw: Any) -> tuple[TestClient, AppServices]:
    settings = kw.pop("settings", None) or full_settings(tmp_path)
    svc = kalshi_services(tmp_path, settings)
    kw.setdefault("autostart", False)
    kw.setdefault("coinbase_autostart", False)
    app = create_app(settings, services=svc, frontend_dist=tmp_path / "nodist", coinbase_services=cb, **kw)
    return TestClient(app), svc


async def populated(tmp_path: Path) -> tuple[CoinbaseServices, Any]:
    """A bar executed (a TST-USD position), one resting buy, one rejected intent, a boom error."""
    fc_cb, fc = await make_services(tmp_path, strategies=[HoldStrategy, BoomStrategy])
    fc.add_product(PID2, bid="9.99", ask="10.00", last="10", open_="12", volume=50)
    await fc_cb.md.refresh_products()
    await fc_cb.engine.run_due_bars(now=NOW)
    await fc_cb.broker.place_order(SpotOrderIntent(PID, "buy", quote_size=Decimal(20), order_type="limit",
                                                   limit_price=Decimal(90), tif="gtc", strategy="t_hold",
                                                   reason="rest"))
    await fc_cb.broker.mark()
    fc_cb.broker.equity_snapshot()
    return fc_cb, fc


@pytest.fixture
def api(tmp_path: Path) -> Any:
    cb, fc = asyncio.run(populated(tmp_path))
    client, svc = make_app(tmp_path, cb)
    with client:
        yield client, cb, fc, svc
    asyncio.run(cb.aclose())


# --------------------------------------------------------------------------- status / engine


def test_status_and_engine_controls(api: Any) -> None:
    c, cb, _, _ = api
    d = c.get("/api/coinbase/status").json()
    assert d["venue"] == "coinbase" and d["mode"] == "paper" and d["server_time"].endswith("Z")
    has(d["engine"], STATUS_ENGINE)
    assert d["engine"]["running"] is False and d["engine"]["products_loaded"] == 2
    assert d["fee_tier"] == {"name": "intro", "label": "Intro (US)", "maker_rate": 0.005, "taker_rate": 0.009}
    assert any(t["name"] == "intro_pre_2026_09" for t in d["fee_tiers"])
    r = c.post("/api/coinbase/engine/start")
    assert r.status_code == 200 and r.json()["engine"]["running"] is True
    r = c.post("/api/coinbase/engine/stop")
    assert r.status_code == 200 and r.json()["engine"]["running"] is False
    r = c.post("/api/coinbase/engine/kill-switch", json={"on": True})
    assert r.json()["engine"]["kill_switch"] is True and r.json()["engine"]["kill_switch_reason"]
    assert cb.broker.open_orders() == []  # the resting buy was cancelled
    assert c.post("/api/coinbase/engine/kill-switch", json={"on": False}).json()["engine"]["kill_switch"] is False
    r = c.post("/api/coinbase/engine/kill-switch", json={})
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)


# --------------------------------------------------------------------------- account


def test_account_equity_and_reset(api: Any) -> None:
    c, _cb, _, _ = api
    d = c.get("/api/coinbase/account").json()
    has(d, ACCOUNT)
    assert d["venue"] == "coinbase" and d["starting_balance"] == 1000 and d["open_positions"] == 1
    assert d["open_orders"] == 1 and d["reserved_cash"] > 0 and d["fees_paid"] > 0
    rows = c.get("/api/coinbase/equity?range=1d").json()
    assert len(rows) >= 2 and rows[-1]["live"] is True
    for r in rows:
        has(r, EQUITY)
        assert r["venue"] == "coinbase"
    assert c.get("/api/coinbase/equity?range=2d").status_code == 422
    r = c.post("/api/coinbase/account/reset", json={"starting_balance": 2500})
    assert r.status_code == 200
    d = r.json()
    assert d["starting_balance"] == 2500 and d["equity"] == 2500 and d["open_positions"] == 0
    assert c.get("/api/coinbase/orders").json() == [] and c.get("/api/coinbase/signals").json() == []
    assert c.post("/api/coinbase/account/reset", json={"starting_balance": -1}).status_code == 422
    assert c.post("/api/coinbase/account/reset").json()["starting_balance"] == 2500


# --------------------------------------------------------------------------- portfolio


def test_positions_orders_fills_cancel(api: Any) -> None:
    c, _cb, _, _ = api
    pos = c.get("/api/coinbase/positions").json()
    assert len(pos) == 1
    has(pos[0], POSITION)
    p = pos[0]
    assert p["product_id"] == PID and p["base_currency"] == "TST" and p["strategy"] == "t_hold"
    assert p["url"] == "https://www.coinbase.com/advanced-trade/spot/TST-USD"
    assert 0.9 < p["weight_of_strategy"] <= 1.01  # the whole 50 % allocation
    opens = c.get("/api/coinbase/orders").json()
    assert len(opens) == 1
    has(opens[0], ORDER)
    assert opens[0]["status"] == "open" and opens[0]["tif"] == "gtc"
    allo = c.get("/api/coinbase/orders?status=all&limit=10").json()
    assert len(allo) == 2 and {o["status"] for o in allo} == {"open", "filled"}
    assert c.get("/api/coinbase/orders?status=bogus").status_code == 422
    fills = c.get("/api/coinbase/fills?limit=5").json()
    assert len(fills) >= 1
    has(fills[0], FILL)
    assert fills[0]["is_taker"] is True and fills[0]["fee_rate"] == 0.009
    oid = opens[0]["id"]
    r = c.post(f"/api/coinbase/orders/{oid}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert c.post(f"/api/coinbase/orders/{oid}/cancel").status_code == 409
    assert c.post("/api/coinbase/orders/99999/cancel").status_code == 404


# --------------------------------------------------------------------------- strategies / risk


def test_strategies_and_patch(api: Any) -> None:
    c, _cb, _, _ = api
    rows = c.get("/api/coinbase/strategies").json()
    assert [r["name"] for r in rows] == ["t_boom", "t_hold"]
    for r in rows:
        has(r, STRATEGY)
        has(r["stats"], STRAT_STATS)
    hold = rows[1]
    assert hold["universe"] == [PID] and hold["bar_granularity_s"] == 3600 and hold["enabled"] is True
    assert hold["stats"]["fills"] >= 1 and hold["stats"]["allocation_pct"] == 50
    assert hold["param_schema"]["weight"]["default"] == 1.0
    assert "strategy bug" in rows[0]["stats"]["last_error"]
    r = c.patch("/api/coinbase/strategies/t_hold", json={"enabled": False, "params": {"weight": 0.3}})
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["params"]["weight"] == 0.3
    assert r.json()["enabled_source"] == "dashboard"
    assert c.patch("/api/coinbase/strategies/t_hold", json={"params": {"weight": 7}}).status_code == 422
    assert c.patch("/api/coinbase/strategies/nope", json={"enabled": True}).status_code == 404
    assert c.patch("/api/coinbase/strategies/t_hold", json={"bogus": 1}).status_code == 422


def test_risk_get_and_patch(api: Any) -> None:
    c, cb, _, _ = api
    d = c.get("/api/coinbase/risk").json()
    assert d["venue"] == "coinbase" and d["kill_switch"] is False
    has(d["limits"], RISK_LIMITS)
    has(d["utilization"], UTIL)
    by_p = d["utilization"]["by_product"]
    assert by_p and by_p[0]["product_id"] == PID and by_p[0]["limit_pct"] == 50 and by_p[0]["equity_pct"] > 0
    r = c.patch("/api/coinbase/risk", json={"max_spread_bps": 25, "min_trade_usd": 5})
    assert r.status_code == 200 and r.json()["limits"]["max_spread_bps"] == 25
    assert cb.risk.limits.max_spread_bps == 25
    assert c.patch("/api/coinbase/risk", json={"max_total_exposure_pct": 500}).status_code == 422
    assert c.patch("/api/coinbase/risk", json={"nope": 1}).status_code == 422
    r = c.patch("/api/coinbase/risk", json={"kill_switch": True})
    assert r.json()["kill_switch"] is True and c.get("/api/coinbase/status").json()["engine"]["kill_switch"] is True


# --------------------------------------------------------------------------- feeds / products / analytics


def test_signals_logs(api: Any) -> None:
    c, *_ = api
    sig = c.get("/api/coinbase/signals?limit=10").json()
    assert sig and sig[0]["decision"] == "executed"
    for s in sig:
        has(s, SIGNAL)
    assert sig[0]["target_weight"] == 1.0 and sig[0]["expected_edge_bps"] == 12.0 and sig[0]["reason"].startswith("hold test")
    assert c.get("/api/coinbase/signals?decision=rejected").json() == []
    logs = c.get("/api/coinbase/logs?limit=50").json()
    assert logs
    for r in logs:
        has(r, LOG)
        assert r["venue"] == "coinbase"
    assert any("strategy bug" in r["message"] for r in c.get("/api/coinbase/logs?kind=strategy").json())


def test_products(api: Any) -> None:
    c, _cb, _fc, _ = api
    rows = c.get("/api/coinbase/products").json()
    assert [r["product_id"] for r in rows] == [PID, PID2]  # by 24 h USD volume
    for r in rows:
        has(r, PRODUCT)
    tst = rows[0]
    assert tst["price"] == 100 and tst["change_24h_pct"] == pytest.approx(100 / 95 * 100 - 100)
    assert tst["volume_24h_usd"] == 100_000 and tst["bid"] == 99.99 and tst["ask"] == 100.0
    assert tst["spread_bps"] == pytest.approx(1.0, abs=0.01)
    assert tst["tradable"] is True and tst["url"].endswith("/TST-USD")
    assert [r["product_id"] for r in c.get("/api/coinbase/products?sort=change").json()] == [PID, PID2]
    assert [r["product_id"] for r in c.get("/api/coinbase/products?search=alt").json()] == [PID2]
    assert len(c.get("/api/coinbase/products?limit=1").json()) == 1
    assert c.get("/api/coinbase/products?sort=nope").status_code == 422


def test_analytics(api: Any) -> None:
    c, *_ = api
    d = c.get("/api/coinbase/analytics").json()
    assert d["venue"] == "coinbase"
    has(d["overall"], AN_OVERALL)
    assert d["overall"]["fees"] > 0 and d["overall"]["turnover"] > 0
    assert set(d["by_strategy"]) >= {"t_hold"} and d["by_strategy"]["t_hold"]["fees"] > 0
    assert set(d["benchmark"]) == {"btc_buy_hold_return_pct", "since"}
    assert d["readiness"]["ready"] is False and d["readiness"]["reasons"]


# --------------------------------------------------------------------------- backtests


def test_backtests(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    c, _cb, _, _ = api
    calls: list[dict[str, Any]] = []

    def fake_run(**kw: Any) -> dict[str, Any]:
        calls.append(kw)
        return {"venue": "coinbase", "metrics": {"total_return_pct": 12.5, "details": {"universe": [PID],
                                                                                        "granularity_s": 3600}},
                "equity_curve": [{"ts": "2026-01-01T00:00:00Z", "equity": 1000.0}],
                "benchmarks": {"btc": [{"ts": "2026-01-01T00:00:00Z", "equity": 1000.0}], "equal_weight": []},
                "trades": [{"ts": "2026-01-01T00:00:00Z", "product_id": PID, "side": "buy"}],
                "by_year": [{"year": 2026, "return_pct": 12.5}], "by_month": [{"month": "2026-01"}],
                "signals": [{"ts": "2026-01-01T00:00:00Z", "product_id": PID, "decision": "executed"}],
                "granularity_s": 3600}

    mod = types.ModuleType("kalshibot.coinbase.backtest")
    mod.run_spot_backtest = fake_run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kalshibot.coinbase.backtest", mod)
    assert c.post("/api/coinbase/backtests", json={"strategy": "nope"}).status_code == 404
    assert c.post("/api/coinbase/backtests", json={"strategy": "t_hold", "params": {"x": 1}}).status_code == 422
    assert c.post("/api/coinbase/backtests", json={"strategy": "t_hold", "fee_tier": "gold"}).status_code == 422
    r = c.post("/api/coinbase/backtests", json={"strategy": "t_hold", "params": {"weight": 0.5},
                                                "fee_tier": "intro_pre_2026_09", "starting_balance": 500})
    assert r.status_code == 202 and r.json()["venue"] == "coinbase"
    bt = r.json()["id"]
    for _ in range(100):
        d = c.get(f"/api/coinbase/backtests/{bt}").json()
        if d["status"] != "running":
            break
        asyncio.run(asyncio.sleep(0.02))
    assert d["status"] == "done", d
    assert calls[0]["fee_tier"].name == "intro_pre_2026_09" and calls[0]["params"]["weight"] == 0.5
    assert calls[0]["strategy_cls"] is HoldStrategy and calls[0]["starting_balance"] == 500
    assert d["metrics"]["total_return_pct"] == 12.5 and d["benchmarks"]["btc"] and d["benchmarks"]["equal_weight"] == []
    assert d["signals"][0]["decision"] == "executed" and d["universe"] == [PID] and d["granularity_s"] == 3600
    assert d["by_year"] and d["trades"] and d["fee_tier"] == "intro_pre_2026_09" and d["starting_balance"] == 500
    lst = c.get("/api/coinbase/backtests").json()
    assert lst[0]["id"] == bt and "details" not in (lst[0]["metrics"] or {})
    assert c.get("/api/coinbase/backtests/9999").status_code == 404


def test_backtest_failure_is_recorded(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    c, *_ = api

    def boom(**kw: Any) -> dict[str, Any]:
        raise RuntimeError("no hourly candles")

    mod = types.ModuleType("kalshibot.coinbase.backtest")
    mod.run_spot_backtest = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kalshibot.coinbase.backtest", mod)
    bt = c.post("/api/coinbase/backtests", json={"strategy": "t_hold"}).json()["id"]
    for _ in range(100):
        d = c.get(f"/api/coinbase/backtests/{bt}").json()
        if d["status"] != "running":
            break
        asyncio.run(asyncio.sleep(0.02))
    assert d["status"] == "failed" and "no hourly candles" in d["error"]


# --------------------------------------------------------------------------- stream


def _events(text: str) -> list[tuple[str, dict[str, Any], str | None]]:
    out = []
    for block in text.split("\n\n"):
        ev, data, eid = None, None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
            elif line.startswith("id: "):
                eid = line[4:]
        if ev:
            out.append((ev, data, eid))
    return out


def test_stream_greeting_replay_and_venue(api: Any) -> None:
    c, _cb, _, _ = api
    r = c.get("/api/coinbase/stream?max_events=1")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.text.startswith("retry: 3000")
    evs = _events(r.text)
    assert evs[0][0] == "account" and evs[0][1]["venue"] == "coinbase" and evs[0][2] is not None
    # the setup published signal/order/fill/bar/log events: replay them
    r = c.get("/api/coinbase/stream?replay=50&max_events=60&duration=0.3")
    evs = _events(r.text)
    kinds = {e[0] for e in evs}
    assert {"signal", "order", "fill", "bar", "log", "account"} <= kinds
    assert all(e[1]["venue"] == "coinbase" for e in evs)
    ids = [int(e[2]) for e in evs if e[2] is not None]
    assert ids == sorted(ids)
    last = ids[-3]
    r = c.get("/api/coinbase/stream?max_events=5&duration=0.3", headers={"Last-Event-ID": str(last)})
    replayed = [int(e[2]) for e in _events(r.text) if e[2] is not None]
    assert replayed and min(replayed) > last
    # the Kalshi stream carries none of it
    k = _events(c.get("/api/stream?replay=100&max_events=100&duration=0.3").text)
    assert all(e[1].get("venue") != "coinbase" for e in k)


def test_unknown_coinbase_path_is_json_404(api: Any) -> None:
    c, *_ = api
    r = c.get("/api/coinbase/nope")
    assert r.status_code == 404 and r.json()["detail"].startswith("Not Found")


def test_every_route_503_when_unavailable(tmp_path: Path) -> None:
    client, _ = make_app(tmp_path, None, build_coinbase=False)
    with client as c:
        for method, path in [("GET", "/api/coinbase/status"), ("POST", "/api/coinbase/engine/start"),
                             ("POST", "/api/coinbase/engine/kill-switch"), ("GET", "/api/coinbase/account"),
                             ("POST", "/api/coinbase/account/reset"), ("GET", "/api/coinbase/equity"),
                             ("GET", "/api/coinbase/positions"), ("GET", "/api/coinbase/orders"),
                             ("POST", "/api/coinbase/orders/1/cancel"), ("GET", "/api/coinbase/fills"),
                             ("GET", "/api/coinbase/strategies"), ("PATCH", "/api/coinbase/strategies/x"),
                             ("GET", "/api/coinbase/risk"), ("PATCH", "/api/coinbase/risk"),
                             ("GET", "/api/coinbase/signals"), ("GET", "/api/coinbase/logs"),
                             ("GET", "/api/coinbase/products"), ("GET", "/api/coinbase/analytics"),
                             ("GET", "/api/coinbase/backtests"), ("POST", "/api/coinbase/backtests"),
                             ("GET", "/api/coinbase/backtests/1"), ("GET", "/api/coinbase/stream"),
                             ("GET", "/api/coinbase/whatever")]:
            r = c.request(method, path, json={} if method != "GET" else None)
            assert r.status_code == 503, (method, path, r.status_code, r.text)
            assert r.json()["detail"].startswith("coinbase venue unavailable: ")
        assert c.get("/api/status").status_code == 200


# --------------------------------------------------------------------------- CLI


def _cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"storage:\n  path: {tmp_path / 'k.sqlite3'}\n"
                   f"coinbase:\n  storage_path: {tmp_path / 'cb.sqlite3'}\n")
    for k in ("KALSHIBOT_STORAGE__PATH", "KALSHIBOT_COINBASE__STORAGE_PATH"):
        monkeypatch.delenv(k, raising=False)
    return cfg


def test_cli_coinbase_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from kalshibot.cli import main
    from kalshibot.coinbase.risk import SpotRiskManager
    from kalshibot.coinbase.store import SpotStore

    cfg = _cli_env(tmp_path, monkeypatch)
    st = SpotStore(tmp_path / "cb.sqlite3")
    SpotRiskManager(None, store=st).set_kill_switch(True, "old")
    st.close()
    assert main(["--config", str(cfg), "coinbase-reset", "-y", "--starting-balance", "1500",
                 "--clear-kill-switch"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["venue"] == "coinbase" and out["starting_balance"] == 1500 and out["equity"] == 1500
    st = SpotStore(tmp_path / "cb.sqlite3")
    assert SpotRiskManager(None, store=st).kill_switch is False
    assert not (tmp_path / "k.sqlite3").exists()  # the Kalshi account was not touched
    # refuses while another process (here: this one) owns the database
    held = SpotStore(tmp_path / "cb2.sqlite3", exclusive=True)
    try:
        assert main(["--config", str(cfg), "coinbase-reset", "-y", "--storage", str(tmp_path / "cb2.sqlite3")]) == 2
        assert "in use" in capsys.readouterr().err
    finally:
        held.close()
        st.close()


def test_cli_coinbase_backtest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                               capsys: pytest.CaptureFixture[str]) -> None:
    from kalshibot.cli import main
    from kalshibot.coinbase import strategies
    from kalshibot.coinbase.store import SpotStore

    cfg = _cli_env(tmp_path, monkeypatch)
    assert main(["--config", str(cfg), "coinbase-backtest", "--strategy", "nope"]) == 2
    assert "unknown coinbase strategy" in capsys.readouterr().err
    monkeypatch.setitem(strategies.REGISTRY, HoldStrategy.name, HoldStrategy)
    assert main(["--config", str(cfg), "coinbase-backtest", "--strategy", "t_hold", "--param", "weight=5"]) == 2
    capsys.readouterr()
    seen: dict[str, Any] = {}

    def fake(cls: Any, params: Any, **kw: Any) -> dict[str, Any]:
        seen.update(cls=cls, params=params, **kw)
        return {"venue": "coinbase", "start": "2025-01-01", "end": "2025-12-31",
                "metrics": {"total_return_pct": 3.0, "details": {"fee_tier": {"name": "intro"}}},
                "trades": [{"product_id": PID, "side": "buy"}], "by_year": [], "equity_curve": [],
                "benchmarks": {"btc": [], "equal_weight": []}, "by_month": [], "signals": []}

    mod = types.ModuleType("kalshibot.coinbase.backtest")
    mod.run_spot_backtest = fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kalshibot.coinbase.backtest", mod)
    assert main(["--config", str(cfg), "coinbase-backtest", "--strategy", "t_hold", "--param", "weight=0.4",
                 "--fee-tier", "intro", "--slippage", "7", "--save", "--opt", "min_trade_usd=5"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["metrics"] == {"total_return_pct": 3.0} and out["trades"] == 1 and out["id"] == 1
    assert seen["params"]["weight"] == 0.4 and seen["slippage"] == 7.0 and seen["min_trade_usd"] == 5
    st = SpotStore(tmp_path / "cb.sqlite3")
    try:
        bt = st.get_backtest(1)
        assert bt is not None and bt["status"] == "done" and bt["fee_tier"] == "intro"
    finally:
        st.close()
