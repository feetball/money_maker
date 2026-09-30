"""SpotRiskManager (contract §11): sizing limits, sells always allowed, kill switch, persistence.

Default limits (CoinbaseRiskSettings): max_position_pct_per_product 50, max_total_exposure_pct 90,
max_strategy_allocation_pct 50, min_cash_reserve $20, max_orders_per_minute 20, daily_loss_limit
$100, max_spread_bps 50, min_trade_usd $10. Test fee tier: taker 1 %.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pydantic
import pytest
from test_cb_broker import make
from test_cb_broker_fakes import PID, T0, TIER, Clock, make_book

from kalshibot.coinbase.config import CoinbaseRiskSettings, CoinbaseSettings
from kalshibot.coinbase.paper import SpotOrder, SpotOrderIntent, SpotPortfolioView, SpotPosition
from kalshibot.coinbase.risk import RiskDecision, SpotRiskManager
from kalshibot.coinbase.store import SpotStore

ALT = "ALT-USD"


def D(x: object) -> Decimal:
    return Decimal(str(x))


def view(*, cash: object = 550, equity: object = 1000, held: object = "4.5", value: object = 450,
         strategy: str = "s1", orders: tuple[SpotOrder, ...] = (), day_start: object = 1000) -> SpotPortfolioView:
    """Account with $1,000 equity; s1 holds 4.5 TST worth $450 (liquidation value)."""
    pos = SpotPosition(product_id=PID, strategy="s1", base_currency="TST", quantity=D(held), cost_basis=D(value),
                       liquidation_value=D(value))
    positions = (pos,) if D(held) > 0 else ()
    return SpotPortfolioView(
        ts=T0, strategy=strategy, starting_balance=D(1000), cash=D(cash), reserved_cash=D(0), equity=D(equity),
        equity_mid=D(equity), realized_pnl=D(0), unrealized_pnl=D(0), fees_paid=D(0),
        day_start_equity=D(day_start), allocation_pct=None, alloc_equity=D(equity),
        positions=tuple(p for p in positions if p.strategy == strategy), open_orders=orders,
        all_positions=positions, all_open_orders=orders, best_asks={ALT: D(100)}, mids={ALT: D("99.95")})


def acct(equity: object = 1000, cash: object = 550, todays_pnl: object = 0) -> Any:
    return SimpleNamespace(equity=D(equity), cash=D(cash), todays_pnl=D(todays_pnl))


def rm(**limits: Any) -> tuple[SpotRiskManager, Clock]:
    clock = Clock()
    return SpotRiskManager(CoinbaseRiskSettings(**limits), clock=clock, fee_tier=TIER), clock


def buy(pid: str, q: object, strategy: str = "s1", **kw: Any) -> SpotOrderIntent:
    return SpotOrderIntent(pid, "buy", quote_size=D(q), strategy=strategy, **kw)


# --------------------------------------------------------------------------- dollar limits


def test_ok_when_within_every_limit() -> None:
    r, _ = rm()
    d = r.check(buy(ALT, 100, "s2"), view(), acct())
    # product 500 - 0, total 900 - 450, s2 500 - 0, cash 550 - 20: all >= 100
    assert d.approved and d.approved_quote == D(100) and not d.partial and d.reason == "ok"


def test_product_limit_sizes_down() -> None:
    r, _ = rm()
    d = r.check(buy(PID, 100), view(), acct())
    # max_position_pct_per_product: 50 % x 1000 - 450 held in TST = 50 (s1's allocation is also 50;
    # the first limit that binds is reported)
    assert d.approved_quote == D(50) and d.partial and d.binding_limit == "max_position_pct_per_product"


def test_total_exposure_limit() -> None:
    r, _ = rm(max_total_exposure_pct=50)
    d = r.check(buy(ALT, 100, "s2"), view(), acct())
    assert d.approved_quote == D(50) and d.binding_limit == "max_total_exposure_pct"  # 500 - 450


def test_strategy_allocation_own_and_fallback() -> None:
    r, _ = rm()
    r.set_strategy_allocations({"s2": 5})
    d = r.check(buy(ALT, 100, "s2"), view(), acct())
    assert d.approved_quote == D(50) and d.binding_limit == "max_strategy_allocation_pct"  # 5 % x 1000
    assert "5% for s2" in d.reason
    assert r.allocation_pct("s2") == 5 and r.allocation_pct("other") == 50
    r.set_strategy_allocations({"s2": None})
    assert r.allocation_pct("s2") == 50
    cb = CoinbaseSettings.model_validate({"strategies": {"trend": {"max_allocation_pct": 30}}})
    assert SpotRiskManager(cb).allocation_pct("trend") == 30


def test_cash_reserve_and_min_trade() -> None:
    r, _ = rm()
    d = r.check(buy(ALT, 100, "s2"), view(cash=60), acct(cash=60))
    assert d.approved_quote == D(40) and d.binding_limit == "min_cash_reserve"  # 60 - 20
    d = r.check(buy(ALT, 100, "s2"), view(cash=25), acct(cash=25))
    assert not d.approved and d.approved_quote == 0 and d.binding_limit == "min_cash_reserve"  # 5 < $10
    d = r.check(buy(ALT, "9.99", "s2"), view(), acct())
    assert not d.approved and d.binding_limit == "min_trade_usd"


def test_base_sized_buy_uses_price_plus_taker_fee() -> None:
    r, _ = rm()
    intent = SpotOrderIntent(ALT, "buy", base_size=D(2), order_type="limit", limit_price=D(100), strategy="s2")
    d = r.check(intent, view(cash=60), acct(cash=60))
    # cost per unit = 100 x 1.01 = 101; headroom 40 -> floor(40 / 101, 8 dp) = 0.39603960
    assert d.approved_base == D("0.39603960") and d.approved_quote is None
    d = r.check(SpotOrderIntent(ALT, "buy", base_size=D("0.5"), order_type="limit", limit_price=D(100),
                                strategy="s2"), view(), acct())
    assert d.approved_base == D("0.5") and d.reason == "ok"  # 50.50 is within every limit
    # without a limit the portfolio's best ask prices it; with nothing known it is rejected
    d = r.check(SpotOrderIntent("XYZ-USD", "buy", base_size=D(1), order_type="limit", strategy="s2"),
                view(), acct())
    assert not d.approved and d.binding_limit == "price"


# --------------------------------------------------------------------------- sells


def test_sells_always_pass_and_are_trimmed_to_holdings() -> None:
    r, _ = rm()
    r.set_kill_switch(True, "manual")
    d = r.check(SpotOrderIntent(PID, "sell", base_size=D(1), strategy="s1"), view(cash=0), acct(cash=0))
    assert d.approved_base == D(1) and d.reduces_risk and "kill switch on" in d.reason
    d = r.check(SpotOrderIntent(PID, "sell", base_size=D(5), strategy="s1"), view(), acct())
    assert d.approved_base == D("4.5") and d.binding_limit == "holdings"
    resting = SpotOrder(id=9, product_id=PID, side="sell", order_type="limit", tif="gtc", base_size=D(1),
                        limit_price=D(110), strategy="s1")
    d = r.check(SpotOrderIntent(PID, "sell", base_size=D(5), strategy="s1"), view(orders=(resting,)), acct())
    assert d.approved_base == D("3.5")  # 4.5 held - 1 committed to the resting sell
    d = r.check(SpotOrderIntent(PID, "sell", base_size=D(1), strategy="s2"), view(), acct())
    assert not d.approved and "nothing to sell" in d.reason  # s2 holds none (s1's units are not its)


def test_sells_bypass_spread_and_rate_limit() -> None:
    r, _ = rm(max_orders_per_minute=1)
    wide = make_book(PID, bids=[(90, 1)], asks=[(110, 1)])
    for _ in range(3):
        d = r.check(SpotOrderIntent(PID, "sell", base_size=D(1), strategy="s1"), view(), acct(), book=wide)
        assert d.approved
    assert r.orders_last_minute() == 0  # sells do not use up the buy budget
    assert r.check(buy(ALT, 20, "s1"), view(), acct()).approved


# --------------------------------------------------------------------------- guards


def test_orders_per_minute_is_per_strategy() -> None:
    r, clock = rm(max_orders_per_minute=2)
    assert r.check(buy(ALT, 20, "s2"), view(), acct()).approved
    assert r.check(buy(ALT, 20, "s2"), view(), acct()).approved
    d = r.check(buy(ALT, 20, "s2"), view(), acct())
    assert not d.approved and d.binding_limit == "max_orders_per_minute"
    assert r.check(buy(ALT, 20, "s3"), view(), acct()).approved  # another strategy's budget
    assert r.orders_last_minute() == 3 and r.orders_last_minute(strategy="s2") == 2
    clock.advance(61)
    assert r.check(buy(ALT, 20, "s2"), view(), acct()).approved
    # record=False is a dry run
    r2, _ = rm(max_orders_per_minute=1)
    assert r2.check(buy(ALT, 20), view(), acct(), record=False).approved
    assert r2.check(buy(ALT, 20), view(), acct()).approved


def test_spread_guard() -> None:
    r, _ = rm()
    wide = make_book(ALT, bids=[(99, 1)], asks=[(100, 1)])  # 1 / 99.5 = 100.5 bps > 50
    d = r.check(buy(ALT, 50, "s2"), view(), acct(), book=wide)
    assert not d.approved and d.binding_limit == "max_spread_bps" and "100.5 bps" in d.reason
    tight = make_book(ALT, bids=[("99.9", 1)], asks=[(100, 1)])  # ~10 bps
    assert r.check(buy(ALT, 50, "s2"), view(), acct(), book=tight).approved
    assert not r.check(buy(ALT, 50, "s2"), view(), acct(), spread_bps=51).approved
    assert r.check(buy(ALT, 50, "s2"), view(), acct()).approved  # unknown spread: not checked


def test_daily_loss_trips_kill_switch_and_auto_releases() -> None:
    r, clock = rm()
    d = r.check(buy(ALT, 50, "s2"), view(), acct(todays_pnl=-100))  # -100 <= -$100
    assert not d.approved and d.binding_limit == "kill_switch" and r.kill_switch and r.kill_switch_auto
    assert "daily loss limit" in r.kill_switch_reason
    clock.advance(3600)
    assert not r.check(buy(ALT, 50, "s2"), view(), acct()).approved  # same UTC day: still on
    clock.now = clock.now.replace(hour=0) + timedelta(days=1)
    assert r.check(buy(ALT, 50, "s2"), view(), acct()).approved  # next UTC day: released
    assert not r.kill_switch


def test_daily_loss_without_auto_release_and_manual_trip_stay_on() -> None:
    r, clock = rm()
    r.update_limits({"kill_switch_auto_release": False})
    r.evaluate(acct(todays_pnl=-150))
    assert r.kill_switch
    clock.advance(2 * 86400)
    assert not r.check(buy(ALT, 50, "s2"), view(), acct()).approved
    r2, clock2 = rm()
    r2.set_kill_switch(True, "manual")
    clock2.advance(2 * 86400)
    assert r2.evaluate(acct()) is True
    r2.set_kill_switch(False)
    assert r2.check(buy(ALT, 50, "s2"), view(), acct()).approved
    r3, _ = rm(daily_loss_limit=0)  # 0 disables the daily-loss kill switch
    assert r3.evaluate(acct(todays_pnl=-10_000)) is False


def test_kill_switch_and_overrides_persist() -> None:
    store = SpotStore(":memory:")
    r = SpotRiskManager(CoinbaseSettings(), store=store, clock=Clock())
    r.set_kill_switch(True, "operator")
    r.update_limits({"max_spread_bps": 30, "min_trade_usd": 5})
    r2 = SpotRiskManager(CoinbaseSettings(), store=store, clock=Clock())
    assert r2.kill_switch and r2.kill_switch_reason == "operator" and not r2.kill_switch_auto
    assert r2.limits.max_spread_bps == 30 and r2.limits.min_trade_usd == D(5)
    assert any(l_["kind"] == "risk" and "kill switch ON" in l_["message"] for l_ in store.list_logs())
    with pytest.raises(ValueError):
        r2.update_limits({"bogus": 1})
    with pytest.raises(pydantic.ValidationError):
        r2.update_limits({"min_cash_reserve": -1})
    with pytest.raises(pydantic.ValidationError):
        r2.update_limits({"max_total_exposure_pct": 101})
    assert r2.limits.max_spread_bps == 30  # a failed update changes nothing


def test_invalid_stored_override_is_skipped_not_fatal() -> None:
    # a hand-edited / out-of-bounds override must not stop the venue from starting:
    # max_total_exposure_pct=150 violates le=100 -> ignored (config default 90 stays);
    # the valid min_trade_usd=7 override still applies.
    store = SpotStore(":memory:")
    store.save_risk_limits({"max_total_exposure_pct": 150, "min_trade_usd": 7, "not_a_limit": 1})
    r = SpotRiskManager(CoinbaseSettings(), store=store, clock=Clock())
    assert r.limits.max_total_exposure_pct == 90
    assert r.limits.min_trade_usd == D(7)


# --------------------------------------------------------------------------- reporting / integration


def test_to_json_shape() -> None:
    r, _ = rm()
    r.set_strategy_allocations({"s2": 10})
    out = r.to_json(view(), acct())
    assert out["venue"] == "coinbase" and out["kill_switch"] is False
    assert set(out["limits"]) == {"max_position_pct_per_product", "max_total_exposure_pct",
                                  "max_strategy_allocation_pct", "min_cash_reserve", "max_orders_per_minute",
                                  "daily_loss_limit", "max_spread_bps", "min_trade_usd", "kill_switch_auto_release"}
    u = out["utilization"]
    assert u["total_exposure"] == 450.0 and u["total_exposure_pct"] == 45.0
    assert u["by_product"] == [{"key": PID, "product_id": PID, "exposure": 450.0, "limit": 500.0, "pct": 90.0}]
    rows = {x["strategy"]: x for x in u["by_strategy"]}
    assert rows["s1"]["limit"] == 500.0 and rows["s2"]["limit"] == 100.0 and rows["s2"]["allocation_pct"] == 10
    assert u["daily_pnl"] == 0.0


def test_decision_apply() -> None:
    intent = buy(ALT, 100, "s2", reason="trend")
    d = RiskDecision(approved_quote=D(40), requested_quote=D(100), binding_limit="min_cash_reserve")
    sized = d.apply(intent)
    assert sized.quote_size == D(40) and sized.reason == "trend" and intent.quote_size == D(100)
    assert d.partial and d.approved


async def test_with_a_real_broker_portfolio() -> None:
    b, md = make()
    md.set_book(PID, bids=[("99.9", 50)], asks=[(100, 50)])
    await b.place_order(SpotOrderIntent(PID, "buy", quote_size=D(404), strategy="s1"))  # 4 @ 100 + 4.00 fee
    r = SpotRiskManager(CoinbaseSettings(), fee_tier=TIER, clock=md.clock)
    pv, a = b.portfolio_view("s1", allocation_pct=r.allocation_pct("s1")), b.account()
    # s1 holds 4 x 99.9 = 399.60 less the 4.00 exit fee = 395.60; equity 596 + 395.60 = 991.60
    # -> product cap 495.80 - 395.60 = 100.20
    d = r.check(SpotOrderIntent(PID, "buy", quote_size=D(150), strategy="s1"), pv, a,
                book=await md.book(PID))
    assert d.approved_quote == D("100.20") and d.binding_limit == "max_position_pct_per_product"
    o = await b.place_order(d.apply(SpotOrderIntent(PID, "buy", quote_size=D(150), strategy="s1")))
    assert o.status == "filled" and o.filled_quote + o.fees <= D("100.20")
