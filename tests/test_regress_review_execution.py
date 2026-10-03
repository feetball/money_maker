"""Regressions from the execution / risk review (2026-09-27).

* taker latency: an engine order walks only a book received ``paper.taker_latency_s`` after the
  decision, never the decision book (stale-quote pick-off);
* maker crossing fills and the queue bound wait for a pass that read the prints;
* all-or-none baskets that would have legged are counted and logged;
* the order-rate budget is per strategy (another strategy's burst cannot reject btc15m's order);
* per-strategy allocation caps, per-strategy daily-loss pauses and the daily kill-switch release;
* the ``kalshi_settled`` feed shares the engine's rate-limited client.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from conftest import FakeKalshiClient, ScriptedStrategy, standard_market

from kalshibot.api.server import build_services
from kalshibot.config import RiskSettings, Settings
from kalshibot.feeds import FeedRegistry
from kalshibot.kalshi.models import Orderbook
from kalshibot.money import ZERO, D
from kalshibot.paper.broker import PaperBroker
from kalshibot.paper.models import Order, PortfolioView, Position
from kalshibot.paper.sim import ManualClock, StaticMarketData, make_market
from kalshibot.risk import RiskManager, StrategyLimits, strategy_limits
from kalshibot.store import Store
from kalshibot.strategies.base import OrderIntent
from kalshibot.strategies.btc15m_favorite import Btc15mFavorite
from kalshibot.strategies.ladder_favorite import LadderFavorite
from kalshibot.strategies.maker_favorite import MakerFavoriteHarvest
from kalshibot.strategies.no_basket_arb import NoBasketArb

T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
T = "KXWTIW-26OCT02-B80"


def intent(ticker: str = T, side: str = "yes", count: int = 10, price: str = "0.90", **kw: Any) -> OrderIntent:
    return OrderIntent(ticker=ticker, side=side, count=count, limit_price=D(price), strategy=kw.pop("strategy", "s1"),
                       **kw)


# --------------------------------------------------------------------------- taker latency (finding 7)


class MovingExchange(StaticMarketData):
    """The exchange re-prices while the order is in flight: ``sleep`` advances the clock and swaps the
    book. Books requested with ``max_age_s > 0`` come from a cache filled at the decision time."""

    def __init__(self, clock: ManualClock) -> None:
        super().__init__([make_market(T, close_time=T0 + timedelta(days=2), yes_bid="0.89", yes_ask="0.90")],
                         clock=clock)
        self.set_series("KXWTIW")
        self.set_book(T, yes_bids=[("0.89", 500)], no_bids=[("0.10", 500)])  # YES ask 0.90
        self.cached = dataclasses_replace(self.books[T], ts=clock())  # what the decision saw
        self.clk = clock

    async def sleep(self, seconds: float) -> None:
        self.clk.advance(seconds)
        self.set_book(T, yes_bids=[("0.94", 500)], no_bids=[("0.05", 500)])  # YES ask 0.95 now

    async def orderbook(self, ticker: str, max_age_s: float = 5) -> Orderbook:
        self.calls.append(("orderbook", ticker, max_age_s))
        if max_age_s > 0:
            return self.cached  # a cache hit on the decision book
        return dataclasses_replace(self.books[ticker], ts=self.clock())


def dataclasses_replace(obj: Any, **kw: Any) -> Any:
    import dataclasses

    return dataclasses.replace(obj, **kw)


async def test_engine_orders_never_walk_the_decision_book() -> None:
    clk = ManualClock(T0)
    md = MovingExchange(clk)
    b = PaperBroker(md, None, starting_balance=1000, clock=clk, sleep=md.sleep, taker_latency_s=0.25)
    o = await b.place_order(intent(), decided_at=T0)
    # the book moved from 0.90 to 0.95 during the 0.25 s latency: the IOC at 0.90 cannot fill
    assert o.status != "filled" and o.filled_count == 0
    assert clk.now == T0 + timedelta(seconds=0.25)
    assert ("orderbook", T, 0) in md.calls  # the cached decision book was refused, a new one fetched
    # without a decision time (direct calls, tests): the usual fresh read (the cache, here)
    clk2 = ManualClock(T0)
    md2 = MovingExchange(clk2)
    b2 = PaperBroker(md2, None, starting_balance=1000, clock=clk2, sleep=md2.sleep, taker_latency_s=0.25)
    o2 = await b2.place_order(intent())
    assert o2.status == "filled" and o2.avg_fill_price == D("0.90")


async def test_latency_setting_and_not_before() -> None:
    s = Settings()
    assert s.paper.taker_latency_s == 0.25
    b = PaperBroker(StaticMarketData(), None, settings=s)
    assert b.taker_latency_s == 0.25 and b.not_before(T0) == T0 + timedelta(seconds=0.25) and b.not_before(None) is None


async def test_engine_stamps_the_decision_time_and_primes_books_after_it(settings: Settings,
                                                                          fake_client: FakeKalshiClient) -> None:
    for t in ("KXT-26SEP28-A", "KXT-26SEP28-B"):
        standard_market(fake_client, t, close_time=datetime.now(UTC) + timedelta(days=1))
    settings.paper.taker_latency_s = 0.05
    svc = build_services(settings, client=fake_client, strategies={"scripted": ScriptedStrategy},
                         feeds=FeedRegistry())
    seen: list[Any] = []
    po = svc.broker.place_order

    async def spy(i: Any, **kw: Any) -> Any:
        seen.append(kw.get("decided_at"))
        return await po(i, **kw)

    svc.broker.place_order = spy  # type: ignore[method-assign]
    try:
        svc.engine.update_strategy("scripted", enabled=True)
        svc.engine.runtimes["scripted"].instance.queue = [[
            OrderIntent(ticker="KXT-26SEP28-A", side="yes", count=1, limit_price=D("0.45"), reason="x"),
            OrderIntent(ticker="KXT-26SEP28-B", side="yes", count=1, limit_price=D("0.45"), reason="x")]]
        before = datetime.now(UTC)
        await svc.engine.tick()
        assert len(seen) == 2 and all(d is not None and d >= before for d in seen)
        books = [b for (t, b) in svc.md._books.items()]
        assert books and all(ob.ts >= seen[0] + timedelta(seconds=0.05) for _, ob in books)
        assert all(o.status == "filled" for o in svc.store.list_orders("all"))
    finally:
        await svc.aclose()


# --------------------------------------------------------------------------- maker crossing (finding 10)


async def _maker_scenario(first_pass_polls: int | None) -> tuple[Order, tuple[int, Any]]:
    clk = ManualClock(T0)
    md = StaticMarketData([make_market(T, close_time=T0 + timedelta(days=2), yes_bid="0.90", yes_ask="0.92")],
                          clock=clk)
    md.set_series("KXWTIW")
    md.set_book(T, yes_bids=[("0.90", 500)], no_bids=[("0.08", 100)])
    b = PaperBroker(md, None, starting_balance=1000, clock=clk)
    o = await b.place_order(intent(count=11, tif="gtc", expires_in_s=3600, strategy="maker_favorite"))
    assert o.queue_ahead == 500
    clk.advance(10)
    md.add_trade(T, "0.90", 400, clk(), taker_side="no")  # sold into the 500 bids ahead of us
    clk.advance(3)  # the other 100 ahead cancel; a seller offers 5 @ 0.90 (crosses our bid)
    md.set_book(T, yes_bids=[("0.89", 50)], no_bids=[("0.10", 5)])
    clk.advance(2)
    await b.process_resting_orders(max_trade_polls=first_pass_polls)
    after_first = (o.filled_count, o.queue_ahead)
    clk.advance(15)
    await b.process_resting_orders()  # reads the tape
    return o, after_first


async def test_unpolled_pass_defers_crossing_and_queue_bound() -> None:
    o, first = await _maker_scenario(first_pass_polls=0)  # the first pass skipped the tape (poll cap)
    assert first == (0, D(500))  # nothing applied before the prints (was: filled 5, queue_ahead 0)
    assert o.filled_count == 5  # the 400 print went to the queue ahead; only the 5 offered cross us (was 11)
    control, _ = await _maker_scenario(first_pass_polls=None)  # tape read on the first pass
    assert control.filled_count == 5


# --------------------------------------------------------------------------- legged baskets (finding 11)


async def test_would_have_legged_baskets_are_counted_and_logged() -> None:
    clk = ManualClock(T0)
    md = StaticMarketData([make_market(t, close_time=T0 + timedelta(days=1), yes_bid="0.40", yes_ask="0.45")
                           for t in ("KXE-26-A", "KXE-26-B")], clock=clk)
    md.set_series("KXE")
    md.set_book("KXE-26-A", yes_bids=[("0.40", 100)], no_bids=[("0.55", 100)])
    md.set_book("KXE-26-B", yes_bids=[("0.40", 3)], no_bids=[("0.55", 100)])  # only 3 NO @ 0.60
    logged: list[str] = []
    b = PaperBroker(md, None, starting_balance=1000, clock=clk)
    b._log = lambda level, kind, msg, **d: logged.append(kind)  # type: ignore[method-assign]
    legs = [intent("KXE-26-A", "no", 5, "0.60", group_id="g", strategy="no_basket_arb"),
            intent("KXE-26-B", "no", 5, "0.60", group_id="g", strategy="no_basket_arb")]
    orders = await b.place_basket(legs, all_or_none=True)
    assert all(o.status == "rejected" for o in orders)
    assert b.legged_baskets == {"no_basket_arb": 1} and "basket_legged" in logged
    assert b.strategy_stats()["no_basket_arb"]["legged_baskets"] == 1


# --------------------------------------------------------------------------- per-strategy order rate (8, 19)


def rm(**limits: Any) -> RiskManager:
    loose = dict(max_position_cost_per_market=10**6, max_exposure_per_event=10**6, max_total_exposure_pct=100,
                 max_strategy_allocation_pct=100, min_cash_reserve=0, max_orders_per_minute=0, daily_loss_limit=0,
                 min_seconds_to_close=0, max_spread=1)
    return RiskManager(RiskSettings(**(loose | limits)), clock=lambda: T0)


def pv(equity: Any = 1000, positions: tuple[Any, ...] = (), orders: tuple[Any, ...] = (), day_start: Any = None,
       daily: dict[str, Decimal] | None = None) -> PortfolioView:
    k = D(equity)
    return PortfolioView(ts=T0, starting_balance=D(1000), cash=k, reserved_cash=ZERO, equity=k, equity_mid=k,
                         realized_pnl=ZERO, unrealized_pnl=ZERO, fees_paid=ZERO,
                         day_start_equity=D(day_start) if day_start is not None else k, positions=positions,
                         open_orders=orders, strategy_daily_pnl=daily or {})


def mkt(ticker: str = T) -> Any:
    return make_market(ticker, yes_bid="0.89", yes_ask="0.90", close_time=T0 + timedelta(days=1))


def test_another_strategys_burst_cannot_use_up_btc15m_order_budget() -> None:
    r = rm(max_orders_per_minute=30)
    for _ in range(20):
        assert r.check(intent(count=1, strategy="ladder_favorite"), mkt(), pv()).approved_count == 1
    for _ in range(10):
        assert r.check(intent(count=1, strategy="maker_favorite"), mkt(), pv()).approved_count == 1
    assert r.orders_last_minute() == 30
    d = r.check(intent(count=55, strategy="btc15m_favorite"), mkt(), pv())
    assert d.approved_count == 55  # was: rejected "max_orders_per_minute reached (30)"
    for _ in range(9):
        r.check(intent(count=1, strategy="ladder_favorite"), mkt(), pv())
    assert r.check(intent(count=1, strategy="ladder_favorite"), mkt(), pv()).approved_count == 1  # its 30th
    d = r.check(intent(count=1, strategy="ladder_favorite"), mkt(), pv())
    assert d.approved_count == 0 and d.binding_limit == "max_orders_per_minute" and "ladder_favorite" in d.reason
    assert r.orders_last_minute(strategy="ladder_favorite") == 30 and r.orders_last_minute() == 41


def test_basket_dry_run_unrecords_the_right_strategy() -> None:
    r = rm(max_orders_per_minute=30)
    r.record_order(T0, "btc15m_favorite")
    ds = r.check_basket([intent("KXE-1", strategy="arb"), intent("KXE-2", strategy="arb")], [mkt("KXE-1"), mkt("KXE-2")],
                        pv())
    assert [d.approved_count for d in ds] == [10, 10]
    assert r.orders_last_minute(strategy="arb") == 0 and r.orders_last_minute(strategy="btc15m_favorite") == 1


# --------------------------------------------------------------------------- per-strategy allocation (13)


def test_strategy_allocations_default_from_the_classes_and_config() -> None:
    classes = {"btc15m_favorite": Btc15mFavorite, "ladder_favorite": LadderFavorite,
               "maker_favorite": MakerFavoriteHarvest, "no_basket_arb": NoBasketArb}
    lim = strategy_limits(Settings(), classes)
    assert {k: v.max_allocation_pct for k, v in lim.items()} == {
        "btc15m_favorite": 10, "ladder_favorite": 15, "maker_favorite": 10, "no_basket_arb": 10}
    assert {k: v.daily_loss_limit for k, v in lim.items()} == {
        "btc15m_favorite": 100, "ladder_favorite": 45, "maker_favorite": 30, "no_basket_arb": 0}
    s = Settings.model_validate({"strategies": {"ladder_favorite": {"max_allocation_pct": 5, "daily_loss_limit": 12}}})
    got = strategy_limits(s, classes)["ladder_favorite"]
    assert got == StrategyLimits(max_allocation_pct=5.0, daily_loss_limit=D(12))
    assert RiskManager(s).allocation_pct("ladder_favorite") == 5.0  # explicit config without the classes too
    assert RiskManager(s).allocation_pct("other") == 50.0  # fallback: risk.max_strategy_allocation_pct
    assert Settings().risk.max_total_exposure_pct == 60 and Settings().risk.daily_loss_limit == 150


def test_experimental_strategies_cannot_starve_the_primary() -> None:
    # defaults: 60% total, 50% fallback. The clock is pinned to T0: the market closes T0 + 1 day, so
    # against the real clock it is long closed and min_seconds_to_close rejects before the caps are tested
    r = RiskManager(Settings(), clock=lambda: T0)
    r.set_strategy_limits(strategy_limits(Settings(), {"btc15m_favorite": Btc15mFavorite,
                                                       "ladder_favorite": LadderFavorite,
                                                       "maker_favorite": MakerFavoriteHarvest}))
    ladder = tuple(Position(ticker=f"KXL-{i}", strategy="ladder_favorite", event_ticker=f"KXL-{i}", side="yes",
                            count=10, cost_basis=D("14.95")) for i in range(10))  # $149.50 of its $150
    d = r.check(intent("KXL-NEW", count=10, price="0.98", strategy="ladder_favorite"), mkt("KXL-NEW"),
                pv(positions=ladder))
    # was approved: the ladder could keep buying up to the global 50% ($500)
    assert d.approved_count == 0 and d.binding_limit == "max_strategy_allocation_pct" and "15%" in d.reason
    maker = tuple(Position(ticker=f"KXM-{i}", strategy="maker_favorite", event_ticker=f"KXM-{i}", side="yes",
                           count=11, cost_basis=D("9.90")) for i in range(10))  # $99 of its $100
    d = r.check(intent(count=55, strategy="btc15m_favorite"), mkt(), pv(positions=ladder + maker))
    assert d.approved_count == 55  # was: "max_total_exposure_pct: no headroom ($0.20 left)"
    util = {row["key"]: row for row in r.utilization(pv(positions=ladder + maker))["by_strategy"]}
    assert util["ladder_favorite"]["limit"] == 150.0 and util["ladder_favorite"]["allocation_pct"] == 15.0


async def test_engine_installs_per_strategy_limits(settings: Settings, fake_client: FakeKalshiClient) -> None:
    svc = build_services(settings, client=fake_client,
                         strategies={"btc15m_favorite": Btc15mFavorite, "ladder_favorite": LadderFavorite},
                         feeds=FeedRegistry())
    try:
        assert svc.risk.allocation_pct("btc15m_favorite") == 10 and svc.risk.allocation_pct("ladder_favorite") == 15
        rows = {r["name"]: r for r in svc.engine.strategies_json()}
        assert rows["ladder_favorite"]["experimental"] is True and rows["btc15m_favorite"]["experimental"] is False
        assert rows["ladder_favorite"]["risk_limits"]["max_allocation_pct"] == 15
    finally:
        await svc.aclose()


# --------------------------------------------------------------------------- kill switch / pauses (17)


def test_daily_loss_trip_is_released_at_the_next_utc_day_but_manual_stays() -> None:
    store = Store(":memory:")
    now = [T0]
    r = RiskManager(RiskSettings(daily_loss_limit=100, max_orders_per_minute=0), store=store, clock=lambda: now[0])
    assert r.evaluate(pv(equity=890, day_start=1000)) is True and r.kill_switch_auto
    now[0] = T0 + timedelta(hours=11)  # 23:00 same day: still on
    assert r.evaluate(pv()) is True
    r2 = RiskManager(RiskSettings(daily_loss_limit=100), store=store, clock=lambda: now[0])  # restart
    assert r2.kill_switch and r2.kill_switch_auto
    now[0] = T0 + timedelta(hours=13)  # next UTC day
    assert r2.evaluate(pv()) is False and "released" not in r2.kill_switch_reason
    assert r2.check(intent(count=1), mkt(), pv()).approved_count == 1
    r2.set_kill_switch(True, "operator")  # manual: sticky across days
    now[0] = T0 + timedelta(days=3)
    assert r2.evaluate(pv()) is True
    off = RiskManager(RiskSettings(daily_loss_limit=100, kill_switch_auto_release=False), clock=lambda: now[0])
    off.set_kill_switch(True, "daily", auto=True, now=T0)
    assert off.evaluate(pv()) is True  # auto release disabled: sticky


def test_strategy_daily_loss_pauses_only_that_strategy_until_the_next_day() -> None:
    store = Store(":memory:")
    now = [T0]
    r = RiskManager(Settings(), store=store, clock=lambda: now[0])
    r.set_strategy_limits({"ladder_favorite": StrategyLimits(15, D(45)), "btc15m_favorite": StrategyLimits(10, D(100))})
    port = pv(daily={"ladder_favorite": D("-46"), "btc15m_favorite": D("-20")})
    d = r.check(intent(count=10, price="0.98", strategy="ladder_favorite"), mkt(), port)
    assert d.approved_count == 0 and d.binding_limit == "strategy_daily_loss_limit" and "paused" in d.reason
    assert r.check(intent(count=10, strategy="btc15m_favorite"), mkt(), port).approved_count == 10
    assert not r.kill_switch  # the account is not stopped
    # the pause holds for the day even after the P&L recovers, and survives a restart
    r2 = RiskManager(Settings(), store=store, clock=lambda: now[0])
    r2.set_strategy_limits({"ladder_favorite": StrategyLimits(15, D(45))})
    assert r2.check(intent(count=10, price="0.98", strategy="ladder_favorite"), mkt(), pv()).approved_count == 0
    now[0] = T0 + timedelta(hours=13)
    assert r2.check(intent(count=10, price="0.98", strategy="ladder_favorite"), mkt(), pv()).approved_count == 10


async def test_broker_reports_each_strategys_pnl_today() -> None:
    clk = ManualClock(T0)
    md = StaticMarketData([make_market(T, close_time=T0 + timedelta(hours=2), yes_bid="0.89", yes_ask="0.90")],
                          clock=clk)
    md.set_series("KXWTIW")
    md.set_book(T, yes_bids=[("0.89", 500)], no_bids=[("0.10", 500)])
    b = PaperBroker(md, None, starting_balance=1000, clock=clk)
    assert b.portfolio().strategy_daily_pnl == {}
    await b.place_order(intent(count=10, strategy="s1"))
    daily = b.portfolio().strategy_daily_pnl
    assert daily["s1"] < 0  # the entry is marked at the 0.89 bid (and paid a fee)
    md.set_market(make_market(T, status="finalized", result="no", close_time=T0 + timedelta(hours=2)))
    clk.advance(hours=3)
    await b.settle_market(await md.market(T))
    assert b.portfolio().strategy_daily_pnl["s1"] == pytest.approx(D("-9.07"), abs=D("0.01"))
    clk.advance(days=1)  # a new UTC day starts from zero
    assert b.portfolio().strategy_daily_pnl["s1"] == 0


# --------------------------------------------------------------------------- settled feed client (12)


async def test_kalshi_settled_feed_shares_the_engines_client(settings: Settings, fake_client: FakeKalshiClient) -> None:
    svc = build_services(settings, client=fake_client, strategies={}, feeds=None)
    try:
        assert svc.feeds.kalshi_settled._client is fake_client  # counted against kalshi.max_rps
    finally:
        await svc.aclose()


async def test_arrival_book_is_not_delayed_by_slow_lookups() -> None:
    """The order arrives ``taker_latency_s`` after the decision however long the market/fee lookups
    take (a busy rate limiter must not stretch the simulated latency to seconds)."""
    import asyncio

    events: list[str] = []

    class SlowLookups(StaticMarketData):
        async def market(self, ticker: str, fresh: bool = False) -> Any:
            events.append("market_start")
            await asyncio.sleep(0.05)
            events.append("market_end")
            return await super().market(ticker, fresh)

        async def orderbook(self, ticker: str, max_age_s: float = 5) -> Orderbook:
            events.append(f"book(max_age={max_age_s:g})")
            return await super().orderbook(ticker, max_age_s)

    md = SlowLookups([make_market(T, close_time=datetime.now(UTC) + timedelta(days=2), yes_bid="0.89",
                                  yes_ask="0.90")])
    md.set_series("KXWTIW")
    md.set_book(T, yes_bids=[("0.89", 500)], no_bids=[("0.10", 500)])
    b = PaperBroker(md, None, starting_balance=1000, taker_latency_s=0.01)
    o = await b.place_order(intent(), decided_at=datetime.now(UTC))
    assert o.status == "filled"
    assert events.index("market_start") < events.index("book(max_age=2)") < events.index("market_end")
