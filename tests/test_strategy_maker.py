"""Maker favourite harvest (``kalshibot/strategies/maker_favorite.py``) with fake contexts,
plus end-to-end passes through the real engine, risk manager and paper broker."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from conftest import FakeKalshiClient, raw_market

from kalshibot.api.server import AppServices, build_services
from kalshibot.config import Settings
from kalshibot.engine import EngineContext
from kalshibot.feeds import FeedRegistry
from kalshibot.fees import resolve_fee_params, trading_fee
from kalshibot.kalshi.models import Event, Market, Orderbook, Series
from kalshibot.money import ZERO, D
from kalshibot.paper.models import Order, PortfolioView, Position
from kalshibot.strategies import REGISTRY
from kalshibot.strategies.base import CancelIntent, OrderIntent
from kalshibot.strategies.maker_favorite import (
    BOOK_COOLDOWN_S,
    META_RETRY_S,
    NO_CANCEL_EXPIRY_S,
    OUTCOME_TIMING_SERIES,
    RETRY_S,
    MakerFavoriteHarvest,
    pick_favourite,
)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
NAME = "maker_favorite"


# --------------------------------------------------------------------------- fakes


def market(ticker: str, *, yes_bid: Any = "0.90", yes_ask: Any = "0.92", close_in: timedelta = timedelta(days=2),
           vol: Any = 1000, **extra: Any) -> Market:
    d = raw_market(ticker, close_time=NOW + close_in, yes_bid=yes_bid, yes_ask=yes_ask, volume_24h=vol)
    d.update(extra)
    return Market.from_api(d)


def book_for(m: Market, depth: Any = 500) -> Orderbook:
    yes = [(m.yes_bid, depth)] if m.yes_bid is not None else []
    no = [(m.no_bid, depth)] if m.no_bid is not None else []
    return Orderbook.from_levels(m.ticker, yes_bids=yes, no_bids=no, ts=NOW)


def book(ticker: str, yes: Any = None, no: Any = None, depth: Any = 500) -> Orderbook:
    return Orderbook.from_levels(ticker, yes_bids=[(yes, depth)] if yes else [],
                                 no_bids=[(no, depth)] if no else [], ts=NOW)


def series(ticker: str, *, category: str = "Economics", fee_type: str = "quadratic") -> Series:
    return Series.from_api({"ticker": ticker, "title": ticker, "category": category, "fee_type": fee_type,
                            "fee_multiplier": 1})


def event(event_ticker: str, *, category: str = "Economics", fee_type_override: str | None = None) -> Event:
    d: dict[str, Any] = {"event_ticker": event_ticker, "series_ticker": event_ticker.split("-")[0],
                         "category": category}
    if fee_type_override:
        d["fee_type_override"] = fee_type_override
    return Event.from_api(d)


def portfolio(*, orders: tuple[Order, ...] = (), positions: tuple[Position, ...] = ()) -> PortfolioView:
    k = D(1000)
    return PortfolioView(ts=NOW, starting_balance=k, cash=k, reserved_cash=ZERO, equity=k, equity_mid=k,
                         realized_pnl=ZERO, unrealized_pnl=ZERO, fees_paid=ZERO, day_start_equity=k,
                         positions=positions, open_orders=orders)


def resting(oid: int, m: Market, *, side: str = "yes", price: Any = "0.90", strategy: str = NAME,
            count: int = 11, filled: int = 0) -> Order:
    return Order(id=oid, ticker=m.ticker, side=side, action="buy", count=count, limit_price=D(price),  # type: ignore[arg-type]
                 tif="gtc", strategy=strategy, created_at=NOW - timedelta(minutes=5), filled_count=filled,
                 status="partially_filled" if filled else "open", expires_at=NOW + timedelta(minutes=10),
                 event_ticker=m.event_ticker)


def held(m: Market, *, strategy: str = NAME) -> Position:
    return Position(ticker=m.ticker, strategy=strategy, event_ticker=m.event_ticker, side="yes", count=11,
                    cost_basis=D("9.90"))


class MinimalCtx:
    """Only the ARCHITECTURE.md section 7 protocol (no ``event``/``orderbooks``/``cancel`` extras)."""

    def __init__(self, markets: list[Market], *, series_map: dict[str, Series] | None = None,
                 books: dict[str, Orderbook] | None = None, cached_events: dict[str, Event] | None = None,
                 pf: PortfolioView | None = None, now: datetime = NOW) -> None:
        self.now = now
        self.markets = {m.ticker: m for m in markets}
        self.events: dict[str, Event] = dict(cached_events or {})
        self.portfolio = pf or portfolio()
        self.feeds = FeedRegistry()
        self.series_map = dict(series_map) if series_map is not None else {
            m.series_ticker: series(m.series_ticker) for m in markets}
        self.books = dict(books) if books is not None else {m.ticker: book_for(m) for m in markets}
        self.calls: Counter[str] = Counter()
        self.book_tickers: list[str] = []
        self.logs: list[tuple[str, dict[str, Any]]] = []

    async def series(self, series_ticker: str) -> Series:
        self.calls["series"] += 1
        if series_ticker not in self.series_map:
            raise KeyError(series_ticker)
        return self.series_map[series_ticker]

    async def orderbook(self, ticker: str) -> Orderbook:
        self.calls["orderbook"] += 1
        self.book_tickers.append(ticker)
        return self.books[ticker]

    def _event_for_fees(self, m: Market) -> Event | None:
        return self.events.get(m.event_ticker)

    def fee(self, market: Market, price: Any, count: Any, is_taker: bool = True) -> Decimal:
        ft, mult = resolve_fee_params(self.series_map.get(market.series_ticker), self._event_for_fees(market))
        return trading_fee(D(price), D(count), is_taker=is_taker, fee_type=ft, fee_multiplier=mult)

    def log(self, msg: str, **data: Any) -> None:
        self.logs.append((msg, data))


class FakeCtx(MinimalCtx):
    """Like the engine's context: lazily fetched events, batched books and cancel support."""

    def __init__(self, markets: list[Market], *, events: dict[str, Event] | None = None, **kw: Any) -> None:
        super().__init__(markets, **kw)
        self.remote_events = dict(events) if events is not None else {
            m.event_ticker: event(m.event_ticker) for m in markets}

    async def event(self, event_ticker: str) -> Event:
        self.calls["event"] += 1
        if event_ticker not in self.remote_events:
            raise KeyError(event_ticker)
        ev = self.remote_events[event_ticker]
        self.events[event_ticker] = ev  # like the market-data cache behind ctx.events
        return ev

    async def orderbooks(self, tickers: list[str]) -> dict[str, Orderbook]:
        self.calls["orderbooks"] += 1
        self.book_tickers.extend(tickers)
        return {t: self.books[t] for t in tickers if t in self.books}

    def cancel(self, order_id: int | None = None, *, ticker: str | None = None, reason: str = "") -> None:
        raise AssertionError("the strategy returns CancelIntent items instead")

    def _event_for_fees(self, m: Market) -> Event | None:
        return self.events.get(m.event_ticker) or self.remote_events.get(m.event_ticker)


class NoCancelCtx(FakeCtx):
    """An engine context without cancel support."""

    cancel = None  # type: ignore[assignment]


async def run(strat: MakerFavoriteHarvest, ctx: MinimalCtx) -> list[Any]:
    out = await strat.on_tick(ctx)  # type: ignore[arg-type]
    for i in out:
        assert i.problems() == [], i.problems()
        assert i.strategy == NAME
    return out


def entries(out: list[Any]) -> list[OrderIntent]:
    return [i for i in out if isinstance(i, OrderIntent) and i.replaces is None]


# --------------------------------------------------------------------------- declaration


def test_registered_experimental_and_not_backtestable() -> None:
    assert REGISTRY[NAME] is MakerFavoriteHarvest
    assert MakerFavoriteHarvest.backtestable is False
    assert MakerFavoriteHarvest.experimental is True
    assert MakerFavoriteHarvest.description.startswith("EXPERIMENTAL")
    s = MakerFavoriteHarvest()
    assert s.universe().max_days_to_close == 3 and s.universe().series_tickers == []
    # the defaults satisfy their own schema (the UI edits them through it)
    assert MakerFavoriteHarvest.resolve_params(MakerFavoriteHarvest.default_params, strict=True) == s.params
    assert set(MakerFavoriteHarvest.param_schema) == set(MakerFavoriteHarvest.default_params)
    assert s.params["max_resting_orders"] == 8 and s.params["order_dollars"] == 10
    assert s.params["expires_in_s"] == 3600 and s.params["max_open_cost"] == 100
    assert s.params["prior_edge"] == 0.0  # no optimistic prior: the repo's own data is negative in this band
    assert s.params["exclude_categories"] == ["Sports", "Crypto", "Mentions"]
    assert MakerFavoriteHarvest({"max_days_to_close": 7}).universe().max_days_to_close == 7


def test_outcome_timing_series_match_the_research_flags() -> None:
    assert len(OUTCOME_TIMING_SERIES) == 55
    assert {"KXTRUMPMENTION", "KXPGATOP10", "KXPRIMARYMOV", "KXCLAUDE"} <= OUTCOME_TIMING_SERIES


@pytest.mark.parametrize(("yb", "nb", "want"), [
    ("0.90", "0.08", ("yes", "0.90")),
    ("0.85", "0.13", ("yes", "0.85")),
    ("0.96", "0.02", ("yes", "0.96")),
    ("0.05", "0.93", ("no", "0.93")),
    ("0.97", "0.01", None),  # above max_bid
    ("0.84", "0.14", None),  # below min_bid
    ("0.88", "0.08", None),  # spread 4c
    ("0.90", "0.10", None),  # locked
    ("0.90", None, None),  # one-sided
    (None, "0.90", None),
])
def test_pick_favourite(yb: str | None, nb: str | None, want: tuple[str, str] | None) -> None:
    q, why = pick_favourite(D(yb) if yb else None, D(nb) if nb else None, min_bid=D("0.85"), max_bid=D("0.96"),
                            max_spread=D("0.03"))
    if want is None:
        assert q is None and why
    else:
        assert q is not None and (q.side, q.bid) == (want[0], D(want[1])) and why == ""


# --------------------------------------------------------------------------- entries


async def test_joins_yes_favourite_at_best_bid() -> None:
    m = market("KXGAS-26OCT01-T3", yes_bid="0.90", yes_ask="0.92", vol=5400)
    bk = Orderbook.from_levels(m.ticker, yes_bids=[("0.90", 500), ("0.89", 100)], no_bids=[("0.08", 300)], ts=NOW)
    ctx = FakeCtx([m], books={m.ticker: bk})
    [i] = await run(MakerFavoriteHarvest({"prior_edge": 0.005}), ctx)
    assert (i.ticker, i.side, i.action, i.tif, i.replaces) == (m.ticker, "yes", "buy", "gtc", None)
    assert i.limit_price == D("0.90") and i.count == 11  # floor(10 / 0.90)
    assert i.expires_in_s == 3600
    assert i.expected_edge == D("0.005") and i.fair_value == pytest.approx(0.905)
    for s in ("EXPERIMENTAL", "PRIOR", "YES best bid 0.90", "500 ahead", "quadratic", "spread 2c", "+0.5c"):
        assert s in i.reason, s
    assert ctx.calls == Counter({"series": 1, "event": 1, "orderbooks": 1})
    # without cancel support the lifetime is capped so quotes refresh anyway
    [j] = await run(MakerFavoriteHarvest(), NoCancelCtx([m], books={m.ticker: bk}))
    assert j.expires_in_s == NO_CANCEL_EXPIRY_S == 900


async def test_joins_no_favourite() -> None:
    m = market("KXGAS-26OCT01-T9", yes_bid="0.05", yes_ask="0.07")
    [i] = await run(MakerFavoriteHarvest({"prior_edge": 0.005}), FakeCtx([m]))
    assert i.side == "no" and i.limit_price == D("0.93") and i.count == 10  # floor(10 / 0.93)
    assert i.fair_value == pytest.approx(0.935)


@pytest.mark.parametrize(("kw", "why"), [
    ({"yes_bid": "0.97", "yes_ask": "0.98"}, "bid above 0.96"),
    ({"yes_bid": "0.84", "yes_ask": "0.86"}, "bid below 0.85"),
    ({"yes_bid": "0.88", "yes_ask": "0.92"}, "spread 4c"),
    ({"yes_bid": "0.90", "yes_ask": None}, "one-sided"),
    ({"close_in": timedelta(hours=5)}, "closes within 6 h"),
    ({"close_in": timedelta(days=8)}, "closes after 7 d"),
    ({"vol": 5}, "no 24 h volume"),
    ({"status": "initialized"}, "not active"),
    ({"mve_collection_ticker": "KXMVESPORTS"}, "MVE combo"),
])
async def test_snapshot_filters(kw: dict[str, Any], why: str) -> None:
    m = market("KXGAS-26OCT01-T3", **kw)
    ctx = FakeCtx([m])
    assert await run(MakerFavoriteHarvest(), ctx) == [], why
    assert ctx.calls == Counter(), why  # rejected before any lookup


async def test_series_level_exclusions_need_no_lookup() -> None:
    a = market("KXTRUMPMENTION-26OCT01-X")  # outcome-timing dependent (research flag)
    b = market("KXMVECROSS-26OCT01-X")  # combo
    c = market("KXCUSTOM-26OCT01-X")
    ctx = FakeCtx([a, b, c])
    assert await run(MakerFavoriteHarvest({"exclude_series": ["kxcustom"]}), ctx) == []
    assert ctx.calls == Counter()


@pytest.mark.parametrize(("ser", "ev", "ok"), [
    ({"category": "Sports"}, {}, False),
    ({"category": "sports"}, {}, False),
    ({"fee_type": "quadratic_with_maker_fees"}, {}, False),
    ({"fee_type": "quadratic_with_combo_maker_fees"}, {}, False),
    ({"fee_type": "flat"}, {}, False),
    ({}, {"fee_type_override": "quadratic_with_maker_fees"}, False),  # event override beats the series
    ({"fee_type": "quadratic_with_maker_fees"}, {"fee_type_override": "quadratic"}, True),
    ({"category": ""}, {"category": "Sports"}, False),  # series category missing: the event's decides
    ({"category": ""}, {"category": ""}, False),  # unknown category
    ({}, {}, True),
])
async def test_fee_type_and_category(ser: dict[str, Any], ev: dict[str, Any], ok: bool) -> None:
    m = market("KXGAS-26OCT01-T3")
    ctx = FakeCtx([m], series_map={"KXGAS": series("KXGAS", **ser)},
                  events={m.event_ticker: event(m.event_ticker, **ev)})
    out = await run(MakerFavoriteHarvest(), ctx)
    assert bool(out) is ok
    if not ok:
        assert "orderbooks" not in ctx.calls  # no book fetched for a rejected series


async def test_nonzero_maker_fee_from_ctx_fee_is_rejected() -> None:
    class ScheduledFeeCtx(FakeCtx):  # e.g. a scheduled fee change the series object does not show yet
        def fee(self, market: Market, price: Any, count: Any, is_taker: bool = True) -> Decimal:
            return trading_fee(D(price), D(count), is_taker=is_taker, fee_type="quadratic_with_maker_fees")

    m = market("KXGAS-26OCT01-T3")
    strat = MakerFavoriteHarvest()
    assert await run(strat, ScheduledFeeCtx([m])) == []
    assert strat.last_scan["skipped"]["maker fee is not zero"] == 1


async def test_hard_cap_and_volume_priority() -> None:
    ms = [market(f"KXS{i:02d}-26OCT01-A", vol=100 * (i + 1)) for i in range(20)]  # 20 series/events
    busy = [market(f"KXB{i}-26OCT01-A", vol=10**6) for i in range(3)]
    pf = portfolio(orders=tuple(resting(i + 1, m) for i, m in enumerate(busy)))
    cached = {m.event_ticker: event(m.event_ticker) for m in ms}
    ctx = FakeCtx(ms + busy, pf=pf, cached_events=cached)
    out = await run(MakerFavoriteHarvest({"max_resting_orders": 15, "max_open_cost": 1000}), ctx)
    assert len(out) == 12  # 15 - 3 resting (which are within 1c: kept, no action)
    assert [i.ticker for i in out] == [m.ticker for m in sorted(ms, key=lambda m: -m.volume_24h)[:12]]
    assert not {i.ticker for i in out} & {m.ticker for m in busy}  # never a second order in a market
    assert ctx.calls["orderbooks"] == 1 and len(ctx.book_tickers) == 3 + 20  # one batch: resting + candidates


async def test_cold_start_lookups_are_budgeted() -> None:
    ms = [market(f"KXS{i:02d}-26OCT01-A", vol=100 * (i + 1)) for i in range(20)]
    strat = MakerFavoriteHarvest({"max_resting_orders": 15, "max_open_cost": 1000})
    ctx = FakeCtx(ms)  # nothing cached: a series and an event lookup per market
    out = await run(strat, ctx)
    assert len(out) == 10 and ctx.calls["series"] + ctx.calls["event"] == 20
    assert strat.last_scan["skipped"]["lookup budget spent"] == 10


async def test_no_free_slot_and_nothing_to_maintain_means_no_requests() -> None:
    ms = [market(f"KXS{i:02d}-26OCT01-A") for i in range(16)]
    pf = portfolio(orders=tuple(resting(i + 1, m) for i, m in enumerate(ms[:15])))
    ctx = NoCancelCtx(ms, pf=pf)
    assert await run(MakerFavoriteHarvest(), ctx) == []
    assert ctx.calls == Counter()
    ctx2 = FakeCtx(ms)
    assert await run(MakerFavoriteHarvest({"max_resting_orders": 0}), ctx2) == [] and ctx2.calls == Counter()
    # with cancel support the resting orders are still re-checked (one batched book request)
    ctx3 = FakeCtx(ms, pf=pf)
    assert await run(MakerFavoriteHarvest(), ctx3) == []
    assert ctx3.calls == Counter({"orderbooks": 1}) and len(ctx3.book_tickers) == 15


async def test_one_position_per_market() -> None:
    a, b = market("KXA-26OCT01-A"), market("KXB-26OCT01-A")
    pf = portfolio(positions=(held(a), held(b, strategy="other")))
    out = await run(MakerFavoriteHarvest(), FakeCtx([a, b], pf=pf))
    assert [i.ticker for i in out] == [b.ticker]  # another strategy's position does not count


async def test_markets_per_event_cap() -> None:
    ms = [market(f"KXLAD-26OCT01-T{i}", vol=100 + i) for i in range(4)]  # one event
    out = await run(MakerFavoriteHarvest(), FakeCtx(ms))
    assert len(out) == 2
    pf = portfolio(positions=(held(ms[0]),))
    out = await run(MakerFavoriteHarvest(), FakeCtx(ms, pf=pf))
    assert len(out) == 1 and out[0].ticker != ms[0].ticker
    out = await run(MakerFavoriteHarvest({"max_markets_per_event": 4}), FakeCtx(ms))
    assert len(out) == 4


async def test_expiry_is_capped_before_the_close_window() -> None:
    a = market("KXA-26OCT01-A", close_in=timedelta(hours=6, minutes=10))
    b = market("KXB-26OCT01-A", close_in=timedelta(hours=6, seconds=30))
    for ctx in (FakeCtx([a, b]), NoCancelCtx([a, b])):
        strat = MakerFavoriteHarvest()
        out = await run(strat, ctx)
        assert [(i.ticker, i.expires_in_s) for i in out] == [(a.ticker, 600)]
        assert strat.last_scan["skipped"]["too close to the close cut-off"] == 1
    [i] = await run(MakerFavoriteHarvest({"expires_in_s": 600}), FakeCtx([market("KXC-26OCT01-A")]))
    assert i.expires_in_s == 600


async def test_fresh_book_decides_and_failed_markets_cool_down() -> None:
    m = market("KXA-26OCT01-A")  # snapshot: 0.90 / 0.92
    wide = book(m.ticker, yes="0.90", no="0.05")
    strat = MakerFavoriteHarvest()
    ctx = FakeCtx([m], books={m.ticker: wide})
    assert await run(strat, ctx) == []
    assert strat.last_scan["skipped"]["book: spread too wide"] == 1
    ctx.books[m.ticker] = book_for(m)
    ctx.now = NOW + timedelta(seconds=60)
    assert await run(strat, ctx) == [] and ctx.calls["orderbooks"] == 1  # cooling down: no new fetch
    ctx.now = NOW + timedelta(seconds=BOOK_COOLDOWN_S + 1)
    [i] = await run(strat, ctx)
    assert i.limit_price == D("0.90")
    # the live book moved: the order joins the *current* best bid, not the snapshot's
    n = market("KXN-26OCT01-A")
    moved = Orderbook.from_levels(n.ticker, yes_bids=[("0.91", 40)], no_bids=[("0.08", 100)], ts=NOW)
    [j] = await run(MakerFavoriteHarvest(), FakeCtx([n], books={n.ticker: moved}))
    assert j.limit_price == D("0.91") and "40 ahead" in j.reason


async def test_unfilled_intent_is_retried_only_after_a_delay() -> None:
    m = market("KXA-26OCT01-A")
    strat = MakerFavoriteHarvest()
    ctx = FakeCtx([m])  # the portfolio never shows the order (e.g. the risk manager rejected it)
    assert len(await run(strat, ctx)) == 1
    ctx.now = NOW + timedelta(seconds=60)
    assert await run(strat, ctx) == []
    ctx.now = NOW + timedelta(seconds=RETRY_S + 1)
    assert len(await run(strat, ctx)) == 1


async def test_lookup_budget_spreads_metadata_over_ticks() -> None:
    ms = [market(f"KXS{i}-26OCT01-A", vol=1000 - i) for i in range(4)]
    cached = {m.event_ticker: event(m.event_ticker) for m in ms}  # events already in ctx.events
    strat = MakerFavoriteHarvest({"max_lookups_per_tick": 2})
    ctx = FakeCtx(ms, cached_events=cached)
    out = await run(strat, ctx)
    assert [i.ticker for i in out] == [ms[0].ticker, ms[1].ticker] and ctx.calls["series"] == 2
    assert strat.last_scan["skipped"]["lookup budget spent"] == 2
    assert "event" not in ctx.calls
    ctx.now = NOW + timedelta(seconds=60)
    out = await run(strat, ctx)
    assert [i.ticker for i in out] == [ms[2].ticker, ms[3].ticker] and ctx.calls["series"] == 4


async def test_failed_lookups_are_skipped_and_retried_later() -> None:
    m = market("KXA-26OCT01-A")
    strat = MakerFavoriteHarvest()
    ctx = FakeCtx([m], series_map={})
    assert await run(strat, ctx) == [] and ctx.calls["series"] == 1
    ctx.now = NOW + timedelta(seconds=60)
    assert await run(strat, ctx) == [] and ctx.calls["series"] == 1
    ctx.series_map["KXA"] = series("KXA")
    ctx.now = NOW + timedelta(seconds=META_RETRY_S + 1)
    assert len(await run(strat, ctx)) == 1
    # an unavailable event (fee override unknown) is skipped the same way
    ctx2 = FakeCtx([m], events={})
    assert await run(MakerFavoriteHarvest(), ctx2) == [] and ctx2.calls["event"] == 1


async def test_protocol_only_context() -> None:
    a, b = market("KXA-26OCT01-A"), market("KXB-26OCT01-A", yes_bid="0.03", yes_ask="0.05")
    ctx = MinimalCtx([a, b], series_map={"KXA": series("KXA"), "KXB": series("KXB", category="Sports")})
    out = await run(MakerFavoriteHarvest(), ctx)
    assert [(i.ticker, i.side, i.expires_in_s) for i in out] == [(a.ticker, "yes", NO_CANCEL_EXPIRY_S)]
    assert ctx.calls == Counter({"series": 2, "orderbook": 1})


async def test_sub_cent_tick_markets() -> None:
    m = market("KXA-26OCT01-A", yes_bid="0.905", yes_ask="0.915",
               price_ranges=[{"start": "0.0000", "end": "1.0000", "step": "0.0010"}])
    [i] = await run(MakerFavoriteHarvest({"prior_edge": 0.005}), FakeCtx([m]))
    assert i.limit_price == D("0.905") and i.count == 11
    # 11 x 0.905 = 9.955 is finer than the cent balance precision: that rounding lowers the edge
    assert i.expected_edge == D("0.005") - D("0.005") / 11
    off = market("KXB-26OCT01-A", yes_bid="0.905", yes_ask="0.915")  # cent grid: 0.905 is off it
    assert await run(MakerFavoriteHarvest(), FakeCtx([off])) == []


# --------------------------------------------------------------------------- resting orders


async def test_stale_resting_orders_are_logged_once_without_cancel_support() -> None:
    moved = market("KXA-26OCT01-A", yes_bid="0.93", yes_ask="0.94")  # our 0.90 bid is 3c behind
    closing = market("KXB-26OCT01-A", close_in=timedelta(hours=5))
    fine = market("KXC-26OCT01-A", yes_bid="0.91", yes_ask="0.92")  # 1c: within stale_move
    pf = portfolio(orders=(resting(1, moved), resting(2, closing), resting(3, fine)))
    strat = MakerFavoriteHarvest()
    ctx = NoCancelCtx([moved, closing, fine], pf=pf)
    assert await run(strat, ctx) == []  # nothing else to enter, and it cannot cancel
    msgs = [msg for msg, _ in ctx.logs]
    assert len(msgs) == 2
    assert "order 1" in msgs[0] and "best YES bid 0.93 vs our 0.90" in msgs[0] and "cannot cancel" in msgs[0]
    assert "order 2" in msgs[1] and "closes in 5.0 h" in msgs[1]
    await run(strat, ctx)
    assert len(ctx.logs) == 2  # once per order
    assert "orderbooks" not in ctx.calls  # no books for orders it cannot act on


async def test_resting_orders_kept_moved_or_cancelled() -> None:
    ms = {k: market(f"KX{k}-26OCT01-A") for k in "ABCDEFGH"}
    ms["C"] = market("KXC-26OCT01-A", close_in=timedelta(hours=5))
    books = {
        "A": book(ms["A"].ticker, yes="0.91", no="0.08"),  # moved 1c: keep the queue position
        "B": book(ms["B"].ticker, yes="0.92", no="0.07"),  # moved up 2c: re-quote
        "C": book(ms["C"].ticker, yes="0.90", no="0.08"),  # closes in 5 h: cancel
        "D": book(ms["D"].ticker, yes="0.97", no="0.02"),  # out of range: cancel
        "E": book(ms["E"].ticker, yes="0.50", no="0.48"),  # no favourite any more: cancel
        "F": book(ms["F"].ticker, no="0.08"),  # our side is empty: cancel
        "G": book(ms["G"].ticker, yes="0.88", no="0.10"),  # moved down 2c (still ok): re-quote
        "H": book(ms["H"].ticker, yes="0.87", no="0.08"),  # moved 3c and spread 5c: cancel
    }
    orders = {k: resting(i + 1, ms[k]) for i, k in enumerate("ABCDEFGH")}
    orders["G"] = resting(7, ms["G"], filled=5)  # partly filled: only the rest is re-quoted
    pf = portfolio(orders=tuple(orders.values()))
    strat = MakerFavoriteHarvest()
    ctx = FakeCtx(list(ms.values()), pf=pf, books={ms[k].ticker: b for k, b in books.items()})
    out = await run(strat, ctx)
    cancels = {c.order_id: c.reason for c in out if isinstance(c, CancelIntent)}
    replaces = {i.replaces: i for i in out if isinstance(i, OrderIntent)}
    assert set(cancels) == {orders[k].id for k in "CDEFH"}
    assert "closes in 5.0 h" in cancels[orders["C"].id]
    assert "0.90 -> 0.97" in cancels[orders["D"].id] and "no favourite bid in range" in cancels[orders["D"].id]
    assert "no YES bid left" in cancels[orders["F"].id]
    assert "spread too wide" in cancels[orders["H"].id]
    assert set(replaces) == {orders["B"].id, orders["G"].id}
    b, g = replaces[orders["B"].id], replaces[orders["G"].id]
    assert (b.ticker, b.side, b.limit_price, b.count, b.tif) == (ms["B"].ticker, "yes", D("0.92"), 10, "gtc")
    assert "re-quote of order 2" in b.reason
    assert (g.limit_price, g.count) == (D("0.88"), 6)  # 11 - 5 filled
    assert strat.last_scan["cancels"] == 5 and strat.last_scan["replaces"] == 2
    assert ctx.calls["orderbooks"] == 1  # every resting order in one batch
    # cancelled markets are not re-entered right away, even once the order is gone; a
    # re-quote that never shows up (rejected) waits like any unfilled entry
    ctx.portfolio = portfolio()
    ctx.now = NOW + timedelta(seconds=60)
    ctx.books = {ms[k].ticker: book_for(ms[k]) for k in ms}
    out = await run(strat, ctx)
    assert {i.ticker for i in out} == {ms["A"].ticker}
    ctx.now = NOW + timedelta(seconds=max(RETRY_S, BOOK_COOLDOWN_S) + 1)
    ctx.portfolio = portfolio(orders=(resting(99, ms["A"]),))
    out = await run(strat, ctx)
    assert {i.ticker for i in out} == {ms[k].ticker for k in "BCDEFGH"} - {ms["C"].ticker}  # C closes < 6 h


# --------------------------------------------------------------------------- end to end


def _stack(tmp_path: Any, fc: FakeKalshiClient) -> AppServices:
    settings = Settings()
    settings.storage.path = str(tmp_path / "maker.sqlite3")
    settings.engine.autostart = False
    svc = build_services(settings, client=fc, strategies={NAME: MakerFavoriteHarvest}, feeds=FeedRegistry())
    svc.md.scanner_days_to_close = 0
    return svc


def _fake_market(fc: FakeKalshiClient, t: str = "KXGAS-26OCT01-T3") -> str:
    fc.add_market(t, close_time=datetime.now(UTC) + timedelta(days=2), yes_bid="0.90", yes_ask="0.92",
                  volume_24h=500)
    fc.set_book(t, yes=[("0.90", 100)], no=[("0.08", 100)])
    fc.set_series(t.split("-")[0], category="Economics")
    fc.set_event(t.rsplit("-", 1)[0], category="Economics")
    return t


ENGINE_CANCELS = callable(getattr(EngineContext, "cancel", None))


async def test_engine_places_resting_bid_that_fills_from_later_trades(tmp_path: Any) -> None:
    fc = FakeKalshiClient()
    t = _fake_market(fc)
    svc = _stack(tmp_path, fc)
    try:
        svc.engine.update_strategy(NAME, enabled=True, params={"prior_edge": 0.005})
        await svc.md.refresh_universe(force=True)
        await svc.engine.tick()
        [s] = svc.store.list_signals()
        assert s["decision"] == "executed" and "resting 11 @ 0.90" in s["decision_reason"]
        assert "queue ahead 100" in s["decision_reason"]
        [o] = svc.broker.open_orders()
        assert o.tif == "gtc" and o.strategy == NAME and o.fees == ZERO
        assert o.expires_at is not None and o.created_at is not None
        life = 3600 if ENGINE_CANCELS else NO_CANCEL_EXPIRY_S
        assert abs((o.expires_at - o.created_at).total_seconds() - life) < 1
        # 150 YES contracts sold into the bids at 0.90: 100 ahead of us, then our 11
        fc.add_trade(t, "0.90", 150, datetime.now(UTC) + timedelta(seconds=1), taker="no")
        await svc.engine._job_orders()
        [o] = svc.store.list_orders("all")
        assert o.status == "filled" and o.filled_count == 11 and o.avg_fill_price == D("0.90")
        pos = svc.broker.position(t, NAME)
        assert pos is not None and pos.count == 11 and pos.fees_paid == ZERO  # maker, quadratic: free
        assert pos.expected_edge_total == D("0.005") * 11
        await svc.engine.tick()  # held: never re-entered
        assert len(svc.store.list_signals()) == 1
        assert svc.broker.cash == D(1000) - D("9.90")
    finally:
        await svc.aclose()


@pytest.mark.skipif(not ENGINE_CANCELS, reason="the engine context has no cancel support yet")
async def test_engine_requotes_when_the_bid_moves(tmp_path: Any) -> None:
    fc = FakeKalshiClient()
    t = _fake_market(fc)
    svc = _stack(tmp_path, fc)
    try:
        svc.engine.update_strategy(NAME, enabled=True)
        await svc.md.refresh_universe(force=True)
        await svc.engine.tick()
        [old] = svc.broker.open_orders()
        fc.set_book(t, yes=[("0.92", 100)], no=[("0.07", 100)])  # the bid moved up 2c
        svc.md._books.clear()
        await svc.engine.tick()
        [new] = svc.broker.open_orders()
        assert new.id != old.id and new.limit_price == D("0.92") and new.count == 10
        assert svc.broker.get_order(old.id).status == "cancelled"  # type: ignore[union-attr]
    finally:
        await svc.aclose()
