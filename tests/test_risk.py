"""RiskManager (ARCHITECTURE.md §8): every limit, the kill switch, exits, kelly_count.

New cost per contract at a 0.50 limit = 0.50 + 0.07 * 0.5 * 0.5 = 0.5175 (taker-fee bound).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from kalshibot.config import RiskSettings, Settings
from kalshibot.kalshi.models import Orderbook
from kalshibot.money import ZERO, D
from kalshibot.paper import make_market
from kalshibot.paper.models import Order, PortfolioView, Position
from kalshibot.risk import RiskManager, kelly_count
from kalshibot.store import Store

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
A = "KXEV-26SEP-A"
A2 = "KXEV-26SEP-B"  # same event
OTHER = "KXOTHER-26SEP-X"

LOOSE = dict(max_position_cost_per_market=10**6, max_exposure_per_event=10**6, max_total_exposure_pct=100,
             max_strategy_allocation_pct=100, min_cash_reserve=0, max_orders_per_minute=0, daily_loss_limit=0,
             min_seconds_to_close=0, max_spread=1)


@dataclass
class Intent:
    ticker: str = A
    side: str = "yes"
    limit_price: Decimal = D("0.50")
    count: int = 200
    action: str = "buy"
    strategy: str = "s1"


def rm(**limits) -> RiskManager:
    return RiskManager(RiskSettings(**(LOOSE | limits)), clock=lambda: T0)


def market(ticker=A, **kw):
    kw.setdefault("yes_bid", "0.49")
    kw.setdefault("yes_ask", "0.51")
    kw.setdefault("close_time", T0 + timedelta(days=1))
    return make_market(ticker, **kw)


def pos(ticker, cost, strategy="s1", side="yes", count=None):
    return Position(ticker=ticker, strategy=strategy, event_ticker=ticker.rsplit("-", 1)[0], side=side,
                    count=count if count is not None else 10, cost_basis=D(cost))


def order(ticker, reserved, strategy="s1"):
    return Order(id=1, ticker=ticker, side="yes", action="buy", count=10, limit_price=D("0.5"), tif="gtc",
                 strategy=strategy, event_ticker=ticker.rsplit("-", 1)[0], reserved=D(reserved))


def pv(equity=1000, cash=None, positions=(), orders=(), day_start=None) -> PortfolioView:
    return PortfolioView(ts=T0, starting_balance=D(1000), cash=D(cash if cash is not None else equity),
                         reserved_cash=sum((o.reserved for o in orders), ZERO), equity=D(equity), equity_mid=D(equity),
                         realized_pnl=ZERO, unrealized_pnl=ZERO, fees_paid=ZERO,
                         day_start_equity=D(day_start if day_start is not None else equity),
                         positions=tuple(positions), open_orders=tuple(orders))


def test_all_clear_approves_everything():
    d = rm().check(Intent(), market(), pv())
    assert (d.approved_count, d.reason, d.binding_limit, d.approved, d.partial) == (200, "ok", None, True, False)


def test_max_position_cost_per_market():
    r = rm(max_position_cost_per_market=50)
    d = r.check(Intent(), market(), pv())
    assert (d.approved_count, d.binding_limit) == (96, "max_position_cost_per_market")  # 50 / .5175 = 96.6
    assert d.partial and "reduced 200 -> 96" in d.reason
    # other strategies' positions and resting orders in the ticker count too
    d = r.check(Intent(), market(), pv(positions=[pos(A, 30, strategy="s2")]))
    assert d.approved_count == 38  # 20 / .5175 = 38.6
    d = r.check(Intent(), market(), pv(positions=[pos(A, 30)], orders=[order(A, 10)]))
    assert d.approved_count == 19  # 10 / .5175 = 19.3
    d = r.check(Intent(), market(), pv(positions=[pos(A, 50)]))
    assert d.approved_count == 0 and not d.approved and "no headroom" in d.reason


def test_max_exposure_per_event():
    r = rm(max_exposure_per_event=100)
    d = r.check(Intent(), market(), pv(positions=[pos(A2, 90), pos(OTHER, 500)]))
    assert (d.approved_count, d.binding_limit) == (19, "max_exposure_per_event")


def test_max_total_exposure_pct():
    r = rm(max_total_exposure_pct=80)  # 800 of 1000 equity
    d = r.check(Intent(), market(), pv(positions=[pos(OTHER, 700)], orders=[order(A2, 90)]))
    assert (d.approved_count, d.binding_limit) == (19, "max_total_exposure_pct")


def test_max_strategy_allocation_pct():
    r = rm(max_strategy_allocation_pct=50)  # 500 per strategy
    port = pv(positions=[pos(OTHER, 495), pos(A2, 300, strategy="s2")])
    d = r.check(Intent(), market(), port)
    assert (d.approved_count, d.binding_limit) == (9, "max_strategy_allocation_pct")  # 5 / .5175
    assert r.check(Intent(strategy="s2", count=500), market(), port).approved_count == 386  # 200 / .5175
    assert r.check(Intent(strategy="s3"), market(), port).approved_count == 200


def test_min_cash_reserve():
    r = rm(min_cash_reserve=50)
    assert r.check(Intent(), market(), pv(cash=60)).approved_count == 19
    d = r.check(Intent(), market(), pv(cash=50))
    assert (d.approved_count, d.binding_limit) == (0, "min_cash_reserve")


def test_max_orders_per_minute():
    now = [T0]
    r = RiskManager(RiskSettings(**(LOOSE | dict(max_orders_per_minute=3))), clock=lambda: now[0])
    assert [r.check(Intent(count=1), market(), pv()).approved_count for _ in range(3)] == [1, 1, 1]
    d = r.check(Intent(count=1), market(), pv())
    assert (d.approved_count, d.binding_limit) == (0, "max_orders_per_minute")
    assert r.orders_last_minute() == 3
    now[0] = T0 + timedelta(seconds=60)
    assert r.check(Intent(count=1), market(), pv(), record=False).approved_count == 1
    assert r.orders_last_minute() == 0


def test_daily_loss_trips_sticky_kill_switch_but_exits_still_allowed():
    store = Store(":memory:")
    r = RiskManager(RiskSettings(**(LOOSE | dict(daily_loss_limit=100))), store=store, clock=lambda: T0)
    assert r.check(Intent(), market(), pv(equity=901, day_start=1000)).approved_count == 200  # -99: fine
    d = r.check(Intent(), market(), pv(equity=900, day_start=1000))  # -100: trips
    assert (d.approved_count, d.binding_limit) == (0, "kill_switch") and r.kill_switch
    assert "daily loss limit" in r.kill_switch_reason
    # sticky even after P&L recovers, and persisted
    assert r.check(Intent(), market(), pv()).approved_count == 0
    r2 = RiskManager(RiskSettings(**(LOOSE | dict(daily_loss_limit=100))), store=store, clock=lambda: T0)
    assert r2.kill_switch
    # exits: holding 10 YES, buying 15 NO closes 10 (allowed) and would open 5 (blocked)
    port = pv(positions=[pos(A, 4, side="yes", count=10)])
    d = r2.check(Intent(side="no", count=15), market(), port)
    assert (d.approved_count, d.closing_count) == (10, 10) and "closing contracts only" in d.reason
    assert r2.check(Intent(side="yes", count=5, action="sell"), market(), port).approved_count == 5
    r2.set_kill_switch(False)
    assert not RiskManager(RiskSettings(**LOOSE), store=store).kill_switch
    assert r2.check(Intent(), market(), pv()).approved_count == 200
    assert any(row["kind"] == "risk" for row in store.list_logs())


def test_manual_kill_switch_blocks_entries():
    r = rm()
    r.set_kill_switch(True, "operator")
    d = r.check(Intent(), market(), pv())
    assert d.approved_count == 0 and "operator" in d.reason
    assert r.evaluate(pv()) is True


def test_min_seconds_to_close():
    r = rm(min_seconds_to_close=300)
    d = r.check(Intent(), market(close_time=T0 + timedelta(seconds=299)), pv())
    assert (d.approved_count, d.binding_limit) == (0, "min_seconds_to_close")
    assert r.check(Intent(), market(close_time=T0 + timedelta(seconds=301)), pv()).approved_count == 200


def test_max_spread_guard():
    r = rm(max_spread=D("0.10"))
    assert r.check(Intent(), market(yes_bid="0.40", yes_ask="0.50"), pv()).approved_count == 200  # == max
    d = r.check(Intent(), market(yes_bid="0.40", yes_ask="0.52"), pv())
    assert (d.approved_count, d.binding_limit) == (0, "max_spread")
    d = r.check(Intent(), make_market(A, yes_bid="0.40", close_time=T0 + timedelta(days=1)), pv())
    assert d.approved_count == 0 and "two-sided" in d.reason
    # a fresh book overrides the (CDN-stale) market snapshot
    book = Orderbook.from_levels(A, yes_bids=[("0.45", 1)], no_bids=[("0.53", 1)])  # spread .02
    assert r.check(Intent(), market(yes_bid="0.40", yes_ask="0.52"), pv(), book=book).approved_count == 200


def test_sell_intent_is_sized_as_buying_the_other_side():
    r = rm(max_position_cost_per_market=10)
    d = r.check(Intent(action="sell", limit_price=D("0.60")), market(), pv())
    assert d.approved_count == 23  # buy NO @ .40: .40 + .07*.4*.6 = .4168 -> 10 / .4168 = 23.99


def test_bad_intents():
    r = rm()
    assert r.check(Intent(count=0), market(), pv()).approved_count == 0
    assert r.check(Intent(limit_price=D("1")), market(), pv()).approved_count == 0


def test_kelly_count():
    assert kelly_count(0.6, 0.5, 1000, 0.25) == 100  # f* = .2; stake 50; 50 / .5
    assert kelly_count(0.6, 0.5, 1000, 0.25, cap=40) == 40
    assert kelly_count(0.5, 0.5, 1000, 0.25) == 0
    assert kelly_count(0.4, 0.5, 1000, 1) == 0
    assert kelly_count(0.6, 0.5, 1000, 0.25, fee=0.02) == 80  # f* = .08/.48; stake 41.67; / .52
    assert kelly_count(D("0.9"), D("0.10"), D("100"), D("1")) == 888  # f* = .8/.9 -> 88.89 / .10
    assert kelly_count(0.6, 0, 1000, 0.25) == 0 and kelly_count(0.6, 0.5, 0, 0.25) == 0


def test_update_limits_validates_and_persists():
    store = Store(":memory:")
    r = RiskManager(Settings(), store=store)
    assert r.limits.max_spread == D("0.10")
    r.update_limits({"max_spread": 0.05, "max_orders_per_minute": 5})
    assert r.limits.max_spread == D("0.05") and store.get_risk_limits()["max_orders_per_minute"] == 5
    with pytest.raises(ValueError):
        r.update_limits({"nope": 1})
    with pytest.raises(ValidationError):
        r.update_limits({"max_total_exposure_pct": 150})
    assert RiskManager(Settings(), store=store).limits.max_spread == D("0.05")


def test_utilization_payload():
    r = rm(max_exposure_per_event=100, max_strategy_allocation_pct=50)
    port = pv(positions=[pos(A, 30), pos(OTHER, 20, strategy="s2")], orders=[order(A2, 10)], day_start=990)
    j = r.to_json(port)
    assert j["kill_switch"] is False and j["limits"]["max_exposure_per_event"] == 100.0
    u = j["utilization"]
    assert u["total_exposure"] == 60.0 and u["total_exposure_pct"] == 6.0 and u["daily_pnl"] == 10.0
    ev = {row["key"]: row for row in u["by_event"]}
    assert ev["KXEV-26SEP"]["exposure"] == 40.0 and ev["KXEV-26SEP"]["pct"] == 40.0
    st = {row["key"]: row for row in u["by_strategy"]}
    assert st["s1"]["exposure"] == 40.0 and st["s1"]["limit"] == 500.0
