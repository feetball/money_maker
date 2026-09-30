"""SpotPaperBroker resting GTC orders (contract §6 rule 4): post-only, queue_ahead, fills only
from later public trades with ``maker_side`` semantics, book crossing at the limit, maker fees,
cash reservation, expiry and cancels.

TST-USD (base increment 0.01), fees maker 0.5 % / taker 1 % (rounded up to the cent per
order), $1,000 cash; book asks 100 x 1, 101 x 2, 105 x 10 / bids 99 x 1, 98 x 2, 90 x 10.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from test_cb_broker import identity, make
from test_cb_broker_fakes import PID, make_trade, standard_md

from kalshibot.coinbase.broker import OrderNotFoundError, OrderNotOpenError
from kalshibot.coinbase.paper import SpotOrderIntent


def D(x: object) -> Decimal:
    return Decimal(str(x))


def gtc(side: str, base: object, limit: object, *, post_only: bool = True, strategy: str = "s1",
        **kw: object) -> SpotOrderIntent:
    return SpotOrderIntent(PID, side, base_size=D(base), order_type="limit", limit_price=D(limit),  # type: ignore[arg-type]
                           tif="gtc", post_only=post_only, strategy=strategy, **kw)  # type: ignore[arg-type]


async def test_post_only_that_would_cross_is_rejected() -> None:
    b, md = make()
    o = await b.place_order(gtc("buy", 1, 100))  # limit 100 >= best ask 100
    assert o.status == "rejected" and "would cross" in o.status_reason
    await b.place_order(SpotOrderIntent(PID, "buy", quote_size=D(150), strategy="s1"))
    o = await b.place_order(gtc("sell", 1, 99))  # limit 99 <= best bid 99
    assert o.status == "rejected" and "would cross" in o.status_reason


async def test_resting_buy_queue_and_trade_fills_with_maker_semantics() -> None:
    b, md = make()
    t0 = md.clock.now
    md.add_trades([make_trade(101, 99, 5, "buy", t0 - timedelta(seconds=1))])  # before placement: ignored
    o = await b.place_order(gtc("buy", 1, 99))
    # queue_ahead = displayed bid size at 99 = 1; reserve 1 x 99 + maker fee ceil(0.495) = 0.50 -> 99.50
    assert o.status == "open" and o.decision == "resting"
    assert o.queue_ahead == D(1) and o.reserved == D("99.50") and b.cash == D("900.50")
    assert o.expires_at == t0 + timedelta(seconds=3600)
    identity(b)

    md.clock.advance(10)
    t = md.clock.now
    md.add_trades([
        make_trade(102, 99, 5, "sell", t),  # maker_side sell = a resting ask was lifted: never fills a bid
        make_trade(103, 99, "0.6", "buy", t),  # hit the bids at our price: burns queue 1 -> 0.4
        make_trade(104, 99, "0.7", "buy", t),  # burns 0.4, the remaining 0.3 fills us
        make_trade(105, "98.5", "0.5", "buy", t),  # trades THROUGH 99: fills 0.5 at our limit 99
    ])
    fills = await b.maintain()
    assert [(f.price, f.base_size, f.is_taker, f.trade_id) for f in fills] == [
        (D(99), D("0.3"), False, 104), (D(99), D("0.5"), False, 105)]
    # maker fees cumulative per order: fee_for(29.7) = ceil(0.1485) = 0.15; fee_for(79.2) = 0.40 -> 0.25
    assert [f.fee for f in fills] == [D("0.15"), D("0.25")] and all(f.fee_rate == D("0.005") for f in fills)
    (o,) = b.open_orders()
    assert o.status == "partially_filled" and o.filled_base == D("0.8") and o.queue_ahead == 0
    # paid from the reservation: 99.50 - (29.70 + 0.15) - (49.50 + 0.25) = 19.90; free cash unchanged
    assert o.reserved == D("19.90") and b.cash == D("900.50")
    identity(b)

    # the cursor: re-delivered prints (ids <= 105) are ignored; a new one at 99 fills the rest
    md.clock.advance(10)
    md.add_trades([make_trade(106, 99, 1, "buy", md.clock.now)])
    fills = await b.maintain()
    assert ("trades_since", PID, 105) in md.calls
    assert [(f.base_size, f.fee) for f in fills] == [(D("0.2"), D("0.10"))]  # fee_for(99) = 0.50 in total
    o = b.get_order(o.id)
    assert o.status == "filled" and o.fees == D("0.50") and o.reserved == 0
    (p,) = b.positions()
    assert p.quantity == D(1) and p.cost_basis == D("99.50")  # exactly the original reservation
    assert b.cash == D("900.50")
    identity(b)
    assert await b.maintain() == []  # nothing open


async def test_trades_at_or_after_expiry_do_not_fill_and_expiry_releases_cash() -> None:
    b, md = make()
    o = await b.place_order(gtc("buy", 1, 99, expires_in_s=60))
    md.clock.advance(61)
    md.add_trades([make_trade(200, 98, 5, "buy", md.clock.now)])  # after expires_at
    assert await b.maintain() == []
    o = b.get_order(o.id)
    assert o.status == "expired" and o.reserved == 0 and b.cash == D(1000) and o.decision == "unfilled"
    identity(b)


async def test_expiry_waits_for_the_tape_then_grace() -> None:
    b, md = make()
    o = await b.place_order(gtc("buy", 1, 99, expires_in_s=60))
    md.fail_trades.add(PID)
    md.clock.advance(61)
    await b.maintain()
    assert b.get_order(o.id).status == "open"  # a print before the expiry could still fill it
    md.clock.advance(300)
    await b.maintain()
    assert b.get_order(o.id).status == "expired" and b.cash == D(1000)


async def test_book_crossing_fills_at_limit_and_respects_consumed() -> None:
    b, md = make(fill_on_book_cross=True)  # opt-in (default off: no fills from quotes alone)
    o = await b.place_order(gtc("buy", 2, 99))
    # reserve 198 + ceil(0.99) = 198.99
    assert o.reserved == D("198.99")
    md.clock.advance(5)
    md.set_book(PID, bids=[(98, 1)], asks=[("98.5", "0.5"), (99, "0.7"), (100, 5)])
    fills = await b.maintain()
    # asks <= 99 cross our bid: 0.5 (at 98.5) + 0.7 (at 99) fill AT OUR LIMIT 99, maker
    assert [(f.price, f.base_size, f.is_taker) for f in fills] == [(D(99), D("0.5"), False), (D(99), D("0.7"), False)]
    # fees: fee_for(49.5) = ceil(0.2475) = 0.25; fee_for(118.8) = ceil(0.594) = 0.60 -> 0.35
    assert [f.fee for f in fills] == [D("0.25"), D("0.35")]
    o = b.get_order(o.id)
    assert o.filled_base == D("1.2") and o.queue_ahead == 0 and o.status == "partially_filled"
    assert o.reserved == D("198.99") - D("118.80") - D("0.60")
    # the same displayed asks are now consumed: the next pass fills nothing more
    md.clock.advance(5)
    assert await b.maintain() == []
    identity(b)


async def test_queue_ahead_only_shrinks_with_the_book() -> None:
    b, md = make()
    md.set_book(PID, bids=[(99, 3)], asks=[(100, 1)])
    o = await b.place_order(gtc("buy", 1, 99))
    assert o.queue_ahead == D(3)
    md.set_book(PID, bids=[(99, 5)], asks=[(100, 1)])  # joiners queue behind us
    await b.maintain()
    assert b.get_order(o.id).queue_ahead == D(3)
    md.set_book(PID, bids=[(99, "1.5")], asks=[(100, 1)])  # people ahead cancelled
    await b.maintain()
    assert b.get_order(o.id).queue_ahead == D("1.5")
    # improving the best bid: nobody ahead
    o2 = await b.place_order(gtc("buy", 1, "99.5"))
    assert o2.queue_ahead == 0


async def test_one_print_is_shared_in_price_time_priority() -> None:
    b, md = make()
    md.set_book(PID, bids=[(98, 1)], asks=[(100, 5)])
    a1 = await b.place_order(gtc("buy", 1, 99, strategy="a"))  # improves: queue 0
    md.clock.advance(1)
    a2 = await b.place_order(gtc("buy", 1, 99, strategy="b"))  # same price, later: queue 0 too
    a3 = await b.place_order(gtc("buy", 1, 98, strategy="c"))  # queue 1 (displayed at 98)
    md.clock.advance(1)
    md.add_trades([make_trade(300, 98, "1.6", "buy", md.clock.now)])
    await b.maintain()
    # the print at 98 goes through 99: a (earliest at the best price) gets 1, b the other 0.6;
    # c at 98 gets nothing (print exhausted; its queue of 1 would come first anyway)
    got = {o.strategy: o.filled_base for o in (b.get_order(a1.id), b.get_order(a2.id), b.get_order(a3.id))}
    assert got == {"a": D(1), "b": D("0.6"), "c": D(0)}


async def test_resting_sell_fills_and_realizes() -> None:
    b, md = make(fill_on_book_cross=True)  # the book-cross part is opt-in
    await b.place_order(SpotOrderIntent(PID, "buy", base_size=D(1), order_type="limit", limit_price=D(100),
                                        strategy="s1"))  # cost 100 + 1.00 = 101
    o = await b.place_order(gtc("sell", 1, 101))
    # queue_ahead = displayed ask size at 101 = 2; a sell reserves no cash (its base is committed)
    assert o.queue_ahead == D(2) and o.reserved == 0 and b.cash == D(899)
    assert b.available_to_sell("s1", PID) == 0
    extra = await b.place_order(SpotOrderIntent(PID, "sell", base_size=D("0.5"), strategy="s1"))
    assert extra.status == "rejected" and "no shorting" in extra.status_reason
    md.clock.advance(5)
    md.add_trades([make_trade(400, 101, "2.5", "buy", md.clock.now),  # maker_side buy: not ours
                   make_trade(401, 101, "2.5", "sell", md.clock.now)])  # lifts asks: burns 2, fills 0.5
    md.set_book(PID, bids=[("101.5", "0.3"), (101, 1)], asks=[(102, 1)])  # bids cross our 101
    fills = await b.maintain()
    # print: 0.5 @ 101, fee ceil(0.2525) = 0.26; book: 0.3 + 0.2 more at OUR limit 101:
    # cumulative fee_for(80.8) = 0.41 -> 0.15; fee_for(101) = 0.51 -> 0.10
    assert [(f.price, f.base_size, f.fee) for f in fills] == [
        (D(101), D("0.5"), D("0.26")), (D(101), D("0.3"), D("0.15")), (D(101), D("0.2"), D("0.10"))]
    o = b.get_order(o.id)
    # proceeds 101 - 0.51 = 100.49; realized 100.49 - 101 (fee-inclusive cost) = -0.51
    assert o.status == "filled" and o.realized_pnl == D("-0.51")
    assert b.cash == D("999.49") and b.positions() == []
    a = b.account()
    assert a.trades == 1 and a.wins == 0 and a.realized_pnl == D("-0.51")
    assert b.consumed(PID)[(PID, "bid", D("101.5"))] == D("0.3")
    identity(b)


async def test_non_post_only_gtc_takes_marketable_part_then_rests() -> None:
    md = standard_md()
    md.set_book(PID, bids=[(99, 1)], asks=[(100, 1), (101, 1), (102, 5)])
    b, _ = make(md)
    o = await b.place_order(gtc("buy", 3, 101, post_only=False))
    # taker: 1 @ 100 + 1 @ 101 = 201, fee ceil(2.01) = 2.01 (taker 1 %)
    # rest 1 @ 101 reserves 101 + ceil(0.505) = 101.51 -> cash 1000 - 203.01 - 101.51 = 695.48
    assert o.status == "partially_filled" and o.filled_base == D(2) and o.taker_fees == D("2.01")
    assert o.reserved == D("101.51") and b.cash == D("695.48") and o.queue_ahead == 0
    identity(b)
    # the asks it took are consumed: the book "crossing" our 101 bid fills nothing more
    md.clock.advance(5)
    assert await b.maintain() == []
    md.add_trades([make_trade(500, 101, "0.4", "buy", md.clock.now)])
    (f,) = await b.maintain()
    # maker: 0.4 @ 101 = 40.40, fee ceil(0.202) = 0.21 (maker accumulator is separate)
    assert (f.base_size, f.fee, f.is_taker) == (D("0.4"), D("0.21"), False)
    assert b.get_order(o.id).reserved == D("101.51") - D("40.61")
    identity(b)


async def test_gtc_buy_by_quote_is_sized_to_fit() -> None:
    md = standard_md()
    md.set_book(PID, bids=[(49, 5)], asks=[(51, 5)])
    b, _ = make(md)
    o = await b.place_order(SpotOrderIntent(PID, "buy", quote_size=D(100), order_type="limit", limit_price=D(50),
                                            tif="gtc", post_only=True, strategy="s1"))
    # largest base with base x 50 + ceil(0.5 %) <= 100: 1.99 -> 99.50 + 0.50 = 100.00
    assert o.base_size == D("1.99") and o.reserved == D("100.00") and o.quote_size == D(100)


async def test_cancel_syncs_the_tape_first_and_releases_cash() -> None:
    b, md = make()
    o = await b.place_order(gtc("buy", 1, 99))
    md.clock.advance(5)
    md.add_trades([make_trade(600, "98", "0.25", "buy", md.clock.now)])  # through 99: fills 0.25 first
    c = await b.cancel_order(o.id, "strategy changed its mind")
    assert c.status == "cancelled" and c.filled_base == D("0.25") and c.decision == "partial"
    assert c.status_reason == "strategy changed its mind" and c.reserved == 0
    # cash: 1000 - 0.25 x 99 - fee ceil(0.12375) = 0.13 = 975.12
    assert b.cash == D("975.12")
    with pytest.raises(OrderNotOpenError):
        await b.cancel_order(o.id)
    with pytest.raises(OrderNotFoundError):
        await b.cancel_order(9999)
    identity(b)


async def test_cancel_that_the_sync_filled_returns_filled() -> None:
    b, md = make()
    o = await b.place_order(gtc("buy", 1, 99))
    md.clock.advance(5)
    md.add_trades([make_trade(700, 98, 5, "buy", md.clock.now)])
    c = await b.cancel_order(o.id)
    assert c.status == "filled"


async def test_cancel_all_by_side() -> None:
    b, md = make()
    await b.place_order(SpotOrderIntent(PID, "buy", base_size=D(1), order_type="limit", limit_price=D(100),
                                        strategy="s1"))
    buy = await b.place_order(gtc("buy", 1, 98))
    sell = await b.place_order(gtc("sell", 1, 104))
    out = await b.cancel_all(side="buy", reason="kill switch")
    assert [o.id for o in out] == [buy.id] and out[0].status_reason == "kill switch"
    assert [o.id for o in b.open_orders()] == [sell.id]
    identity(b)


async def test_delisted_product_cancels_resting_orders() -> None:
    b, md = make()
    o = await b.place_order(gtc("buy", 1, 99))
    from test_cb_broker_fakes import make_product
    md.products[PID] = make_product(PID, status="delisted")
    await b.maintain()
    assert b.get_order(o.id).status == "cancelled" and b.cash == D(1000)
