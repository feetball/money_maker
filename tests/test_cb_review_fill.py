"""Regression tests for the fill-realism review of the Coinbase paper broker and backtester.

Each test reproduces one finding: queue-ahead double counting when the tape lags the book,
maker fills from book movement alone, equity that ignores the exit fee, backtest slippage
for products without a spread snapshot, fills with no volume cap, and the zero-latency fill.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from test_cb_broker import identity, make
from test_cb_broker_fakes import PID, T0, TIER, FakeSpotMD, Clock, make_product, make_trade

from kalshibot.coinbase.backtest import (
    DEFAULT_SLIPPAGE_BPS,
    KNOWN_BIASES,
    SpotDataset,
    run_spot_backtest,
)
from kalshibot.coinbase.broker import SpotPaperBroker
from kalshibot.coinbase.config import CoinbaseSettings
from kalshibot.coinbase.fees import DEFAULT_TIER, fee_for
from kalshibot.coinbase.paper import SpotOrderIntent
from kalshibot.coinbase.store import SpotStore
from kalshibot.coinbase.strategies import SpotStrategy, TargetWeight


def D(x: object) -> Decimal:
    return Decimal(str(x))


def gtc(side: str, base: object, limit: object, *, strategy: str = "s1") -> SpotOrderIntent:
    return SpotOrderIntent(PID, side, base_size=D(base), order_type="limit", limit_price=D(limit),  # type: ignore[arg-type]
                           tif="gtc", post_only=True, strategy=strategy)


def fresh_md() -> FakeSpotMD:
    md = FakeSpotMD(clock=Clock())
    md.products[PID] = make_product(PID)
    return md


# --------------------------------------------------------------------------- broker: queue ahead


async def test_queue_bound_from_a_book_ahead_of_the_tape_is_not_burned_twice() -> None:
    md = fresh_md()
    md.set_book(PID, bids=[(99, 10), (98, 5)], asks=[(100, 5)])
    md.add_trades([make_trade(1, 100, 1, "sell", T0 - timedelta(seconds=30))])
    b, _ = make(md)
    await b.maintain()
    o = await b.place_order(gtc("buy", 2, 99))
    assert o.queue_ahead == D(10)
    await b.maintain()
    # a seller sells exactly the 10 ahead of us at 99 (t0+3): the fresh book already shows it...
    md.clock.advance(5)
    md.set_book(PID, bids=[(98, 5)], asks=[(100, 5)])
    assert await b.maintain() == []  # ...but the CDN-cached tape does not yet
    assert b.get_order(o.id).queue_ahead == 0
    md.clock.advance(5)
    md.add_trades([make_trade(2, 99, 10, "buy", T0 + timedelta(seconds=3))])  # the tape catches up
    fills = await b.maintain()
    assert fills == [] and b.get_order(o.id).filled_base == 0  # that print went to the 10 ahead of us
    # a later print at our price (after the book we bounded with) does reach us
    md.clock.advance(5)
    md.add_trades([make_trade(3, 99, 1, "buy", md.clock.now)])
    fills = await b.maintain()
    assert [(f.base_size, f.price, f.is_taker) for f in fills] == [(D(1), D(99), False)]
    identity(b)


async def test_pre_bound_print_larger_than_the_queue_still_reaches_us() -> None:
    md = fresh_md()
    md.set_book(PID, bids=[(99, 10)], asks=[(100, 5)])
    md.add_trades([make_trade(1, 100, 1, "sell", T0 - timedelta(seconds=30))])
    b, _ = make(md)
    await b.maintain()
    o = await b.place_order(gtc("buy", 2, 99))
    md.clock.advance(5)
    md.set_book(PID, bids=[(98, 5)], asks=[(100, 5)])  # 99 cleared by a print we have not seen yet
    await b.maintain()
    md.clock.advance(5)
    md.add_trades([make_trade(2, 99, 11, "buy", T0 + timedelta(seconds=3))])  # 10 ahead + 1 for us
    fills = await b.maintain()
    assert [(f.base_size, f.price) for f in fills] == [(D(1), D(99))]
    assert b.get_order(o.id).filled_base == D(1)


async def test_queue_bound_survives_a_restart() -> None:
    md = fresh_md()
    md.set_book(PID, bids=[(99, 10)], asks=[(100, 5)])
    md.add_trades([make_trade(1, 100, 1, "sell", T0 - timedelta(seconds=30))])
    store = SpotStore(":memory:")
    b, _ = make(md, store=store)
    await b.maintain()
    o = await b.place_order(gtc("buy", 2, 99))
    md.clock.advance(5)
    md.set_book(PID, bids=[(98, 5)], asks=[(100, 5)])
    await b.maintain()
    b2 = SpotPaperBroker(md, store, clock=md.clock, starting_balance=D(1000), fee_tier=TIER)
    md.clock.advance(5)
    md.add_trades([make_trade(2, 99, 10, "buy", T0 + timedelta(seconds=3))])
    assert await b2.maintain() == [] and b2.get_order(o.id).filled_base == 0


async def test_resting_placement_reads_a_fresh_book() -> None:
    md = fresh_md()
    md.set_book(PID, bids=[(99, 10)], asks=[(100, 5)])
    seen: list[float] = []
    orig = md.book

    async def spy(product_id: str, max_age_s: float = 2) -> Any:
        seen.append(max_age_s)
        return await orig(product_id, max_age_s)

    md.book = spy  # type: ignore[method-assign]
    b, _ = make(md)
    await b.place_order(gtc("buy", 1, 99))
    assert seen[-1] == 0  # queue_ahead comes from the current book, not a cached one


# --------------------------------------------------------------------------- broker: no fills from quotes alone


async def test_book_movement_alone_does_not_fill_by_default() -> None:
    md = fresh_md()
    md.set_book(PID, bids=[(99, 10)], asks=[(100, 5)])
    md.add_trades([make_trade(1, 100, 1, "sell", T0 - timedelta(seconds=30))])
    b, _ = make(md)
    o = await b.place_order(gtc("buy", 2, 99))
    md.clock.advance(5)
    md.set_book(PID, bids=[("98.5", 10)], asks=[("98.9", 5)])  # quotes re-priced, no print
    assert await b.maintain() == []
    assert b.get_order(o.id).status == "open"
    assert CoinbaseSettings().paper.fill_on_book_cross is False
    # opt-in keeps the old behaviour
    b2 = SpotPaperBroker(md, SpotStore(":memory:"), clock=md.clock, fee_tier=TIER,
                         settings=CoinbaseSettings(paper={"fill_on_book_cross": True}))
    md.set_book(PID, bids=[(99, 10)], asks=[(100, 5)])
    o2 = await b2.place_order(gtc("buy", 2, 99))
    md.clock.advance(5)
    md.set_book(PID, bids=[("98.5", 10)], asks=[("98.9", 5)])
    fills = await b2.maintain()
    assert [(f.base_size, f.price, f.is_taker) for f in fills] == [(D(2), D(99), False)]
    assert b2.get_order(o2.id).status == "filled"


# --------------------------------------------------------------------------- broker: exit fee in equity


async def test_equity_is_net_of_the_exit_fee() -> None:
    md = fresh_md()
    md.set_book(PID, bids=[("99.99", 50)], asks=[(100, 50)])
    b = SpotPaperBroker(md, SpotStore(":memory:"), clock=md.clock, fee_tier=DEFAULT_TIER, starting_balance=1000)
    await b.place_order(SpotOrderIntent(PID, "buy", quote_size=D(100), strategy="s"))
    md.set_book(PID, bids=[(101, 50)], asks=[("101.01", 50)])  # +1 %
    await b.mark()
    a = b.account()
    p = b.positions()[0]
    gross = p.quantity * D(101)
    assert p.exit_fee == fee_for(gross, is_taker=True, tier=DEFAULT_TIER)
    assert p.liquidation_value == gross - p.exit_fee
    assert p.mark_price == D(101)  # the exit price itself stays gross
    identity(b)
    await b.place_order(SpotOrderIntent(PID, "sell", base_size=p.quantity, strategy="s"))
    a2 = b.account()
    assert a.equity == a2.equity and a.total_pnl == a2.total_pnl < 0  # marked == what selling realizes
    assert a.to_json()["positions_exit_fee"] == float(p.exit_fee)


# --------------------------------------------------------------------------- backtester


class BuyAt(SpotStrategy):
    name = "cb_test_review_buy_at"
    description = "test: buy one product at a time, optionally sell later"
    default_params = {"at": 0, "sell_at": 0, "product": "BTC-USD", "weight": 1.0}
    param_schema = {"at": {"type": "int"}, "sell_at": {"type": "int"}, "product": {"type": "str"},
                    "weight": {"type": "float"}}
    history_bars = 1
    rebalance_band = 0.0

    def universe(self, products: Any) -> list[str]:
        return [self.params["product"]] if self.params["product"] in products else []

    def on_bar(self, ctx: Any) -> list[TargetWeight] | None:
        t = int(ctx.bar_end.timestamp())
        if t == self.params["at"]:
            return [TargetWeight(self.params["product"], self.params["weight"], "buy")]
        if self.params["sell_at"] and t == self.params["sell_at"]:
            return []
        return None


DAY = 86400
TB = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())


def flat(n: int, *, px: float = 100.0, vol: float = 1e6) -> list[tuple[int, float, float, float, float, float]]:
    return [(TB + i * DAY, px, px, px, px, vol) for i in range(n)]


def test_unmeasured_product_slippage_scales_with_liquidity() -> None:
    # MAJOR is measured at 20 bps full spread; THIN has no snapshot and $300/day of volume
    ds = SpotDataset.from_rows({"MAJOR-USD": flat(40, vol=1e6), "THIN-USD": flat(40, vol=3.0),
                                "MID-USD": flat(40, vol=1e3)},
                               spreads_bps={"MAJOR-USD": 20.0})
    at = TB + 35 * DAY
    kw = {"dataset": ds, "benchmarks": False, "start": "2024-01-02"}
    major = run_spot_backtest(BuyAt, {"at": at, "product": "MAJOR-USD", "weight": 0.05}, **kw)["trades"][0]
    thin = run_spot_backtest(BuyAt, {"at": at, "product": "THIN-USD", "weight": 0.05}, **kw)
    mid = run_spot_backtest(BuyAt, {"at": at, "product": "MID-USD", "weight": 0.05}, **kw)["trades"][0]
    t = thin["trades"][0]
    assert major["slippage_bps"] == 10.0
    # never cheaper than the widest measured product, and wider the thinner the product
    assert t["slippage_bps"] > mid["slippage_bps"] >= 10.0 > DEFAULT_SLIPPAGE_BPS
    assert t["slippage_bps"] >= 100
    s = thin["metrics"]["details"]["slippage"]
    assert s["fallback_fills"] == 1 and s["floor_bps"] == 10.0
    # a pessimistic multiplier scales every one-way cost
    m2 = run_spot_backtest(BuyAt, {"at": at, "product": "MAJOR-USD", "weight": 0.05}, slippage_multiplier=2,
                           **kw)["trades"][0]
    assert m2["slippage_bps"] == 20.0


def test_fills_are_capped_by_the_bar_volume() -> None:
    rows = [(TB + i * DAY, 1.0, 1.0, 1.0, 1.0, 1e4) for i in range(10)]
    rows[5] = (TB + 5 * DAY, 0.5, 1.0, 0.5, 1.0, 0.1)  # $0.05 traded in the fill bar, open at 0.50
    ds = SpotDataset.from_rows({"THIN-USD": rows}, spreads_bps={"THIN-USD": 1.0})
    r = run_spot_backtest(BuyAt, {"at": TB + 5 * DAY, "product": "THIN-USD"}, dataset=ds, benchmarks=False,
                          start="2024-01-02")
    assert r["trades"] == []  # 10 % of $0.05 is below min_market_funds: nothing fills
    assert r["metrics"]["total_return_pct"] < 1
    assert r["signals"][0]["decision"] in ("rejected", "unfilled")
    assert r["metrics"]["details"]["participation"]["capped_fills"] >= 1
    # a bar with $1,000 traded: at most 10 % = $100 of it is ours
    rows[5] = (TB + 5 * DAY, 1.0, 1.0, 1.0, 1.0, 1000.0)
    ds = SpotDataset.from_rows({"THIN-USD": rows}, spreads_bps={"THIN-USD": 0.0})
    r = run_spot_backtest(BuyAt, {"at": TB + 5 * DAY, "product": "THIN-USD"}, dataset=ds, benchmarks=False,
                          start="2024-01-02")
    buy = r["trades"][0]
    assert buy["notional"] <= 100.0 + 1e-9 and buy["notional"] > 99
    assert r["signals"][0]["decision"] == "partial"
    off = run_spot_backtest(BuyAt, {"at": TB + 5 * DAY, "product": "THIN-USD"}, dataset=ds, benchmarks=False,
                            start="2024-01-02", max_participation=0)
    assert off["trades"][0]["notional"] > 900


def test_backtest_equity_is_net_of_the_exit_fee() -> None:
    ds = SpotDataset.from_rows({"BTC-USD": flat(10, vol=1e6)}, spreads_bps={"BTC-USD": 0.0})
    r = run_spot_backtest(BuyAt, {"at": TB + 3 * DAY}, dataset=ds, benchmarks=False, start="2024-01-02")
    buy = r["trades"][0]
    held = Decimal(str(buy["base_size"])) * 100
    cash = 1000 - buy["notional"] - buy["fee"]
    expect = cash + float(held) * (1 - float(DEFAULT_TIER.taker_rate))
    assert r["metrics"]["final_equity"] == pytest.approx(expect, abs=1e-3)


def test_zero_latency_fill_is_documented_and_a_pessimistic_price_is_available() -> None:
    assert any("latency" in b for b in KNOWN_BIASES)
    rows = [(TB + i * DAY, 100.0, 120.0, 100.0, 120.0, 1e6) for i in range(10)]  # every bar rallies
    ds = SpotDataset.from_rows({"BTC-USD": rows}, spreads_bps={"BTC-USD": 0.0})
    base = run_spot_backtest(BuyAt, {"at": TB + 3 * DAY}, dataset=ds, benchmarks=False, start="2024-01-02")
    assert "latency" in base["metrics"]["details"]["look_ahead"]
    assert base["trades"][0]["price"] == 100.0
    pess = run_spot_backtest(BuyAt, {"at": TB + 3 * DAY}, dataset=ds, benchmarks=False, start="2024-01-02",
                             fill_price="pessimistic")
    assert pess["trades"][0]["price"] == pytest.approx((100 + 120 + 100 + 120) / 4)
    assert math.isfinite(pess["metrics"]["final_equity"])
