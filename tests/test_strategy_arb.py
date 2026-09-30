"""Mutually-exclusive NO-basket arbitrage: payout bound, fees, depth walk, all-or-none routing,
thresholds and the documented traps."""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from conftest import FakeKalshiClient

from kalshibot.api.server import AppServices, build_services
from kalshibot.config import Settings
from kalshibot.feeds import FeedRegistry
from kalshibot.fees import trading_fee
from kalshibot.kalshi.client import KalshiNotFound
from kalshibot.kalshi.models import Event, Market, Orderbook
from kalshibot.money import ONE, ZERO, D
from kalshibot.paper.models import PortfolioView, Position
from kalshibot.paper.sim import make_market, make_series
from kalshibot.strategies import REGISTRY
from kalshibot.strategies.no_basket_arb import (
    ROUNDING_SLACK,
    Leg,
    NoBasketArb,
    best_plan,
    is_mve,
    nested_ladder,
    no_ladder,
    plan_basket,
)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
CLOSE = NOW + timedelta(days=2)
EV = "KXARB-26SEP29"


# --------------------------------------------------------------------------- helpers


def fee_fn(mult: Any = 1) -> Any:
    def fee(market: Market, price: Decimal, count: int) -> Decimal:
        return trading_fee(D(price), D(count), is_taker=True, fee_multiplier=D(mult))
    return fee


def leg(ticker: str, asks: Sequence[tuple[Any, Any]], rate: Any = "0.07") -> Leg:
    """A leg from NO asks ``[(price, size), ...]`` (best first)."""
    m = make_market(ticker, event_ticker=EV, close_time=CLOSE)
    book = Orderbook.from_levels(ticker, yes_bids=[(ONE - D(p), D(s)) for p, s in asks])
    return Leg(m, no_ladder(book), D(rate))


def portfolio(cash: Any = 1000, positions: Iterable[Position] = ()) -> PortfolioView:
    c = D(cash)
    return PortfolioView(ts=NOW, starting_balance=D(1000), cash=c, reserved_cash=ZERO, equity=c, equity_mid=c,
                         realized_pnl=ZERO, unrealized_pnl=ZERO, fees_paid=ZERO, day_start_equity=c,
                         positions=tuple(positions))


class FakeCtx:
    """Minimal StrategyContext: universe markets, a lazily fetched event cache, fresh books."""

    def __init__(self, markets: Iterable[Market], books: dict[str, Orderbook], remote_events: Iterable[Event],
                 *, mult: dict[str, Any] | None = None, pv: PortfolioView | None = None,
                 now: datetime = NOW) -> None:
        self.now = now
        self.markets = {m.ticker: m for m in markets}
        self.events: dict[str, Event] = {}
        self.remote = {e.event_ticker: e for e in remote_events}
        self.books = books
        self.portfolio = pv or portfolio()
        self.feeds = None
        self.mult = mult or {}
        self.logs: list[tuple[str, dict[str, Any]]] = []
        self.event_calls: list[str] = []
        self.book_calls: list[list[str]] = []

    async def series(self, series_ticker: str) -> Any:
        return make_series(series_ticker)

    async def orderbook(self, ticker: str) -> Orderbook:
        return self.books[ticker]

    async def orderbooks(self, tickers: Iterable[str]) -> dict[str, Orderbook]:
        tickers = list(tickers)
        self.book_calls.append(tickers)
        return {t: self.books[t] for t in tickers if t in self.books}

    async def event(self, event_ticker: str) -> Event | None:
        self.event_calls.append(event_ticker)
        ev = self.remote.get(event_ticker)
        if ev is None:
            raise KalshiNotFound(404, "event not found", f"/events/{event_ticker}")
        self.events[event_ticker] = ev
        return ev

    def fee(self, market: Market, price: Any, count: Any, is_taker: bool = True) -> Decimal:
        return trading_fee(D(price), D(count), is_taker=is_taker, fee_multiplier=D(self.mult.get(market.ticker, 1)))

    def log(self, msg: str, **data: Any) -> None:
        self.logs.append((msg, data))


def scenario(spec: dict[str, tuple[Sequence[tuple[Any, Any]], Any]], *, me: bool = True,
             event: str = EV, close: dict[str, datetime] | None = None, extra_event_markets: Sequence[Market] = (),
             **ctx_kw: Any) -> FakeCtx:
    """``spec``: ``{suffix: ([(yes_bid, size), ...], yes_ask)}``; the NO book mirrors the YES ask."""
    markets, books = [], {}
    for suffix, (bids, ask) in spec.items():
        t = f"{event}-{suffix}"
        best = max(D(p) for p, _ in bids) if bids else None
        m = make_market(t, event_ticker=event, close_time=(close or {}).get(suffix, CLOSE), yes_bid=best,
                        yes_ask=ask)
        markets.append(m)
        books[t] = Orderbook.from_levels(t, yes_bids=bids, no_bids=[(ONE - D(ask), 500)] if ask is not None else [])
    ev = Event.from_api({"event_ticker": event, "series_ticker": event.split("-")[0], "mutually_exclusive": me},
                        markets=[*markets, *extra_event_markets])
    return FakeCtx(markets, books, [ev], **ctx_kw)


#: NO asks 0.55 / 0.60 / 0.65 (sum 1.80 < payout floor 2), 40 deep each.
ARB3 = {"A": ([("0.45", 40)], "0.46"), "B": ([("0.40", 40)], "0.41"), "C": ([("0.35", 40)], "0.36")}


# --------------------------------------------------------------------------- payout bound


def test_payout_floor_holds_for_every_outcome() -> None:
    legs = [leg("KXARB-26SEP29-A", [("0.55", 40)]), leg("KXARB-26SEP29-B", [("0.60", 40)]),
            leg("KXARB-26SEP29-C", [("0.65", 40)])]
    plan = plan_basket(EV, legs, fee_fn(), min_profit=D("0.01"), max_units=1000)
    assert plan is not None and plan.units == 40 and plan.limits == (D("0.55"), D("0.60"), D("0.65"))
    # worst-case debit per leg = U x limit + ceil_cent(0.07 x U x P(1-P)) + $0.01 rounding slack
    assert plan.leg_costs == (D("22.71"), D("24.69"), D("26.65"))
    assert plan.payout_floor == 80 and plan.guaranteed_profit == D("5.95")
    # mutually exclusive: at most one leg wins (or none of S: the winner is outside the basket)
    for winner in [*range(3), None]:
        payout = sum(plan.units for i in range(3) if i != winner)
        assert payout >= plan.payout_floor
        assert payout - plan.cost_bound >= plan.guaranteed_profit
    # the broker charges less than the bound when it fills at the walked levels
    assert plan.expected_profit == D(80) - (D(22) + D("0.70")) - (D(24) + D("0.68")) - (D(26) + D("0.64"))
    assert plan.expected_profit >= plan.guaranteed_profit


def test_subset_basket_keeps_the_floor() -> None:
    """Legs left out of S (no NO ask, or a winner outside S) never lower the |S|-1 floor."""
    legs = [leg("KXARB-26SEP29-A", [("0.55", 40)]), leg("KXARB-26SEP29-B", [("0.60", 40)]),
            leg("KXARB-26SEP29-C", [("0.65", 40)]), leg("KXARB-26SEP29-D", [])]
    plan = best_plan(EV, legs, fee_fn(), min_profit=D("0.01"), max_units=1000, max_legs=20)
    assert plan is not None and {lg.ticker[-1] for lg in plan.legs} == {"A", "B", "C"}
    assert best_plan(EV, legs, fee_fn(), min_profit=D("0.01"), max_units=1000, max_legs=20, partial=False) is None
    # a thin longshot is dropped when that allows more units
    thin = [*legs[:3], leg("KXARB-26SEP29-E", [("0.98", 2)])]
    plan2 = best_plan(EV, thin, fee_fn(), min_profit=D("0.01"), max_units=1000, max_legs=20)
    assert plan2 is not None and plan2.units == 40 and len(plan2.legs) == 3
    # with only 2 legs allowed, the two best contributors are used
    plan3 = best_plan(EV, legs, fee_fn(), min_profit=D("0.01"), max_units=1000, max_legs=2)
    assert plan3 is None or len(plan3.legs) == 2


def test_no_basket_needs_two_legs() -> None:
    assert plan_basket(EV, [leg("KXARB-26SEP29-A", [("0.10", 10)])], fee_fn(), min_profit=D("0.01"),
                       max_units=100) is None


# --------------------------------------------------------------------------- fees


def test_fees_decide_the_trade() -> None:
    # NO asks sum to 1.98: +$0.02 per basket gross, about $0.047 of taker fees
    asks = [("0.60", 40), ("0.65", 40), ("0.73", 40)]
    legs = [leg(f"KXARB-26SEP29-{c}", [a]) for c, a in zip("ABC", asks, strict=True)]
    assert plan_basket(EV, legs, fee_fn(1), min_profit=D("0.01"), max_units=1000) is None
    free = [Leg(lg.market, lg.ladder, ZERO) for lg in legs]
    plan = plan_basket(EV, free, fee_fn(0), min_profit=D("0.01"), max_units=1000)
    assert plan is not None and plan.units == 40
    assert plan.guaranteed_profit == D("0.80") - 3 * ROUNDING_SLACK  # rounding slack is still charged


def test_per_leg_fee_parameters_are_used() -> None:
    legs = [leg("KXARB-26SEP29-A", [("0.55", 40)], rate="0.035"), leg("KXARB-26SEP29-B", [("0.60", 40)]),
            leg("KXARB-26SEP29-C", [("0.65", 40)])]
    mult = {"KXARB-26SEP29-A": D("0.5")}

    def fee(market: Market, price: Decimal, count: int) -> Decimal:
        return trading_fee(price, count, is_taker=True, fee_multiplier=mult.get(market.ticker, ONE))

    plan = plan_basket(EV, legs, fee, min_profit=D("0.01"), max_units=1000)
    assert plan is not None
    for lg, lim, cost in zip(plan.legs, plan.limits, plan.leg_costs, strict=True):
        m = mult.get(lg.ticker, ONE)
        assert cost == 40 * lim + trading_fee(lim, 40, is_taker=True, fee_multiplier=m) + ROUNDING_SLACK
    assert plan.leg_costs[0] == D("22.00") + D("0.35") + D("0.01")  # ceil_cent(0.035 x 40 x 0.2475)


def test_fee_coefficient_from_context() -> None:
    ctx = scenario(ARB3, mult={f"{EV}-A": D("0.5")})
    a, b = ctx.markets[f"{EV}-A"], ctx.markets[f"{EV}-B"]
    assert NoBasketArb.fee_coefficient(ctx, a) == D("0.035")  # inferred from ctx.fee
    assert NoBasketArb.fee_coefficient(ctx, b) == D("0.07")

    class WithParams(FakeCtx):
        def fee_params(self, market: Market) -> tuple[str, Decimal]:
            return ("quadratic", D("0.5"))

    ctx2 = WithParams(list(ctx.markets.values()), ctx.books, [])
    assert NoBasketArb.fee_coefficient(ctx2, b) == D("0.035")


# --------------------------------------------------------------------------- depth walk


def test_depth_walk_sizes_to_the_thinnest_leg() -> None:
    legs = [leg("KXARB-26SEP29-A", [("0.55", 10), ("0.56", 50)]), leg("KXARB-26SEP29-B", [("0.60", 30)]),
            leg("KXARB-26SEP29-C", [("0.65", 100)])]
    plan = plan_basket(EV, legs, fee_fn(), min_profit=D("0.01"), max_units=1000)
    assert plan is not None and plan.units == 30  # B has only 30
    assert plan.limits == (D("0.56"), D("0.60"), D("0.65"))
    # worst case: all 30 of A at its 0.56 limit
    assert plan.leg_costs == (D("17.33"), D("18.52"), D("19.99")) and plan.guaranteed_profit == D("4.16")
    # expected on the current books: 10 @ 0.55 + 20 @ 0.56 for A, fees cumulative per order
    assert plan.expected_profit == D(60) - D("17.22") - D("18.51") - D("19.98")
    # caps
    assert plan_basket(EV, legs, fee_fn(), min_profit=D("0.01"), max_units=7).units == 7
    capped = plan_basket(EV, legs, fee_fn(), min_profit=D("0.01"), max_units=1000, basket_room=D("20"))
    assert capped is not None and capped.units == 10 and capped.cost_bound <= 20
    leg_capped = plan_basket(EV, legs, fee_fn(), min_profit=D("0.01"), max_units=1000,
                             leg_rooms=[None, None, D("6.00")])
    assert leg_capped is not None and leg_capped.units == 8  # (6.00 - 0.01) / (0.65 + 0.07 x 0.2275)
    assert plan_basket(EV, legs, fee_fn(), min_profit=D("0.01"), max_units=1000, cash_room=D("0.5")) is None


def test_depth_walk_does_not_go_deeper_when_the_worst_case_earns_less() -> None:
    # 11 more contracts of A at 0.63 are still profitable, but the worst case re-prices all 21 at 0.63
    legs = [leg("KXARB-26SEP29-A", [("0.55", 10), ("0.63", 11)]), leg("KXARB-26SEP29-B", [("0.60", 100)]),
            leg("KXARB-26SEP29-C", [("0.65", 100)])]
    plan = plan_basket(EV, legs, fee_fn(), min_profit=D("0.01"), max_units=1000)
    assert plan is not None and plan.units == 10 and plan.guaranteed_profit == D("1.46")
    # a deep level whose unit edge is below the threshold stops the walk
    legs2 = [leg("KXARB-26SEP29-A", [("0.55", 10), ("0.80", 100)]), *legs[1:]]
    assert plan_basket(EV, legs2, fee_fn(), min_profit=D("0.01"), max_units=1000).units == 10


def test_fractional_depth_is_floored_per_level_like_the_broker() -> None:
    lg = leg("KXARB-26SEP29-A", [("0.55", "10.7"), ("0.56", "0.4"), ("0.57", "3.9")])
    assert lg.ladder == ((D("0.55"), 10), (D("0.57"), 3))


# --------------------------------------------------------------------------- on_tick: grouping & thresholds


async def test_on_tick_emits_one_all_or_none_basket() -> None:
    ctx = scenario(ARB3)
    strat = NoBasketArb()
    intents = await strat.on_tick(ctx)
    assert len(intents) == 3
    gids = {i.group_id for i in intents}
    assert len(gids) == 1 and next(iter(gids)).startswith(f"no_basket_arb:{EV}:")
    assert all(i.side == "no" and i.action == "buy" and i.tif == "ioc" and i.count == 40 for i in intents)
    assert all(i.strategy == "no_basket_arb" and i.fair_value is None and i.problems() == [] for i in intents)
    assert {i.ticker: i.limit_price for i in intents} == {
        f"{EV}-A": D("0.55"), f"{EV}-B": D("0.60"), f"{EV}-C": D("0.65")}
    # the legs' expected edges add up to the basket's guaranteed floor ($5.95)
    total = sum(i.expected_edge * i.count for i in intents)
    assert abs(total - D("5.95")) < D("0.001")
    assert "pays >= $2/basket" in intents[0].reason and "guaranteed" in intents[0].reason
    assert ctx.event_calls == [EV]  # lazily looked up once
    assert any(m.startswith("NO basket") for m, _ in ctx.logs)
    summary = [d for m, d in ctx.logs if m.startswith("scan:")]
    assert len(summary) == 1 and summary[0]["fired"] == [EV] and summary[0]["events_me"] == 0
    # cooldown: the same event is not retried on the next tick
    assert await strat.on_tick(ctx) == []
    assert ctx.event_calls == [EV]


async def test_margin_below_threshold_does_not_trade() -> None:
    # guaranteed $5.95 / 40 = $0.14875 per unit basket
    assert len(await NoBasketArb({"min_profit_per_basket": 0.148}).on_tick(scenario(ARB3))) == 3
    ctx = scenario(ARB3)
    assert await NoBasketArb({"min_profit_per_basket": 0.149}).on_tick(ctx) == []
    # an ordinary (fair) event: NO asks sum above N-1 -> nothing, and one summary line per tick
    fair = {"A": ([("0.30", 40)], "0.32"), "B": ([("0.30", 40)], "0.32"), "C": ([("0.36", 40)], "0.38")}
    ctx = scenario(fair)
    strat = NoBasketArb({"prescreen_margin": -1})
    assert await strat.on_tick(ctx) == []
    (msg, data), = [(m, d) for m, d in ctx.logs if m.startswith("scan:")]
    assert data["best_book_margin"] < 0 and data["best_book_event"] == EV and data["fired"] == []
    assert len(ctx.logs) == 1


async def test_discovery_learns_flags_of_near_misses() -> None:
    # snapshot margin about -0.06: below the prescreen (-0.02), above the discovery floor (-0.10)
    near = {"A": ([("0.32", 40)], "0.33"), "B": ([("0.32", 40)], "0.33"), "C": ([("0.33", 40)], "0.34")}
    ctx = scenario(near)
    strat = NoBasketArb()
    assert await strat.on_tick(ctx) == [] and ctx.event_calls == [EV] and ctx.book_calls == []
    assert await strat.on_tick(ctx) == [] and ctx.event_calls == [EV]
    data = [d for msg, d in ctx.logs if msg.startswith("scan:")][-1]
    assert data["events_me"] == 1 and data["best_snapshot_event"] == EV
    assert -0.10 < data["best_snapshot_margin"] < -0.02
    far = scenario(near)
    assert await NoBasketArb({"discovery_margin": -0.05}).on_tick(far) == [] and far.event_calls == []


async def test_prescreen_skips_book_fetches() -> None:
    fair = {"A": ([("0.30", 40)], "0.32"), "B": ([("0.30", 40)], "0.32"), "C": ([("0.36", 40)], "0.38")}
    ctx = scenario(fair)
    # snapshot margin ~ -0.086 < -0.02: no books; below -0.05 no discovery lookup either
    assert await NoBasketArb({"discovery_margin": -0.05}).on_tick(ctx) == []
    assert ctx.book_calls == [] and ctx.event_calls == []


# --------------------------------------------------------------------------- traps


async def test_not_mutually_exclusive_is_never_traded_and_remembered() -> None:
    ctx = scenario(ARB3, me=False)
    strat = NoBasketArb()
    assert await strat.on_tick(ctx) == []
    assert await strat.on_tick(ctx) == []
    assert ctx.event_calls == [EV] and ctx.book_calls == []


async def test_unknown_event_is_marked_not_tradable() -> None:
    ctx = scenario(ARB3)
    ctx.remote.clear()
    strat = NoBasketArb()
    assert await strat.on_tick(ctx) == [] and await strat.on_tick(ctx) == []
    assert ctx.event_calls == [EV]


async def test_event_fetch_budget() -> None:
    ctx = scenario(ARB3)
    assert await NoBasketArb({"max_event_fetches_per_tick": 0}).on_tick(ctx) == []
    assert ctx.event_calls == []


async def test_mixed_close_times_skipped_unless_allowed() -> None:
    close = {"C": CLOSE + timedelta(days=3)}
    assert await NoBasketArb().on_tick(scenario(ARB3, close=close)) == []
    assert len(await NoBasketArb({"allow_mixed_close_times": True}).on_tick(scenario(ARB3, close=close))) == 3


async def test_resolved_yes_leg_skips_the_event() -> None:
    done = make_market(f"{EV}-D", event_ticker=EV, close_time=CLOSE, status="finalized", result="yes")
    assert await NoBasketArb().on_tick(scenario(ARB3, extra_event_markets=[done])) == []
    lost = make_market(f"{EV}-D", event_ticker=EV, close_time=CLOSE, status="finalized", result="no")
    assert len(await NoBasketArb().on_tick(scenario(ARB3, extra_event_markets=[lost]))) == 3


async def test_partial_baskets_and_untradable_legs() -> None:
    spec = {**ARB3, "D": ([], "0.02")}  # D has no YES bids -> no NO ask
    intents = await NoBasketArb().on_tick(scenario(spec))
    assert sorted(i.ticker[-1] for i in intents) == ["A", "B", "C"]
    assert await NoBasketArb({"allow_partial_baskets": False}).on_tick(scenario(spec)) == []
    wide = {**ARB3, "C": ([("0.35", 40)], "0.50")}  # spread 0.15 > max_leg_spread: C is left out
    assert await NoBasketArb().on_tick(scenario(wide)) == []  # A + B alone: 0.55 + 0.60 > 1


async def test_mve_and_closing_legs_are_excluded() -> None:
    m = make_market("KXMVESPORTS-X-A", event_ticker="KXMVESPORTS-X", close_time=CLOSE)
    assert is_mve(m)
    assert is_mve(make_market("KXFOO-1-A", close_time=CLOSE, mve_collection_ticker="KXMVEC"))
    assert not is_mve(make_market("KXFOO-1-A", close_time=CLOSE))
    soon = {s: NOW + timedelta(minutes=5) for s in "ABC"}
    assert await NoBasketArb().on_tick(scenario(ARB3, close=soon)) == []


def test_nested_threshold_ladders_are_not_looked_up() -> None:
    def m(t: str, st: str, floor: Any = None, cap: Any = None, who: Any = None, rules: str = "") -> Market:
        return make_market(t, close_time=CLOSE, strike_type=st, floor_strike=floor, cap_strike=cap,
                           custom_strike=who, rules_primary=rules)

    assert nested_ladder([m("X-1-A", "greater", 10), m("X-1-B", "greater", 20)])
    assert nested_ladder([m("X-1-A", "less", cap=10), m("X-1-B", "less_or_equal", cap=20)])
    # a range partition (one lower tail, buckets, one upper tail) is not nested
    assert not nested_ladder([m("X-1-A", "less", cap=10), m("X-1-B", "between", 10, 20),
                              m("X-1-C", "greater", 20)])
    # different entities, or "exactly N" mislabelled as greater, are not evidence
    assert not nested_ladder([m("X-1-A", "greater", 10, who={"team": "A"}),
                              m("X-1-B", "greater", 20, who={"team": "B"})])
    assert not nested_ladder([m("X-1-A", "greater", 1, 1, rules="exactly 1"),
                              m("X-1-B", "greater", 2, 2, rules="exactly 2")])


async def test_nested_ladder_event_skips_the_lookup() -> None:
    ctx = scenario(ARB3)
    ctx.markets = {t: make_market(t, event_ticker=EV, close_time=CLOSE, yes_bid=x.yes_bid, yes_ask=x.yes_ask,
                                  strike_type="greater", floor_strike=str(10 * (i + 1)))
                   for i, (t, x) in enumerate(sorted(ctx.markets.items()))}
    assert await NoBasketArb().on_tick(ctx) == [] and ctx.event_calls == []
    assert [d for msg, d in ctx.logs if msg.startswith("scan:")][0]["events_nested"] == 1
    ctx.events[EV] = ctx.remote[EV]  # an authoritative flag wins over the heuristic
    assert len(await NoBasketArb().on_tick(ctx)) == 3


async def test_held_event_is_not_reentered() -> None:
    pos = Position(ticker=f"{EV}-A", event_ticker=EV, side="no", count=40, cost_basis=D("22.00"),
                   strategy="no_basket_arb")
    ctx = scenario(ARB3, pv=portfolio(positions=[pos]))
    assert await NoBasketArb().on_tick(ctx) == []
    assert ctx.event_calls == []


async def test_sizing_respects_cost_caps_and_existing_exposure() -> None:
    intents = await NoBasketArb({"max_basket_cost": 20}).on_tick(scenario(ARB3))
    assert {i.count for i in intents} == {10}  # (20 - 0.03) / 1.85 per basket
    other = Position(ticker=f"{EV}-B", event_ticker=EV, side="yes", count=100, cost_basis=D("70"),
                     strategy="someone_else")
    assert await NoBasketArb().on_tick(scenario(ARB3, pv=portfolio(positions=[other]))) == []
    # cash room = cash - cash_reserve: $10 buys 5 baskets ($1.85 each + rounding slack), $1 none
    assert {i.count for i in await NoBasketArb().on_tick(scenario(ARB3, pv=portfolio(cash=60)))} == {5}
    assert await NoBasketArb().on_tick(scenario(ARB3, pv=portfolio(cash=51))) == []


def test_registered_and_declares_universe() -> None:
    assert REGISTRY["no_basket_arb"] is NoBasketArb
    s = NoBasketArb({"max_days_to_close": 3})
    assert s.universe().max_days_to_close == 3 and not NoBasketArb.backtestable
    assert set(NoBasketArb.param_schema) == set(NoBasketArb.default_params)


# --------------------------------------------------------------------------- engine routing (fake exchange)


def arb_exchange(fc: FakeKalshiClient) -> list[str]:
    fc.page_size = 100  # one /markets?tickers= page, like the real API for <= 100 tickers
    fc.set_series("KXARB")
    fc.set_event(EV, mutually_exclusive=True)
    tickers = []
    close = datetime.now(UTC) + timedelta(days=1)
    for suffix, (bids, ask) in ARB3.items():
        t = f"{EV}-{suffix}"
        (bid, depth), = bids
        fc.add_market(t, event_ticker=EV, yes_bid=bid, yes_ask=ask, close_time=close)
        fc.set_book(t, yes=[(bid, depth)], no=[(ONE - D(ask), 500)])
        tickers.append(t)
    return tickers


def arb_stack(settings: Settings, fc: FakeKalshiClient) -> AppServices:
    svc = build_services(settings, client=fc, strategies={"no_basket_arb": NoBasketArb}, feeds=FeedRegistry())
    svc.md.scanner_days_to_close = 0
    return svc


async def test_engine_routes_basket_through_risk_and_broker(settings: Settings, fake_client: FakeKalshiClient) -> None:
    tickers = arb_exchange(fake_client)
    svc = arb_stack(settings, fake_client)
    svc.engine.update_strategy("no_basket_arb", enabled=True)
    await svc.md.refresh_universe(force=True)
    await svc.engine.tick()
    orders = svc.store.list_orders("all")
    assert len(orders) == 3 and all(o.status == "filled" and o.filled_count == 40 for o in orders)
    assert len({o.group_id for o in orders}) == 1 and orders[0].group_id.startswith("no_basket_arb:")
    sig = svc.store.list_signals()
    assert len(sig) == 3 and all(s["decision"] == "executed" for s in sig)
    cost = D(22) + D("0.70") + D(24) + D("0.68") + D(26) + D("0.64")
    assert svc.broker.cash == D(1000) - cost
    assert {(p.ticker, p.side, p.count) for p in svc.broker.positions()} == {(t, "no", 40) for t in tickers}
    # settle: A wins -> B and C pay $1 each: 80 >= payout floor, P&L >= guaranteed $5.95
    for t in tickers:
        fake_client.update_market(t, status="finalized", result="yes" if t.endswith("-A") else "no")
    await svc.md.refresh_markets(tickers)  # what the engine's settlement job does first
    settled = await svc.broker.check_settlements()
    assert sum(s.payout for s in settled) == 80
    assert sum(s.pnl for s in settled) == D(80) - cost >= D("5.95")
    await svc.aclose()


async def test_engine_rejects_whole_basket_when_risk_trims_a_leg(settings: Settings,
                                                                 fake_client: FakeKalshiClient) -> None:
    arb_exchange(fake_client)
    settings.risk.max_position_cost_per_market = D(20)  # leg C costs ~$26.6 > $20; strategy cap is $45
    svc = arb_stack(settings, fake_client)
    svc.engine.update_strategy("no_basket_arb", enabled=True)
    await svc.md.refresh_universe(force=True)
    await svc.engine.tick()
    sig = svc.store.list_signals()
    assert len(sig) == 3 and all(s["decision"] == "rejected" for s in sig)
    assert all("all-or-none" in s["decision_reason"] for s in sig)
    assert svc.broker.positions() == [] and svc.broker.cash == D(1000)
    assert len({s["group_id"] for s in sig}) == 1
    await svc.aclose()


@pytest.mark.parametrize("winner", [None, 0, 1, 2])
def test_bound_matches_brute_force(winner: int | None) -> None:
    """Brute force over small ladders: whenever a plan exists, every settlement pays its floor."""
    ladders = [[("0.50", 5), ("0.52", 5)], [("0.60", 8)], [("0.70", 3), ("0.71", 20)]]
    legs = [leg(f"KXARB-26SEP29-{c}", lad) for c, lad in zip("ABC", ladders, strict=True)]
    for mp in (D("0.001"), D("0.01"), D("0.05")):
        plan = plan_basket(EV, legs, fee_fn(), min_profit=mp, max_units=1000)
        if plan is None:
            continue
        payout = sum(plan.units for i in range(3) if i != winner)
        assert payout - plan.cost_bound >= plan.guaranteed_profit >= mp * plan.units
    # every subset of the ranked legs obeys the same floor
    for n in (2, 3):
        for sub in itertools.combinations(legs, n):
            p = plan_basket(EV, list(sub), fee_fn(), min_profit=D("0.001"), max_units=1000)
            assert p is None or p.payout_floor == (n - 1) * p.units
