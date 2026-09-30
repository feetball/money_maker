"""Kalshi trading-fee model and the exchange's fee/balance rounding rule (ARCHITECTURE.md §5).

Sources
-------
* Kalshi Fee Schedule PDF (effective Feb 5, 2026; https://kalshi.com/docs/kalshi-fee-schedule.pdf):
  ``fees = round up(0.07 x C x P x (1-P))``; maker fees ``round up(0.0175 x C x P x (1-P))``;
  the "Specific Trading Fees Table" (INX/NASDAQ100) uses 0.035. No settlement fee.
* OpenAPI ``Series.fee_type`` (docs.kalshi.com/api-reference/market/get-series):
  ``quadratic`` = general table (makers free), ``quadratic_with_maker_fees`` = general table
  plus maker fees (25% of taker), ``quadratic_with_combo_maker_fees`` = maker multiplier 0.5
  instead of 0.25, ``flat`` = specific table (0.035). ``fee_multiplier`` M scales everything
  (seen: 1, 0.5, 0). Event ``fee_type_override``/``fee_multiplier_override`` take precedence.
* Fee rounding (docs.kalshi.com/getting_started/fee_rounding), per fill, given signed
  ``revenue`` (negative for the buyer) and the unrounded ``model_fee``:

  1. ``trade_fee = ceil_6dp(model_fee)``
  2. ``aligned_change = floor_precision(revenue - trade_fee)``
  3. ``rounding_fee = (revenue - trade_fee) - aligned_change``
  4. add ``rounding_fee`` to the **order's** accumulator (shared by all fills of the order,
     taker and maker)
  5. rebate the accumulator in multiples of the precision, capped so the fill's net fee
     is never negative.

  ``net fee = trade fee + rounding fee - rebate``. Precision is $0.01 for non-direct
  (FCM-cleared) members and $0.0001 for direct members. Doc example ($0.01): a buy with
  revenue -0.055 and model fee 0.00363825 -> trade fee 0.003639, balance change -0.06,
  trade + rounding fee = 0.005.
* docs/kalshi_api_notes.md §1 (repo) — consolidated rules and the worked-example table.

Policy
------
The simulator defaults to ``precision = CENT`` per ARCHITECTURE.md §5 (conservative;
reproduces the PDF tables exactly for whole contracts at whole-cent prices). ``0.0001``
(direct kalshi.com members, which docs/kalshi_api_notes.md §1.3 calls the realistic case)
is selected with ``paper.fee_precision``. All rounding lives in :func:`round_fill`;
everything else is the unrounded fee model.

With whole contracts at whole-cent prices the per-order accumulator makes the total fee
charged after n fills equal ``ceil_to(precision, sum(trade_fee_i))`` — the cumulative
per-order rule of ARCHITECTURE.md §5 — as long as every fill carries a non-zero fee.
At sub-penny prices or fractional sizes the principal itself can be finer than the
precision; that excess is charged as part of the rounding fee (it is what the balance
actually loses), so :func:`trading_fee` can be non-zero even when the model fee is 0.
"""

from __future__ import annotations

import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from typing import Any

from kalshibot.money import CENT, CENTI_CENT, ONE, ZERO, D, Number, floor_to

__all__ = [
    "FEE_GRANULARITY",
    "KNOWN_FEE_TYPES",
    "MAKER_SHARE",
    "PRECISION_DIRECT",
    "PRECISION_FCM",
    "TAKER_COEFF",
    "FillFee",
    "OrderFeeAccumulator",
    "ceil_6dp",
    "fee_rate",
    "raw_fee",
    "resolve_fee_params",
    "round_fill",
    "trading_fee",
]

#: Fees are six-decimal dollar amounts.
FEE_GRANULARITY = Decimal("0.000001")
#: Balance precision for non-direct (FCM-cleared) members — the conservative default.
PRECISION_FCM = CENT
#: Balance precision for direct members (since 2026-05-28).
PRECISION_DIRECT = CENTI_CENT

#: Base taker coefficient per fee_type (before the multiplier).
TAKER_COEFF: Mapping[str, Decimal] = {
    "quadratic": Decimal("0.07"),
    "quadratic_with_maker_fees": Decimal("0.07"),
    "quadratic_with_combo_maker_fees": Decimal("0.07"),
    "flat": Decimal("0.035"),
}
#: Maker fee as a share of the taker coefficient.
MAKER_SHARE: Mapping[str, Decimal] = {
    "quadratic": ZERO,
    "quadratic_with_maker_fees": Decimal("0.25"),
    "quadratic_with_combo_maker_fees": Decimal("0.5"),
    "flat": ZERO,
}
KNOWN_FEE_TYPES = frozenset(TAKER_COEFF)

# Unknown fee types (e.g. perps' margin_market_maker_program_fees) are charged conservatively:
# full taker rate and "assume maker fees" (share 0.25), per docs/kalshi_api_notes.md §1.5.
_UNKNOWN_TAKER = Decimal("0.07")
_UNKNOWN_MAKER_SHARE = Decimal("0.25")
_warned_types: set[str] = set()


# --------------------------------------------------------------------------- fee model (unrounded)


def fee_rate(fee_type: str = "quadratic", fee_multiplier: Number = ONE, *, is_taker: bool) -> Decimal:
    """Coefficient k such that ``model_fee = k * C * P * (1 - P)``."""
    if fee_type in TAKER_COEFF:
        taker = TAKER_COEFF[fee_type]
        share = MAKER_SHARE[fee_type]
    else:
        if fee_type not in _warned_types:
            _warned_types.add(fee_type)
            warnings.warn(
                f"unknown Kalshi fee_type {fee_type!r}; charging conservatively "
                f"(taker 0.07, maker share 0.25)",
                RuntimeWarning,
                stacklevel=2,
            )
        taker, share = _UNKNOWN_TAKER, _UNKNOWN_MAKER_SHARE
    mult = D(fee_multiplier)
    if mult < 0:
        raise ValueError(f"fee_multiplier must be >= 0, got {mult}")
    return (taker if is_taker else taker * share) * mult


def raw_fee(
    price: Number,
    count: Number,
    *,
    is_taker: bool,
    fee_type: str = "quadratic",
    fee_multiplier: Number = ONE,
) -> Decimal:
    """Unrounded model fee in dollars: ``k * C * P * (1 - P)`` (P = fill price of the side traded)."""
    p = D(price)
    c = D(count)
    if not ZERO <= p <= ONE:
        raise ValueError(f"price must be in [0, 1], got {p}")
    if c < 0:
        raise ValueError(f"count must be >= 0, got {c}")
    return fee_rate(fee_type, fee_multiplier, is_taker=is_taker) * c * p * (ONE - p)


def ceil_6dp(x: Number) -> Decimal:
    """Round up to $0.000001 (the trade-fee granularity)."""
    return D(x).quantize(FEE_GRANULARITY, rounding=ROUND_CEILING)


# --------------------------------------------------------------------------- rounding policy


@dataclass(frozen=True, slots=True)
class FillFee:
    """Result of charging one fill (all amounts in dollars)."""

    revenue: Decimal  # signed principal: -P*C for a buy, +P*C for a sell
    model_fee: Decimal  # unrounded formula value
    trade_fee: Decimal  # ceil_6dp(model_fee)
    rounding_fee: Decimal  # restores the balance precision (includes principal rounding)
    rebate: Decimal  # refund from the order's accumulator
    net_fee: Decimal  # trade_fee + rounding_fee - rebate (>= 0)
    balance_change: Decimal  # aligned_change + rebate; on the precision grid
    accumulator: Decimal  # order accumulator after this fill


def round_fill(
    revenue: Number,
    model_fee: Number,
    accumulator: Number = ZERO,
    precision: Number = PRECISION_FCM,
) -> FillFee:
    """Apply Kalshi's fee/balance rounding to one fill — the single rounding-policy function.

    ``revenue`` is signed (negative when buying). ``accumulator`` is the order's carried
    rounding overpayment before this fill; the returned :class:`FillFee` carries the new value.
    Implements docs.kalshi.com/getting_started/fee_rounding steps 1-5 exactly.
    """
    rev = D(revenue)
    mf = D(model_fee)
    prec = D(precision)
    acc = D(accumulator)
    if mf < 0:
        raise ValueError("model_fee must be >= 0")
    if prec <= 0:
        raise ValueError("precision must be > 0")
    trade_fee = ceil_6dp(mf)  # 1
    gross_change = rev - trade_fee
    aligned_change = floor_to(gross_change, prec)  # 2
    rounding_fee = gross_change - aligned_change  # 3
    acc = acc + rounding_fee  # 4
    gross_fee = trade_fee + rounding_fee
    rebate = min(floor_to(acc, prec), floor_to(gross_fee, prec))  # 5 (capped: net fee >= 0)
    rebate = max(rebate, ZERO)
    acc = acc - rebate
    return FillFee(
        revenue=rev,
        model_fee=mf,
        trade_fee=trade_fee,
        rounding_fee=rounding_fee,
        rebate=rebate,
        net_fee=gross_fee - rebate,
        balance_change=aligned_change + rebate,
        accumulator=acc,
    )


# --------------------------------------------------------------------------- public API


def trading_fee(
    price: Number,
    count: Number,
    *,
    is_taker: bool,
    fee_type: str = "quadratic",
    fee_multiplier: Number = ONE,
    precision: Number = PRECISION_FCM,
    is_buy: bool = True,
) -> Decimal:
    """Total fee in dollars for one order execution at one price (a single-fill order).

    Equals the net fee Kalshi charges when the whole order fills at ``price`` in one fill:
    ``ceil_cent(k*C*P*(1-P))`` for whole contracts at whole-cent prices (the published
    tables). For multi-fill orders use :class:`OrderFeeAccumulator`.
    """
    c = D(count)
    p = D(price)
    if c == 0:
        return ZERO
    mf = raw_fee(p, c, is_taker=is_taker, fee_type=fee_type, fee_multiplier=fee_multiplier)
    revenue = -(p * c) if is_buy else p * c
    return round_fill(revenue, mf, ZERO, precision).net_fee


@dataclass(slots=True)
class OrderFeeAccumulator:
    """Per-order fee state: charge each fill with the order's shared rounding accumulator.

    Create one per order and call :meth:`on_fill` for every fill (taker or maker)::

        acc = OrderFeeAccumulator(precision=settings.paper.fee_precision)
        f = acc.on_fill(price, count, is_buy=True, is_taker=True, fee_type=ft, fee_multiplier=m)
        cash -= price * count + f.net_fee      # == -f.balance_change
    """

    precision: Decimal = PRECISION_FCM
    accumulator: Decimal = ZERO
    total_fee: Decimal = ZERO  # sum of net fees charged so far
    total_trade_fee: Decimal = ZERO
    total_balance_change: Decimal = ZERO
    fills: list[FillFee] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.precision = D(self.precision)

    def on_fill(
        self,
        price: Number,
        count: Number,
        *,
        is_buy: bool = True,
        is_taker: bool,
        fee_type: str = "quadratic",
        fee_multiplier: Number = ONE,
    ) -> FillFee:
        p = D(price)
        c = D(count)
        mf = raw_fee(p, c, is_taker=is_taker, fee_type=fee_type, fee_multiplier=fee_multiplier)
        revenue = -(p * c) if is_buy else p * c
        res = round_fill(revenue, mf, self.accumulator, self.precision)
        self.accumulator = res.accumulator
        self.total_fee += res.net_fee
        self.total_trade_fee += res.trade_fee
        self.total_balance_change += res.balance_change
        self.fills.append(res)
        return res

    @property
    def state(self) -> dict[str, str]:
        """Serializable state (so a resting order's accumulator survives a restart)."""
        return {"precision": str(self.precision), "accumulator": str(self.accumulator),
                "total_fee": str(self.total_fee)}

    @classmethod
    def from_state(cls, state: Mapping[str, Any]) -> OrderFeeAccumulator:
        return cls(
            precision=D(state.get("precision", PRECISION_FCM)),
            accumulator=D(state.get("accumulator", ZERO)),
            total_fee=D(state.get("total_fee", ZERO)),
        )


def resolve_fee_params(series: Any, event: Any = None) -> tuple[str, Decimal]:
    """Effective ``(fee_type, fee_multiplier)``: event overrides beat the series.

    ``series``/``event`` are :class:`~kalshibot.kalshi.models.Series` /
    :class:`~kalshibot.kalshi.models.Event` (or anything with the same attributes).
    Resolve at fill time — overrides are scheduled (e.g. MLB M=0.5 -> 1.0 at first pitch).
    """
    fee_type = getattr(series, "fee_type", None) or "quadratic"
    mult = getattr(series, "fee_multiplier", None)
    mult = ONE if mult is None else D(mult)
    if event is not None:
        ft_o = getattr(event, "fee_type_override", None)
        m_o = getattr(event, "fee_multiplier_override", None)
        if ft_o:
            fee_type = ft_o
        if m_o is not None:
            mult = D(m_o)
    return fee_type, mult
