"""Coinbase spot strategy interface (docs/COINBASE_CONTRACT.md §8): bar-based target weights.

PAPER TRADING ONLY. A spot strategy is **pure decision logic** that runs once per closed bar
(``bar_granularity_s``: 3600 or 86400). On each bar the engine (or the backtester) builds a
:class:`SpotContext` with the strategy's pre-loaded candles and calls :meth:`SpotStrategy.on_bar`,
which returns the portfolio the strategy wants to hold, as :class:`TargetWeight` items
(fractions of *this strategy's allocation*). It never does I/O and never places orders: the
engine turns the targets into order intents with
:func:`kalshibot.coinbase.rebalance.plan_rebalance` (the backtester uses the same planner),
runs them through the spot risk manager and the paper broker, and records every intent as a
signal with its decision and reason.

Rules for strategy authors
--------------------------
* ``on_bar`` is **synchronous and pure**: read ``ctx.candles(pid, n)`` (closed bars only,
  ``end <= ctx.bar_end``, oldest first), ``ctx.stats(pid)``, ``ctx.portfolio`` and
  ``ctx.params``; return

  - ``None``  -> no change (keep whatever is held; nothing is traded),
  - ``[]``    -> all cash (sell everything this strategy holds),
  - ``[TargetWeight(...), ...]`` -> hold exactly these weights; products the strategy holds
    that are not listed are sold to 0. Weights are fractions of the strategy's allocation
    equity in ``[0, 1]`` and must sum to <= 1 (the remainder is cash). Spot only: no
    shorting, no leverage.
* Attach a human-readable ``reason`` (shown in the UI) and, if the strategy has a model,
  ``expected_edge_bps`` (after fees) and ``score`` to every target.
* Declare the products you trade in :meth:`SpotStrategy.universe` (USD products only);
  ``history_bars`` is the lookback the engine/backtester must pre-load per product.
* Parameters: ``default_params`` + ``param_schema`` (``{name: {type, min, max, help}}``,
  the same schema language and validation as the Kalshi side,
  :func:`kalshibot.strategies.base.coerce_params`). ``self.params`` holds the effective values.
* Costs are high (Coinbase retail taker fees are ~1.2% per trade at the entry tier): prefer
  slow signals and set ``rebalance_band`` so weight drift does not churn the account.

Validation (:func:`normalize_targets`, applied by the planner, engine and backtester alike):
non-finite weights and malformed items are dropped, negative weights are clamped to 0 (no
shorting), weights above 1 to 1, duplicate products keep the last target, products outside
``ctx.products`` are dropped, and if the weights sum to more than 1 they are scaled down
proportionally. Each correction is reported as a problem string (the engine logs them).

Enabled / params resolution mirrors the Kalshi engine (``kalshibot/engine.py``):
``enabled`` = the dashboard toggle stored in the Coinbase database if set, else
``coinbase.strategies.<name>.enabled`` in the config if set, else the class's
``enabled_by_default`` (source "dashboard" / "config" / "default"); ``params`` = the class
``default_params`` <- config ``coinbase.strategies.<name>.params`` <- stored overrides
(invalid merged params fall back to the defaults, with the error reported).
See :func:`resolve_enabled`, :func:`resolve_strategy_params`, :func:`strategy_config` and
:func:`allocation_pct`.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from kalshibot.strategies.base import ParamError, coerce_params

if TYPE_CHECKING:
    from kalshibot.coinbase.models import Candle, Product, Stats
    from kalshibot.coinbase.paper import SpotPortfolioView

__all__ = [
    "EXECUTIONS",
    "GRANULARITIES",
    "ParamError",
    "SpotContext",
    "SpotStrategy",
    "TargetWeight",
    "allocation_pct",
    "coerce_params",
    "normalize_targets",
    "resolve_enabled",
    "resolve_strategy_params",
    "strategy_config",
]

#: supported bar sizes (seconds): hourly and daily (UTC days)
GRANULARITIES: tuple[int, ...] = (3600, 86400)
#: ``taker`` = market/IOC orders; ``maker_then_taker`` = post-only at the touch for
#: ``coinbase.engine.maker_timeout_s``, then taker for the remainder (engine, §10)
EXECUTIONS: tuple[str, ...] = ("taker", "maker_then_taker")

#: slack when checking that weights sum to <= 1 (float noise)
_SUM_TOL = 1e-9


def _finite_or_none(x: Any) -> float | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


# --------------------------------------------------------------------------- TargetWeight


@dataclass
class TargetWeight:
    """One product the strategy wants to hold, as a fraction of its allocation equity.

    ``weight`` in ``[0, 1]``; the weights of one ``on_bar`` result sum to <= 1 (rest = cash).
    Values are normalized in ``__post_init__`` (``product_id`` upper-cased string, ``weight``
    a float - NaN if unparseable, reported by :meth:`problems`); nothing is raised here.
    """

    product_id: str
    weight: float
    reason: str = ""
    expected_edge_bps: float | None = None
    score: float | None = None

    def __post_init__(self) -> None:
        self.product_id = str(self.product_id or "").strip().upper()
        w = _finite_or_none(self.weight)
        self.weight = w if w is not None else math.nan
        self.reason = str(self.reason or "")
        self.expected_edge_bps = _finite_or_none(self.expected_edge_bps)
        self.score = _finite_or_none(self.score)

    def problems(self) -> list[str]:
        """Why this target is malformed (empty list = well-formed)."""
        out: list[str] = []
        if not self.product_id:
            out.append("missing product_id")
        if not math.isfinite(self.weight):
            out.append(f"{self.product_id or '?'}: weight must be a finite number")
        elif self.weight < 0:
            out.append(f"{self.product_id}: weight {self.weight:g} < 0 (spot: no shorting)")
        elif self.weight > 1:
            out.append(f"{self.product_id}: weight {self.weight:g} > 1 (no leverage)")
        return out

    def to_json(self) -> dict[str, Any]:
        return {"product_id": self.product_id, "weight": self.weight if math.isfinite(self.weight) else None,
                "reason": self.reason, "expected_edge_bps": self.expected_edge_bps, "score": self.score}


def normalize_targets(
    result: Any,
    products: Mapping[str, Any] | Iterable[str] | None = None,
) -> tuple[list[TargetWeight] | None, list[str]]:
    """Validate an ``on_bar`` result -> ``(targets, problems)``.

    ``None`` -> ``(None, [])`` (no change). ``[]`` -> ``([], [])`` (all cash). Also accepts a
    single :class:`TargetWeight` and, for convenience, a mapping ``{product_id: weight}`` or
    ``{product_id: TargetWeight}``. ``products`` (ids or a mapping keyed by id): targets for
    other products are dropped. The returned list is sorted by product id, has unique
    products, weights in ``[0, 1]`` and a sum <= 1 (see the module doc for the corrections).
    """
    if result is None:
        return None, []
    problems: list[str] = []
    items: list[Any]
    if isinstance(result, TargetWeight):
        items = [result]
    elif isinstance(result, Mapping):
        items = [v if isinstance(v, TargetWeight) else TargetWeight(k, v) for k, v in result.items()]
    elif isinstance(result, Iterable) and not isinstance(result, str | bytes):
        items = list(result)
    else:
        return [], [f"on_bar returned {type(result).__name__}; expected a list of TargetWeight or None"]
    allowed = None if products is None else {str(p).upper() for p in products}
    by_pid: dict[str, TargetWeight] = {}
    for it in items:
        if not isinstance(it, TargetWeight):
            problems.append(f"ignored {type(it).__name__} item (expected TargetWeight)")
            continue
        tw = TargetWeight(it.product_id, it.weight, it.reason, it.expected_edge_bps, it.score)  # a copy
        bad = tw.problems()
        if not tw.product_id or not math.isfinite(tw.weight):
            problems.extend(bad)
            continue
        if bad:  # out of range: clamp
            problems.extend(bad)
            tw.weight = min(1.0, max(0.0, tw.weight))
        if allowed is not None and tw.product_id not in allowed:
            problems.append(f"{tw.product_id}: not an available product; target dropped")
            continue
        if tw.product_id in by_pid:
            problems.append(f"{tw.product_id}: duplicate target; the last one wins")
        by_pid[tw.product_id] = tw
    out = [by_pid[k] for k in sorted(by_pid)]
    total = math.fsum(t.weight for t in out)
    if total > 1 + _SUM_TOL:
        problems.append(f"weights sum to {total:.6g} > 1; scaled down proportionally")
        for t in out:
            t.weight = t.weight / total
    return out, problems


# --------------------------------------------------------------------------- context


@runtime_checkable
class SpotContext(Protocol):
    """What a spot strategy sees on each closed bar (engine and backtester implement it).

    * ``now``: wall-clock time of the decision (live: ``bar_end + coinbase.engine.bar_delay_s``).
    * ``bar_end``: close time of the bar just completed (= the next bar's open).
    * ``products``: tradable USD products (backtest: products with data at ``bar_end``).
    * ``candles(pid, n)``: the last ``n`` **closed** bars (``end <= bar_end``) of the strategy's
      granularity, oldest first; fewer if less history exists; ``[]`` for unknown products.
    * ``stats(pid)``: 24 h stats (backtest: derived from closed bars) or ``None``.
    * ``portfolio``: this strategy's holdings, allocation equity and cash.
    """

    # read-only members (plain attributes or properties both satisfy the protocol)
    @property
    def now(self) -> datetime: ...

    @property
    def bar_end(self) -> datetime: ...

    @property
    def products(self) -> Mapping[str, Product]: ...

    @property
    def params(self) -> Mapping[str, Any]: ...

    @property
    def portfolio(self) -> SpotPortfolioView: ...

    def candles(self, product_id: str, n: int) -> list[Candle]: ...

    def stats(self, product_id: str) -> Stats | None: ...

    def log(self, msg: str, **data: Any) -> None: ...


# --------------------------------------------------------------------------- SpotStrategy


class SpotStrategy(ABC):
    """Base class. Subclasses set the class attributes and implement :meth:`on_bar`.

    Class attributes may be overridden per instance (e.g. ``self.rebalance_band`` set from a
    parameter in ``__init__``); the engine and backtester read them from the instance.
    """

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    #: not validated out of sample: forward paper-test only (badge in the UI)
    experimental: ClassVar[bool] = False
    default_params: ClassVar[dict[str, Any]] = {}
    param_schema: ClassVar[dict[str, dict[str, Any]]] = {}
    #: bar size in seconds: 3600 or 86400 (UTC days)
    bar_granularity_s: ClassVar[int] = 86400
    #: closed bars per product the engine/backtester must pre-load before ``on_bar``
    history_bars: ClassVar[int] = 250
    #: "taker" or "maker_then_taker" (see :data:`EXECUTIONS`)
    execution: ClassVar[str] = "taker"
    #: no-trade band: resizes smaller than this fraction of the allocation are skipped
    rebalance_band: ClassVar[float] = 0.02
    #: whether ``run_spot_backtest`` can replay it on research/coinbase/data
    backtestable: ClassVar[bool] = True
    #: runs when neither the dashboard nor the config says otherwise (module doc)
    enabled_by_default: ClassVar[bool] = False
    #: per-strategy risk defaults, overridden by ``coinbase.strategies.<name>`` in the config;
    #: ``max_allocation_pct`` (see :func:`allocation_pct`)
    risk_defaults: ClassVar[dict[str, Any]] = {}

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        self.params: dict[str, Any] = self.resolve_params(params)

    @classmethod
    def resolve_params(cls, params: Mapping[str, Any] | None = None, *, strict: bool = False) -> dict[str, Any]:
        """Defaults with ``params`` (validated against ``param_schema``) merged over them."""
        merged = dict(cls.default_params)
        merged.update(coerce_params(cls.param_schema, params, strict=strict))
        return merged

    def universe(self, products: Mapping[str, Product]) -> list[str]:
        """Products this strategy trades (and needs candles for), a subset of ``products``.

        Default: the ``products`` parameter if the strategy has one (a list of ids), else
        ``["BTC-USD"]``; ids missing from ``products`` are left out. Override for anything
        smarter (it is called with the tradable USD products, live and in backtests).
        """
        want = self.params.get("products")
        if isinstance(want, str):
            want = [p.strip() for p in want.split(",") if p.strip()]
        ids = [str(p).upper() for p in (want or ["BTC-USD"])]
        return [p for p in dict.fromkeys(ids) if p in products]

    @abstractmethod
    def on_bar(self, ctx: SpotContext) -> list[TargetWeight] | None:
        """Target weights after the bar that closed at ``ctx.bar_end`` (``None`` = no change)."""

    # -- optional persistence hooks (the engine saves/restores via the store) --------

    def dump_state(self) -> Any:
        """JSON-serializable state to persist after each bar (``None`` = nothing)."""
        return None

    def load_state(self, state: Any) -> None:  # noqa: B027 - optional hook
        """Restore state saved by :meth:`dump_state` (called once after construction)."""

    # -- description --------------------------------------------------------------

    @classmethod
    def class_problems(cls) -> list[str]:
        """Invalid class attributes (the registry refuses such classes)."""
        out: list[str] = []
        if not cls.name:
            out.append("missing name")
        if cls.bar_granularity_s not in GRANULARITIES:
            out.append(f"bar_granularity_s must be one of {GRANULARITIES} (got {cls.bar_granularity_s!r})")
        if cls.execution not in EXECUTIONS:
            out.append(f"execution must be one of {EXECUTIONS} (got {cls.execution!r})")
        hb = cls.history_bars
        if not isinstance(hb, int) or isinstance(hb, bool) or hb < 1:
            out.append(f"history_bars must be a positive integer (got {hb!r})")
        band = _finite_or_none(cls.rebalance_band)
        if band is None or not 0 <= band < 1:
            out.append(f"rebalance_band must be in [0, 1) (got {cls.rebalance_band!r})")
        return out

    @classmethod
    def schema_json(cls) -> dict[str, dict[str, Any]]:
        """``param_schema`` with each spec's ``default`` filled from ``default_params``."""
        out: dict[str, dict[str, Any]] = {}
        for k, spec in cls.param_schema.items():
            s = dict(spec)
            if "default" not in s and k in cls.default_params:
                s["default"] = cls.default_params[k]
            out[k] = s
        return out

    def info(self) -> dict[str, Any]:
        """Static description for ``GET /api/coinbase/strategies`` (instance values)."""
        return {
            "name": self.name,
            "description": self.description,
            "experimental": bool(self.experimental),
            "params": dict(self.params),
            "param_schema": self.schema_json(),
            "bar_granularity_s": int(self.bar_granularity_s),
            "history_bars": int(self.history_bars),
            "execution": self.execution,
            "rebalance_band": float(self.rebalance_band),
            "backtestable": bool(self.backtestable),
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r}, params={self.params!r})"


# --------------------------------------------------------------------------- resolution helpers


def _cb_settings(settings: Any) -> Any:
    """``settings.coinbase`` for a full ``Settings``; a ``CoinbaseSettings`` as is."""
    if settings is None:
        return None
    if isinstance(settings, Mapping):
        return settings.get("coinbase") or settings
    return getattr(settings, "coinbase", None) or settings


def _strategy_entry(settings: Any, name: str) -> Any:
    cb = _cb_settings(settings)
    if cb is None:
        return None
    fn = getattr(cb, "strategy", None)
    if callable(fn):
        try:
            return fn(name)
        except Exception:
            return None
    strategies = cb.get("strategies") if isinstance(cb, Mapping) else getattr(cb, "strategies", None)
    return (strategies or {}).get(name) if isinstance(strategies, Mapping) else None


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, Mapping) else getattr(obj, key, None)


def strategy_config(settings: Any, name: str) -> tuple[bool | None, dict[str, Any]]:
    """``(enabled, params)`` from ``coinbase.strategies.<name>`` of the config.

    ``settings``: a full :class:`kalshibot.config.Settings`, a ``CoinbaseSettings`` or a plain
    mapping; ``enabled`` is ``None`` when the config does not set it.
    """
    entry = _strategy_entry(settings, name)
    if entry is None:
        return None, {}
    enabled = _get(entry, "enabled")
    params = _get(entry, "params")
    return (None if enabled is None else bool(enabled)), dict(params or {})


def resolve_enabled(
    cls: type[SpotStrategy],
    *,
    stored: bool | None = None,
    config: bool | None = None,
) -> tuple[bool, str]:
    """``(enabled, source)``: the dashboard toggle (``stored``) beats the config, which beats
    the class's ``enabled_by_default``; ``source`` is "dashboard" / "config" / "default"."""
    if stored is not None:
        return bool(stored), "dashboard"
    if config is not None:
        return bool(config), "config"
    return bool(getattr(cls, "enabled_by_default", False)), "default"


def resolve_strategy_params(
    cls: type[SpotStrategy],
    *,
    config_params: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """``(params, error)``: ``default_params`` <- ``config_params`` <- ``overrides`` (stored
    dashboard edits), validated (unknown names dropped). If the merged values are invalid the
    defaults are returned with the error message (the engine logs it; nothing raises)."""
    merged = {**(config_params or {}), **(overrides or {})}
    try:
        return cls.resolve_params(merged), None
    except ParamError as e:
        return cls.resolve_params({}), str(e)


def allocation_pct(settings: Any, name: str, cls: type[SpotStrategy] | None = None) -> float | None:
    """This strategy's allocation, % of Coinbase equity: ``coinbase.strategies.<name>.
    max_allocation_pct`` if set, else the class's ``risk_defaults["max_allocation_pct"]``,
    else ``coinbase.risk.max_strategy_allocation_pct`` (``None`` if none is known)."""
    v = _get(_strategy_entry(settings, name), "max_allocation_pct")
    if v is None and cls is not None:
        v = (getattr(cls, "risk_defaults", None) or {}).get("max_allocation_pct")
    if v is None:
        v = _get(_get(_cb_settings(settings), "risk"), "max_strategy_allocation_pct")
    return _finite_or_none(v)
