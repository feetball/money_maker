"""Replay feeds for backtests: the ``crypto`` and ``kalshi_settled`` interfaces over recorded data.

A backtest context registers these under the same names the live app uses, so a strategy
reads ``ctx.feeds.crypto`` / ``ctx.feeds.kalshi_settled`` identically live and in a replay::

    clock = lambda: ctx.now                            # the backtest's snapshot time
    feeds = FeedRegistry({
        "crypto": ReplayCryptoFeed.from_csv("research/crypto_fv/data/spot_BTC-USD.csv", clock=clock),
        "kalshi_settled": ReplaySettledFeed.from_csv("research/crypto_fv/data/markets_KXBTC15M.csv.gz",
                                                     clock=clock),
    })

**No look-ahead.** Both feeds only return what was knowable at ``clock()``:

* :class:`ReplayCryptoFeed` serves complete 1-minute bars whose **end** (bar start + 60 s) is at
  or before ``now``. ``spot()`` is the close of the newest such bar (``ts`` = its end,
  ``bid``/``ask`` None), which is exactly the research's "spot at t" (Coinbase candle ``time``
  is the bucket start; the close is known at start + 60 s).
* :class:`ReplaySettledFeed` serves markets whose settlement is at or before ``now``
  (``settlement_ts``, else ``close_time + settle_delay_s``).

Values match the live classes: :class:`~kalshibot.feeds.crypto.SpotQuote`,
:class:`~kalshibot.feeds.crypto.SpotCandle` (``complete=True``, ``source`` as given, default
``"coinbase"``) and :class:`~kalshibot.kalshi.models.Market`.
"""

from __future__ import annotations

import bisect
import csv
import gzip
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kalshibot.feeds.crypto import FeedError, SpotCandle, SpotQuote
from kalshibot.kalshi.models import Market

__all__ = ["ReplayCryptoFeed", "ReplaySettledFeed"]

Clock = Callable[[], datetime]


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _fixed_clock(now: datetime | None) -> Clock:
    fixed = _utc(now) if now is not None else datetime(1970, 1, 1, tzinfo=UTC)
    return lambda: fixed


def _open(path: str | Path) -> Any:
    p = str(path)
    return gzip.open(p, "rt", newline="") if p.endswith(".gz") else open(p, newline="")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


class ReplayCryptoFeed:
    """1-minute bars replayed up to ``clock()`` (the live :class:`CryptoSpotFeed` interface)."""

    name = "crypto"

    def __init__(
        self,
        bars: Mapping[str, Iterable[SpotCandle | Sequence[Any]]],
        *,
        clock: Clock | None = None,
        now: datetime | None = None,
        source: str = "coinbase",
    ) -> None:
        """``bars``: per symbol, :class:`SpotCandle` objects or ``(start_epoch_s, open, high, low,
        close, volume)`` rows, any order (``ts``/start = bucket start, UTC)."""
        self.source = source
        self._clock: Clock = clock or _fixed_clock(now)
        self._bars: dict[str, list[SpotCandle]] = {}
        self._ends: dict[str, list[float]] = {}
        for sym, rows in bars.items():
            out: dict[float, SpotCandle] = {}
            for r in rows:
                c = r if isinstance(r, SpotCandle) else self._row(r)
                if c is None:
                    continue
                c = SpotCandle(_utc(c.ts), c.open, c.high, c.low, c.close, c.volume, complete=True,
                               source=c.source or source)
                out[c.ts.timestamp()] = c
            ordered = [out[k] for k in sorted(out)]
            self._bars[sym.upper()] = ordered
            self._ends[sym.upper()] = [c.end.timestamp() for c in ordered]
        self.symbols = sorted(self._bars)
        self.request_count = 0

    @staticmethod
    def _row(r: Sequence[Any]) -> SpotCandle | None:
        try:
            t, op, hi, lo, cl = (float(x) for x in r[:5])
            vol = float(r[5]) if len(r) > 5 else 0.0
        except (TypeError, ValueError, IndexError):
            return None
        if not all(math.isfinite(x) for x in (t, op, hi, lo, cl)) or cl <= 0:
            return None
        return SpotCandle(datetime.fromtimestamp(t, tz=UTC), op, hi, lo, cl, vol)

    @classmethod
    def from_csv(cls, path: str | Path, symbol: str = "BTC", *, clock: Clock | None = None,
                 source: str = "coinbase", start: datetime | None = None,
                 end: datetime | None = None) -> ReplayCryptoFeed:
        """Load a research spot file (``ts,low,high,open,close,volume``; ``ts`` = bar start, epoch s)."""
        lo = start.timestamp() if start is not None else -math.inf
        hi = end.timestamp() if end is not None else math.inf
        rows: list[tuple[float, float, float, float, float, float]] = []
        with _open(path) as f:
            for d in csv.DictReader(f):
                try:
                    t = float(d["ts"])
                    if not lo <= t <= hi:
                        continue
                    rows.append((t, float(d["open"]), float(d["high"]), float(d["low"]), float(d["close"]),
                                 float(d.get("volume") or 0.0)))
                except (KeyError, TypeError, ValueError):
                    continue
        return cls({symbol: rows}, clock=clock, source=source)

    # -- clock ---------------------------------------------------------------------

    @property
    def now(self) -> datetime:
        return _utc(self._clock())

    def set_now(self, now: datetime) -> None:
        """Pin the replay clock (alternative to passing ``clock``)."""
        self._clock = _fixed_clock(now)

    def _upto(self, symbol: str) -> tuple[list[SpotCandle], int]:
        sym = symbol.upper()
        bars = self._bars.get(sym)
        if not bars:
            raise FeedError(f"replay: no bars for {sym}")
        n = bisect.bisect_right(self._ends[sym], self.now.timestamp())
        return bars, n

    # -- the CryptoSpotFeed interface ------------------------------------------------

    async def spot(self, symbol: str = "BTC", *, max_age_s: float | None = None) -> SpotQuote:
        bars, n = self._upto(symbol)
        self.request_count += 1
        if n == 0:
            raise FeedError(f"replay: no {symbol.upper()} bar has ended by {_iso(self.now)}")
        c = bars[n - 1]
        return SpotQuote(symbol.upper(), c.close, None, None, c.end, c.source or self.source, self.now)

    async def price(self, symbol: str = "BTC") -> float:
        return (await self.spot(symbol)).price

    async def candles(self, symbol: str = "BTC", minutes: int = 60, *,
                      max_age_s: float | None = None) -> list[SpotCandle]:
        bars, n = self._upto(symbol)
        self.request_count += 1
        minutes = max(1, int(minutes))
        return bars[max(0, n - minutes): n]

    def status(self) -> dict[str, Any]:
        return {"replay": True, "now": _iso(self.now), "symbols": self.symbols, "requests": self.request_count}


class ReplaySettledFeed:
    """Settled markets replayed up to ``clock()`` (the live :class:`KalshiSettledFeed` interface)."""

    name = "kalshi_settled"

    def __init__(self, markets: Iterable[Market], *, clock: Clock | None = None, now: datetime | None = None,
                 settle_delay_s: float = 10.0) -> None:
        self._clock: Clock = clock or _fixed_clock(now)
        self.settle_delay = timedelta(seconds=float(settle_delay_s))
        by_series: dict[str, list[tuple[float, Market]]] = {}
        for m in markets:
            known = self._known_at(m)
            if known is None or m.result not in ("yes", "no", "scalar"):
                continue
            by_series.setdefault(m.series_ticker.upper(), []).append((known.timestamp(), m))
        self._series: dict[str, list[Market]] = {}
        self._known: dict[str, list[float]] = {}
        for s, rows in by_series.items():
            rows.sort(key=lambda r: (r[0], r[1].ticker))
            self._series[s] = [m for _, m in rows]
            self._known[s] = [k for k, _ in rows]
        self.request_count = 0

    def _known_at(self, m: Market) -> datetime | None:
        if m.settlement_ts is not None:
            return m.settlement_ts
        return m.close_time + self.settle_delay if m.close_time is not None else None

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]], **kw: Any) -> ReplaySettledFeed:
        """Markets from API-shaped dicts, or research CSV rows (``close_ts``/``open_ts`` epoch seconds)."""
        out: list[Market] = []
        for r in rows:
            d = dict(r)
            for src, dst in (("close_ts", "close_time"), ("open_ts", "open_time")):
                v = d.get(src)
                if dst not in d and v not in (None, ""):
                    try:
                        d[dst] = _iso(datetime.fromtimestamp(float(v), tz=UTC))
                    except (TypeError, ValueError):
                        pass
            if d.get("settlement_value") not in (None, "") and "settlement_value_dollars" not in d:
                d["settlement_value_dollars"] = d["settlement_value"]
            d.pop("settlement_value", None)
            if not d.get("series_ticker") and d.get("series"):
                d["series_ticker"] = d["series"]
            for k in ("floor_strike", "cap_strike"):
                if d.get(k) == "":
                    d[k] = None
            d.setdefault("status", "finalized")
            out.append(Market.from_api(d))
        return cls(out, **kw)

    @classmethod
    def from_csv(cls, path: str | Path, **kw: Any) -> ReplaySettledFeed:
        """Load a research markets file (``research/crypto_fv/data/markets_<SERIES>.csv.gz``)."""
        with _open(path) as f:
            return cls.from_rows(list(csv.DictReader(f)), **kw)

    @property
    def now(self) -> datetime:
        return _utc(self._clock())

    def set_now(self, now: datetime) -> None:
        self._clock = _fixed_clock(now)

    async def settled_markets(self, series_ticker: str, *, since: datetime | None = None) -> list[Market]:
        s = str(series_ticker).upper()
        self.request_count += 1
        known = self._known.get(s)
        if not known:
            return []
        hi = bisect.bisect_right(known, self.now.timestamp())
        lo = bisect.bisect_left(known, since.timestamp()) if since is not None else 0
        rows = self._series[s][lo:hi]
        return sorted(rows, key=lambda m: m.close_time.timestamp() if m.close_time else -math.inf, reverse=True)

    def status(self) -> dict[str, Any]:
        return {"replay": True, "now": _iso(self.now), "series": {s: len(v) for s, v in self._series.items()},
                "requests": self.request_count}
