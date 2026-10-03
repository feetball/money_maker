"""Fixes for the btc15m paper run (HANDOFF.md): the engine's real clock for strategies, day-clustered
bootstrap CIs, the log retention setting, and the ``/api/health`` endpoint. The btc15m-specific cases
are in the "paper-run fixes" block at the end of ``test_strategy_btc15m.py``. Deterministic, no network."""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from conftest import FakeKalshiClient, standard_market

from kalshibot.analytics import bootstrap_ratio_ci, compute_analytics, day_clusters, to_trade, trade_stats
from kalshibot.api.server import AppServices, build_services
from kalshibot.backtest.runner import compute_metrics
from kalshibot.config import Settings
from kalshibot.feeds import FeedRegistry
from kalshibot.kalshi.client import KalshiAPIError
from kalshibot.money import D
from kalshibot.paper.models import Settlement
from kalshibot.strategies.base import OrderIntent, Strategy, UniverseSpec

T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
TICKER = "KXTEST-26SEP27-A"


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def stack(settings: Settings, fc: FakeKalshiClient, strategies: dict[str, Any], clock: Clock) -> AppServices:
    svc = build_services(settings, client=fc, strategies=strategies, feeds=FeedRegistry(), clock=clock)
    svc.md.scanner_days_to_close = 0
    return svc


# --------------------------------------------------------------------------- 2: ctx.clock()


class SlowReads(Strategy):
    """Its 'network reads' take 20 s of engine time inside every tick."""

    name = "slow_reads"
    description = "Test strategy: advances the engine clock inside on_tick."

    engine_clock: Clock  # set by the test: the clock the engine was built with

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.seen: list[tuple[datetime, datetime]] = []

    def universe(self) -> UniverseSpec:
        return UniverseSpec(max_days_to_close=7)

    async def on_tick(self, ctx: Any) -> list[OrderIntent]:
        self.seen.append((ctx.now, ctx.clock()))
        self.engine_clock.now += timedelta(seconds=20)
        self.seen.append((ctx.now, ctx.clock()))
        return []


async def test_engine_context_exposes_the_real_clock(settings: Settings, fake_client: FakeKalshiClient) -> None:
    standard_market(fake_client, TICKER)
    clock = Clock()
    svc = stack(settings, fake_client, {"slow_reads": SlowReads}, clock)
    probe: SlowReads = svc.engine.runtimes["slow_reads"].instance  # type: ignore[assignment]
    probe.engine_clock = clock
    svc.engine.update_strategy("slow_reads", enabled=True)
    await svc.md.refresh_universe(force=True)
    await svc.engine.tick()
    before, after = probe.seen
    assert before == (T0, T0)  # at the tick start the two agree
    assert after == (T0, T0 + timedelta(seconds=20))  # ctx.now stays frozen, clock() moves on
    await svc.aclose()


# --------------------------------------------------------------------------- 3: day-clustered bootstrap


def test_day_clusters_group_events_by_the_utc_day_of_their_earliest_entry() -> None:
    d1, d2 = datetime(2026, 9, 1, 23, 50, tzinfo=UTC), datetime(2026, 9, 2, 0, 10, tzinfo=UTC)
    got = day_clusters(["A", "B", "C"], [d1, d1 + timedelta(minutes=5), d2])
    assert got == ["2026-09-01", "2026-09-01", "2026-09-02"]  # one regime per day, many events per cluster
    # a partial close and the settlement of the same event never split: the earliest time wins
    assert day_clusters(["A", "A"], [d2, d1]) == ["2026-09-01", "2026-09-01"]
    assert day_clusters(["A", "A"], [d1, d2]) == ["2026-09-01", "2026-09-01"]
    # the day is the UTC day, whatever the offset the time was written with
    tz = timezone(timedelta(hours=-5))
    assert day_clusters(["A"], [datetime(2026, 9, 1, 20, 0, tzinfo=tz)]) == ["2026-09-02"]


def test_day_clusters_parse_iso_strings_and_give_a_timeless_event_its_own_cluster() -> None:
    assert day_clusters(["A", "B"], ["2026-09-01T23:59:59Z", "2026-09-02T00:00:00+00:00"]) == [
        "2026-09-01", "2026-09-02"]
    assert day_clusters(["A"], ["2026-09-01T12:00:00"]) == ["2026-09-01"]  # naive = UTC
    got = day_clusters(["A", "B", "C", "C"], [None, "", "not a time", "2026-09-03T01:00:00Z"])
    assert got[3] == "2026-09-03" and got[2] == "2026-09-03"  # C has one usable time: it is C's day
    assert len({got[0], got[1], got[2]}) == 3  # A and B have none: a cluster each
    assert got[0] != "2026-09-03"
    assert day_clusters([], []) == []


def _row(i: int, pnl: str, event: str, ts: datetime, *, opened_at: datetime | None = None) -> Settlement:
    p = D(pnl)
    return Settlement(id=i, ticker=f"KX-{i}", result="yes", side="yes", count=10, payout=D(10) if p > 0 else D(0),
                      cost_basis=D(9), pnl=p, ts=ts, strategy="s", event_ticker=event, opened_at=opened_at)


def test_analytics_cluster_on_opened_at_before_the_row_time() -> None:
    day = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    # every trade opened on its own day but all of them settle on one: the clusters are the open days
    rows = [_row(i, "1" if i % 3 else "-2", f"E{i}", day + timedelta(days=40), opened_at=day + timedelta(days=i))
            for i in range(60)]
    s = trade_stats([to_trade(r) for r in rows], n_boot=300)
    pnl = [float(r.pnl) for r in rows]
    assert (s["ci_low"], s["ci_high"]) == tuple(round(x, 4) for x in bootstrap_ratio_ci(
        pnl, None, [f"d{i}" for i in range(60)], n_boot=300))
    # without opened_at the shared settlement day is a single cluster: no interval at all
    bare = [_row(i, r.pnl.to_eng_string(), f"E{i}", r.ts) for i, r in enumerate(rows)]
    s = trade_stats([to_trade(r) for r in bare], n_boot=300)
    assert s["ci_low"] is None and any("2 UTC days" in x for x in s["readiness"]["reasons"])


def test_analytics_rows_given_as_dicts_with_iso_times() -> None:
    rows = [{"ticker": f"KX-{i}-A", "event_ticker": f"KX-{i}", "pnl": "1" if i % 4 else "-3", "count": 1,
             "payout": "1", "strategy": "s", "ts": f"2026-09-{1 + i // 2:02d}T{i % 2:02d}:00:00Z"} for i in range(40)]
    out = compute_analytics(rows, n_boot=300)  # two windows a day, 20 days
    ov = out["overall"]
    assert ov["count"] == 40 and ov["ci_low"] is not None and out["params"]["cluster"] == "utc_day"
    days = [f"2026-09-{1 + i // 2:02d}" for i in range(40)]
    lo, hi = bootstrap_ratio_ci([float(r["pnl"]) for r in rows], None, days, n_boot=300)
    assert (ov["ci_low"], ov["ci_high"]) == (round(lo, 4), round(hi, 4))


def test_day_clusters_widen_the_interval_when_a_regime_moves_a_whole_day() -> None:
    n_days, per_day = 60, 8
    rng = np.random.default_rng(3)
    regime = rng.normal(0.0, 1.0, n_days)  # a day-level shock shared by that day's events
    pnl = (np.repeat(regime, per_day) + rng.normal(0.3, 0.3, n_days * per_day)).tolist()
    events = [f"E{i}" for i in range(len(pnl))]
    days = [f"D{i // per_day}" for i in range(len(pnl))]
    lo_e, hi_e = bootstrap_ratio_ci(pnl, None, events, n_boot=1000)
    lo_d, hi_d = bootstrap_ratio_ci(pnl, None, days, n_boot=1000)
    assert hi_d - lo_d > 2 * (hi_e - lo_e)  # per-event resampling looked ~3x more certain than it was


def test_backtest_metrics_resample_days_and_leave_pnl_and_trades_unchanged() -> None:
    trades = []
    for k in range(40):  # 4 one-window events a day over 10 days; day 3 and day 7 lose everything
        day = k // 4
        lose = day in (3, 7)
        trades.append({"ts": f"2026-07-{1 + day:02d}T{k % 4:02d}:00:00Z", "ticker": f"W{k}-00",
                       "event_ticker": f"W{k}", "pnl": -9.0 if lose else 1.0, "count": 10, "price": 0.9})
    m, _ = compute_metrics(trades, [], starting_balance=100, final_equity=100 - 8 * 9 + 32 * 1, fees=0, n_boot=500)
    assert m["n_trades"] == 40 and m["events"] == 40 and m["realized_pnl"] == pytest.approx(32 - 72)
    assert m["ev_per_contract"] == pytest.approx(-40 / 400)
    days = [f"2026-07-{1 + k // 4:02d}" for k in range(40)]
    lo, hi = bootstrap_ratio_ci([t["pnl"] for t in trades], [10.0] * 40, days, n_boot=500)
    assert (m["ev_ci_low"], m["ev_ci_high"]) == (pytest.approx(lo, abs=1e-6), pytest.approx(hi, abs=1e-6))
    elo, ehi = bootstrap_ratio_ci([t["pnl"] for t in trades], [10.0] * 40, [t["event_ticker"] for t in trades],
                                  n_boot=500)
    assert (hi - lo) > 1.5 * (ehi - elo)
    # an event with no time is its own cluster; rows of one event on two days share the first day
    two = [{"ts": "2026-07-01T10:00:00Z", "ticker": "E1-X", "event_ticker": "E1", "pnl": 1.0, "count": 1,
            "price": 0.5},
           {"ts": "2026-07-02T10:00:00Z", "ticker": "E1-Y", "event_ticker": "E1", "pnl": 2.0, "count": 1,
            "price": 0.5},
           {"ts": "2026-07-03T10:00:00Z", "ticker": "E2-X", "event_ticker": "E2", "pnl": -1.0, "count": 1,
            "price": 0.5}]
    m2, _ = compute_metrics(two, [], starting_balance=100, final_equity=102, fees=0, n_boot=200)
    assert m2["ev_ci_low"] is not None
    one_day = [{**t, "ts": "2026-07-01T10:00:00Z"} for t in two]
    m3, _ = compute_metrics(one_day, [], starting_balance=100, final_equity=102, fees=0, n_boot=200)
    assert m3["ev_ci_low"] is None and m3["events"] == 2  # a single day: no variance estimate


# --------------------------------------------------------------------------- 4: engine.keep_log_rows


def test_keep_log_rows_setting_defaults_to_the_old_cap(tmp_path: Any) -> None:
    from kalshibot.config import load_settings

    assert Settings().engine.keep_log_rows == 50_000
    p = tmp_path / "c.yaml"
    p.write_text("engine: {keep_log_rows: 0}\n")
    assert load_settings(p, env={}).engine.keep_log_rows == 0
    assert load_settings(p, env={"KALSHIBOT_ENGINE__KEEP_LOG_ROWS": "1234"}).engine.keep_log_rows == 1234
    with pytest.raises(ValueError):
        Settings.model_validate({"engine": {"keep_log_rows": -1}})


def test_store_prune_keeps_everything_for_a_non_positive_keep_last() -> None:
    from kalshibot.store import Store

    s = Store(":memory:")
    for i in range(20):
        s.insert_log("info", "test", f"m{i}")
    assert s.prune("logs", 0) == 0 and s.count("logs") == 20  # was: deleted the whole table
    assert s.prune("logs", -5) == 0 and s.count("logs") == 20
    assert s.prune("logs", 8) == 12 and s.count("logs") == 8
    with pytest.raises(ValueError):
        s.prune("orders", 0)  # the table check still comes first


@pytest.mark.parametrize(("keep", "left"), [(0, 60), (25, 25), (50_000, 60)])
async def test_housekeeping_follows_keep_log_rows(settings: Settings, fake_client: FakeKalshiClient,
                                                  keep: int, left: int) -> None:
    settings.engine.keep_log_rows = keep
    svc = stack(settings, fake_client, {}, Clock())
    try:
        assert svc.engine.keep_rows == keep
        for i in range(60):
            svc.store.insert_log("info", "test", f"m{i}")
            svc.store.insert_signal(ts=T0, strategy="s", ticker=f"KX-{i}", decision="executed")
        logs0, sigs0 = svc.store.count("logs"), svc.store.count("signals")
        await svc.engine._job_housekeeping()
        assert svc.store.count("logs") == min(logs0, left) and svc.store.count("signals") == min(sigs0, left)
        assert svc.store.list_logs(limit=1)[0]["message"] != ""  # the newest rows are the ones kept
    finally:
        await svc.aclose()


# --------------------------------------------------------------------------- 5: GET /api/health


class Flaky(Strategy):
    """Raises on every tick while ``fail`` is set."""

    name = "flaky"
    description = "Test strategy that raises on demand."
    fail = True

    def universe(self) -> UniverseSpec:
        return UniverseSpec(max_days_to_close=7)

    async def on_tick(self, ctx: Any) -> list[OrderIntent]:
        if self.fail:
            raise RuntimeError("model blew up")
        return []


async def alive(svc: AppServices, clock: Clock) -> asyncio.Task[Any]:
    """Make the engine look started (a live loop task) without running the real scheduler, so the
    tests drive ticks and the clock by hand."""
    eng = svc.engine
    eng.running = True
    eng.started_at = clock.now
    eng._task = asyncio.create_task(asyncio.sleep(3600))
    await asyncio.sleep(0)
    return eng._task


async def settle(svc: AppServices, task: asyncio.Task[Any]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    svc.engine._task = None
    svc.engine.running = False
    await svc.aclose()


async def test_health_reports_a_stopped_engine_and_a_dead_task(settings: Settings,
                                                                fake_client: FakeKalshiClient) -> None:
    clock = Clock()
    svc = stack(settings, fake_client, {}, clock)
    h = svc.engine.health()
    assert h["ok"] is False and h["reason"] == "the engine is stopped" and h["gated"] is None
    await svc.engine.start()
    try:
        assert svc.engine.health()["ok"] is True
        loop_task = svc.engine._task
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task

        async def crash() -> None:
            raise RuntimeError("boom")

        svc.engine._task = asyncio.create_task(crash())  # the loop dies without being asked to stop
        await asyncio.sleep(0)
        assert isinstance(svc.engine._task.exception(), RuntimeError)
        svc.engine.running = False
        svc.engine.last_error = "engine loop crashed: RuntimeError: boom"
        h = svc.engine.health()
        assert h["ok"] is False and "the engine task died" in h["reason"] and "boom" in h["reason"]
        assert svc.engine.status()["running"] is False  # /api/status says so too, but answers 200
    finally:
        await svc.engine.stop()
        await svc.aclose()
    assert svc.engine.health()["reason"] == "the engine is stopped"


async def test_health_needs_a_tick_within_max_120s_or_4_tick_intervals(settings: Settings,
                                                                        fake_client: FakeKalshiClient) -> None:
    clock = Clock()
    svc = stack(settings, fake_client, {}, clock)
    task = await alive(svc, clock)
    try:
        eng = svc.engine
        clock.now = T0 + timedelta(seconds=119)  # no tick yet: measured from the (re)start
        h = eng.health()
        assert h["ok"] is True and h["max_tick_age_s"] == 120 and h["tick_age_s"] == 119.0
        clock.now = T0 + timedelta(seconds=121)
        h = eng.health()
        assert h["ok"] is False and h["reason"] == "the engine has not ticked for 121s (limit 120s)"
        await eng.tick()  # a tick at 121 s resets the age
        assert eng.health()["ok"] is True and eng.health()["last_tick_at"] is not None
        clock.now += timedelta(seconds=100)
        assert eng.health()["ok"] is True
        clock.now += timedelta(seconds=21)
        assert eng.health()["ok"] is False
        # a restart restarts the clock too
        eng.started_at = clock.now
        assert eng.health()["ok"] is True
        eng.intervals["tick"] = 60.0  # slower ticks: the limit is 4 intervals
        assert eng.health()["max_tick_age_s"] == 240
        clock.now += timedelta(seconds=239)
        assert eng.health()["ok"] is True
        clock.now += timedelta(seconds=2)
        assert eng.health()["ok"] is False
    finally:
        await settle(svc, task)


async def test_health_when_kalshi_is_unreachable(settings: Settings, fake_client: FakeKalshiClient) -> None:
    clock = Clock()
    svc = stack(settings, fake_client, {}, clock)
    task = await alive(svc, clock)
    try:
        fake_client.fail.add("get_exchange_status")
        with pytest.raises(KalshiAPIError):
            await svc.engine._job_exchange(fresh=True)
        assert svc.engine.kalshi_down is True
        h = svc.engine.health()
        assert h["ok"] is False and h["reason"].startswith("Kalshi is unreachable (")
        fake_client.fail.discard("get_exchange_status")
        await svc.engine._job_exchange(fresh=True)
        assert svc.engine.kalshi_down is False and svc.engine.health()["ok"] is True
    finally:
        await settle(svc, task)


async def test_a_scheduled_exchange_pause_is_healthy_but_gated(settings: Settings,
                                                                fake_client: FakeKalshiClient) -> None:
    clock = Clock()
    svc = stack(settings, fake_client, {"flaky": Flaky}, clock)
    task = await alive(svc, clock)
    try:
        eng = svc.engine
        eng.update_strategy("flaky", enabled=True)
        await eng.tick()  # the strategy fails once
        fake_client.exchange = {"trading_active": False, "exchange_active": True}
        await eng._job_exchange(fresh=True)
        assert eng.trading_paused is True
        clock.now += timedelta(hours=3)  # nothing ticks during a pause: that is not a failure
        h = eng.health()
        assert h["ok"] is True and h["gated"] == "trading_paused" and h["reason"] is None
        fake_client.exchange = {"trading_active": True, "exchange_active": True}
        await eng._job_exchange(fresh=True)
        h = eng.health()  # trading again, and no tick for 3 h: now it is a problem
        assert h["ok"] is False and h["gated"] is None and "has not ticked" in h["reason"]
    finally:
        await settle(svc, task)


async def test_a_strategy_that_raises_on_every_tick_makes_the_engine_unhealthy(
        settings: Settings, fake_client: FakeKalshiClient) -> None:
    standard_market(fake_client, TICKER)
    clock = Clock()
    svc = stack(settings, fake_client, {"flaky": Flaky}, clock)
    task = await alive(svc, clock)
    try:
        eng = svc.engine
        rt = eng.runtimes["flaky"]
        eng.update_strategy("flaky", enabled=True)
        await svc.md.refresh_universe(force=True)
        for _ in range(30):  # 30 s ticks for 15 minutes, every one raises
            await eng.tick()
            clock.now += timedelta(seconds=30)
            if eng.health()["failing_strategies"]:
                break
        # the old blind spot: the strategy's last_tick_at kept advancing and the engine kept ticking
        assert rt.last_tick_at is not None and rt.last_tick_at > T0 and rt.ticks >= 4
        assert rt.last_ok_at is None and rt.last_error == "RuntimeError: model blew up"
        assert eng.status()["running"] is True and eng.status()["last_tick_at"] is not None
        h = eng.health()
        assert h["ok"] is False and "strategy flaky: on_tick has raised on every tick" in h["reason"]
        assert "model blew up" in h["reason"] and h["failing_strategies"][0]["name"] == "flaky"
        # the first failures are not enough: the verdict waits for max(120 s, 4 ticks)
        assert 120 < h["failing_strategies"][0]["failing_for_s"] <= 180
        # it recovers on the first clean tick
        Flaky.fail = False
        try:
            await eng.tick()
        finally:
            Flaky.fail = True
        assert rt.last_ok_at is not None and rt.last_error is None
        assert eng.health()["ok"] is True
    finally:
        await settle(svc, task)


async def test_a_disabled_or_freshly_enabled_failing_strategy_is_not_blamed_at_once(
        settings: Settings, fake_client: FakeKalshiClient) -> None:
    standard_market(fake_client, TICKER)
    clock = Clock()
    svc = stack(settings, fake_client, {"flaky": Flaky}, clock)
    task = await alive(svc, clock)
    try:
        eng = svc.engine
        await svc.md.refresh_universe(force=True)
        clock.now += timedelta(hours=10)  # the engine has been up for ten hours with flaky off
        await eng.tick()
        assert eng.health()["ok"] is True
        eng.update_strategy("flaky", enabled=True)  # switched on from the dashboard
        await svc.md.refresh_universe(force=True)
        await eng.tick()  # its first tick raises
        assert eng.runtimes["flaky"].last_error is not None and eng.health()["ok"] is True
        clock.now += timedelta(seconds=100)
        await eng.tick()
        assert eng.health()["ok"] is True
        clock.now += timedelta(seconds=30)
        await eng.tick()
        assert eng.health()["ok"] is False
        eng.update_strategy("flaky", enabled=False)  # switched off: nothing to blame
        assert eng.health()["ok"] is True
    finally:
        await settle(svc, task)


def test_health_endpoint_status_codes_and_body(settings: Settings, tmp_path: Any) -> None:
    from fastapi.testclient import TestClient

    from kalshibot.api.server import create_app

    fc = FakeKalshiClient()
    svc = build_services(settings, client=fc, strategies={"flaky": Flaky}, feeds=FeedRegistry())
    svc.md.scanner_days_to_close = 0
    app = create_app(settings, services=svc, autostart=False, frontend_dist=tmp_path / "nodist")
    with TestClient(app) as c:
        r = c.get("/api/health")  # engine not started
        assert r.status_code == 503 and r.json()["detail"] == "the engine is stopped" and r.json()["ok"] is False
        assert c.get("/api/status").status_code == 200  # which cannot tell
        assert c.post("/api/engine/start").status_code == 200
        r = c.get("/api/health")
        assert r.status_code == 200 and r.json()["ok"] is True and r.json()["gated"] is None
        assert r.json()["max_tick_age_s"] == 120
        svc.engine.trading_paused = True  # a scheduled exchange pause
        r = c.get("/api/health")
        assert r.status_code == 200 and r.json()["gated"] == "trading_paused"
        svc.engine.trading_paused = False
        svc.engine.kalshi_down = True
        r = c.get("/api/health")
        assert r.status_code == 503 and r.json()["detail"].startswith("Kalshi is unreachable")
        svc.engine.kalshi_down = False
        assert c.post("/api/engine/stop").status_code == 200
        assert c.get("/api/health").status_code == 503


def test_the_docker_healthcheck_uses_the_health_endpoint() -> None:
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    (line,) = [ln for ln in text.splitlines() if ln.lstrip().startswith("CMD python -c")]
    assert "/api/health" in line and "/api/status" not in line


# --------------------------------------------------------------------------- 6: config.paper-run.yaml

ROOT = Path(__file__).resolve().parent.parent
PAPER_RUN = ROOT / "config.paper-run.yaml"


def test_paper_run_config_loads_without_unknown_keys(caplog: pytest.LogCaptureFixture) -> None:
    from kalshibot.config import load_settings

    with caplog.at_level("WARNING"):
        s = load_settings(PAPER_RUN, env={})
    assert "unknown key" not in caplog.text and s.coinbase.load_error in (None, "")
    assert s.account.starting_balance == 10_000
    assert s.account.profit_sweep_enabled is False and s.account.profit_sweep_pct == 0
    assert s.risk.daily_loss_limit == 0  # no account-wide daily stop
    assert s.engine.scanner_days_to_close == 0 and s.engine.keep_log_rows == 0
    assert s.coinbase.enabled is False
    on = {n for n, c in s.strategies.items() if c.enabled}
    assert on == {"btc15m_favorite"}
    btc = s.strategies["btc15m_favorite"]
    assert btc.daily_loss_limit == 0 and btc.max_allocation_pct is None
    assert btc.params == {"sizing": "fixed", "contracts": 50, "max_spot_age_s": 5}


async def test_paper_run_config_builds_the_stack_the_run_assumes(tmp_path: Any) -> None:
    from kalshibot.config import load_settings
    from kalshibot.strategies import REGISTRY

    s = load_settings(PAPER_RUN, env={})
    s.storage.path = str(tmp_path / "paper-run.sqlite3")
    svc = build_services(s, client=FakeKalshiClient(), feeds=FeedRegistry())
    try:
        rows = {r["name"]: r for r in svc.engine.strategies_json()}
        assert set(rows) == set(REGISTRY)  # every shipped strategy is accounted for ...
        assert [n for n, r in rows.items() if r["enabled"]] == ["btc15m_favorite"]  # ... and one runs
        btc = rows["btc15m_favorite"]
        assert btc["params"]["sizing"] == "fixed" and btc["params"]["contracts"] == 50
        assert btc["params"]["max_spot_age_s"] == 5
        assert btc["risk_limits"]["daily_loss_limit"] is None  # 0 = off
        assert svc.risk.limits.daily_loss_limit == 0
        assert svc.broker.starting_balance == 10_000 and svc.broker.cash == 10_000
        assert svc.broker.profit_sweep_enabled is False
        assert svc.engine.keep_rows == 0
        # the fixed 50-lot is not resized by the account's equity (a falling one shrank a swept account's)
        strat = svc.engine.runtimes["btc15m_favorite"].instance
        for equity in (10_000, 3_000, 800):
            n = strat._count(_SizingCtx(), _market(), 0.95, D("0.90"), D(equity))
            assert n == 50
    finally:
        await svc.aclose()


class _SizingCtx:
    def fee(self, market: Any, price: Any, count: Any, is_taker: bool = True) -> Any:
        from kalshibot.fees import trading_fee

        return trading_fee(D(price), D(count), is_taker=is_taker)


def _market() -> Any:
    return object()


def test_deploy_script_is_valid_bash_and_checks_what_the_run_assumes() -> None:
    import subprocess

    script = ROOT / "deploy" / "deploy-smol.sh"
    assert script.stat().st_mode & 0o111  # executable
    subprocess.run(["bash", "-n", str(script)], check=True)
    text = script.read_text()
    for needle in ("config.paper-run.yaml", "/api/health", "strategies_enabled", "profit_sweep_enabled",
                   "contracts", "max_spot_age_s", "daily_loss_limit", "starting_balance", "NOT READY"):
        assert needle in text, needle
    # the script's expectations are the config's
    import re

    want = dict(re.findall(r'"(sizing|contracts|max_spot_age_s)": ("?\w+"?)', text))
    assert want == {"sizing": '"fixed"', "contracts": "50", "max_spot_age_s": "5"}


# --------------------------------------------------------------------------- 8: .env.example


def test_env_example_documents_exactly_the_keys_deploy_sh_reads() -> None:
    import re

    keys = re.findall(r"^([A-Z_]+)=(.*)$", (ROOT / ".env.example").read_text(), flags=re.M)
    assert dict(keys) == {"KALSHIBOT_PORT": "8765", "KALSHIBOT_BIND": "127.0.0.1"}  # the shipped defaults
    reads = re.search(r"\^KALSHIBOT_\(([A-Z|]+)\)\$", (ROOT / "deploy.sh").read_text())
    assert reads is not None and {f"KALSHIBOT_{k}" for k in reads.group(1).split("|")} == dict(keys).keys()
    defaults = dict(re.findall(r'^(?:PORT|BIND)="\$\{(KALSHIBOT_\w+):-([^}]*)\}"', (ROOT / "deploy.sh").read_text(),
                               flags=re.M))
    assert defaults == dict(keys)


# --------------------------------------------------------------------------- research/paper_run


def test_data_review_script_compiles_and_names_the_frozen_rule() -> None:
    import py_compile

    py_compile.compile(str(ROOT / "research" / "paper_run" / "data_review.py"), doraise=True,
                       cfile=str(Path(tempfile.gettempdir()) / "data_review.pyc"))
    prereg = (ROOT / "research" / "paper_run" / "PREREGISTRATION.md").read_text()
    for needle in ("fixed 50 contracts", "PROPOSED", "1,200", "day-clustered"):
        assert needle in prereg, needle


@pytest.mark.skipif(not os.environ.get("KALSHIBOT_RESEARCH_TESTS"),
                    reason="replays the real research panels (needs pandas, scipy, pyarrow): "
                           "set KALSHIBOT_RESEARCH_TESTS=1")
def test_data_review_reproduces_the_research_numbers() -> None:
    for mod in ("pandas", "scipy", "pyarrow"):
        pytest.importorskip(mod)
    import importlib.util

    spec = importlib.util.spec_from_file_location("data_review", ROOT / "research" / "paper_run" / "data_review.py")
    assert spec is not None and spec.loader is not None
    dr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dr)
    backtest, _ = dr.research_modules()
    want = {"in-sample": (555, 3.70), "holdout": (203, 4.94)}  # PREREGISTRATION.md section 2
    seen = 0
    for name, panel_dir in dr.PERIODS.items():
        if not (panel_dir / f"panel_{dr.SERIES}.parquet").exists():
            continue  # the research folders are gitignored
        t = dr.rule_trades(backtest, dr.load_panel(backtest, panel_dir), "next")
        n, mean_c = want[name]
        assert len(t) == n and round(100 * t.pnl.mean(), 2) == pytest.approx(mean_c, abs=0.006)
        lo, hi = dr.ci(t, "day")
        assert lo < 100 * t.pnl.mean() < hi and lo > 0  # positive with days as the clusters
        seen += 1
    if not seen:
        pytest.skip("research panels not present")
