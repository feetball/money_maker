"""Venue isolation (contract "Hard rules" + §13): the Kalshi venue keeps working - routes,
shapes, engine, kill switch, database - whatever happens to the Coinbase venue (build
failure, import error, disabled or invalid config, locked store, Coinbase unreachable,
exceptions in its engine), and the Coinbase venue never writes into Kalshi state.
Fakes only - no network. PAPER ONLY."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from test_cb_api import kalshi_services, make_app, populated
from test_cb_engine import (
    NOW,
    BoomStrategy,
    HoldStrategy,
    full_settings,
    make_services,
    standard_client,
)

from kalshibot.api.server import create_app
from kalshibot.config import load_settings

#: GET /api/status top-level keys before the Coinbase venue existed (must not change)
KALSHI_STATUS_KEYS = {"mode", "engine", "exchange", "server_time", "version", "marketdata", "feeds",
                      "strategy_load_errors"}


def assert_kalshi_ok(c: Any) -> None:
    r = c.get("/api/status")
    assert r.status_code == 200 and set(r.json()) == KALSHI_STATUS_KEYS
    acct = c.get("/api/account").json()
    assert acct["starting_balance"] == 1000 and "venue" not in acct
    for path in ("/api/positions", "/api/orders", "/api/fills", "/api/strategies", "/api/signals", "/api/logs",
                 "/api/markets", "/api/equity", "/api/risk", "/api/backtests"):
        assert c.get(path).status_code == 200, path
    assert c.get("/api/nope").status_code == 404
    ov = c.get("/api/overview").json()
    assert ov["venues"]["kalshi"]["available"] is True


def test_kalshi_works_when_coinbase_build_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kalshibot.coinbase import services

    async def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("coinbase build exploded")

    monkeypatch.setattr(services, "build_coinbase_services", boom)
    client, _ = make_app(tmp_path, None, build_coinbase=True, autostart=True)
    with client as c:
        assert_kalshi_ok(c)
        assert c.get("/api/status").json()["engine"]["running"] is True
        r = c.get("/api/coinbase/status")
        assert r.status_code == 503 and r.json()["detail"] == "coinbase venue unavailable: coinbase build exploded"
        ov = c.get("/api/overview").json()["venues"]["coinbase"]
        assert ov["available"] is False and "exploded" in ov["unavailable_reason"]


def test_kalshi_works_when_coinbase_modules_fail_to_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "kalshibot.coinbase.api", None)  # import -> ImportError
    monkeypatch.setitem(sys.modules, "kalshibot.coinbase.services", None)
    client, _ = make_app(tmp_path, None, build_coinbase=True)
    with client as c:
        assert_kalshi_ok(c)
        for path in ("/api/coinbase/status", "/api/coinbase/account", "/api/coinbase/stream"):
            r = c.get(path)
            assert r.status_code == 503 and r.json()["detail"].startswith("coinbase venue unavailable")
        assert c.get("/api/overview").json()["venues"]["coinbase"]["available"] is False


def test_kalshi_works_when_coinbase_disabled(tmp_path: Path) -> None:
    s = full_settings(tmp_path)
    s.coinbase.enabled = False
    client, _ = make_app(tmp_path, None, settings=s, build_coinbase=True)
    with client as c:
        assert_kalshi_ok(c)
        r = c.get("/api/coinbase/account")
        assert r.status_code == 503 and "coinbase.enabled: false" in r.json()["detail"]
    assert not Path(s.coinbase.storage_path).exists()  # nothing was created


def test_kalshi_works_with_invalid_coinbase_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"storage:\n  path: {tmp_path / 'k.sqlite3'}\nengine:\n  autostart: false\n"
                   f"coinbase:\n  max_rps: -5\n  storage_path: {tmp_path / 'cb.sqlite3'}\n")
    s = load_settings(str(cfg))
    assert s.coinbase.enabled is False and s.coinbase.load_error
    client, _ = make_app(tmp_path, None, settings=s, build_coinbase=True)
    with client as c:
        assert_kalshi_ok(c)
        r = c.get("/api/coinbase/status")
        assert r.status_code == 503 and "invalid coinbase config" in r.json()["detail"]


def test_real_wiring_builds_separate_store_and_releases_it(tmp_path: Path) -> None:
    """The production build path (real client, no network at build) with its own file + lock."""
    s = full_settings(tmp_path)
    client, _ = make_app(tmp_path, None, settings=s, build_coinbase=True)
    with client as c:
        st = c.get("/api/coinbase/status").json()
        assert st["venue"] == "coinbase" and st["engine"]["running"] is False
        assert c.get("/api/coinbase/account").json()["equity"] == 1000
        assert Path(s.coinbase.storage_path).exists() and s.coinbase.storage_path != s.storage.path
        # a second server on the same Coinbase file: its Coinbase venue is unavailable, its Kalshi fine
        s2 = full_settings(tmp_path / "other")
        s2.coinbase.storage_path = s.coinbase.storage_path
        client2, _ = make_app(tmp_path / "other", None, settings=s2, build_coinbase=True)
        with client2 as c2:
            assert_kalshi_ok(c2)
            r = c2.get("/api/coinbase/status")
            assert r.status_code == 503 and "in use by another kalshibot process" in r.json()["detail"]
    # shutdown released the lock: the file can be opened exclusively again
    from kalshibot.coinbase.store import SpotStore

    SpotStore(s.coinbase.storage_path, exclusive=True).close()
    # two separate databases (Kalshi's has settlements, Coinbase's does not)
    k_tables = {r[0] for r in sqlite3.connect(s.storage.path).execute("SELECT name FROM sqlite_master")}
    cb_tables = {r[0] for r in sqlite3.connect(s.coinbase.storage_path).execute("SELECT name FROM sqlite_master")}
    assert "settlements" in k_tables and "settlements" not in cb_tables


def test_kalshi_unaffected_when_coinbase_unreachable(tmp_path: Path) -> None:
    fc = standard_client()
    fc.down = True
    cb, _ = asyncio.run(make_services(tmp_path, client=fc, load_products=False))
    client, _ = make_app(tmp_path, cb, coinbase_autostart=True, autostart=True)
    with client as c:
        for _ in range(100):
            if c.get("/api/coinbase/status").json()["engine"]["coinbase_reachable"] is False:
                break
            time.sleep(0.02)
        st = c.get("/api/coinbase/status").json()["engine"]
        assert st["running"] is True and st["coinbase_reachable"] is False
        t0 = time.monotonic()
        assert_kalshi_ok(c)
        assert time.monotonic() - t0 < 5
        assert c.get("/api/status").json()["engine"]["running"] is True
        r = c.get("/api/coinbase/products")
        assert r.status_code == 502 and "unreachable" in r.json()["detail"]
        assert c.get("/api/coinbase/account").status_code == 200  # the paper account stays browsable
        ov = c.get("/api/overview").json()["venues"]["coinbase"]
        assert ov["available"] is True and ov["coinbase_reachable"] is False
    asyncio.run(cb.aclose())


async def test_coinbase_engine_crash_never_touches_kalshi_engine(tmp_path: Path) -> None:
    settings = full_settings(tmp_path)
    ksvc = kalshi_services(tmp_path, settings)
    cb, _ = await make_services(tmp_path, strategies=[BoomStrategy, HoldStrategy])

    async def crash() -> None:
        raise RuntimeError("coinbase snapshot exploded")

    cb.engine.jobs["snapshot"].fn = crash
    await ksvc.engine.start()
    await cb.engine.start()
    await asyncio.sleep(0.3)
    # the Coinbase loop recorded its errors; the Kalshi engine did not notice anything
    assert "exploded" in (cb.engine.status()["last_error"] or "")
    ks = ksvc.engine.status()
    assert ks["running"] is True and ks["last_error"] is None and ks["kill_switch"] is False
    # a strategy exception + a crash of the whole Coinbase scheduler
    await cb.engine.run_due_bars(now=NOW)

    async def die(job: Any) -> None:
        raise RuntimeError("scheduler died")

    cb.engine._run_job = die  # type: ignore[method-assign]
    cb.engine.jobs["bars"].next_due = 0
    cb.engine._wake.set()
    for _ in range(100):
        if not cb.engine.status()["running"]:
            break
        await asyncio.sleep(0.02)
    assert cb.engine.status()["running"] is False and "scheduler died" in cb.engine.status()["last_error"]
    assert ksvc.engine.status()["running"] is True
    tick = await ksvc.engine.tick()  # the Kalshi engine still ticks
    assert isinstance(tick, dict)
    # the Kalshi store / bus / kill switch never saw Coinbase activity
    assert not any("coinbase" in (r["message"] or "").lower() for r in ksvc.store.list_logs(limit=500))
    assert all((d or {}).get("venue") != "coinbase" for _, _, d in ksvc.bus.replay(last=500))
    await cb.engine.set_kill_switch(True, "test")
    assert ksvc.risk.kill_switch is False
    await ksvc.engine.set_kill_switch(True, "kalshi test")
    await cb.engine.set_kill_switch(False, "")
    assert cb.risk.kill_switch is False and ksvc.risk.kill_switch is True
    await ksvc.engine.stop()
    await cb.aclose()
    await ksvc.aclose()


def test_existing_routes_and_shapes_unchanged_with_coinbase_running(tmp_path: Path) -> None:
    cb, _ = asyncio.run(populated(tmp_path))
    client, _ = make_app(tmp_path, cb)
    with client as c:
        assert_kalshi_ok(c)
        assert c.get("/api/coinbase/status").status_code == 200
        # Kalshi signals / logs hold no Coinbase rows
        assert c.get("/api/signals").json() == []
        assert not any("coinbase" in r["message"].lower() for r in c.get("/api/logs?limit=500").json())
        # Kalshi reset leaves the Coinbase account alone (and vice versa)
        before = c.get("/api/coinbase/account").json()
        assert c.post("/api/account/reset", json={"starting_balance": 777}).json()["starting_balance"] == 777
        assert c.get("/api/coinbase/account").json()["open_positions"] == before["open_positions"]
        c.post("/api/coinbase/account/reset", json={"starting_balance": 333})
        assert c.get("/api/account").json()["starting_balance"] == 777
    asyncio.run(cb.aclose())


def test_default_test_apps_do_not_build_coinbase(tmp_path: Path) -> None:
    """Apps built with injected Kalshi services (the existing tests) never create
    data/coinbase.sqlite3 or touch Coinbase."""
    from kalshibot.config import Settings

    s = Settings()
    s.storage.path = str(tmp_path / "k.sqlite3")
    svc = kalshi_services(tmp_path, s)
    app = create_app(s, services=svc, autostart=False, frontend_dist=tmp_path / "nodist")
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        assert c.get("/api/coinbase/status").status_code == 503
        assert c.app.state.cb is None  # type: ignore[attr-defined]
        assert "not built" in c.app.state.cb_error  # type: ignore[attr-defined]
