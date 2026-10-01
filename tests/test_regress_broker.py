"""Regression tests for the backend review of ``PaperBroker`` (each one failed before its fix).

Finding ids (F1...) refer to the review list; see the module docstring of
``kalshibot/paper/broker.py`` for the rules these tests pin down.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from conftest import FakeKalshiClient

from kalshibot.marketdata import MarketDataService
from kalshibot.money import D
from kalshibot.paper import ManualClock, PaperBroker, StaticMarketData, make_market
from kalshibot.store import Store

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
A = "KXTEST-26SEP-A"
B = "KXTEST-26SEP-B"


@dataclass
class Intent:
    ticker: str
    side: str
    limit_price: Decimal
    count: int = 1
    action: str = "buy"
    tif: str = "ioc"
    expires_in_s: int | None = None
    strategy: str = "s1"
    reason: str = "test"
    fair_value: float | None = None
    expected_edge: Decimal | None = None
    group_id: str | None = None


def buy(ticker: str, side: str, price: str, count: int, **kw: Any) -> Intent:
    return Intent(ticker=ticker, side=side, limit_price=D(price), count=count, **kw)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(T0)


@pytest.fixture
def md(clock: ManualClock) -> StaticMarketData:
    m = StaticMarketData([make_market(A), make_market(B)], clock=clock)
    m.set_book(A, yes_bids=[("0.40", 50)], no_bids=[("0.58", 10)])  # YES bid .40 x50, YES ask .42 x10
    m.set_book(B, yes_bids=[("0.40", 50)], no_bids=[("0.58", 10)])
    return m


@pytest.fixture
def broker(md: StaticMarketData, clock: ManualClock) -> PaperBroker:
    return PaperBroker(md, Store(":memory:"), starting_balance=1000, profit_sweep_pct=0, clock=clock)


# --------------------------------------------------------------------------- F1 / F8: consumed liquidity


async def test_f1_consumed_level_is_not_reharvested_when_it_shrinks(broker, md, clock):
    assert (await broker.place_order(buy(A, "yes", "0.42", 10))).filled_count == 10
    got = []
    for k in range(1, 6):  # other traders take one contract at a time from the displayed 10-lot
        md.set_book(A, yes_bids=[("0.40", 50)], no_bids=[("0.58", 10 - k)])
        clock.advance(2)
        got.append((await broker.place_order(buy(A, "yes", "0.42", 10))).filled_count)
    assert got == [0, 0, 0, 0, 0]  # was 9, 8, 7, 6, 5: 45 contracts from a level that held 10
    # contracts that join the (now 5-lot) level are new liquidity; the old ones stay taken
    md.set_book(A, yes_bids=[("0.40", 50)], no_bids=[("0.58", 8)])
    clock.advance(2)
    assert (await broker.place_order(buy(A, "yes", "0.42", 10))).filled_count == 3


async def test_f8_ttl_does_not_reexpose_an_unchanged_level(broker, md, clock):
    total = 0
    for i in range(4):
        total += (await broker.place_order(buy(A, "yes", "0.42", 10, strategy=f"s{i}"))).filled_count
        clock.advance(300)
    assert total == 10  # was 40: the same static 10-lot lifted every 300 s
    # once the level is visibly replenished (and the TTL has passed) it counts as fresh liquidity
    md.set_book(A, yes_bids=[("0.40", 50)], no_bids=[("0.58", 12)])
    assert (await broker.place_order(buy(A, "yes", "0.42", 12))).filled_count == 12


# --------------------------------------------------------------------------- F2: buyer prints vs resting bids


async def test_f2_buyer_print_does_not_refill_liquidity_taken_at_placement(broker, md, clock):
    md.set_book(A, yes_bids=[("0.45", 100)], no_bids=[("0.50", 4)])  # only 4 YES offered at .50
    o = await broker.place_order(buy(A, "yes", "0.50", 10, tif="gtc"))
    assert (o.taker_filled_count, o.status, o.queue_ahead) == (4, "partially_filled", D(0))
    md.add_trade(A, "0.50", 4, T0 + timedelta(seconds=1), taker_side="yes")  # a buyer lifts that same offer
    md.set_book(A, yes_bids=[("0.45", 100)], no_bids=[("0.49", 50)])
    clock.advance(5)
    assert await broker.process_resting_orders() == []  # was a 4-lot maker fill: 8 from 4 offered
    assert broker.get_order(o.id).filled_count == 4
    md.add_trade(A, "0.50", 3, T0 + timedelta(seconds=6), taker_side="no")  # a seller hits the bids at .50
    clock.advance(5)
    assert [(f.count, f.is_taker) for f in await broker.process_resting_orders()] == [(3, False)]


async def test_f2_book_cross_fill_is_not_counted_again_from_the_print(broker, md, clock):
    md.set_book(A, yes_bids=[("0.45", 100)], no_bids=[("0.48", 100)])  # YES ask .52
    o = await broker.place_order(buy(A, "yes", "0.50", 10, tif="gtc"))
    md.set_book(A, yes_bids=[("0.45", 100)], no_bids=[("0.50", 6), ("0.48", 100)])  # a .50 offer crosses us
    clock.advance(5)
    assert [f.count for f in await broker.process_resting_orders()] == [6]
    md.add_trade(A, "0.50", 6, clock.now + timedelta(seconds=1), taker_side="yes")  # someone lifts that offer
    md.set_book(A, yes_bids=[("0.45", 100)], no_bids=[("0.48", 100)])
    clock.advance(5)
    assert await broker.process_resting_orders() == []  # was 4 more: 10 fills from a 6-lot offer
    assert broker.get_order(o.id).filled_count == 6


async def test_f2_no_bids_fill_only_from_yes_buyers(broker, md, clock):
    md.set_book(A, yes_bids=[("0.38", 50)], no_bids=[("0.55", 50)])
    o = await broker.place_order(buy(A, "no", "0.60", 10, tif="gtc"))  # improves the NO bid: queue 0
    assert o.status == "open" and o.queue_ahead == 0
    md.add_trade(A, "0.40", 5, T0 + timedelta(seconds=1), taker_side="no")  # a NO buyer hit a YES bid
    clock.advance(2)
    assert await broker.process_resting_orders() == []
    md.add_trade(A, "0.40", 5, T0 + timedelta(seconds=3), taker_side="yes")  # a YES buyer hit a NO bid at .60
    clock.advance(2)
    assert [f.count for f in await broker.process_resting_orders()] == [5]


# --------------------------------------------------------------------------- F5: price-time priority


async def test_f5_one_print_is_shared_in_price_time_priority(broker, md, clock):
    s1 = await broker.place_order(buy(A, "yes", "0.40", 10, tif="gtc", strategy="s1"))  # 50 real ahead
    clock.advance(1)
    s2 = await broker.place_order(buy(A, "yes", "0.41", 10, tif="gtc", strategy="s2"))  # improves: queue 0
    assert (s1.queue_ahead, s2.queue_ahead) == (D(50), D(0))
    md.add_trade(A, "0.40", 60, T0 + timedelta(seconds=2), taker_side="no")
    clock.advance(5)
    new = await broker.process_resting_orders()
    # the seller hits s2 @ .41 first (10), then the 50 real contracts at .40: nothing is left for s1
    assert [(f.order_id, f.count) for f in new] == [(s2.id, 10)]  # was s1 10 + s2 10 (70 from 60)
    assert broker.get_order(s1.id).queue_ahead == 0


# --------------------------------------------------------------------------- F6: fresh market status


def _live_stack(start: datetime, ticker: str) -> tuple[ManualClock, list[float], FakeKalshiClient, PaperBroker]:
    clock = ManualClock(start)
    mono = [0.0]
    fc = FakeKalshiClient()
    fc.add_market(ticker, close_time=datetime(2026, 9, 30, tzinfo=UTC), series_ticker=ticker.split("-")[0])
    fc.set_series(ticker.split("-")[0], "quadratic", 0.5)
    fc.set_book(ticker, yes=[("0.20", 100)], no=[("0.75", 100)])  # YES ask .25
    md = MarketDataService(fc, clock=clock, mono=lambda: mono[0])
    return clock, mono, fc, PaperBroker(md, Store(":memory:"), starting_balance=1000, clock=clock)


async def test_f6_order_rejected_when_market_paused_since_the_cached_snapshot():
    t = "KXMLBHR-26SEP27CLEKC-JRAMIREZ-1"
    clock, mono, fc, b = _live_stack(datetime(2026, 9, 27, 20, 0, tzinfo=UTC), t)
    await b.md.market(t)  # primed by the universe refresh / an earlier order
    fc.update_market(t, status="inactive")  # Kalshi pauses the market; the book stays displayed
    clock.advance(90)
    mono[0] += 90
    o = await b.place_order(buy(t, "yes", "0.25", 40))
    assert o.status == "rejected" and "not active" in o.status_reason  # was filled 40/40


async def test_f6_resting_order_not_crossed_in_a_paused_market():
    t = "KXMLBHR-26SEP27CLEKC-JRAMIREZ-1"
    clock, mono, fc, b = _live_stack(datetime(2026, 9, 27, 20, 0, tzinfo=UTC), t)
    o = await b.place_order(buy(t, "yes", "0.22", 10, tif="gtc"))
    assert o.status == "open"
    fc.update_market(t, status="inactive")
    fc.set_book(t, yes=[("0.20", 100)], no=[("0.80", 100)])  # YES ask .20 crosses our .22 bid
    clock.advance(90)
    mono[0] += 90
    assert await b.process_resting_orders() == []
    assert b.get_order(o.id).filled_count == 0


# --------------------------------------------------------------------------- F7: book read last


async def test_f7_book_is_read_after_the_slow_fee_lookup(broker, md, clock):
    orig = md.event

    async def slow_event(event_ticker: str) -> Any:
        clock.advance(8)  # uncached GET /events/{t} waiting on the rate limiter
        md.set_book(A, yes_bids=[("0.55", 50)], no_bids=[("0.40", 100)])  # the .42 offer is gone: ask .60
        return await orig(event_ticker)

    md.event = slow_event
    o = await broker.place_order(buy(A, "yes", "0.45", 10))
    assert o.filled_count == 0 and o.status == "cancelled"  # was 10 @ .42 from the pre-lookup book


async def test_f7_basket_books_are_fetched_after_every_fee_lookup(broker, md, clock):
    orig = md.event
    calls: list[str] = []

    async def slow_event(event_ticker: str) -> Any:
        calls.append(event_ticker)
        if len(calls) == 2:  # the second leg's lookup is slow; meanwhile leg A's offer is lifted
            clock.advance(3)
            md.set_book(A, yes_bids=[("0.40", 50)], no_bids=[("0.50", 100)])  # A's ask now .50
        return await orig(event_ticker)

    md.event = slow_event
    orders = await broker.place_basket([buy(A, "yes", "0.45", 5), buy(B, "yes", "0.45", 5)])
    assert [o.status for o in orders] == ["rejected", "rejected"]  # leg A was priced from an old book
    assert broker.positions() == []


async def test_f7_stale_book_is_rejected(broker, md, clock):
    orig = md.orderbook

    async def old_book(ticker: str, max_age_s: float = 5) -> Any:
        return replace(await orig(ticker, max_age_s), ts=clock.now - timedelta(seconds=30))

    md.orderbook = old_book
    o = await broker.place_order(buy(A, "yes", "0.45", 5))
    assert o.status == "rejected" and "stale" in o.status_reason


# --------------------------------------------------------------------------- F3 / F16: atomic commits


class FlakyStore(Store):
    """A store whose next call of a named write method raises (disk full / database locked)."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.fail: set[str] = set()

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            self.fail.discard(name)
            raise sqlite3.OperationalError(f"simulated failure in {name}: database or disk is full")

    def insert_settlement(self, s: Any) -> None:
        self._maybe_fail("insert_settlement")
        super().insert_settlement(s)

    def upsert_order(self, o: Any) -> None:
        self._maybe_fail("upsert_order")
        super().upsert_order(o)


async def test_f3_failed_settlement_commit_rolls_memory_back(tmp_path, md, clock):
    path = tmp_path / "flaky.sqlite3"
    store = FlakyStore(path)
    b = PaperBroker(md, store, starting_balance=1000, profit_sweep_pct=0, clock=clock)
    md.set_book(A, yes_bids=[("0.40", 50)], no_bids=[("0.58", 100)])
    await b.place_order(buy(A, "yes", "0.42", 100))
    assert b.cash == D("956.29")  # 1000 - 42 - fee 1.71
    md.set_market(make_market(A, status="finalized", result="yes"))
    store.fail.add("insert_settlement")
    with pytest.raises(sqlite3.OperationalError):
        await b.check_settlements()
    assert b.cash == D("956.29") and b.position(A, "s1").count == 100 and b.realized_pnl == 0
    await b.record_equity_snapshot()  # a later successful commit must not persist half-applied state
    assert [s.count for s in await b.check_settlements()] == [100]
    assert b.cash == D("1056.29") and b.realized_pnl == D("56.29")
    store.close()
    b2 = PaperBroker(md, Store(path), starting_balance=1000, profit_sweep_pct=0, clock=clock)
    assert b2.positions() == [] and b2.cash == D("1056.29") and b2.realized_pnl == D("56.29")
    assert await b2.check_settlements() == []  # never paid twice


async def test_f16_failed_order_commit_returns_rejected_and_ledger_stays_consistent(tmp_path, md, clock):
    path = tmp_path / "flaky2.sqlite3"
    store = FlakyStore(path)
    b = PaperBroker(md, store, starting_balance=1000, clock=clock)
    store.fail.add("upsert_order")
    o = await b.place_order(buy(A, "yes", "0.42", 10))  # used to raise out of place_order
    assert o.status == "rejected" and "store" in o.status_reason
    assert b.cash == D(1000) and b.positions() == [] and b.consumed() == {}
    o2 = await b.place_order(buy(B, "yes", "0.42", 1))
    assert o2.status == "filled"
    cash = b.cash
    store.close()
    b2 = PaperBroker(md, Store(path), starting_balance=1000, clock=clock)
    assert b2.cash == cash and [(p.ticker, p.count) for p in b2.positions()] == [(B, 1)]
    a = b2.account()
    assert a.cash + a.reserved_cash + a.positions_liquidation_value == (
        a.starting_balance + a.realized_pnl + a.unrealized_pnl)


# --------------------------------------------------------------------------- F9: liquidation mark depth


async def test_f9_liquidation_value_walks_the_bid_depth(broker, md, clock):
    md.set_book(A, yes_bids=[("0.30", 1000)], no_bids=[("0.50", 500)])  # YES ask .50
    await broker.place_order(buy(A, "yes", "0.50", 200))
    md.set_book(A, yes_bids=[("0.60", 1), ("0.30", 1000)], no_bids=[("0.35", 500)])  # a 1-lot bid on top
    clock.advance(60)
    a = await broker.record_equity_snapshot()
    assert a.positions_liquidation_value == D("60.30")  # 1 x .60 + 199 x .30 (was 200 x .60 = 120)
    p = broker.positions_json()[0]
    assert p["liquidation_value"] == 60.3 and p["mark_price"] == 0.3015


async def test_f9_positions_in_one_market_share_the_bid_depth(broker, md, clock):
    md.set_book(A, yes_bids=[("0.30", 1000)], no_bids=[("0.50", 500)])
    await broker.place_order(buy(A, "yes", "0.50", 100, strategy="s1"))
    await broker.place_order(buy(A, "yes", "0.50", 100, strategy="s2"))
    md.set_book(A, yes_bids=[("0.60", 100), ("0.30", 1000)], no_bids=[("0.35", 500)])
    clock.advance(60)
    a = await broker.record_equity_snapshot()
    assert a.positions_liquidation_value == D("90")  # 100 x .60 + 100 x .30 for the 200 held in total


# --------------------------------------------------------------------------- F20: bounded broker state


async def test_f20_consumed_entries_and_marks_are_dropped_after_settlement(md, clock):
    b = PaperBroker(md, Store(":memory:"), starting_balance=1000, clock=clock)
    tickers = [f"KXGROW-26SEP-{i}" for i in range(30)]
    for t in tickers:
        md.set_market(make_market(t))
        md.set_book(t, yes_bids=[("0.40", 5)], no_bids=[("0.58", 5)])
        assert (await b.place_order(buy(t, "yes", "0.42", 1))).filled_count == 1
    assert len(b._consumed) == 30
    for t in tickers:
        md.set_market(make_market(t, status="finalized", result="no"))
    clock.advance(600)
    assert len(await b.check_settlements()) == 30
    assert b._consumed == {} and b._marks == {}
    assert b.store.get_kv("broker.consumed") == []


class CountingStore(Store):
    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.kv_writes: Counter[str] = Counter()

    def set_kv(self, key: str, value: Any) -> None:
        self.kv_writes[key] += 1
        super().set_kv(key, value)


async def test_f20_unchanged_broker_state_is_not_rewritten(md, clock):
    st = CountingStore(":memory:")
    b = PaperBroker(md, st, starting_balance=1000, clock=clock)
    await b.place_order(buy(A, "yes", "0.42", 5))
    await b.place_order(buy(A, "yes", "0.40", 5, tif="gtc"))
    n = st.kv_writes["broker.consumed"]
    for _ in range(3):
        clock.advance(15)
        await b.process_resting_orders()
    assert st.kv_writes["broker.consumed"] == n


# --------------------------------------------------------------------------- F21: lock not held across I/O


async def test_f21_cancel_is_not_blocked_by_a_slow_resting_order_pass(broker, md, clock):
    o = await broker.place_order(buy(A, "yes", "0.40", 10, tif="gtc"))
    gate = asyncio.Event()
    orig = md.trades_since

    async def slow_trades(ticker: str, since: datetime) -> Any:
        await gate.wait()
        return await orig(ticker, since)

    md.trades_since = slow_trades
    md.add_trade(A, "0.39", 10, T0 + timedelta(seconds=1))  # would fill the order
    clock.advance(5)
    task = asyncio.create_task(broker.process_resting_orders())
    await asyncio.sleep(0)
    try:
        c = await asyncio.wait_for(broker.cancel_order(o.id), 1.0)
    finally:
        gate.set()
    assert c.status == "cancelled"
    assert await task == []  # the cancelled order is not filled by the pass that was in flight
    assert broker.get_order(o.id).filled_count == 0 and broker.cash == D(1000)
