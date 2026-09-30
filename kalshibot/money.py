"""Decimal helpers for prices, cash, fees and P&L (ARCHITECTURE.md §2).

Ledger math is ``decimal.Decimal`` only. Floats come in from strategy/model code
and are converted with :func:`D` (via ``repr``, so ``0.1`` becomes ``Decimal("0.1")``)
or snapped to a market's tick grid with :func:`price`.

Tick grids
----------
Kalshi describes each market's valid prices with ``price_ranges``: a list of
``{start, end, step}`` bands (docs.kalshi.com/getting_started/fixed_point_migration).
A price is valid when it lies inside a band and sits on that band's grid
(``(p - start) % step == 0``). Whole cents are valid in every structure and order
prices must be strictly inside (0, 1). Example, ``tapered_deci_cent``::

    0.00-0.10 step 0.001 | 0.10-0.90 step 0.01 | 0.90-1.00 step 0.001

The grid helpers here work on a sequence of :class:`PriceRange` and are exact
(pure Decimal arithmetic), including at band boundaries.
"""

from __future__ import annotations

import math
import numbers
from collections.abc import Iterable, Sequence
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Literal, NamedTuple

__all__ = [
    "CENT",
    "CENTI_CENT",
    "DEFAULT_PRICE_RANGES",
    "ONE",
    "ZERO",
    "D",
    "Number",
    "PriceRange",
    "RoundingMode",
    "ceil_cent",
    "ceil_to",
    "clamp_price",
    "f4",
    "floor_cent",
    "floor_to",
    "is_on_grid",
    "is_valid_price",
    "max_valid_price",
    "min_valid_price",
    "next_price_down",
    "next_price_up",
    "price",
    "q4",
    "round_to",
    "snap_price",
    "tick_at",
]

Number = Decimal | int | float | str
RoundingMode = Literal["nearest", "down", "up"]

ZERO = Decimal(0)
ONE = Decimal(1)
CENT = Decimal("0.01")
CENTI_CENT = Decimal("0.0001")
_Q4 = Decimal("0.0001")


class PriceRange(NamedTuple):
    """One band of a market's price grid: valid prices are ``start + k*step`` in [start, end]."""

    start: Decimal
    end: Decimal
    step: Decimal


DEFAULT_PRICE_RANGES: tuple[PriceRange, ...] = (PriceRange(ZERO, ONE, CENT),)


# --------------------------------------------------------------------------- conversion


def _is_boolish(x: object) -> bool:
    # ``bool`` and numpy's ``bool_`` (named ``bool`` in numpy 2) are not numbers here
    return isinstance(x, bool) or type(x).__name__ in ("bool_", "bool")


def D(x: Number | Decimal) -> Decimal:
    """Convert to ``Decimal`` without float artefacts.

    Floats go through their shortest round-trip form, so ``D(0.1) == Decimal("0.1")``.
    Strings are stripped. ``bool`` and ``None`` are rejected. Other real numbers are
    accepted too, notably numpy scalars (``np.float64``/``np.float32`` via their own
    shortest form, ``np.int64`` exactly) since model code produces them.
    """
    if isinstance(x, Decimal):
        return x
    if x is None or _is_boolish(x):
        raise TypeError(f"cannot convert {x!r} to Decimal")
    if isinstance(x, int):
        return Decimal(int(x))
    if isinstance(x, float):
        v = float(x)  # float subclasses (np.float64) have a non-numeric repr
        if not math.isfinite(v):
            raise ValueError(f"cannot convert non-finite float {x!r} to Decimal")
        return Decimal(repr(v))
    if isinstance(x, str):
        return Decimal(x.strip())
    if isinstance(x, numbers.Integral):
        return Decimal(int(x))
    if isinstance(x, numbers.Real):
        v = float(x)
        if not math.isfinite(v):
            raise ValueError(f"cannot convert non-finite number {x!r} to Decimal")
        try:  # numpy scalars print their own shortest form (np.float32(0.1) -> "0.1")
            d = Decimal(str(x).strip())
            if d.is_finite():
                return d
        except ArithmeticError:
            pass
        return Decimal(repr(v))
    raise TypeError(f"cannot convert {type(x).__name__} to Decimal")


def f4(x: Number | Decimal | None) -> float | None:
    """JSON helper: Decimal/number -> float rounded to 4 dp (``None`` passes through)."""
    if x is None:
        return None
    return float(q4(D(x)))


# --------------------------------------------------------------------------- rounding


def _to_step(x: Decimal, step: Decimal, rounding: str) -> Decimal:
    if step <= 0:
        raise ValueError(f"step must be positive, got {step}")
    return (x / step).to_integral_value(rounding=rounding) * step


def ceil_to(x: Number, step: Number) -> Decimal:
    """Round ``x`` up (toward +inf) to a multiple of ``step``."""
    return _to_step(D(x), D(step), ROUND_CEILING)


def floor_to(x: Number, step: Number) -> Decimal:
    """Round ``x`` down (toward -inf) to a multiple of ``step``."""
    return _to_step(D(x), D(step), ROUND_FLOOR)


def round_to(x: Number, step: Number, mode: RoundingMode = "nearest") -> Decimal:
    """Round ``x`` to a multiple of ``step``; ``nearest`` rounds halves up."""
    if mode == "nearest":
        return _to_step(D(x), D(step), ROUND_HALF_UP)
    if mode == "down":
        return floor_to(x, step)
    if mode == "up":
        return ceil_to(x, step)
    raise ValueError(f"unknown rounding mode {mode!r}")


def ceil_cent(x: Number) -> Decimal:
    """Round up (toward +inf) to the cent: ``ceil_cent(0.0101) == 0.02``, ``ceil_cent(-0.015) == -0.01``."""
    return ceil_to(x, CENT)


def floor_cent(x: Number) -> Decimal:
    """Round down (toward -inf) to the cent: ``floor_cent(-0.055) == -0.06``."""
    return floor_to(x, CENT)


def q4(x: Number) -> Decimal:
    """Quantize to 0.0001 (half up) — the API's price precision."""
    return D(x).quantize(_Q4, rounding=ROUND_HALF_UP)


def _norm(x: Decimal) -> Decimal:
    """Express a grid price with 4 dp (API style, e.g. ``0.9000``) when that is exact."""
    q = x.quantize(_Q4)
    return q if q == x else x


# --------------------------------------------------------------------------- tick grids


def _ranges(ranges: Iterable[PriceRange] | None) -> Sequence[PriceRange]:
    rs = tuple(ranges) if ranges is not None else ()
    return rs if rs else DEFAULT_PRICE_RANGES


def _grid_floor(x: Decimal, ranges: Sequence[PriceRange]) -> Decimal | None:
    """Largest grid price <= x (grid includes band endpoints), or None."""
    best: Decimal | None = None
    for r in ranges:
        if x < r.start:
            continue
        top = min(x, r.end)
        cand = r.start + ((top - r.start) / r.step).to_integral_value(ROUND_FLOOR) * r.step
        if best is None or cand > best:
            best = cand
    return best


def _grid_ceil(x: Decimal, ranges: Sequence[PriceRange]) -> Decimal | None:
    """Smallest grid price >= x, or None."""
    best: Decimal | None = None
    for r in ranges:
        if x > r.end:
            continue
        if x <= r.start:
            cand = r.start
        else:
            cand = r.start + ((x - r.start) / r.step).to_integral_value(ROUND_CEILING) * r.step
            if cand > r.end:
                continue
        if best is None or cand < best:
            best = cand
    return best


def next_price_up(p: Number, ranges: Iterable[PriceRange] | None = None) -> Decimal | None:
    """Smallest grid price strictly greater than ``p`` (may be 1.0), or None."""
    x = D(p)
    best: Decimal | None = None
    for r in _ranges(ranges):
        if x >= r.end:
            continue
        if x < r.start:
            cand = r.start
        else:
            cand = r.start + (((x - r.start) / r.step).to_integral_value(ROUND_FLOOR) + 1) * r.step
            if cand > r.end:
                continue
        if best is None or cand < best:
            best = cand
    return None if best is None else _norm(best)


def next_price_down(p: Number, ranges: Iterable[PriceRange] | None = None) -> Decimal | None:
    """Largest grid price strictly less than ``p`` (may be 0.0), or None."""
    x = D(p)
    best: Decimal | None = None
    for r in _ranges(ranges):
        if x <= r.start:
            continue
        if x > r.end:
            cand = _grid_floor(r.end, (r,))
        else:
            cand = r.start + (((x - r.start) / r.step).to_integral_value(ROUND_CEILING) - 1) * r.step
        if cand is not None and (best is None or cand > best):
            best = cand
    return None if best is None else _norm(best)


def min_valid_price(ranges: Iterable[PriceRange] | None = None) -> Decimal:
    """Lowest valid order price (the first grid point above 0)."""
    p = next_price_up(ZERO, ranges)
    if p is None or p >= ONE:
        raise ValueError("price grid has no valid price inside (0, 1)")
    return p


def max_valid_price(ranges: Iterable[PriceRange] | None = None) -> Decimal:
    """Highest valid order price (the last grid point below 1)."""
    p = next_price_down(ONE, ranges)
    if p is None or p <= ZERO:
        raise ValueError("price grid has no valid price inside (0, 1)")
    return p


def clamp_price(p: Number, ranges: Iterable[PriceRange] | None = None) -> Decimal:
    """Clamp ``p`` into [min_valid_price, max_valid_price]."""
    rs = _ranges(ranges)
    return _norm(min(max(D(p), min_valid_price(rs)), max_valid_price(rs)))


def tick_at(p: Number, ranges: Iterable[PriceRange] | None = None) -> Decimal:
    """Tick size of the band containing ``p``.

    Bands are treated as half-open ``[start, end)`` except the last, so at a
    boundary the *upper* band's step is returned (``tick_at(0.10)`` on
    ``tapered_deci_cent`` is 0.01). Use :func:`next_price_up`/:func:`next_price_down`
    when direction matters.
    """
    x = D(p)
    rs = sorted(_ranges(ranges), key=lambda r: r.start)
    for i, r in enumerate(rs):
        last = i == len(rs) - 1
        if r.start <= x < r.end or (last and x == r.end):
            return r.step
    return rs[0].step if x < rs[0].start else rs[-1].step


def is_on_grid(p: Number, ranges: Iterable[PriceRange] | None = None) -> bool:
    """True when ``p`` lies on the grid of a band that contains it (endpoints included)."""
    x = D(p)
    return any(r.start <= x <= r.end and (x - r.start) % r.step == 0 for r in _ranges(ranges))


def is_valid_price(p: Number, ranges: Iterable[PriceRange] | None = None) -> bool:
    """True when ``p`` is an acceptable order price: on the grid and strictly inside (0, 1)."""
    x = D(p)
    return ZERO < x < ONE and is_on_grid(x, ranges)


def snap_price(
    x: Number,
    ranges: Iterable[PriceRange] | None = None,
    mode: RoundingMode = "nearest",
    *,
    clamp: bool = True,
) -> Decimal:
    """Snap ``x`` to the market's price grid.

    ``down``/``up`` give the nearest grid price <= / >= x; ``nearest`` picks the
    closer one (ties round up). With ``clamp`` (default) the result is forced into
    the valid order range [min_valid_price, max_valid_price].
    """
    if mode not in ("nearest", "down", "up"):
        raise ValueError(f"unknown rounding mode {mode!r}")
    rs = _ranges(ranges)
    v = D(x)
    lo = _grid_floor(v, rs)
    hi = _grid_ceil(v, rs)
    if mode == "down":
        out = lo if lo is not None else hi
    elif mode == "up":
        out = hi if hi is not None else lo
    elif lo is None:
        out = hi
    elif hi is None:
        out = lo
    else:
        out = lo if (v - lo) < (hi - v) else hi
    assert out is not None  # at least one band exists
    return clamp_price(out, rs) if clamp else _norm(out)


def price(
    x: Number,
    tick: Decimal | Iterable[PriceRange] = CENT,
    mode: RoundingMode = "nearest",
    *,
    clamp: bool = False,
) -> Decimal:
    """Round a model price to the tick grid (ARCHITECTURE.md §2).

    ``tick`` is either a uniform tick size (default one cent) or a market's
    ``price_ranges`` (tick-aware, e.g. ``price(p, market.price_ranges)``).
    ``clamp=True`` additionally forces the result strictly inside (0, 1).
    """
    if isinstance(tick, Decimal | int | float | str) and not isinstance(tick, bool):
        step = D(tick)
        out = round_to(x, step, mode)
        if clamp:
            out = min(max(out, step), ONE - step)
        return _norm(out)
    return snap_price(x, tick, mode, clamp=clamp)
