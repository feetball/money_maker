"""Marks of closed-but-unsettled positions (ARCHITECTURE.md §6 rule 8, §12 ``mark_stale``).

A market that has closed has an empty book until it is determined. It must not be marked at
$0 (fake drawdowns, a tripped daily-loss kill switch): it keeps the last liquidation value
observed **before** ``close_time`` - never more than that pre-close bid ladder would have paid
us, net of the bids we had consumed ourselves - flagged stale, until the result is known
(then the payout).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kalshibot.money import D
from kalshibot.paper import ManualClock, PaperBroker, StaticMarketData, make_market
from kalshibot.store import Store

T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
CLOSE = T0 + timedelta(minutes=10)
A = "KXBTC15M-26SEP271215-15"


@dataclass
class Intent:
    ticker: str
    side: str
    limit_price: Decimal
    count: int
    action: str = "buy"
    tif: str = "ioc"
    strategy: str = "s1"
    reason: str = "test"
    expires_in_s: int | None = None


def buy(side: str, price: str, count: int, **kw) -> Intent:
    return Intent(ticker=A, side=side, limit_price=D(price), count=count, **kw)


def market(status: str = "active", close: datetime = CLOSE, **kw):
    return make_market(A, close_time=close, status=status, **kw)


@pytest.fixture
def clock():
    return ManualClock(T0)


@pytest.fixture
def md(clock):
    m = StaticMarketData([market()], clock=clock)
    # YES bid .90 x50 / .89 x100 ; NO bid .08 x100 (YES ask .92)
    m.set_book(A, yes_bids=[("0.90", 50), ("0.89", 100)], no_bids=[("0.08", 100)])
    return m


@pytest.fixture
def broker(md, clock):
    return PaperBroker(md, Store(":memory:"), starting_balance=1000, clock=clock)


def row(b: PaperBroker, strategy: str = "s1") -> dict:
    return next(r for r in b.positions_json() if r["strategy"] == strategy)


async def close_market(md: StaticMarketData, clock: ManualClock, *, status: str = "closed", after_s: float = 5,
                       **kw) -> None:
    clock.set(CLOSE + timedelta(seconds=after_s))
    md.set_market(market(status, **kw))
    md.set_book(A)  # Kalshi: a closed market's book is empty


async def test_open_market_marks_are_not_stale(broker):
    await broker.place_order(buy("yes", "0.92", 20))
    r = row(broker)
    assert r["mark_stale"] is False and r["mark_ts"] is not None
    assert r["liquidation_value"] == 18.0  # 20 x .90


async def test_closed_market_keeps_the_last_pre_close_value_flagged_stale(broker, md, clock):
    await broker.place_order(buy("yes", "0.92", 20))
    clock.set(CLOSE - timedelta(seconds=30))
    md.set_book(A, yes_bids=[("0.96", 10), ("0.95", 100)], no_bids=[("0.03", 100)])
    await broker.refresh_marks()
    pre = broker.account().positions_liquidation_value
    assert pre == D("19.10")  # 10 x .96 + 10 x .95

    await close_market(md, clock)
    await broker.refresh_marks()  # empty book after close
    await broker.check_settlements()  # closed, no result: marks only
    a = await broker.record_equity_snapshot()
    assert a.positions_liquidation_value == pre  # not $0
    r = row(broker)
    assert r["mark_stale"] is True and r["liquidation_value"] == 19.1 and r["mark_price"] == 0.955
    assert r["mark_ts"].startswith("2026-09-27T12:09:30")  # observed before close

    # a book re-populated after close can never lift the mark above the pre-close ladder
    md.set_book(A, yes_bids=[("0.99", 1000)], no_bids=[("0.00", 0)])
    await broker.refresh_marks()
    assert broker.account().positions_liquidation_value == pre
    assert a.max_drawdown_pct < 1  # no fake drawdown

    # determined: marked at the payout, no longer stale
    md.set_market(market("determined", result="yes"))
    await broker.check_settlements()
    assert broker.account().positions_liquidation_value == D("20")
    assert row(broker)["mark_stale"] is False
    # finalized: settled at the payout
    md.set_market(market("finalized", result="yes"))
    [s] = await broker.check_settlements()
    assert s.payout == D("20.00") and broker.positions() == []


async def test_book_seen_after_close_time_is_ignored_even_while_status_lags(broker, md, clock):
    await broker.place_order(buy("yes", "0.92", 20))  # marked at T0: 20 x .90
    clock.set(CLOSE + timedelta(seconds=1))  # Kalshi still says "active" for a moment
    md.set_book(A, yes_bids=[], no_bids=[("0.01", 5)])  # one-sided: YES bids already gone
    await broker.refresh_marks()
    assert broker.account().positions_liquidation_value == D("18.00")
    assert row(broker)["mark_stale"] is True
    # and a book observed before close (fetched just in time) is still accepted before that
    b2_clock = ManualClock(CLOSE - timedelta(seconds=2))
    md2 = StaticMarketData([market()], clock=b2_clock)
    md2.set_book(A, yes_bids=[("0.90", 50)], no_bids=[("0.08", 100)])
    b2 = PaperBroker(md2, Store(":memory:"), starting_balance=1000, clock=b2_clock)
    await b2.place_order(buy("yes", "0.92", 10))
    md2.set_book(A, yes_bids=[("0.97", 50)], no_bids=[("0.02", 100)])
    books = {A: await md2.orderbook(A)}  # seen at close - 2 s ...
    b2_clock.set(CLOSE + timedelta(seconds=1))
    async with b2._lock:
        b2._apply_books(books, b2_clock())  # ... applied after close: still a pre-close observation
    assert b2.account().positions_liquidation_value == D("9.70")


async def test_frozen_mark_is_net_of_our_own_consumption(broker, md, clock):
    await broker.place_order(buy("yes", "0.92", 10))  # s1: YES 10
    await broker.place_order(buy("no", "0.10", 45, strategy="s2"))  # s2 takes 45 of the 50 YES bids at .90
    assert broker.consumed(A) == {(A, "no", D("0.10")): D("45"), (A, "yes", D("0.92")): D("10")}
    s1_value = D("5") * D("0.90") + D("5") * D("0.89")
    assert row(broker, "s1")["liquidation_value"] == float(s1_value)

    await close_market(md, clock)
    await broker.check_settlements()  # closed: the mark freezes, then consumed entries are forgotten
    assert broker.consumed(A) == {}
    assert row(broker, "s1")["liquidation_value"] == float(s1_value)  # not 10 x .90
    assert row(broker, "s1")["mark_stale"] is True


async def test_frozen_mark_survives_a_restart_with_its_whole_ladder(tmp_path, clock):
    md = StaticMarketData([market()], clock=clock)
    levels = [(D("0.95") - D("0.01") * i, 1) for i in range(15)]  # 15 levels of 1 contract
    md.set_book(A, yes_bids=levels, no_bids=[("0.03", 100)])
    path = tmp_path / "paper.sqlite3"
    st = Store(path)
    b1 = PaperBroker(md, st, starting_balance=1000, clock=clock)
    await b1.place_order(buy("yes", "0.97", 15))
    expected = sum((p for p, _ in levels), D(0))
    assert b1.account().positions_liquidation_value == expected
    await close_market(md, clock)
    await b1.check_settlements()
    await b1.record_equity_snapshot()
    st.close()

    b2 = PaperBroker(md, Store(path), starting_balance=1000, clock=clock)
    assert b2.account().positions_liquidation_value == expected  # all 15 levels, not the usual 10
    assert row(b2)["mark_stale"] is True
    b2.store.close()


async def test_stale_mark_thaws_when_the_close_time_is_extended(broker, md, clock):
    await broker.place_order(buy("yes", "0.92", 20))
    await close_market(md, clock, status="active")  # past close_time, status lagging
    await broker.check_settlements()
    assert row(broker)["mark_stale"] is True
    later = CLOSE + timedelta(hours=1)
    md.set_market(market("active", close=later))  # Kalshi extended the close
    md.set_book(A, yes_bids=[("0.80", 100)], no_bids=[("0.15", 100)])
    await broker.check_settlements()
    await broker.refresh_marks()
    r = row(broker)
    assert r["mark_stale"] is False and r["liquidation_value"] == 16.0


async def test_no_pre_close_observation_is_never_invented(md, clock):
    """A held market whose first book arrives after close keeps its (cost-basis) value."""
    b = PaperBroker(md, Store(":memory:"), starting_balance=1000, clock=clock)
    await b.place_order(buy("yes", "0.92", 5))
    b._marks.clear()  # e.g. lost state
    await close_market(md, clock)
    md.set_book(A, yes_bids=[("0.99", 1000)], no_bids=[])
    await b.refresh_marks()
    assert b.mark(A) is None
    assert b.account().positions_liquidation_value == D("4.60")  # cost basis, not a post-close book


def test_api_positions_flag_stale_marks(settings, tmp_path) -> None:
    """End to end: engine + market data + broker + ``GET /api/positions`` (additive fields)."""
    import asyncio

    from conftest import DummyStrategy, FakeKalshiClient, standard_market
    from fastapi.testclient import TestClient

    from kalshibot.api.server import build_services, create_app
    from kalshibot.feeds import FeedRegistry

    t = "KXTEST-26SEP27-A"
    fc = FakeKalshiClient()
    standard_market(fc, t)  # YES bid .40 x100 / ask .45
    svc = build_services(settings, client=fc, strategies={"dummy": DummyStrategy}, feeds=FeedRegistry())
    svc.md.scanner_days_to_close = 0

    async def setup() -> None:
        svc.engine.update_strategy("dummy", enabled=True)
        await svc.md.refresh_universe(force=True)
        await svc.engine.tick()  # 2 YES @ .45, marked at the .40 bid
        await svc.engine._job_snapshot()
        past = datetime.now(UTC) - timedelta(seconds=5)
        fc.update_market(t, status="closed", close_time=past.isoformat().replace("+00:00", "Z"))
        fc.set_book(t)  # empty after close
        await svc.engine._job_settlement()
        await svc.engine._job_snapshot()

    asyncio.run(setup())
    app = create_app(settings, services=svc, autostart=False, frontend_dist=tmp_path / "nodist")
    with TestClient(app) as c:
        [p] = c.get("/api/positions").json()
        assert p["mark_stale"] is True and p["mark_ts"] and p["liquidation_value"] == 0.8
        assert c.get("/api/account").json()["positions_liquidation_value"] == 0.8
