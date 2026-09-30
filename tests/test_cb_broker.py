"""SpotPaperBroker: taker execution, rejections, positions, cash, marks (contract §6 rules 1-3, 5-7).

Every expectation below is computed by hand in the comments. Unless stated otherwise:
TST-USD, base increment 0.01, quote increment 0.01, min funds $1; fees maker 0.5 % /
taker 1 %, rounded UP to the cent per order (``fee_for(cumulative notional)``); book
asks 100 x 1, 101 x 2, 105 x 10 / bids 99 x 1, 98 x 2, 90 x 10; $1,000 starting cash.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest
from test_cb_broker_fakes import PID, TIER, Clock, FakeSpotMD, make_product, standard_md

from kalshibot.coinbase.broker import SpotPaperBroker
from kalshibot.coinbase.config import CoinbaseSettings
from kalshibot.coinbase.fees import DEFAULT_TIER, FEE_TIERS, FeeTier
from kalshibot.coinbase.paper import SpotOrderIntent
from kalshibot.coinbase.store import SpotStore


def D(x: object) -> Decimal:
    return Decimal(str(x))


def make(md: FakeSpotMD | None = None, *, store: SpotStore | None = None, start: str = "1000",
         **kw: object) -> tuple[SpotPaperBroker, FakeSpotMD]:
    md = md or standard_md()
    b = SpotPaperBroker(md, store if store is not None else SpotStore(":memory:"), clock=md.clock,
                        starting_balance=D(start), fee_tier=kw.pop("fee_tier", TIER), **kw)  # type: ignore[arg-type]
    return b, md


def buy_q(q: object, **kw: object) -> SpotOrderIntent:
    return SpotOrderIntent(PID, "buy", quote_size=D(q), strategy=str(kw.pop("strategy", "s1")), **kw)  # type: ignore[arg-type]


def sell_b(b: object, **kw: object) -> SpotOrderIntent:
    return SpotOrderIntent(PID, "sell", base_size=D(b), strategy=str(kw.pop("strategy", "s1")), **kw)  # type: ignore[arg-type]


def identity(br: SpotPaperBroker) -> None:
    """cash + reserved + liquidation value == starting balance + realized + unrealized (exact)."""
    a = br.account()
    assert a.cash + a.reserved_cash + a.positions_liquidation_value == a.equity
    assert a.equity == a.starting_balance + a.realized_pnl + a.unrealized_pnl
    assert a.total_pnl == a.realized_pnl + a.unrealized_pnl


# --------------------------------------------------------------------------- settings / tiers


def test_settings_defaults_and_tier_resolution() -> None:
    md = standard_md()
    cb = CoinbaseSettings()
    b = SpotPaperBroker(md, SpotStore(":memory:"), settings=cb, clock=md.clock)
    assert b.tier == DEFAULT_TIER  # the lowest-volume retail tier (fees.py)
    assert b.max_slippage_bps == 100 and b.ttl_s == 300 and b.default_gtc_expiry_s == 3600
    assert b.account().starting_balance == 1000
    other = next(name for name in FEE_TIERS if FEE_TIERS[name] != DEFAULT_TIER)
    cb2 = CoinbaseSettings.model_validate({"fee_tier": other, "starting_balance": 500,
                                           "paper": {"max_slippage_bps": 25}})
    b2 = SpotPaperBroker(md, None, settings=cb2, clock=md.clock)
    assert b2.tier == FEE_TIERS[other] and b2.max_slippage_bps == 25 and b2.cash == 500
    # a full Settings object works too (its coinbase section is used)
    from kalshibot.config import Settings
    b4 = SpotPaperBroker(md, None, settings=Settings(), clock=md.clock)
    assert b4.tier == DEFAULT_TIER and b4.cash == 1000
    cb3 = CoinbaseSettings.model_validate({"fee_rates": {"maker": 0.001, "taker": 0.002}})
    b3 = SpotPaperBroker(md, None, settings=cb3, clock=md.clock)
    assert (b3.tier.maker_rate, b3.tier.taker_rate) == (D("0.001"), D("0.002"))
    assert b3.fee_tier_json()["name"] == "custom"


# --------------------------------------------------------------------------- market buy by quote


async def test_market_buy_by_quote_walks_asks_and_spends_quote_incl_fee() -> None:
    b, md = make()
    o = await b.place_order(buy_q(150))
    # level 100 x 1: 1.00 unit -> notional 100, fee_for(100) = 1.00, cost 101 (<= 150)
    # level 101: largest k (0.01 units) with 100 + 1.01k + ceil(1% of it) <= 150:
    #   k=49 -> 149.49 + 1.50 = 150.99 > 150; k=48 -> 148.48 + 1.49 = 149.97 -> 0.48 @ 101
    assert o.status == "filled" and o.decision == "executed"
    assert o.filled_base == D("1.48") and o.filled_quote == D("148.48")
    assert o.fees == D("1.49")  # ceil(1.4848) once per order, not per level
    fills = b.store.list_fills()[::-1]  # type: ignore[union-attr]
    assert [(f.price, f.base_size, f.fee, f.is_taker) for f in fills] == [
        (D(100), D("1.00"), D("1.00"), True), (D(101), D("0.48"), D("0.49"), True)]  # 1.49 - 1.00
    assert all(f.fee_rate == D("0.01") for f in fills)
    assert b.cash == D("850.03")  # 1000 - 149.97
    (p,) = b.positions()
    assert p.quantity == D("1.48") and p.cost_basis == D("149.97")  # fee-inclusive
    assert p.avg_cost == D("149.97") / D("1.48")
    # liquidation: 1 x 99 + 0.48 x 98 = 146.04 gross, net of the exit fee ceil(1.4604) = 1.47 -> 144.57;
    # mid 99.5 -> 1.48 x 99.5 = 147.26 (no fee)
    assert p.liquidation_value == D("144.57") and p.exit_fee == D("1.47") and p.mid_value == D("147.26")
    assert p.mark_price == D("146.04") / D("1.48")  # the exit price itself is before the fee
    a = b.account()
    assert a.equity == D("994.60") and a.unrealized_pnl == D("-5.40") and a.equity_mid == D("997.29")
    assert a.positions_exit_fee == D("1.47")
    assert a.fees_paid == D("1.49") and a.fills == 2
    identity(b)


async def test_consumed_liquidity_is_not_taken_twice() -> None:
    b, md = make()
    await b.place_order(buy_q(150))  # took 1 @ 100 and 0.48 @ 101
    assert b.consumed(PID) == {(PID, "ask", D(100)): D("1.00"), (PID, "ask", D(101)): D("0.48")}
    o = await b.place_order(buy_q(50))
    # 100 is used up (1 - 1 = 0); 101 has 2 - 0.48 = 1.52 left: k=49 -> 49.49 + 0.50 = 49.99 <= 50
    assert [(f.price, f.base_size) for f in b.store.list_fills(order_id=o.id)] == [(D(101), D("0.49"))]  # type: ignore[union-attr]
    assert o.fees == D("0.50") and o.status == "filled"
    assert b.consumed(PID)[(PID, "ask", D(101))] == D("0.97")


async def test_consumed_liquidity_shrinks_and_expires_like_kalshi() -> None:
    b, md = make()
    await b.place_order(buy_q(150))
    # others traded at 101: the level now shows 0.3 < our 0.48 -> our entry shrinks to 0.3
    md.set_book(PID, bids=[(99, 1)], asks=[(100, 1), (101, "0.3"), (105, 10)])
    await b.mark()
    assert b.consumed(PID)[(PID, "ask", D(101))] == D("0.3")
    # unchanged level after the TTL: never re-harvested (no evidence of a re-quote)
    md.clock.advance(301)
    await b.mark()
    assert (PID, "ask", D(100)) in b.consumed(PID)
    # the level grows (makers re-quoted) and the TTL since our take has passed -> dropped
    md.set_book(PID, bids=[(99, 1)], asks=[(100, 5), (101, "0.3"), (105, 10)])
    await b.mark()
    assert (PID, "ask", D(100)) not in b.consumed(PID)
    # a vanished level drops its entry
    md.set_book(PID, bids=[(99, 1)], asks=[(100, 5), (105, 10)])
    await b.mark()
    assert b.consumed(PID) == {}


async def test_market_buy_slippage_cap_leaves_partial() -> None:
    md = standard_md()
    md.set_book(PID, bids=[(99, 1)], asks=[(100, 1), ("101.5", 5)])
    b, _ = make(md)
    o = await b.place_order(buy_q(500))
    # cap = best ask 100 x (1 + 100 bps) = 101.00 < 101.5 -> only 1 @ 100 (cost 101); remainder cancelled
    assert o.status == "cancelled" and o.decision == "partial"
    assert o.filled_base == D(1) and "slippage cap" in o.status_reason
    assert b.cash == D(899)
    b2, _ = make(standard_md(), max_slippage_bps=200)  # cap 102 -> 1 @ 100 + more at 101
    o2 = await b2.place_order(buy_q(500))
    assert o2.filled_base > D(1)


async def test_market_buy_with_no_asks_is_unfilled() -> None:
    md = standard_md()
    md.set_book(PID, bids=[(99, 1)], asks=[])
    b, _ = make(md)
    o = await b.place_order(buy_q(50))
    assert o.status == "cancelled" and o.decision == "unfilled" and o.filled_base == 0
    assert b.cash == D(1000)


async def test_quote_too_small_for_one_increment() -> None:
    md = standard_md(base_increment="1", min_market_funds="0.5")
    b, _ = make(md)
    o = await b.place_order(buy_q("50"))
    # one unit costs 100 + 1.00 fee > 50
    assert o.status == "cancelled" and o.decision == "unfilled" and "too small" in o.status_reason


# --------------------------------------------------------------------------- base-sized, sells, realized P&L


async def test_round_trip_realized_pnl_and_identity() -> None:
    b, md = make()
    o1 = await b.place_order(SpotOrderIntent(PID, "buy", base_size=D(2), order_type="limit", limit_price=D(101),
                                             strategy="s1"))
    # 1 @ 100 (fee 1.00) + 1 @ 101 (cumulative fee_for(201) = 2.01 -> 1.01): cost 203.01
    assert o1.status == "filled" and o1.fees == D("2.01") and b.cash == D("796.99")
    (p,) = b.positions()
    assert p.cost_basis == D("203.01") and p.avg_cost == D("101.505")
    identity(b)

    o2 = await b.place_order(sell_b(1))
    # market sell floor = 99 x (1 - 1 %) = 98.01 -> 1 @ 99: notional 99, fee 0.99, proceeds 98.01
    # realized = 99 - 0.99 - 101.505 (avg cost x 1) = -3.495
    assert o2.status == "filled" and o2.realized_pnl == D("-3.495")
    assert b.cash == D("895.00") and b.realized_pnl == D("-3.495")
    (p,) = b.positions()
    assert p.quantity == D(1) and p.cost_basis == D("101.505") and p.realized_pnl == D("-3.495")
    # liquidation nets out our own consumed bid at 99: 1 x 98 = 98, less the exit fee 0.98 = 97.02
    # -> unrealized 97.02 - 101.505 = -4.485
    a = b.account()
    assert a.positions_liquidation_value == D("97.02") and a.unrealized_pnl == D("-4.485")
    assert a.equity == D("992.02") and a.trades == 1 and a.wins == 0 and a.win_rate == 0.0
    identity(b)

    o3 = await b.place_order(SpotOrderIntent(PID, "sell", base_size=D(1), order_type="limit", limit_price=D(98),
                                             strategy="s1"))
    # bid 99 consumed -> 1 @ 98: fee 0.98, proceeds 97.02; realized 98 - 0.98 - 101.505 = -4.485
    assert o3.realized_pnl == D("-4.485") and b.cash == D("992.02")
    assert b.positions() == []
    a = b.account()
    assert a.realized_pnl == D("-7.98") and a.equity == D("992.02") and a.fees_paid == D("3.98")
    assert a.trades == 2 and a.open_positions == 0
    identity(b)
    # the closed position row keeps its history in the store
    row = b.store.get_position("s1", PID)  # type: ignore[union-attr]
    assert row is not None and row.quantity == 0 and row.realized_pnl == D("-7.98") and row.fees_paid == D("3.98")


async def test_winning_trade_counts_and_reopen_keeps_history() -> None:
    b, md = make()
    await b.place_order(SpotOrderIntent(PID, "buy", base_size=D(1), order_type="limit", limit_price=D(100),
                                        strategy="s1"))  # cost 100 + 1.00
    md.set_book(PID, bids=[(110, 5)], asks=[(111, 5)])
    o = await b.place_order(sell_b(1))
    # 1 @ 110: fee 1.10 -> realized 110 - 1.10 - 101 = 7.90
    assert o.realized_pnl == D("7.90")
    a = b.account()
    assert (a.trades, a.wins, a.win_rate) == (1, 1, 1.0)
    await b.place_order(SpotOrderIntent(PID, "buy", base_size=D(1), order_type="limit", limit_price=D(111),
                                        strategy="s1"))  # 111 + 1.11
    (p,) = b.positions()
    assert p.quantity == D(1) and p.cost_basis == D("112.11") and p.realized_pnl == D("7.90")
    assert p.fees_paid == D("3.21")  # 1.00 + 1.10 + 1.11
    stats = b.strategy_stats()["s1"]
    assert stats["trades"] == 1 and stats["wins"] == 1 and stats["realized_pnl"] == D("7.90")
    assert stats["fills"] == 3 and stats["fees"] == D("3.21") and stats["open_positions"] == 1


async def test_positions_are_per_strategy_and_share_cash() -> None:
    b, md = make()
    await b.place_order(buy_q(50, strategy="a"))
    await b.place_order(buy_q(50, strategy="b"))
    assert {p.strategy for p in b.positions()} == {"a", "b"}
    assert [p.strategy for p in b.positions("a")] == ["a"]
    # strategy b cannot sell a's holding (no shorting per strategy)
    qa = b.positions("a")[0].quantity
    o = await b.place_order(sell_b(qa + qa, strategy="b"))
    assert o.status == "rejected" and "no shorting" in o.status_reason
    identity(b)


async def test_pro_rata_liquidation_across_strategies() -> None:
    md = standard_md()
    md.set_book(PID, bids=[(99, 10)], asks=[(100, 10)])
    b, _ = make(md)
    await b.place_order(SpotOrderIntent(PID, "buy", base_size=D(1), order_type="limit", limit_price=D(100),
                                        strategy="a"))
    await b.place_order(SpotOrderIntent(PID, "buy", base_size=D(3), order_type="limit", limit_price=D(100),
                                        strategy="b"))
    md.set_book(PID, bids=[(99, 2), (90, 10)], asks=[(100, 10)])
    await b.mark()
    # 4 units walk together: 2 x 99 + 2 x 90 = 378; a gets 1/4 = 94.5, b the remainder 283.5;
    # each nets its own exit fee: 94.5 - 0.95 and 283.5 - 2.84
    vals = {p.strategy: p.liquidation_value for p in b.positions()}
    assert vals == {"a": D("93.55"), "b": D("280.66")}
    assert b.account().positions_liquidation_value == D("374.21")  # 378 less 0.95 + 2.84 exit fees
    identity(b)


async def test_liquidation_beyond_depth_uses_deepest_bid_and_unmarked_uses_cost() -> None:
    md = standard_md()
    md.set_book(PID, bids=[], asks=[(100, 10)])  # no bids: nothing to mark with
    b, _ = make(md)
    await b.place_order(SpotOrderIntent(PID, "buy", base_size=D(5), order_type="limit", limit_price=D(100),
                                        strategy="s1"))  # 500 + 5.00 fee
    (p,) = b.positions()
    assert p.liquidation_value is None and p.value == D("505.00") and p.unrealized_pnl == 0
    identity(b)
    md.set_book(PID, bids=[(99, 1), (98, 2)], asks=[(100, 10)])
    await b.mark()
    # 1 x 99 + 2 x 98 + (5 - 3) x 98 (deepest stored bid) = 491, less the exit fee 4.91
    assert b.positions()[0].liquidation_value == D("486.09")
    # an empty bid side later keeps the previous ladder
    md.set_book(PID, bids=[], asks=[(100, 10)])
    await b.mark()
    assert b.positions()[0].liquidation_value == D("486.09")
    identity(b)


# --------------------------------------------------------------------------- rejections


@pytest.mark.parametrize(("intent", "why"), [
    (SpotOrderIntent(PID, "hold", quote_size=D(10)), "side must be"),  # type: ignore[arg-type]
    (SpotOrderIntent(PID, "buy", quote_size=D(10), order_type="stop"), "order_type"),  # type: ignore[arg-type]
    (SpotOrderIntent(PID, "buy", quote_size=D(10), tif="gtc"), "immediate-or-cancel"),
    (SpotOrderIntent(PID, "buy", base_size=D(1), order_type="limit"), "positive limit_price"),
    (SpotOrderIntent(PID, "buy", base_size=D(1), order_type="limit", limit_price=D(99), post_only=True),
     "post_only needs a GTC"),
    (SpotOrderIntent(PID, "buy", quote_size=D(10), base_size=D(1), order_type="limit", limit_price=D(99)),
     "not both"),
    (SpotOrderIntent(PID, "buy", base_size=D(1)), "market buys are sized by quote_size"),
    (SpotOrderIntent(PID, "sell", quote_size=D(10)), "sells are sized by base_size"),
    (SpotOrderIntent(PID, "buy", quote_size=D(-5)), "must be positive"),
    (SpotOrderIntent("", "buy", quote_size=D(5)), "product_id"),
])
async def test_shape_rejections(intent: SpotOrderIntent, why: str) -> None:
    b, md = make()
    o = await b.place_order(intent)
    assert o.status == "rejected" and why in o.status_reason and o.decision == "rejected"
    assert b.cash == D(1000) and b.positions() == []
    assert b.store.get_order(o.id).status == "rejected"  # type: ignore[union-attr]  # rejections are recorded too
    assert not any(c[0] == "book" for c in md.calls)  # rejected before any request


@pytest.mark.parametrize(("kw", "why"), [
    ({"status": "offline"}, "not tradable"),
    ({"trading_disabled": True}, "not tradable"),
    ({"cancel_only": True}, "not tradable"),
    ({"limit_only": True}, "limit-only"),
    ({"post_only": True}, "post-only"),
])
async def test_product_state_rejections(kw: dict, why: str) -> None:
    b, md = make(standard_md(**kw))
    o = await b.place_order(buy_q(50))
    assert o.status == "rejected" and why in o.status_reason


async def test_limit_only_product_accepts_limit_orders() -> None:
    b, md = make(standard_md(limit_only=True))
    o = await b.place_order(SpotOrderIntent(PID, "buy", base_size=D(1), order_type="limit", limit_price=D(100)))
    assert o.status == "filled"


async def test_product_lookup_failure_and_stale_books() -> None:
    md = standard_md()
    md.fail_products.add(PID)
    b, _ = make(md)
    o = await b.place_order(buy_q(50))
    assert o.status == "rejected" and "unavailable" in o.status_reason
    md.fail_products.clear()
    # a book 10 s old (> 2 + 3 s) is re-fetched once with max_age_s=0; the fresh one is used
    md.stale_books[PID] = md.clock.now - timedelta(seconds=10)
    o = await b.place_order(buy_q(50))
    assert o.status == "filled"
    assert ("book", PID, 0) in md.calls
    # ... and if the re-fetch is stale too, the order is rejected
    md.fresh_on_refetch = False
    o = await b.place_order(buy_q(50))
    assert o.status == "rejected" and "stale" in o.status_reason


async def test_increment_rounding() -> None:
    b, md = make()
    o = await b.place_order(SpotOrderIntent(PID, "buy", base_size=D("1.239"), order_type="limit",
                                            limit_price=D("100.567")))
    # base down to 1.23; buy limit down to 100.56 -> 1 @ 100 + 0.23 @ ... 101 > 100.56 -> only 1 @ 100
    assert o.base_size == D("1.23") and o.limit_price == D("100.56")
    assert o.filled_base == D(1) and o.decision == "partial"
    o2 = await b.place_order(SpotOrderIntent(PID, "sell", base_size=D("0.999"), order_type="limit",
                                             limit_price=D("98.001")))
    # sell limit UP to 98.01 (never more aggressive than intended); base down to 0.99
    assert o2.limit_price == D("98.01") and o2.base_size == D("0.99")
    o3 = await b.place_order(SpotOrderIntent(PID, "buy", base_size=D("0.004"), order_type="limit",
                                             limit_price=D(100)))
    assert o3.status == "rejected" and "below base_increment" in o3.status_reason
    o4 = await b.place_order(buy_q("0.004"))
    assert o4.status == "rejected" and "below quote_increment" in o4.status_reason


async def test_min_market_funds() -> None:
    b, md = make(standard_md(min_market_funds="10"))
    o = await b.place_order(buy_q("9.99"))
    assert o.status == "rejected" and "min_market_funds" in o.status_reason
    o = await b.place_order(SpotOrderIntent(PID, "buy", base_size=D("0.09"), order_type="limit", limit_price=D(100)))
    assert o.status == "rejected" and "min_market_funds" in o.status_reason  # 0.09 x 100 = 9 < 10
    await b.place_order(buy_q(150))
    o = await b.place_order(sell_b("0.1"))  # 0.1 x best bid 99 = 9.9 < 10
    assert o.status == "rejected" and "min_market_funds" in o.status_reason


async def test_no_shorting_and_insufficient_cash() -> None:
    b, md = make(start="120")
    o = await b.place_order(sell_b(1))
    assert o.status == "rejected" and "no shorting" in o.status_reason
    o = await b.place_order(buy_q(150))
    assert o.status == "rejected" and "insufficient cash" in o.status_reason and b.cash == D(120)
    # base-sized limit buy: bound = 2 x 101 + ceil(2.02) = 204.02 > 120
    o = await b.place_order(SpotOrderIntent(PID, "buy", base_size=D(2), order_type="limit", limit_price=D(101)))
    assert o.status == "rejected" and "insufficient cash: need 204.02" in o.status_reason
    o = await b.place_order(buy_q(101))  # exactly 1 @ 100 + 1.00
    assert o.status == "filled" and b.cash == D(19)
    o = await b.place_order(sell_b("1.01"))
    assert o.status == "rejected" and "no shorting" in o.status_reason
    identity(b)


async def test_store_failure_rejects_as_not_recorded_and_restores_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    b, md = make()
    before = b.account()

    def boom(*a: object, **k: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(b.store, "insert_fill", boom)
    o = await b.place_order(buy_q(150))
    assert o.status == "rejected" and o.status_reason.startswith("not recorded")
    assert b.cash == before.cash and b.positions() == [] and b.consumed() == {}
    assert b.store.get_order(o.id) is None  # type: ignore[union-attr]
    monkeypatch.undo()
    o = await b.place_order(buy_q(150))
    assert o.status == "filled"
    identity(b)


# --------------------------------------------------------------------------- views / snapshots


async def test_portfolio_view_and_positions_json() -> None:
    b, md = make()
    await b.place_order(buy_q(150, strategy="a"))
    pv = b.portfolio_view("a", allocation_pct=50)
    a = b.account()
    # allocation is sized before exit fees (holdings are planned at mids)
    assert pv.alloc_equity == (a.equity + a.positions_exit_fee) / 2 and pv.equity == a.equity and pv.cash == a.cash
    assert pv.holdings == {PID: D("1.48")} and pv.values == {PID: D("144.57")}  # net of the exit fee
    assert pv.best_bids[PID] == D(99) and pv.best_asks[PID] == D(100) and pv.mids[PID] == D("99.5")
    assert pv.exposure(product_id=PID) == D("144.57") and pv.total_exposure == D("144.57")
    assert b.portfolio_view("b").positions == () and len(b.portfolio_view("b").all_positions) == 1
    (row,) = b.positions_json()
    assert row["venue"] == "coinbase" and row["quantity"] == 1.48 and row["weight_of_strategy"] == 1.0
    assert row["url"] == "https://www.coinbase.com/advanced-trade/spot/TST-USD"
    (row,) = b.positions_json({"a": D("289.14")})
    assert row["weight_of_strategy"] == 0.5  # 144.57 / 289.14
    assert row["exit_fee"] == 1.47 and row["liquidation_value_gross"] == 146.04
    acct = a.to_json()
    assert acct["venue"] == "coinbase" and set(acct) >= {
        "starting_balance", "cash", "reserved_cash", "positions_liquidation_value", "positions_mid_value",
        "equity", "equity_mid", "realized_pnl", "unrealized_pnl", "fees_paid", "total_pnl", "total_return_pct",
        "todays_pnl", "max_drawdown_pct", "open_positions", "open_orders", "trades", "win_rate"}


async def test_marks_equity_snapshot_drawdown_and_todays_pnl() -> None:
    b, md = make()
    await b.place_order(SpotOrderIntent(PID, "buy", base_size=D(1), order_type="limit", limit_price=D(100)))
    # cost 101, cash 899; mark 1 x 99 - exit fee 0.99 -> equity 997.01 (the first account() of the
    # day sets day start)
    snap = b.equity_snapshot()
    assert snap["equity"] == 997.01 and snap["venue"] == "coinbase"
    md.set_book(PID, bids=[(89, 5)], asks=[(90, 5)])
    await b.mark()
    snap = b.equity_snapshot()
    # equity 899 + 89 - 0.89 = 987.11: drawdown from the 997.01 peak = 9.90 / 997.01
    assert snap["equity"] == 987.11
    a = b.account()
    assert a.todays_pnl == D("-9.90") and abs(float(a.max_drawdown_pct) - 990 / 997.01) < 1e-9
    rows = b.store.list_equity()  # type: ignore[union-attr]
    assert [r["equity"] for r in rows] == [D("997.01"), D("987.11")]
    # a new UTC day starts a new day-start equity
    md.clock.advance(86400)
    assert b.account().todays_pnl == 0


async def test_events_are_emitted_after_commit() -> None:
    b, md = make()
    seen: list[str] = []
    unsub = b.subscribe(lambda kind, obj: seen.append(kind))
    await b.place_order(buy_q(150))
    assert seen == ["fill", "fill", "order"]
    unsub()
    await b.place_order(buy_q(10))
    assert len(seen) == 3


async def test_reset_wipes_account() -> None:
    b, md = make()
    await b.place_order(buy_q(150))
    a = b.reset(D(500))
    assert a.equity == D(500) and a.cash == D(500) and b.positions() == [] and b.consumed() == {}
    assert b.store.list_fills() == [] and b.store.get_account()["cash"] == D(500)  # type: ignore[union-attr,index]


async def test_realistic_btc_intro_tier() -> None:
    """BTC-USD with real increments and a 0.60 % / 1.20 % retail tier."""
    md = FakeSpotMD(clock=Clock())
    md.products["BTC-USD"] = make_product("BTC-USD", base_increment="0.00000001", quote_increment="0.01",
                                          min_market_funds="1")
    md.set_book("BTC-USD", bids=[("84475.94", "0.5")], asks=[("84475.95", "0.01"), ("84476.10", "2")])
    b = SpotPaperBroker(md, SpotStore(":memory:"), clock=md.clock,
                        fee_tier=FeeTier("retail", D("0.006"), D("0.012")))
    o = await b.place_order(SpotOrderIntent("BTC-USD", "buy", quote_size=D(100), strategy="s"))
    # 100 / 1.012 = 98.8142 of notional -> 0.00116972 BTC (84475.95: 0.01 BTC available is plenty)
    # 0.00116972 x 84475.95 = 98.81322... + fee ceil(1.18575...) = 1.19 -> 99.99... <= 100
    assert o.status == "filled" and o.fees == D("1.19")
    assert o.filled_quote + o.fees <= D(100)
    one_more = (o.filled_base + D("0.00000001")) * D("84475.95")
    assert one_more + (one_more * D("0.012")).quantize(D("0.01"), rounding="ROUND_CEILING") > D(100)
    assert b.cash == D(1000) - o.filled_quote - o.fees
    identity(b)
