"""Coinbase spot backtester (docs/COINBASE_CONTRACT.md §12) - PAPER / RESEARCH ONLY.

``run_spot_backtest(strategy_cls, params, start=, end=, starting_balance=, fee_tier=,
slippage=, data_dir=)`` replays the research candles in ``research/coinbase/data`` (see its
``loader.py`` / ``README.md``; read here with pyarrow + numpy, no pandas) through the **same**
:class:`~kalshibot.coinbase.strategies.base.SpotStrategy` classes and the **same**
:func:`~kalshibot.coinbase.rebalance.plan_rebalance` planner the live engine uses.

Clock, decisions and fills (no look-ahead, by construction)
    The replay walks the regular UTC grid of the strategy's ``bar_granularity_s`` (3600 or
    86400). At each grid time ``T`` (= the close of the bar that just completed = the open of
    the next one):

    1. **Decide at bar close.** The strategy's :class:`SpotContext` has ``bar_end = T`` and
       ``now = T + bar_delay_s`` (60 s, like the engine). ``ctx.candles(pid, n)`` returns only
       bars with ``end <= T`` (oldest first); ``ctx.stats(pid)`` is derived from those bars;
       ``ctx.products`` holds the universe products that have traded recently
       (``stale_after_s``) - a product listed later is invisible until its first bar has
       closed, and current exchange status (e.g. a later delisting) is never shown;
       ``ctx.portfolio`` is marked at the closes known at ``T``.
    2. The targets go through ``normalize_targets`` and ``plan_rebalance`` (prices = the last
       closes, cash-aware; sells first, then buys).
    3. **Fill at the next bar's open.** Each intent fills at the open of the product's bar
       starting at ``T`` (the bar ``t+1``; ``fill_price="pessimistic"``: the worse of the open
       and (O+H+L+C)/4, since live trading starts only at ``T + bar_delay_s``), moved against
       us by the half-spread slippage (``slippage="spread"``: half the median spread of
       ``book_snapshot.parquet`` for the product; products without a snapshot get
       :func:`fallback_slippage_bps` - a point-in-time estimate from their trailing 30-day USD
       volume, never below the widest measured half-spread; or a fixed number of bps; all
       times ``slippage_multiplier``), plus the taker fee of ``fee_tier``
       (``kalshibot.coinbase.fees``, rounded up to the cent). Each fill is capped at
       ``max_participation`` (10 %) of the bar's USD volume (volume x (O+H+L+C)/4); the rest
       is not filled (``partial``; the strategy re-plans next bar). Sells by ``base_size``
       (capped at the holding), buys by ``quote_size`` including the fee (capped at cash; size
       rounded down to ``base_increment``); ``min_market_funds`` applies. No bar at ``T`` for
       that product (no trades) -> the intent is ``unfilled`` (the strategy re-plans next bar).
    4. **Mark at that bar's close** (``T + g``): equity = cash + holdings at close x
       (1 - half-spread) x (1 - taker rate), i.e. net of the exit fee (``equity_mid`` at the
       close, no fee). Allocations are sized before exit fees, like the live broker.

    So a decision made with the bar ending at ``T`` can only trade at prices from after ``T``,
    and nothing the strategy sees depends on any bar ending after ``T``
    (``tests/test_cb_backtest.py`` perturbs every bar after ``T`` and checks that decisions,
    fills and equity up to ``T`` do not change).

Benchmarks (same grid, fees and slippage, via the same simulator; never capped by ``limits``
and without the $ minimum trade - only ``min_market_funds`` - so a small account can still
spread over a broad universe)
    ``btc``          buy BTC-USD with all cash at the first bar's fill, hold;
    ``equal_weight`` equal weights over the strategy's live universe, rebalanced on the
                     first bar of every month (2% band).

Output (a JSON-safe dict; extends the Kalshi backtest detail ``{metrics, equity_curve, trades,
by_month}``)::

    {venue: "coinbase", strategy, params, start, end, starting_balance, granularity_s,
     metrics: {total_return_pct, cagr_pct, vol_pct, sharpe, sortino, max_drawdown_pct,
               max_drawdown, calmar, turnover_per_year, fees_paid, fees, pct_time_invested,
               avg_exposure_pct, trades, n_trades, buys, sells, round_trips, win_rate,
               final_equity, total_pnl, years, bars,
               excess_return_vs_btc_pct, excess_return_vs_equal_weight_pct,
               benchmarks: {btc: {...same metrics...} | None, equal_weight: {...} | None},
               details: {period, universe, fee_tier, slippage, options, dataset,
                         signals, skip_reasons, target_problems, strategy_errors, errors,
                         strategy_logs, stuck_positions, look_ahead, known_biases, elapsed_s}},
     equity_curve: [{ts, equity, equity_mid, cash, invested, exposure_pct, drawdown_pct}],
     benchmarks: {btc: [{ts, equity}], equal_weight: [{ts, equity}]},
     trades: [{ts, product_id, side, base_size, price, open_price, slippage_bps, notional,
               fee, fee_rate, is_taker, realized_pnl, strategy, reason, target_weight}],
     by_year: [{year, return_pct, pnl, start_equity, end_equity, max_drawdown_pct, trades,
                fees, btc_return_pct, equal_weight_return_pct}],
     by_month: [{month, return_pct, pnl, trades, fees, btc_return_pct, equal_weight_return_pct}],
     signals: [{ts, product_id, side, target_weight, quote_size, base_size, expected_edge_bps,
                reason, decision, decision_reason}]}

The equity curve and benchmark curves hold one point per UTC day (the last bar close of the
day), thinned uniformly to ``max_points``; ``by_year`` / ``by_month`` / Sharpe use the daily
points; max drawdown uses every bar. Timestamps are ISO-8601 UTC with ``Z``.

Other options: ``min_trade_usd`` (10, ``coinbase.risk.min_trade_usd``), ``allocation_pct``
(100: the share of equity the strategy's weights refer to), ``limits`` (optional dict with
``max_position_pct_per_product`` / ``max_total_exposure_pct`` / ``min_cash_reserve`` applied to
the strategy's targets like the live risk caps - off by default, never applied to the
benchmarks), ``bar_delay_s`` (60), ``stale_after_s`` (3 days daily / 1 day hourly),
``benchmarks`` (True), ``max_participation`` (0.10; 0 = off), ``slippage_multiplier`` (1.0),
``fill_price`` ("open" | "pessimistic"), ``max_points`` (2500), ``max_trades`` (2000, the most recent are kept),
``max_signals`` (500; results are stored in SQLite, so the defaults keep one run near
1 MB), ``settings`` (a ``Settings``/``CoinbaseSettings``: defaults for the fee tier,
``min_trade_usd`` and ``bar_delay_s``), ``dataset`` (a :class:`SpotDataset`, e.g. synthetic
bars in tests) and ``progress`` (callback with the fraction done).
"""

from __future__ import annotations

import logging
import math
import time
from bisect import bisect_right
from collections import Counter, deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar

import numpy as np

from kalshibot.analytics import drawdown
from kalshibot.coinbase.fees import DEFAULT_TIER, FeeTier, fee_for, get_tier, notional_for_budget, resolve_tier
from kalshibot.coinbase.models import Candle, Product, Stats
from kalshibot.coinbase.paper import SpotOrderIntent, SpotPortfolioView, SpotPosition
from kalshibot.coinbase.rebalance import floor_to, plan_from_view
from kalshibot.coinbase.strategies.base import GRANULARITIES, SpotStrategy, TargetWeight, normalize_targets

__all__ = [
    "BTC",
    "DEFAULT_OPTIONS",
    "DEFAULT_SLIPPAGE_BPS",
    "RESEARCH_DATA_DIR",
    "BacktestSpotContext",
    "BarSeries",
    "SpotBacktestError",
    "SpotDataset",
    "compute_spot_metrics",
    "fallback_slippage_bps",
    "load_research_dataset",
    "load_research_products",
    "run_spot_backtest",
]

log = logging.getLogger(__name__)

#: ``research/coinbase/data`` next to the package (the repo layout); override with ``data_dir``.
RESEARCH_DATA_DIR = Path(__file__).resolve().parents[2] / "research" / "coinbase" / "data"
#: minimum one-way slippage (bps) for products without a spread snapshot; the effective floor
#: is the widest measured half-spread of the snapshot when that is wider
DEFAULT_SLIPPAGE_BPS = 5.0
#: products without a spread snapshot: half-spread (bps) = 10 ** (A + B x log10(trailing 30-day
#: USD volume)), point in time. Fitted on the 25 products of book_snapshot.parquet (Sep 2026:
#: BTC 0.0 .. BONK 13.6 bps); it gives ~50 bps at $1M/30 d and ~210 bps at $100k/30 d, in line
#: with live books of thin products (DEXT ~165, WAXL ~81, KRL ~53 bps).
FALLBACK_SPREAD_FIT = (5.5, -0.634)
#: cap of the fallback one-way slippage (bps)
FALLBACK_MAX_BPS = 300.0
BTC = "BTC-USD"
DAY = 86400
YEAR_DAYS = 365.25
ZERO = Decimal(0)

DEFAULT_OPTIONS: dict[str, Any] = {
    "min_trade_usd": 10,
    "allocation_pct": 100.0,
    "limits": None,
    "bar_delay_s": 60,
    "stale_after_s": None,
    "benchmarks": True,
    "max_points": 2500,
    "max_trades": 2000,
    "max_signals": 500,
    #: at most this fraction of the fill bar's USD volume (volume x (O+H+L+C)/4) per fill; 0 = off
    "max_participation": 0.10,
    #: every one-way slippage (measured, fallback or fixed bps) is multiplied by this
    "slippage_multiplier": 1.0,
    #: "open" (zero latency: the bar's open) or "pessimistic" (buys: max(open, (O+H+L+C)/4),
    #: sells: min(...)) - the engine cannot trade before bar_end + bar_delay_s
    "fill_price": "open",
}

KNOWN_BIASES = [
    "Fills at the next bar's open +/- a half-spread from a recent order-book snapshot: no depth "
    "impact and today's spreads applied to all history (older / smaller markets were wider). "
    "Products without a snapshot use a point-in-time estimate from their trailing 30-day USD "
    "volume (never below the widest measured half-spread).",
    "Zero latency: the fill uses the open of the bar starting at the decision's bar_end, i.e. the "
    "first print at or after it, while the engine decides at bar_end + bar_delay_s (60 s) or later. "
    "Signals that predict the first minutes after the close look better than they trade; "
    "fill_price='pessimistic' fills at the worse of the open and (O+H+L+C)/4 instead.",
    "Each fill is capped at max_participation (10%) of the fill bar's USD volume; within the cap "
    "the whole size fills at the open (no depth impact).",
    "Current increments and minimum funds are applied to all history.",
    "Delisted products stay in the data until their last bar; a position in one cannot be sold after "
    "its last bar and is marked at its last close.",
    "Benchmarks hold uncapped weights; live risk caps (e.g. 50% per product) are only applied to the "
    "strategy when the 'limits' option is set.",
]


class SpotBacktestError(RuntimeError):
    """The data is missing/unusable or the request is invalid (the message says what to do)."""


# --------------------------------------------------------------------------- helpers


def _iso(ts: int | datetime | None) -> str | None:
    if ts is None:
        return None
    dt = ts if isinstance(ts, datetime) else datetime.fromtimestamp(int(ts), tz=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=UTC)


def _d(x: float) -> Decimal:
    """Float -> Decimal via the shortest repr (``0.1`` -> ``Decimal("0.1")``)."""
    return Decimal(repr(float(x)))


def _r(x: Any, nd: int = 6) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if math.isfinite(v) else None


def _to_ts(value: Any) -> int:
    """Unix seconds from int/float seconds, ``datetime``, ``numpy.datetime64`` or ISO string."""
    if isinstance(value, bool):
        raise TypeError("bool is not a timestamp")
    if isinstance(value, int | float | np.integer | np.floating):
        return int(value)
    if isinstance(value, np.datetime64):
        return int(value.astype("datetime64[s]").astype(np.int64))
    if isinstance(value, datetime):
        return int((value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp())
    if isinstance(value, date):
        return int(datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp())
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return int((dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp())


def _parse_when(value: Any, *, end: bool = False) -> int | None:
    """``YYYY-MM-DD`` (a whole UTC day: ``end`` dates are inclusive), ISO datetime, datetime,
    date or unix seconds -> unix seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        ts = _to_ts(value)
        return ts + DAY if end else ts
    if isinstance(value, str) and len(value.strip()) == 10:
        try:
            d = date.fromisoformat(value.strip())
        except ValueError:
            pass
        else:
            ts = _to_ts(d)
            return ts + DAY if end else ts
    try:
        return _to_ts(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"invalid date/time {value!r}: {e}") from None


def _default_product(pid: str) -> Product:
    base, _, quote = pid.partition("-")
    return Product(product_id=pid, base_currency=base, quote_currency=quote or "USD",
                   base_increment=Decimal("0.00000001"), quote_increment=Decimal("0.01"),
                   min_market_funds=Decimal("1"), status="online", trading_disabled=False,
                   post_only=False, limit_only=False, cancel_only=False, display_name=f"{base}/{quote or 'USD'}")


# --------------------------------------------------------------------------- bars


class BarSeries:
    """One product's bars as numpy arrays (``start`` = bar open, unix s; ascending, unique).

    Rows with missing / non-positive prices are dropped; duplicate starts keep the last row.
    """

    __slots__ = ("_candles", "_closes", "_cum_usd", "_index", "_memo", "_starts", "close", "granularity_s",
                 "high", "low", "open", "product_id", "start", "volume")

    def __init__(self, product_id: str, granularity_s: int, start: Any, open_: Any, high: Any, low: Any,
                 close: Any, volume: Any = None) -> None:
        st = np.asarray(start, dtype=np.int64)
        cols = [np.asarray(x, dtype=np.float64) for x in (open_, high, low, close)]
        vol = np.zeros(len(st)) if volume is None else np.asarray(volume, dtype=np.float64)
        if not all(len(c) == len(st) for c in (*cols, vol)):
            raise ValueError(f"{product_id}: bar columns differ in length")
        ok = np.ones(len(st), dtype=bool)
        for c in cols:
            ok &= np.isfinite(c) & (c > 0)
        vol = np.where(np.isfinite(vol) & (vol > 0), vol, 0.0)
        order = np.argsort(st[ok], kind="stable")
        st = st[ok][order]
        cols = [c[ok][order] for c in cols]
        vol = vol[ok][order]
        if len(st) > 1:  # unique starts, keep the last occurrence
            keep = np.append(st[1:] != st[:-1], True)
            st, cols, vol = st[keep], [c[keep] for c in cols], vol[keep]
        self.product_id = product_id
        self.granularity_s = int(granularity_s)
        self.start = st
        self.open, self.high, self.low, self.close = cols
        self.volume = vol
        self._candles: list[Candle] | None = None
        self._index: dict[int, int] | None = None
        # scalar lookups: plain lists + bisect are ~10x faster than numpy for one value
        self._starts: list[int] = st.tolist()
        self._closes: list[float] = self.close.tolist()
        self._memo: dict[int, int] = {}
        self._cum_usd: np.ndarray | None = None

    def __len__(self) -> int:
        return len(self.start)

    @property
    def has_volume(self) -> bool:
        """Whether the series carries volume at all (synthetic bars may not)."""
        return bool(len(self.volume)) and float(self.volume.max()) > 0

    def bar_usd_volume(self, i: int) -> float:
        """USD traded in bar ``i``: volume x (O+H+L+C)/4."""
        return float(self.volume[i]) * self.vwap_proxy(i)

    def vwap_proxy(self, i: int) -> float:
        return (float(self.open[i]) + float(self.high[i]) + float(self.low[i]) + float(self.close[i])) / 4

    def usd_volume_closed(self, t: int, window_s: int) -> float:
        """USD volume of the bars that closed in ``(t - window_s, t]`` (known at ``t``)."""
        if self._cum_usd is None:
            usd = self.volume * (self.open + self.high + self.low + self.close) / 4
            self._cum_usd = np.concatenate([[0.0], np.cumsum(usd)])
        k = bisect_right(self._starts, t - self.granularity_s)
        j = bisect_right(self._starts, t - window_s - self.granularity_s)
        return float(self._cum_usd[k] - self._cum_usd[j])

    @property
    def first_start(self) -> int | None:
        return int(self.start[0]) if len(self.start) else None

    @property
    def last_end(self) -> int | None:
        return int(self.start[-1]) + self.granularity_s if len(self.start) else None

    def n_closed(self, t: int) -> int:
        """Number of bars that have **closed** by ``t`` (``start + granularity <= t``)."""
        k = self._memo.get(t)
        if k is None:
            if len(self._memo) > 16:
                self._memo.clear()
            k = self._memo[t] = bisect_right(self._starts, t - self.granularity_s)
        return k

    def index_at(self, start_ts: int) -> int | None:
        """Index of the bar that opens exactly at ``start_ts`` (``None``: no trades then)."""
        if self._index is None:
            self._index = {int(s): i for i, s in enumerate(self.start)}
        return self._index.get(int(start_ts))

    def last_close(self, t: int) -> float | None:
        k = self.n_closed(t)
        return self._closes[k - 1] if k else None

    def candle_list(self) -> list[Candle]:
        """All bars as :class:`Candle` objects (built once; internal - never hand it out whole)."""
        if self._candles is None:
            g, pid = self.granularity_s, self.product_id
            self._candles = [
                Candle(product_id=pid, start=datetime.fromtimestamp(int(s), tz=UTC), granularity_s=g,
                       open=_d(o), high=_d(h), low=_d(lo), close=_d(c), volume=_d(v))
                for s, o, h, lo, c, v in zip(self.start.tolist(), self.open.tolist(), self.high.tolist(),
                                             self.low.tolist(), self.close.tolist(), self.volume.tolist(),
                                             strict=True)
            ]
        return self._candles

    def stats_at(self, t: int) -> Stats | None:
        """24 h stats as of ``t`` from bars closed by ``t`` (``None`` before the first bar)."""
        k = self.n_closed(t)
        if k == 0:
            return None
        j = self.n_closed(t - DAY)
        j30 = self.n_closed(t - 30 * DAY)
        last = float(self.close[k - 1])
        if k > j:
            o, h, lo = float(self.open[j]), float(self.high[j:k].max()), float(self.low[j:k].min())
            v24 = float(self.volume[j:k].sum())
        else:  # nothing traded in the last 24 h
            o = h = lo = last
            v24 = 0.0
        v30 = float(self.volume[j30:k].sum())
        return Stats(product_id=self.product_id, open=_d(o), high=_d(h), low=_d(lo), last=_d(last),
                     volume_24h=_d(v24), volume_30d=_d(v30))


@dataclass
class SpotDataset:
    """Bars of one granularity for a set of products, plus product metadata and spreads."""

    granularity_s: int
    bars: dict[str, BarSeries]
    products: dict[str, Product] = field(default_factory=dict)
    #: median full spread (bps) per product from the order-book snapshot
    spreads_bps: dict[str, float] = field(default_factory=dict)
    source: str = "memory"
    #: replay-internal: last live-product set per (universe, staleness)
    _live_cache: dict[Any, tuple[int, dict[str, Product]]] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        for pid in self.bars:
            if pid not in self.products:
                self.products[pid] = _default_product(pid)

    @classmethod
    def from_rows(cls, rows: Mapping[str, Iterable[Sequence[Any]]], *, granularity_s: int = DAY,
                  products: Mapping[str, Product] | None = None,
                  spreads_bps: Mapping[str, float] | None = None, source: str = "memory") -> SpotDataset:
        """``{product_id: [(ts, open, high, low, close[, volume]), ...]}`` -> dataset.

        ``ts`` is the bar OPEN (unix s, datetime or ISO string). Products without metadata get
        default increments (1e-8 base, $0.01 quote, $1 minimum funds).
        """
        bars: dict[str, BarSeries] = {}
        for pid, rs in rows.items():
            rs = list(rs)
            cols: list[Sequence[Any]] = list(zip(*rs, strict=False)) if rs else [(), (), (), (), ()]
            vol = cols[5] if len(cols) > 5 else None
            bars[pid] = BarSeries(pid, granularity_s, [_to_ts(x) for x in cols[0]], cols[1], cols[2], cols[3],
                                  cols[4], vol)
        return cls(granularity_s=granularity_s, bars=bars, products=dict(products or {}),
                   spreads_bps=dict(spreads_bps or {}), source=source)

    def describe(self) -> dict[str, Any]:
        firsts = [b._starts[0] for b in self.bars.values() if len(b)]
        lasts = [b._starts[-1] + b.granularity_s for b in self.bars.values() if len(b)]
        return {"source": self.source, "granularity_s": self.granularity_s, "products": len(self.bars),
                "bars": int(sum(len(b) for b in self.bars.values())),
                "first": _iso(min(firsts)) if firsts else None, "last": _iso(max(lasts)) if lasts else None,
                "spread_snapshot_products": len(self.spreads_bps)}


# --------------------------------------------------------------------------- research data


def _data_dir(data_dir: Any = None) -> Path:
    return Path(data_dir) if data_dir else RESEARCH_DATA_DIR


def _pq() -> Any:
    try:
        import pyarrow.parquet as pq
    except ImportError as e:  # pragma: no cover - pyarrow is a project dependency
        raise SpotBacktestError("pyarrow is required to read research/coinbase/data (uv sync)") from e
    return pq


def _col(table: Any, name: str) -> np.ndarray:
    import pyarrow as pa

    c = table.column(name)
    if pa.types.is_dictionary(c.type):
        c = c.cast(pa.string())
    return np.asarray(c.to_numpy(zero_copy_only=False) if hasattr(c, "to_numpy") else c.to_pylist())


def _ts_col(table: Any) -> np.ndarray:
    arr = np.asarray(table.column("ts").to_numpy())
    if np.issubdtype(arr.dtype, np.datetime64):
        return arr.astype("datetime64[s]").astype(np.int64)
    return arr.astype(np.int64)


def load_research_products(data_dir: Any = None) -> dict[str, Product]:
    """USD products from ``products.parquet`` (else ``products_raw.parquet``) as :class:`Product`.

    Exchange status flags are a snapshot of *today*, so they are not carried into the replay:
    every product is ``online`` (tradability in the past is decided by the presence of bars).
    Stablecoin / pegged flags are kept in ``Product.raw``. ``{}`` if no metadata file exists.
    """
    d = _data_dir(data_dir)
    path = next((p for p in (d / "products.parquet", d / "products_raw.parquet") if p.exists()), None)
    if path is None:
        return {}
    pq = _pq()
    rows = pq.read_table(path).to_pylist()
    out: dict[str, Product] = {}
    for r in rows:
        pid = str(r.get("product") or "")
        if not pid or (r.get("quote") or pid.partition("-")[2]) != "USD" or r.get("status") == "unlisted":
            continue

        def dec(key: str, default: str, r: Mapping[str, Any] = r) -> Decimal:
            v = r.get(f"{key}_str") or r.get(key)
            try:
                x = Decimal(str(v)) if v is not None and v == v else Decimal(default)
            except Exception:
                x = Decimal(default)
            return x if x.is_finite() and x > 0 else Decimal(default)

        base = str(r.get("base") or pid.partition("-")[0])
        out[pid] = Product(product_id=pid, base_currency=base, quote_currency="USD",
                           base_increment=dec("base_increment", "0.00000001"),
                           quote_increment=dec("quote_increment", "0.01"),
                           min_market_funds=dec("min_market_funds", "1"),
                           status="online", trading_disabled=False, post_only=False, limit_only=False,
                           cancel_only=False, display_name=f"{base}/USD",
                           raw={"is_stablecoin": bool(r.get("is_stablecoin") or False),
                                "fx_stablecoin": bool(r.get("fx_stablecoin") or False),
                                "is_pegged_derivative": bool(r.get("is_pegged_derivative") or False),
                                "status_now": r.get("status"), "base_name": r.get("base_name")})
    return out


def _available(data_dir: Path, granularity_s: int) -> list[str]:
    if granularity_s == 3600:
        return sorted(p.stem for p in (data_dir / "hourly").glob("*.parquet"))
    path = data_dir / "daily.parquet"
    if not path.exists():
        return []
    table = _pq().read_table(path, columns=["product"])
    return sorted({str(x) for x in _col(table, "product")})


def _load_spreads(data_dir: Path) -> dict[str, float]:
    path = data_dir / "book_snapshot.parquet"
    if not path.exists():
        return {}
    try:
        table = _pq().read_table(path, columns=["product", "spread_bps"])
    except Exception as e:  # a partial snapshot must not stop a backtest
        log.warning("book snapshot unreadable (%s); default slippage used", e)
        return {}
    prods = _col(table, "product")
    spreads = np.asarray(_col(table, "spread_bps"), dtype=np.float64)
    out: dict[str, float] = {}
    for pid in sorted(set(prods.tolist())):
        v = spreads[(prods == pid) & np.isfinite(spreads) & (spreads >= 0)]
        if len(v):
            out[str(pid)] = float(np.median(v))
    return out


def _load_bars(data_dir: Path, granularity_s: int, product_ids: Iterable[str], start_ts: int | None,
               end_ts: int | None) -> dict[str, BarSeries]:
    pq = _pq()
    ids = sorted(set(product_ids))
    tables: list[Any] = []
    cols = ["product", "ts", "open", "high", "low", "close", "volume"]
    if granularity_s == 3600:
        for pid in ids:
            path = data_dir / "hourly" / f"{pid}.parquet"
            if path.exists():
                tables.append(pq.read_table(path, columns=cols))
    elif granularity_s == DAY:
        path = data_dir / "daily.parquet"
        if not path.exists():
            raise SpotBacktestError(f"no daily candles at {path} (run research/coinbase/data/fetch.py)")
        if ids:
            tables.append(pq.read_table(path, columns=cols, filters=[("product", "in", ids)]))
    else:
        raise SpotBacktestError(f"unsupported bar granularity {granularity_s}s (use one of {GRANULARITIES})")
    out: dict[str, BarSeries] = {}
    for table in tables:
        if table.num_rows == 0:
            continue
        prods = _col(table, "product")
        ts = _ts_col(table)
        keep = np.ones(len(ts), dtype=bool)
        if start_ts is not None:
            keep &= ts >= start_ts
        if end_ts is not None:
            keep &= ts < end_ts
        arrays = {c: np.asarray(table.column(c).to_numpy(), dtype=np.float64) for c in cols[2:]}
        idx = np.nonzero(keep)[0]
        if not len(idx):
            continue
        uniq, inv = np.unique(prods[idx], return_inverse=True)
        order = np.argsort(inv, kind="stable")
        bounds = np.searchsorted(inv[order], np.arange(len(uniq) + 1))
        for j, pid in enumerate(uniq.tolist()):
            m = idx[order[bounds[j]:bounds[j + 1]]]
            out[str(pid)] = BarSeries(str(pid), granularity_s, ts[m], arrays["open"][m], arrays["high"][m],
                                      arrays["low"][m], arrays["close"][m], arrays["volume"][m])
    return out


def load_research_dataset(data_dir: Any = None, *, granularity_s: int = DAY,
                          product_ids: Iterable[str] | None = None, start_ts: int | None = None,
                          end_ts: int | None = None) -> SpotDataset:
    """Bars (``[start_ts, end_ts)`` on bar open) for ``product_ids`` (None = all USD products
    with data) + metadata + spreads from ``research/coinbase/data``."""
    d = _data_dir(data_dir)
    if not d.exists():
        raise SpotBacktestError(f"research data directory {d} does not exist")
    products = load_research_products(d)
    avail = set(_available(d, granularity_s))
    ids = sorted(avail if product_ids is None else avail & set(product_ids))
    bars = _load_bars(d, granularity_s, ids, start_ts, end_ts)
    usd = {pid: products.get(pid) or _default_product(pid) for pid in bars if pid.endswith("-USD")}
    return SpotDataset(granularity_s=granularity_s, bars={p: b for p, b in bars.items() if p in usd},
                       products=usd, spreads_bps=_load_spreads(d), source=str(d))


def _research_universe_products(data_dir: Path, granularity_s: int) -> dict[str, Product]:
    """Every USD product with bars of this granularity (metadata where known)."""
    meta = load_research_products(data_dir)
    return {pid: meta.get(pid) or _default_product(pid)
            for pid in _available(data_dir, granularity_s) if pid.endswith("-USD")}


# --------------------------------------------------------------------------- context


class BacktestSpotContext:
    """:class:`~kalshibot.coinbase.strategies.base.SpotContext` at one bar close of the replay.

    Only data from bars that closed by ``bar_end`` is reachable through this API.
    """

    __slots__ = ("__bars", "__portfolio", "__sim", "__t", "bar_end", "now", "params", "products")

    def __init__(self, sim: _SpotSim, t: int, products: Mapping[str, Product]) -> None:
        self.__sim = sim
        self.__bars = sim.ds.bars
        self.__t = t
        self.__portfolio: SpotPortfolioView | None = None
        self.bar_end = _dt(t)
        self.now = _dt(t + sim.bar_delay_s)
        self.products: Mapping[str, Product] = MappingProxyType(dict(products))
        self.params: Mapping[str, Any] = MappingProxyType(dict(sim.strategy.params))

    @property
    def portfolio(self) -> SpotPortfolioView:
        """This strategy's holdings marked at the closes known at ``bar_end`` (built on first use)."""
        if self.__portfolio is None:
            self.__portfolio = self.__sim.view(self.__t, self.products)
        return self.__portfolio

    def candles(self, product_id: str, n: int) -> list[Candle]:
        """The last ``n`` bars with ``end <= bar_end``, oldest first (a new list)."""
        bs = self.__bars.get(product_id)
        try:
            n = int(n)
        except (TypeError, ValueError):
            return []
        if bs is None or n <= 0:
            return []
        k = bs.n_closed(self.__t)
        return bs.candle_list()[max(0, k - n):k]

    def stats(self, product_id: str) -> Stats | None:
        bs = self.__bars.get(product_id)
        return bs.stats_at(self.__t) if bs is not None else None

    def log(self, msg: str, **data: Any) -> None:
        self.__sim.note_log(msg, data)


# --------------------------------------------------------------------------- simulator


class _PriceMap(Mapping[str, Decimal]):
    """Lazy ``{product_id: Decimal}`` of the closes known at ``t`` (``side`` -1: bid proxy =
    close x (1 - half-spread), 0: mid = close, +1: ask proxy) - computed only when read."""

    __slots__ = ("_cache", "_keys", "_side", "_sim", "_t")

    def __init__(self, sim: _SpotSim, t: int, keys: frozenset[str], side: int) -> None:
        self._sim, self._t, self._keys, self._side = sim, t, keys, side
        self._cache: dict[str, Decimal | None] = {}

    def _value(self, pid: str) -> Decimal | None:
        if pid not in self._cache:
            c = self._sim._close(pid, self._t) if pid in self._keys else None
            self._cache[pid] = None if c is None else _d(c * (1 + self._side * self._sim.slip_at(pid, self._t)))
        return self._cache[pid]

    def __getitem__(self, pid: str) -> Decimal:
        v = self._value(pid)
        if v is None:
            raise KeyError(pid)
        return v

    def __iter__(self) -> Iterator[str]:
        return (p for p in sorted(self._keys) if self._value(p) is not None)

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __contains__(self, pid: object) -> bool:
        return isinstance(pid, str) and self._value(pid) is not None


@dataclass
class _Pos:
    qty: Decimal = ZERO
    cost: Decimal = ZERO  # incl. buy fees
    realized: Decimal = ZERO
    fees: Decimal = ZERO
    opened_at: datetime | None = None


class _SpotSim:
    """One strategy over the grid: decide at bar close, fill at next open, mark at its close."""

    def __init__(self, strategy: SpotStrategy, ds: SpotDataset, *, name: str, universe: Sequence[str],
                 starting_balance: Decimal, tier: FeeTier, slip_bps: Mapping[str, float], default_slip_bps: float,
                 min_trade_usd: Decimal, allocation_pct: float, bar_delay_s: int, stale_after_s: int,
                 limits: Mapping[str, Any] | None, max_signals: int, slip_mode: str = "bps",
                 slip_multiplier: float = 1.0, max_participation: float = 0.0,
                 fill_price: str = "open") -> None:
        self.strategy = strategy
        self.ds = ds
        self.g = ds.granularity_s
        self.name = name
        self.universe = [p for p in dict.fromkeys(universe) if p in ds.bars]
        self._universe_key = tuple(self.universe)
        self.start_balance = starting_balance
        self.cash = starting_balance
        self.tier = tier
        self.slip_mode = slip_mode
        self.slip_mult = float(slip_multiplier)
        #: fixed one-way slippage (fraction) per product: measured ("spread") or everything ("bps")
        self.slip = {p: float(slip_bps[p]) / 1e4 * self.slip_mult for p in ds.bars if p in slip_bps}
        self.default_slip = float(default_slip_bps) / 1e4  # "bps": the fixed value; "spread": the floor
        self._slip_cache: dict[tuple[str, int], float] = {}
        self.max_participation = float(max_participation or 0.0)
        self.fill_price = fill_price
        self.taker_rate = float(tier.taker_rate)
        self.min_trade = min_trade_usd
        self.alloc_pct = float(allocation_pct)
        self.bar_delay_s = int(bar_delay_s)
        self.stale_after_s = int(stale_after_s)
        self.limits = dict(limits) if limits else None
        self.band = float(getattr(strategy, "rebalance_band", 0.02) or 0.0)
        self.pos: dict[str, _Pos] = {}
        self.fees = ZERO
        self.realized = ZERO
        #: (ts, equity, equity_mid, cash, invested) at every bar close
        self.curve: list[tuple[int, float, float, float, float]] = []
        self.trades: list[dict[str, Any]] = []
        self.signals: deque[dict[str, Any]] = deque(maxlen=max(0, int(max_signals)))
        self.stats: Counter[str] = Counter()
        self.skips: Counter[str] = Counter()
        self.problems: Counter[str] = Counter()
        self.logs: Counter[str] = Counter()
        self.log_samples: list[str] = []
        self.errors: list[str] = []
        self.traded_notional = 0.0
        self._t = 0

    # -- state views ----------------------------------------------------------------

    def slip_at(self, pid: str, t: int) -> float:
        """One-way slippage (fraction of price) for ``pid`` at ``t``: measured half-spread, else
        (``slippage="spread"``) the point-in-time liquidity estimate, else the fixed bps."""
        v = self.slip.get(pid)
        if v is not None:
            return v
        if self.slip_mode != "spread":
            return self.default_slip * self.slip_mult
        key = (pid, t // DAY)  # the 30-day volume barely moves within a day
        hit = self._slip_cache.get(key)
        if hit is None:
            hit = self._slip_cache[key] = fallback_slippage_bps(self.ds.bars.get(pid), t,
                                                                self.default_slip * 1e4) / 1e4 * self.slip_mult
        return hit

    def _close(self, pid: str, t: int) -> float | None:
        bs = self.ds.bars.get(pid)
        return bs.last_close(t) if bs is not None else None

    def live_products(self, t: int) -> dict[str, Product]:
        """Universe products with a bar closed by ``t`` whose last bar ended within ``stale_after_s``."""
        key = (self._universe_key, self.stale_after_s)
        hit = self.ds._live_cache.get(key)
        if hit is not None and hit[0] == t:  # simulators sharing a universe step in lockstep
            return dict(hit[1])
        out: dict[str, Product] = {}
        for pid in self.universe:
            bs = self.ds.bars[pid]
            k = bs.n_closed(t)
            if k and t - (bs._starts[k - 1] + self.g) <= self.stale_after_s:
                out[pid] = self.ds.products[pid]
        self.ds._live_cache[key] = (t, out)
        return dict(out)

    def _marks(self, t: int) -> tuple[float, float, float]:
        """(liquidation value net of the exit taker fee, mid value, cash) of the holdings at the
        closes known at ``t``."""
        liq = mid = 0.0
        for pid, p in self.pos.items():
            c = self._close(pid, t)
            if c is None or p.qty <= 0:
                continue
            q = float(p.qty)
            liq += q * c * (1 - self.slip_at(pid, t)) * (1 - self.taker_rate)  # net of the exit fee
            mid += q * c
        return liq, mid, float(self.cash)

    def view(self, t: int, live: Mapping[str, Product]) -> SpotPortfolioView:
        positions: list[SpotPosition] = []
        keys = frozenset(live) | frozenset(self.pos)
        mids = _PriceMap(self, t, keys, 0)
        bids = _PriceMap(self, t, keys, -1)
        asks = _PriceMap(self, t, keys, 1)
        liq_total = mid_total = cost_total = fee_total = ZERO
        ts = _dt(t)
        for pid in sorted(self.pos):
            p = self.pos[pid]
            if p.qty <= 0:
                continue
            bid, mp = bids.get(pid), mids.get(pid)
            gross = p.qty * bid if bid is not None else None
            exit_fee = gross * self.tier.taker_rate if gross is not None else None
            liq = gross - exit_fee if gross is not None and exit_fee is not None else None
            fee_total += exit_fee or ZERO
            mv = p.qty * mp if mp is not None else None
            liq_total += liq if liq is not None else p.cost
            mid_total += mv if mv is not None else p.cost
            cost_total += p.cost
            positions.append(SpotPosition(product_id=pid, strategy=self.name,
                                          base_currency=self.ds.products[pid].base_currency, quantity=p.qty,
                                          cost_basis=p.cost, realized_pnl=p.realized, fees_paid=p.fees,
                                          opened_at=p.opened_at, updated_at=ts, liquidation_value=liq,
                                          exit_fee=exit_fee, mid_value=mv, best_bid=bid, mid_price=mp,
                                          mark_ts=ts))
        equity = self.cash + liq_total
        alloc = (equity + fee_total) * _d(self.alloc_pct) / 100  # before exit fees (like the broker)
        pos_t = tuple(positions)
        return SpotPortfolioView(
            ts=ts, strategy=self.name, starting_balance=self.start_balance, cash=self.cash, reserved_cash=ZERO,
            equity=equity, equity_mid=self.cash + mid_total, realized_pnl=self.realized,
            unrealized_pnl=liq_total - cost_total, fees_paid=self.fees, day_start_equity=None,
            allocation_pct=self.alloc_pct, alloc_equity=max(ZERO, alloc), positions=pos_t, open_orders=(),
            all_positions=pos_t, all_open_orders=(), best_bids=bids, best_asks=asks, mids=mids)

    # -- logging -----------------------------------------------------------------------

    def note_log(self, msg: str, data: Mapping[str, Any]) -> None:
        self.logs[str(data.get("kind") or "log")] += 1
        if len(self.log_samples) < 20:
            self.log_samples.append(f"{_iso(self._t)} {msg}")

    def _error(self, msg: str) -> None:
        self.stats["strategy_errors"] += 1
        if len(self.errors) < 20:
            self.errors.append(f"{_iso(self._t)} {msg}")

    def _signal(self, t: int, intent: SpotOrderIntent, decision: str, why: str) -> None:
        self.stats[f"signals_{decision}"] += 1
        self.signals.append({
            "ts": _iso(t), "product_id": intent.product_id, "side": intent.side,
            "target_weight": _r(intent.target_weight), "quote_size": _r(intent.quote_size, 8),
            "base_size": _r(intent.base_size, 8), "expected_edge_bps": _r(intent.expected_edge_bps),
            "reason": intent.reason, "decision": decision, "decision_reason": why})

    # -- one bar -------------------------------------------------------------------------

    def _apply_limits(self, targets: list[TargetWeight], view: SpotPortfolioView) -> list[TargetWeight]:
        lim = self.limits or {}
        alloc = float(view.alloc_equity)
        equity = float(view.equity)
        if alloc <= 0:
            return targets
        cap_one = lim.get("max_position_pct_per_product")
        cap_all = lim.get("max_total_exposure_pct")
        reserve = lim.get("min_cash_reserve")
        if cap_one is not None:
            m = float(cap_one) / 100 * equity / alloc
            for tw in targets:
                tw.weight = min(tw.weight, m)
        total_cap = 1.0
        if cap_all is not None:
            total_cap = min(total_cap, float(cap_all) / 100 * equity / alloc)
        if reserve is not None:
            total_cap = min(total_cap, max(0.0, (equity - float(reserve)) / alloc))
        total = math.fsum(tw.weight for tw in targets)
        if total > total_cap > 0:
            for tw in targets:
                tw.weight *= total_cap / total
        elif total > 0 >= total_cap:
            for tw in targets:
                tw.weight = 0.0
        return targets

    def step(self, t: int) -> None:
        """Decide at bar close ``t``, fill at the open of the bars starting at ``t``, mark at their close."""
        self._t = t
        self.stats["bars"] += 1
        live = self.live_products(t)
        ctx = BacktestSpotContext(self, t, live)
        try:
            raw = self.strategy.on_bar(ctx)
        except Exception as e:  # a strategy error never stops the replay (like the engine)
            self._error(f"on_bar: {type(e).__name__}: {e}")
            raw = None
        targets, problems = normalize_targets(raw, live)
        for p in problems:
            self.problems[p[:100]] += 1
        if targets is not None:
            self.stats["decisions"] += 1
            view = ctx.portfolio
            if self.limits:
                targets = self._apply_limits(targets, view)
            plan_products = dict(live)
            for pid in self.pos:
                plan_products.setdefault(pid, self.ds.products[pid])
            # the engine's call: holdings, mids (= the last closes), allocation and cash from the view
            plan = plan_from_view(targets, view, plan_products, band=self.band, min_trade_usd=self.min_trade,
                                  strategy=self.name, fee_rate=self.tier.taker_rate)
            for s in plan.skipped:
                self.skips[s["reason"]] += 1
            filled = 0
            for intent in plan.intents:
                filled += self.execute(intent, t)
            if filled:
                self.stats["rebalances"] += 1
        liq, mid, cash = self._marks(t + self.g)
        self.curve.append((t + self.g, cash + liq, cash + mid, cash, liq))

    def start_point(self, t: int) -> None:
        liq, mid, cash = self._marks(t)
        self.curve.append((t, cash + liq, cash + mid, cash, liq))

    # -- execution --------------------------------------------------------------------------

    def execute(self, intent: SpotOrderIntent, t: int) -> int:
        """Fill ``intent`` at the open of the product's bar starting at ``t``. 1 if filled."""
        pid = intent.product_id
        bs = self.ds.bars.get(pid)
        i = bs.index_at(t) if bs is not None else None
        if bs is None or i is None:
            self._signal(t, intent, "unfilled", "no bar at the fill time (no trades)")
            return 0
        prod = self.ds.products[pid]
        open_ = float(bs.open[i])
        ref = open_
        if self.fill_price == "pessimistic":  # no zero-latency fill at the first print after bar_end
            vw = bs.vwap_proxy(i)
            ref = max(open_, vw) if intent.side == "buy" else min(open_, vw)
        slip = self.slip_at(pid, t)
        fallback = pid not in self.slip and self.slip_mode == "spread"
        rate = self.tier.taker_rate
        ts = _dt(t)
        # participation cap: at most max_participation of the USD that traded in the fill bar
        cap = (_d(self.max_participation * bs.bar_usd_volume(i))
               if self.max_participation > 0 and bs.has_volume else None)
        capped = False
        if intent.side == "sell":
            p = self.pos.get(pid)
            held = p.qty if p is not None else ZERO
            base = min(intent.base_size or ZERO, held)
            if base <= 0:
                self._signal(t, intent, "rejected", "nothing to sell (no shorting)")
                return 0
            price = _d(ref) * (1 - _d(slip))
            if cap is not None and base * price > cap:
                base = floor_to(cap / price, prod.base_increment)
                capped = True
                self.stats["capped_fills"] += 1
            notional = base * price
            if base <= 0 or notional < prod.min_market_funds:
                why = (f"bar volume too thin: {self.max_participation:.0%} of it is below min_market_funds"
                       if capped else f"notional below min_market_funds ${prod.min_market_funds}")
                self._signal(t, intent, "rejected", why)
                return 0
            fee = fee_for(notional, is_taker=True, tier=self.tier)
            cost_part = p.cost if base >= held else p.cost * base / held  # type: ignore[union-attr]
            realized = notional - fee - cost_part
            self.cash += notional - fee
            p.qty -= base  # type: ignore[union-attr]
            p.cost -= cost_part  # type: ignore[union-attr]
            p.realized += realized  # type: ignore[union-attr]
            p.fees += fee  # type: ignore[union-attr]
            if p.qty <= 0:  # type: ignore[union-attr]
                del self.pos[pid]
            self.realized += realized
        else:
            budget = min(intent.quote_size or ZERO, self.cash)
            if budget < prod.min_market_funds:
                self._signal(t, intent, "rejected", "insufficient cash" if (intent.quote_size or ZERO) > self.cash
                             else f"below min_market_funds ${prod.min_market_funds}")
                return 0
            price = _d(ref) * (1 + _d(slip))
            want = notional_for_budget(budget, is_taker=True, tier=self.tier)
            if cap is not None and want > cap:
                want = cap
                capped = True
                self.stats["capped_fills"] += 1
            base = floor_to(want / price, prod.base_increment)
            notional = base * price
            if base <= 0 or notional < prod.min_market_funds:
                why = (f"bar volume too thin: {self.max_participation:.0%} of it is below min_market_funds"
                       if capped else "size below the base increment / min_market_funds")
                self._signal(t, intent, "rejected", why)
                return 0
            fee = fee_for(notional, is_taker=True, tier=self.tier)
            self.cash -= notional + fee
            p = self.pos.get(pid)
            if p is None:
                p = self.pos[pid] = _Pos(opened_at=ts)
            p.qty += base
            p.cost += notional + fee
            p.fees += fee
            realized = None
        self.fees += fee
        self.traded_notional += float(notional)
        self.stats["buys" if intent.side == "buy" else "sells"] += 1
        if fallback:
            self.stats["fallback_slippage_fills"] += 1
        if realized is not None:
            self.stats["round_trips"] += 1
            if realized > 0:
                self.stats["wins"] += 1
        partial = capped or (intent.side == "sell" and intent.base_size is not None and base < intent.base_size)
        self._signal(t, intent, "partial" if partial else "executed",
                     f"capped at {self.max_participation:.0%} of the bar's USD volume" if capped else "")
        self.trades.append({
            "ts": _iso(t), "product_id": pid, "side": intent.side, "base_size": _r(base, 8),
            "price": _r(price, 8), "open_price": _r(open_, 8), "slippage_bps": _r(slip * 1e4, 3),
            "capped": capped,
            "notional": _r(notional, 6), "fee": _r(fee, 2), "fee_rate": _r(rate, 6), "is_taker": True,
            "realized_pnl": _r(realized, 6), "strategy": self.name, "reason": intent.reason,
            "target_weight": _r(intent.target_weight)})
        return 1

    def stuck_positions(self, end_t: int) -> list[dict[str, Any]]:
        out = []
        for pid, p in sorted(self.pos.items()):
            bs = self.ds.bars[pid]
            last = bs.last_end
            if last is not None and end_t - last > self.stale_after_s:
                out.append({"product_id": pid, "quantity": _r(p.qty, 8), "last_bar_end": _iso(last)})
        return out


# --------------------------------------------------------------------------- benchmarks


class _BuyHoldBTC(SpotStrategy):
    name = "benchmark_btc_buy_hold"
    description = "Buy BTC-USD with all cash at the first bar, then hold."
    history_bars = 1
    rebalance_band = 0.0
    backtestable = True

    def universe(self, products: Mapping[str, Product]) -> list[str]:
        return [BTC] if BTC in products else []

    def on_bar(self, ctx: Any) -> list[TargetWeight] | None:
        if BTC in ctx.products and ctx.portfolio.quantity(BTC) <= 0:
            return [TargetWeight(BTC, 1.0, "benchmark: buy and hold BTC")]
        return None


class _EqualWeight(SpotStrategy):
    name = "benchmark_equal_weight"
    description = "Equal weights over the live universe, rebalanced on the first bar of every month."
    history_bars = 1
    rebalance_band = 0.02
    backtestable = True
    members: ClassVar[list[str]] = []

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        super().__init__(params)
        self._month: tuple[int, int] | None = None

    def universe(self, products: Mapping[str, Product]) -> list[str]:
        return [p for p in self.members if p in products]

    def on_bar(self, ctx: Any) -> list[TargetWeight] | None:
        month = (ctx.bar_end.year, ctx.bar_end.month)
        live = sorted(ctx.products)
        if month == self._month or not live:
            return None
        self._month = month
        return [TargetWeight(p, 1.0 / len(live), "benchmark: equal weight") for p in live]


# --------------------------------------------------------------------------- metrics


_Point = tuple[int, float, float, float, float]  # (ts, equity, equity_mid, cash, invested)


def _daily_points(curve: Sequence[_Point]) -> list[_Point]:
    """The last point of each UTC day (a point at ``ts`` belongs to the day of ``ts - 1 s``,
    so the bar close at 00:00 counts for the day that just ended). The first point is kept."""
    if not curve:
        return []
    out: dict[date, _Point] = {}
    for p in curve[1:]:
        out[_dt(p[0] - 1).date()] = p
    first = curve[0]
    pts = [out[k] for k in sorted(out)]
    return [first, *[p for p in pts if p[0] > first[0]]]


def _period_key(ts: int, kind: str) -> str:
    d = _dt(ts - 1)
    return f"{d.year:04d}" if kind == "year" else f"{d.year:04d}-{d.month:02d}"


def _period_returns(points: Sequence[tuple[int, float]], kind: str) -> dict[str, tuple[float, float, float]]:
    """``{period: (start_equity, end_equity, max_drawdown_pct)}`` over consecutive periods."""
    out: dict[str, tuple[float, float, float]] = {}
    if len(points) < 2:
        return out
    prev_end = points[0][1]
    cur: str | None = None
    seg: list[float] = []
    start_eq = prev_end
    for ts, eq in points[1:]:
        k = _period_key(ts, kind)
        if k != cur:
            if cur is not None:
                out[cur] = (start_eq, seg[-1], drawdown([start_eq, *seg])[1])
                start_eq = seg[-1]
            cur, seg = k, []
        seg.append(eq)
    if cur is not None and seg:
        out[cur] = (start_eq, seg[-1], drawdown([start_eq, *seg])[1])
    return out


def compute_spot_metrics(curve: Sequence[tuple[int, float, float, float, float]], trades: Sequence[Mapping[str, Any]],
                         *, starting_balance: float, fees_paid: float, traded_notional: float,
                         stats: Mapping[str, int] | None = None) -> dict[str, Any]:
    """Performance metrics of one simulated account (see the module doc for the keys).

    Returns are daily (last point of each UTC day), annualized with 365 days (crypto trades
    every day); risk-free rate 0. Max drawdown uses every bar close.
    """
    stats = stats or {}
    eq = np.asarray([p[1] for p in curve], dtype=float)
    final = float(eq[-1]) if len(eq) else float(starting_balance)
    start = float(starting_balance)
    days = (curve[-1][0] - curve[0][0]) / DAY if len(curve) > 1 else 0.0
    years = days / YEAR_DAYS
    total_ret = final / start - 1 if start > 0 else None
    cagr = (final / start) ** (1 / years) - 1 if years > 0 and start > 0 and final > 0 else None
    daily = _daily_points(curve)
    de = np.asarray([p[1] for p in daily], dtype=float)
    vol = sharpe = sortino = None
    if len(de) >= 3 and np.all(de[:-1] > 0):
        r = de[1:] / de[:-1] - 1
        sd = float(r.std(ddof=1))
        mean = float(r.mean())
        vol = sd * math.sqrt(365)
        sharpe = mean / sd * math.sqrt(365) if sd > 0 else None
        downside = float(np.sqrt(np.mean(np.minimum(r, 0.0) ** 2)))
        sortino = mean / downside * math.sqrt(365) if downside > 0 else None
    dd_usd, dd_pct = drawdown(eq.tolist()) if len(eq) else (0.0, 0.0)
    inv = np.asarray([p[4] for p in curve], dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        expo = np.where(eq > 0, inv / eq, 0.0)
    avg_eq = float(eq.mean()) if len(eq) else start
    buys, sells = int(stats.get("buys", 0)), int(stats.get("sells", 0))
    rts, wins = int(stats.get("round_trips", 0)), int(stats.get("wins", 0))
    return {
        "total_return_pct": _r(total_ret * 100 if total_ret is not None else None, 4),
        "cagr_pct": _r(cagr * 100 if cagr is not None else None, 4),
        "vol_pct": _r(vol * 100 if vol is not None else None, 4),
        "sharpe": _r(sharpe, 4),
        "sortino": _r(sortino, 4),
        "max_drawdown_pct": _r(dd_pct, 4),
        "max_drawdown": _r(dd_usd, 4),
        "calmar": _r(cagr / (dd_pct / 100) if cagr is not None and dd_pct > 0 else None, 4),
        "turnover_per_year": _r(traded_notional / avg_eq / years if years > 0 and avg_eq > 0 else None, 4),
        "fees_paid": _r(fees_paid, 2),
        "fees": _r(fees_paid, 2),
        "fees_pct_of_start": _r(fees_paid / start * 100 if start > 0 else None, 4),
        "pct_time_invested": _r(float((expo > 0.01).mean()) * 100 if len(expo) else 0.0, 4),
        "avg_exposure_pct": _r(float(expo.mean()) * 100 if len(expo) else 0.0, 4),
        "trades": buys + sells,
        "n_trades": buys + sells,
        "buys": buys,
        "sells": sells,
        "round_trips": rts,
        "win_rate": _r(wins / rts if rts else None, 4),
        "final_equity": _r(final, 4),
        "total_pnl": _r(final - start, 4),
        "years": _r(years, 4),
        "bars": len(curve) - 1 if curve else 0,
    }


# --------------------------------------------------------------------------- entry point


def _resolve_strategy(strategy_cls: Any) -> type[SpotStrategy]:
    if isinstance(strategy_cls, str):
        from kalshibot.coinbase.strategies import REGISTRY

        if strategy_cls not in REGISTRY:
            known = ", ".join(sorted(REGISTRY)) or "(none)"
            raise ValueError(f"unknown coinbase strategy {strategy_cls!r}; known: {known}")
        return REGISTRY[strategy_cls]
    if isinstance(strategy_cls, type) and issubclass(strategy_cls, SpotStrategy):
        return strategy_cls
    raise TypeError(f"{strategy_cls!r} is not a SpotStrategy subclass or a registered name")


def _resolve_fee_tier(fee_tier: Any, settings: Any) -> FeeTier:
    if fee_tier is None:
        cb = getattr(settings, "coinbase", settings) if settings is not None else None
        tier_fn = getattr(cb, "tier", None)
        return tier_fn() if callable(tier_fn) else DEFAULT_TIER
    if isinstance(fee_tier, FeeTier):
        return fee_tier
    if isinstance(fee_tier, Mapping):
        return resolve_tier(None, fee_tier)
    return get_tier(str(fee_tier))


def fallback_slippage_bps(bars: BarSeries | None, t: int, floor_bps: float) -> float:
    """One-way slippage (bps) at ``t`` for a product without a spread snapshot: the fitted
    half-spread for its trailing 30-day USD volume (bars closed by ``t``), clamped to
    ``[floor_bps, FALLBACK_MAX_BPS]``. Bars without any volume data get ``floor_bps``."""
    if bars is None or not bars.has_volume:
        return floor_bps
    usd30 = bars.usd_volume_closed(t, 30 * DAY)
    if usd30 <= 0:
        return max(floor_bps, FALLBACK_MAX_BPS)
    a, b = FALLBACK_SPREAD_FIT
    est = 10 ** (a + b * math.log10(usd30))
    return max(floor_bps, min(FALLBACK_MAX_BPS, est))


def _slippage(slippage: Any, ds: SpotDataset) -> tuple[str, float, dict[str, float]]:
    """(mode, default bps, per-product bps) - one way, applied against us at the fill.
    ``spread``: the default is the floor of the fallback model (the widest measured half-spread,
    at least ``DEFAULT_SLIPPAGE_BPS``)."""
    if slippage is None or (isinstance(slippage, str) and slippage.strip().lower() == "spread"):
        measured = {p: s / 2 for p, s in ds.spreads_bps.items() if s >= 0}
        return "spread", max([DEFAULT_SLIPPAGE_BPS, *measured.values()]), measured
    try:
        bps = float(slippage)
    except (TypeError, ValueError):
        raise ValueError(f"slippage must be 'spread' or a number of bps (got {slippage!r})") from None
    if not math.isfinite(bps) or bps < 0 or bps >= 10_000:
        raise ValueError(f"slippage must be a non-negative number of bps below 10000 (got {slippage!r})")
    return "bps", bps, {}


def _opt(options: Mapping[str, Any], settings: Any) -> dict[str, Any]:
    opts = dict(DEFAULT_OPTIONS)
    cb = getattr(settings, "coinbase", settings) if settings is not None else None
    if cb is not None:
        risk = getattr(cb, "risk", None)
        eng = getattr(cb, "engine", None)
        min_trade, delay = getattr(risk, "min_trade_usd", None), getattr(eng, "bar_delay_s", None)
        if min_trade is not None:
            opts["min_trade_usd"] = min_trade
        if delay is not None:
            opts["bar_delay_s"] = delay
    unknown = set(options) - set(DEFAULT_OPTIONS)
    if unknown:
        raise TypeError(f"unknown backtest option(s): {', '.join(sorted(unknown))}")
    opts.update({k: v for k, v in options.items() if v is not None or k == "limits"})
    return opts


def _thin(idx: list[int], n: int) -> list[int]:
    if n <= 1 or len(idx) <= n:
        return idx
    step = (len(idx) - 1) / (n - 1)
    return sorted({idx[round(i * step)] for i in range(n)})


def run_spot_backtest(
    strategy_cls: type[SpotStrategy] | str,
    params: Mapping[str, Any] | None = None,
    *,
    start: Any = None,
    end: Any = None,
    starting_balance: Any = 1000,
    fee_tier: str | FeeTier | Mapping[str, Any] | None = None,
    slippage: str | float = "spread",
    data_dir: Any = None,
    dataset: SpotDataset | None = None,
    settings: Any = None,
    progress: Callable[[float], None] | None = None,
    **options: Any,
) -> dict[str, Any]:
    """Replay ``strategy_cls`` (a class or a registered name) - see the module docstring.

    ``start``/``end``: ``YYYY-MM-DD`` (end inclusive), ISO datetimes or unix seconds; default:
    ``history_bars`` bars after the universe's first bar (warm-up) .. the last complete bar.
    Raises ``ValueError`` (bad request: parameters, dates, strategy) or
    :class:`SpotBacktestError` (data missing / empty period).
    """
    wall = time.monotonic()
    opts = _opt(options, settings)
    cls = _resolve_strategy(strategy_cls)
    if not getattr(cls, "backtestable", True):
        raise ValueError(f"coinbase strategy {cls.name or cls.__name__!r} is not backtestable")
    resolved = cls.resolve_params(params, strict=True)  # ParamError is a ValueError
    strat = cls(resolved)
    g = int(getattr(strat, "bar_granularity_s", DAY))
    if g not in GRANULARITIES:
        raise ValueError(f"unsupported bar granularity {g}s (use one of {GRANULARITIES})")
    history = max(1, int(getattr(strat, "history_bars", 1)))
    tier = _resolve_fee_tier(fee_tier, settings)
    try:
        bal = _d(starting_balance) if isinstance(starting_balance, float) else Decimal(str(starting_balance))
    except (ArithmeticError, ValueError, TypeError):
        raise ValueError(f"starting_balance must be a number (got {starting_balance!r})") from None
    if not bal.is_finite() or bal <= 0:
        raise ValueError(f"starting_balance must be positive (got {starting_balance!r})")
    s_req = _parse_when(start)
    e_req = _parse_when(end, end=True)
    if s_req is not None and e_req is not None and e_req <= s_req:
        raise ValueError("end must be after start")

    # -- data -------------------------------------------------------------------------
    if dataset is not None:
        if dataset.granularity_s != g:
            raise SpotBacktestError(f"dataset has {dataset.granularity_s}s bars; the strategy needs {g}s")
        all_products = {p: dataset.products[p] for p in dataset.bars if len(dataset.bars[p])}
        universe = [p for p in strat.universe(MappingProxyType(dict(all_products))) if p in all_products]
        ds = dataset
    else:
        ddir = _data_dir(data_dir)
        if not ddir.exists():
            raise SpotBacktestError(f"research data directory {ddir} does not exist")
        all_products = _research_universe_products(ddir, g)
        if not all_products:
            raise SpotBacktestError(f"no {'hourly' if g == 3600 else 'daily'} candles in {ddir}")
        universe = [p for p in strat.universe(MappingProxyType(dict(all_products))) if p in all_products]
        margin = (history + 2) * g
        ds = load_research_dataset(ddir, granularity_s=g, product_ids=[*universe, BTC],
                                   start_ts=None if s_req is None else s_req - margin, end_ts=e_req)
        universe = [p for p in universe if p in ds.bars]
    universe = list(dict.fromkeys(universe))
    if not universe:
        raise SpotBacktestError(f"strategy {cls.name!r}: its universe has no products with data")

    firsts = [ds.bars[p]._starts[0] for p in universe if len(ds.bars[p])]
    lasts = [ds.bars[p]._starts[-1] + g for p in universe if len(ds.bars[p])]
    if not firsts:
        raise SpotBacktestError("the universe has no bars")
    s_ts = s_req if s_req is not None else min(firsts) + history * g
    e_ts = e_req if e_req is not None else max(lasts)
    e_ts = min(e_ts, max(lasts))
    s_ts = -(-s_ts // g) * g  # align to the bar grid
    e_ts = e_ts // g * g
    grid = list(range(s_ts, e_ts - g + 1, g))
    if not grid:
        raise SpotBacktestError(f"empty period: universe data covers {_iso(min(firsts))} .. {_iso(max(lasts))}"
                                f" (requested {_iso(s_req)} .. {_iso(e_req)})")
    mode, default_bps, per_product = _slippage(slippage, ds)
    try:
        mult = float(opts["slippage_multiplier"])
        part = float(opts["max_participation"] or 0.0)
    except (TypeError, ValueError):
        raise ValueError("slippage_multiplier and max_participation must be numbers") from None
    if not math.isfinite(mult) or mult < 0:
        raise ValueError(f"slippage_multiplier must be >= 0 (got {opts['slippage_multiplier']!r})")
    if not math.isfinite(part) or part < 0 or part > 1:
        raise ValueError(f"max_participation must be between 0 and 1 (got {opts['max_participation']!r})")
    fill_price = str(opts["fill_price"] or "open").strip().lower()
    if fill_price not in ("open", "pessimistic"):
        raise ValueError(f"fill_price must be 'open' or 'pessimistic' (got {opts['fill_price']!r})")
    stale = int(opts["stale_after_s"]) if opts["stale_after_s"] is not None else (3 * DAY if g == DAY else DAY)
    common: dict[str, Any] = {
        "starting_balance": bal, "tier": tier, "slip_bps": per_product, "default_slip_bps": default_bps,
        "min_trade_usd": Decimal(str(opts["min_trade_usd"])), "bar_delay_s": int(opts["bar_delay_s"]),
        "stale_after_s": stale, "max_signals": int(opts["max_signals"]), "slip_mode": mode,
        "slip_multiplier": mult, "max_participation": part, "fill_price": fill_price,
    }

    sims: dict[str, _SpotSim] = {"strategy": _SpotSim(
        strat, ds, name=cls.name, universe=universe, allocation_pct=float(opts["allocation_pct"]),
        limits=opts["limits"], **common)}
    # benchmarks: no signals kept, and no $ minimum trade (only min_market_funds), so an equal
    # weight over hundreds of products is not wiped out by the dust rule of a small account
    bench_common = {**common, "max_signals": 0, "min_trade_usd": ZERO}
    if opts["benchmarks"]:
        if BTC in ds.bars and len(ds.bars[BTC]):
            sims["btc"] = _SpotSim(_BuyHoldBTC(), ds, name=_BuyHoldBTC.name, universe=[BTC],
                                   allocation_pct=100.0, limits=None, **bench_common)
        ew_cls = type("_EqualWeightRun", (_EqualWeight,), {"members": list(universe)})
        ew = ew_cls()
        sims["equal_weight"] = _SpotSim(ew, ds, name=_EqualWeight.name, universe=ew.universe(all_products),
                                        allocation_pct=100.0, limits=None, **bench_common)

    n = len(grid)
    every = max(1, n // 100)
    for sim in sims.values():
        sim.start_point(grid[0])
    for k, t in enumerate(grid):
        for sim in sims.values():
            sim.step(t)
        if progress is not None and k % every == 0:
            try:
                progress(k / n)
            except Exception:
                pass

    # -- results -------------------------------------------------------------------------
    main = sims["strategy"]
    metrics = compute_spot_metrics(main.curve, main.trades, starting_balance=float(bal), fees_paid=float(main.fees),
                                   traded_notional=main.traded_notional, stats=main.stats)
    bench_metrics: dict[str, Any] = {"btc": None, "equal_weight": None}
    for key in ("btc", "equal_weight"):
        b = sims.get(key)
        if b is not None:
            bench_metrics[key] = compute_spot_metrics(b.curve, b.trades, starting_balance=float(bal),
                                                      fees_paid=float(b.fees), traded_notional=b.traded_notional,
                                                      stats=b.stats)
    for key in ("btc", "equal_weight"):
        bm = bench_metrics[key]
        tr, br = metrics["total_return_pct"], bm["total_return_pct"] if bm else None
        metrics[f"excess_return_vs_{key}_pct"] = _r(tr - br, 4) if tr is not None and br is not None else None
    metrics["benchmarks"] = bench_metrics

    # equity curve: daily points, thinned
    daily_idx: dict[date, int] = {}
    for i, p in enumerate(main.curve[1:], start=1):
        daily_idx[_dt(p[0] - 1).date()] = i
    idx = [0, *[i for i in sorted(daily_idx.values())]]
    keep = _thin(idx, int(opts["max_points"]))
    eq_all = np.asarray([p[1] for p in main.curve], dtype=float)
    peak = np.maximum.accumulate(eq_all)
    curve_out = []
    for i in keep:
        ts, e, em, c, inv = main.curve[i]
        curve_out.append({"ts": _iso(ts), "equity": _r(e, 4), "equity_mid": _r(em, 4), "cash": _r(c, 4),
                          "invested": _r(inv, 4), "exposure_pct": _r(inv / e * 100 if e > 0 else 0.0, 4),
                          "drawdown_pct": _r((peak[i] - e) / peak[i] * 100 if peak[i] > 0 else 0.0, 4)})
    benchmarks_out: dict[str, list[dict[str, Any]]] = {}
    for key in ("btc", "equal_weight"):
        b = sims.get(key)
        benchmarks_out[key] = ([{"ts": _iso(b.curve[i][0]), "equity": _r(b.curve[i][1], 4)} for i in keep]
                               if b is not None else [])

    # by year / by month (daily points)
    def pts(sim: _SpotSim | None) -> list[tuple[int, float]]:
        return [(p[0], p[1]) for p in _daily_points(sim.curve)] if sim is not None else []

    main_pts = pts(main)
    bench_periods = {key: {kind: _period_returns(pts(sims.get(key)), kind) for kind in ("year", "month")}
                     for key in ("btc", "equal_weight")}
    trade_by: dict[str, Counter[str]] = {"year": Counter(), "month": Counter()}
    fee_by: dict[str, dict[str, float]] = {"year": {}, "month": {}}
    for tr in main.trades:
        ts = _to_ts(tr["ts"])
        for kind in ("year", "month"):
            # a fill at bar open T belongs to the period of T (not of the bar close before it)
            pk = _period_key(ts + 1, kind)
            trade_by[kind][pk] += 1
            fee_by[kind][pk] = fee_by[kind].get(pk, 0.0) + float(tr["fee"] or 0.0)

    def rows(kind: str) -> list[dict[str, Any]]:
        out = []
        for k, (s0, s1, dd) in _period_returns(main_pts, kind).items():
            row: dict[str, Any] = {kind: k, "return_pct": _r((s1 / s0 - 1) * 100 if s0 > 0 else None, 4),
                                   "pnl": _r(s1 - s0, 4)}
            if kind == "year":
                row.update({"start_equity": _r(s0, 4), "end_equity": _r(s1, 4), "max_drawdown_pct": _r(dd, 4)})
            row.update({"trades": trade_by[kind].get(k, 0), "fees": _r(fee_by[kind].get(k, 0.0), 2)})
            for key in ("btc", "equal_weight"):
                bp = bench_periods[key][kind].get(k)
                row[f"{key}_return_pct"] = _r((bp[1] / bp[0] - 1) * 100 if bp and bp[0] > 0 else None, 4)
            out.append(row)
        return out

    by_year, by_month = rows("year"), rows("month")
    trades = main.trades
    if len(trades) > int(opts["max_trades"]):
        metrics["trades_total"] = len(trades)
        metrics["trades_truncated"] = True
        trades = trades[-int(opts["max_trades"]):]
    st = main.stats
    metrics["details"] = {
        "strategy": cls.name,
        "params": dict(strat.params),
        "period": {"start": _iso(grid[0]), "end": _iso(grid[-1] + g)},
        "granularity_s": g,
        "universe": universe,
        "fee_tier": tier.as_dict(),
        "slippage": {"mode": mode, "default_bps": default_bps,
                     "by_product_bps": {p: _r(v, 3) for p, v in sorted(per_product.items()) if p in universe},
                     "multiplier": mult,
                     # products without a snapshot: point-in-time estimate from 30-day USD volume
                     "floor_bps": default_bps if mode == "spread" else None,
                     "fallback_max_bps": FALLBACK_MAX_BPS if mode == "spread" else None,
                     "fallback_model": ("half_spread_bps = 10^(%g %+g x log10(usd_volume_30d))"
                                        % FALLBACK_SPREAD_FIT) if mode == "spread" else None,
                     "fallback_fills": st["fallback_slippage_fills"],
                     "fallback_products": sorted(p for p in universe if p not in per_product)
                     if mode == "spread" else []},
        "participation": {"max_participation": part, "capped_fills": st["capped_fills"]},
        "fill_price": fill_price,
        "options": {"min_trade_usd": float(opts["min_trade_usd"]), "allocation_pct": float(opts["allocation_pct"]),
                    "limits": dict(opts["limits"]) if opts["limits"] else None, "bar_delay_s": int(opts["bar_delay_s"]),
                    "stale_after_s": stale, "rebalance_band": main.band,
                    "execution": "taker (next bar open)", "strategy_execution": getattr(strat, "execution", "taker")},
        "dataset": ds.describe(),
        "decisions": st["decisions"],
        "rebalances": st["rebalances"],
        "signals": {k.removeprefix("signals_"): v for k, v in sorted(st.items()) if k.startswith("signals_")},
        "skip_reasons": dict(main.skips.most_common(15)),
        "target_problems": dict(main.problems.most_common(15)),
        "strategy_errors": st["strategy_errors"],
        "errors": main.errors,
        "strategy_logs": dict(main.logs.most_common(20)),
        "strategy_log_samples": main.log_samples,
        "stuck_positions": main.stuck_positions(grid[-1] + g),
        "look_ahead": "decisions at each bar close see only bars with end <= bar_end; fills at the next "
                      "bar's open +/- half-spread + taker fee" + (
                          " with zero latency (the open is the first print at bar_end; live trades no "
                          "earlier than bar_end + bar_delay_s)" if fill_price == "open" else
                          " at the worse of the open and (O+H+L+C)/4 (latency-pessimistic)"),
        "known_biases": KNOWN_BIASES,
        "elapsed_s": round(time.monotonic() - wall, 2),
    }
    return {
        "venue": "coinbase",
        "strategy": cls.name,
        "params": dict(strat.params),
        "start": _iso(grid[0]),
        "end": _iso(grid[-1] + g),
        "starting_balance": float(bal),
        "granularity_s": g,
        "metrics": metrics,
        "equity_curve": curve_out,
        "benchmarks": benchmarks_out,
        "trades": trades,
        "by_year": by_year,
        "by_month": by_month,
        "signals": list(main.signals),
    }
