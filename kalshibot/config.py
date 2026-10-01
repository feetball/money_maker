"""Settings (ARCHITECTURE.md §14): ``config.yaml`` if present, else defaults, plus env overrides.

Resolution order (later wins):

1. built-in defaults (the values documented in ``config.example.yaml``)
2. the YAML file: explicit ``path`` argument, else ``$KALSHIBOT_CONFIG``, else
   ``./config.yaml`` if it exists
3. environment variables ``KALSHIBOT_<SECTION>__<KEY>`` (double underscore separates
   levels; values are parsed as YAML scalars), e.g.::

       KALSHIBOT_KALSHI__MAX_RPS=2
       KALSHIBOT_ENGINE__AUTOSTART=false
       KALSHIBOT_STRATEGIES__MY_STRAT__ENABLED=true

Money-like values (balances, limits, prices, fee precision) are ``Decimal``; YAML
floats are converted via ``money.D`` so ``0.1`` stays exactly ``Decimal("0.1")``.
Unknown keys are kept (``extra="allow"``) and logged, so other modules can add sections.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, ValidationError, field_validator

from kalshibot.money import D

__all__ = [
    "CONFIG_ENV",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_MIN_TRADES_BY_STRATEGY",
    "ENV_PREFIX",
    "EXAMPLE_CONFIG_PATH",
    "AccountSettings",
    "AnalyticsSettings",
    "CoinbaseSettings",
    "EngineSettings",
    "KalshiSettings",
    "Money",
    "NonNegMoney",
    "PaperSettings",
    "RiskSettings",
    "ServerSettings",
    "Settings",
    "StorageSettings",
    "StrategySettings",
    "apply_env_overrides",
    "ensure_config_file",
    "load_settings",
    "resolve_storage_path",
    "save_settings",
]

log = logging.getLogger(__name__)

ENV_PREFIX = "KALSHIBOT_"
CONFIG_ENV = "KALSHIBOT_CONFIG"
DEFAULT_CONFIG_PATH = Path("config.yaml")
EXAMPLE_CONFIG_PATH = Path("config.example.yaml")


def _to_decimal(v: Any) -> Any:
    if isinstance(v, Decimal | int | float | str) and not isinstance(v, bool):
        try:
            d = D(v)
        except (ArithmeticError, TypeError, ValueError):  # decimal.InvalidOperation is an ArithmeticError
            raise ValueError(f"not a number: {v!r}") from None
        if not d.is_finite():
            raise ValueError(f"not a finite number: {v!r}")
        return d
    return v


#: Decimal parsed exactly from YAML/env numbers; serialized to JSON as a float.
#: Malformed values raise ``ValueError`` (so pydantic reports a ``ValidationError``).
Money = Annotated[
    Decimal,
    BeforeValidator(_to_decimal),
    PlainSerializer(lambda d: float(d), return_type=float, when_used="json"),
]
#: A non-negative ``Money`` (balances and dollar limits).
NonNegMoney = Annotated[Money, Field(ge=0)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="allow", validate_assignment=True)


# The Coinbase venue's settings (docs/COINBASE_CONTRACT.md §15). A Coinbase import failure must
# never stop the Kalshi venue: fall back to a stand-in that keeps the YAML section (as extra
# keys) and reports the venue as disabled with the reason.
try:
    from kalshibot.coinbase.config import CoinbaseSettings
except Exception as _cb_import_error:  # pragma: no cover - exercised in a subprocess test
    log.exception("config: Coinbase settings unavailable; Coinbase venue disabled (Kalshi unaffected)")
    _CB_IMPORT_ERROR = f"coinbase settings unavailable: {type(_cb_import_error).__name__}: {_cb_import_error}"

    class CoinbaseSettings(_Section):  # type: ignore[no-redef]
        enabled: bool = False
        load_error: str | None = Field(default=_CB_IMPORT_ERROR, exclude=True)

        def set_resolved_storage_path(self, resolved: str) -> None:
            pass


class KalshiSettings(_Section):
    base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    max_rps: float = Field(3.0, gt=0)  # team budget: keep live usage <= 3 req/s
    timeout: float = Field(15.0, gt=0)


class AccountSettings(_Section):
    starting_balance: NonNegMoney = D(1000)
    #: % of each closed/settled trade's profit moved out of ``cash`` into ``reserved_profit`` (never
    #: spent on new orders, not part of sizing/risk equity); 100 = keep all profit separate, 0 = old
    #: behaviour (profit stays in the tradeable pool). Losses are never swept.
    profit_sweep_pct: float = Field(100, ge=0, le=100)


class EngineSettings(_Section):
    autostart: bool = True
    universe_refresh_s: float = Field(120, gt=0)
    tick_s: float = Field(30, gt=0)
    order_poll_s: float = Field(15, gt=0)
    settlement_poll_s: float = Field(60, gt=0)
    snapshot_s: float = Field(60, gt=0)
    #: Markets-page baseline: active markets closing within this many days are always in the
    #: universe (0 = only what enabled strategies ask for).
    scanner_days_to_close: float = Field(0.5, ge=0)
    #: Page cap for one /markets window scan (1000 markets per page).
    universe_max_pages: int = Field(150, ge=1)
    #: Full re-scan of the close-time window at least this often (seconds).
    universe_window_rescan_s: float = Field(900, gt=0)


class PaperSettings(_Section):
    consumed_liquidity_ttl_s: float = Field(300, ge=0)
    default_gtc_expiry_s: float = Field(3600, gt=0)
    #: Simulated order latency for engine orders: the order reaches the (paper) exchange this
    #: many seconds after the strategy decided, and walks only a book received at or after that
    #: moment (never the book the decision was made on).
    taker_latency_s: float = Field(0.25, ge=0, le=10)
    #: Trade-tape reads (one request each) per resting-order pass; 0 = no cap.
    max_trade_polls_per_pass: int = Field(8, ge=0)
    #: Balance precision for fee rounding (fees.round_fill): 0.01 = conservative
    #: (non-direct member, matches published tables), 0.0001 = direct member.
    fee_precision: Annotated[Money, Field(gt=0)] = D("0.01")


class RiskSettings(_Section):
    max_position_cost_per_market: NonNegMoney = D(50)
    max_exposure_per_event: NonNegMoney = D(100)
    max_total_exposure_pct: float = Field(60, ge=0, le=100)
    #: fallback for strategies without their own ``max_allocation_pct`` (StrategySettings)
    max_strategy_allocation_pct: float = Field(50, ge=0, le=100)
    min_cash_reserve: NonNegMoney = D(50)
    #: per strategy: one strategy's orders can never use up another's budget
    max_orders_per_minute: int = Field(30, ge=0)
    daily_loss_limit: NonNegMoney = D(150)  # account-wide hard stop; 0 disables the daily-loss kill switch
    #: release a kill switch tripped by ``daily_loss_limit`` at the next UTC day (manual trips stay on)
    kill_switch_auto_release: bool = True
    min_seconds_to_close: float = Field(300, ge=0)
    max_spread: Annotated[Money, Field(ge=0, le=1)] = D("0.10")
    kelly_fraction: float = Field(0.25, ge=0, le=1)


class StrategySettings(_Section):
    #: None (unset) = the strategy class's ``enabled_by_default``; the dashboard toggle beats both
    enabled: bool | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    #: this strategy's cap on its exposure, % of equity (None = the strategy class's
    #: ``risk_defaults``, else ``risk.max_strategy_allocation_pct``)
    max_allocation_pct: float | None = Field(None, ge=0, le=100)
    #: pause only this strategy's entries until the next UTC day once its P&L today reaches
    #: -limit (None = the class's ``risk_defaults``; 0 = off)
    daily_loss_limit: NonNegMoney | None = None


#: Settled trades a strategy needs before its go-live verdict can be "ready" (rare-loss strategies
#: need far more evidence than the 200-trade default: a 1c edge takes ~1,200-3,600 bets).
DEFAULT_MIN_TRADES_BY_STRATEGY: dict[str, int] = {
    "btc15m_favorite": 300, "ladder_favorite": 1500, "maker_favorite": 1000, "no_basket_arb": 200,
}


class AnalyticsSettings(_Section):
    #: Go-live readiness needs at least this many settled trades ...
    min_settled_trades: int = Field(200, ge=1)
    #: ... or this many for the strategies listed here (per-strategy verdicts)
    min_settled_trades_by_strategy: dict[str, Annotated[int, Field(ge=1)]] = Field(
        default_factory=lambda: dict(DEFAULT_MIN_TRADES_BY_STRATEGY))
    #: ... and a max drawdown (percentage points of peak equity) within this limit.
    max_drawdown_pct: float = Field(20, gt=0, le=100)


class ServerSettings(_Section):
    host: str = "127.0.0.1"
    port: int = Field(8765, gt=0, lt=65536)


class StorageSettings(_Section):
    path: str = "data/kalshibot.sqlite3"


class Settings(_Section):
    kalshi: KalshiSettings = Field(default_factory=KalshiSettings)
    account: AccountSettings = Field(default_factory=AccountSettings)
    engine: EngineSettings = Field(default_factory=EngineSettings)
    paper: PaperSettings = Field(default_factory=PaperSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    strategies: dict[str, StrategySettings] = Field(default_factory=dict)
    analytics: AnalyticsSettings = Field(default_factory=AnalyticsSettings)
    #: External data feeds, e.g. ``{crypto: {symbols: [BTC, ETH], ttl_s: 5, sources: [coinbase, kraken]}}``
    #: (free-form; read by :func:`kalshibot.feeds.build_feeds`).
    feeds: dict[str, Any] = Field(default_factory=dict)
    server: ServerSettings = Field(default_factory=ServerSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    #: The separate Coinbase spot PAPER venue (docs/COINBASE_CONTRACT.md §15). Optional in
    #: the YAML; an invalid section disables only Coinbase (see ``_coinbase_isolated``).
    coinbase: CoinbaseSettings = Field(default_factory=CoinbaseSettings)

    #: File the settings were loaded from (None = defaults only). Not part of the YAML.
    config_path: Path | None = Field(default=None, exclude=True)

    @field_validator("coinbase", mode="wrap")
    @classmethod
    def _coinbase_isolated(cls, v: Any, handler: Any) -> CoinbaseSettings:
        """A bad ``coinbase:`` section must never stop the Kalshi venue: fall back to a
        disabled Coinbase venue whose ``load_error`` says why."""
        try:
            return handler(v)
        except ValidationError as e:
            reason = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'coinbase'}: {err['msg']}" for err in e.errors())
            log.error("config: invalid coinbase section; Coinbase venue disabled (Kalshi unaffected): %s", reason)
            return CoinbaseSettings(enabled=False, load_error=f"invalid coinbase config: {reason}")

    def strategy(self, name: str) -> StrategySettings:
        """Settings for strategy ``name`` (defaults if not configured)."""
        return self.strategies.get(name) or StrategySettings()


# --------------------------------------------------------------------------- loading


def _parse_env_value(raw: str) -> Any:
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def apply_env_overrides(data: dict[str, Any], env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Merge ``KALSHIBOT_<SECTION>__<KEY>=value`` variables into ``data`` (in place; returned)."""
    env = os.environ if env is None else env
    for key in sorted(env):
        if not key.startswith(ENV_PREFIX) or key == CONFIG_ENV:
            continue
        parts = [p.lower() for p in key[len(ENV_PREFIX) :].split("__") if p]
        if len(parts) < 2:
            continue  # only nested keys are settings
        node = data
        for p in parts[:-1]:
            nxt = node.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                node[p] = nxt
            node = nxt
        node[parts[-1]] = _parse_env_value(env[key])
    return data


def _warn_unknown(model: BaseModel, prefix: str = "") -> None:
    for k in model.model_extra or {}:
        log.warning("config: unknown key %s%s (kept, but unused by core settings)", prefix, k)
    for name in type(model).model_fields:
        v = getattr(model, name)
        if isinstance(v, BaseModel):
            _warn_unknown(v, f"{prefix}{name}.")


def load_settings(
    path: str | os.PathLike[str] | None = None, *, env: Mapping[str, str] | None = None
) -> Settings:
    """Load settings: YAML (if any) + ``KALSHIBOT_*`` env overrides over defaults."""
    env = os.environ if env is None else env
    cfg_path: Path | None
    if path is not None:
        cfg_path = Path(path)
        if not cfg_path.exists():
            raise FileNotFoundError(f"config file not found: {cfg_path}")
    elif env.get(CONFIG_ENV):
        cfg_path = Path(env[CONFIG_ENV])
        if not cfg_path.exists():
            raise FileNotFoundError(f"{CONFIG_ENV} points to a missing file: {cfg_path}")
    else:
        cfg_path = DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.exists() else None

    data: dict[str, Any] = {}
    if cfg_path is not None:
        loaded = yaml.safe_load(cfg_path.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{cfg_path}: top level must be a mapping")
        data = loaded
    # a section whose keys are all commented out parses as None: treat it as absent
    for key in [k for k, v in data.items() if v is None]:
        del data[key]
    apply_env_overrides(data, env)
    settings = Settings.model_validate(data)
    settings.config_path = cfg_path
    if cfg_path is not None:
        settings.storage.path = resolve_storage_path(settings.storage.path, cfg_path)
        cb_path = getattr(settings.coinbase, "storage_path", None)
        if isinstance(cb_path, str):
            settings.coinbase.set_resolved_storage_path(resolve_storage_path(cb_path, cfg_path))
    _warn_unknown(settings)
    return settings


def resolve_storage_path(path: str, config_path: str | os.PathLike[str] | None) -> str:
    """A relative ``storage.path`` is relative to the config file's directory (not the cwd),
    so ``kalshibot -c /repo/config.yaml ...`` uses the same database from anywhere.
    ``:memory:`` and absolute paths are returned unchanged."""
    if not path or path == ":memory:" or path.startswith("file:") or config_path is None:
        return path
    p = Path(path).expanduser()
    if p.is_absolute():
        return str(p)
    return str((Path(config_path).expanduser().resolve().parent / p).resolve())


def ensure_config_file(
    path: str | os.PathLike[str] = DEFAULT_CONFIG_PATH,
    example: str | os.PathLike[str] = EXAMPLE_CONFIG_PATH,
) -> Path:
    """Copy ``config.example.yaml`` to ``config.yaml`` on first run (no-op if it exists)."""
    dst = Path(path)
    src = Path(example)
    if not dst.exists() and src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    return dst


def save_settings(settings: Settings, path: str | os.PathLike[str] | None = None) -> Path:
    """Write settings as YAML (comments from the example file are not preserved)."""
    dst = Path(path) if path is not None else (settings.config_path or DEFAULT_CONFIG_PATH)
    data = settings.model_dump(mode="json")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(yaml.safe_dump(data, sort_keys=False))
    return dst
