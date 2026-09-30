"""Regression tests for the backend review: money, config, CLI, store and API (each failed before its fix)."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from conftest import DummyStrategy, FakeKalshiClient, standard_market
from fastapi.testclient import TestClient
from pydantic import ValidationError

from kalshibot.api.server import AppServices, build_services, create_app
from kalshibot.cli import main
from kalshibot.config import Settings, load_settings
from kalshibot.feeds import FeedRegistry
from kalshibot.money import D, price
from kalshibot.risk import kelly_count
import kalshibot.store as store_mod
from kalshibot.store import Store
from kalshibot.strategies.base import OrderIntent

np = pytest.importorskip("numpy")


# --------------------------------------------------------------------------- F13: numpy scalars


def test_f13_money_accepts_numpy_scalars():
    assert D(np.float64(0.453)) == Decimal("0.453")  # was decimal.InvalidOperation
    assert D(np.float32(0.25)) == Decimal("0.25")
    assert D(np.int64(3)) == Decimal(3)  # was TypeError
    with pytest.raises(TypeError):
        D(np.bool_(True))
    with pytest.raises(ValueError):
        D(np.float64("nan"))
    assert price(np.float64(0.453)) == Decimal("0.45")
    assert kelly_count(np.float64(0.6), D("0.5"), 1000, 0.25) == 100  # f* = .2, $50 stake / $.50


def test_f13_order_intent_keeps_numpy_values():
    i = OrderIntent(ticker="T", side="yes", count=np.int64(3), limit_price=np.float64(0.45),
                    expected_edge=np.float64(0.05))
    assert i.problems() == []
    assert (i.count, i.limit_price, i.expected_edge) == (3, D("0.45"), D("0.05"))  # edge was silently None


# --------------------------------------------------------------------------- F15 / F25 / F26: config


def test_f15_bad_money_values_raise_validation_errors(tmp_path: Path):
    with pytest.raises(ValidationError):
        load_settings(env={"KALSHIBOT_RISK__MAX_SPREAD": "abc"})  # was decimal.InvalidOperation
    for key, val in (("MAX_POSITION_COST_PER_MARKET", "-5"), ("DAILY_LOSS_LIMIT", "-1"), ("MAX_SPREAD", "-0.1")):
        with pytest.raises(ValidationError):
            load_settings(env={f"KALSHIBOT_RISK__{key}": val})


def test_f25_section_with_every_key_commented_out(tmp_path: Path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("paper:\n#  fee_precision: 0.0001\nrisk:\nengine:\n  tick_s: 10\n")
    s = load_settings(cfg, env={})  # was: 'Input should be a valid dictionary' for paper/risk
    assert s.paper.fee_precision == D("0.01") and s.engine.tick_s == 10


def test_f25_cli_reports_config_errors_in_one_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"risk: {{max_spread: abc}}\nstorage: {{path: {tmp_path / 'db.sqlite3'}}}\n")
    assert main(["-c", str(cfg), "reset", "-y"]) == 2  # was an uncaught pydantic traceback
    err = capsys.readouterr().err
    assert "config" in err and "max_spread" in err and "Traceback" not in err


def test_f26_relative_storage_path_is_relative_to_the_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    cfg = proj / "config.yaml"
    cfg.write_text("storage: {path: data/x.sqlite3}\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    s = load_settings(cfg, env={})
    assert Path(s.storage.path) == (proj / "data" / "x.sqlite3").resolve()  # was ./data/x.sqlite3 in cwd
    cfg.write_text("account: {starting_balance: 10}\n")  # the default path follows the config file too
    assert Path(load_settings(cfg, env={}).storage.path) == (proj / "data" / "kalshibot.sqlite3").resolve()


# --------------------------------------------------------------------------- F17: single writer


def test_f17_store_takes_an_exclusive_process_lock(tmp_path: Path):
    p = tmp_path / "db.sqlite3"
    s1 = Store(p, exclusive=True)
    with pytest.raises(store_mod.StoreLockedError):
        Store(p, exclusive=True)
    s1.close()
    Store(p, exclusive=True).close()  # released on close
    fresh = tmp_path / "new" / "dir" / "db.sqlite3"  # first run: the data directory does not exist yet
    store_mod.ProcessLock(fresh).acquire().release()
    Store(fresh, exclusive=True).close()


def test_f17_cli_reset_refuses_while_the_server_holds_the_database(tmp_path: Path,
                                                                   capsys: pytest.CaptureFixture[str]):
    db = tmp_path / "db.sqlite3"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"storage: {{path: {db}}}\n")
    s = Settings()
    s.storage.path = str(db)
    s.engine.autostart = False
    svc = build_services(s, client=FakeKalshiClient(), strategies={}, feeds=FeedRegistry())  # `serve`
    svc.store.save_account(starting_balance=D(500), cash=D(123))
    assert main(["-c", str(cfg), "reset", "-y"]) == 2  # was: silently wiped, then undone by the server
    assert "in use" in capsys.readouterr().err
    assert svc.store.get_account()["cash"] == D(123)
    asyncio.run(svc.aclose())
    assert main(["-c", str(cfg), "reset", "-y"]) == 0  # fine once the server is gone


# --------------------------------------------------------------------------- F14 / F19: analytics & scans


def _app(settings: Settings, tmp_path: Path) -> tuple[TestClient, AppServices, FakeKalshiClient]:
    fc = FakeKalshiClient()
    svc = build_services(settings, client=fc, strategies={"dummy": DummyStrategy}, feeds=FeedRegistry())
    svc.md.scanner_days_to_close = 0
    app = create_app(settings, services=svc, autostart=False, frontend_dist=tmp_path / "nodist")
    return TestClient(app), svc, fc


def test_f14_analytics_drawdown_uses_every_equity_snapshot(settings: Settings, tmp_path: Path):
    client, svc, _ = _app(settings, tmp_path)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    with svc.store.transaction():
        for i in range(10_000):
            eq = D(700) if i in (3001, 7001) else D(1000)  # two single-snapshot troughs at odd indices
            svc.store.insert_equity_snapshot(ts=base + timedelta(minutes=i), equity=eq, equity_mid=eq, cash=eq)
    with client:
        d = client.get("/api/analytics").json()
    assert d["overall"]["max_drawdown_pct"] == 30.0  # was 0.0 on the 5,000-point thinned series
    assert d["readiness"]["ready"] is False
    assert any("drawdown" in r for r in d["readiness"]["reasons"])


def test_f15_patch_risk_rejects_bad_money_values_with_422(settings: Settings, tmp_path: Path):
    client, svc, _ = _app(settings, tmp_path)
    with client:
        r = client.patch("/api/risk", json={"max_spread": "abc"})
        assert r.status_code == 422 and "max_spread" in r.json()["detail"]  # was 500 InvalidOperation
        for body in ({"max_position_cost_per_market": -5}, {"daily_loss_limit": -1}):
            assert client.patch("/api/risk", json=body).status_code == 422  # was 200 and stored
        assert client.get("/api/risk").json()["limits"]["daily_loss_limit"] == 150  # the unchanged default


async def _populate(svc: AppServices, fc: FakeKalshiClient) -> None:
    for t in ("KXTEST-26SEP27-A", "KXTEST-26SEP27-B"):
        standard_market(fc, t)
    svc.engine.update_strategy("dummy", enabled=True)
    await svc.md.refresh_universe(force=True)
    await svc.engine.tick()
    fc.update_market("KXTEST-26SEP27-B", status="finalized", result="yes", settlement_value_dollars="1.0000")
    await svc.engine._job_settlement()


def test_f19_dashboard_polls_do_not_rescan_the_ledger(settings: Settings, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch):
    client, svc, fc = _app(settings, tmp_path)
    asyncio.run(_populate(svc, fc))
    calls: Counter[str] = Counter()
    for name in ("list_settlements", "strategy_summary", "list_equity"):
        orig = getattr(svc.store, name)

        def wrapped(*a: Any, _n: str = name, _o: Any = orig, **kw: Any) -> Any:
            calls[_n] += 1
            return _o(*a, **kw)

        monkeypatch.setattr(svc.store, name, wrapped)
    with client:
        a1 = client.get("/api/analytics").json()
        a2 = client.get("/api/analytics").json()
        s1 = client.get("/api/strategies").json()
        client.get("/api/strategies")
    assert a1 == a2 and a1["overall"]["count"] == 1
    assert calls["list_settlements"] <= 1  # was one full scan per poll, even on a cache hit
    assert calls["list_equity"] == 0
    assert calls["strategy_summary"] == 0  # was a full fills + settlements scan per poll
    st = {s["name"]: s["stats"] for s in s1}["dummy"]
    assert (st["orders"], st["fills"], st["settled"]) == (2, 2, 1)


def test_f19_old_equity_snapshots_are_downsampled(settings: Settings, tmp_path: Path):
    client, svc, _ = _app(settings, tmp_path)
    now = datetime.now(UTC)
    start = now - timedelta(days=20)
    with svc.store.transaction():
        for i in range(20 * 24 * 60):  # 20 days of 1-minute snapshots
            eq = D(900) if i == 5 * 24 * 60 + 17 else D(1000)  # one single-minute trough 15 days ago
            svc.store.insert_equity_snapshot(ts=start + timedelta(minutes=i), equity=eq, equity_mid=eq, cash=eq)
    n0 = svc.store.count("equity_snapshots")
    asyncio.run(svc.engine._job_housekeeping())  # was: logs and signals only
    n1 = svc.store.count("equity_snapshots")
    assert n1 < n0 * 0.5
    recent = svc.store.list_equity(since=now - timedelta(days=6))
    assert len(recent) >= 6 * 24 * 60 - 1  # the last week keeps full resolution
    assert min(r["equity"] for r in svc.store.list_equity()) == D(900)  # the trough survives
    asyncio.run(svc.aclose())
