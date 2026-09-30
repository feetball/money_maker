from decimal import Decimal

import pytest

from kalshibot.money import (
    CENT,
    DEFAULT_PRICE_RANGES,
    ONE,
    ZERO,
    D,
    PriceRange,
    ceil_cent,
    ceil_to,
    clamp_price,
    f4,
    floor_cent,
    floor_to,
    is_on_grid,
    is_valid_price,
    max_valid_price,
    min_valid_price,
    next_price_down,
    next_price_up,
    price,
    q4,
    round_to,
    snap_price,
    tick_at,
)

# Real structures from docs.kalshi.com/getting_started/fixed_point_migration
TAPERED = (
    PriceRange(D("0"), D("0.1"), D("0.001")),
    PriceRange(D("0.1"), D("0.9"), D("0.01")),
    PriceRange(D("0.9"), D("1"), D("0.001")),
)
EDGE_HALF = (
    PriceRange(D("0"), D("0.1"), D("0.005")),
    PriceRange(D("0.1"), D("0.9"), D("0.01")),
    PriceRange(D("0.9"), D("1"), D("0.005")),
)
DECI_CENTI = (  # center_deci_edge_centi_cent (combos)
    PriceRange(D("0"), D("0.01"), D("0.0001")),
    PriceRange(D("0.01"), D("0.99"), D("0.001")),
    PriceRange(D("0.99"), D("1"), D("0.0001")),
)


class TestD:
    def test_float_goes_through_repr(self):
        assert D(0.1) == Decimal("0.1")
        assert D(0.93) == Decimal("0.93")
        assert D(1e-6) == Decimal("0.000001")

    def test_int_str_decimal(self):
        assert D(5) == Decimal(5)
        assert D(" 0.5500 ") == Decimal("0.55")
        x = Decimal("0.12")
        assert D(x) is x

    @pytest.mark.parametrize("bad", [None, True, [1]])
    def test_rejects(self, bad):
        with pytest.raises(TypeError):
            D(bad)

    def test_rejects_nan(self):
        with pytest.raises(ValueError):
            D(float("nan"))

    def test_constants(self):
        assert (ZERO, ONE, CENT) == (Decimal(0), Decimal(1), Decimal("0.01"))


class TestRounding:
    def test_ceil_floor_cent(self):
        assert ceil_cent(D("0.0101")) == D("0.02")
        assert ceil_cent(D("0.01")) == D("0.01")
        assert ceil_cent(D("-0.015")) == D("-0.01")  # toward +inf
        assert floor_cent(D("-0.055")) == D("-0.06")  # toward -inf
        assert floor_cent(D("0.019")) == D("0.01")

    def test_q4(self):
        assert q4(D("0.12345")) == D("0.1235")
        assert str(q4(D("0.5"))) == "0.5000"

    def test_to_step(self):
        assert ceil_to(D("0.0001"), D("0.0001")) == D("0.0001")
        assert floor_to(D("0.03999"), D("0.005")) == D("0.035")
        assert round_to(D("0.0325"), D("0.005")) == D("0.035")  # half up
        with pytest.raises(ValueError):
            round_to(1, 0)
        with pytest.raises(ValueError):
            round_to(1, CENT, "sideways")  # type: ignore[arg-type]

    def test_f4(self):
        assert f4(D("0.123456")) == 0.1235
        assert f4(None) is None


class TestPriceUniformTick:
    def test_nearest_down_up(self):
        assert price(0.555) == D("0.56")
        assert price(0.554) == D("0.55")
        assert price(0.559, mode="down") == D("0.55")
        assert price(0.551, mode="up") == D("0.56")
        assert price(D("0.55")) == D("0.55")

    def test_custom_tick(self):
        assert price(0.1234, D("0.005")) == D("0.125")
        assert price(0.1234, D("0.001"), "down") == D("0.123")

    def test_clamp(self):
        assert price(0.999) == D("1.00")
        assert price(0.999, clamp=True) == D("0.99")
        assert price(0.001, clamp=True) == D("0.01")

    def test_four_dp_output(self):
        assert str(price(0.5)) == "0.5000"


class TestGrid:
    def test_default_is_cent(self):
        assert DEFAULT_PRICE_RANGES == (PriceRange(ZERO, ONE, CENT),)
        assert tick_at(D("0.5")) == CENT
        assert snap_price(0.555) == D("0.56")

    @pytest.mark.parametrize(
        "p,tick",
        [("0.05", "0.001"), ("0.0999", "0.001"), ("0.10", "0.01"), ("0.5", "0.01"),
         ("0.8999", "0.01"), ("0.90", "0.001"), ("1.0", "0.001"), ("0", "0.001")],
    )
    def test_tick_at_tapered(self, p, tick):
        assert tick_at(D(p), TAPERED) == D(tick)

    def test_tick_at_outside(self):
        assert tick_at(D("-1"), TAPERED) == D("0.001")
        assert tick_at(D("2"), TAPERED) == D("0.001")

    @pytest.mark.parametrize(
        "x,nearest,down,up",
        [
            ("0.0543", "0.054", "0.054", "0.055"),
            ("0.0995", "0.1", "0.099", "0.1"),
            ("0.1004", "0.1", "0.1", "0.11"),  # no 0.101 in the center band
            ("0.105", "0.11", "0.1", "0.11"),
            ("0.8951", "0.9", "0.89", "0.9"),
            ("0.9005", "0.901", "0.9", "0.901"),
            ("0.5", "0.5", "0.5", "0.5"),
        ],
    )
    def test_snap_tapered(self, x, nearest, down, up):
        assert snap_price(x, TAPERED) == D(nearest)
        assert snap_price(x, TAPERED, "down") == D(down)
        assert snap_price(x, TAPERED, "up") == D(up)

    def test_snap_clamps_to_open_interval(self):
        assert snap_price(0.99995, TAPERED) == D("0.999")
        assert snap_price(1.2, TAPERED) == D("0.999")
        assert snap_price(-0.3, TAPERED) == D("0.001")
        assert snap_price(0.00001, TAPERED, clamp=False) == D("0")
        assert snap_price(0.0004, DECI_CENTI) == D("0.0004")
        assert snap_price(0.9999, DECI_CENTI) == D("0.9999")

    def test_snap_half_cent_edges(self):
        assert snap_price(0.0321, EDGE_HALF) == D("0.030")
        assert snap_price(0.0326, EDGE_HALF) == D("0.035")
        assert snap_price(0.937, EDGE_HALF, "up") == D("0.94")
        assert snap_price(0.555, EDGE_HALF) == D("0.56")

    def test_price_with_ranges(self):
        assert price(0.0543, TAPERED) == D("0.054")
        assert price(0.9999, TAPERED, clamp=True) == D("0.999")

    def test_next_prices(self):
        assert next_price_up(D("0.1"), TAPERED) == D("0.11")
        assert next_price_down(D("0.1"), TAPERED) == D("0.099")
        assert next_price_up(D("0.9"), TAPERED) == D("0.901")
        assert next_price_down(D("0.9"), TAPERED) == D("0.89")
        assert next_price_up(D("0.55")) == D("0.56")
        assert next_price_down(D("0.55")) == D("0.54")
        assert next_price_up(D("1")) is None
        assert next_price_down(D("0")) is None

    def test_valid_range(self):
        assert min_valid_price() == D("0.01")
        assert max_valid_price() == D("0.99")
        assert min_valid_price(TAPERED) == D("0.001")
        assert max_valid_price(TAPERED) == D("0.999")
        assert min_valid_price(DECI_CENTI) == D("0.0001")
        assert clamp_price(D("0.9995"), TAPERED) == D("0.999")

    def test_validity(self):
        assert is_valid_price(D("0.055"), TAPERED)
        assert not is_valid_price(D("0.555"), TAPERED)
        assert is_valid_price(D("0.55"), TAPERED)
        assert not is_valid_price(D("0"), TAPERED)
        assert not is_valid_price(D("1"), TAPERED)
        assert is_on_grid(D("1"), TAPERED)
        assert not is_valid_price(D("0.555"))
        # whole cents are valid in every structure
        for rs in (TAPERED, EDGE_HALF, DECI_CENTI):
            assert all(is_valid_price(D(c) / 100, rs) for c in range(1, 100))

    def test_snap_always_lands_on_grid(self):
        for rs in (TAPERED, EDGE_HALF, DECI_CENTI, DEFAULT_PRICE_RANGES):
            for i in range(0, 1001, 7):
                x = D(i) / 1000
                for mode in ("nearest", "down", "up"):
                    assert is_valid_price(snap_price(x, rs, mode), rs), (rs, x, mode)
