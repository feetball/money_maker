"""Strategy interface (ARCHITECTURE.md §7): ``Strategy``, ``OrderIntent``, ``StrategyContext``.

A strategy is **pure decision logic**. Once per engine tick it receives a
:class:`StrategyContext` (market snapshot, order books, portfolio, fees, external feeds)
and returns a list of :class:`OrderIntent`. It never talks HTTP directly and never calls
the broker: the engine runs every intent through the risk manager and then the paper
broker, and records each one (with the decision and its reason) in the signals feed.

Rules for strategy authors
--------------------------
* Attach ``reason``, ``expected_edge`` ($/contract after fees at ``limit_price``) and, if
  the strategy has a model, ``fair_value`` (model P(``side`` wins)) to every intent.
* Prices are ``Decimal`` dollars for the side named; snap model prices to the market's
  grid with ``market.round_price(x, "down")`` or ``money.price(x, market.price_ranges)``.
* Don't re-enter a market you already hold unless your rules say so:
  ``ctx.portfolio.holds(ticker, self.name)`` / ``ctx.portfolio.has_open_order(...)``.
* Declare the data you need in :meth:`Strategy.universe`. ``ctx.markets`` only contains
  open markets matching *your* spec.
* Parameters: ``default_params`` + ``param_schema`` (``{name: {type, min, max, help}}``,
  types ``int|float|bool|str|enum|list|object``; ``enum`` specs list ``choices``).
  ``self.params`` holds the effective values (config and UI overrides merged over the
  defaults, validated by :func:`coerce_params`).

Batch-friendly data access: ``await ctx.orderbooks([...])`` fetches many books in one
request (100 per call); ``asyncio.gather(*(ctx.orderbook(t) for t in ts))`` is
coalesced into batch requests too. Calling ``await ctx.orderbook(t)`` one by one over
hundreds of markets is slow (the public API budget is ~3 requests/s).

Timing (``Strategy.tick_interval_s``)
-------------------------------------
Each strategy runs on its own fixed grid of ``tick_interval_s`` seconds (``None`` = the
engine's ``engine.tick_s``, default 30; values below 1 s are raised to 1 s). Strategies
tick concurrently, so a slow strategy never delays another; if a strategy's previous tick
is still running when its next slot comes, that slot is **skipped** (never bunched) and
counted in ``/api/strategies`` ``skipped_ticks``. A strategy that must act inside a short
window (e.g. 60 s) should declare ``tick_interval_s`` at most a third of it. An instance
attribute (e.g. set from a param in ``__init__``) overrides the class attribute.

Newly listed markets: the universe is refreshed every ``engine.universe_refresh_s``
(120 s). For short-lived series (e.g. 15-minute crypto windows) declare
``UniverseSpec(series_tickers=[...], refresh_s=20)``: those series are then also re-read
on their own every ``refresh_s`` seconds (one request per series; at least 15 s, the CDN
cache of ``/markets``).

Cancels and cancel/replace
--------------------------
``on_tick`` may return :class:`CancelIntent` items next to its order intents
(``CancelIntent(order_id=...)`` for one resting order, ``CancelIntent(ticker=...)`` for all
of the strategy's resting orders in a market). The engine applies every cancel of the tick
**before** placing its new orders, and only for the strategy's own orders. Before
cancelling, the resting orders' markets are brought up to date with the trade tape (prints
since the last poll can still fill them, as they would have on the exchange), so a cancel
may come back ``filled`` instead; ``on_fill`` is called for such fills.

To move a quote, return ``OrderIntent(..., tif="gtc", replaces=old_order_id)``: the engine
cancels ``old_order_id`` first and places the new order with ``count`` reduced by whatever
the old order filled since the tick's portfolio snapshot; if the old order is no longer
open (filled, expired, cancelled) the replacement is **not** placed (signal ``rejected``).
The engine context also offers ``ctx.cancel(order_id=None, *, ticker=None, reason="")``,
which queues a ``CancelIntent`` exactly as if it had been returned.
"""

from __future__ import annotations

import contextlib
import math
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, runtime_checkable

from kalshibot.money import ONE, D

if TYPE_CHECKING:
    from kalshibot.feeds import FeedRegistry
    from kalshibot.kalshi.models import Event, Market, Orderbook, Series
    from kalshibot.paper.models import Fill, PortfolioView, Settlement

__all__ = [
    "CancelIntent",
    "OrderIntent",
    "ParamError",
    "Strategy",
    "StrategyContext",
    "UniverseSpec",
    "coerce_params",
]

Side = Literal["yes", "no"]
Action = Literal["buy", "sell"]
Tif = Literal["ioc", "gtc"]


# --------------------------------------------------------------------------- OrderIntent


def _opt_decimal(x: Any) -> Decimal | None:
    if x is None:
        return None
    try:
        v = D(x)
    except (TypeError, ValueError, InvalidOperation):
        return None
    return v if v.is_finite() else None


def _opt_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


@dataclass
class OrderIntent:
    """What a strategy wants to do. The risk manager may reduce ``count``.

    ``limit_price`` is required (keyword). Values are normalized in ``__post_init__``:
    ``limit_price``/``expected_edge`` become ``Decimal`` (floats via their shortest repr),
    ``count`` an ``int``, ``side``/``action``/``tif`` lower-case strings. Invalid values are
    *not* raised here; the engine records them as rejected signals.
    """

    ticker: str
    side: Side
    action: Action = "buy"
    count: int = 1  # desired; risk manager may reduce
    limit_price: Decimal = field(kw_only=True)  # price for `side`
    tif: Tif = "ioc"
    expires_in_s: int | None = None  # gtc only
    strategy: str = ""
    reason: str = ""  # human-readable, shown in UI
    fair_value: float | None = None  # model P(side wins), if any
    expected_edge: Decimal | None = None  # $/contract after fees at limit_price
    group_id: str | None = None  # all-or-none basket id
    #: cancel/replace: id of this strategy's resting order to cancel first (see module doc)
    replaces: int | None = None

    def __post_init__(self) -> None:
        self.ticker = str(self.ticker or "")
        self.side = str(self.side or "").lower()  # type: ignore[assignment]
        self.action = str(self.action or "buy").lower()  # type: ignore[assignment]
        self.tif = str(self.tif or "ioc").lower()  # type: ignore[assignment]
        lp = _opt_decimal(self.limit_price)
        self.limit_price = lp if lp is not None else self.limit_price
        try:
            c = D(self.count)
            self.count = int(c) if c == c.to_integral_value() else self.count
        except (TypeError, ValueError, InvalidOperation):
            pass
        self.expected_edge = _opt_decimal(self.expected_edge)
        self.fair_value = _opt_float(self.fair_value)
        if self.expires_in_s is not None:
            try:
                self.expires_in_s = int(self.expires_in_s)
            except (TypeError, ValueError):
                self.expires_in_s = None
        self.reason = str(self.reason or "")
        self.strategy = str(self.strategy or "")
        if self.replaces is not None and not isinstance(self.replaces, bool):
            with contextlib.suppress(TypeError, ValueError, OverflowError):  # problems() reports it
                self.replaces = int(self.replaces)

    # -- helpers ----------------------------------------------------------------------

    def problems(self) -> list[str]:
        """Why this intent is malformed (empty list = well-formed)."""
        out: list[str] = []
        if not self.ticker:
            out.append("missing ticker")
        if self.side not in ("yes", "no"):
            out.append(f"side must be 'yes' or 'no' (got {self.side!r})")
        if self.action not in ("buy", "sell"):
            out.append(f"action must be 'buy' or 'sell' (got {self.action!r})")
        if self.tif not in ("ioc", "gtc"):
            out.append(f"tif must be 'ioc' or 'gtc' (got {self.tif!r})")
        if not isinstance(self.count, int) or isinstance(self.count, bool) or self.count <= 0:
            out.append(f"count must be a positive whole number (got {self.count!r})")
        if not isinstance(self.limit_price, Decimal) or not (0 < self.limit_price < 1):
            out.append(f"limit_price must be a Decimal strictly inside (0, 1) (got {self.limit_price!r})")
        if self.replaces is not None and (not isinstance(self.replaces, int) or isinstance(self.replaces, bool)
                                          or self.replaces <= 0):
            out.append(f"replaces must be an order id (got {self.replaces!r})")
        return out

    @property
    def buy_side(self) -> str:
        """Side actually bought (``sell yes @ p`` == ``buy no @ 1 - p``)."""
        if self.action == "buy":
            return self.side
        return "no" if self.side == "yes" else "yes"

    @property
    def buy_price(self) -> Decimal:
        return self.limit_price if self.action == "buy" else ONE - self.limit_price

    def to_json(self) -> dict[str, Any]:
        lp = self.limit_price
        return {
            "ticker": self.ticker,
            "side": self.side,
            "action": self.action,
            "count": self.count,
            "limit_price": float(lp) if isinstance(lp, Decimal) else None,
            "tif": self.tif,
            "expires_in_s": self.expires_in_s,
            "strategy": self.strategy,
            "reason": self.reason,
            "fair_value": self.fair_value,
            "expected_edge": float(self.expected_edge) if self.expected_edge is not None else None,
            "group_id": self.group_id,
            "replaces": self.replaces,
        }


# --------------------------------------------------------------------------- CancelIntent


@dataclass
class CancelIntent:
    """Cancel this strategy's resting order(s): by ``order_id``, or every one in ``ticker``.

    Returned from ``on_tick`` next to :class:`OrderIntent` items (or queued with
    ``ctx.cancel(...)``). The engine applies cancels before the tick's new orders, only to
    the strategy's own open orders (others are ignored and logged), after syncing the
    orders' markets with the trade tape. ``reason`` is shown on the cancelled order.
    """

    order_id: int | None = None
    ticker: str | None = None
    reason: str = ""
    strategy: str = ""

    def __post_init__(self) -> None:
        if self.order_id is not None and not isinstance(self.order_id, bool):
            with contextlib.suppress(TypeError, ValueError, OverflowError):  # problems() reports it
                self.order_id = int(self.order_id)
        self.ticker = str(self.ticker) if self.ticker else None
        self.reason = str(self.reason or "")
        self.strategy = str(self.strategy or "")

    def problems(self) -> list[str]:
        out: list[str] = []
        if self.order_id is None and not self.ticker:
            out.append("cancel needs an order_id or a ticker")
        if self.order_id is not None and (not isinstance(self.order_id, int) or isinstance(self.order_id, bool)
                                          or self.order_id <= 0):
            out.append(f"order_id must be a positive integer (got {self.order_id!r})")
        return out

    def to_json(self) -> dict[str, Any]:
        return {"order_id": self.order_id, "ticker": self.ticker, "reason": self.reason, "strategy": self.strategy}


# --------------------------------------------------------------------------- UniverseSpec


@dataclass
class UniverseSpec:
    """Market data a strategy needs (the engine's universe is the union over strategies).

    * ``max_days_to_close``: active markets closing within N days (from now).
    * ``series_tickers``: every open market of these series (any close time).
    * ``refresh_s`` (optional): also re-read ``series_tickers`` on their own every
      ``refresh_s`` seconds (>= 15 s), so newly listed markets of short-lived series (e.g.
      15-minute crypto windows) show up in ``ctx.markets`` well before the next full
      universe refresh. Costs one request per series per refresh; ``None`` = only the
      regular universe refresh (``engine.universe_refresh_s``).

    An empty spec (both unset) asks for no markets.
    """

    max_days_to_close: float | None = None
    series_tickers: list[str] = field(default_factory=list)
    refresh_s: float | None = None

    def __post_init__(self) -> None:
        self.series_tickers = [str(s) for s in (self.series_tickers or ()) if s]
        if self.max_days_to_close is not None:
            self.max_days_to_close = float(self.max_days_to_close)
        if self.refresh_s is not None:
            try:
                v = float(self.refresh_s)
            except (TypeError, ValueError):
                v = math.nan
            self.refresh_s = v if math.isfinite(v) and v > 0 else None

    @property
    def is_empty(self) -> bool:
        return not self.series_tickers and not (self.max_days_to_close and self.max_days_to_close > 0)

    def matches(self, market: Market, now: datetime | None = None) -> bool:
        """Whether ``market`` falls inside this spec (status is not checked)."""
        if market.series_ticker in self.series_tickers:
            return True
        if self.max_days_to_close and self.max_days_to_close > 0 and market.close_time is not None:
            now = now or datetime.now(UTC)
            return now <= market.close_time <= now + timedelta(days=self.max_days_to_close)
        return False


# --------------------------------------------------------------------------- context


@runtime_checkable
class StrategyContext(Protocol):
    """What a strategy sees on each tick (the engine's ``EngineContext`` implements it).

    Beyond the §7 contract the engine's context also offers ``strategy`` (name),
    ``params``, ``async orderbook(ticker, max_age_s=...)`` (``max_age_s=0``: a book fetched
    now, never a cached one), ``async orderbooks(tickers)``, ``async market(ticker)``,
    ``async event(event_ticker)`` (lazily fetched, cached ~1 h) and
    ``cancel(order_id=None, *, ticker=None, reason="")`` (queues a :class:`CancelIntent`;
    returning one from ``on_tick`` is equivalent and also works in other contexts).
    """

    now: datetime
    markets: Mapping[str, Market]  # open universe (this strategy's spec)
    events: Mapping[str, Event]  # events fetched so far (lazy cache)
    portfolio: PortfolioView  # positions, open orders, cash, equity
    feeds: FeedRegistry  # external data (spot prices, etc.)

    async def series(self, series_ticker: str) -> Series: ...

    async def orderbook(self, ticker: str) -> Orderbook: ...

    def fee(self, market: Market, price: Any, count: Any, is_taker: bool = True) -> Decimal: ...

    def log(self, msg: str, **data: Any) -> None: ...


# --------------------------------------------------------------------------- params


class ParamError(ValueError):
    """Invalid strategy parameters (unknown name, wrong type, out of range)."""


_INT = {"int", "integer"}
_FLOAT = {"float", "number", "decimal"}
_BOOL = {"bool", "boolean"}
_STR = {"str", "string"}
_LIST = {"list", "array"}
_OBJ = {"object", "dict"}


def _choices(spec: Mapping[str, Any]) -> Sequence[Any] | None:
    for k in ("enum", "choices", "options"):
        v = spec.get(k)
        if isinstance(v, list | tuple):
            return list(v)
    return None


def _coerce_one(name: str, value: Any, spec: Mapping[str, Any]) -> Any:
    typ = str(spec.get("type", "")).lower()
    if value is None:
        if spec.get("nullable") or spec.get("default", ...) is None:
            return None
        raise ParamError(f"{name}: must not be null")
    try:
        if typ in _INT:
            if isinstance(value, bool):
                raise ParamError(f"{name}: expected an integer, got a boolean")
            f = float(value)
            if not math.isfinite(f) or f != int(f):
                raise ParamError(f"{name}: expected an integer, got {value!r}")
            value = int(f)
        elif typ in _FLOAT:
            if isinstance(value, bool):
                raise ParamError(f"{name}: expected a number, got a boolean")
            value = float(value)
            if not math.isfinite(value):
                raise ParamError(f"{name}: must be finite")
        elif typ in _BOOL:
            if isinstance(value, str):
                low = value.strip().lower()
                if low in ("true", "1", "yes", "on"):
                    value = True
                elif low in ("false", "0", "no", "off"):
                    value = False
                else:
                    raise ParamError(f"{name}: expected a boolean, got {value!r}")
            elif isinstance(value, int | float):
                value = bool(value)
            elif not isinstance(value, bool):
                raise ParamError(f"{name}: expected a boolean, got {value!r}")
        elif typ in _STR:
            if not isinstance(value, str | int | float) or isinstance(value, bool):
                raise ParamError(f"{name}: expected a string, got {value!r}")
            value = str(value)
        elif typ in _LIST:
            if isinstance(value, str):
                value = [v.strip() for v in value.split(",") if v.strip()]
            if not isinstance(value, list | tuple):
                raise ParamError(f"{name}: expected a list, got {value!r}")
            value = list(value)
        elif typ in _OBJ:
            if not isinstance(value, Mapping):
                raise ParamError(f"{name}: expected an object, got {value!r}")
            value = dict(value)
        # "enum" and unknown types: value kept as-is (checked against choices below)
    except ParamError:
        raise
    except (TypeError, ValueError, OverflowError) as e:
        raise ParamError(f"{name}: invalid value {value!r} for type {typ or 'any'} ({e})") from None
    if isinstance(value, int | float) and not isinstance(value, bool):
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and value < lo:
            raise ParamError(f"{name}: {value} is below the minimum {lo}")
        if hi is not None and value > hi:
            raise ParamError(f"{name}: {value} is above the maximum {hi}")
    ch = _choices(spec)
    if ch is not None:
        vals = value if isinstance(value, list) and typ in _LIST else [value]
        for v in vals:
            if v not in ch:
                raise ParamError(f"{name}: {v!r} is not one of {ch}")
    return value


def coerce_params(
    schema: Mapping[str, Mapping[str, Any]],
    params: Mapping[str, Any] | None,
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """Validate/coerce ``params`` against ``schema``.

    ``strict``: unknown names raise :class:`ParamError`; otherwise they are dropped.
    Parameters missing from the schema entirely (schema ``{}``) pass through untouched.
    """
    out: dict[str, Any] = {}
    errors: list[str] = []
    for k, v in (params or {}).items():
        spec = schema.get(k)
        if spec is None:
            if not schema:
                out[k] = v
            elif strict:
                errors.append(f"unknown parameter {k!r} (known: {', '.join(sorted(schema))})")
            continue
        try:
            out[k] = _coerce_one(k, v, spec)
        except ParamError as e:
            errors.append(str(e))
    if errors:
        raise ParamError("; ".join(errors))
    return out


# --------------------------------------------------------------------------- Strategy


class Strategy(ABC):
    """Base class. Subclasses set the class attributes and implement :meth:`on_tick`."""

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    default_params: ClassVar[dict[str, Any]] = {}
    param_schema: ClassVar[dict[str, dict[str, Any]]] = {}
    #: for backtests (§10): whether the strategy can run on candle data
    backtestable: ClassVar[bool] = False
    #: seconds between ticks (``None`` = ``engine.tick_s``); see "Timing" in the module doc
    tick_interval_s: ClassVar[float | None] = None
    #: not validated out of sample: forward paper-test only (shown as a badge; ``/api/strategies``)
    experimental: ClassVar[bool] = False
    #: whether the strategy runs when nothing says otherwise. Precedence (first set wins): the
    #: dashboard toggle (stored in the database), ``strategies.<name>.enabled`` in the config
    #: (or ``KALSHIBOT_STRATEGIES__<NAME>__ENABLED``), then this. The shipped research strategies
    #: set it to True, so a ``config.yaml`` created before they existed (``strategies: {}``) runs
    #: them after a restart; ``enabled: false`` in the config or the dashboard switch turns one off.
    enabled_by_default: ClassVar[bool] = False
    #: default per-strategy risk limits, overridden by ``strategies.<name>`` in the config:
    #: ``max_allocation_pct`` (cap on this strategy's exposure, % of equity; falls back to
    #: ``risk.max_strategy_allocation_pct``) and ``daily_loss_limit`` (dollars; pauses only this
    #: strategy's entries until the next UTC day; 0 = off). See ``kalshibot.risk.strategy_limits``.
    risk_defaults: ClassVar[dict[str, Any]] = {}

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        self.params: dict[str, Any] = self.resolve_params(params)

    @classmethod
    def resolve_params(cls, params: Mapping[str, Any] | None = None, *, strict: bool = False) -> dict[str, Any]:
        """Defaults with ``params`` (validated against ``param_schema``) merged over them."""
        merged = dict(cls.default_params)
        merged.update(coerce_params(cls.param_schema, params, strict=strict))
        return merged

    def universe(self) -> UniverseSpec:
        """What market data this strategy needs (default: nothing)."""
        return UniverseSpec()

    @abstractmethod
    async def on_tick(self, ctx: StrategyContext) -> list[OrderIntent]:
        """Return the intents for this tick (may be empty)."""

    def on_fill(self, fill: Fill) -> None:  # noqa: B027 - optional hook
        """Called after one of this strategy's orders filled (paper)."""

    def on_settlement(self, s: Settlement) -> None:  # noqa: B027 - optional hook
        """Called when one of this strategy's positions settled or was closed."""

    # -- optional persistence hooks (engine saves/restores via the store) -----------

    def dump_state(self) -> Any:
        """JSON-serializable state to persist after each tick (``None`` = nothing)."""
        return None

    def load_state(self, state: Any) -> None:  # noqa: B027 - optional hook
        """Restore state saved by :meth:`dump_state` (called once after construction)."""

    # -- description ----------------------------------------------------------------

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

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r}, params={self.params!r})"


def intents_list(result: Any) -> list[Any]:
    """Normalize an ``on_tick`` return value (None / single intent / iterable) to a list."""
    if result is None:
        return []
    if isinstance(result, OrderIntent | CancelIntent):
        return [result]
    if isinstance(result, Iterable) and not isinstance(result, str | bytes | Mapping):
        return list(result)
    return [result]
