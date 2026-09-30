"""Coinbase Advanced Trade spot fee schedule + fee math (docs/COINBASE_CONTRACT.md §5).

PAPER TRADING ONLY. Fees are charged in USD (the quote currency) on every fill:

* buy:  cash debit     = notional + fee
* sell: cash proceeds  = notional - fee

``fee = ceil(notional x rate, $0.01)`` by default: rounding UP to the cent is deliberately
conservative (Coinbase computes fees at full precision - the Exchange docs example
``0.2433492642`` is unrounded - so real fees are never higher than ours). Pass
``precision=None`` to :func:`fee_for` for exact, unrounded fees. Maker rate for resting
orders that fill from later trades, taker rate for anything that crosses the book.

Fee schedule (docs/coinbase_api_notes.md §3, sources: Coinbase blog "We're lowering fees for
many active traders on Coinbase Advanced", 2026-09-16, and help.coinbase.com "Coinbase
Advanced fees"; news coverage the same week: investing.com, securities.io, primexbt.com,
cryptodaily.co.uk). Keys are the ``coinbase.fee_tier`` config names::

    key                 tier (30-day volume)                     maker    taker
    intro               Intro, US  (>= $0)  current since 2026-09-16  0.50%    0.90%   <- DEFAULT_TIER
    intro_eu            Intro, EU / UK / CA                       0.25%    0.50%
    intro_intl          Intro, rest of world (AU, SG, BR, IN ...)  0.09%    0.10%
    intro_pre_2026_09   Intro, all regions, before 2026-09-16     0.60%    1.20%
    vip_8               VIP 8 (>= $1B), "as low as"               0.00%    0.02%

``DEFAULT_TIER`` is ``intro``: the lowest-volume retail tier a fresh US account pays today.
Paper volume never moves the user's real tier. The Advanced 1 - VIP 7 rates after the
2026-09-16 change are only visible after sign-in (third-party tables disagree and predate
the change), so they are deliberately not listed: put your own rates in
``coinbase.fee_rates: {maker, taker}``. ``intro_pre_2026_09`` (the 1.20% taker rate the
research sensitivity runs use) is a pessimistic check. Stable pairs (``fx_stablecoin``)
trade at ~0% and are not modelled: the paper venue trades USD pairs of volatile assets.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any

__all__ = [
    "CENT",
    "DEFAULT_TIER",
    "DEFAULT_TIER_NAME",
    "FEE_TIERS",
    "TIER_ALIASES",
    "FeeTier",
    "buy_cost",
    "custom_tier",
    "fee_for",
    "fee_rate",
    "get_tier",
    "normalize_tier_name",
    "notional_for_budget",
    "resolve_tier",
    "round_trip_bps",
    "sell_proceeds",
]

CENT = Decimal("0.01")
_ZERO = Decimal(0)


@dataclass(frozen=True)
class FeeTier:
    """One fee tier. Rates are fractions: ``Decimal("0.006")`` = 0.60%."""

    name: str
    maker_rate: Decimal
    taker_rate: Decimal
    label: str = ""
    #: 30-day trailing volume (USD) at which the tier starts (informational)
    min_volume_usd: Decimal = _ZERO

    def __post_init__(self) -> None:
        for r in (self.maker_rate, self.taker_rate):
            if not isinstance(r, Decimal) or not r.is_finite() or r < 0 or r >= 1:
                raise ValueError(f"fee rate must be a Decimal in [0, 1): {r!r}")

    def rate(self, *, is_taker: bool) -> Decimal:
        return self.taker_rate if is_taker else self.maker_rate

    def as_dict(self) -> dict[str, Any]:
        """JSON shape used by ``GET /api/coinbase/status`` (``fee_tier``)."""
        return {"name": self.name, "label": self.label or self.name,
                "maker_rate": float(self.maker_rate), "taker_rate": float(self.taker_rate)}


def _t(name: str, label: str, min_vol: int, maker: str, taker: str) -> FeeTier:
    return FeeTier(name, Decimal(maker), Decimal(taker), label, Decimal(min_vol))


FEE_TIERS: dict[str, FeeTier] = {t.name: t for t in (
    _t("intro", "Intro (US)", 0, "0.005", "0.009"),
    _t("intro_eu", "Intro (EU/UK/CA)", 0, "0.0025", "0.005"),
    _t("intro_intl", "Intro (rest of world)", 0, "0.0009", "0.001"),
    _t("intro_pre_2026_09", "Intro (before 2026-09-16)", 0, "0.006", "0.012"),
    _t("vip_8", "VIP 8", 1_000_000_000, "0", "0.0002"),
)}

#: other accepted spellings of tier keys (after :func:`normalize_tier_name`)
TIER_ALIASES: dict[str, str] = {"intro_us": "intro", "us": "intro", "vip8": "vip_8"}

DEFAULT_TIER_NAME = "intro"
DEFAULT_TIER: FeeTier = FEE_TIERS[DEFAULT_TIER_NAME]


def normalize_tier_name(name: str) -> str:
    """Loose spelling -> tier key: ``"Intro EU"`` -> ``"intro_eu"``, ``"VIP 8"``/``"vip8"`` -> ``"vip_8"``,
    ``"Intro (US)"``-style labels are not accepted (use the key)."""
    s = re.sub(r"[\s\-]+", "_", str(name).strip().lower())
    s = re.sub(r"^([a-z]+)(\d)", r"\1_\2", s)
    return TIER_ALIASES.get(s, s)


def get_tier(name: str | FeeTier | None) -> FeeTier:
    """Tier by (loosely spelled) name; ``None`` -> ``DEFAULT_TIER``. Unknown -> ``KeyError``."""
    if name is None:
        return DEFAULT_TIER
    if isinstance(name, FeeTier):
        return name
    key = normalize_tier_name(name)
    try:
        return FEE_TIERS[key]
    except KeyError:
        raise KeyError(f"unknown Coinbase fee tier {name!r}; known: {', '.join(FEE_TIERS)}") from None


def custom_tier(maker_rate: Any, taker_rate: Any, name: str = "custom") -> FeeTier:
    """Explicit rates (fractions, e.g. ``0.006``) as a tier."""
    return FeeTier(name, Decimal(str(maker_rate)), Decimal(str(taker_rate)), "Custom rates")


def resolve_tier(tier: str | FeeTier | None = None,
                 rates: Mapping[str, Any] | Any | None = None) -> FeeTier:
    """Explicit ``rates`` (``{maker, taker}`` mapping or an object with those attributes)
    win over a tier name; neither -> ``DEFAULT_TIER``."""
    if rates is not None:
        get = rates.get if isinstance(rates, Mapping) else (lambda k: getattr(rates, k, None))
        maker, taker = get("maker"), get("taker")
        if maker is None or taker is None:
            raise ValueError("fee rates need both 'maker' and 'taker'")
        return custom_tier(maker, taker)
    return get_tier(tier)


def fee_rate(tier: FeeTier, *, is_taker: bool) -> Decimal:
    return tier.rate(is_taker=is_taker)


def fee_for(notional: Decimal, *, is_taker: bool, tier: FeeTier = DEFAULT_TIER,
            precision: Decimal | None = CENT) -> Decimal:
    """USD fee on a fill of ``notional`` USD, rounded UP to ``precision`` (default one cent;
    ``None`` = exact, as Coinbase computes it). ``notional <= 0`` -> 0."""
    n = Decimal(notional)
    if n <= 0:
        return Decimal("0.00") if precision is not None else _ZERO
    fee = n * tier.rate(is_taker=is_taker)
    return fee if precision is None else fee.quantize(precision, rounding=ROUND_CEILING)


def buy_cost(notional: Decimal, *, is_taker: bool, tier: FeeTier = DEFAULT_TIER) -> Decimal:
    """Cash debited for a buy: notional + fee."""
    return Decimal(notional) + fee_for(notional, is_taker=is_taker, tier=tier)


def sell_proceeds(notional: Decimal, *, is_taker: bool, tier: FeeTier = DEFAULT_TIER) -> Decimal:
    """Cash credited for a sell: notional - fee."""
    return Decimal(notional) - fee_for(notional, is_taker=is_taker, tier=tier)


def notional_for_budget(budget: Decimal, *, is_taker: bool, tier: FeeTier = DEFAULT_TIER) -> Decimal:
    """Largest notional with ``notional + fee_for(notional) <= budget`` (a buy by
    ``quote_size`` spends the budget *including* the fee). Not rounded to a grid: the
    caller still rounds the base size down to ``base_increment``, which only lowers it."""
    b = Decimal(budget)
    if b <= 0:
        return _ZERO
    rate = tier.rate(is_taker=is_taker)
    n = b / (1 + rate)
    fee = fee_for(n, is_taker=is_taker, tier=tier)
    if n + fee > b:  # the cent round-up pushed us over: give the rounding back
        n = b - fee  # smaller notional -> fee_for(n) <= fee, so n + fee_for(n) <= b
    return max(_ZERO, n)


def round_trip_bps(tier: FeeTier = DEFAULT_TIER, *, is_taker: bool = True) -> float:
    """Buy + sell fee in basis points of notional (ignores cent rounding): 180 for ``intro`` taker."""
    return float(tier.rate(is_taker=is_taker) * 2 * 10_000)
