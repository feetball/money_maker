"""Fee model + rounding tests.

Worked examples: docs/kalshi_api_notes.md §1.4 (reproduces the Kalshi Fee Schedule PDF
tables) and docs.kalshi.com/getting_started/fee_rounding.
"""

import random
from decimal import Decimal
from types import SimpleNamespace

import pytest

from kalshibot.fees import (
    PRECISION_DIRECT,
    PRECISION_FCM,
    OrderFeeAccumulator,
    ceil_6dp,
    fee_rate,
    raw_fee,
    resolve_fee_params,
    round_fill,
    trading_fee,
)
from kalshibot.money import CENT, ZERO, D, ceil_to

# P | 1 taker | 10 taker | 100 taker | 100 @ M=0.5 | 100 maker (0.0175) | raw/contract | 1 direct
NOTES_TABLE = [
    ("0.01", "0.01", "0.01", "0.07", "0.04", "0.02", "0.000693", "0.0007"),
    ("0.05", "0.01", "0.04", "0.34", "0.17", "0.09", "0.003325", "0.0034"),
    ("0.10", "0.01", "0.07", "0.63", "0.32", "0.16", "0.0063", "0.0063"),
    ("0.20", "0.02", "0.12", "1.12", "0.56", "0.28", "0.0112", "0.0112"),
    ("0.30", "0.02", "0.15", "1.47", "0.74", "0.37", "0.0147", "0.0147"),
    ("0.40", "0.02", "0.17", "1.68", "0.84", "0.42", "0.0168", "0.0168"),
    ("0.50", "0.02", "0.18", "1.75", "0.88", "0.44", "0.0175", "0.0175"),
    ("0.70", "0.02", "0.15", "1.47", "0.74", "0.37", "0.0147", "0.0147"),
    ("0.90", "0.01", "0.07", "0.63", "0.32", "0.16", "0.0063", "0.0063"),
    ("0.99", "0.01", "0.01", "0.07", "0.04", "0.02", "0.000693", "0.0007"),
]


@pytest.mark.parametrize("row", NOTES_TABLE, ids=[r[0] for r in NOTES_TABLE])
def test_worked_examples_table(row):
    p, t1, t10, t100, half100, maker100, raw1, direct1 = (D(x) for x in row)
    assert trading_fee(p, 1, is_taker=True) == t1
    assert trading_fee(p, 10, is_taker=True) == t10
    assert trading_fee(p, 100, is_taker=True) == t100
    assert trading_fee(p, 100, is_taker=True, fee_multiplier=D("0.5")) == half100
    # the 0.035 "Specific Trading Fees Table" (fee_type flat) gives the same column
    assert trading_fee(p, 100, is_taker=True, fee_type="flat") == half100
    assert trading_fee(p, 100, is_taker=False, fee_type="quadratic_with_maker_fees") == maker100
    assert raw_fee(p, 1, is_taker=True) == raw1
    assert trading_fee(p, 1, is_taker=True, precision=PRECISION_DIRECT) == direct1


# docs/kalshi_api_notes.md §1.4 (buyer side): P | C | taker raw | taker @1c | taker @0.01c |
# maker (k=0.25) raw | maker @1c | maker @0.01c | MLB pre-game (M=0.5) taker @0.01c
NOTES_EXACT_TABLE = [
    ("0.01", "1", "0.000693", "0.01", "0.0007", "0.000173", "0.01", "0.0002", "0.0004"),
    ("0.01", "100", "0.0693", "0.07", "0.0693", "0.017325", "0.02", "0.0174", "0.0347"),
    ("0.05", "1", "0.003325", "0.01", "0.0034", "0.000831", "0.01", "0.0009", "0.0017"),
    ("0.05", "100", "0.3325", "0.34", "0.3325", "0.083125", "0.09", "0.0832", "0.1663"),
    ("0.10", "10", "0.063", "0.07", "0.0630", "0.01575", "0.02", "0.0158", "0.0315"),
    ("0.25", "1", "0.013125", "0.02", "0.0132", "0.003281", "0.01", "0.0033", "0.0066"),
    ("0.25", "100", "1.3125", "1.32", "1.3125", "0.328125", "0.33", "0.3282", "0.6563"),
    ("0.50", "1", "0.0175", "0.02", "0.0175", "0.004375", "0.01", "0.0044", "0.0088"),
    ("0.50", "10", "0.175", "0.18", "0.1750", "0.04375", "0.05", "0.0438", "0.0875"),
    ("0.50", "100", "1.75", "1.75", "1.7500", "0.4375", "0.44", "0.4375", "0.8750"),
    ("0.50", "0.5", "0.00875", "0.01", "0.0088", "0.002188", "0.01", "0.0022", "0.0044"),
    ("0.90", "100", "0.63", "0.63", "0.6300", "0.1575", "0.16", "0.1575", "0.3150"),
    ("0.99", "100", "0.0693", "0.07", "0.0693", "0.017325", "0.02", "0.0174", "0.0347"),
    ("0.005", "100", "0.034825", "0.04", "0.0349", "0.008706", "0.01", "0.0088", "0.0175"),
    ("0.999", "100", "0.006993", "0.01", "0.0070", "0.001748", "0.01", "0.0018", "0.0035"),
]


@pytest.mark.parametrize("row", NOTES_EXACT_TABLE, ids=[f"{r[0]}x{r[1]}" for r in NOTES_EXACT_TABLE])
def test_worked_examples_exact_rule(row):
    p, c, t_raw, t_cent, t_cc, m_raw, m_cent, m_cc, mlb_cc = (D(x) for x in row)
    q6 = Decimal("0.000001")
    mk = {"fee_type": "quadratic_with_maker_fees"}
    assert raw_fee(p, c, is_taker=True).quantize(q6) == t_raw.quantize(q6)
    assert trading_fee(p, c, is_taker=True, precision=PRECISION_FCM) == t_cent
    assert trading_fee(p, c, is_taker=True, precision=PRECISION_DIRECT) == t_cc
    assert raw_fee(p, c, is_taker=False, **mk).quantize(q6, rounding="ROUND_HALF_UP") == m_raw
    assert trading_fee(p, c, is_taker=False, precision=PRECISION_FCM, **mk) == m_cent
    assert trading_fee(p, c, is_taker=False, precision=PRECISION_DIRECT, **mk) == m_cc
    assert trading_fee(p, c, is_taker=True, fee_multiplier=D("0.5"), precision=PRECISION_DIRECT) == mlb_cc


def test_notes_subpenny_fractional_examples():
    # notes §1.3: buy 0.5 YES at $0.005 -> direct: balance -0.0027 (fee 0.0002); FCM: -0.01 (fee 0.0075)
    for prec, change, fee in ((PRECISION_DIRECT, "-0.0027", "0.0002"), (PRECISION_FCM, "-0.01", "0.0075")):
        acc = OrderFeeAccumulator(precision=prec)
        f = acc.on_fill(D("0.005"), D("0.5"), is_taker=True)
        # model fee 0.000174125 -> ceil_6dp = 0.000175 (the notes print 0.000174: a typo;
        # the balance change and effective fee below match the notes)
        assert f.model_fee == D("0.000174125") and f.trade_fee == D("0.000175")
        assert f.balance_change == D(change)
        assert f.net_fee == D(fee)


def test_key_prices_exact_raw():
    assert raw_fee(D("0.50"), 1, is_taker=True) == D("0.0175")
    assert raw_fee(D("0.01"), 1, is_taker=True) == D("0.000693")
    assert raw_fee(D("0.99"), 1, is_taker=True) == D("0.000693")
    # symmetric in P <-> 1-P: a YES buyer at P and the NO side at 1-P pay the same
    for c in range(1, 100):
        p = D(c) / 100
        assert raw_fee(p, 7, is_taker=True) == raw_fee(1 - p, 7, is_taker=True)


def test_one_cent_fee_is_100pct_of_premium_under_cent_rounding():
    assert trading_fee(D("0.01"), 1, is_taker=True) == D("0.01")


class TestMultipliersAndTypes:
    def test_half_multiplier(self):
        assert raw_fee(D("0.5"), 100, is_taker=True, fee_multiplier=D("0.5")) == D("0.875")
        assert trading_fee(D("0.5"), 100, is_taker=True, fee_multiplier=0.5) == D("0.88")

    def test_zero_multiplier_is_fee_free(self):
        for p in ("0.01", "0.5", "0.99"):
            assert trading_fee(D(p), 100, is_taker=True, fee_multiplier=0) == ZERO
            assert trading_fee(D(p), 100, is_taker=False, fee_type="quadratic_with_maker_fees",
                               fee_multiplier=D(0)) == ZERO

    def test_makers_free_on_quadratic(self):
        assert fee_rate("quadratic", 1, is_taker=False) == ZERO
        assert trading_fee(D("0.5"), 100, is_taker=False) == ZERO

    def test_maker_rates(self):
        assert fee_rate("quadratic_with_maker_fees", 1, is_taker=False) == D("0.0175")
        assert fee_rate("quadratic_with_combo_maker_fees", 1, is_taker=False) == D("0.035")
        assert fee_rate("quadratic_with_maker_fees", D("0.5"), is_taker=False) == D("0.00875")
        assert fee_rate("flat", 1, is_taker=False) == ZERO
        # maker fee at 0.50 for 100 contracts: 0.0175*100*0.25 = 0.4375 -> 0.44
        assert trading_fee(D("0.5"), 100, is_taker=False, fee_type="quadratic_with_maker_fees") == D("0.44")
        assert trading_fee(D("0.5"), 100, is_taker=False, fee_type="quadratic_with_combo_maker_fees") == D("0.88")

    def test_taker_rates(self):
        for ft in ("quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"):
            assert fee_rate(ft, 1, is_taker=True) == D("0.07")
        assert fee_rate("flat", 1, is_taker=True) == D("0.035")

    def test_unknown_type_is_conservative_and_warns(self):
        with pytest.warns(RuntimeWarning):
            assert fee_rate("some_future_type", 1, is_taker=True) == D("0.07")
        assert fee_rate("some_future_type", 1, is_taker=False) == D("0.0175")

    def test_validation(self):
        with pytest.raises(ValueError):
            raw_fee(D("1.5"), 1, is_taker=True)
        with pytest.raises(ValueError):
            raw_fee(D("0.5"), -1, is_taker=True)
        with pytest.raises(ValueError):
            fee_rate("quadratic", -1, is_taker=True)

    def test_zero_count(self):
        assert trading_fee(D("0.5"), 0, is_taker=True) == ZERO


class TestRoundingRule:
    def test_ceil_6dp(self):
        assert ceil_6dp(D("0.00363825")) == D("0.003639")
        assert ceil_6dp(D("0.000693")) == D("0.000693")
        assert ceil_6dp(D("0.0000001")) == D("0.000001")

    def test_doc_fcm_example(self):
        # docs.kalshi.com/getting_started/fee_rounding "FCM-cleared fill"
        r = round_fill(D("-0.055"), D("0.00363825"), ZERO, CENT)
        assert r.trade_fee == D("0.003639")
        assert r.balance_change == D("-0.06")
        assert r.rounding_fee == D("0.001361")
        assert r.trade_fee + r.rounding_fee == D("0.005")
        assert r.rebate == ZERO
        assert r.net_fee == D("0.005")
        assert r.accumulator == D("0.001361")

    def test_doc_example_via_trading_fee(self):
        # 1 contract bought at a sub-penny price 0.055: fee incl. principal rounding = 0.005
        assert raw_fee(D("0.055"), 1, is_taker=True) == D("0.00363825")
        assert trading_fee(D("0.055"), 1, is_taker=True) == D("0.005")
        # direct member: -0.058639 floors to -0.0587 -> fee 0.0037
        assert trading_fee(D("0.055"), 1, is_taker=True, precision=PRECISION_DIRECT) == D("0.0037")

    def test_doc_accumulator_table(self):
        # three fills each adding 0.004 of rounding; the third has enough fee to cover a 0.01 rebate
        acc = ZERO
        rows = []
        for _ in range(3):
            r = round_fill(D("-0.50"), D("0.006"), acc, CENT)
            assert r.rounding_fee == D("0.004")
            rows.append((r.rebate, r.accumulator))
            acc = r.accumulator
        assert rows == [(ZERO, D("0.004")), (ZERO, D("0.008")), (D("0.01"), D("0.002"))]

    def test_rebate_capped_so_net_fee_not_negative(self):
        # accumulator already holds 0.009; a fee-free fill with on-grid principal cannot rebate
        r = round_fill(D("-0.50"), ZERO, D("0.009"), CENT)
        assert r.rounding_fee == ZERO and r.rebate == ZERO and r.net_fee == ZERO
        # accumulator 0.012, a fill whose gross fee is exactly 0.01 -> rebate 0.01, net 0
        r = round_fill(D("-0.50"), D("0.006"), D("0.008"), CENT)
        assert r.rebate == D("0.01") and r.net_fee == ZERO and r.accumulator == D("0.002")

    def test_sell_side_revenue(self):
        # selling: revenue positive, balance floors toward -inf -> still pays up to the cent
        r = round_fill(D("2.75"), D("0.003639"), ZERO, CENT)
        assert r.balance_change == D("2.74")
        assert r.net_fee == D("0.01")
        assert trading_fee(D("0.55"), 5, is_taker=True, is_buy=False) == trading_fee(D("0.55"), 5, is_taker=True)

    def test_principal_rounding_counts_as_fee(self):
        # 0.03 contracts x $0.555 = 0.01665 principal; maker on quadratic (no model fee)
        assert trading_fee(D("0.555"), D("0.03"), is_taker=False, precision=CENT) == D("0.00335")
        assert trading_fee(D("0.555"), D("0.03"), is_taker=False, precision=PRECISION_DIRECT) == D("0.00005")

    def test_balance_change_always_on_grid(self):
        rng = random.Random(7)
        for prec in (PRECISION_FCM, PRECISION_DIRECT):
            acc = OrderFeeAccumulator(precision=prec)
            for _ in range(200):
                p = D(rng.randint(1, 999)) / 1000
                c = D(rng.randint(1, 5000)) / 100
                f = acc.on_fill(p, c, is_buy=rng.random() < 0.5, is_taker=rng.random() < 0.7,
                                fee_type="quadratic_with_maker_fees")
                assert f.balance_change % prec == 0
                assert f.net_fee >= 0
                assert ZERO <= f.accumulator
                assert f.balance_change == f.revenue - f.net_fee


class TestOrderAccumulator:
    def test_multi_fill_equals_cumulative_ceiling(self):
        """Whole contracts at whole-cent prices: total charged == ceil_to(precision, sum trade fees)
        after every fill (the per-order rule of ARCHITECTURE.md §5)."""
        rng = random.Random(42)
        for prec in (CENT, PRECISION_DIRECT):
            for _ in range(200):
                acc = OrderFeeAccumulator(precision=prec)
                total_trade = ZERO
                for _ in range(rng.randint(1, 6)):
                    p = D(rng.randint(1, 99)) / 100
                    c = rng.randint(1, 40)
                    f = acc.on_fill(p, c, is_taker=True)
                    total_trade += f.trade_fee
                    assert acc.total_fee == ceil_to(total_trade, prec)

    def test_multi_level_walk_cheaper_than_per_fill_rounding(self):
        acc = OrderFeeAccumulator()
        fills = [(D("0.40"), 3), (D("0.41"), 2), (D("0.42"), 1)]
        per_fill = sum(trading_fee(p, c, is_taker=True) for p, c in fills)
        for p, c in fills:
            acc.on_fill(p, c, is_taker=True)
        raw = sum(raw_fee(p, c, is_taker=True) for p, c in fills)
        assert acc.total_fee == ceil_to(raw, CENT) == D("0.11")
        assert per_fill == D("0.12")
        assert acc.total_balance_change == -(sum(p * c for p, c in fills) + acc.total_fee)

    def test_taker_then_maker_share_accumulator(self):
        acc = OrderFeeAccumulator()
        acc.on_fill(D("0.50"), 1, is_taker=True, fee_type="quadratic_with_maker_fees")  # 0.0175 -> 0.02
        assert acc.total_fee == D("0.02")
        assert acc.accumulator == D("0.0025")
        acc.on_fill(D("0.50"), 1, is_taker=False, fee_type="quadratic_with_maker_fees")  # 0.004375
        assert acc.total_fee == ceil_to(D("0.0175") + D("0.004375"), CENT) == D("0.03")

    def test_state_roundtrip(self):
        acc = OrderFeeAccumulator(precision=PRECISION_DIRECT)
        acc.on_fill(D("0.055"), 1, is_taker=True)
        again = OrderFeeAccumulator.from_state(acc.state)
        assert again.precision == PRECISION_DIRECT
        assert again.accumulator == acc.accumulator
        assert again.total_fee == acc.total_fee


class TestResolveFeeParams:
    def test_series_only(self):
        s = SimpleNamespace(fee_type="quadratic_with_maker_fees", fee_multiplier=D("0.5"))
        assert resolve_fee_params(s) == ("quadratic_with_maker_fees", D("0.5"))

    def test_event_override_wins(self):
        s = SimpleNamespace(fee_type="quadratic", fee_multiplier=D("0.5"))
        e = SimpleNamespace(fee_type_override="quadratic", fee_multiplier_override=Decimal(1))
        assert resolve_fee_params(s, e) == ("quadratic", D(1))

    def test_cleared_override_falls_back(self):
        s = SimpleNamespace(fee_type="quadratic", fee_multiplier=D("0.5"))
        e = SimpleNamespace(fee_type_override=None, fee_multiplier_override=None)
        assert resolve_fee_params(s, e) == ("quadratic", D("0.5"))

    def test_defaults(self):
        assert resolve_fee_params(SimpleNamespace()) == ("quadratic", D(1))
