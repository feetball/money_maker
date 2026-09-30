"""Engine: strategy tick -> risk -> paper broker -> store, signals, settlement polling, loop."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from conftest import BoomStrategy, DummyStrategy, FakeKalshiClient, ScriptedStrategy, standard_market

from kalshibot.api.server import AppServices, build_services
from kalshibot.config import Settings
from kalshibot.engine import EventBus, is_network_error, jsonable
from kalshibot.feeds import FeedRegistry
from kalshibot.kalshi.client import KalshiAPIError, KalshiNotFound
from kalshibot.money import D
from kalshibot.strategies.base import OrderIntent, ParamError

TICKER = "KXTEST-26SEP27-A"


def stack(settings: Settings, fc: FakeKalshiClient, strategies: dict[str, Any]) -> AppServices:
    svc = build_services(settings, client=fc, strategies=strategies, feeds=FeedRegistry())
    svc.md.scanner_days_to_close = 0
    return svc


async def enable(svc: AppServices, *names: str) -> None:
    for n in names:
        svc.engine.update_strategy(n, enabled=True)
    await svc.md.refresh_universe(force=True)


def drain(q: asyncio.Queue) -> list[tuple[str, Any]]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def test_tick_executes_through_risk_broker_store(settings: Settings, fake_client: FakeKalshiClient) -> None:
    standard_market(fake_client, TICKER)
    svc = stack(settings, fake_client, {"dummy": DummyStrategy})
    q = svc.bus.subscribe()
    await enable(svc, "dummy")
    info = await svc.engine.tick()
    assert info["intents"] == 1 and info["tick_count"] == 1 and info["universe_size"] == 1

    orders = svc.store.list_orders("all")
    assert len(orders) == 1 and orders[0].status == "filled" and orders[0].filled_count == 2
    assert orders[0].avg_fill_price == D("0.45") and orders[0].strategy == "dummy"
    pos = svc.broker.position(TICKER, "dummy")
    assert pos is not None and pos.count == 2 and pos.side == "yes"
    # fee = ceil_cent(0.07 * 2 * 0.45 * 0.55) = ceil_cent(0.03465) = 0.04
    assert svc.broker.cash == D(1000) - D("0.90") - D("0.04")

    sig = svc.store.list_signals()
    assert len(sig) == 1
    s = sig[0]
    assert s["decision"] == "executed" and s["strategy"] == "dummy" and s["ticker"] == TICKER
    assert s["count"] == 2 and s["limit_price"] == D("0.45") and s["fair_value"] == 0.7
    assert "filled 2/2 @ 0.4500" in s["decision_reason"] and s["order_id"] == orders[0].id
    assert s["title"] == f"Title of {TICKER}"

    kinds = [k for k, _ in drain(q)]
    for k in ("signal", "order", "fill", "tick", "account", "log"):
        assert k in kinds, k
    rt = svc.engine.runtimes["dummy"]
    assert len(rt.instance.fills) == 1 and rt.ticks == 1 and rt.intents == 1 and rt.last_error is None
    assert svc.store.get_strategy_state("dummy")["state"] == {"ticks": 1}

    await svc.engine.tick()  # holds the market now: no re-entry
    assert len(svc.store.list_signals()) == 1
    assert svc.store.get_strategy_state("dummy")["state"] == {"ticks": 2}
    await svc.aclose()


async def test_risk_rejection_is_recorded(settings: Settings, fake_client: FakeKalshiClient) -> None:
    standard_market(fake_client, TICKER, yes_bid="0.10", yes_ask="0.45")  # spread 0.35 > max 0.10
    svc = stack(settings, fake_client, {"dummy": DummyStrategy})
    await enable(svc, "dummy")
    await svc.engine.tick()
    s = svc.store.list_signals()[0]
    assert s["decision"] == "rejected" and s["decision_reason"].startswith("risk: ")
    assert "spread" in s["decision_reason"] and s["order_id"] is None
    assert svc.store.list_orders("all") == []
    await svc.aclose()


async def test_risk_reduction_marks_partial(settings: Settings, fake_client: FakeKalshiClient) -> None:
    standard_market(fake_client, TICKER, yes_bid="0.44", yes_ask="0.45", depth=1000)
    settings.risk.max_position_cost_per_market = D(5)
    svc = stack(settings, fake_client, {"scripted": ScriptedStrategy})
    svc.engine.runtimes["scripted"].instance.queue = [[
        OrderIntent(ticker=TICKER, side="yes", count=100, limit_price=D("0.45"), reason="big")]]
    await enable(svc, "scripted")
    await svc.engine.tick()
    s = svc.store.list_signals()[0]
    assert s["decision"] == "partial"
    assert "max_position_cost_per_market" in s["decision_reason"]
    assert 0 < svc.store.list_orders("all")[0].filled_count < 100
    await svc.aclose()


async def test_invalid_intents_are_rejected_signals(settings: Settings, fake_client: FakeKalshiClient) -> None:
    standard_market(fake_client, TICKER)
    svc = stack(settings, fake_client, {"scripted": ScriptedStrategy})
    svc.engine.runtimes["scripted"].instance.queue = [[
        OrderIntent(ticker=TICKER, side="maybe", limit_price=D("0.45")),
        OrderIntent(ticker="NOPE-1", side="yes", limit_price=D("0.45")),
        {"ticker": TICKER, "side": "yes", "limit_price": "0.45", "count": 1, "reason": "dict intent"},
        OrderIntent(ticker=TICKER, side="yes", limit_price=D("0.45"), strategy="someone_else"),
    ]]
    await enable(svc, "scripted")
    await svc.engine.tick()
    sig = list(reversed(svc.store.list_signals()))
    assert [s["decision"] for s in sig] == ["rejected", "rejected", "executed", "rejected"]
    assert "invalid intent" in sig[0]["decision_reason"] and "side" in sig[0]["decision_reason"]
    assert "market unavailable" in sig[1]["decision_reason"]
    assert "does not match" in sig[3]["decision_reason"]
    await svc.aclose()


async def test_failing_strategy_never_kills_the_tick(settings: Settings, fake_client: FakeKalshiClient) -> None:
    standard_market(fake_client, TICKER)
    svc = stack(settings, fake_client, {"boom": BoomStrategy, "dummy": DummyStrategy})
    await enable(svc, "boom", "dummy")
    info = await svc.engine.tick()
    assert info["strategies"] == ["boom", "dummy"] and info["intents"] == 1
    boom = svc.engine.runtimes["boom"]
    assert boom.errors == 1 and boom.last_error == "RuntimeError: kaboom"
    assert svc.broker.position(TICKER, "dummy") is not None
    assert any("kaboom" in r["message"] for r in svc.store.list_logs(kind="strategy"))
    await svc.aclose()


async def test_strategy_timeout(settings: Settings, fake_client: FakeKalshiClient) -> None:
    class Slow(ScriptedStrategy):
        name = "slow"

        async def on_tick(self, ctx: Any) -> list[Any]:
            await asyncio.sleep(10)
            return []

    svc = stack(settings, fake_client, {"slow": Slow})
    svc.engine.tick_timeout_s = 0.05
    await enable(svc, "slow")
    await svc.engine.tick()
    assert "timed out" in svc.engine.runtimes["slow"].last_error
    await svc.aclose()


async def test_basket_all_or_none(settings: Settings, fake_client: FakeKalshiClient) -> None:
    standard_market(fake_client, "KXB-EV-A", yes_bid="0.44", yes_ask="0.45")
    standard_market(fake_client, "KXB-EV-B", yes_bid="0.44", yes_ask="0.45", depth=1)
    svc = stack(settings, fake_client, {"scripted": ScriptedStrategy})
    legs = [OrderIntent(ticker="KXB-EV-A", side="yes", count=5, limit_price=D("0.45"), group_id="g1"),
            OrderIntent(ticker="KXB-EV-B", side="yes", count=5, limit_price=D("0.45"), group_id="g1")]
    ok = [OrderIntent(ticker="KXB-EV-A", side="yes", count=1, limit_price=D("0.45"), group_id="g2"),
          OrderIntent(ticker="KXB-EV-B", side="yes", count=1, limit_price=D("0.45"), group_id="g2")]
    svc.engine.runtimes["scripted"].instance.queue = [legs, ok]
    await enable(svc, "scripted")
    await svc.engine.tick()
    sig = svc.store.list_signals()
    assert len(sig) == 2 and all(s["decision"] == "rejected" for s in sig)
    assert all("basket" in s["decision_reason"] for s in sig)
    assert svc.broker.positions() == []
    await svc.engine.tick()
    sig = svc.store.list_signals(limit=2)
    assert all(s["decision"] == "executed" for s in sig)
    assert {p.ticker for p in svc.broker.positions()} == {"KXB-EV-A", "KXB-EV-B"}
    await svc.aclose()


async def test_resting_order_fills_from_later_trade(settings: Settings, fake_client: FakeKalshiClient) -> None:
    standard_market(fake_client, TICKER)  # bid 0.40 / ask 0.45
    svc = stack(settings, fake_client, {"scripted": ScriptedStrategy})
    svc.engine.runtimes["scripted"].instance.queue = [[
        OrderIntent(ticker=TICKER, side="yes", count=3, limit_price=D("0.41"), tif="gtc", reason="maker")]]
    await enable(svc, "scripted")
    await svc.engine.tick()
    s = svc.store.list_signals()[0]
    assert s["decision"] == "executed" and "resting 3 @ 0.41" in s["decision_reason"]
    assert len(svc.broker.open_orders()) == 1
    fake_client.add_trade(TICKER, "0.40", 5, datetime.now(UTC) + timedelta(seconds=1))
    await svc.engine._job_orders()
    o = svc.store.list_orders("all")[0]
    assert o.status == "filled" and o.filled_count == 3 and o.avg_fill_price == D("0.41")
    assert svc.broker.open_orders() == []
    await svc.aclose()


async def test_settlement_polling_uses_batch_refresh(settings: Settings, fake_client: FakeKalshiClient) -> None:
    standard_market(fake_client, TICKER)
    svc = stack(settings, fake_client, {"dummy": DummyStrategy})
    await enable(svc, "dummy")
    await svc.engine.tick()
    await svc.engine._job_settlement()  # still active: nothing happens
    assert svc.store.list_settlements() == []
    fake_client.update_market(TICKER, status="finalized", result="yes", settlement_value_dollars="1.0000")
    n_single = fake_client.count("get_market")
    await svc.engine._job_settlement()
    st = svc.store.list_settlements()
    assert len(st) == 1 and st[0].result == "yes" and st[0].payout == D(2) and st[0].kind == "settlement"
    assert fake_client.count("get_market") == n_single  # served from the batch-primed cache
    assert any(c[0] == "get" and "tickers" in c[2] for c in fake_client.calls)
    assert svc.broker.positions() == []
    assert svc.broker.cash == D(1000) - D("0.94") + D(2)
    rt = svc.engine.runtimes["dummy"]
    assert len(rt.instance.settlements) == 1
    await svc.aclose()


async def test_snapshot_job_records_equity_and_trips_kill_switch(settings: Settings,
                                                                 fake_client: FakeKalshiClient) -> None:
    standard_market(fake_client, TICKER)
    settings.risk.daily_loss_limit = D("0.5")
    svc = stack(settings, fake_client, {"dummy": DummyStrategy})
    await enable(svc, "dummy")
    await svc.engine._job_snapshot()  # sets the day-start equity (1000)
    await svc.engine.tick()  # buys 2 @ 0.45 + 0.04 fee
    fake_client.set_book(TICKER, yes=[("0.01", 10)], no=[("0.55", 10)])  # bid collapses -> big mark loss
    svc.md._books.clear()
    await svc.engine._job_snapshot()
    rows = svc.store.list_equity()
    assert len(rows) == 2 and rows[-1]["equity"] < rows[0]["equity"]
    assert svc.risk.kill_switch and "daily loss" in svc.risk.kill_switch_reason
    await svc.aclose()


async def test_backoff_and_pause_when_kalshi_unreachable(settings: Settings, fake_client: FakeKalshiClient) -> None:
    standard_market(fake_client, TICKER)
    svc = stack(settings, fake_client, {"dummy": DummyStrategy})
    await enable(svc, "dummy")
    eng = svc.engine
    fake_client.fail.add("get_exchange_status")
    job = eng.jobs["exchange"]
    for expected in (1, 2):
        svc.md._exchange_ts = None
        await eng._job_wrapper(job)
        assert job.failures == expected
    assert eng.kalshi_down and "exchange" in eng.last_error
    # the exchange check gates the ticks: its backoff is capped at 60 s (other jobs: 300 s)
    assert job.next_due - eng.mono() == pytest.approx(min(30 * 4, 60), abs=1)
    await eng._job_tick()
    assert eng.tick_count == 0  # paused while unreachable
    fake_client.fail.clear()
    svc.md._exchange_ts = None
    await eng._job_wrapper(job)
    assert job.failures == 0 and not eng.kalshi_down
    assert eng.last_error is None and eng.last_error_at is not None  # cleared once the job recovers
    fake_client.exchange["trading_active"] = False
    svc.md._exchange_ts = None
    await eng._job_wrapper(job)
    assert eng.trading_paused
    await eng._job_tick()
    assert eng.tick_count == 0
    await svc.aclose()


def test_is_network_error() -> None:
    assert is_network_error(KalshiAPIError(None, "timeout"))
    assert is_network_error(KalshiAPIError(503, "x"))
    assert is_network_error(KalshiAPIError(429, "x"))
    assert not is_network_error(KalshiAPIError(400, "x"))
    assert not is_network_error(KalshiNotFound(404, "x"))
    assert not is_network_error(ValueError("x"))


async def test_update_strategy_persists_and_restores(settings: Settings, fake_client: FakeKalshiClient) -> None:
    standard_market(fake_client, TICKER)
    svc = stack(settings, fake_client, {"dummy": DummyStrategy})
    eng = svc.engine
    assert not eng.runtimes["dummy"].enabled and eng.md.specs == {}
    eng.update_strategy("dummy", enabled=True, params={"count": 7, "series": ["KXOTHER"]})
    rt = eng.runtimes["dummy"]
    assert rt.enabled and rt.instance.params["count"] == 7
    assert eng.md.specs["dummy"].series_tickers == ["KXOTHER"]
    with pytest.raises(ParamError):
        eng.update_strategy("dummy", params={"count": 0})
    with pytest.raises(KeyError):
        eng.update_strategy("nope", enabled=True)
    stored = svc.store.get_strategy_state("dummy")
    assert stored["enabled"] is True and stored["params"] == {"count": 7, "series": ["KXOTHER"]}
    js = eng.strategy_json("dummy")
    assert js["params"]["count"] == 7 and js["enabled"] and js["backtestable"]
    assert js["stats"]["orders"] == 0 and js["param_schema"]["count"]["default"] == 2
    await svc.aclose()
    # a new process with the same database restores enabled + params
    svc2 = stack(settings, FakeKalshiClient(), {"dummy": DummyStrategy})
    rt2 = svc2.engine.runtimes["dummy"]
    assert rt2.enabled and rt2.instance.params["count"] == 7
    await svc2.aclose()


async def test_config_params_and_enabled(settings: Settings, fake_client: FakeKalshiClient) -> None:
    settings.strategies["dummy"] = settings.strategy("dummy").model_copy(
        update={"enabled": True, "params": {"max_price": 0.3, "bogus": 1}})
    svc = stack(settings, fake_client, {"dummy": DummyStrategy})
    rt = svc.engine.runtimes["dummy"]
    assert rt.enabled and rt.instance.params["max_price"] == 0.3 and "bogus" not in rt.instance.params
    await svc.aclose()


async def test_engine_loop_runs_jobs_without_overlap(settings: Settings, fake_client: FakeKalshiClient) -> None:
    standard_market(fake_client, TICKER)
    for k in ("tick_s", "order_poll_s", "settlement_poll_s", "snapshot_s", "universe_refresh_s"):
        setattr(settings.engine, k, 0.05)

    active = {"now": 0, "max": 0}

    class Probe(ScriptedStrategy):
        name = "probe"

        def universe(self) -> Any:
            from kalshibot.strategies.base import UniverseSpec
            return UniverseSpec(max_days_to_close=3)

        async def on_tick(self, ctx: Any) -> list[Any]:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            await asyncio.sleep(0.02)
            active["now"] -= 1
            return []

    svc = stack(settings, fake_client, {"probe": Probe})
    svc.md.min_refresh_interval_s = 0
    svc.engine.update_strategy("probe", enabled=True)
    q = svc.bus.subscribe()
    await svc.engine.start()
    await svc.engine.start()  # idempotent
    for _ in range(200):
        if svc.engine.tick_count >= 3:
            break
        await asyncio.sleep(0.02)
    st = svc.engine.status()
    assert st["running"] and st["tick_count"] >= 3 and st["universe_size"] == 1
    assert st["started_at"] and st["last_tick_at"] and st["strategies_enabled"] == ["probe"]
    assert st["jobs"]["exchange"]["runs"] >= 1 and st["jobs"]["snapshot"]["runs"] >= 1
    assert active["max"] == 1
    await svc.engine.stop()
    assert not svc.engine.status()["running"]
    assert svc.store.list_equity()
    kinds = {k for k, _ in drain(q)}
    assert {"tick", "account", "log"} <= kinds
    await svc.aclose()


async def test_event_bus_bounded_and_threadsafe() -> None:
    bus = EventBus(maxsize=3)
    bus.bind()
    q = bus.subscribe()
    for i in range(5):
        bus.publish("tick", {"i": i, "d": Decimal("0.123456")})
    got = drain(q)
    assert [d["i"] for _, d in got] == [2, 3, 4]  # oldest dropped
    assert got[0][1]["d"] == 0.1235
    t = threading.Thread(target=lambda: bus.publish("log", {"x": 1}))
    t.start()
    t.join()
    await asyncio.sleep(0.01)
    assert q.get_nowait() == ("log", {"x": 1})
    bus.unsubscribe(q)
    assert bus.subscribers == 0


def test_jsonable() -> None:
    now = datetime(2026, 9, 26, 12, tzinfo=UTC)
    assert jsonable({"a": Decimal("1.23456"), "b": [now], "c": float("nan"), "d": (1, "x")}) == {
        "a": 1.2346, "b": ["2026-09-26T12:00:00.000000Z"], "c": None, "d": [1, "x"]}
