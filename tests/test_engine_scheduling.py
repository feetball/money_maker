"""Engine tick scheduling (ARCHITECTURE.md §7 ``tick_interval_s``, §9).

Each strategy ticks on its own fixed grid (no drift; late slots are skipped, never bunched),
strategies tick concurrently (a slow one never delays another), and a failing strategy is
isolated. The BTC 15-minute rule needs >= 2 evaluations inside a 60-second window.
"""

from __future__ import annotations

import asyncio
from typing import Any

from conftest import BoomStrategy, DummyStrategy, FakeKalshiClient, ScriptedStrategy, standard_market

from kalshibot.api.server import AppServices, build_services
from kalshibot.config import Settings
from kalshibot.engine import MIN_TICK_INTERVAL_S
from kalshibot.feeds import FeedRegistry
from kalshibot.strategies.base import UniverseSpec

TICKER = "KXTEST-26SEP27-A"


class Mono:
    """Settable monotonic clock."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Counter(ScriptedStrategy):
    name = "counter"
    gate: asyncio.Event | None = None

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.calls = 0

    def universe(self) -> UniverseSpec:
        return UniverseSpec(max_days_to_close=3)

    async def on_tick(self, ctx: Any) -> list[Any]:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        return []


class Fast(Counter):
    name = "fast"
    tick_interval_s = 10


class Slow(Counter):
    name = "slow"  # engine.tick_s (30 s)


def stack(settings: Settings, fc: FakeKalshiClient, strategies: dict[str, Any]) -> AppServices:
    svc = build_services(settings, client=fc, strategies=strategies, feeds=FeedRegistry())
    svc.md.scanner_days_to_close = 0
    return svc


async def settle(svc: AppServices) -> None:
    """Wait for every tick round in flight."""
    for _ in range(100):
        rounds = list(svc.engine._rounds)
        if not rounds:
            return
        await asyncio.gather(*rounds, return_exceptions=True)


async def test_each_strategy_ticks_on_its_own_grid_without_drift(settings, fake_client) -> None:
    svc = stack(settings, fake_client, {"fast": Fast, "slow": Slow})
    eng = svc.engine
    mono = eng.mono = Mono(1000.0)
    for n in ("fast", "slow"):
        eng.update_strategy(n, enabled=True)
    fast, slow = eng.runtimes["fast"], eng.runtimes["slow"]
    assert eng.strategy_interval(fast) == 10 and eng.strategy_interval(slow) == 30
    assert eng.jobs["tick"].interval == 10  # the tick job wakes for the fastest strategy

    ran = []
    for t in (1000, 1005, 1010, 1020, 1030):
        mono.t = t
        ran.append(eng.dispatch())
        await settle(svc)
    assert ran == [["fast", "slow"], [], ["fast"], ["fast"], ["fast", "slow"]]
    assert fast.instance.calls == 4 and slow.instance.calls == 2
    assert eng.tick_count == 4  # one per round that ran something

    mono.t = 1047  # 7 s late for the 1040 slot: runs once, the grid does not shift
    assert eng.dispatch() == ["fast"]
    await settle(svc)
    assert fast.last_lag_ms == 7000 and fast.next_due == 1050 and fast.skipped_ticks == 0
    mono.t = 1075  # slots 1050 and 1060 were missed: counted, not bunched
    assert eng.dispatch() == ["fast", "slow"]
    await settle(svc)
    assert fast.skipped_ticks == 2 and fast.next_due == 1080 and fast.last_lag_ms == 5000
    assert eng._next_tick_due(0) == 1080
    await svc.aclose()


async def test_busy_strategy_skips_its_slot_instead_of_queueing(settings, fake_client) -> None:
    svc = stack(settings, fake_client, {"fast": Fast})
    eng = svc.engine
    mono = eng.mono = Mono(1000.0)
    eng.update_strategy("fast", enabled=True)
    rt = eng.runtimes["fast"]
    rt.instance.gate = asyncio.Event()
    assert eng.dispatch() == ["fast"]
    await asyncio.sleep(0)
    mono.t = 1010
    assert eng.dispatch() == [] and rt.skipped_ticks == 1 and rt.busy  # never two ticks at once
    assert any("skipped 1 tick slot" in r["message"] for r in svc.store.list_logs(kind="strategy"))
    rt.instance.gate.set()
    await settle(svc)
    mono.t = 1020
    assert eng.dispatch() == ["fast"]
    await settle(svc)
    assert rt.instance.calls == 2
    await svc.aclose()


async def test_a_slow_strategy_never_delays_another(settings, fake_client) -> None:
    """Real loop: 'slow' blocks for a while; 'fast' keeps its cadence meanwhile."""
    standard_market(fake_client, TICKER)
    settings.engine.tick_s = 0.05
    class Every(Counter):
        name = "fast"  # engine.tick_s (0.05 s here)

    svc = stack(settings, fake_client, {"fast": Every, "slow": Slow})
    eng = svc.engine
    for n in ("fast", "slow"):
        eng.update_strategy(n, enabled=True)
    gate = asyncio.Event()
    eng.runtimes["slow"].instance.gate = gate
    await eng.start()
    try:
        for _ in range(200):
            if eng.runtimes["fast"].instance.calls >= 6:
                break
            await asyncio.sleep(0.02)
        assert eng.runtimes["fast"].instance.calls >= 6
        assert eng.runtimes["slow"].instance.calls == 1 and eng.runtimes["slow"].skipped_ticks >= 1
    finally:
        gate.set()
        await eng.stop()
    assert not eng.status()["running"]
    await svc.aclose()


async def test_sixty_second_window_gets_at_least_two_evaluations(settings, fake_client) -> None:
    """Default tick_s (30) guarantees 2 slots in any 60 s window; tick_interval_s=10 gives >= 5."""
    for interval, expect in ((None, 2), (10, 6)):
        for offset in (0.0, 7.5, 29.9):  # windows at any phase of the grid
            svc = stack(settings, FakeKalshiClient(), {"fast": Fast})
            eng = svc.engine
            rt = eng.runtimes["fast"]
            rt.instance.tick_interval_s = interval  # an instance attribute overrides the class
            mono = eng.mono = Mono(1000.0)
            eng.update_strategy("fast", enabled=True)
            lo, hi = 1120.0 + offset, 1180.0 + offset
            in_window = 0
            while True:
                mono.t = max(mono.t, rt.next_due) if rt.next_due > 0 else mono.t
                if mono.t >= hi:
                    break
                if eng.dispatch() and mono.t >= lo:
                    in_window += 1
                await settle(svc)
            assert in_window >= expect, (interval, offset, in_window)
            await svc.aclose()


async def test_tick_interval_is_clamped_and_reported(settings, fake_client) -> None:
    class TooFast(Fast):
        name = "toofast"
        tick_interval_s = 0.01

    svc = stack(settings, fake_client, {"toofast": TooFast, "fast": Fast})
    eng = svc.engine
    assert eng.strategy_interval(eng.runtimes["toofast"]) == MIN_TICK_INTERVAL_S
    js = eng.strategy_json("fast")
    assert js["tick_interval_s"] == 10 and js["skipped_ticks"] == 0 and js["cancels"] == 0
    assert "last_tick_lag_ms" in js
    assert eng.jobs["tick"].interval == settings.engine.tick_s  # nothing enabled yet
    eng.update_strategy("fast", enabled=True)
    assert eng.jobs["tick"].interval == 10
    await svc.aclose()


async def test_failing_strategy_is_isolated_in_scheduled_rounds(settings, fake_client) -> None:
    standard_market(fake_client, TICKER)
    svc = stack(settings, fake_client, {"boom": BoomStrategy, "dummy": DummyStrategy})
    eng = svc.engine
    for n in ("boom", "dummy"):
        eng.update_strategy(n, enabled=True)
    await svc.md.refresh_universe(force=True)
    q = svc.bus.subscribe()
    assert eng.dispatch() == ["boom", "dummy"]
    await settle(svc)
    assert eng.runtimes["boom"].errors == 1 and "kaboom" in eng.runtimes["boom"].last_error
    assert svc.broker.position(TICKER, "dummy") is not None
    ticks = []
    while not q.empty():
        kind, data = q.get_nowait()
        if kind == "tick":
            ticks.append(data)
    assert ticks and ticks[-1]["strategies"] == ["boom", "dummy"] and ticks[-1]["intents"] == 1
    await svc.aclose()


async def test_heartbeat_ticks_with_no_strategy_enabled(settings, fake_client) -> None:
    svc = stack(settings, fake_client, {"dummy": DummyStrategy})
    eng = svc.engine
    mono = eng.mono = Mono(1000.0)
    await eng._job_tick()
    await eng._job_tick()  # same instant: no second heartbeat
    assert eng.tick_count == 1 and eng.last_tick_at is not None
    mono.t += settings.engine.tick_s
    await eng._job_tick()
    assert eng.tick_count == 2
    await svc.aclose()


async def test_paused_trading_gates_dispatch_and_rechecks_every_second(settings, fake_client) -> None:
    svc = stack(settings, fake_client, {"fast": Fast})
    eng = svc.engine
    mono = eng.mono = Mono(1000.0)
    eng.update_strategy("fast", enabled=True)
    eng.trading_paused = True
    await eng._job_tick()
    assert eng.runtimes["fast"].instance.calls == 0 and eng._next_tick_due(0) == 1001.0
    eng.trading_paused = False
    await eng._job_tick()
    await settle(svc)
    assert eng.runtimes["fast"].instance.calls == 1
    mono.t = 1000.5
    assert eng._next_tick_due(0) == 1010.0
    await svc.aclose()


async def test_concurrent_strategies_cannot_race_past_risk_limits(settings, fake_client) -> None:
    """Risk check + placement hold the execution lock: two strategies deciding at the same time
    still see each other's orders (without it both would pass the per-market limit)."""
    from kalshibot.money import D
    from kalshibot.strategies.base import OrderIntent

    standard_market(fake_client, TICKER)  # YES ask .45 x100
    settings.risk.max_position_cost_per_market = 10

    class Buyer(Counter):
        name = "buyer_a"

        async def on_tick(self, ctx: Any) -> list[Any]:
            await asyncio.sleep(0)
            return [OrderIntent(ticker=TICKER, side="yes", count=20, limit_price=D("0.45"), reason="r",
                                expected_edge=D("0.01"))]

    class BuyerB(Buyer):
        name = "buyer_b"

    svc = stack(settings, fake_client, {"buyer_a": Buyer, "buyer_b": BuyerB})
    eng = svc.engine
    for n in ("buyer_a", "buyer_b"):
        eng.update_strategy(n, enabled=True)
    await svc.md.refresh_universe(force=True)
    fresh_market = svc.md.market

    async def with_latency(ticker: str, fresh: bool = False) -> Any:  # the broker's fresh read yields
        await asyncio.sleep(0.01)
        return await fresh_market(ticker, fresh=fresh)

    svc.md.market = with_latency  # type: ignore[method-assign]
    assert eng.dispatch() == ["buyer_a", "buyer_b"]
    await settle(svc)
    counts = {p.strategy: p.count for p in svc.broker.positions()}
    assert counts["buyer_a"] == 20 and counts.get("buyer_b", 0) <= 2  # b only got the leftover room
    assert sum(p.cost_basis for p in svc.broker.positions()) <= 10
    await svc.aclose()
