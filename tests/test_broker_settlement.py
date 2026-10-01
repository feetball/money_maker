"""PaperBroker settlement, marks, equity identity and restart restore (ARCHITECTURE.md §6 rules 7-8)."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kalshibot.money import ZERO, D
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
    # A: YES bid .38 x100, YES ask .40 x100 (NO bid .60)
    m.set_book(A, yes_bids=[("0.38", 100)], no_bids=[("0.60", 100)])
    m.set_book(B, yes_bids=[("0.38", 100)], no_bids=[("0.60", 100)])
    return m


@pytest.fixture
def broker(md, clock):
    return PaperBroker(md, Store(":memory:"), starting_balance=1000, profit_sweep_pct=0, clock=clock)


def finalize(md, ticker, result, value=None, status="finalized"):
    md.set_market(make_market(ticker, status=status, result=result, settlement_value=value))


def assert_identities(b: PaperBroker):
    a = b.account()
    open_book = sum((p.cost_basis + p.open_fees for p in b.positions()), ZERO)
    # cash conservation: what left the account is either still at cost in positions or realized
    assert a.cash + a.reserved_cash + open_book == a.starting_balance + a.realized_pnl
    # equity identity
    assert a.equity == a.cash + a.reserved_cash + a.positions_liquidation_value
    assert a.equity == a.starting_balance + a.realized_pnl + a.unrealized_pnl
    assert a.total_pnl == a.realized_pnl + a.unrealized_pnl
    if b.store is not None:
        assert a.realized_pnl == sum((s.pnl for s in b.store.list_settlements(limit=None)), ZERO)
        assert a.fees_paid == sum((f.fee for f in b.store.list_fills(limit=None)), ZERO)


# --------------------------------------------------------------------------- settlement


@pytest.mark.parametrize(
    ("result", "value", "payout", "pnl"),
    [
        ("yes", "1.0000", "10.00", "5.83"),  # 10 - 4.00 - .17
        ("no", "0.0000", "0.00", "-4.17"),
        ("scalar", "0.3700", "3.70", "-0.47"),  # void / fair price: YES gets .37 per contract
    ],
)
async def test_settlement_yes_no_void(broker, md, clock, result, value, payout, pnl):
    await broker.place_order(buy(A, "yes", "0.40", 10, expected_edge=D("0.05"), fair_value=0.45))
    clock.advance(60)
    assert await broker.check_settlements() == []  # still active
    finalize(md, A, result, value)
    clock.advance(60)
    [s] = await broker.check_settlements()
    assert ("market", A, True) in md.calls  # polled fresh
    assert (s.kind, s.result, s.side, s.count, s.payout, s.cost_basis, s.fees, s.pnl) == (
        "settlement", result, "yes", 10, D(payout), D("4.00"), D("0.17"), D(pnl))
    assert s.expected_edge == D("0.50") and s.fair_value == pytest.approx(0.45)
    assert s.settlement_value == D(value)
    assert broker.positions() == [] and broker.cash == D("1000") - D("4.17") + D(payout)
    assert broker.realized_pnl == D(pnl)
    assert_identities(broker)
    clock.advance(60)
    assert await broker.check_settlements() == []  # settled once


async def test_void_pays_no_holders_one_minus_value_and_floors(broker, md):
    await broker.place_order(buy(A, "no", "0.62", 10, strategy="s2"))  # NO ask = 1 - .38; cost 6.20, fee .17
    await broker.place_order(buy(A, "yes", "0.40", 3, strategy="s3"))  # cost 1.20, fee ceil(.0504) = .06
    finalize(md, A, "scalar", "0.3333")
    out = {s.strategy: s for s in await broker.check_settlements()}
    assert out["s2"].payout == D("6.66")  # 10 x .6667 = 6.667 -> floored to the cent
    assert out["s2"].pnl == D("6.66") - D("6.20") - D("0.17")
    assert out["s3"].payout == D("0.99")  # 3 x .3333 = .9999 -> .99
    assert_identities(broker)


# --------------------------------------------------------------------------- profit sweep


async def test_profit_sweep_moves_realized_gains_out_of_cash(md, clock):
    """``AccountSettings.profit_sweep_pct``: winning settlements/closes keep cash flat and move
    the gain to ``reserved_profit`` instead, so it is never put back in the tradeable pool."""
    b = PaperBroker(md, Store(":memory:"), starting_balance=1000, profit_sweep_pct=100, clock=clock)
    await b.place_order(buy(A, "yes", "0.40", 10))  # cost 4.00, fee .17
    finalize(md, A, "yes", "1.0000")  # payout 10.00, pnl 10 - 4.17 = 5.83 (profit)
    [s] = await b.check_settlements()
    assert s.pnl == D("5.83")
    # the profit never reached cash: cash is as if the trade had returned exactly its cost back
    assert b.cash == D("1000")
    assert b.reserved_profit == D("5.83")
    assert b.realized_pnl == D("5.83")  # still reported for analytics
    a = b.account()
    assert a.equity == b.cash  # tradeable equity excludes reserved_profit
    assert a.net_worth == a.equity + a.reserved_profit == D("1000") + D("5.83")
    assert a.total_pnl == D("5.83")  # true total P&L still reflects the swept profit


async def test_profit_sweep_leaves_losses_in_cash(md, clock):
    b = PaperBroker(md, Store(":memory:"), starting_balance=1000, profit_sweep_pct=100, clock=clock)
    await b.place_order(buy(A, "yes", "0.40", 10))  # cost 4.00, fee .17
    finalize(md, A, "no", "0.0000")  # payout 0, pnl -4.17 (loss)
    [s] = await b.check_settlements()
    assert s.pnl == D("-4.17")
    assert b.cash == D("1000") - D("4.17")  # loss stays in cash, nothing to sweep
    assert b.reserved_profit == ZERO


async def test_profit_sweep_pct_partial(md, clock):
    b = PaperBroker(md, Store(":memory:"), starting_balance=1000, profit_sweep_pct=50, clock=clock)
    await b.place_order(buy(A, "yes", "0.40", 10))
    finalize(md, A, "yes", "1.0000")  # pnl 5.83; half (2.915 -> floored to .01) is swept
    await b.check_settlements()
    assert b.reserved_profit == D("2.91")
    assert b.cash == D("1000") - D("4.17") + D("10.00") - D("2.91")


async def test_determined_marks_but_waits_for_finalized(broker, md):
    await broker.place_order(buy(A, "yes", "0.40", 10))
    finalize(md, A, "yes", "1.0000", status="determined")
    md.set_book(A)  # book empties after close
    assert await broker.check_settlements() == []
    a = broker.account()
    assert a.positions_liquidation_value == D("10") and a.unrealized_pnl == D("10") - D("4.17")
    early = PaperBroker(md, None, starting_balance=1000, profit_sweep_pct=0, clock=broker.clock,
                        settle_on_determined=True)
    await early.place_order(buy(B, "yes", "0.40", 1))
    finalize(md, B, "no", "0", status="determined")
    assert [s.result for s in await early.check_settlements()] == ["no"]


async def test_settlement_cancels_resting_orders_and_survives_poll_errors(broker, md):
    await broker.place_order(buy(A, "yes", "0.40", 10))
    g = await broker.place_order(buy(A, "yes", "0.30", 5, tif="gtc"))
    await broker.place_order(buy(B, "yes", "0.40", 1))
    md.markets.pop(B)  # B now 404s (archived): logged, not fatal
    finalize(md, A, "no", "0")
    out = await broker.check_settlements()
    assert [s.ticker for s in out] == [A]
    assert broker.get_order(g.id).status == "cancelled" and broker.reserved_cash == 0
    assert any("settlement poll failed" in r["message"] for r in broker.store.list_logs())
    assert_identities(broker)


# --------------------------------------------------------------------------- marks


async def test_liquidation_vs_mid_marking(broker, md):
    await broker.place_order(buy(A, "yes", "0.40", 10))
    await broker.place_order(buy(B, "no", "0.62", 5))
    md.set_book(A, yes_bids=[("0.45", 10)], no_bids=[("0.50", 10)])  # YES bid .45 / ask .50 -> mid .475
    md.set_book(B, yes_bids=[("0.30", 10)], no_bids=[("0.66", 10)])  # NO bid .66, YES mid (.30+.34)/2
    await broker.refresh_marks()
    a = broker.account()
    assert a.positions_liquidation_value == D("4.50") + D("3.30")  # 10 x .45 + 5 x .66
    assert a.positions_mid_value == D("4.75") + D("3.40")  # 10 x .475 + 5 x (1 - .32)
    assert a.equity == a.cash + a.positions_liquidation_value
    assert a.equity_mid == a.cash + a.positions_mid_value
    rows = {r["ticker"]: r for r in broker.positions_json()}
    assert rows[A]["mark_price"] == 0.45 and rows[A]["yes_bid"] == 0.45 and rows[A]["yes_ask"] == 0.5
    md.set_book(A)  # an empty book (market closed) keeps the previous mark
    await broker.refresh_marks()
    assert broker.account().positions_liquidation_value == D("7.80")
    assert_identities(broker)


async def test_equity_snapshot_and_drawdown(broker, md, clock):
    await broker.place_order(buy(A, "yes", "0.40", 100))  # cost 40.00 + fee 1.68
    a1 = await broker.record_equity_snapshot()
    assert a1.equity == D("1000") - D("41.68") + D("38.00")  # marked at the .38 bid
    md.set_book(A, yes_bids=[("0.20", 100)], no_bids=[("0.60", 100)])
    clock.advance(60)
    a2 = await broker.record_equity_snapshot()
    assert a2.equity == a1.equity - D("18")
    assert a2.max_drawdown_pct == (D("18") / a1.equity * 100)
    assert a2.todays_pnl == a2.equity - a1.equity  # day baseline set by the first snapshot
    rows = broker.store.list_equity()
    assert [r["equity"] for r in rows] == [a1.equity, a2.equity]


async def test_equity_identity_conserved_over_random_session(md, clock):
    rng = random.Random(7)
    b = PaperBroker(md, Store(":memory:"), starting_balance=500, profit_sweep_pct=0, clock=clock)
    md.set_series("KXTEST", "quadratic_with_maker_fees", 1)
    for step in range(120):
        t = rng.choice([A, B])
        bid = D(rng.randint(20, 70)) / 100
        ask = bid + D(rng.randint(1, 6)) / 100
        md.set_book(t, yes_bids=[(bid, rng.randint(1, 40)), (bid - D("0.01"), 50)],
                    no_bids=[(1 - ask, rng.randint(1, 40)), (1 - ask - D("0.01"), 50)])
        side = rng.choice(["yes", "no"])
        action = rng.choice(["buy", "buy", "sell"])
        px = (ask if side == "yes" else 1 - bid) if action == "buy" else (bid if side == "yes" else 1 - ask)
        px += D(rng.choice([-2, -1, 0, 1])) / 100
        px = min(max(px, D("0.01")), D("0.99"))
        tif = rng.choice(["ioc", "ioc", "gtc"])
        await b.place_order(buy(t, side, px, rng.randint(1, 30), action=action, tif=tif,
                                strategy=rng.choice(["s1", "s2"])))
        if step % 5 == 0:
            md.add_trade(t, bid - D(rng.choice([0, 1])) / 100, rng.randint(1, 60), clock.now + timedelta(seconds=1))
        clock.advance(7)
        if step % 3 == 0:
            await b.process_resting_orders()
        if step % 10 == 0:
            await b.refresh_marks()
        assert_identities(b)
    finalize(md, A, "yes", "1")
    finalize(md, B, "scalar", "0.4200")
    await b.check_settlements()
    await b.process_resting_orders()
    assert_identities(b)
    assert b.positions() == [] and b.open_orders() == []
    assert b.account().equity == b.cash
    assert b.cash == D("500") + b.realized_pnl
    assert b.store.count("fills") > 50  # the session actually traded


# --------------------------------------------------------------------------- restart


async def test_restart_restores_full_state(tmp_path, md, clock):
    path = tmp_path / "paper.sqlite3"
    st = Store(path)
    b1 = PaperBroker(md, st, starting_balance=1000, clock=clock)
    await b1.place_order(buy(A, "yes", "0.40", 10, expected_edge=D("0.03"), fair_value=0.5))
    md.set_series("KXTEST", "quadratic_with_maker_fees", 1)
    g = await b1.place_order(buy(B, "yes", "0.38", 10, tif="gtc"))  # joins the .38 bid, queue 100
    md.add_trade(B, "0.38", 104, T0 + timedelta(seconds=1))  # burns the queue, fills 4 (maker)
    clock.advance(2)
    [f] = await b1.process_resting_orders()
    assert f.count == 4
    await b1.place_order(buy(A, "yes", "0.40", 5, strategy="s2"))  # consumes 15 at .40 in total
    await b1.record_equity_snapshot()
    before = b1.account()
    order_before = b1.get_order(g.id)
    st.close()

    st2 = Store(path)
    b2 = PaperBroker(md, st2, starting_balance=999_999, clock=clock)  # stored account wins
    after = b2.account()
    for k in ("starting_balance", "cash", "reserved_cash", "realized_pnl", "fees_paid", "equity",
              "positions_liquidation_value", "open_positions", "open_orders", "max_drawdown_pct"):
        assert getattr(after, k) == getattr(before, k), k
    o = b2.get_order(g.id)
    assert (o.status, o.filled_count, o.queue_ahead, o.reserved, o.fee_state) == (
        order_before.status, 4, D("0"), order_before.reserved, order_before.fee_state)
    p = b2.position(A, "s1")
    assert (p.count, p.cost_basis, p.open_fees, p.expected_edge_total, p.fair_value) == (
        10, D("4.00"), D("0.17"), D("0.30"), 0.5)
    assert b2.consumed(A) == {(A, "yes", D("0.40")): D("15")}
    # the old print is not replayed; a new one fills the rest with the restored fee accumulator
    clock.advance(5)
    assert await b2.process_resting_orders() == []
    md.add_trade(B, "0.37", 6, clock.now + timedelta(seconds=1))
    clock.advance(2)
    [f2] = await b2.process_resting_orders()
    assert f2.count == 6 and f2.id == 4  # id counters continue (fills 1..3 existed)
    o = b2.get_order(g.id)
    # maker fees: 4 @ .38 raw .016492 -> .02 ; 6 @ .38 raw .024738 -> .03 ; total .05 == ceil(.04123)
    assert o.status == "filled" and o.fees == D("0.05")
    assert_identities(b2)
    st2.close()


async def test_reset_wipes_paper_state(broker, md):
    await broker.place_order(buy(A, "yes", "0.40", 10))
    await broker.place_order(buy(A, "yes", "0.30", 5, tif="gtc"))
    a = await broker.reset(250)
    assert (a.cash, a.equity, a.open_positions, a.open_orders) == (D("250"), D("250"), 0, 0)
    assert broker.store.list_orders() == [] and broker.store.list_fills() == []
    assert broker.store.get_account()["cash"] == D("250")
    o = await broker.place_order(buy(A, "yes", "0.40", 1))
    assert o.id == 1


async def test_listeners_receive_events(broker):
    seen = []
    unsub = broker.subscribe(lambda kind, obj: seen.append((kind, obj.id)))
    o = await broker.place_order(buy(A, "yes", "0.40", 2))
    assert seen == [("fill", 1), ("order", o.id)]
    unsub()
    await broker.place_order(buy(A, "yes", "0.40", 2))
    assert len(seen) == 2


async def test_portfolio_view_is_a_copy(broker):
    await broker.place_order(buy(A, "yes", "0.40", 10))
    pv = broker.portfolio()
    assert pv.holds(A, "s1") and not pv.holds(A, "s2")
    assert pv.exposure(ticker=A) == D("4.00") and pv.exposure(event_ticker="KXTEST-26SEP") == D("4.00")
    pv.positions[0].count = 0
    assert broker.position(A, "s1").count == 10
