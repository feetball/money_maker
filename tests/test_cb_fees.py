"""Coinbase fee schedule + fee math (kalshibot/coinbase/fees.py, docs/COINBASE_CONTRACT.md §5,
docs/coinbase_api_notes.md §3)."""

from decimal import Decimal

import pytest

from kalshibot.coinbase import fees
from kalshibot.coinbase.fees import (
    DEFAULT_TIER,
    FEE_TIERS,
    FeeTier,
    buy_cost,
    custom_tier,
    fee_for,
    get_tier,
    normalize_tier_name,
    notional_for_budget,
    resolve_tier,
    round_trip_bps,
    sell_proceeds,
)

D = Decimal


def test_default_tier_is_current_us_intro_tier():
    """Intro (US), current since 2026-09-16: 0.50% maker / 0.90% taker (notes §3.1, §3.5)."""
    assert DEFAULT_TIER is FEE_TIERS["intro"] and fees.DEFAULT_TIER_NAME == "intro"
    assert DEFAULT_TIER.maker_rate == D("0.005") and DEFAULT_TIER.taker_rate == D("0.009")
    assert DEFAULT_TIER.min_volume_usd == 0  # the lowest-volume retail tier


def test_schedule_matches_notes():
    rates = {k: (t.maker_rate, t.taker_rate) for k, t in FEE_TIERS.items()}
    assert rates == {
        "intro": (D("0.005"), D("0.009")),
        "intro_eu": (D("0.0025"), D("0.005")),
        "intro_intl": (D("0.0009"), D("0.001")),
        "intro_pre_2026_09": (D("0.006"), D("0.012")),
        "vip_8": (D("0"), D("0.0002")),
    }
    for t in FEE_TIERS.values():
        assert isinstance(t.maker_rate, Decimal) and isinstance(t.taker_rate, Decimal)
        assert D(0) <= t.maker_rate <= t.taker_rate < D("0.02"), t.name
        assert t.label


@pytest.mark.parametrize(
    ("notional", "is_taker", "expected"),
    [
        (D("100"), True, D("0.90")),        # exact: no round-up
        (D("100"), False, D("0.50")),
        (D("50"), False, D("0.25")),
        (D("83.33"), True, D("0.75")),      # 0.74997 -> ceil to the cent
        (D("0.01"), True, D("0.01")),       # 0.00009 -> one cent
        (D("1.0000001"), False, D("0.01")),  # 0.0050000005 -> 0.01
        (D("84475.95"), True, D("760.29")),  # 760.28355 -> 760.29
        (D("0"), True, D("0.00")),
        (D("-5"), True, D("0.00")),
    ],
)
def test_fee_for_rounds_up_to_cent(notional, is_taker, expected):
    fee = fee_for(notional, is_taker=is_taker, tier=DEFAULT_TIER)
    assert fee == expected
    assert fee.as_tuple().exponent == -2  # always a cent amount
    assert fee >= notional * DEFAULT_TIER.rate(is_taker=is_taker)  # conservative: never below exact


def test_fee_for_exact_mode_matches_exchange_docs_example():
    """notes §3.2: price 8087.38 x size 0.006018 at 0.5% -> fee 0.2433492642 (unrounded)."""
    notional = D("8087.38") * D("0.006018")
    assert notional == D("48.66985284")
    assert fee_for(notional, is_taker=False, tier=DEFAULT_TIER, precision=None) == D("0.2433492642")
    assert fee_for(notional, is_taker=False, tier=DEFAULT_TIER) == D("0.25")
    assert fee_for(notional, is_taker=False, tier=DEFAULT_TIER, precision=D("0.0001")) == D("0.2434")
    assert fee_for(D(0), is_taker=True, precision=None) == 0


def test_fee_for_other_tiers():
    assert fee_for(D("100"), is_taker=True) == D("0.90")  # default tier
    assert fee_for(D("100"), is_taker=True, tier=FEE_TIERS["intro_pre_2026_09"]) == D("1.20")
    vip = FEE_TIERS["vip_8"]
    assert fee_for(D("1000"), is_taker=False, tier=vip) == D("0.00")
    assert fee_for(D("1000"), is_taker=True, tier=vip) == D("0.20")


def test_buy_cost_and_sell_proceeds():
    assert buy_cost(D("100"), is_taker=True) == D("100.90")
    assert sell_proceeds(D("100"), is_taker=True) == D("99.10")
    assert buy_cost(D("83.33"), is_taker=True) == D("84.08")
    assert sell_proceeds(D("83.33"), is_taker=False, tier=FEE_TIERS["intro_eu"]) == D("83.33") - D("0.21")


@pytest.mark.parametrize("budget", ["0.01", "1", "10", "10.13", "99.99", "100", "100.90", "333.33", "1000", "12345.67"])
@pytest.mark.parametrize("tier_name", sorted(FEE_TIERS))
@pytest.mark.parametrize("is_taker", [True, False])
def test_notional_for_budget_fits_and_is_tight(budget, tier_name, is_taker):
    tier = FEE_TIERS[tier_name]
    b = D(budget)
    n = notional_for_budget(b, is_taker=is_taker, tier=tier)
    assert n >= 0
    assert n + fee_for(n, is_taker=is_taker, tier=tier) <= b
    # tight: one more cent of notional would not fit (or the budget is fully used)
    bigger = n + D("0.01")
    assert bigger + fee_for(bigger, is_taker=is_taker, tier=tier) > b or n == b


def test_notional_for_budget_edges():
    assert notional_for_budget(D(0), is_taker=True) == 0
    assert notional_for_budget(D(-3), is_taker=True) == 0
    assert notional_for_budget(D("100.90"), is_taker=True) == D(100)


@pytest.mark.parametrize(
    ("spelling", "key"),
    [("intro", "intro"), ("Intro", "intro"), (" INTRO ", "intro"), ("intro-us", "intro"), ("Intro US", "intro"),
     ("Intro EU", "intro_eu"), ("intro-intl", "intro_intl"), ("VIP 8", "vip_8"), ("vip8", "vip_8"),
     ("intro_pre_2026_09", "intro_pre_2026_09")],
)
def test_get_tier_accepts_loose_spellings(spelling, key):
    assert normalize_tier_name(spelling) == key
    assert get_tier(spelling) is FEE_TIERS[key]


def test_get_tier_misc():
    assert get_tier(None) is DEFAULT_TIER
    t = custom_tier("0.001", "0.002")
    assert get_tier(t) is t
    with pytest.raises(KeyError, match="unknown Coinbase fee tier"):
        get_tier("platinum")
    with pytest.raises(KeyError):
        get_tier("advanced_1")  # post-2026-09-16 Advanced rates are not public: use fee_rates


def test_resolve_tier_rates_win_over_name():
    t = resolve_tier("intro_eu", {"maker": 0.004, "taker": 0.008})
    assert t.name == "custom" and t.maker_rate == D("0.004") and t.taker_rate == D("0.008")

    class Rates:
        maker = D("0.001")
        taker = D("0.002")

    assert resolve_tier(None, Rates()).taker_rate == D("0.002")
    assert resolve_tier("intro_eu") is FEE_TIERS["intro_eu"]
    assert resolve_tier() is DEFAULT_TIER
    with pytest.raises(ValueError, match="maker"):
        resolve_tier(None, {"taker": 0.01})


@pytest.mark.parametrize("bad", [D("-0.001"), D("1"), D("NaN"), 0.006])
def test_fee_tier_rejects_bad_rates(bad):
    with pytest.raises(ValueError):
        FeeTier("x", bad, D("0.01"))


def test_as_dict_and_round_trip_bps():
    assert DEFAULT_TIER.as_dict() == {"name": "intro", "label": "Intro (US)", "maker_rate": 0.005, "taker_rate": 0.009}
    assert round_trip_bps() == 180.0  # taker in + taker out (notes §3.5)
    assert round_trip_bps(DEFAULT_TIER, is_taker=False) == 100.0
    assert fees.fee_rate(DEFAULT_TIER, is_taker=True) == D("0.009")
