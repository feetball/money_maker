"""alt15m_stale: stacked probability, trigger window, edge filter, sizing by depth, entry cap,
stale-data skips, registry. Deterministic, no network."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from conftest import iso

from kalshibot.feeds.crypto import SpotCandle, SpotQuote
from kalshibot.fees import trading_fee
from kalshibot.kalshi.models import Market, Orderbook
from kalshibot.money import ONE, D
from kalshibot.strategies import REGISTRY
from kalshibot.strategies.alt15m_stale import SPECS, Alt15mStale, stacked_probability

C = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
NOW = C - timedelta(minutes=8)
TICKER = "KXDOGE15M-26SEP300800-00"
S0 = 0.10


def bars(until: datetime, minutes: int = 300) -> list[SpotCandle]:
    out, price, start = [], S0, until - timedelta(minutes=minutes)
    for i in range(minutes):
        op = price
        price = op * math.exp(0.0006 * math.sin(1.3 * i))
        out.append(SpotCandle(start + timedelta(minutes=i), op, max(op, price), min(op, price), price, 1.0,
                              source="coinbase"))
    return out


class Feed:
    def __init__(self, spot: float, now: datetime = NOW, age: float = 0.5) -> None:
        self.b, self.spot_px, self.now, self.age = bars(now), spot, now, age

    async def candles(self, symbol: str, minutes: int = 60, **kw: Any) -> list[SpotCandle]:
        return self.b

    async def spot(self, symbol: str, **kw: Any) -> SpotQuote:
        return SpotQuote(symbol, self.spot_px, None, None, self.now - timedelta(seconds=30), "coinbase",
                         self.now - timedelta(seconds=self.age))


@dataclass
class Ctx:
    now: datetime
    markets: dict[str, Market]
    feeds: Any
    books: dict[str, Orderbook]
    portfolio: Any = None
    logs: list[str] = field(default_factory=list)

    async def orderbook(self, ticker: str, max_age_s: float | None = None) -> Orderbook:
        return self.books[ticker]

    def fee(self, market: Market, price: Any, count: Any, is_taker: bool = True) -> Decimal:
        return trading_fee(D(price), D(count), is_taker=is_taker)

    def log(self, msg: str, **data: Any) -> None:
        self.logs.append(msg)


def market(strike: float, close: datetime = C) -> Market:
    return Market.from_api({
        "ticker": TICKER, "event_ticker": TICKER.rsplit("-", 1)[0], "series_ticker": "KXDOGE15M",
        "status": "active", "market_type": "binary", "open_time": iso(close - timedelta(minutes=15)),
        "close_time": iso(close), "strike_type": "greater_or_equal", "floor_strike": strike,
    })


def book(yb: str, ya: str, size: int = 40) -> Orderbook:
    return Orderbook.from_levels(TICKER, yes_bids=[(D(yb), size)], no_bids=[(ONE - D(ya), size)])


def run(spot: float, strike: float, yb: str, ya: str, *, now: datetime = NOW, size: int = 40, age: float = 0.5,
        strat: Alt15mStale | None = None):
    m = market(strike)
    ctx = Ctx(now, {TICKER: m}, {"crypto": Feed(spot, now, age)}, {TICKER: book(yb, ya, size)})
    s = strat or Alt15mStale()
    return asyncio.run(s.on_tick(ctx)), ctx, s


def test_registered_and_defaults() -> None:
    assert REGISTRY["alt15m_stale"] is Alt15mStale
    assert Alt15mStale.enabled_by_default and set(SPECS) == {"KXDOGE15M", "KXSOL15M", "KXXRP15M"}


def test_stack_is_between_mid_and_model_and_monotonic() -> None:
    sp = SPECS["KXDOGE15M"]
    assert stacked_probability(sp, 0.5, 0.5) == stacked_probability(sp, 0.5, 0.5)
    assert stacked_probability(sp, 0.9, 0.5) > stacked_probability(sp, 0.6, 0.5)
    assert stacked_probability(sp, 0.5, 0.9) > stacked_probability(sp, 0.5, 0.6)


def test_buys_yes_when_spot_is_far_above_a_stale_quote() -> None:
    out, ctx, _ = run(spot=0.1004, strike=0.1000, yb="0.48", ya="0.50")
    assert len(out) == 1
    o = out[0]
    assert (o.side, o.action, o.tif, o.limit_price, o.strategy) == ("yes", "buy", "ioc", D("0.50"), "alt15m_stale")
    assert o.count == 23 and o.expected_edge is not None and o.expected_edge >= D("0.02")


def test_buys_no_when_spot_is_far_below() -> None:
    out, _, _ = run(spot=0.0996, strike=0.1000, yb="0.48", ya="0.50")
    assert [o.side for o in out] == ["no"]


def test_no_trade_when_quote_agrees_with_spot() -> None:
    out, _, _ = run(spot=0.1000, strike=0.1000, yb="0.49", ya="0.51")
    assert out == []


def test_size_limited_by_depth_and_min_contracts() -> None:
    out, _, _ = run(spot=0.1004, strike=0.1000, yb="0.48", ya="0.50", size=12)
    assert out[0].count == 12
    out, _, _ = run(spot=0.1004, strike=0.1000, yb="0.48", ya="0.50", size=3)
    assert out == []


def test_window_and_wide_spread_and_stale_spot_skip() -> None:
    assert run(0.1004, 0.1, "0.48", "0.50", now=C - timedelta(minutes=2))[0] == []  # too close to expiry
    assert run(0.1004, 0.1, "0.48", "0.50", now=C - timedelta(minutes=14))[0] == []  # too early
    assert run(0.1004, 0.1, "0.30", "0.50")[0] == []  # spread 20c
    out, ctx, _ = run(0.1004, 0.1, "0.48", "0.50", age=10)
    assert out == [] and any("stale" in m for m in ctx.logs)


def test_price_band() -> None:
    assert run(0.1004, 0.1, "0.93", "0.95")[0] == []


def test_one_entry_per_market() -> None:
    out, ctx, s = run(0.1004, 0.1, "0.48", "0.50")
    assert out
    assert asyncio.run(s.on_tick(ctx)) == []
    s2 = Alt15mStale(); s2.load_state(s.dump_state())
    assert asyncio.run(s2.on_tick(ctx)) == []
