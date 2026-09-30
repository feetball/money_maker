"""PaperBroker fill simulation (ARCHITECTURE.md §6 rules 1-6, 9, 10).

Every expected number is computed by hand in the comments. Fee arithmetic follows
docs.kalshi.com/getting_started/fee_rounding with precision $0.01 unless stated:
per fill ``trade_fee = ceil_6dp(raw)``, ``aligned = floor_cent(-principal - trade_fee)``,
``rounding = gross - aligned`` into the order accumulator, rebates in whole cents.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

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


def buy(ticker, side, price, count, **kw):
    return Intent(ticker=ticker, side=side, limit_price=D(price), count=count, **kw)


@pytest.fixture
def clock():
    return ManualClock(T0)


@pytest.fixture
def md(clock):
    m = StaticMarketData([make_market(A), make_market(B)], clock=clock)
    # YES asks 0.42 x10, 0.43 x15, 0.45 x100, 0.46 x50 (mirrored NO bids); YES bid 0.40 x50
    m.set_book(A, yes_bids=[("0.40", 50)], no_bids=[("0.58", 10), ("0.57", 15), ("0.55", 100), ("0.54", 50)])
    return m


@pytest.fixture
def broker(md, clock):
    return PaperBroker(md, Store(":memory:"), starting_balance=1000, clock=clock)


def fills(broker, order_id=None):
    return sorted(broker.store.list_fills(limit=None, order_id=order_id), key=lambda f: f.id)


# --------------------------------------------------------------------------- rule 1 + 2: taker walk


async def test_multi_level_taker_fill_and_fees(broker, md):
    o = await broker.place_order(buy(A, "yes", "0.45", 30))
    # rule 1: the broker fetched the book itself with max_age_s=2
    assert ("orderbook", A, 2.0) in md.calls
    assert o.status == "filled" and o.filled_count == 30
    got = [(f.price, f.count, f.fee, f.is_taker) for f in fills(broker)]
    # fill 1: 10 @ .42 raw .17052 -> gross -4.37052 -> -4.38, rounding .00948 -> fee .18
    # fill 2: 15 @ .43 raw .257355 -> -6.707355 -> -6.71, acc .012125 -> rebate .01 -> fee .25
    # fill 3:  5 @ .45 raw .086625 -> -2.336625 -> -2.34, acc .0055 -> fee .09
    assert got == [(D("0.42"), 10, D("0.18"), True), (D("0.43"), 15, D("0.25"), True),
                   (D("0.45"), 5, D("0.09"), True)]
    assert o.fees == D("0.52")  # == ceil_cent(0.5145), the cumulative per-order rule
    assert o.avg_fill_price == D("0.43")  # 12.90 / 30
    assert broker.cash == D("986.58")  # 1000 - 12.90 - 0.52
    p = broker.position(A, "s1")
    assert (p.side, p.count, p.cost_basis, p.open_fees) == ("yes", 30, D("12.90"), D("0.52"))


async def test_multi_level_fees_direct_member_precision(md, clock):
    b = PaperBroker(md, None, starting_balance=1000, clock=clock, fee_precision=D("0.0001"))
    o = await b.place_order(buy(A, "yes", "0.45", 30))
    # .17052 -> .1706 ; .257355 -> .2574 - rebate .0001 = .2573 ; .086625 -> .0867 - .0001 = .0866
    assert o.fees == D("0.5145")  # exactly the raw sum (0.17052 + 0.257355 + 0.086625)
    assert b.cash == D("1000") - D("12.90") - D("0.5145")


async def test_limit_respected_and_remainder_cancelled(broker):
    o = await broker.place_order(buy(A, "yes", "0.43", 30))
    assert [(f.price, f.count) for f in fills(broker)] == [(D("0.42"), 10), (D("0.43"), 15)]
    assert o.status == "cancelled" and o.filled_count == 25 and o.decision == "partial"
    assert "unfilled" in o.status_reason
    assert o.fees == D("0.43")  # .18 + .25
    o2 = await broker.place_order(buy(A, "yes", "0.41", 5))
    assert o2.filled_count == 0 and o2.status == "cancelled" and o2.decision == "unfilled"


async def test_sell_is_buy_of_other_side(broker, md):
    # NO asks mirror YES bids: YES bid .40 x50 -> NO ask .60
    o = await broker.place_order(buy(A, "yes", "0.40", 5, action="sell"))  # sell YES @ .40 == buy NO @ .60
    assert o.status == "filled"
    f = fills(broker)[0]
    assert (f.side, f.action, f.price, f.count) == ("yes", "sell", D("0.40"), 5)
    p = broker.position(A, "s1")
    assert (p.side, p.count, p.cost_basis) == ("no", 5, D("3.00"))  # opened NO at .60
    assert o.fees == D("0.09")  # ceil(.07*5*.4*.6 = .084)


# --------------------------------------------------------------------------- rule 3: consumed liquidity


async def test_consumed_liquidity_then_ttl_expiry(broker, md, clock):
    o1 = await broker.place_order(buy(A, "yes", "0.42", 8))
    assert o1.filled_count == 8
    clock.advance(10)
    o2 = await broker.place_order(buy(A, "yes", "0.42", 5))
    assert o2.filled_count == 2  # displayed 10 - consumed 8
    assert broker.consumed(A) == {(A, "yes", D("0.42")): D("10")}
    clock.advance(299)  # 309 s after first use, 299 s after the last use -> still consumed
    o3 = await broker.place_order(buy(A, "yes", "0.42", 5))
    assert o3.filled_count == 0
    clock.advance(1)  # 300 s after the last use, but the level is unchanged: a stale offer stays taken
    o4 = await broker.place_order(buy(A, "yes", "0.42", 5))
    assert o4.filled_count == 0
    # the level is re-quoted larger (13 > the 10 shown after our last take) and the TTL has
    # passed: the makers are assumed to have refreshed it, so all of it is available again
    md.set_book(A, yes_bids=[("0.40", 50)], no_bids=[("0.58", 13), ("0.57", 15)])
    o5 = await broker.place_order(buy(A, "yes", "0.42", 13))
    assert o5.filled_count == 13


async def test_consumed_survives_level_shrink(broker, md):
    await broker.place_order(buy(A, "yes", "0.42", 8))
    # others trade 5 of the displayed 10 at .42: they came out of the 2 we did not take (and 3 of
    # "ours"), so nothing is left for us - the consumed amount shrinks to the displayed 5
    md.set_book(A, yes_bids=[("0.40", 50)], no_bids=[("0.58", 5), ("0.57", 15)])
    o = await broker.place_order(buy(A, "yes", "0.42", 5))
    assert o.filled_count == 0
    assert broker.consumed(A) == {(A, "yes", D("0.42")): D("5")}
    # 4 new contracts join the level: displayed 9 - consumed 5 -> 4 available
    md.set_book(A, yes_bids=[("0.40", 50)], no_bids=[("0.58", 9)])
    o = await broker.place_order(buy(A, "yes", "0.42", 9))
    assert o.filled_count == 4
    # the level disappears: the consumption is gone with it
    md.set_book(A, yes_bids=[("0.40", 50)], no_bids=[("0.57", 15)])
    await broker.place_order(buy(A, "yes", "0.42", 1))
    assert broker.consumed(A) == {}


# --------------------------------------------------------------------------- rule 4: resting maker orders


async def test_maker_queue_ahead(broker, md, clock):
    md.add_trade(A, "0.40", 100, T0 - timedelta(seconds=1))  # printed before placement: ignored
    o = await broker.place_order(buy(A, "yes", "0.40", 10, tif="gtc"))
    assert o.status == "open" and o.filled_count == 0
    assert o.queue_ahead == D("50")  # displayed YES bids at .40
    assert o.reserved == D("4.01")  # 10 x .40 + maker fee 0 (quadratic) + .01 buffer
    assert broker.cash == D("995.99")
    md.add_trade(A, "0.40", 30, T0 + timedelta(seconds=5))  # queue 50 -> 20
    md.add_trade(A, "0.40", 25, T0 + timedelta(seconds=6))  # queue 20 -> 0, excess 5 fills us
    md.add_trade(A, "0.41", 100, T0 + timedelta(seconds=7))  # above our bid: nothing
    md.add_trade(A, "0.39", 100, T0 + timedelta(seconds=8), is_block_trade=True)  # block trades excluded
    clock.advance(10)
    new = await broker.process_resting_orders()
    assert [(f.count, f.price, f.is_taker, f.fee) for f in new] == [(5, D("0.40"), False, D("0"))]
    assert new[0].ts == T0 + timedelta(seconds=6)
    o = broker.get_order(o.id)
    assert (o.status, o.filled_count, o.queue_ahead) == ("partially_filled", 5, D("0"))
    assert o.reserved == D("2.01")
    assert broker.cash == D("995.99")  # 1000 - 2.00 paid - 2.01 still reserved
    # nothing new printed -> no double counting
    clock.advance(10)
    assert await broker.process_resting_orders() == []


async def test_maker_trade_through_and_maker_fees(broker, md, clock):
    md.set_series("KXTEST", "quadratic_with_maker_fees", 1)
    o = await broker.place_order(buy(A, "yes", "0.40", 10, tif="gtc"))
    # reserve 4.00 + ceil(.0175*10*.4*.6 = .042) = .05 + .01
    assert o.reserved == D("4.06") and o.queue_ahead == D("50")
    md.add_trade(A, "0.38", 4, T0 + timedelta(seconds=1))  # strictly through: fills 4 despite the queue
    clock.advance(2)
    f1 = await broker.process_resting_orders()
    # maker raw .0175*4*.24 = .0168 -> gross -1.6168 -> -1.62, acc .0032 -> fee .02
    assert [(f.count, f.price, f.fee, f.is_taker) for f in f1] == [(4, D("0.40"), D("0.02"), False)]
    assert broker.get_order(o.id).reserved == D("2.44")  # 6 x .40 + ceil(.0252) + .01
    md.add_trade(A, "0.39", 20, T0 + timedelta(seconds=3))
    clock.advance(2)
    f2 = await broker.process_resting_orders()
    # raw .0252 -> gross -2.4252 -> -2.43, acc .008 -> fee .03 ; total .05 == ceil(.042)
    assert [(f.count, f.fee) for f in f2] == [(6, D("0.03"))]
    o = broker.get_order(o.id)
    assert o.status == "filled" and o.fees == D("0.05") and o.reserved == 0
    assert broker.cash == D("995.95")  # 1000 - 4.00 - 0.05


async def test_crossing_book_fills_at_our_limit(broker, md, clock):
    o = await broker.place_order(buy(A, "yes", "0.40", 10, tif="gtc"))
    assert o.status == "open"
    # the book now offers YES at .38 x3 and .39 x20 (NO bids .62 / .61): it crosses our .40 bid
    md.set_book(A, yes_bids=[("0.35", 10)], no_bids=[("0.62", 3), ("0.61", 20)])
    clock.advance(5)
    new = await broker.process_resting_orders()
    assert [(f.count, f.price, f.is_taker) for f in new] == [(3, D("0.40"), False), (7, D("0.40"), False)]
    assert broker.get_order(o.id).status == "filled"
    # the crossing liquidity we used is consumed: .38 gone, 13 left at .39
    o2 = await broker.place_order(buy(A, "yes", "0.39", 20))
    assert [(f.price, f.count) for f in fills(broker, o2.id)] == [(D("0.39"), 13)]


async def test_own_orders_share_a_print_in_time_priority(broker, md, clock):
    a = await broker.place_order(buy(A, "yes", "0.40", 10, tif="gtc"))
    clock.advance(1)
    b = await broker.place_order(buy(A, "yes", "0.40", 10, tif="gtc", strategy="s2"))
    assert a.queue_ahead == b.queue_ahead == D("50")
    md.add_trade(A, "0.40", 60, T0 + timedelta(seconds=2))
    clock.advance(5)
    new = await broker.process_resting_orders()
    # a: queue 50 burns 50, excess 10 -> fills 10. b: queue 50 burns 50, excess 60-50-10 = 0
    assert [(f.order_id, f.count) for f in new] == [(a.id, 10)]
    assert broker.get_order(b.id).queue_ahead == 0 and broker.get_order(b.id).filled_count == 0


async def test_queue_ahead_capped_by_displayed_size(broker, md, clock):
    o = await broker.place_order(buy(A, "yes", "0.40", 10, tif="gtc"))
    md.set_book(A, yes_bids=[("0.40", 20)], no_bids=[("0.55", 100)])  # people ahead of us left
    clock.advance(5)
    await broker.process_resting_orders()
    assert broker.get_order(o.id).queue_ahead == D("20")
    md.set_book(A, yes_bids=[("0.40", 35)], no_bids=[("0.55", 100)])  # joiners queue behind us
    clock.advance(5)
    await broker.process_resting_orders()
    assert broker.get_order(o.id).queue_ahead == D("20")


async def test_improving_the_bid_has_zero_queue(broker):
    o = await broker.place_order(buy(A, "yes", "0.41", 3, tif="gtc"))
    assert o.queue_ahead == 0 and o.status == "open"


async def test_fractional_prints_accumulate(broker, md, clock):
    o = await broker.place_order(buy(A, "yes", "0.41", 3, tif="gtc"))  # improves the bid: queue 0
    md.add_trade(A, "0.41", "0.5", T0 + timedelta(seconds=1))
    clock.advance(2)
    assert await broker.process_resting_orders() == []
    assert broker.get_order(o.id).fill_credit == D("0.5")
    md.add_trade(A, "0.41", "0.75", T0 + timedelta(seconds=3))
    clock.advance(2)
    new = await broker.process_resting_orders()
    assert [f.count for f in new] == [1] and broker.get_order(o.id).fill_credit == D("0.25")


async def test_marketable_gtc_takes_then_rests(broker):
    o = await broker.place_order(buy(A, "yes", "0.43", 30, tif="gtc"))
    # taker part: 10 @ .42 + 15 @ .43 (fees .18 + .25); the other 5 rest at .43 with queue 0
    assert (o.taker_filled_count, o.filled_count, o.status) == (25, 25, "partially_filled")
    assert o.queue_ahead == 0 and o.fees == D("0.43")
    assert o.reserved == D("2.16")  # 5 x .43 + 0 maker fee + .01
    assert broker.cash == D("1000") - D("10.65") - D("0.43") - D("2.16")


async def test_gtc_expiry_and_market_close(broker, md, clock):
    o = await broker.place_order(buy(A, "yes", "0.40", 10, tif="gtc", expires_in_s=60))
    assert o.expires_at == T0 + timedelta(seconds=60)
    md.add_trade(A, "0.30", 5, T0 + timedelta(seconds=61))  # after expiry: must not fill
    clock.advance(61)
    assert await broker.process_resting_orders() == []
    o = broker.get_order(o.id)
    assert o.status == "expired" and o.reserved == 0 and broker.cash == D("1000")
    # market closing expires resting orders too
    o2 = await broker.place_order(buy(A, "yes", "0.40", 10, tif="gtc"))
    md.set_market(make_market(A, close_time=clock.now + timedelta(seconds=30)))
    clock.advance(31)
    await broker.process_resting_orders()
    o2 = broker.get_order(o2.id)
    assert o2.status == "expired" and o2.status_reason == "market closed" and broker.cash == D("1000")


async def test_cancel_releases_reservation(broker):
    o = await broker.place_order(buy(A, "yes", "0.40", 10, tif="gtc"))
    assert broker.reserved_cash == D("4.01")
    c = await broker.cancel_order(o.id)
    assert c.status == "cancelled" and broker.reserved_cash == 0 and broker.cash == D("1000")
    assert (await broker.cancel_order(o.id)).status == "cancelled"  # idempotent
    with pytest.raises(KeyError):
        await broker.cancel_order(9999)


# --------------------------------------------------------------------------- rule 5: netting


async def test_yes_then_no_netting_realized_pnl(broker, md):
    md.set_book(A, no_bids=[("0.60", 100)])  # YES ask .40
    await broker.place_order(buy(A, "yes", "0.40", 10))  # cost 4.00, fee ceil(.168) = .17
    md.set_book(A, yes_bids=[("0.45", 100)])  # NO ask .55
    await broker.place_order(buy(A, "no", "0.55", 4))  # fee ceil(.0693) = .07
    s = broker.store.list_settlements()[0]
    # realized = 4 x (1 - .40 - .55) - (opening fee share .17*4/10 = .068) - .07 = .20 - .138
    assert (s.kind, s.result, s.side, s.count) == ("close", "closed", "yes", 4)
    assert (s.payout, s.cost_basis, s.fees, s.pnl, s.exit_price) == (
        D("1.80"), D("1.60"), D("0.138"), D("0.062"), D("0.45"))
    p = broker.position(A, "s1")
    assert (p.side, p.count, p.cost_basis, p.open_fees) == ("yes", 6, D("2.40"), D("0.102"))
    assert broker.cash == D("997.56")  # 1000 - 4.17 - (2.20 + .07) + 4 x $1 pair redemption
    # flip: buy 10 NO closes the remaining 6 YES and opens 4 NO; fee ceil(.17325) = .18 split 6/4
    await broker.place_order(buy(A, "no", "0.55", 10))
    s2 = broker.store.list_settlements()[0]
    # 6 x .45 - 2.40 - (.102 + .108) = .09
    assert (s2.count, s2.pnl, s2.fees) == (6, D("0.09"), D("0.210"))
    p = broker.position(A, "s1")
    assert (p.side, p.count, p.cost_basis, p.open_fees) == ("no", 4, D("2.20"), D("0.072"))
    assert broker.realized_pnl == D("0.152")
    assert broker.cash == D("997.88")  # 997.56 - 5.68 + 6
    # ledger identity: cash + position book value == start + realized
    assert broker.cash + p.cost_basis + p.open_fees == D("1000") + broker.realized_pnl


async def test_sell_closes_like_buying_the_other_side(broker, md):
    md.set_book(A, no_bids=[("0.60", 100)])
    await broker.place_order(buy(A, "yes", "0.40", 10))
    md.set_book(A, yes_bids=[("0.45", 100)])
    await broker.place_order(buy(A, "yes", "0.45", 4, action="sell"))  # sell 4 YES @ .45
    s = broker.store.list_settlements()[0]
    assert (s.pnl, s.exit_price) == (D("0.062"), D("0.45"))
    assert broker.position(A, "s1").count == 6


async def test_netting_is_per_strategy(broker, md):
    md.set_book(A, yes_bids=[("0.45", 100)], no_bids=[("0.60", 100)])
    await broker.place_order(buy(A, "yes", "0.40", 5, strategy="s1"))
    await broker.place_order(buy(A, "no", "0.55", 5, strategy="s2"))
    assert broker.position(A, "s1").side == "yes" and broker.position(A, "s2").side == "no"
    assert broker.store.list_settlements() == []


# --------------------------------------------------------------------------- rule 6: cash


async def test_insufficient_cash_rejected(md, clock):
    b = PaperBroker(md, Store(":memory:"), starting_balance=10, clock=clock)
    o = await b.place_order(buy(A, "yes", "0.45", 30))
    # needs 30 x .45 + ceil(.07*30*.45*.55 = .51975) = 13.50 + .52 = 14.02 > 10
    assert o.status == "rejected" and "insufficient cash" in o.status_reason
    assert b.cash == D("10") and b.store.list_fills() == []
    g = await b.place_order(buy(A, "yes", "0.40", 20, tif="gtc"))  # needs 8.00 + .34 + .01 = 8.35
    assert g.status == "open" and g.reserved == D("8.01") and b.cash == D("1.99")
    g2 = await b.place_order(buy(A, "yes", "0.40", 5, tif="gtc"))  # needs 2.00 + .09 + .01 > 1.99
    assert g2.status == "rejected"
    await b.cancel_order(g.id)
    assert b.cash == D("10")


# --------------------------------------------------------------------------- rule 9: baskets


async def test_basket_all_or_none(broker, md):
    md.set_book(B, yes_bids=[("0.70", 5)], no_bids=[("0.20", 50)])  # NO ask .30 x5 only
    legs = [buy(A, "yes", "0.42", 10), buy(B, "no", "0.30", 10)]
    orders = await broker.place_basket(legs)
    assert [o.status for o in orders] == ["rejected", "rejected"]
    assert "only 5/10 fillable" in orders[1].status_reason and orders[0].status_reason.startswith("basket rejected")
    assert broker.store.list_fills() == [] and broker.cash == D("1000") and broker.consumed() == {}
    md.set_book(B, yes_bids=[("0.70", 50)], no_bids=[("0.20", 50)])
    orders = await broker.place_basket(legs)
    assert [o.status for o in orders] == ["filled", "filled"]
    assert orders[0].group_id == orders[1].group_id == f"basket-{orders[0].id}"


async def test_basket_legs_share_consumed_liquidity(broker):
    legs = [buy(A, "yes", "0.42", 6), buy(A, "yes", "0.42", 6, strategy="s2")]  # only 10 at .42
    orders = await broker.place_basket(legs)
    assert all(o.status == "rejected" for o in orders)
    assert "only 4/6" in orders[1].status_reason


async def test_basket_rejects_gtc_legs_and_insufficient_cash(md, clock):
    b = PaperBroker(md, None, starting_balance=5, clock=clock)
    orders = await b.place_basket([buy(A, "yes", "0.42", 5, tif="gtc")])
    assert orders[0].status == "rejected" and "must be ioc" in orders[0].status_reason
    md.set_book(B, yes_bids=[("0.70", 50)], no_bids=[("0.20", 50)])
    orders = await b.place_basket([buy(A, "yes", "0.42", 5), buy(B, "no", "0.30", 10)])  # 2.20 + 3.12 > 5
    assert [o.status for o in orders] == ["rejected", "rejected"] and b.cash == D("5")


# --------------------------------------------------------------------------- rule 10: rejections


@pytest.mark.parametrize(
    ("change", "expect"),
    [
        (dict(status="closed"), "not active"),
        (dict(status="inactive"), "not active"),
        (dict(close_time=T0), "closed at"),
    ],
)
async def test_rejects_untradable_markets(broker, md, change, expect):
    md.set_market(make_market(A, **change))
    o = await broker.place_order(buy(A, "yes", "0.45", 1))
    assert o.status == "rejected" and expect in o.status_reason
    assert not any(c[0] == "orderbook" for c in md.calls)


async def test_rejects_when_trading_paused(broker, md):
    md.exchange = {"trading_active": True, "exchange_index_statuses": [
        {"exchange_index": 0, "trading_active": False}, {"exchange_index": 2, "trading_active": True}]}
    o = await broker.place_order(buy(A, "yes", "0.45", 1))
    assert o.status == "rejected" and "trading paused" in o.status_reason
    md.set_market(make_market(A, exchange_index=2))
    assert (await broker.place_order(buy(A, "yes", "0.45", 1))).status == "filled"
    broker.set_exchange_status({"trading_active": False})  # engine-fed status wins
    assert (await broker.place_order(buy(A, "yes", "0.45", 1))).status == "rejected"


@pytest.mark.parametrize(
    ("side", "price", "ok"),
    [("yes", "0.405", False), ("yes", "0", False), ("yes", "1", False), ("yes", "1.2", False),
     ("no", "0.555", False), ("yes", "0.45", True), ("no", "0.99", True)],
)
async def test_price_grid(broker, side, price, ok):
    o = await broker.place_order(buy(A, side, price, 1))
    assert (o.status != "rejected") is ok, o.status_reason


async def test_sub_penny_grid(broker, md):
    tapered = [{"start": "0.0000", "end": "0.1000", "step": "0.0010"},
               {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
               {"start": "0.9000", "end": "1.0000", "step": "0.0010"}]
    md.set_market(make_market(A, price_ranges=tapered))
    md.set_book(A, yes_bids=[("0.050", 10)], no_bids=[("0.944", 10)])  # YES ask .056
    assert (await broker.place_order(buy(A, "yes", "0.056", 1))).status == "filled"
    assert (await broker.place_order(buy(A, "no", "0.945", 1))).status != "rejected"  # YES-price .055 on grid
    assert (await broker.place_order(buy(A, "yes", "0.155", 1))).status == "rejected"  # cent band


@pytest.mark.parametrize(
    "bad",
    [dict(side="maybe"), dict(action="short"), dict(tif="fok"), dict(count=0), dict(count=1.5),
     dict(limit_price=None)],
)
async def test_invalid_intents_rejected(broker, md, bad):
    kw = dict(ticker=A, side="yes", limit_price=D("0.45"), count=1) | bad
    o = await broker.place_order(Intent(**kw))
    assert o.status == "rejected" and o.decision == "rejected"
    assert broker.cash == D("1000")


async def test_count_override_and_unknown_market(broker, md):
    o = await broker.place_order(buy(A, "yes", "0.45", 30), count=3)
    assert o.count == 3 and o.filled_count == 3
    o2 = await broker.place_order(buy("NOPE-1", "yes", "0.45", 1))
    assert o2.status == "rejected" and "market lookup failed" in o2.status_reason


async def test_fee_params_resolved_at_fill_time(broker, md):
    md.set_series("KXTEST", "quadratic", "0.5")
    o = await broker.place_order(buy(A, "yes", "0.42", 10))
    assert o.fees == D("0.09")  # ceil(.5 * .17052 = .08526)
    from kalshibot.kalshi.models import Event
    md.events["KXTEST-26SEP"] = Event.from_api({"event_ticker": "KXTEST-26SEP", "series_ticker": "KXTEST",
                                                "fee_type_override": "quadratic", "fee_multiplier_override": "1"})
    o2 = await broker.place_order(buy(A, "yes", "0.43", 15))
    assert o2.fees == D("0.26")  # event override M=1: ceil(.257355)


async def test_series_outage_charges_conservatively(broker, md):
    md.fail.add("series")
    o = await broker.place_order(buy(A, "yes", "0.40", 10, tif="gtc"))
    # fallback quadratic_with_maker_fees: maker reserve 4.00 + ceil(.042) + .01
    assert o.reserved == D("4.06")


async def test_non_finite_inputs_rejected(broker):
    o = await broker.place_order(buy(A, "yes", "NaN", 1))
    assert o.status == "rejected" and "limit_price" in o.status_reason
    o = await broker.place_order(Intent(ticker=A, side="yes", limit_price=D("0.45"), count=float("inf")))
    assert o.status == "rejected" and "count" in o.status_reason


async def test_settings_wiring(md, clock):
    from kalshibot.config import Settings

    s = Settings.model_validate({"account": {"starting_balance": 250},
                                 "paper": {"fee_precision": 0.0001, "consumed_liquidity_ttl_s": 5,
                                           "default_gtc_expiry_s": 90}})
    b = PaperBroker(md, None, settings=s, clock=clock)
    assert (b.cash, b.precision, b.ttl_s, b.default_gtc_expiry_s) == (D("250"), D("0.0001"), 5.0, 90.0)
    o = await b.place_order(buy(A, "yes", "0.40", 1, tif="gtc"))
    assert o.expires_at == T0 + timedelta(seconds=90) and o.reserved == D("0.4001")
