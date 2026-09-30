"""Historical data for the backtester (ARCHITECTURE.md §10): market timelines + candle quotes.

Both sources are read in place from ``research/`` (nothing is copied):

``hourly`` (Adapter A) - ``research/data``: settled markets (``markets.parquet``) with hourly
    candles (``candles_hourly.parquet``), plus the calibration study's fill candles
    (``research/calibration/candles_fill_hourly.parquet``) and the **archived-era holdout**
    candles (``research/calibration/verify_leakage/candles_hist_hourly.parquet``: markets settled
    2026-05-20 .. 07-27, the independent out-of-sample period that refuted the ladder rule; the
    main candle files cover almost none of it, so without them every ladder backtest would only
    replay the window the rule was selected on). Needs **pyarrow** (a project dependency since
    2026-09-27; ``uv sync`` installs it). Replays on the UTC hour grid.
``minute`` (Adapter B) - ``research/crypto_fv``: 1-minute candles and markets of short-dated crypto
    series (``data/{candles,markets}_<SERIES>.csv.gz``, plus the untouched holdout fetches in
    ``verify_stats/out/hist_*`` and ``verify_leakage/data_holdout``) and the Coinbase spot file
    (``data/spot_BTC-USD.csv``). Plain CSV (no extra dependency). Replays on the UTC minute grid.

No look-ahead, by construction (:class:`ReplayMarketData`):

* A market is **active** at ``now`` iff ``open_time <= now < close_time`` (the actual close: you
  can see that a market is still open) and at least one candle has ended by ``now``. Its quote is
  the close of the **last candle whose period ended at or before** ``now`` (candles are sparse:
  a missing period means no change, so forward-filling is exact). A YES bid of 0 / ask of 1 means
  "no quote on that side".
* After the actual close it is ``closed`` (no book, no result); from ``settlement_ts`` on it is
  ``finalized`` with the recorded result. ``result``, ``settlement_value``, ``expiration_value``
  and ``settlement_ts`` are never visible before that.
* ``close_time`` while active: the dataset stores the **actual** close, which for
  ``can_close_early`` markets can be an early close caused by the outcome. The scheduled close is
  not recorded, so a market that closed more than an hour before its
  ``expected_expiration_time`` shows the EET as its close time while active and simply
  disappears (status ``closed``) at its actual close; other markets show their actual close
  (their scheduled close, e.g. 5 minutes before the EET). Strategies and universe filters never
  see an early close before it happens.
* The order book is synthetic: one level per side at the candle's bid/ask with ``book_size``
  contracts (configurable; candles carry no depth).

The ``research`` universe (hourly default) is the one the calibration study validated
(``research/calibration/calib_lib.py``): markets not in an outcome-timing-dependent series
(``series_flags.csv``), expected expiration before the end of the data, and every market of the
event has candles (``event_all_candles``; avoids the candle-selection bias described in
``research/data/README.md``). For the archived era a market whose candles were fetched but came
back empty (``verify_leakage/hist_fetched_tickers.parquet``) counts as covered, as in the
holdout evaluation (``verify_leakage/eval_hist.py``); only those events are complete.
"""

from __future__ import annotations

import bisect
import csv
import dataclasses
import gzip
import json
import logging
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from kalshibot.fees import resolve_fee_params
from kalshibot.kalshi.client import KalshiNotFound
from kalshibot.kalshi.models import Level, Market, Orderbook, Series, Trade
from kalshibot.money import ONE, ZERO, D
from kalshibot.paper.sim import StaticMarketData
from kalshibot.strategies.base import UniverseSpec

__all__ = [
    "RESEARCH_DIR",
    "BacktestDataError",
    "ReplayDataset",
    "ReplayMarketData",
    "load_dataset",
    "load_hourly",
    "load_minute",
    "minute_series_available",
]

log = logging.getLogger(__name__)

#: ``research/`` next to the package (the repo layout); override with the ``data_dir`` option.
RESEARCH_DIR = Path(__file__).resolve().parents[2] / "research"

_NO_TS = -1
#: an actual close more than this before the expected expiration counts as an early close
EARLY_CLOSE_GAP_S = 3600
_FAR = np.int64(2**62)
_SHIFT = np.int64(2**32)  # candle key = market index << 32 | end_ts (epoch seconds < 2**32)

TAPERED = [{"start": "0.0000", "end": "0.1000", "step": "0.0010"},
           {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
           {"start": "0.9000", "end": "1.0000", "step": "0.0010"}]
PRICE_LEVEL_STRUCTURES: dict[str, list[dict[str, str]]] = {
    "linear_cent": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}],
    "tapered_deci_cent": TAPERED,
    "deci_cent": [{"start": "0.0000", "end": "1.0000", "step": "0.0010"}],
}
#: spot product file per short-dated crypto series (research/crypto_fv/data/spot_<product>.csv)
SPOT_PRODUCTS = {"KXBTC15M": ("BTC", "BTC-USD"), "KXETH15M": ("ETH", "ETH-USD"), "KXSOL15M": ("SOL", "SOL-USD"),
                 "KXXRP15M": ("XRP", "XRP-USD"), "KXDOGE15M": ("DOGE", "DOGE-USD")}


class BacktestDataError(RuntimeError):
    """The historical data is missing or unreadable (the message says what to do)."""


# --------------------------------------------------------------------------- columns


class _StrCol:
    """Dictionary-encoded string column (memory-light for 10^5..10^6 rows)."""

    __slots__ = ("codes", "values")

    def __init__(self, codes: np.ndarray, values: Sequence[str | None]) -> None:
        self.codes = codes
        self.values = list(values)

    @classmethod
    def from_list(cls, xs: Iterable[Any]) -> _StrCol:
        lookup: dict[Any, int] = {}
        vals: list[str | None] = []
        codes = []
        for x in xs:
            v = None if x is None or (isinstance(x, float) and math.isnan(x)) else str(x)
            c = lookup.get(v)
            if c is None:
                c = lookup[v] = len(vals)
                vals.append(v)
            codes.append(c)
        return cls(np.asarray(codes, dtype=np.int32), vals)

    def take(self, idx: np.ndarray) -> _StrCol:
        return _StrCol(self.codes[idx], self.values)

    def __getitem__(self, i: int) -> str | None:
        return self.values[self.codes[i]]

    def __len__(self) -> int:
        return len(self.codes)


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _epoch(value: Any) -> int:
    """ISO string / epoch number / datetime -> epoch seconds (``-1`` when unset)."""
    if value is None or value == "":
        return _NO_TS
    if isinstance(value, datetime):
        return int((value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp())
    if isinstance(value, int | float) and not isinstance(value, bool):
        return int(value) if math.isfinite(value) and value > 0 else _NO_TS
    s = str(value).strip()
    try:
        return int(float(s))
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return _NO_TS
    return int((dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp())


# --------------------------------------------------------------------------- dataset


class ReplayDataset:
    """Immutable market timelines + candle quotes (shared read-only by concurrent runs).

    Per market ``i``: ``tickers[i]``, ``open_ts``/``close_ts`` (actual close)/``shown_close_ts``
    (the close time shown while active, see the module doc)/``settle_ts``/``eet_ts`` (epoch s),
    ``result`` and the static API fields (:meth:`api`). Candles are stored sorted by
    ``(market, end)``: rows ``cand_start[i]:cand_stop[i]`` belong to market ``i``.
    """

    def __init__(
        self,
        *,
        kind: str,
        step_s: int,
        default_fill: str,
        cols: Mapping[str, Any],
        candle_idx: np.ndarray,
        candle_end: np.ndarray,
        candle_bid: np.ndarray,
        candle_ask: np.ndarray,
        series_info: Mapping[str, Series] | None = None,
        feeds: Callable[[Callable[[], datetime]], Any] | None = None,
        info: Mapping[str, Any] | None = None,
    ) -> None:
        self.kind = kind
        self.step_s = int(step_s)
        self.default_fill = default_fill
        self.cols = dict(cols)
        self.tickers: list[str] = list(cols["ticker"])
        self.n = len(self.tickers)
        self.index = {t: i for i, t in enumerate(self.tickers)}
        self.open_ts = np.asarray(cols["open_ts"], dtype=np.int64)
        self.close_ts = np.asarray(cols["close_ts"], dtype=np.int64)
        self.eet_ts = np.asarray(cols.get("eet_ts", np.full(self.n, _NO_TS)), dtype=np.int64)
        settle = np.asarray(cols["settle_ts"], dtype=np.int64)
        self.settle_ts = np.where(settle > 0, np.maximum(settle, self.close_ts), self.close_ts + 60)
        early = np.asarray(cols.get("can_close_early", np.zeros(self.n, dtype=bool)), dtype=bool)
        closed_early = early & (self.eet_ts > 0) & (self.close_ts < self.eet_ts - EARLY_CLOSE_GAP_S)
        self.shown_close_ts = np.where(closed_early, self.eet_ts, self.close_ts).astype(np.int64)
        self.result = cols["result"]
        self.event_of = cols["event_ticker"]
        self.series_of = cols["series_ticker"]
        self.series_info: dict[str, Series] = dict(series_info or {})
        self.make_feeds = feeds
        self.info: dict[str, Any] = dict(info or {})

        # candles: sort by (market, end), keep the first row of duplicate (market, end) keys
        idx = np.asarray(candle_idx, dtype=np.int64)
        end = np.asarray(candle_end, dtype=np.int64)
        key = idx * _SHIFT + end
        _, first = np.unique(key, return_index=True)  # sorted unique keys, first occurrence
        self.cand_key = key[first]
        self.cand_end = end[first]
        self.cand_bid = np.round(np.asarray(candle_bid, dtype=np.float64)[first], 4)
        self.cand_ask = np.round(np.asarray(candle_ask, dtype=np.float64)[first], 4)
        cidx = idx[first]
        self.cand_start = np.searchsorted(cidx, np.arange(self.n), side="left").astype(np.int64)
        self.cand_stop = np.searchsorted(cidx, np.arange(self.n), side="right").astype(np.int64)
        has = self.cand_stop > self.cand_start
        self.first_end = np.where(has, self.cand_end[np.minimum(self.cand_start, max(len(self.cand_end) - 1, 0))],
                                  _FAR) if len(self.cand_end) else np.full(self.n, _FAR, dtype=np.int64)

        # universe lookups: by shown close time, overall and per series
        order = np.argsort(self.shown_close_ts, kind="stable")
        self.by_close_idx = order
        self.by_close_ts = self.shown_close_ts[order]
        self.series_markets: dict[str, tuple[np.ndarray, np.ndarray, int]] = {}
        ser_codes = self.series_of.codes if isinstance(self.series_of, _StrCol) else None
        if ser_codes is not None:
            for code, name in enumerate(self.series_of.values):
                sel = order[ser_codes[order] == code]
                if len(sel) and name:
                    life = int((self.shown_close_ts[sel] - self.open_ts[sel]).max())
                    self.series_markets[name] = (sel, self.shown_close_ts[sel], max(life, 0))
        live = has & (self.open_ts > 0)
        self.first_ts = int(self.first_end[live].min()) if live.any() else 0
        self.last_ts = int(self.close_ts[live].max()) if live.any() else 0
        self._price_ranges: dict[str | None, Any] = {}

    # -- construction helpers ----------------------------------------------------------

    @classmethod
    def from_records(
        cls,
        markets: Iterable[Mapping[str, Any]],
        candles: Mapping[str, Iterable[Sequence[Any]]],
        *,
        kind: str = "hourly",
        step_s: int | None = None,
        default_fill: str | None = None,
        series: Iterable[Series | Mapping[str, Any]] = (),
        feeds: Callable[[Callable[[], datetime]], Any] | None = None,
        info: Mapping[str, Any] | None = None,
    ) -> ReplayDataset:
        """Build a dataset from plain rows (tests, small ad-hoc replays).

        ``markets``: dicts with ``ticker``, ``event_ticker``, ``series_ticker``, ``open_ts``,
        ``close_ts``, ``settle_ts``, ``result`` (``yes``/``no``) and optionally ``eet_ts``,
        ``can_close_early``, ``category``, ``fee_type``, ``fee_multiplier``, ``title``,
        ``strike_type``, ``floor_strike``, ``cap_strike``, ``expiration_value``,
        ``price_level_structure``, ``price_ranges`` (list of ``{start, end, step}``),
        ``settlement_value``. Times are epoch seconds / ISO strings / datetimes.
        ``candles``: ``{ticker: [(end_ts, yes_bid, yes_ask), ...]}`` (bid 0 / ask 1 = no quote).
        """
        rows = list(markets)
        tick = [str(r["ticker"]) for r in rows]
        cols: dict[str, Any] = {
            "ticker": tick,
            "event_ticker": _StrCol.from_list(r.get("event_ticker") or t.rsplit("-", 1)[0] for r, t in zip(rows, tick)),
            "series_ticker": _StrCol.from_list(r.get("series_ticker") or str(r.get("event_ticker") or t).split("-")[0]
                                               for r, t in zip(rows, tick)),
            "open_ts": [_epoch(r.get("open_ts", r.get("open_time"))) for r in rows],
            "close_ts": [_epoch(r.get("close_ts", r.get("close_time"))) for r in rows],
            "settle_ts": [_epoch(r.get("settle_ts", r.get("settlement_ts"))) for r in rows],
            "eet_ts": [_epoch(r.get("eet_ts", r.get("expected_expiration_time"))) for r in rows],
            "can_close_early": [bool(r.get("can_close_early", False)) for r in rows],
            "result": _StrCol.from_list(str(r.get("result") or "") for r in rows),
        }
        for k in ("title", "strike_type", "expiration_value", "price_level_structure", "category"):
            cols[k] = _StrCol.from_list(r.get(k) for r in rows)
        for k in ("floor_strike", "cap_strike", "settlement_value"):
            cols[k] = np.asarray([_num(r.get(k)) if r.get(k) not in (None, "") else math.nan for r in rows],
                                 dtype=np.float64)
        cols["price_ranges"] = _StrCol.from_list(json.dumps(r["price_ranges"]) if r.get("price_ranges") else None
                                                 for r in rows)
        index = {t: i for i, t in enumerate(tick)}
        ci: list[int] = []
        ce: list[int] = []
        cb: list[float] = []
        ca: list[float] = []
        for t, cs in candles.items():
            i = index.get(t)
            if i is None:
                continue
            for c in cs:
                ci.append(i)
                ce.append(_epoch(c[0]))
                cb.append(float(c[1]))
                ca.append(float(c[2]))
        info_series: dict[str, Series] = {}
        for s in series:
            obj = s if isinstance(s, Series) else Series.from_api(dict(s))
            info_series[obj.ticker] = obj
        for r in rows:  # series fee/category from the market rows when not given explicitly
            st = str(r.get("series_ticker") or str(r.get("event_ticker") or r["ticker"]).split("-")[0])
            if st not in info_series:
                info_series[st] = Series.from_api({
                    "ticker": st, "title": st, "category": r.get("category") or "",
                    "fee_type": r.get("fee_type") or "quadratic",
                    "fee_multiplier": r.get("fee_multiplier") if r.get("fee_multiplier") is not None else 1})
        step = step_s or (60 if kind == "minute" else 3600)
        fill = default_fill or ("next_ask" if kind == "minute" else "same")
        return cls(kind=kind, step_s=step, default_fill=fill, cols=cols, candle_idx=np.asarray(ci, dtype=np.int64),
                   candle_end=np.asarray(ce, dtype=np.int64), candle_bid=np.asarray(cb, dtype=np.float64),
                   candle_ask=np.asarray(ca, dtype=np.float64), series_info=info_series, feeds=feeds, info=info)

    # -- per-market static fields ---------------------------------------------------------

    def _col(self, name: str, i: int) -> Any:
        c = self.cols.get(name)
        if c is None:
            return None
        v = c[i]
        if isinstance(v, float | np.floating):
            return None if math.isnan(v) else float(v)
        if isinstance(v, np.generic):
            return v.item()
        return v

    def price_ranges(self, i: int) -> Any:
        raw = self._col("price_ranges", i)
        if raw not in self._price_ranges:
            if raw:
                try:
                    self._price_ranges[raw] = json.loads(raw)
                except ValueError:
                    self._price_ranges[raw] = None
            else:
                self._price_ranges[raw] = None
        pr = self._price_ranges[raw]
        if pr:
            return pr
        return PRICE_LEVEL_STRUCTURES.get(self._col("price_level_structure", i) or "",
                                          PRICE_LEVEL_STRUCTURES["linear_cent"])

    def api(self, i: int) -> dict[str, Any]:
        """Static, outcome-free API fields of market ``i`` (status ``active``, no quotes)."""
        eet = int(self.eet_ts[i])
        d: dict[str, Any] = {
            "ticker": self.tickers[i],
            "event_ticker": self.event_of[i],
            "series_ticker": self.series_of[i],
            "title": self._col("title", i) or self.tickers[i],
            "status": "active",
            "market_type": "binary",
            "open_time": int(self.open_ts[i]) if self.open_ts[i] > 0 else None,
            "close_time": int(self.shown_close_ts[i]),
            "expected_expiration_time": eet if eet > 0 else None,
            "can_close_early": bool(self.cols["can_close_early"][i]) if "can_close_early" in self.cols else False,
            "price_level_structure": self._col("price_level_structure", i) or "",
            "price_ranges": self.price_ranges(i),
            "strike_type": self._col("strike_type", i) or "",
            "floor_strike": self._col("floor_strike", i),
            "cap_strike": self._col("cap_strike", i),
            "exchange_index": 0,
        }
        return d

    def settlement_value(self, i: int) -> Decimal | None:
        r = self.result[i]
        if r == "yes":
            return ONE
        if r == "no":
            return ZERO
        v = self._col("settlement_value", i)
        return D(str(v)) if v is not None else None

    def category(self, i: int) -> str:
        s = self.series_info.get(self.series_of[i] or "")
        return (s.category if s is not None else "") or (self._col("category", i) or "")

    def describe(self) -> dict[str, Any]:
        def iso(ts: int) -> str | None:
            return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z") if ts > 0 else None
        return {"kind": self.kind, "markets": self.n, "candles": int(len(self.cand_end)), "step_s": self.step_s,
                "first": iso(self.first_ts), "last": iso(self.last_ts), **self.info}


# --------------------------------------------------------------------------- per-run view


class ReplayMarketData(StaticMarketData):
    """The broker's :class:`~kalshibot.paper.broker.MarketDataProvider` over a dataset at the
    backtest clock (the real :class:`~kalshibot.paper.broker.PaperBroker` runs on top of it).

    Per-run state only (caches of built :class:`Market` objects); the dataset is shared.
    Unlike :class:`StaticMarketData` it does not record calls (a replay makes millions).
    """

    def __init__(self, ds: ReplayDataset, clock: Callable[[], datetime], *, book_size: Any = 250) -> None:
        super().__init__(clock=clock)
        self.ds = ds
        self.book_size = D(book_size)
        if self.book_size <= 0:
            raise ValueError("book_size must be positive")
        self._base: dict[int, Market] = {}
        self._quoted: dict[int, tuple[int, Market]] = {}
        self._final: dict[int, Market] = {}
        self._closed: dict[int, Market] = {}
        self._dec: dict[float, Decimal] = {}

    # -- helpers ---------------------------------------------------------------------------

    def now_s(self) -> int:
        return int(self.clock().timestamp())

    def _d(self, x: float) -> Decimal:
        v = self._dec.get(x)
        if v is None:
            v = self._dec[x] = D(repr(round(float(x), 4)))
        return v

    def _base_market(self, i: int) -> Market:
        m = self._base.get(i)
        if m is None:
            m = self._base[i] = Market.from_api(self.ds.api(i))
        return m

    def row_at(self, i: int, now: int) -> int:
        """Row of the last candle of market ``i`` that ended at or before ``now`` (-1: none)."""
        ds = self.ds
        j = int(np.searchsorted(ds.cand_key, i * _SHIFT + now, side="right")) - 1
        return j if j >= ds.cand_start[i] else -1

    def status_at(self, i: int, now: int) -> str:
        ds = self.ds
        if now >= ds.settle_ts[i]:
            return "finalized"
        if now >= ds.close_ts[i]:
            return "closed"
        if ds.open_ts[i] > 0 and now < ds.open_ts[i]:
            return "initialized"
        return "active"

    def _quote(self, j: int) -> tuple[Decimal | None, Decimal | None]:
        ds = self.ds
        if j < 0:
            return None, None
        b, a = float(ds.cand_bid[j]), float(ds.cand_ask[j])
        return (self._d(b) if 0 < b < 1 else None), (self._d(a) if 0 < a < 1 else None)

    def _active(self, i: int, j: int) -> Market:
        hit = self._quoted.get(i)
        if hit is not None and hit[0] == j:
            return hit[1]
        base = self._base_market(i)
        bid, ask = self._quote(j)
        size = self.book_size
        m = dataclasses.replace(
            base, yes_bid=bid, yes_ask=ask, no_bid=(ONE - ask) if ask is not None else None,
            no_ask=(ONE - bid) if bid is not None else None, yes_bid_size=size if bid is not None else ZERO,
            yes_ask_size=size if ask is not None else ZERO)
        self._quoted[i] = (j, m)
        return m

    def market_at(self, i: int, now: int | None = None) -> Market:
        """Market ``i`` as the API would have shown it at ``now`` (default: the clock)."""
        now = self.now_s() if now is None else now
        st = self.status_at(i, now)
        if st == "active":
            return self._active(i, self.row_at(i, now))
        self._quoted.pop(i, None)
        base = self._base_market(i)
        close = datetime.fromtimestamp(int(self.ds.close_ts[i]), tz=UTC)
        if st == "finalized":
            m = self._final.get(i)
            if m is None:
                res = self.ds.result[i] or ""
                m = self._final[i] = dataclasses.replace(
                    base, status="finalized", close_time=close, result=res,
                    settlement_value=self.ds.settlement_value(i),
                    settlement_ts=datetime.fromtimestamp(int(self.ds.settle_ts[i]), tz=UTC),
                    expiration_value=str(self.ds._col("expiration_value", i) or ""))
                self._base.pop(i, None)
                self._closed.pop(i, None)
            return m
        if st == "closed":
            m = self._closed.get(i)
            if m is None:
                m = self._closed[i] = dataclasses.replace(base, status="closed", close_time=close)
            return m
        return dataclasses.replace(base, status="initialized")

    def book_at(self, i: int, now: int | None = None) -> Orderbook:
        """Synthetic book: one ``book_size`` level per side at the candle's bid/ask (empty unless active)."""
        now = self.now_s() if now is None else now
        ts = self.clock()
        t = self.ds.tickers[i]
        if self.status_at(i, now) != "active":
            return Orderbook(t, ts, (), ())
        bid, ask = self._quote(self.row_at(i, now))
        yes = (Level(bid, self.book_size),) if bid is not None else ()
        no = (Level(ONE - ask, self.book_size),) if ask is not None else ()
        return Orderbook(t, ts, yes, no)

    def snapshot(self, spec: UniverseSpec | None, now: int | None = None) -> dict[str, Market]:
        """Active markets matching ``spec`` at ``now`` (the engine's ``markets_for``), quoted."""
        now = self.now_s() if now is None else now
        ds = self.ds
        if spec is None or spec.is_empty or ds.n == 0:
            return {}
        parts: list[np.ndarray] = []
        if spec.max_days_to_close and spec.max_days_to_close > 0:
            hi_ts = now + int(spec.max_days_to_close * 86400)
            lo = bisect.bisect_right(ds.by_close_ts, now)  # type: ignore[arg-type]
            hi = bisect.bisect_right(ds.by_close_ts, hi_ts)  # type: ignore[arg-type]
            if hi > lo:
                parts.append(ds.by_close_idx[lo:hi])
        for s in spec.series_tickers:
            got = ds.series_markets.get(s)
            if got is None:
                continue
            sel, closes, life = got
            lo = bisect.bisect_right(closes, now)  # type: ignore[arg-type]
            hi = bisect.bisect_right(closes, now + life + 1)  # type: ignore[arg-type]
            if hi > lo:
                parts.append(sel[lo:hi])
        if not parts:
            return {}
        idx = parts[0] if len(parts) == 1 else np.unique(np.concatenate(parts))
        ok = (ds.open_ts[idx] <= now) & (ds.close_ts[idx] > now) & (ds.first_end[idx] <= now)
        idx = idx[ok]
        if not len(idx):
            return {}
        rows = np.searchsorted(ds.cand_key, idx * _SHIFT + now, side="right") - 1
        out: dict[str, Market] = {}
        tick = ds.tickers
        for i, j in zip(idx.tolist(), rows.tolist(), strict=True):
            out[tick[i]] = self._active(i, j)
        return out

    def forget(self, now: int | None = None) -> None:
        """Drop cached objects of markets that have closed (bounded memory over long replays)."""
        now = self.now_s() if now is None else now
        ds = self.ds
        for cache in (self._quoted, self._base):
            for i in [i for i in cache if ds.close_ts[i] <= now]:
                del cache[i]
        for i in [i for i in self._final if ds.settle_ts[i] < now - 86400]:
            del self._final[i]
        for i in [i for i in self._closed if ds.settle_ts[i] <= now]:
            del self._closed[i]

    def fee_params(self, market: Market, at: datetime | None = None) -> tuple[str, Decimal]:
        """(fee_type, multiplier) of the market's series as recorded in the dataset."""
        return resolve_fee_params(self.ds.series_info.get(market.series_ticker), None)

    # -- MarketDataProvider ---------------------------------------------------------------------

    def _index(self, ticker: str) -> int:
        i = self.ds.index.get(ticker)
        if i is None:
            raise KalshiNotFound(404, f"market {ticker} not found", f"/markets/{ticker}")
        return i

    async def orderbook(self, ticker: str, max_age_s: float = 5) -> Orderbook:
        return self.book_at(self._index(ticker))

    async def orderbooks(self, tickers: Iterable[str], max_age_s: float = 5) -> dict[str, Orderbook]:
        out: dict[str, Orderbook] = {}
        for t in dict.fromkeys(tickers):
            i = self.ds.index.get(t)
            if i is not None:
                out[t] = self.book_at(i)
        return out

    async def market(self, ticker: str, fresh: bool = False) -> Market:
        return self.market_at(self._index(ticker))

    def known_market(self, ticker: str) -> Market | None:
        i = self.ds.index.get(ticker)
        return None if i is None else self.market_at(i)

    async def series(self, series_ticker: str) -> Series:
        s = self.ds.series_info.get(series_ticker)
        if s is None:
            raise KalshiNotFound(404, f"series {series_ticker} not found", f"/series/{series_ticker}")
        return s

    async def event(self, event_ticker: str) -> None:
        return None

    async def trades_since(self, ticker: str, since: datetime) -> list[Trade]:
        return []  # candles carry no trade tape: resting (maker) orders only fill if the book crosses them

    async def exchange_status(self) -> None:
        return None


# --------------------------------------------------------------------------- loaders


def _need_pyarrow() -> Any:
    try:
        import pyarrow as pa
        import pyarrow.compute as pc
        import pyarrow.parquet as pq
    except ImportError as e:  # pragma: no cover - depends on the environment
        raise BacktestDataError(
            "the hourly research data (research/data/*.parquet) needs pyarrow, a project dependency that is "
            "not installed in this environment: run `uv sync` (or `uv run --with pyarrow kalshibot backtest ...`)"
        ) from e
    return pa, pc, pq


def _ts_seconds(pa: Any, pc: Any, col: Any) -> np.ndarray:
    """Arrow timestamp/int column -> int64 epoch seconds (nulls -> -1)."""
    typ = col.type
    if pa.types.is_timestamp(typ):
        div = {"s": 1, "ms": 1000, "us": 10**6, "ns": 10**9}[typ.unit]
        raw = pc.cast(col, pa.int64())
        arr = np.asarray(pc.fill_null(raw, -div).to_numpy(zero_copy_only=False), dtype=np.int64)
        return np.where(arr >= 0, arr // div, _NO_TS)
    arr = np.asarray(pc.fill_null(pc.cast(col, pa.float64()), -1.0).to_numpy(zero_copy_only=False))
    return np.where(arr > 0, arr, _NO_TS).astype(np.int64)


def _str_col(pc: Any, col: Any) -> _StrCol:
    enc = pc.dictionary_encode(col.combine_chunks() if hasattr(col, "combine_chunks") else col)
    codes = np.asarray(enc.indices.fill_null(-1).to_numpy(zero_copy_only=False), dtype=np.int32)
    values = enc.dictionary.to_pylist()
    if (codes < 0).any():
        codes = np.where(codes < 0, len(values), codes).astype(np.int32)
        values = [*values, None]
    return _StrCol(codes, values)


def _float_col(pa: Any, pc: Any, col: Any) -> np.ndarray:
    return np.asarray(pc.fill_null(pc.cast(col, pa.float64()), math.nan).to_numpy(zero_copy_only=False),
                      dtype=np.float64)


def _outcome_dep_series(path: Path) -> set[str] | None:
    if not path.exists():
        return None
    out: set[str] = set()
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if str(r.get("outcome_dep", "")).strip().lower() in ("true", "1"):
                out.add(str(r.get("series_ticker") or r.get("") or "").strip())
    return out


def load_hourly(data_dir: str | Path | None = None, *, universe: str = "research",
                categories: Iterable[str] | None = None) -> ReplayDataset:
    """Adapter A: ``research/data`` hourly candles (+ calibration fill candles).

    ``universe``: ``research`` (default; the calibration study's universe, see the module doc) or
    ``all`` (every settled yes/no market with candles). ``categories`` optionally restricts the
    replayed markets to these series categories (speed; strategies still filter themselves).
    """
    pa, pc, pq = _need_pyarrow()
    root = Path(data_dir) if data_dir else RESEARCH_DIR
    mk_path = root / "data" / "markets.parquet"
    hist_dir = root / "calibration" / "verify_leakage"
    cand_paths = [root / "data" / "candles_hourly.parquet", root / "calibration" / "candles_fill_hourly.parquet",
                  hist_dir / "candles_hist_hourly.parquet"]
    if not mk_path.exists() or not cand_paths[0].exists():
        raise BacktestDataError(f"hourly research data not found under {root} (need data/markets.parquet and "
                                "data/candles_hourly.parquet)")
    if universe not in ("research", "all"):
        raise BacktestDataError(f"unknown universe {universe!r} (research | all)")
    cand_paths = [p for p in cand_paths if p.exists()]
    mcols = ["ticker", "event_ticker", "series_ticker", "title", "strike_type", "floor_strike", "cap_strike",
             "open_time", "close_time", "expected_expiration_time", "can_close_early", "settlement_ts", "result",
             "settlement_value", "expiration_value", "price_level_structure", "price_ranges", "category",
             "fee_type", "fee_multiplier", "series_title", "frequency"]
    t = pq.read_table(mk_path, columns=mcols)
    result = t.column("result").to_pylist()
    tick_all = t.column("ticker").to_pylist()
    n_all = len(tick_all)

    # candle files: read once, tickers dictionary-encoded (no per-row strings)
    files = [_read_candle_file(pa, pq, p) for p in cand_paths]
    with_candles: set[str] = set()
    for f in files:
        used = np.unique(f[1])
        with_candles.update(f[0][k] for k in used.tolist())
    has = np.fromiter((x in with_candles for x in tick_all), dtype=bool, count=n_all)
    ok = np.fromiter((r in ("yes", "no") for r in result), dtype=bool, count=n_all) & has
    # archived-era markets whose (possibly empty) candle list was fetched count as covered
    fetched_path = hist_dir / "hist_fetched_tickers.parquet"
    fetched: set[str] = set()
    if fetched_path.exists():
        fetched = set(pq.read_table(fetched_path, columns=["ticker"]).column("ticker").to_pylist())
    covered = has | np.fromiter((x in fetched for x in tick_all), dtype=bool, count=n_all) if fetched else has
    close_all = _ts_seconds(pa, pc, t.column("close_time"))
    eet_all = _ts_seconds(pa, pc, t.column("expected_expiration_time"))
    event_all = _str_col(pc, t.column("event_ticker"))
    series_all = _str_col(pc, t.column("series_ticker"))
    info: dict[str, Any] = {"source": str(root), "universe": universe, "candle_files": [str(p) for p in cand_paths],
                            "hist_fetched_tickers": len(fetched)}
    if universe == "research":
        # calib_lib.universe(): not outcome-timing dependent, EET before the end of the data;
        # build_panel: every market of the event has candles
        flags = _outcome_dep_series(root / "calibration" / "series_flags.csv")
        if flags is None:
            log.warning("research/calibration/series_flags.csv not found: outcome-timing series not excluded")
            info["outcome_dep_series"] = "unavailable"
            dep = np.zeros(n_all, dtype=bool)
        else:
            info["outcome_dep_series"] = len(flags)
            flag_codes = np.fromiter((v in flags for v in series_all.values), dtype=bool, count=len(series_all.values))
            dep = flag_codes[series_all.codes]
        valid_close = close_all[close_all > 0]
        data_end = (int(valid_close.max()) // 86400 + 1) * 86400 if len(valid_close) else 0
        info["data_end"] = datetime.fromtimestamp(data_end, tz=UTC).isoformat().replace("+00:00", "Z")
        in_scope = ~dep & (eet_all > 0) & (eet_all < data_end)
        n_ev = len(event_all.values)
        missing = np.bincount(event_all.codes, weights=(~covered).astype(np.float64), minlength=n_ev)
        all_candles = missing[event_all.codes] == 0
        ok &= in_scope & all_candles
    if categories:
        want = {str(c).strip().casefold() for c in categories if str(c).strip()}
        cats = _str_col(pc, t.column("category"))
        cat_ok = np.fromiter(((v or "").casefold() in want for v in cats.values), dtype=bool, count=len(cats.values))
        ok &= cat_ok[cats.codes]
        info["categories"] = sorted(want)
    sel = np.nonzero(ok)[0]
    t = t.take(pa.array(sel))
    tickers = [tick_all[i] for i in sel.tolist()]
    cols: dict[str, Any] = {
        "ticker": tickers,
        "event_ticker": event_all.take(sel),
        "series_ticker": series_all.take(sel),
        "open_ts": _ts_seconds(pa, pc, t.column("open_time")),
        "close_ts": close_all[sel],
        "eet_ts": eet_all[sel],
        "settle_ts": _ts_seconds(pa, pc, t.column("settlement_ts")),
        "can_close_early": np.asarray(pc.fill_null(t.column("can_close_early"), False).to_numpy(zero_copy_only=False),
                                      dtype=bool),
        "result": _str_col(pc, t.column("result")),
        "floor_strike": _float_col(pa, pc, t.column("floor_strike")),
        "cap_strike": _float_col(pa, pc, t.column("cap_strike")),
        "settlement_value": _float_col(pa, pc, t.column("settlement_value")),
    }
    for k in ("title", "strike_type", "expiration_value", "price_level_structure", "price_ranges", "category"):
        cols[k] = _str_col(pc, t.column(k))
    # series metadata (as of the dataset fetch): category, fee type and multiplier
    series_info: dict[str, Series] = {}
    s_codes = cols["series_ticker"].codes
    first = np.unique(s_codes, return_index=True)[1]
    cat, ft, fm = cols["category"], _str_col(pc, t.column("fee_type")), _float_col(pa, pc, t.column("fee_multiplier"))
    stitle, freq = _str_col(pc, t.column("series_title")), _str_col(pc, t.column("frequency"))
    for i in first.tolist():
        name = cols["series_ticker"][i]
        if not name:
            continue
        mult = fm[i]
        series_info[name] = Series.from_api({
            "ticker": name, "title": stitle[i] or name, "category": cat[i] or "", "frequency": freq[i] or "",
            "fee_type": ft[i] or "quadratic", "fee_multiplier": 1.0 if math.isnan(mult) else float(mult)})

    # candles of the selected markets: hourly file first (it wins on duplicate (ticker, end))
    index = {tk: i for i, tk in enumerate(tickers)}
    parts: list[tuple[np.ndarray, ...]] = []
    for dictionary, codes, end, bid, ask in files:
        lut = np.fromiter((index.get(v, -1) for v in dictionary), dtype=np.int64, count=len(dictionary))
        mi = lut[codes] if len(lut) else np.zeros(0, dtype=np.int64)
        keep = (mi >= 0) & np.isfinite(bid) & np.isfinite(ask)
        parts.append((mi[keep], end[keep], bid[keep], ask[keep]))
    del files, t
    ci, ce, cb, ca = (np.concatenate([p[k] for p in parts]) if parts else np.zeros(0) for k in range(4))
    ds = ReplayDataset(kind="hourly", step_s=3600, default_fill="same", cols=cols, candle_idx=ci, candle_end=ce,
                       candle_bid=cb, candle_ask=ca, series_info=series_info, feeds=None, info=info)
    try:  # hand Arrow's freed buffers back to the OS (the server process lives on)
        pa.default_memory_pool().release_unused()
    except Exception:  # pragma: no cover - older pyarrow
        pass
    return ds


def _read_candle_file(pa: Any, pq: Any, path: Path) -> tuple[list[str], np.ndarray, np.ndarray, np.ndarray,
                                                             np.ndarray]:
    """(ticker dictionary, per-row codes, end_period_ts, yes_bid_close, yes_ask_close) of a candle file."""
    t = pq.read_table(path, columns=["ticker", "end_period_ts", "yes_bid_close", "yes_ask_close"],
                      read_dictionary=["ticker"]).unify_dictionaries()
    col = t.column("ticker")
    if col.num_chunks == 0 or len(col) == 0:
        z = np.zeros(0)
        return [], z.astype(np.int64), z.astype(np.int64), z, z
    dictionary = col.chunk(0).dictionary.to_pylist()
    codes = np.concatenate([np.asarray(c.indices.fill_null(0).to_numpy(zero_copy_only=False), dtype=np.int64)
                            for c in col.chunks])
    end = np.asarray(t.column("end_period_ts").cast(pa.int64()).to_numpy(), dtype=np.int64)
    bid = np.asarray(t.column("yes_bid_close").cast(pa.float64()).fill_null(math.nan).to_numpy(), dtype=np.float64)
    ask = np.asarray(t.column("yes_ask_close").cast(pa.float64()).fill_null(math.nan).to_numpy(), dtype=np.float64)
    return dictionary, codes, end, bid, ask


def _open_text(path: Path) -> Any:
    return gzip.open(path, "rt", newline="") if path.suffix == ".gz" else open(path, newline="")


def _minute_sources(root: Path, series: str) -> list[tuple[Path, Path]]:
    cf = root / "crypto_fv"
    cands = [(cf / "data" / f"markets_{series}.csv.gz", cf / "data" / f"candles_{series}.csv.gz"),
             (cf / "verify_stats" / "out" / f"hist_markets_{series}.csv.gz",
              cf / "verify_stats" / "out" / f"hist_candles_{series}.csv.gz"),
             (cf / "verify_leakage" / "data_holdout" / f"markets_{series}.csv.gz",
              cf / "verify_leakage" / "data_holdout" / f"candles_{series}.csv.gz")]
    return [(m, c) for m, c in cands if m.exists() and c.exists()]


def minute_series_available(series: Iterable[str], data_dir: str | Path | None = None) -> bool:
    """True when every series has 1-minute research data (the ``minute`` adapter can replay it)."""
    root = Path(data_dir) if data_dir else RESEARCH_DIR
    ss = list(series)
    return bool(ss) and all(_minute_sources(root, s) for s in ss)


def _series_meta(root: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    p = root / "crypto_fv" / "cache" / "series_crypto.json"
    try:
        with open(p) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return out
    for s in (d.get("series") if isinstance(d, Mapping) else d) or ():
        if isinstance(s, Mapping) and s.get("ticker"):
            out[str(s["ticker"])] = dict(s)
    return out


def load_minute(series: Iterable[str], data_dir: str | Path | None = None) -> ReplayDataset:
    """Adapter B: 1-minute candles of short-dated crypto series from ``research/crypto_fv``.

    Markets and candles are merged from the in-sample fetch and the holdout fetches (first
    source wins per ticker / per candle). Registers the replay feeds the live strategies use:
    ``crypto`` (Coinbase 1-minute bars, :class:`~kalshibot.feeds.replay.ReplayCryptoFeed`) and
    ``kalshi_settled`` (:class:`~kalshibot.feeds.replay.ReplaySettledFeed`), both clocked by the
    backtest (only bars that ended / markets settled by ``now``).
    """
    from kalshibot.feeds import FeedRegistry, ReplayCryptoFeed, ReplaySettledFeed

    root = Path(data_dir) if data_dir else RESEARCH_DIR
    names = [str(s) for s in series]
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    candles: dict[str, list[tuple[int, float, float]]] = {}
    used: list[str] = []
    for s in names:
        srcs = _minute_sources(root, s)
        if not srcs:
            raise BacktestDataError(f"no 1-minute research data for {s} under {root / 'crypto_fv'}")
        for mpath, cpath in srcs:
            used.append(str(mpath))
            fresh: set[str] = set()
            with _open_text(mpath) as f:
                for r in csv.DictReader(f):
                    t = r.get("ticker") or ""
                    if not t or t in seen or r.get("result") not in ("yes", "no"):
                        continue
                    seen.add(t)
                    fresh.add(t)
                    rows.append({**r, "series_ticker": r.get("series") or s})
            with _open_text(cpath) as f:
                for r in csv.DictReader(f):
                    t = r.get("ticker") or ""
                    if t not in fresh:
                        continue
                    try:
                        candles.setdefault(t, []).append((int(float(r["ts"])), float(r["yes_bid"]),
                                                          float(r["yes_ask"])))
                    except (KeyError, TypeError, ValueError):
                        continue
    meta = _series_meta(root)
    markets = []
    for r in rows:
        pls = r.get("pls") or r.get("price_level_structure") or "linear_cent"
        markets.append({
            "ticker": r["ticker"], "event_ticker": r.get("event_ticker"), "series_ticker": r["series_ticker"],
            "open_ts": r.get("open_ts"), "close_ts": r.get("close_ts"), "settle_ts": r.get("settlement_ts"),
            "eet_ts": r.get("expected_expiration_ts") or None,
            "can_close_early": str(r.get("can_close_early", "")).lower() == "true",
            "result": r.get("result"), "strike_type": r.get("strike_type"), "floor_strike": r.get("floor_strike"),
            "cap_strike": r.get("cap_strike"), "expiration_value": r.get("expiration_value"),
            "settlement_value": r.get("settlement_value"), "price_level_structure": pls,
            "price_ranges": PRICE_LEVEL_STRUCTURES.get(pls)})
    series_objs = []
    for s in names:
        m = meta.get(s, {})
        series_objs.append(Series.from_api({
            "ticker": s, "title": m.get("title") or s, "category": m.get("category") or "Crypto",
            "frequency": m.get("frequency") or "", "fee_type": m.get("fee_type") or "quadratic",
            "fee_multiplier": m.get("fee_multiplier") if m.get("fee_multiplier") is not None else 1}))
    spot_files = {}
    for s in names:
        sym, product = SPOT_PRODUCTS.get(s, (s.removeprefix("KX").removesuffix("15M"), ""))
        p = root / "crypto_fv" / "data" / f"spot_{product}.csv"
        if product and p.exists():
            spot_files[sym] = p
    settled_rows = [{k: v for k, v in r.items() if k not in ("series",)} for r in rows]

    def feeds(clock: Callable[[], datetime]) -> Any:
        reg = FeedRegistry()
        if spot_files:
            bars: dict[str, list[tuple[float, float, float, float, float, float]]] = {}
            for sym, p in spot_files.items():
                bars[sym] = _read_spot(p)
            reg.register("crypto", ReplayCryptoFeed(bars, clock=clock))
        reg.register("kalshi_settled", ReplaySettledFeed.from_rows(settled_rows, clock=clock))
        return reg

    info = {"source": str(root / "crypto_fv"), "series": names, "market_files": used,
            "spot_files": {k: str(v) for k, v in spot_files.items()}}
    return ReplayDataset.from_records(markets, candles, kind="minute", step_s=60, default_fill="next_ask",
                                      series=series_objs, feeds=feeds, info=info)


def _read_spot(path: Path) -> list[tuple[float, float, float, float, float, float]]:
    """Research spot file (``ts,low,high,open,close,volume``; ``ts`` = bar start) -> replay rows."""
    out = []
    with open(path, newline="") as f:
        for d in csv.DictReader(f):
            try:
                out.append((float(d["ts"]), float(d["open"]), float(d["high"]), float(d["low"]), float(d["close"]),
                            float(d.get("volume") or 0.0)))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def load_dataset(kind: str, *, series: Sequence[str] = (), data_dir: str | Path | None = None,
                 universe: str = "research", categories: Iterable[str] | None = None) -> ReplayDataset:
    """``hourly`` (Adapter A) or ``minute`` (Adapter B, needs ``series``)."""
    if kind == "hourly":
        return load_hourly(data_dir, universe=universe, categories=categories)
    if kind == "minute":
        if not series:
            raise BacktestDataError("the minute adapter needs the strategy's series (UniverseSpec.series_tickers)")
        return load_minute(series, data_dir)
    raise BacktestDataError(f"unknown data kind {kind!r} (hourly | minute)")
