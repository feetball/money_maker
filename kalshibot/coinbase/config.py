"""``coinbase:`` settings section (docs/COINBASE_CONTRACT.md §15) - PAPER TRADING ONLY.

Mounted on :class:`kalshibot.config.Settings` as ``settings.coinbase`` with defaults, so a
``config.yaml`` without a ``coinbase:`` section keeps working. Env overrides use the
existing mechanism, e.g.::

    KALSHIBOT_COINBASE__ENABLED=false
    KALSHIBOT_COINBASE__MAX_RPS=2
    KALSHIBOT_COINBASE__STORAGE_PATH=/tmp/cb.sqlite3
    KALSHIBOT_COINBASE__FEE_TIER=intro_2
    KALSHIBOT_COINBASE__RISK__MAX_SPREAD_BPS=30
    KALSHIBOT_COINBASE__STRATEGIES__MY_STRAT__ENABLED=true

Isolation: an invalid ``coinbase:`` section never stops the Kalshi venue.
``Settings`` catches the validation error and substitutes
``CoinbaseSettings(enabled=False, load_error="invalid coinbase config: ...")``, so the
server can report the reason while Kalshi keeps running.

This module deliberately imports nothing from :mod:`kalshibot.config` (which imports it),
so either module can be imported first.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    PrivateAttr,
    field_serializer,
    field_validator,
)

from kalshibot.coinbase.fees import DEFAULT_TIER_NAME, FEE_TIERS, FeeTier, normalize_tier_name, resolve_tier
from kalshibot.money import D

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_STORAGE_PATH",
    "CoinbaseEngineSettings",
    "CoinbaseFeeRates",
    "CoinbasePaperSettings",
    "CoinbaseRiskSettings",
    "CoinbaseSettings",
    "CoinbaseStrategySettings",
]

DEFAULT_BASE_URL = "https://api.exchange.coinbase.com"
DEFAULT_STORAGE_PATH = "data/coinbase.sqlite3"


def _to_decimal(v: Any) -> Any:
    if isinstance(v, Decimal | int | float | str) and not isinstance(v, bool):
        try:
            d = D(v)
        except (ArithmeticError, TypeError, ValueError):
            raise ValueError(f"not a number: {v!r}") from None
        if not d.is_finite():
            raise ValueError(f"not a finite number: {v!r}")
        return d
    return v


#: Exact Decimal from YAML/env numbers; JSON-serialized as a float (same as kalshibot.config.Money).
_Money = Annotated[
    Decimal,
    BeforeValidator(_to_decimal),
    PlainSerializer(lambda d: float(d), return_type=float, when_used="json"),
]
_NonNegMoney = Annotated[_Money, Field(ge=0)]
#: A fee rate as a fraction: 0.006 = 0.60%.
_Rate = Annotated[_Money, Field(ge=0, lt=1)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="allow", validate_assignment=True)


class CoinbaseFeeRates(_Section):
    """Explicit fee rates (fractions); when set they win over ``fee_tier``."""

    maker: _Rate = D("0.005")  # a missing side defaults to the Intro (US) rate
    taker: _Rate = D("0.009")


class CoinbasePaperSettings(_Section):
    #: market orders stop walking the book this far (bps) past the best price
    max_slippage_bps: float = Field(100, ge=0)
    #: consumed displayed depth stays unavailable this long (like the Kalshi broker)
    consumed_liquidity_ttl_s: float = Field(300, ge=0)
    #: resting GTC orders expire after this unless the intent sets ``expires_in_s``
    default_gtc_expiry_s: float = Field(3600, gt=0)
    #: fill a resting order at its limit when the displayed book crosses it with no public print.
    #: Off by default (docs/coinbase_api_notes.md §6.1 rule 4: never fill from book movement
    #: alone - most crossing quotes are post-only and would not have traded with us).
    fill_on_book_cross: bool = False


class CoinbaseEngineSettings(_Section):
    autostart: bool = True
    #: run a strategy this long after its bar closes (lets Coinbase publish and finish the bar;
    #: the newest bucket was seen aggregating for up to ~60 s)
    bar_delay_s: float = Field(60, ge=0)
    #: resting-order maintenance (fills from public trades + expiry)
    maintenance_s: float = Field(15, gt=0)
    #: marks + equity snapshot
    snapshot_s: float = Field(60, gt=0)
    products_refresh_s: float = Field(3600, gt=0)
    #: ``maker_then_taker``: rest post-only this long, then take the remainder
    maker_timeout_s: float = Field(120, ge=0)


class CoinbaseRiskSettings(_Section):
    max_position_pct_per_product: float = Field(50, ge=0, le=100)  # % of Coinbase equity
    max_total_exposure_pct: float = Field(90, ge=0, le=100)
    #: fallback for strategies without their own ``max_allocation_pct``
    max_strategy_allocation_pct: float = Field(50, ge=0, le=100)
    min_cash_reserve: _NonNegMoney = D(20)
    max_orders_per_minute: int = Field(20, ge=0)
    #: account-wide: trips the Coinbase kill switch (blocks buys; risk-reducing sells allowed); 0 disables
    daily_loss_limit: _NonNegMoney = D(100)
    max_spread_bps: float = Field(50, ge=0)
    min_trade_usd: _NonNegMoney = D(10)


class CoinbaseStrategySettings(_Section):
    #: None (unset) = the strategy class's default; a dashboard toggle beats both
    enabled: bool | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    #: this strategy's allocation, % of Coinbase equity (None = ``risk.max_strategy_allocation_pct``)
    max_allocation_pct: float | None = Field(None, ge=0, le=100)


class CoinbaseSettings(_Section):
    enabled: bool = True
    base_url: str = DEFAULT_BASE_URL
    #: client-side rate limit for Coinbase public REST (requests/second)
    max_rps: float = Field(3.0, gt=0)
    timeout: float = Field(10.0, gt=0)
    starting_balance: _NonNegMoney = D(1000)
    #: a key of ``kalshibot.coinbase.fees.FEE_TIERS`` (spelling is normalized: "Intro EU" -> "intro_eu")
    fee_tier: str = DEFAULT_TIER_NAME
    #: explicit ``{maker, taker}`` rates; win over ``fee_tier`` when set
    fee_rates: CoinbaseFeeRates | None = None
    #: separate SQLite file; a relative path is relative to the config file's directory
    #: (resolved by ``load_settings``, like ``storage.path``)
    storage_path: str = DEFAULT_STORAGE_PATH
    paper: CoinbasePaperSettings = Field(default_factory=CoinbasePaperSettings)
    engine: CoinbaseEngineSettings = Field(default_factory=CoinbaseEngineSettings)
    risk: CoinbaseRiskSettings = Field(default_factory=CoinbaseRiskSettings)
    strategies: dict[str, CoinbaseStrategySettings] = Field(default_factory=dict)

    #: Why the configured section was rejected (then ``enabled`` is False). Not part of the YAML.
    load_error: str | None = Field(default=None, exclude=True)

    #: (as configured, as resolved) - so a dump writes the path the way the user wrote it
    _storage_path_pair: tuple[str, str] | None = PrivateAttr(default=None)

    @field_validator("fee_tier", mode="before")
    @classmethod
    def _known_tier(cls, v: Any) -> Any:
        if v is None:
            return DEFAULT_TIER_NAME
        key = normalize_tier_name(str(v))
        if key not in FEE_TIERS:
            raise ValueError(f"unknown fee tier {v!r}; known: {', '.join(FEE_TIERS)}")
        return key

    @field_serializer("storage_path")
    def _dump_storage_path(self, v: str) -> str:
        pair = self._storage_path_pair
        return pair[0] if pair is not None and pair[1] == v else v

    def set_resolved_storage_path(self, resolved: str) -> None:
        """Replace ``storage_path`` with its resolved form (dumps keep the configured form)."""
        configured = self.storage_path
        self.storage_path = resolved
        self._storage_path_pair = (configured, resolved)

    def tier(self) -> FeeTier:
        """The effective fee tier: ``fee_rates`` (name ``"custom"``) if set, else ``fee_tier``."""
        return resolve_tier(self.fee_tier, self.fee_rates)

    def strategy(self, name: str) -> CoinbaseStrategySettings:
        """Settings for Coinbase strategy ``name`` (defaults if not configured)."""
        return self.strategies.get(name) or CoinbaseStrategySettings()
