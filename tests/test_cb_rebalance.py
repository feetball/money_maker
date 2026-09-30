"""Coinbase rebalance planner (contract §9): deterministic target weights -> spot order intents."""

from __future__ import annotations

import random
from decimal import Decimal
from typing import Any

import pytest

from kalshibot.coinbase.models import Product
from kalshibot.coinbase.paper import SpotOrderIntent
from kalshibot.coinbase.rebalance import floor_to, plan_rebalance, plan_rebalance_detailed
from kalshibot.coinbase.strategies.base import TargetWeight

D = Decimal


def prod(pid: str, *, base_inc: str = "0.00000001", min_funds: str = "1", status: str = "online",
         disabled: bool = False) -> Product:
    base, _, quote = pid.partition("-")
    return Product(product_id=pid, base_currency=base, quote_currency=quote, base_increment=D(base_inc),
                   quote_increment=D("0.01"), min_market_funds=D(min_funds), status=status,
                   trading_disabled=disabled, post_only=False, limit_only=False, cancel_only=False)


PRODUCTS = {
    "BTC-USD": prod("BTC-USD"),
    "ETH-USD": prod("ETH-USD", base_inc="0.0001"),
    "DOGE-USD": prod("DOGE-USD", base_inc="1"),
    "BIG-USD": prod("BIG-USD", min_funds="25"),
    "OFF-USD": prod("OFF-USD", status="delisted"),
}
PRICES = {"BTC-USD": D("50000"), "ETH-USD": D("2000"), "DOGE-USD": D("0.1"), "BIG-USD": D("10"),
          "OFF-USD": D("5")}


def plan(targets: Any, holdings: dict[str, Any] | None = None, *, alloc: Any = 1000, band: float = 0.02,
         min_trade: Any = 10, **kw: Any) -> list[SpotOrderIntent]:
    return plan_rebalance(targets, holdings or {}, PRICES, D(str(alloc)), PRODUCTS, band=band,
                          min_trade_usd=min_trade, strategy="s1", **kw)


def summary(intents: list[SpotOrderIntent]) -> list[tuple[str, str, Decimal | None]]:
    return [(i.product_id, i.side, i.quote_size if i.side == "buy" else i.base_size) for i in intents]


def test_entry_from_cash() -> None:
    out = plan([TargetWeight("ETH-USD", 0.3, "momentum", expected_edge_bps=40), TargetWeight("BTC-USD", 0.5, "trend")])
    assert summary(out) == [("BTC-USD", "buy", D("500.00")), ("ETH-USD", "buy", D("300.00"))]
    btc, eth = out
    assert (btc.order_type, btc.tif, btc.strategy, btc.base_size) == ("market", "ioc", "s1", None)
    assert btc.target_weight == 0.5 and btc.reason.startswith("trend; enter 0.0% -> 50.0%")
    assert eth.expected_edge_bps == 40
    assert all(isinstance(i.quote_size, Decimal) for i in out)


def test_none_means_no_change_and_empty_means_all_cash() -> None:
    held = {"BTC-USD": D("0.01"), "ETH-USD": D("0.1")}
    assert plan(None, held) == []
    out = plan([], held)
    assert summary(out) == [("BTC-USD", "sell", D("0.01")), ("ETH-USD", "sell", D("0.1"))]
    assert all(i.reason.startswith("exit: not in targets") and i.target_weight == 0.0 for i in out)


def test_sells_before_buys_and_products_not_in_targets_go_to_zero() -> None:
    held = {"ETH-USD": D("0.3"), "DOGE-USD": D("1000")}  # $600 ETH, $100 DOGE
    out = plan([TargetWeight("ETH-USD", 0.2), TargetWeight("BTC-USD", 0.5)], held)
    # sells (sorted) first, then buys
    assert summary(out) == [("DOGE-USD", "sell", D("1000")), ("ETH-USD", "sell", D("0.2000")),
                            ("BTC-USD", "buy", D("500.00"))]


def test_band_applies_to_resizes_only() -> None:
    held = {"BTC-USD": D("0.0098")}  # $490 of a $1000 allocation
    assert plan([TargetWeight("BTC-USD", 0.5)], held) == []  # +$10 < 2% band ($20)
    assert summary(plan([TargetWeight("BTC-USD", 0.53)], held)) == [("BTC-USD", "buy", D("40.00"))]
    assert summary(plan([TargetWeight("BTC-USD", 0.45)], held)) == [("BTC-USD", "sell", D("0.0008"))]
    detailed = plan_rebalance_detailed([TargetWeight("BTC-USD", 0.5)], held, PRICES, D(1000), PRODUCTS, band=0.02,
                                       min_trade_usd=10, strategy="s1")
    assert detailed.intents == [] and "band" in detailed.skipped[0]["reason"]
    assert detailed.current_weights["BTC-USD"] == pytest.approx(0.49)
    # entries and full exits are not banded (only the minimum trade applies)
    assert summary(plan([TargetWeight("ETH-USD", 0.015)], band=0.02)) == [("ETH-USD", "buy", D("15.00"))]
    assert summary(plan([], {"ETH-USD": D("0.0075")}, band=0.02)) == [("ETH-USD", "sell", D("0.0075"))]


def test_minimum_trade_and_min_market_funds() -> None:
    assert plan([TargetWeight("BTC-USD", 0.009)]) == []  # $9 < $10 minimum trade
    # BIG-USD needs min_market_funds $25: a $20 target is dust -> nothing to buy
    d = plan_rebalance_detailed([TargetWeight("BIG-USD", 0.02)], {}, PRICES, D(1000), PRODUCTS, band=0,
                                min_trade_usd=10, strategy="s1")
    assert d.intents == [] and d.target_weights["BIG-USD"] == 0.0
    assert summary(plan([TargetWeight("BIG-USD", 0.03)], band=0)) == [("BIG-USD", "buy", D("30.00"))]
    # a holding worth less than the minimum can't be sold (dust stays)
    d = plan_rebalance_detailed([], {"BTC-USD": D("0.0001")}, PRICES, D(1000), PRODUCTS, band=0, min_trade_usd=10,
                                strategy="s1")  # $5
    assert d.intents == [] and "minimum trade" in d.skipped[0]["reason"]


def test_dust_target_becomes_full_exit() -> None:
    held = {"ETH-USD": D("0.25")}  # $500
    out = plan([TargetWeight("ETH-USD", 0.005)], held)  # $5 target < $10 minimum -> flat
    assert summary(out) == [("ETH-USD", "sell", D("0.25"))]


def test_increment_rounding_and_no_shorting() -> None:
    out = plan([TargetWeight("DOGE-USD", 0.1234567)], band=0)
    assert summary(out) == [("DOGE-USD", "buy", D("123.45"))]  # cents, rounded down
    held = {"DOGE-USD": D("1234"), "ETH-USD": D("0.12345")}  # ETH off-grid holding
    out = plan([TargetWeight("DOGE-USD", 0.05)], held, band=0)
    sells = {i.product_id: i.base_size for i in out}
    assert sells["DOGE-USD"] == D("734")  # (123.4 - 50) / 0.1 = 734 units, base increment 1
    assert sells["ETH-USD"] == D("0.1234")  # rounded DOWN to 0.0001, never above the holding
    for i in out:
        assert i.base_size is not None and i.base_size <= D(str(held[i.product_id]))
    assert floor_to(D("0.123456789"), D("0.00000001")) == D("0.12345678")
    assert floor_to(D("7"), D("5")) == D("5")
    assert floor_to(D("-1"), D("0.1")) == 0


def test_sum_above_one_is_scaled_and_negative_weights_clamped() -> None:
    d = plan_rebalance_detailed([TargetWeight("BTC-USD", 0.8), TargetWeight("ETH-USD", 0.8),
                                 TargetWeight("DOGE-USD", -0.5)], {}, PRICES, D(1000), PRODUCTS, band=0,
                                min_trade_usd=10, strategy="s1")
    assert summary(d.intents) == [("BTC-USD", "buy", D("500.00")), ("ETH-USD", "buy", D("500.00"))]
    assert any("scaled down" in p for p in d.problems) and any("no shorting" in p for p in d.problems)


def test_cash_limits_buys_pro_rata() -> None:
    held = {"ETH-USD": D("0.1")}  # $200, fully exited below
    d = plan_rebalance_detailed([TargetWeight("BTC-USD", 0.6), TargetWeight("DOGE-USD", 0.3)], held, PRICES,
                                D(1000), PRODUCTS, band=0, min_trade_usd=10, strategy="s1", cash=D("400"),
                                fee_rate=D("0.012"))
    # budget = 400 + 200 x (1 - 1.2%) = 597.60 for 900 of buys -> scale 0.664
    assert summary(d.intents) == [("ETH-USD", "sell", D("0.1")), ("BTC-USD", "buy", D("398.40")),
                                  ("DOGE-USD", "buy", D("199.20"))]
    assert sum(i.quote_size for i in d.buys) <= D("597.60")
    assert "scaled to cash" in d.buys[0].reason
    # too little cash: buys below the minimum trade are dropped
    d = plan_rebalance_detailed([TargetWeight("BTC-USD", 0.5), TargetWeight("ETH-USD", 0.01)], {}, PRICES,
                                D(1000), PRODUCTS, band=0, min_trade_usd=10, strategy="s1", cash=D("300"))
    assert summary(d.intents) == [("BTC-USD", "buy", D("294.11"))]
    assert any(s["reason"] == "not enough cash" for s in d.skipped)


def test_untradable_unknown_and_unpriced_products_are_skipped() -> None:
    d = plan_rebalance_detailed([TargetWeight("OFF-USD", 0.2), TargetWeight("BTC-USD", 0.2)],
                                {"NOPE-USD": D("3"), "ETH-USD": D("1")}, {**PRICES, "NOPE-USD": D("10"),
                                                                          "ETH-USD": None},
                                D(1000), PRODUCTS, band=0, min_trade_usd=10, strategy="s1")
    assert summary(d.intents) == [("BTC-USD", "buy", D("200.00"))]
    reasons = {s["product_id"]: s["reason"] for s in d.skipped}
    assert "not tradable" in reasons["OFF-USD"]
    assert reasons["NOPE-USD"] == "unknown product"
    assert reasons["ETH-USD"] == "no price"


def test_deterministic_and_order_independent() -> None:
    targets = [TargetWeight(p, w) for p, w in [("BTC-USD", 0.3), ("ETH-USD", 0.25), ("DOGE-USD", 0.2),
                                               ("BIG-USD", 0.1)]]
    held = {"ETH-USD": D("0.4"), "DOGE-USD": D("500"), "BTC-USD": D("0.001")}
    ref = summary(plan(targets, held, cash=D("100"), fee_rate=D("0.012")))
    rng = random.Random(7)
    for _ in range(10):
        t2 = targets[:]
        rng.shuffle(t2)
        h2 = dict(rng.sample(sorted(held.items()), len(held)))
        assert summary(plan(t2, h2, cash=D("100"), fee_rate=D("0.012"))) == ref
    sides = [s for _, s, _ in ref]
    assert sides == sorted(sides, key=lambda s: s != "sell")  # every sell before every buy


def test_mapping_targets_and_zero_allocation() -> None:
    assert summary(plan({"BTC-USD": 0.1})) == [("BTC-USD", "buy", D("100.00"))]
    # no allocation equity: nothing to buy, holdings go to zero
    assert summary(plan([TargetWeight("BTC-USD", 1.0)], {"ETH-USD": D("0.1")}, alloc=0)) == [
        ("ETH-USD", "sell", D("0.1"))]
