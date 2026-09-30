"""Ladder favourites near expiry (``ladder_favorite``, research rule B4-ladder-72h): trigger conditions,
category/series exclusions, horizon cut, per-event cap, no re-entry, minimum order size."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from conftest import FakeKalshiClient, iso, raw_market

from kalshibot.api.server import build_services
from kalshibot.config import Settings
from kalshibot.feeds import FeedRegistry
from kalshibot.fees import trading_fee
from kalshibot.kalshi.client import KalshiNotFound
from kalshibot.kalshi.models import Event, Market, Orderbook, Series
from kalshibot.money import ONE, ZERO, D
from kalshibot.paper.models import Order, PortfolioView, Position
from kalshibot.strategies import REGISTRY
from kalshibot.strategies.ladder_favorite import OUTCOME_TIMING_SERIES, SERIES_RETRY, LadderFavorite

NOW = datetime(2026, 9, 27, 14, 0, tzinfo=UTC)
NAME = "ladder_favorite"


# --------------------------------------------------------------------------- fake context


def market(ticker: str, *, eet_h: float | None = 24.0, bid: Any = "0.97", ask: Any = "0.98",
           close_h: float = 24.0, status: str = "active", event: str | None = None,
           **raw: Any) -> Market:
    d = raw_market(ticker, close_time=NOW + timedelta(hours=close_h), status=status, event_ticker=event,
                   yes_bid=bid, yes_ask=ask)
    if eet_h is not None:
        d["expected_expiration_time"] = iso(NOW + timedelta(hours=eet_h))
    d.update(raw)
    return Market.from_api(d)


def book(ticker: str, bid: Any = "0.97", ask: Any = "0.98", *, depth: Any = 100,
         ask_depth: Any = None) -> Orderbook:
    yes = [(bid, depth)] if bid is not None else []
    no = [(ONE - D(ask), ask_depth if ask_depth is not None else depth)] if ask is not None else []
    return Orderbook.from_levels(ticker, yes_bids=yes, no_bids=no, ts=NOW)


def portfolio(positions: tuple[Position, ...] = (), orders: tuple[Order, ...] = ()) -> PortfolioView:
    k = D(1000)
    return PortfolioView(ts=NOW, starting_balance=k, cash=k, reserved_cash=ZERO, equity=k, equity_mid=k,
                         realized_pnl=ZERO, unrealized_pnl=ZERO, fees_paid=ZERO, day_start_equity=None,
                         positions=positions, open_orders=orders)


def position(ticker: str, cost: Any, count: int = 15, strategy: str = NAME) -> Position:
    return Position(ticker=ticker, strategy=strategy, event_ticker=ticker.rsplit("-", 1)[0], side="yes",
                    count=count, cost_basis=D(cost))


@dataclass
class FakeCtx:
    """Minimal ``StrategyContext``: markets, books, series categories, portfolio, fees, log."""

    now: datetime = NOW
    markets: dict[str, Market] = field(default_factory=dict)
    events: dict[str, Event] = field(default_factory=dict)
    portfolio: PortfolioView = field(default_factory=portfolio)
    feeds: Any = None
    books: dict[str, Orderbook] = field(default_factory=dict)
    categories: dict[str, str] = field(default_factory=dict)  # series ticker -> category
    logs: list[str] = field(default_factory=list)
    series_calls: list[str] = field(default_factory=list)
    book_calls: list[str] = field(default_factory=list)

    def add(self, m: Market, *, category: str | None = "Commodities", bk: Orderbook | None = None,
            depth: Any = 100) -> Market:
        self.markets[m.ticker] = m
        if category is not None:
            self.categories[m.series_ticker] = category
        self.books[m.ticker] = bk if bk is not None else book(m.ticker, m.yes_bid, m.yes_ask, depth=depth)
        return m

    async def series(self, series_ticker: str) -> Series:
        self.series_calls.append(series_ticker)
        if series_ticker not in self.categories:
            raise KalshiNotFound(404, "series not found", f"/series/{series_ticker}")
        return Series.from_api({"ticker": series_ticker, "title": series_ticker,
                                "category": self.categories[series_ticker], "fee_type": "quadratic",
                                "fee_multiplier": 1})

    async def orderbook(self, ticker: str) -> Orderbook:
        self.book_calls.append(ticker)
        if ticker not in self.books:
            raise KalshiNotFound(404, "market not found", ticker)
        return self.books[ticker]

    def fee(self, market: Market, price: Any, count: Any, is_taker: bool = True) -> Decimal:
        return trading_fee(D(price), D(count), is_taker=is_taker)

    def log(self, msg: str, **data: Any) -> None:
        self.logs.append(msg)


async def run(strategy: LadderFavorite, ctx: FakeCtx) -> dict[str, Any]:
    return {i.ticker: i for i in await strategy.on_tick(ctx)}


# --------------------------------------------------------------------------- metadata


def test_registered_metadata_and_defaults() -> None:
    assert REGISTRY[NAME] is LadderFavorite
    s = LadderFavorite()
    assert LadderFavorite.backtestable is True
    assert "EXPERIMENTAL" in s.description and "failed an independent holdout" in s.description
    assert "forward paper-test only" in s.description
    assert s.universe().max_days_to_close == 3 and s.universe().series_tickers == []
    p = s.params
    assert p["min_yes_bid"] == 0.97 and p["max_hours_to_expiry"] == 72
    assert sorted(p["categories"]) == ["Commodities", "Crypto", "Economics", "Financials"]
    assert p["min_contracts"] == 10 and p["max_event_cost"] == 20 and p["max_position_cost"] == 10
    assert p["max_group_cost"] == 40 and p["max_total_cost"] == 150 and p["max_intents_per_tick"] == 5
    assert p["decision_grid_s"] == 3600 and p["decision_window_s"] == 120
    assert LadderFavorite.experimental is True
    assert LadderFavorite.risk_defaults == {"max_allocation_pct": 15, "daily_loss_limit": 45}
    assert 0.98 < p["fair_value"] < 1
    assert set(LadderFavorite.default_params) == set(LadderFavorite.param_schema)
    assert "KX10YRDIRHM" in OUTCOME_TIMING_SERIES and len(OUTCOME_TIMING_SERIES) == 55


# --------------------------------------------------------------------------- trigger


async def test_trigger_buys_yes_at_the_ask_with_reason_fair_value_and_edge() -> None:
    ctx = FakeCtx()
    ctx.add(market("KXWTI-26SEP28-T70"))
    out = await run(LadderFavorite(), ctx)
    it = out["KXWTI-26SEP28-T70"]
    assert (it.side, it.action, it.tif, it.strategy) == ("yes", "buy", "ioc", NAME)
    assert it.limit_price == D("0.98")
    assert it.count == 10  # floor($10 / 0.98)
    assert it.fair_value == pytest.approx(0.993)
    # fee = ceil_cent(0.07 * 10 * 0.98 * 0.02) = 0.02 -> 0.002/contract; edge = 0.993 - 0.98 - 0.002
    assert it.expected_edge == D("0.011")
    assert "B4-ladder-72h" in it.reason and "experimental" in it.reason and "Commodities" in it.reason
    assert it.problems() == []


@pytest.mark.parametrize(
    ("bid", "ask", "fires"),
    [
        ("0.97", "0.98", True),  # boundary: bid exactly 0.97
        ("0.98", "0.99", True),
        ("0.99", "0.995", True),  # sub-cent ask is still < 1.00
        ("0.96", "0.98", False),  # bid below 0.97
        ("0.969", "0.98", False),
        ("0.97", None, False),  # no YES ask (no NO bids): one-sided
        (None, "0.98", False),  # no YES bid: one-sided
    ],
)
async def test_trigger_conditions_on_the_book(bid: Any, ask: Any, fires: bool) -> None:
    ctx = FakeCtx()
    m = market("KXGOLDD-26SEP2817-T3700", bid="0.97", ask="0.98",  # snapshot passes the pre-filter
               price_ranges=[{"start": "0.0000", "end": "0.9900", "step": "0.0100"},
                             {"start": "0.9900", "end": "1.0000", "step": "0.0010"}])
    ctx.add(m, bk=book(m.ticker, bid, ask))
    out = await run(LadderFavorite(), ctx)
    assert (m.ticker in out) is fires
    if fires:
        assert out[m.ticker].limit_price == D(ask)


async def test_snapshot_prefilter_skips_markets_without_a_favourite_quote() -> None:
    ctx = FakeCtx()
    ctx.add(market("KXWTI-26SEP28-T60", bid="0.95", ask="0.97"), bk=book("KXWTI-26SEP28-T60", "0.97", "0.98"))
    ctx.add(market("KXWTI-26SEP28-T61", bid="0.97", ask=None), bk=book("KXWTI-26SEP28-T61", "0.97", "0.98"))
    assert await run(LadderFavorite(), ctx) == {}
    assert ctx.book_calls == [] and ctx.series_calls == []  # no requests for non-candidates


async def test_closed_or_inactive_markets_are_skipped() -> None:
    ctx = FakeCtx()
    ctx.add(market("KXWTI-26SEP28-T62", status="closed"))
    ctx.add(market("KXWTI-26SEP28-T63", close_h=-0.1))
    assert await run(LadderFavorite(), ctx) == {}


# --------------------------------------------------------------------------- horizon


@pytest.mark.parametrize(
    ("eet_h", "fires"),
    [(0.5, True), (71.9, True), (72.0, False), (80.0, False), (-1.0, True), (None, False)],
)
async def test_horizon_cut_on_expected_expiration(eet_h: float | None, fires: bool) -> None:
    ctx = FakeCtx()
    m = ctx.add(market("KXINXU-26SEP30H1600-T6500", eet_h=eet_h, close_h=max(eet_h or 24, 1)))
    out = await run(LadderFavorite(), ctx)
    assert (m.ticker in out) is fires


async def test_horizon_param_narrows_the_window() -> None:
    ctx = FakeCtx()
    ctx.add(market("KXINXU-26SEP30H1600-T6500", eet_h=30))
    assert await run(LadderFavorite({"max_hours_to_expiry": 24}), ctx) == {}
    assert len(await run(LadderFavorite({"max_hours_to_expiry": 48}), ctx)) == 1


# --------------------------------------------------------------------------- category / series


@pytest.mark.parametrize(
    ("category", "fires"),
    [("Commodities", True), ("Financials", True), ("Crypto", True), ("Economics", True),
     ("economics", True), ("Sports", False), ("Politics", False), ("Climate and Weather", False)],
)
async def test_category_filter_uses_the_series_category(category: str, fires: bool) -> None:
    ctx = FakeCtx()
    m = ctx.add(market("KXSER-26SEP28-T1"), category=category)
    out = await run(LadderFavorite(), ctx)
    assert (m.ticker in out) is fires
    assert ctx.series_calls == ["KXSER"]


async def test_event_category_is_the_fallback_and_failed_lookups_back_off() -> None:
    ctx = FakeCtx()
    m = ctx.add(market("KXNEW-26SEP28-T1"), category=None)  # series lookup fails (404)
    s = LadderFavorite({"decision_grid_s": 0})  # every tick (this test is about the category lookups)
    assert await run(s, ctx) == {}
    assert ctx.series_calls == ["KXNEW"]
    assert await run(s, ctx) == {}
    assert ctx.series_calls == ["KXNEW"]  # not retried within SERIES_RETRY
    ctx.events[m.event_ticker] = Event.from_api({"event_ticker": m.event_ticker, "series_ticker": "KXNEW",
                                                 "category": "Financials"})
    assert m.ticker in await run(s, ctx)  # the cached event's category is used
    ctx2 = FakeCtx(now=NOW + SERIES_RETRY)
    ctx2.add(market("KXNEW-26SEP28-T2"), category="Crypto")
    s._category_fail["KXNEW"] = NOW
    assert "KXNEW-26SEP28-T2" in await run(s, ctx2)  # retried after the back-off
    assert ctx2.series_calls == ["KXNEW"]


async def test_series_category_is_cached() -> None:
    ctx = FakeCtx()
    ctx.add(market("KXWTI-26SEP28-T70"))
    ctx.add(market("KXWTI-26SEP28-T71", event="KXWTI-26SEP29"))
    s = LadderFavorite()
    await run(s, ctx)
    ctx.add(market("KXWTI-26SEP28-T72", event="KXWTI-26SEP30"))
    await run(s, ctx)
    assert ctx.series_calls == ["KXWTI"]


@pytest.mark.parametrize(
    ("ticker", "raw", "params"),
    [
        ("KX10YRDIRHM-26SEP28-T4", {}, {}),  # outcome-timing dependent (series_flags.csv), Financials
        ("KXTRUMPMENTION-26SEP28-X", {}, {"categories": ["Mentions"]}),  # stays excluded in any category
        ("KXMVECROSSCATEGORY-S2026-ABC", {}, {}),  # MVE combo by ticker
        ("KXCOMBO-26SEP28-X", {"mve_collection_ticker": "KXMVECOLL"}, {}),  # MVE by field
        ("KXWTI-26SEP28-T70", {}, {"exclude_series": ["kxwti"]}),  # user exclusion
    ],
)
async def test_series_exclusions(ticker: str, raw: dict[str, Any], params: dict[str, Any]) -> None:
    ctx = FakeCtx()
    m = market(ticker, **raw)
    ctx.add(m, category=(params.get("categories") or ["Financials"])[0])
    assert await run(LadderFavorite(params), ctx) == {}
    assert ctx.book_calls == []


# --------------------------------------------------------------------------- per-event cap


async def test_per_event_cap_limits_a_ladder() -> None:
    ctx = FakeCtx()
    ev = "KXGOLDD-26SEP2817"
    for k in range(4):
        ctx.add(market(f"{ev}-T37{k}0", event=ev, bid="0.97", ask="0.98"))
    ctx.add(market("KXWTI-26SEP28-T70"))  # another event is unaffected
    out = await run(LadderFavorite(), ctx)
    ladder = [i for t, i in out.items() if t.startswith(ev)]
    assert len(ladder) == 2 and "KXWTI-26SEP28-T70" in out
    cost = sum(i.count * i.limit_price for i in ladder)
    assert cost == D("19.60") and cost <= 20
    assert any("event cap" in msg for msg in ctx.logs)


async def test_event_cap_shrinks_the_last_order() -> None:
    ctx = FakeCtx()
    ev = "KXGOLDD-26SEP2817"
    for k in range(3):
        ctx.add(market(f"{ev}-T37{k}0", event=ev, bid="0.97", ask="0.98"))
    out = await run(LadderFavorite({"max_event_cost": 25, "max_position_cost": 15}), ctx)
    assert sorted(i.count for i in out.values()) == [10, 15]  # $14.70, then floor($10.30 / 0.98) = 10


async def test_event_cap_counts_existing_exposure() -> None:
    ev = "KXGOLDD-26SEP2817"
    ctx = FakeCtx(portfolio=portfolio((position(f"{ev}-T3600", "15.00", count=15),)))
    ctx.add(market(f"{ev}-T3700", event=ev))
    assert await run(LadderFavorite(), ctx) == {}  # $5 left < 10 contracts
    assert ctx.book_calls == []  # decided before fetching the book
    # another strategy's position in the event does not use up this strategy's cap
    ctx2 = FakeCtx(portfolio=portfolio((position(f"{ev}-T3600", "15.00", count=15, strategy="other"),)))
    ctx2.add(market(f"{ev}-T3700", event=ev))
    assert len(await run(LadderFavorite(), ctx2)) == 1


# --------------------------------------------------------------------------- no re-entry


async def test_one_entry_per_market_ever() -> None:
    ctx = FakeCtx()
    ctx.add(market("KXWTI-26SEP28-T70"))
    s = LadderFavorite()
    assert len(await run(s, ctx)) == 1
    # the IOC did not fill (portfolio still empty) and the trigger still holds: no second attempt
    assert await run(s, ctx) == {}
    assert s.has_entered("KXWTI-26SEP28-T70")


async def test_state_round_trip_prevents_reentry_after_restart() -> None:
    ctx = FakeCtx()
    ctx.add(market("KXWTI-26SEP28-T70"))
    s = LadderFavorite()
    await run(s, ctx)
    state = s.dump_state()
    assert state == {"entered": {"KXWTI-26SEP28-T70": NOW.isoformat()}}
    assert s.dump_state() is not state  # fresh object: the engine compares states to decide saving
    s2 = LadderFavorite()
    s2.load_state(state)
    assert await run(s2, ctx) == {}
    s3 = LadderFavorite()
    s3.load_state({"entered": {"X": "garbage", "": NOW.isoformat()}})  # tolerated
    s3.load_state(None)
    assert len(await run(s3, ctx)) == 1


async def test_held_or_pending_markets_are_not_reentered_without_state() -> None:
    ctx = FakeCtx(portfolio=portfolio((position("KXWTI-26SEP28-T70", "14.70"),)))
    ctx.add(market("KXWTI-26SEP28-T70"))
    s = LadderFavorite()
    assert await run(s, ctx) == {}
    assert s.has_entered("KXWTI-26SEP28-T70")
    order = Order(id=1, ticker="KXWTI-26SEP28-T71", side="yes", action="buy", count=10,
                  limit_price=D("0.97"), tif="gtc", strategy=NAME, event_ticker="KXWTI-26SEP28")
    ctx2 = FakeCtx(portfolio=portfolio(orders=(order,)))
    ctx2.add(market("KXWTI-26SEP28-T71"))
    assert await run(LadderFavorite(), ctx2) == {}


async def test_old_entries_are_pruned_once_the_market_is_gone() -> None:
    s = LadderFavorite()
    s.load_state({"entered": {"OLD-1": (NOW - timedelta(days=30)).isoformat(),
                              "RECENT-1": (NOW - timedelta(days=1)).isoformat()}})
    await run(s, FakeCtx())
    assert not s.has_entered("OLD-1") and s.has_entered("RECENT-1")


# --------------------------------------------------------------------------- size


async def test_thin_ask_waits_without_using_up_the_market() -> None:
    ctx = FakeCtx()
    m = ctx.add(market("KXWTI-26SEP28-T70"), depth=5)
    s = LadderFavorite()
    assert await run(s, ctx) == {}
    assert not s.has_entered(m.ticker)
    assert any("only 5 contracts" in msg for msg in ctx.logs)
    ctx.books[m.ticker] = book(m.ticker, depth=12)
    assert await run(s, ctx) == {}  # same decision slot: waits for the next hour boundary
    ctx.now = NOW + timedelta(hours=1)
    out = await run(s, ctx)
    assert out[m.ticker].count == 10  # the next boundary; limited by the $10 position cap


@pytest.mark.parametrize(("cap", "count"), [(5.0, None), (9.7, None), (9.8, 10), (15.0, 15), (20.0, 20)])
async def test_min_order_size(cap: float, count: int | None) -> None:
    ctx = FakeCtx()
    ctx.add(market("KXWTI-26SEP28-T70"))
    out = await run(LadderFavorite({"max_position_cost": cap}), ctx)
    if count is None:
        assert out == {}
        assert any("min_contracts" in msg for msg in ctx.logs)
    else:
        assert out["KXWTI-26SEP28-T70"].count == count


async def test_max_intents_per_tick_defers_the_rest() -> None:
    ctx = FakeCtx()
    for k in range(3):
        ctx.add(market(f"KXWTI-26SEP2{k}-T70"))
    s = LadderFavorite({"max_intents_per_tick": 2})
    assert len(await run(s, ctx)) == 2
    assert len(await run(s, ctx)) == 1
    assert await run(s, ctx) == {}


async def test_uses_the_batch_orderbooks_call_when_available() -> None:
    class BatchCtx(FakeCtx):
        batch_calls: list[list[str]] = []

        async def orderbooks(self, tickers: list[str]) -> dict[str, Orderbook]:
            self.batch_calls.append(list(tickers))
            return {t: self.books[t] for t in tickers}

    ctx = BatchCtx()
    ctx.add(market("KXWTI-26SEP28-T70"))
    ctx.add(market("KXWTI-26SEP29-T70"))
    assert len(await run(LadderFavorite(), ctx)) == 2
    assert ctx.batch_calls == [["KXWTI-26SEP28-T70", "KXWTI-26SEP29-T70"]] and ctx.book_calls == []


# --------------------------------------------------------------------------- engine end to end


async def test_engine_runs_the_strategy_through_risk_and_the_paper_broker(tmp_path: Any) -> None:
    settings = Settings()
    settings.storage.path = str(tmp_path / "t.sqlite3")
    settings.engine.autostart = False
    fc = FakeKalshiClient()
    now = datetime.now(UTC)
    t = "KXWTI-26SEP28-T70"
    fc.add_market(t, close_time=now + timedelta(hours=20), yes_bid="0.97", yes_ask="0.98")
    fc.update_market(t, expected_expiration_time=iso(now + timedelta(hours=20)))
    fc.set_book(t, yes=[("0.97", 100)], no=[("0.02", 100)])
    fc.set_series("KXWTI", category="Commodities")
    fc.add_market("KXSPORT-26SEP28-A", close_time=now + timedelta(hours=20), yes_bid="0.97", yes_ask="0.98")
    fc.update_market("KXSPORT-26SEP28-A", expected_expiration_time=iso(now + timedelta(hours=20)))
    fc.set_book("KXSPORT-26SEP28-A", yes=[("0.97", 100)], no=[("0.02", 100)])
    fc.set_series("KXSPORT", category="Sports")
    svc = build_services(settings, client=fc, strategies={NAME: LadderFavorite}, feeds=FeedRegistry())
    svc.md.scanner_days_to_close = 0
    try:
        # continuous evaluation: the wall clock is not on the hourly decision grid
        svc.engine.update_strategy(NAME, enabled=True, params={"decision_grid_s": 0})
        await svc.md.refresh_universe(force=True)
        await svc.engine.tick()
        orders = svc.store.list_orders("all")
        assert len(orders) == 1 and orders[0].ticker == t and orders[0].status == "filled"
        assert orders[0].filled_count == 10 and orders[0].avg_fill_price == D("0.98")
        pos = svc.broker.position(t, NAME)
        assert pos is not None and pos.count == 10 and pos.side == "yes"
        sig = svc.store.list_signals()
        assert len(sig) == 1 and sig[0]["fair_value"] == pytest.approx(0.993)
        assert sig[0]["expected_edge"] == D("0.011") and "B4-ladder-72h" in sig[0]["reason"]
        await svc.engine.tick()  # holds it now (and it is recorded as entered): no second signal
        assert len(svc.store.list_signals()) == 1
        assert svc.store.get_strategy_state(NAME)["state"]["entered"].keys() == {t}
    finally:
        await svc.aclose()
