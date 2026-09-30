"""Strategy cancels and cancel/replace (ARCHITECTURE.md §7 ``CancelIntent``, ``replaces``) and the
fair, rate-limited trade-tape polling of resting orders (§6 rule 4, §9 ``orders``)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from conftest import FakeKalshiClient, standard_market

from kalshibot.api.server import AppServices, build_services
from kalshibot.config import Settings
from kalshibot.feeds import FeedRegistry
from kalshibot.money import D
from kalshibot.paper import ManualClock, PaperBroker, StaticMarketData, make_market
from kalshibot.store import Store
from kalshibot.strategies.base import CancelIntent, OrderIntent, Strategy, UniverseSpec

TICKER = "KXTEST-26SEP27-A"


class Planner(Strategy):
    """Returns ``self.plan(ctx)`` (a callable set by the test) on every tick."""

    name = "planner"
    description = "test"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.plan: Callable[[Any], list[Any]] = lambda ctx: []
        self.fills: list[Any] = []

    def universe(self) -> UniverseSpec:
        return UniverseSpec(max_days_to_close=3)

    async def on_tick(self, ctx: Any) -> list[Any]:
        return self.plan(ctx)

    def on_fill(self, fill: Any) -> None:
        self.fills.append(fill)


class Other(Planner):
    name = "other"


def bid(price: str = "0.41", count: int = 5, **kw: Any) -> OrderIntent:
    return OrderIntent(ticker=TICKER, side="yes", count=count, limit_price=D(price), tif="gtc",
                       reason="quote", expected_edge=D("0.01"), **kw)


async def stack(settings: Settings, fc: FakeKalshiClient, *names: str) -> AppServices:
    classes = {"planner": Planner, "other": Other}
    standard_market(fc, TICKER)  # YES bid .40 x100 / ask .45 (NO bid .55 x100)
    svc = build_services(settings, client=fc, strategies={n: classes[n] for n in names}, feeds=FeedRegistry())
    svc.md.scanner_days_to_close = 0
    for n in names:
        svc.engine.update_strategy(n, enabled=True)
    await svc.md.refresh_universe(force=True)
    return svc


def plan(svc: AppServices, name: str, fn: Callable[[Any], list[Any]]) -> None:
    svc.engine.runtimes[name].instance.plan = fn


async def run(svc: AppServices, name: str, fn: Callable[[Any], list[Any]]) -> None:
    plan(svc, name, fn)
    await svc.engine._tick_strategy(svc.engine.runtimes[name], svc.broker.clock())
    plan(svc, name, lambda ctx: [])


def sell_print(fc: FakeKalshiClient, price: str, count: int) -> None:
    """A taker selling YES into the bids (fills YES bids at ``price``) right now."""
    fc.add_trade(TICKER, price, count, datetime.now(UTC) + timedelta(milliseconds=200), taker="no")


async def test_cancel_intent_cancels_own_order_and_releases_cash(settings, fake_client) -> None:
    svc = await stack(settings, fake_client, "planner")
    await run(svc, "planner", lambda ctx: [bid()])
    [o] = svc.broker.open_orders()
    assert o.queue_ahead == 0 and svc.broker.reserved_cash > 0
    await run(svc, "planner", lambda ctx: [CancelIntent(order_id=o.id, reason="requote")])
    got = svc.broker.get_order(o.id)
    assert got.status == "cancelled" and got.status_reason == "requote"
    assert svc.broker.reserved_cash == 0 and svc.broker.cash == D(1000)
    assert svc.engine.runtimes["planner"].cancels == 1
    assert any("cancelled 1 resting order" in r["message"] for r in svc.store.list_logs(kind="strategy"))
    await svc.aclose()


async def test_cancel_first_syncs_the_trade_tape(settings, fake_client) -> None:
    """A print that reached the exchange before the cancel still fills the order."""
    svc = await stack(settings, fake_client, "planner")
    await run(svc, "planner", lambda ctx: [bid(count=5)])
    [o] = svc.broker.open_orders()
    sell_print(fake_client, "0.41", 3)
    await asyncio.sleep(0.25)
    await run(svc, "planner", lambda ctx: [CancelIntent(order_id=o.id)])
    got = svc.broker.get_order(o.id)
    assert got.status == "cancelled" and got.filled_count == 3
    assert svc.broker.position(TICKER, "planner").count == 3
    assert [f.count for f in svc.engine.runtimes["planner"].instance.fills] == [3]  # on_fill hook ran
    await svc.aclose()


async def test_a_strategy_cannot_cancel_another_strategys_order(settings, fake_client) -> None:
    svc = await stack(settings, fake_client, "planner", "other")
    await run(svc, "planner", lambda ctx: [bid()])
    [o] = svc.broker.open_orders()
    await run(svc, "other", lambda ctx: [CancelIntent(order_id=o.id), CancelIntent(ticker=TICKER), CancelIntent()])
    assert svc.broker.get_order(o.id).is_open
    msgs = [r["message"] for r in svc.store.list_logs(kind="strategy")]
    assert any("cancel ignored" in m and f"order {o.id} is not an order of other" in m
               and "needs an order_id or a ticker" in m for m in msgs)
    await svc.aclose()


async def test_ctx_cancel_by_ticker_cancels_all_own_orders_there(settings, fake_client) -> None:
    svc = await stack(settings, fake_client, "planner", "other")
    await run(svc, "planner", lambda ctx: [bid("0.41"), bid("0.39")])
    await run(svc, "other", lambda ctx: [bid("0.38")])
    assert len(svc.broker.open_orders()) == 3

    def cancel_all(ctx: Any) -> list[Any]:
        ctx.cancel(ticker=TICKER, reason="flatten")
        return []

    await run(svc, "planner", cancel_all)
    assert [o.strategy for o in svc.broker.open_orders()] == ["other"]
    await svc.aclose()


async def test_replace_moves_the_quote(settings, fake_client) -> None:
    svc = await stack(settings, fake_client, "planner")
    await run(svc, "planner", lambda ctx: [bid("0.41")])
    [old] = svc.broker.open_orders()
    await run(svc, "planner", lambda ctx: [bid("0.42", replaces=ctx.portfolio.orders_for(TICKER)[0].id)])
    [new] = svc.broker.open_orders()
    assert new.id != old.id and new.limit_price == D("0.42") and new.count == 5
    got = svc.broker.get_order(old.id)
    assert got.status == "cancelled" and got.status_reason == "replaced: quote"
    sig = svc.store.list_signals(limit=1)[0]
    assert sig["decision"] == "executed" and sig["order_id"] == new.id
    await svc.aclose()


async def test_replace_count_is_reduced_by_fills_before_the_cancel(settings, fake_client) -> None:
    svc = await stack(settings, fake_client, "planner")
    await run(svc, "planner", lambda ctx: [bid("0.41", count=5)])
    [old] = svc.broker.open_orders()
    sell_print(fake_client, "0.41", 2)
    await asyncio.sleep(0.25)
    await run(svc, "planner", lambda ctx: [bid("0.42", count=5, replaces=old.id)])
    [new] = svc.broker.open_orders()
    assert new.count == 3 and svc.broker.get_order(old.id).filled_count == 2
    sig = svc.store.list_signals(limit=1)[0]
    assert sig["count"] == 3 and "count reduced by 2" in sig["decision_reason"]
    await svc.aclose()


async def test_replace_is_skipped_when_the_old_order_already_filled(settings, fake_client) -> None:
    svc = await stack(settings, fake_client, "planner")
    await run(svc, "planner", lambda ctx: [bid("0.41", count=5)])
    [old] = svc.broker.open_orders()
    sell_print(fake_client, "0.41", 9)
    await asyncio.sleep(0.25)
    await run(svc, "planner", lambda ctx: [bid("0.42", count=5, replaces=old.id)])
    assert svc.broker.open_orders() == [] and svc.broker.get_order(old.id).status == "filled"
    sig = svc.store.list_signals(limit=1)[0]
    assert sig["decision"] == "rejected" and "was filled before the cancel" in sig["decision_reason"]
    # replacing an order that is not ours / unknown is rejected too
    await run(svc, "planner", lambda ctx: [bid("0.42", replaces=987654)])
    sig = svc.store.list_signals(limit=1)[0]
    assert sig["decision"] == "rejected" and "not an open order of planner" in sig["decision_reason"]
    await svc.aclose()


async def test_cancels_apply_before_new_orders_of_the_same_tick(settings, fake_client) -> None:
    """Cancel/replace frees the old reservation before the new order's risk check."""
    settings.risk.max_position_cost_per_market = 3  # room for one 5-lot quote at .41 only
    svc = await stack(settings, fake_client, "planner")
    await run(svc, "planner", lambda ctx: [bid("0.41", count=5)])
    [old] = svc.broker.open_orders()
    await run(svc, "planner", lambda ctx: [bid("0.42", count=5), CancelIntent(order_id=old.id)])
    [new] = svc.broker.open_orders()
    assert new.limit_price == D("0.42") and new.count == 5
    await svc.aclose()


# --------------------------------------------------------------------------- broker: polling budget


T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


@dataclass
class Intent:
    ticker: str
    side: str
    limit_price: Decimal
    count: int
    action: str = "buy"
    tif: str = "gtc"
    strategy: str = "s1"
    reason: str = "test"
    expires_in_s: int | None = None


def five_markets(clock: ManualClock) -> tuple[StaticMarketData, list[str]]:
    ts = [f"KXTEST-26SEP27-{c}" for c in "ABCDE"]
    md = StaticMarketData([make_market(t) for t in ts], clock=clock)
    for t in ts:
        md.set_book(t, yes_bids=[("0.40", 100)], no_bids=[("0.55", 100)])
    return md, ts


def tape_reads(md: StaticMarketData) -> list[str]:
    return [c[1] for c in md.calls if c[0] == "trades_since"]


async def test_trade_tape_reads_are_capped_and_round_robin(tmp_path) -> None:
    clock = ManualClock(T0)
    md, ts = five_markets(clock)
    b = PaperBroker(md, Store(":memory:"), starting_balance=1000, clock=clock, max_trade_polls_per_pass=2)
    for t in ts:
        await b.place_order(Intent(t, "yes", D("0.41"), 5))
    seen: list[list[str]] = []
    for _ in range(3):
        md.calls.clear()
        clock.advance(15)
        await b.process_resting_orders()
        seen.append(tape_reads(md))
    assert [len(x) for x in seen] == [2, 2, 2]
    assert set(seen[0]) | set(seen[1]) | set(seen[2]) == set(ts)  # every market read within 3 passes
    assert set(seen[2]) == {ts[4], seen[0][0]}  # the unread one, then the least recently read again
    # an explicit ticker list (cancel sync) reads all of them regardless of the cap
    md.calls.clear()
    await b.process_resting_orders(ts)
    assert sorted(tape_reads(md)) == ts


async def test_expiry_waits_for_the_pass_that_reads_the_prints() -> None:
    """A print before the expiry still fills the order even if its market was not read in the
    expiring pass; markets with an order due to expire are read first."""
    clock = ManualClock(T0)
    md, ts = five_markets(clock)
    a, bb = ts[0], ts[1]
    b = PaperBroker(md, Store(":memory:"), starting_balance=1000, clock=clock, max_trade_polls_per_pass=1)
    oa = await b.place_order(Intent(a, "yes", D("0.41"), 5, expires_in_s=60))
    ob = await b.place_order(Intent(bb, "yes", D("0.41"), 5, expires_in_s=60))
    md.add_trade(a, "0.41", 2, T0 + timedelta(seconds=30), taker_side="no")
    md.add_trade(bb, "0.41", 4, T0 + timedelta(seconds=30), taker_side="no")
    clock.advance(90)  # both expired, only one tape read per pass
    await b.process_resting_orders()
    assert b.get_order(oa.id).status == "expired" and b.get_order(oa.id).filled_count == 2
    assert b.get_order(ob.id).is_open  # deferred, not expired without its prints
    await b.process_resting_orders()
    assert b.get_order(ob.id).status == "expired" and b.get_order(ob.id).filled_count == 4


async def test_cancel_orders_filters_by_strategy_and_syncs() -> None:
    clock = ManualClock(T0)
    md, ts = five_markets(clock)
    b = PaperBroker(md, Store(":memory:"), starting_balance=1000, clock=clock)
    o = await b.place_order(Intent(ts[0], "yes", D("0.41"), 5))
    assert await b.cancel_orders([o.id], strategy="someone-else") == []
    assert b.get_order(o.id).is_open
    md.add_trade(ts[0], "0.41", 1, T0 + timedelta(seconds=1), taker_side="no")
    clock.advance(2)
    md.calls.clear()
    [got] = await b.cancel_orders([o.id, o.id, 424242], strategy="s1", reason="bye")
    assert ("trades_since", ts[0]) in [c[:2] for c in md.calls]
    assert got.status == "cancelled" and got.filled_count == 1 and got.status_reason == "bye"
    assert b.reserved_cash == 0


@pytest.mark.parametrize("cap, expect", [(None, 8), (0, None), (3, 3)])
def test_trade_poll_cap_setting(cap: int | None, expect: int | None) -> None:
    b = PaperBroker(StaticMarketData(), None, starting_balance=10, max_trade_polls_per_pass=cap)
    assert b.max_trade_polls_per_pass == expect
