"""BTC 15-minute favourite at 10 min to close (``btc15m_favorite``, the primary research rule): trigger
timing, favourite side, band edges, model edge filter, fees, settlement basis, no double entry, stale
spot data, sizing, the replay/settled feeds and one engine round trip. Deterministic, no network."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from conftest import FakeKalshiClient, iso

from kalshibot.api.server import build_services
from kalshibot.config import Settings
from kalshibot.feeds import FeedRegistry, KalshiSettledFeed, ReplayCryptoFeed, ReplaySettledFeed, build_feeds
from kalshibot.feeds.crypto import FeedError, SpotCandle, SpotQuote
from kalshibot.fees import trading_fee
from kalshibot.kalshi.models import Event, Market, Orderbook, Series
from kalshibot.money import ONE, ZERO, D
from kalshibot.paper.models import Order, PortfolioView, Position
from kalshibot.strategies import REGISTRY
from kalshibot.strategies.base import coerce_params
from kalshibot.strategies.btc15m_favorite import (
    T_DOF,
    VOL_MULT,
    Btc15mFavorite,
    ModelUnavailable,
    ModelView,
    ewma_variance,
    final_minute_mid,
    horizon_minutes,
    minute_grid,
    parse_expiration_value,
    prob_above,
    rolling_basis,
    student_t_cdf,
    unit_t_cdf,
)

NAME = "btc15m_favorite"
SERIES = "KXBTC15M"
C = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)  # close of the traded window
OPEN = C - timedelta(minutes=15)
T0 = C - timedelta(minutes=9, seconds=55)  # default decision time: 9.92 min to close (window (9.75, 10])
T0_MIN = 9 + 55 / 60
TICKER = "KXBTC15M-26SEP270800-00"
TAPER = [{"start": "0.0000", "end": "0.1000", "step": "0.0010"},
         {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
         {"start": "0.9000", "end": "1.0000", "step": "0.0010"}]
S0 = 84_000.0
FIXED = {"sizing": "fixed", "contracts": 20}


# --------------------------------------------------------------------------- data


def spot_bars(until: datetime = C + timedelta(minutes=10), minutes: int = 1100) -> list[SpotCandle]:
    """Deterministic Coinbase-like 1-minute bars (log returns ~ +-0.04%) ending at ``until``."""
    out = []
    price = S0
    start = until - timedelta(minutes=minutes)
    for i in range(minutes):
        op = price
        price = op * math.exp(0.0004 * math.sin(1.7 * i) + 0.00002 * math.cos(0.3 * i))
        out.append(SpotCandle(start + timedelta(minutes=i), op, max(op, price), min(op, price), price, 1.0,
                              source="coinbase"))
    return out


BARS = spot_bars()
BY_END = {int(c.end.timestamp()): c for c in BARS}


def cb_mid(close: datetime) -> float:
    c = BY_END[int(close.timestamp())]
    return (c.open + c.close) / 2


def settled_markets(n: int = 60, basis_of: Any = float) -> list[Market]:
    """Past windows; k=1 closed at this window's open (excluded from the basis), k=2 before, ...
    Window k settles at Coinbase final-minute mid + ``basis_of(k)``."""
    out = []
    for k in range(1, n + 1):
        close = OPEN - timedelta(minutes=15 * (k - 1))
        xv = cb_mid(close) + basis_of(k)
        out.append(Market.from_api({
            "ticker": f"{SERIES}-P{k:03d}-00", "event_ticker": f"{SERIES}-P{k:03d}", "series_ticker": SERIES,
            "status": "finalized", "market_type": "binary", "result": "yes" if k % 2 else "no",
            "open_time": iso(close - timedelta(minutes=15)), "close_time": iso(close),
            "settlement_ts": iso(close + timedelta(seconds=6)), "strike_type": "greater_or_equal",
            "floor_strike": 84000.0, "expiration_value": f"{xv:,.2f}" if k % 10 == 3 else f"{xv:.2f}",
        }))
    return out


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def replay_feeds(clock: Clock, *, n_settled: int = 60, bars: list[SpotCandle] | None = None) -> FeedRegistry:
    return FeedRegistry({
        "crypto": ReplayCryptoFeed({"BTC": BARS if bars is None else bars}, clock=clock),
        "kalshi_settled": ReplaySettledFeed(settled_markets(n_settled), clock=clock),
    })


def window_market(strike: Any = "84000", *, close: datetime = C, series: str = SERIES,
                  ticker: str = TICKER, status: str = "active") -> Market:
    return Market.from_api({
        "ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "series_ticker": series, "status": status,
        "market_type": "binary", "open_time": iso(close - timedelta(minutes=15)), "close_time": iso(close),
        "strike_type": "greater_or_equal", "floor_strike": float(strike), "price_ranges": TAPER,
    })


def book(yes_bid: Any, yes_ask: Any, *, ticker: str = TICKER, depth: Any = 5000) -> Orderbook:
    return Orderbook.from_levels(ticker, yes_bids=[(D(yes_bid), depth)] if yes_bid is not None else [],
                                 no_bids=[(ONE - D(yes_ask), depth)] if yes_ask is not None else [])


def portfolio(positions: tuple[Position, ...] = (), orders: tuple[Order, ...] = (),
              equity: Any = 1000) -> PortfolioView:
    k = D(equity)
    return PortfolioView(ts=T0, starting_balance=k, cash=k, reserved_cash=ZERO, equity=k, equity_mid=k,
                         realized_pnl=ZERO, unrealized_pnl=ZERO, fees_paid=ZERO, day_start_equity=None,
                         positions=positions, open_orders=orders)


@dataclass
class FakeCtx:
    now: datetime = T0
    markets: dict[str, Market] = field(default_factory=dict)
    events: dict[str, Event] = field(default_factory=dict)
    portfolio: PortfolioView = field(default_factory=portfolio)
    feeds: Any = None
    books: dict[str, Orderbook] = field(default_factory=dict)
    logs: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    book_calls: list[str] = field(default_factory=list)
    book_error: bool = False

    async def series(self, series_ticker: str) -> Series:
        return Series.from_api({"ticker": series_ticker, "fee_type": "quadratic", "fee_multiplier": 1})

    async def orderbook(self, ticker: str) -> Orderbook:
        self.book_calls.append(ticker)
        if self.book_error:
            raise httpx.ConnectError("boom")
        return self.books[ticker]

    def fee(self, market: Market, price: Any, count: Any, is_taker: bool = True) -> Decimal:
        return trading_fee(D(price), D(count), is_taker=is_taker)

    def log(self, msg: str, **data: Any) -> None:
        self.logs.append((msg, data))

    @property
    def skips(self) -> list[str]:
        return [d.get("skip", "") for _, d in self.logs]

    @property
    def text(self) -> str:
        return "\n".join(m for m, _ in self.logs)


def make_ctx(*, now: datetime = T0, strike: Any = "84000", yes_bid: Any = "0.89", yes_ask: Any = "0.90",
             feeds: Any = "replay", clock: Clock | None = None, **kw: Any) -> FakeCtx:
    clock = clock or Clock(now)
    clock.now = now
    m = window_market(strike)
    fr = replay_feeds(clock) if feeds == "replay" else feeds
    return FakeCtx(now=now, markets={m.ticker: m}, feeds=fr, books={m.ticker: book(yes_bid, yes_ask)}, **kw)


async def model_at(now: datetime = T0, strike: Any = "84000") -> ModelView:
    ctx = make_ctx(now=now, strike=strike)
    return await Btc15mFavorite().model(ctx, ctx.markets[TICKER])


async def strike_for(p_yes: float, now: datetime = T0) -> Decimal:
    """The strike (on the cent grid) at which the model says P(YES) ~= ``p_yes`` at ``now``."""
    mv = await model_at(now)
    sig = VOL_MULT * math.sqrt(mv.sigma_min ** 2 * mv.tau_min)
    lo, hi = mv.spot * 0.95, mv.spot * 1.05
    for _ in range(200):  # P(YES) falls as the strike rises
        mid = (lo + hi) / 2
        if prob_above(mv.spot, mid, sig, T_DOF, mv.basis) > p_yes:
            lo = mid
        else:
            hi = mid
    return D(f"{lo:.2f}")


async def run(strategy: Btc15mFavorite, ctx: FakeCtx) -> list[Any]:
    return list(await strategy.on_tick(ctx))


# --------------------------------------------------------------------------- metadata


def test_registered_metadata_and_schema() -> None:
    assert REGISTRY[NAME] is Btc15mFavorite
    s = Btc15mFavorite()
    assert s.backtestable is True
    assert s.universe().series_tickers == [SERIES] and s.universe().max_days_to_close is None
    assert s.universe().refresh_s == 20 and Btc15mFavorite.tick_interval_s == 5.0  # decide within 5 s of 10:00
    assert coerce_params(Btc15mFavorite.param_schema, Btc15mFavorite.default_params) == Btc15mFavorite.default_params
    assert set(Btc15mFavorite.param_schema) == set(Btc15mFavorite.default_params)
    sj = Btc15mFavorite.schema_json()
    assert sj["min_model_edge"]["default"] == 0.01 and sj["price_min"]["default"] == 0.85
    assert sj["entry_minutes_max"]["default"] == 10.0 and sj["entry_minutes_min"]["default"] == 9.75
    assert sj["use_model"]["default"] is True and sj["max_cost_per_trade"]["default"] == 50.0


# --------------------------------------------------------------------------- model math


@pytest.mark.parametrize(("x", "nu", "ref"), [  # scipy.stats.t.cdf
    (-40, 3.5, 4.383220554686511e-06), (-8.3, 3.5, 0.0010063187788272154), (-3.1, 3.5, 0.02167735292719041),
    (-1.3, 3.5, 0.13629770790218498), (-0.2, 3.5, 0.42627583484130727), (0.0, 3.5, 0.5),
    (0.7, 3.5, 0.7361829288271604), (1.3, 3.5, 0.863702292097815), (2.5, 3.5, 0.962152678095255),
    (6.0, 3.5, 0.9970555236775422), (25, 3.5, 0.9999774004861893), (1.1, 2, 0.8069800647022711),
    (-2.2, 4, 0.046326335089817296), (0.5, 30, 0.6896384975574363), (3, 7.25, 0.9904329335034154),
])
def test_student_t_cdf_matches_scipy(x: float, nu: float, ref: float) -> None:
    assert student_t_cdf(x, nu) == pytest.approx(ref, rel=1e-10, abs=1e-13)


def test_t_cdf_closed_forms_and_unit_variance() -> None:
    for x in (-3.0, -0.4, 0.0, 1.1, 7.0):
        assert student_t_cdf(x, 1) == pytest.approx(0.5 + math.atan(x) / math.pi, abs=1e-13)  # Cauchy
        assert student_t_cdf(x, 2) == pytest.approx(0.5 + x / (2 * math.sqrt(2 + x * x)), abs=1e-13)
    # unit variance: z / sqrt((nu-2)/nu) is the standard t argument
    assert unit_t_cdf(1.0, 3.5) == pytest.approx(student_t_cdf(1.0 / math.sqrt(1.5 / 3.5), 3.5), abs=1e-15)
    assert unit_t_cdf(1.0, None) == pytest.approx(0.8413447460685429, abs=1e-12)


def test_prob_above_matches_the_research_formula() -> None:
    # 1 - scipy t(3.5) cdf of ((ln(K - b) - ln S) / sig) / sqrt(1.5 / 3.5), computed with scipy
    assert prob_above(84912.1, 84887.51, 0.0021, 3.5, 2.52) == pytest.approx(0.5853690698620575, abs=1e-12)
    assert prob_above(100.0, 100.0, 0.01, 3.5, 0.0) == pytest.approx(0.5)
    assert prob_above(100.0, 99.0, 0.01, 3.5) > 0.5 > prob_above(100.0, 101.0, 0.01, 3.5)
    # a positive basis (settlement above Coinbase) raises P(YES)
    assert prob_above(100.0, 100.0, 0.01, 3.5, basis=0.5) > 0.5
    assert horizon_minutes(10) == pytest.approx(10 - 2 / 3)
    assert horizon_minutes(9.5) == pytest.approx(9.5 - 2 / 3)
    assert horizon_minutes(0.5) == pytest.approx(1 / 3)


def test_ewma_variance_follows_pandas_adjust_true() -> None:
    closes = [100.0]
    for r in (0.01, -0.02, 0.005, 0.0, 0.03):
        closes.append(closes[-1] * math.exp(r))
    rs = [0.01, -0.02, 0.005, 0.0, 0.03]
    w = 0.5 ** (1 / 10)
    num = sum(w ** (len(rs) - 1 - i) * r * r for i, r in enumerate(rs))
    den = sum(w ** (len(rs) - 1 - i) for i in range(len(rs)))
    assert ewma_variance(closes, 10, min_periods=5) == pytest.approx(num / den, rel=1e-12)
    assert ewma_variance(closes, 10, min_periods=6) is None  # fewer returns than min_periods
    const = [100 * math.exp(0.001 * i) for i in range(80)]
    assert ewma_variance(const, 10, min_periods=60) == pytest.approx(1e-6, rel=1e-9)


def test_minute_grid_forward_fills_missing_minutes() -> None:
    t = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)
    bars = [SpotCandle(t, 1, 1, 1, 10.0, 1), SpotCandle(t + timedelta(minutes=3), 1, 1, 1, 13.0, 1)]
    g = minute_grid(bars)
    e0 = int(t.timestamp()) + 60
    assert g == [(e0, 10.0), (e0 + 60, 10.0), (e0 + 120, 10.0), (e0 + 180, 13.0)]


def test_basis_helpers() -> None:
    assert parse_expiration_value("84264.76") == 84264.76
    assert parse_expiration_value("79,604.96") == 79604.96  # research dropped these (NaN); we parse them
    assert parse_expiration_value("") is None and parse_expiration_value("Cancelled") is None
    assert parse_expiration_value(None) is None
    # median of the 48 most recent (close, settlement, coinbase) records; needs >= 10
    recs = [(float(i), 100.0 + i, 100.0) for i in range(1, 61)]  # basis i at close i
    assert rolling_basis(recs) == (pytest.approx(36.5), 48)  # closes 13..60
    assert rolling_basis(recs[:10]) == (pytest.approx(5.5), 10)
    assert rolling_basis(recs[:9]) == (None, 9)
    # final-minute mid: (open + close) / 2 of the bar ending at the close; missing bar -> last close
    t = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)
    bars = {int((t + timedelta(minutes=i + 1)).timestamp()): SpotCandle(t + timedelta(minutes=i), 10.0 + i,
                                                                     11.0 + i, 9.0 + i, 10.5 + i, 1)
            for i in (0, 1, 3)}
    e = int(t.timestamp())
    assert final_minute_mid(bars, e + 60) == pytest.approx(10.25)
    assert final_minute_mid(bars, e + 180) == pytest.approx(11.5)  # bar ending there is missing -> ffill close
    assert final_minute_mid(bars, e + 600) is None  # not covered by the bars
    assert final_minute_mid(bars, e) is None


async def test_model_uses_last_bar_spot_basis_before_open_and_actual_horizon() -> None:
    mv = await model_at()
    last = [c for c in BARS if c.end <= T0][-1]
    assert mv.spot == last.close
    assert mv.strike == 84000.0
    # windows k=2..49 (k=1 closed at this window's open and is excluded): median of 2..49
    assert mv.basis == pytest.approx(25.5, abs=0.01) and mv.basis_n == 48
    assert mv.minutes_left == pytest.approx(T0_MIN) and mv.tau_min == pytest.approx(T0_MIN - 2 / 3)
    grid = [v for t, v in minute_grid([c for c in BARS if c.end <= T0]) if t >= T0.timestamp() - 7200]
    assert mv.sigma_min == pytest.approx(math.sqrt(ewma_variance(grid)), rel=1e-12)
    sig = VOL_MULT * mv.sigma_min * math.sqrt(mv.tau_min)
    assert mv.p_yes == pytest.approx(prob_above(mv.spot, 84000.0, sig, 3.5, mv.basis), abs=1e-15)


# --------------------------------------------------------------------------- trigger timing


@pytest.mark.parametrize(("minutes", "fires"), [
    (11.0, False), (10.5, False), (10.001, False), (10.0, True), (9.9, True), (9.76, True),
    (9.75, False), (9.5, False), (9.01, False), (9.0, False), (8.0, False), (5.0, False),
])
async def test_trigger_window_is_the_first_15_s_after_10_minutes_before_close(minutes: float, fires: bool) -> None:
    now = C - timedelta(seconds=round(minutes * 60, 3))
    strike = await strike_for(0.995, now) if fires else D("84000")
    ctx = make_ctx(now=now, strike=strike)
    out = await run(Btc15mFavorite(FIXED), ctx)
    assert bool(out) is fires
    if not fires:
        assert ctx.book_calls == [] and ctx.logs == []  # outside the window: no requests, no noise


async def test_fires_once_per_window_even_across_a_restart() -> None:
    strike = await strike_for(0.995, C - timedelta(minutes=9.9))
    s = Btc15mFavorite(FIXED)
    clock = Clock(C - timedelta(minutes=9.9))
    ctx = make_ctx(now=clock.now, strike=strike, clock=clock)
    assert len(await run(s, ctx)) == 1 and s.has_decided(TICKER)
    ctx2 = make_ctx(now=C - timedelta(minutes=9.85), strike=strike)
    assert await run(s, ctx2) == [] and ctx2.book_calls == []
    s2 = Btc15mFavorite(FIXED)
    s2.load_state(s.dump_state())
    ctx3 = make_ctx(now=C - timedelta(minutes=9.8), strike=strike)
    assert await run(s2, ctx3) == [] and ctx3.book_calls == []


async def test_a_skip_is_also_final_for_the_window() -> None:
    s = Btc15mFavorite(FIXED)
    ctx = make_ctx(yes_bid="0.60", yes_ask="0.61")
    assert await run(s, ctx) == [] and ctx.skips == ["band"]
    ctx2 = make_ctx(now=T0 + timedelta(seconds=5), yes_bid="0.89", yes_ask="0.90",
                    strike=await strike_for(0.995))
    assert await run(s, ctx2) == [] and ctx2.book_calls == []


async def test_other_series_and_inactive_markets_are_ignored() -> None:
    ctx = make_ctx()
    eth = window_market(ticker="KXETH15M-26SEP270800-00", series="KXETH15M")
    ctx.markets = {eth.ticker: eth, TICKER: window_market(status="closed")}
    assert await run(Btc15mFavorite(FIXED), ctx) == [] and ctx.book_calls == []


# --------------------------------------------------------------------------- side & band


async def test_buys_yes_when_yes_is_the_favourite_and_the_model_agrees() -> None:
    strike = await strike_for(0.99)
    ctx = make_ctx(strike=strike, yes_bid="0.89", yes_ask="0.90")
    (it,) = await run(Btc15mFavorite(FIXED), ctx)
    mv = await model_at(strike=strike)
    assert it.ticker == TICKER and it.side == "yes" and it.action == "buy" and it.tif == "ioc"
    assert it.limit_price == D("0.90") and it.count == 20 and it.strategy == NAME
    assert it.fair_value == pytest.approx(mv.p_yes, abs=1e-6)
    fee = trading_fee(D("0.90"), 20, is_taker=True)
    assert it.expected_edge == (D(mv.p_yes) - D("0.90") - fee / 20).quantize(Decimal("0.000001"))
    assert "YES ask 0.90 in [0.85, 0.97]" in it.reason and "model-confirmed" in it.reason
    assert "basis +25.5" in it.reason and "hold to settlement" in it.reason
    # the decision is logged like every skip (one line per window)
    assert len(ctx.logs) == 1 and ctx.logs[0][1]["trade"] == "yes"
    assert "trade at 9.92 min to close" in ctx.logs[0][0] and "buy 20 YES @ 0.90 IOC" in ctx.logs[0][0]


async def test_buys_no_when_no_is_the_favourite_and_the_model_agrees() -> None:
    strike = await strike_for(0.01)
    ctx = make_ctx(strike=strike, yes_bid="0.09", yes_ask="0.10")  # NO ask = 0.91
    (it,) = await run(Btc15mFavorite(FIXED), ctx)
    mv = await model_at(strike=strike)
    assert it.side == "no" and it.limit_price == D("0.91")
    assert it.fair_value == pytest.approx(1 - mv.p_yes, abs=1e-6)
    assert "NO ask 0.91" in it.reason


@pytest.mark.parametrize(("yes_bid", "yes_ask", "side"), [
    ("0.84", "0.85", "yes"),  # YES ask at the lower edge
    ("0.969", "0.97", "yes"),  # upper edge
    ("0.83", "0.84", None),  # just below
    ("0.97", "0.971", None),  # just above (0.001 ticks above 0.90)
    ("0.15", "0.16", "no"),  # NO ask 0.85
    ("0.03", "0.031", "no"),  # NO ask 0.97
    ("0.16", "0.17", None),  # NO ask 0.84
    ("0.029", "0.03", None),  # NO ask 0.971
    ("0.50", "0.51", None),  # no favourite
])
async def test_price_band_edges_are_inclusive(yes_bid: str, yes_ask: str, side: str | None) -> None:
    p_yes = 0.9995 if D(yes_ask) > D("0.5") else 0.0005
    ctx = make_ctx(strike=await strike_for(p_yes), yes_bid=yes_bid, yes_ask=yes_ask)
    out = await run(Btc15mFavorite(FIXED), ctx)
    if side is None:
        assert out == [] and ctx.skips == ["band"] and "price out of band" in ctx.text
    else:
        assert [i.side for i in out] == [side]


async def test_one_sided_book_uses_the_side_that_exists() -> None:
    ctx = make_ctx(strike=await strike_for(0.9995), yes_bid=None, yes_ask="0.90")
    (it,) = await run(Btc15mFavorite(FIXED), ctx)
    assert it.side == "yes"


# --------------------------------------------------------------------------- model edge & fees


async def _edge_case(min_edge: float, *, contracts: int = 20, p_yes: float = 0.93) -> tuple[list[Any], FakeCtx, float]:
    strike = await strike_for(p_yes)
    mv = await model_at(strike=strike)
    fee = float(trading_fee(D("0.90"), contracts, is_taker=True)) / contracts
    edge = mv.p_yes - 0.90 - fee
    ctx = make_ctx(strike=strike)
    s = Btc15mFavorite({"sizing": "fixed", "contracts": contracts, "min_contracts": 1,
                        "min_model_edge": edge + min_edge})
    return await run(s, ctx), ctx, edge


async def test_model_edge_filter_threshold() -> None:
    out, _, edge = await _edge_case(-1e-4)
    assert len(out) == 1 and 0.02 < edge < 0.03
    out, ctx, _ = await _edge_case(+1e-4)
    assert out == [] and ctx.skips == ["model"] and "model disagrees" in ctx.text
    assert "P(YES)=" in ctx.text and "after fee" in ctx.text


async def test_default_threshold_rejects_an_overpriced_favourite() -> None:
    ctx = make_ctx(strike=await strike_for(0.905), yes_bid="0.89", yes_ask="0.90")
    assert await run(Btc15mFavorite(FIXED), ctx) == [] and ctx.skips == ["model"]
    ctx = make_ctx(strike=await strike_for(0.92), yes_bid="0.89", yes_ask="0.90")  # 2c - fee > 1c
    assert len(await run(Btc15mFavorite(FIXED), ctx)) == 1


async def test_fee_is_included_in_the_edge() -> None:
    strike = await strike_for(0.93)
    mv = await model_at(strike=strike)
    raw = mv.p_yes - 0.90
    fee20 = float(trading_fee(D("0.90"), 20, is_taker=True)) / 20  # 0.13 / 20
    # passes before the fee, fails after it
    ctx = make_ctx(strike=strike)
    s = Btc15mFavorite({**FIXED, "min_model_edge": raw - fee20 / 2})
    assert await run(s, ctx) == [] and ctx.skips == ["model"]
    # the fee of the actual order: a 1-lot pays a whole cent at 0.90, 100 contracts 0.63c each
    fee1 = float(trading_fee(D("0.90"), 1, is_taker=True))
    fee100 = float(trading_fee(D("0.90"), 100, is_taker=True)) / 100
    assert fee1 == 0.01 and fee100 == pytest.approx(0.0063)
    thr = raw - (fee1 + fee100) / 2
    one = Btc15mFavorite({"sizing": "fixed", "contracts": 1, "min_contracts": 1, "min_model_edge": thr})
    hundred = Btc15mFavorite({"sizing": "fixed", "contracts": 100, "min_contracts": 1, "min_model_edge": thr,
                              "max_cost_per_trade": 1000})
    assert await run(one, make_ctx(strike=strike)) == []
    (it,) = await run(hundred, make_ctx(strike=strike))
    assert it.count == 100
    assert float(it.expected_edge) == pytest.approx(raw - fee100, abs=1e-6)


# --------------------------------------------------------------------------- no double entry


async def test_no_entry_when_the_window_is_already_held() -> None:
    pos = Position(ticker=TICKER, strategy=NAME, event_ticker=TICKER.rsplit("-", 1)[0], side="yes", count=20,
                   cost_basis=D("18"))
    ctx = make_ctx(strike=await strike_for(0.995), portfolio=portfolio(positions=(pos,)))
    s = Btc15mFavorite(FIXED)
    assert await run(s, ctx) == [] and ctx.book_calls == [] and ctx.skips == ["held"]
    assert "no second entry" in ctx.text and s.has_decided(TICKER)


async def test_no_entry_with_an_open_order_in_the_window() -> None:
    o = Order(id=7, ticker=TICKER, side="yes", action="buy", count=20, limit_price=D("0.90"), strategy=NAME)
    ctx = make_ctx(strike=await strike_for(0.995), portfolio=portfolio(orders=(o,)))
    assert await run(Btc15mFavorite(FIXED), ctx) == [] and ctx.skips == ["held"]


async def test_another_strategys_position_does_not_block() -> None:
    pos = Position(ticker=TICKER, strategy="someone_else", side="yes", count=5, cost_basis=D("4.5"))
    ctx = make_ctx(strike=await strike_for(0.995), portfolio=portfolio(positions=(pos,)))
    assert len(await run(Btc15mFavorite(FIXED), ctx)) == 1


# --------------------------------------------------------------------------- stale / missing spot data


class StaleSpot:
    """Wraps a replay feed; ``spot()`` returns a quote ``age_s`` old (or from another exchange)."""

    def __init__(self, inner: ReplayCryptoFeed, age_s: float = 0, source: str = "coinbase",
                 fail: bool = False) -> None:
        self.inner, self.age_s, self.source, self.fail = inner, age_s, source, fail

    async def spot(self, symbol: str = "BTC") -> SpotQuote:
        if self.fail:
            raise FeedError("spot BTC unavailable (coinbase: 503; kraken: 503)")
        q = await self.inner.spot(symbol)
        ts = self.inner.now - timedelta(seconds=self.age_s)
        return SpotQuote(q.symbol, q.price, None, None, ts, self.source, ts)

    async def candles(self, symbol: str = "BTC", minutes: int = 60) -> list[SpotCandle]:
        return await self.inner.candles(symbol, minutes)


def feeds_with(clock: Clock, crypto: Any = None, settled: Any = None, *, n_settled: int = 60) -> FeedRegistry:
    base = replay_feeds(clock, n_settled=n_settled)
    reg = {"crypto": crypto(base.crypto) if callable(crypto) else base.crypto,
           "kalshi_settled": settled if settled is not None else base.kalshi_settled}
    return FeedRegistry(reg)


def _kraken_bars() -> list[SpotCandle]:
    return [SpotCandle(c.ts, c.open, c.high, c.low, c.close, c.volume, source="kraken") for c in BARS]


class BrokenSettled:
    async def settled_markets(self, series_ticker: str, *, since: datetime | None = None) -> list[Market]:
        raise httpx.ConnectError("kalshi down")


@pytest.mark.parametrize(("make", "needle"), [
    (lambda c: feeds_with(c, lambda f: StaleSpot(f, age_s=61)), "spot stale"),
    (lambda c: feeds_with(c, lambda f: StaleSpot(f, source="kraken")), "spot not from Coinbase"),
    (lambda c: feeds_with(c, lambda f: StaleSpot(f, fail=True)), "Coinbase spot: FeedError"),
    (lambda c: FeedRegistry({"crypto": ReplayCryptoFeed({"BTC": [b for b in BARS if b.end <= T0 - timedelta(
        minutes=4)]}, clock=c), "kalshi_settled": replay_feeds(c).kalshi_settled}), "candles stale"),
    (lambda c: FeedRegistry({"crypto": ReplayCryptoFeed({"BTC": _kraken_bars()}, clock=c),
                             "kalshi_settled": replay_feeds(c).kalshi_settled}), "candles not from Coinbase"),
    (lambda c: FeedRegistry({"crypto": ReplayCryptoFeed({"BTC": [b for b in BARS if b.end > T0 - timedelta(
        minutes=30)]}, clock=c), "kalshi_settled": replay_feeds(c).kalshi_settled}), "too little bar history"),
    (lambda c: feeds_with(c, n_settled=10), "basis needs 10 settled windows"),  # k=1 excluded -> 9
    (lambda c: feeds_with(c, settled=BrokenSettled()), "settled markets: ConnectError"),
    (lambda c: FeedRegistry(), "feeds 'crypto' and 'kalshi_settled' are required"),
    (lambda c: None, "feeds 'crypto' and 'kalshi_settled' are required"),
])
async def test_no_trade_without_fresh_coinbase_data(make: Any, needle: str) -> None:
    clock = Clock(T0)
    strike = await strike_for(0.995)
    ctx = make_ctx(strike=strike, feeds=make(clock), clock=clock)
    s = Btc15mFavorite(FIXED)
    assert await run(s, ctx) == []
    assert ctx.skips == ["no_model"] and needle in ctx.text and "will retry within the window" in ctx.text
    assert not s.has_decided(TICKER)  # missing data is not a decision
    with pytest.raises(ModelUnavailable, match=needle.split(":")[0]):
        await s.model(ctx, ctx.markets[TICKER])


async def test_retries_later_in_the_window_once_spot_is_fresh_again() -> None:
    clock = Clock(T0)
    strike = await strike_for(0.995)
    s = Btc15mFavorite(FIXED)
    stale = make_ctx(strike=strike, feeds=feeds_with(clock, lambda f: StaleSpot(f, age_s=300)), clock=clock)
    assert await run(s, stale) == [] and stale.skips == ["no_model"]
    later = T0 + timedelta(seconds=5)
    fresh = make_ctx(now=later, strike=strike)
    (it,) = await run(s, fresh)
    assert it.side == "yes" and s.has_decided(TICKER)


async def test_order_book_error_is_retried() -> None:
    s = Btc15mFavorite(FIXED)
    ctx = make_ctx(strike=await strike_for(0.995), book_error=True)
    assert await run(s, ctx) == [] and ctx.skips == ["no_book"] and not s.has_decided(TICKER)


async def test_blind_mode_trades_the_favourite_without_spot_data() -> None:
    ctx = make_ctx(feeds=FeedRegistry(), yes_bid="0.89", yes_ask="0.90")
    (it,) = await run(Btc15mFavorite({"use_model": False, "contracts": 15}), ctx)
    assert it.side == "yes" and it.fair_value is None and it.expected_edge == D("0.02") and it.count == 15
    assert "blind favourite (no model)" in it.reason
    # blind mode still reports the model when it can be computed, but does not filter on it
    ctx = make_ctx(strike=await strike_for(0.5), yes_bid="0.89", yes_ask="0.90")
    (it,) = await run(Btc15mFavorite({"use_model": False, **FIXED}), ctx)
    assert it.fair_value == pytest.approx(0.5, abs=0.01) and it.expected_edge < 0


# --------------------------------------------------------------------------- sizing & price


async def test_kelly_sizing_is_capped_by_dollars_and_floored_at_min_contracts() -> None:
    strike = await strike_for(0.99)
    (it,) = await run(Btc15mFavorite(), make_ctx(strike=strike))  # defaults: kelly 0.25, $50 cap
    fee = trading_fee(D("0.90"), it.count, is_taker=True)
    assert it.count == 55 and it.count * D("0.90") + fee <= 50  # 56 would cost $50.40 + fee
    small = make_ctx(strike=strike, portfolio=portfolio(equity=20))  # Kelly says a handful -> floor 10
    (it,) = await run(Btc15mFavorite(), small)
    assert it.count == 10
    (it,) = await run(Btc15mFavorite({"max_contracts": 12}), make_ctx(strike=strike))
    assert it.count == 12


async def test_order_too_small_for_the_dollar_cap_is_skipped() -> None:
    ctx = make_ctx(strike=await strike_for(0.99))
    s = Btc15mFavorite({"max_cost_per_trade": 5.0})  # 5 contracts at 0.90 < min_contracts 10
    assert await run(s, ctx) == [] and ctx.skips == ["size"] and s.has_decided(TICKER)


@pytest.mark.parametrize(("ticks", "ask", "limit"), [(0, "0.90", "0.90"), (1, "0.90", "0.901"),
                                                     (2, "0.90", "0.902"), (1, "0.89", "0.90"),
                                                     (1, "0.85", "0.86")])
async def test_slippage_ticks_follow_the_tapered_grid(ticks: int, ask: str, limit: str) -> None:
    ctx = make_ctx(strike=await strike_for(0.999), yes_bid=str(D(ask) - D("0.01")), yes_ask=ask)
    (it,) = await run(Btc15mFavorite({**FIXED, "slippage_ticks": ticks}), ctx)
    assert it.limit_price == D(limit)
    fee = trading_fee(D(limit), 20, is_taker=True)
    assert abs(it.expected_edge - (D(it.fair_value) - D(limit) - fee / 20)) < D("0.000002")  # at the limit


# --------------------------------------------------------------------------- basis cache


async def test_basis_cache_is_filled_once_and_persisted() -> None:
    clock = Clock(T0)
    feeds = replay_feeds(clock)
    s = Btc15mFavorite(FIXED)
    ctx = make_ctx(strike=await strike_for(0.995), feeds=feeds, clock=clock)
    await s.model(ctx, ctx.markets[TICKER])
    state = s.dump_state()
    assert len(state["cb_mid"]) == 56  # windows k=2..57 (the 56 newest eligible)
    close = OPEN - timedelta(minutes=15)  # k=2
    assert state["cb_mid"][str(int(close.timestamp()))] == pytest.approx(cb_mid(close))
    s2 = Btc15mFavorite(FIXED)
    s2.load_state(state)
    calls = []
    orig = feeds.crypto.candles

    async def spy(symbol: str = "BTC", minutes: int = 60, **kw: Any) -> list[SpotCandle]:
        calls.append(minutes)
        return await orig(symbol, minutes)

    feeds.crypto.candles = spy  # type: ignore[method-assign]
    mv = await s2.model(ctx, ctx.markets[TICKER])
    assert calls == [120] and mv.basis == pytest.approx(25.5, abs=0.01)
    calls.clear()
    await Btc15mFavorite(FIXED).model(ctx, ctx.markets[TICKER])  # cold start: one long fetch
    assert calls == [900]


async def test_an_unpriceable_old_window_does_not_trigger_long_fetches_every_window() -> None:
    gap_close = OPEN - timedelta(minutes=15 * 39)  # window k=40, ~10 h old
    bars = [b for b in BARS if not gap_close - timedelta(seconds=400) <= b.end <= gap_close]
    clock = Clock(T0)
    feeds = replay_feeds(clock, bars=bars)
    calls: list[int] = []
    orig = feeds.crypto.candles

    async def spy(symbol: str = "BTC", minutes: int = 60, **kw: Any) -> list[SpotCandle]:
        calls.append(minutes)
        return await orig(symbol, minutes)

    feeds.crypto.candles = spy  # type: ignore[method-assign]
    s = Btc15mFavorite(FIXED)
    ctx = make_ctx(feeds=feeds, clock=clock)
    mv = await s.model(ctx, ctx.markets[TICKER])
    assert calls == [900] and mv.basis_n == 48  # k=2..50 without k=40
    assert str(int(gap_close.timestamp())) not in s.dump_state()["cb_mid"]
    mv = await s.model(ctx, ctx.markets[TICKER])
    assert calls == [900, 120] and mv.basis_n == 48  # known gap: no second long fetch


# --------------------------------------------------------------------------- feeds


async def test_replay_feeds_never_look_ahead() -> None:
    clock = Clock(datetime(2026, 9, 27, 11, 50, 30, tzinfo=UTC))
    f = ReplayCryptoFeed({"btc": [(b.ts.timestamp(), b.open, b.high, b.low, b.close, b.volume) for b in BARS]},
                         clock=clock)
    cs = await f.candles("BTC", 5)
    assert len(cs) == 5 and cs[-1].end == datetime(2026, 9, 27, 11, 50, tzinfo=UTC)
    assert all(c.complete and c.source == "coinbase" for c in cs)
    q = await f.spot("BTC")
    assert q.price == cs[-1].close and q.ts == cs[-1].end and q.fetched_at == clock.now
    clock.now = datetime(2020, 1, 1, tzinfo=UTC)
    with pytest.raises(FeedError):
        await f.spot("BTC")
    with pytest.raises(FeedError):
        await f.candles("ETH", 5)

    ms = settled_markets(5)
    sf = ReplaySettledFeed(ms, clock=Clock(OPEN + timedelta(seconds=5)))  # k=1 settles at OPEN + 6 s
    got = await sf.settled_markets(SERIES)
    assert [m.ticker for m in got] == [m.ticker for m in ms[1:]]  # newest first, k=1 not yet settled
    sf.set_now(OPEN + timedelta(seconds=6))
    assert (await sf.settled_markets(SERIES))[0].ticker == ms[0].ticker
    assert len(await sf.settled_markets(SERIES, since=OPEN - timedelta(minutes=16))) == 2
    assert await sf.settled_markets("KXETH15M") == []


def test_replay_settled_from_research_rows() -> None:
    rows = [{"series": SERIES, "event_ticker": "KXBTC15M-26SEP261900", "ticker": "KXBTC15M-26SEP261900-00",
             "strike_type": "greater_or_equal", "floor_strike": "84275.12", "cap_strike": "",
             "open_ts": "1790462700", "close_ts": "1790463600", "result": "no", "expiration_value": "84264.76",
             "settlement_value": "0.0000", "volume": "3710179.65", "settlement_ts": "2026-09-26T23:00:06.49314Z"}]
    f = ReplaySettledFeed.from_rows(rows, now=datetime(2026, 9, 27, tzinfo=UTC))
    m = f._series[SERIES][0]
    assert m.series_ticker == SERIES and m.close_time == datetime(2026, 9, 26, 23, 0, tzinfo=UTC)
    assert m.floor_strike == D("84275.12") and m.expiration_value == "84264.76" and m.result == "no"


class FakeMarketsClient:
    def __init__(self, markets: list[Market]) -> None:
        self.markets = markets
        self.calls: list[dict[str, Any]] = []

    async def get_markets(self, **filters: Any) -> tuple[list[Market], str | None]:
        self.calls.append(filters)
        return list(self.markets), None


async def test_kalshi_settled_feed_queries_and_caches() -> None:
    ms = settled_markets(5)
    fc = FakeMarketsClient(list(reversed(ms)))
    mono = [0.0]
    f = KalshiSettledFeed(fc, ttl_s=60, clock=lambda: mono[0])
    since = OPEN - timedelta(hours=14)
    got = await f.settled_markets(SERIES, since=since)
    assert [m.ticker for m in got] == [m.ticker for m in ms]  # newest close first
    assert fc.calls == [{"series_ticker": SERIES, "status": "settled", "limit": 1000,
                         "min_settled_ts": int(since.timestamp())}]
    await f.settled_markets(SERIES, since=since + timedelta(minutes=15))  # later since: cache hit
    assert len(fc.calls) == 1
    await f.settled_markets(SERIES, since=since - timedelta(minutes=15))  # earlier since: refetch
    assert len(fc.calls) == 2
    mono[0] = 61.0
    await f.settled_markets(SERIES, since=since)
    assert len(fc.calls) == 3 and f.status()["series"][SERIES]["markets"] == 5
    await f.aclose()  # shared client: not closed by the feed


async def test_kalshi_settled_feed_with_a_bare_get_client() -> None:
    fc = FakeKalshiClient(page_size=2)
    for k, close in enumerate((OPEN, OPEN - timedelta(minutes=15), OPEN - timedelta(minutes=30))):
        fc.add_market(f"{SERIES}-Q{k}-00", close_time=close, status="finalized", result="yes")
    f = KalshiSettledFeed(fc)
    got = await f.settled_markets(SERIES)
    assert [m.close_time for m in got] == [OPEN, OPEN - timedelta(minutes=15), OPEN - timedelta(minutes=30)]
    assert fc.count("get") == 2  # paged by cursor


def test_build_feeds_registers_the_settled_feed() -> None:
    reg = build_feeds(Settings())
    assert isinstance(reg.kalshi_settled, KalshiSettledFeed) and reg.kalshi_settled.request_count == 0
    assert reg.kalshi_settled.base_url == Settings().kalshi.base_url


# --------------------------------------------------------------------------- engine round trip


async def test_engine_round_trip_fills_the_favourite(settings: Settings, fake_client: FakeKalshiClient) -> None:
    strike = await strike_for(0.99)
    fc = fake_client
    fc.add_market(TICKER, close_time=C, yes_bid="0.89", yes_ask="0.90", series_ticker=SERIES)
    fc.update_market(TICKER, open_time=iso(OPEN), strike_type="greater_or_equal", floor_strike=float(strike),
                     price_ranges=TAPER)
    fc.set_book(TICKER, yes=[("0.89", 5000)], no=[("0.10", 5000)])
    fc.set_series(SERIES)
    clock = Clock(T0)
    svc = build_services(settings, client=fc, strategies={NAME: Btc15mFavorite}, feeds=replay_feeds(clock),
                         clock=clock)
    svc.md.scanner_days_to_close = 0
    svc.engine.update_strategy(NAME, enabled=True)
    await svc.md.refresh_universe(force=True)
    info = await svc.engine.tick()
    assert info["intents"] == 1
    (order,) = svc.store.list_orders("all")
    assert order.status == "filled" and order.side == "yes" and order.filled_count == 55
    assert order.avg_fill_price == D("0.90") and order.strategy == NAME and order.fair_value is not None
    pos = svc.broker.position(TICKER, NAME)
    assert pos is not None and pos.count == 55
    (sig,) = svc.store.list_signals()
    assert sig["decision"] == "executed" and "model-confirmed" in sig["reason"]
    clock.now = T0 + timedelta(seconds=5)  # next 5 s tick inside the window: no second entry
    await svc.engine.tick()
    assert len(svc.store.list_orders("all")) == 1
    await svc.aclose()
