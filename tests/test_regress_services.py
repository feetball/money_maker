"""Regression tests for the backend review: market data, risk and engine (each failed before its fix)."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import DummyStrategy, FakeKalshiClient, ScriptedStrategy, standard_market

from kalshibot.api.server import AppServices, build_services
from kalshibot.config import RiskSettings, Settings
from kalshibot.engine import EngineContext
from kalshibot.feeds import FeedRegistry
from kalshibot.kalshi.models import Event
from kalshibot.marketdata import MarketDataService
from kalshibot.money import D
from kalshibot.paper import ManualClock, PaperBroker, make_market
from kalshibot.paper.models import Order, PortfolioView, Position
from kalshibot.risk import RiskManager
from kalshibot.store import Store
from kalshibot.strategies.base import OrderIntent

T = "KXTEST-26SEP27-A"


def stack(settings: Settings, fc: FakeKalshiClient, strategies: dict[str, Any], **kw: Any) -> AppServices:
    svc = build_services(settings, client=fc, strategies=strategies, feeds=FeedRegistry(), **kw)
    svc.md.scanner_days_to_close = 0
    return svc


async def enable(svc: AppServices, *names: str) -> None:
    for n in names:
        svc.engine.update_strategy(n, enabled=True)
    await svc.md.refresh_universe(force=True)


# --------------------------------------------------------------------------- F4 / F10: scheduled fee overrides


MLB_T = "KXMLBGAME-26SEP271910CLEKC-CLE"
MLB_EV = "KXMLBGAME-26SEP271910CLEKC"


def _mlb(start: datetime) -> tuple[ManualClock, list[float], FakeKalshiClient, MarketDataService]:
    clock = ManualClock(start)
    mono = [0.0]
    fc = FakeKalshiClient()
    fc.add_market(MLB_T, close_time=datetime(2026, 9, 30, tzinfo=UTC), event_ticker=MLB_EV,
                  series_ticker="KXMLBGAME")
    fc.set_series("KXMLBGAME", "quadratic_with_maker_fees", 0.5)
    fc.set_event(MLB_EV)
    fc.event_fee_changes.append({
        "id": "fc1", "event_ticker": MLB_EV, "series_ticker": "KXMLBGAME",
        "fee_type_override": "quadratic_with_maker_fees", "fee_multiplier_override": 1,
        "scheduled_ts": "2026-09-27T19:10:00Z"})  # first pitch
    fc.set_book(MLB_T, yes=[("0.45", 500)], no=[("0.50", 500)])  # YES ask .50
    return clock, mono, fc, MarketDataService(fc, clock=clock, mono=lambda: mono[0])


async def test_f4_scheduled_event_fee_override_applies_at_fill_time():
    clock, mono, fc, md = _mlb(datetime(2026, 9, 27, 18, 50, tzinfo=UTC))
    b = PaperBroker(md, Store(":memory:"), starting_balance=1000, clock=clock)
    o1 = await b.place_order(OrderIntent(ticker=MLB_T, side="yes", count=100, limit_price=D("0.50"), strategy="s"))
    assert o1.fees == D("0.88")  # pre-game M = 0.5: ceil(0.875)
    fc.events[MLB_EV]["fee_type_override"] = "quadratic_with_maker_fees"  # what Kalshi shows after first pitch
    fc.events[MLB_EV]["fee_multiplier_override"] = 1
    clock.advance(40 * 60)
    mono[0] += 40 * 60
    o2 = await b.place_order(OrderIntent(ticker=MLB_T, side="yes", count=100, limit_price=D("0.50"), strategy="s"))
    assert o2.fees == D("1.75")  # in-game M = 1 (was 0.88 from the hour-old cached event)


async def test_f10_strategy_fee_estimate_uses_the_schedule(settings: Settings):
    clock, mono, fc, _ = _mlb(datetime(2026, 9, 27, 18, 50, tzinfo=UTC))
    svc = build_services(settings, client=fc, strategies={"scripted": ScriptedStrategy}, feeds=FeedRegistry(),
                         clock=clock)
    svc.md.mono = lambda: mono[0]
    m = await svc.md.market(MLB_T)
    await svc.md.series("KXMLBGAME")
    await svc.md.event(MLB_EV)
    await svc.engine.tick()  # every tick keeps the fee schedule current (no request unless due)
    rt = svc.engine.runtimes["scripted"]
    ctx = EngineContext(svc.engine, rt, clock.now, {MLB_T: m}, svc.broker.portfolio())
    assert ctx.fee(m, D("0.50"), 100) == D("0.88")
    clock.advance(40 * 60)
    mono[0] += 40 * 60
    ctx = EngineContext(svc.engine, rt, clock.now, {MLB_T: m}, svc.broker.portfolio())
    assert ctx.fee(m, D("0.50"), 100) == D("1.75")
    await svc.aclose()


async def test_f4_scheduled_series_fee_change_applies_after_its_time():
    clock = ManualClock(datetime(2026, 7, 3, 16, 0, tzinfo=UTC))
    mono = [0.0]
    fc = FakeKalshiClient()
    fc.add_market("KXINX-26JUL03-B1", close_time=datetime(2026, 7, 4, tzinfo=UTC), series_ticker="KXINX")
    fc.set_series("KXINX", "quadratic", 0.5)
    fc.series_fee_changes.append({"id": "s1", "series_ticker": "KXINX", "fee_type": "quadratic",
                                  "fee_multiplier": 1, "scheduled_ts": "2026-07-03T17:00:00Z"})
    md = MarketDataService(fc, clock=clock, mono=lambda: mono[0])
    m = await md.market("KXINX-26JUL03-B1")
    assert await md.fee_params(m) == ("quadratic", D("0.5"))
    clock.advance(3600)
    mono[0] += 3600
    assert await md.fee_params(m) == ("quadratic", D(1))


# --------------------------------------------------------------------------- F18: event cache


async def test_f18_event_cache_is_evicted_and_refill_is_fast():
    now = datetime.now(UTC)
    fc = FakeKalshiClient(page_size=1000)
    for i in range(1000):
        for k in "ABC":
            fc.add_market(f"KXEV{i:04d}-26SEP27-{k}", close_time=now + timedelta(hours=6))
    mono = [1000.0]
    md = MarketDataService(fc, clock=lambda: now, mono=lambda: mono[0], scanner_days_to_close=1)
    await md.refresh_universe(force=True)
    assert md.universe_size == 3000
    for i in range(1000):
        et = f"KXEV{i:04d}-26SEP27"
        md.events[et] = Event.from_api({"event": {"event_ticker": et, "series_ticker": f"KXEV{i:04d}"}})
        md._event_ts[et] = mono[0]
    t0 = time.perf_counter()
    md._refill_events()
    assert time.perf_counter() - t0 < 0.5  # was ~1.5 s: a full sort of the universe per cached event
    assert [m.ticker for m in md.events["KXEV0007-26SEP27"].markets] == [
        "KXEV0007-26SEP27-A", "KXEV0007-26SEP27-B", "KXEV0007-26SEP27-C"]
    # events whose markets left the universe are evicted once their TTL has expired
    for i in range(500):
        for k in "ABC":
            fc.markets.pop(f"KXEV{i:04d}-26SEP27-{k}")
    mono[0] += md.event_ttl_s + 1
    await md.refresh_universe(force=True)
    assert len(md.events) == 500 and "KXEV0007-26SEP27" not in md.events
    assert set(md._event_ts) <= set(md.events)


# --------------------------------------------------------------------------- F11: basket risk accumulates


EV = "KXTEST-26SEP27"
LEGS = [f"{EV}-A", f"{EV}-B", f"{EV}-C"]


async def test_f11_basket_legs_share_the_event_limit(settings: Settings, fake_client: FakeKalshiClient):
    for t in LEGS:
        standard_market(fake_client, t, yes_bid="0.50", yes_ask="0.55", depth=500)
    svc = stack(settings, fake_client, {"scripted": ScriptedStrategy})
    await enable(svc, "scripted")
    legs = [OrderIntent(ticker=t, side="yes", count=80, limit_price=D("0.55"), group_id="g1") for t in LEGS]
    sig = await svc.engine.execute_intents("scripted", legs)
    assert [s["decision"] for s in sig] == ["rejected"] * 3  # was executed: $132 against a $100 event limit
    assert "max_exposure_per_event" in sig[0]["decision_reason"]
    assert svc.broker.positions() == []
    small = [OrderIntent(ticker=t, side="yes", count=30, limit_price=D("0.55"), group_id="g2") for t in LEGS]
    sig = await svc.engine.execute_intents("scripted", small)
    assert [s["decision"] for s in sig] == ["executed"] * 3
    assert svc.broker.portfolio().exposure(event_ticker=EV) <= svc.risk.limits.max_exposure_per_event
    await svc.aclose()


# --------------------------------------------------------------------------- F12: resting exits


def _pv(positions: tuple[Position, ...], orders: tuple[Order, ...]) -> PortfolioView:
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    return PortfolioView(ts=now, starting_balance=D(1000), cash=D(900), reserved_cash=D(0), equity=D(1000),
                         equity_mid=D(1000), realized_pnl=D(0), unrealized_pnl=D(0), fees_paid=D(0),
                         day_start_equity=D(1000), positions=positions, open_orders=orders)


def test_f12_resting_exit_orders_count_against_the_position():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    rm = RiskManager(RiskSettings(max_orders_per_minute=0), clock=lambda: now)
    rm.set_kill_switch(True, "test")
    m = make_market(T, yes_bid="0.40", yes_ask="0.45", close_time=now + timedelta(days=1))
    p = Position(ticker=T, strategy="s", event_ticker=EV, side="yes", count=10, cost_basis=D("4.50"))
    exit_ = OrderIntent(ticker=T, side="yes", action="sell", count=10, limit_price=D("0.50"), tif="gtc", strategy="s")
    assert rm.check(exit_, m, _pv((p,), ())).approved_count == 10  # a real exit is always allowed
    resting = Order(id=1, ticker=T, side="yes", action="sell", count=10, limit_price=D("0.50"), tif="gtc",
                    strategy="s", event_ticker=EV, reserved=D("5.01"))
    d = rm.check(exit_, m, _pv((p,), (resting,)))
    assert d.approved_count == 0 and d.binding_limit == "kill_switch"  # was 'ok (closes existing position)'
    part = Order(id=2, ticker=T, side="no", action="buy", count=10, limit_price=D("0.50"), tif="gtc",
                 strategy="s", event_ticker=EV, filled_count=6, reserved=D("2.01"))  # 4 still resting
    d = rm.check(exit_, m, _pv((p,), (part,)))
    assert (d.approved_count, d.closing_count) == (6, 6)


async def test_f12_kill_switch_blocks_a_second_resting_exit(settings: Settings, fake_client: FakeKalshiClient):
    standard_market(fake_client, T, yes_bid="0.40", yes_ask="0.45", depth=100)
    svc = stack(settings, fake_client, {"scripted": ScriptedStrategy})
    await enable(svc, "scripted")
    eng = svc.engine
    await eng.execute_intents("scripted", [OrderIntent(ticker=T, side="yes", count=10, limit_price=D("0.45"))])
    svc.risk.set_kill_switch(True, "test")
    sig = []
    for _ in range(2):
        sig += await eng.execute_intents("scripted", [OrderIntent(ticker=T, side="yes", action="sell", count=10,
                                                                  limit_price=D("0.50"), tif="gtc")])
    assert [s["decision"] for s in sig] == ["executed", "rejected"]
    assert "kill switch" in sig[1]["decision_reason"]
    await svc.aclose()


# --------------------------------------------------------------------------- F21: batched order polling


async def test_f21_orders_job_and_tick_fetch_books_in_batches(settings: Settings, fake_client: FakeKalshiClient):
    tickers = [f"{EV}-A", f"{EV}-B", f"{EV}-C"]
    for t in tickers:
        standard_market(fake_client, t)  # bid .40 / ask .45
    svc = stack(settings, fake_client, {"dummy": DummyStrategy})
    await enable(svc, "dummy")
    n_single, n_batch = fake_client.count("get_orderbook"), fake_client.count("get_orderbooks")
    await svc.engine.tick()
    assert len(svc.broker.positions()) == 3
    assert fake_client.count("get_orderbook") == n_single  # was one single-book request per intent
    assert fake_client.count("get_orderbooks") == n_batch + 1
    for t in tickers:
        await svc.broker.place_order(OrderIntent(ticker=t, side="yes", count=1, limit_price=D("0.41"), tif="gtc",
                                                 strategy="other"))
    svc.md._books.clear()
    n_single, n_batch = fake_client.count("get_orderbook"), fake_client.count("get_orderbooks")
    await svc.engine._job_orders()
    assert fake_client.count("get_orderbook") == n_single  # was 3 single-book requests
    assert fake_client.count("get_orderbooks") == n_batch + 1
    await svc.aclose()


# --------------------------------------------------------------------------- F22: start/stop race


class SlowWindowClient(FakeKalshiClient):
    delay = 0.15

    async def get(self, path: str, params: dict[str, Any] | None = None, *, repeat: Any = ()) -> dict:
        if path == "/markets" and "min_close_ts" in (params or {}):
            await asyncio.sleep(self.delay)
        return await super().get(path, params, repeat=repeat)


def _loops() -> int:
    return sum(1 for t in asyncio.all_tasks() if t.get_name() == "kalshibot-engine" and not t.done())


async def test_f22_start_during_stop_leaves_exactly_one_visible_loop():
    fc = SlowWindowClient(page_size=2)
    for i in range(10):
        standard_market(fc, f"KXWIN-{i}-A", close_time=datetime.now(UTC) + timedelta(hours=3))
    s = Settings()
    s.engine.autostart = False
    svc = build_services(s, client=fc, store=Store(":memory:"), strategies={"scripted": ScriptedStrategy})
    eng = svc.engine
    await eng.start()
    await asyncio.sleep(0.3)  # a universe refresh is in flight
    old = eng._task
    stopper = asyncio.create_task(eng.stop())  # POST /api/engine/stop
    while not old.done():
        await asyncio.sleep(0)
    await eng.start()  # POST /api/engine/start arrives while stop() is still cleaning up
    await stopper
    await asyncio.sleep(0.05)
    assert _loops() == eng.status()["running"]  # was: running=False with a live, invisible loop
    await eng.start()
    await asyncio.sleep(0.05)
    assert _loops() == 1
    await eng.stop()
    await asyncio.sleep(0.05)
    assert _loops() == 0
    await svc.aclose()


# --------------------------------------------------------------------------- F23: forced refresh not lost


async def test_f23_strategy_enabled_during_a_refresh_is_picked_up_right_after_it():
    fc = SlowWindowClient(page_size=2)
    now = datetime.now(UTC)
    for i in range(10):  # 5 slow pages: each full scan takes ~0.75 s
        standard_market(fc, f"KXWIN-{i}-A", close_time=now + timedelta(hours=3))
    standard_market(fc, "KXNEW-1-A", close_time=now + timedelta(days=5))  # only via series_tickers
    s = Settings()
    s.engine.autostart = False
    svc = build_services(s, client=fc, store=Store(":memory:"), strategies={"dummy": DummyStrategy})
    eng, md = svc.engine, svc.md
    await eng.start()
    await asyncio.sleep(0.3)
    assert eng.jobs["universe"].task is not None and not eng.jobs["universe"].task.done()
    eng.update_strategy("dummy", enabled=True, params={"series": ["KXNEW"], "days": 0})
    for _ in range(60):  # 3 s: well below the 120 s scheduled refresh
        if "KXNEW-1-A" in md.markets:
            break
        await asyncio.sleep(0.05)
    assert "KXNEW-1-A" in md.markets  # was only after the next scheduled refresh (~120 s)
    await eng.stop()
    await svc.aclose()


# --------------------------------------------------------------------------- F24: recovery after an outage


async def test_f24_ticks_resume_soon_after_kalshi_recovers(settings: Settings, fake_client: FakeKalshiClient):
    standard_market(fake_client, T)
    svc = stack(settings, fake_client, {})
    eng, md = svc.engine, svc.md
    mono = [1000.0]
    eng.mono = md.mono = lambda: mono[0]
    job = eng.jobs["exchange"]
    fake_client.fail.add("get_exchange_status")
    for _ in range(4):
        mono[0] = max(mono[0], job.next_due)
        await eng._run_job(job)
        assert job.next_due - mono[0] <= 60  # was 60 / 120 / 240 / 300 s
    assert eng.kalshi_down
    fake_client.fail.clear()
    mono[0] += 1
    await md.market(T, fresh=True)  # any successful Kalshi request shows it is reachable again
    ticks = eng.tick_count
    await eng._job_tick()
    assert not eng.kalshi_down and eng.tick_count == ticks + 1
    await svc.aclose()
