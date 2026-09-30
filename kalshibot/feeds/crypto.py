"""Crypto spot feed: BTC/ETH (any symbol) spot price and recent 1-minute candles.

Public, unauthenticated REST only: Coinbase Exchange (primary) with Kraken as fallback.
No keys, no order endpoints. Results are cached with a TTL and concurrent callers share one
request. Values are ``float`` (model inputs, not ledger money).

    feed = CryptoSpotFeed()
    q = await feed.spot("BTC")             # SpotQuote(price=..., bid=..., ask=..., source="coinbase")
    cs = await feed.candles("ETH", 120)    # oldest-first SpotCandle list, 1-minute bars (.source)

Coinbase ``/products/{P}/candles`` returns at most 300 bars per request (newest first,
``[time, low, high, open, close, volume]``); longer histories are paged. Kraken ``/OHLC``
returns up to 720 bars (oldest first). The newest bar is usually still in progress
(``SpotCandle.complete`` is False for it).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from kalshibot.kalshi.client import TokenBucket

__all__ = ["COINBASE_URL", "KRAKEN_URL", "CryptoSpotFeed", "FeedError", "SpotCandle", "SpotQuote"]

log = logging.getLogger(__name__)

COINBASE_URL = "https://api.exchange.coinbase.com"
KRAKEN_URL = "https://api.kraken.com"
COINBASE_MAX_CANDLES = 300
KRAKEN_PAIRS = {"BTC": "XBTUSD", "DOGE": "XDGUSD"}


class FeedError(RuntimeError):
    """Every source failed for a request (the last good value is still in ``feed.last``)."""


@dataclass(frozen=True, slots=True)
class SpotQuote:
    symbol: str
    price: float  # last trade price
    bid: float | None
    ask: float | None
    ts: datetime  # exchange timestamp of the last trade (or fetch time)
    source: str  # "coinbase" | "kraken"
    fetched_at: datetime

    @property
    def mid(self) -> float:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return self.price

    def to_json(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "price": self.price, "bid": self.bid, "ask": self.ask,
                "ts": _iso(self.ts), "source": self.source, "fetched_at": _iso(self.fetched_at)}


@dataclass(frozen=True, slots=True)
class SpotCandle:
    ts: datetime  # bar start (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float
    complete: bool = True
    source: str = ""  # "coinbase" | "kraken" ("" = unknown, e.g. a hand-built bar)

    @property
    def end(self) -> datetime:
        return self.ts + timedelta(minutes=1)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _parse_time(s: Any, default: datetime) -> datetime:
    if not isinstance(s, str) or not s:
        return default
    txt = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return default
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


class CryptoSpotFeed:
    """Spot prices and 1-minute candles with TTL caching and exchange fallback."""

    name = "crypto"

    def __init__(
        self,
        symbols: Iterable[str] = ("BTC", "ETH"),
        *,
        ttl_s: float = 5.0,
        candle_ttl_s: float = 30.0,
        max_rps: float = 3.0,
        timeout: float = 10.0,
        sources: Sequence[str] = ("coinbase", "kraken"),
        coinbase_url: str = COINBASE_URL,
        kraken_url: str = KRAKEN_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        wallclock: Callable[[], datetime] | None = None,
    ) -> None:
        self.symbols = [s.upper() for s in symbols]
        self.ttl_s = float(ttl_s)
        self.candle_ttl_s = float(candle_ttl_s)
        self.sources = [s for s in sources if s in ("coinbase", "kraken")] or ["coinbase"]
        self.coinbase_url = coinbase_url.rstrip("/")
        self.kraken_url = kraken_url.rstrip("/")
        self._clock = clock
        self._wall = wallclock or (lambda: datetime.now(UTC))
        self._limiter = TokenBucket(max_rps, 1.0)
        self._http = httpx.AsyncClient(
            timeout=timeout, transport=transport,
            headers={"User-Agent": "kalshibot-paper/0.1 (paper trading; public market data)",
                     "Accept": "application/json"})
        self._spot: dict[str, tuple[float, SpotQuote]] = {}
        self._candles: dict[tuple[str, int], tuple[float, list[SpotCandle]]] = {}
        self._locks: dict[Any, asyncio.Lock] = {}
        self.last: dict[str, SpotQuote] = {}
        self.errors: dict[str, str] = {}
        self.request_count = 0

    # -- plumbing ------------------------------------------------------------------

    def _lock(self, key: Any) -> asyncio.Lock:
        lk = self._locks.get(key)
        if lk is None:
            lk = self._locks[key] = asyncio.Lock()
        return lk

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        await self._limiter.acquire()
        self.request_count += 1
        resp = await self._http.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def coinbase_product(symbol: str) -> str:
        return f"{symbol.upper()}-USD"

    @staticmethod
    def kraken_pair(symbol: str) -> str:
        s = symbol.upper()
        return KRAKEN_PAIRS.get(s, f"{s}USD")

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- spot ----------------------------------------------------------------------

    async def spot(self, symbol: str = "BTC", *, max_age_s: float | None = None) -> SpotQuote:
        """Latest spot quote (cached ``ttl_s``). Raises :class:`FeedError` if every source fails."""
        sym = symbol.upper()
        ttl = self.ttl_s if max_age_s is None else max_age_s
        hit = self._spot.get(sym)
        if hit is not None and self._clock() - hit[0] < ttl:
            return hit[1]
        async with self._lock(("spot", sym)):
            hit = self._spot.get(sym)
            if hit is not None and self._clock() - hit[0] < ttl:
                return hit[1]
            errors: list[str] = []
            for src in self.sources:
                try:
                    q = await (self._coinbase_spot(sym) if src == "coinbase" else self._kraken_spot(sym))
                except Exception as e:  # try the next source
                    errors.append(f"{src}: {type(e).__name__}: {e}")
                    continue
                self._spot[sym] = (self._clock(), q)
                self.last[sym] = q
                self.errors.pop(sym, None)
                return q
            msg = "; ".join(errors)
            self.errors[sym] = msg
            log.warning("crypto spot %s unavailable: %s", sym, msg)
            raise FeedError(f"spot {sym} unavailable ({msg})")

    async def price(self, symbol: str = "BTC") -> float:
        return (await self.spot(symbol)).price

    async def _coinbase_spot(self, sym: str) -> SpotQuote:
        d = await self._get(f"{self.coinbase_url}/products/{self.coinbase_product(sym)}/ticker")
        price = _f(d.get("price")) if isinstance(d, dict) else None
        if price is None or price <= 0:
            raise ValueError(f"bad coinbase ticker: {str(d)[:120]}")
        now = self._wall()
        return SpotQuote(sym, price, _f(d.get("bid")), _f(d.get("ask")), _parse_time(d.get("time"), now),
                         "coinbase", now)

    async def _kraken_spot(self, sym: str) -> SpotQuote:
        d = await self._get(f"{self.kraken_url}/0/public/Ticker", {"pair": self.kraken_pair(sym)})
        res = self._kraken_result(d)
        row = next(iter(res.values()), None) if isinstance(res, dict) else None
        if not isinstance(row, dict):
            raise ValueError(f"bad kraken ticker: {str(d)[:120]}")
        price = _f((row.get("c") or [None])[0])
        if price is None or price <= 0:
            raise ValueError(f"bad kraken ticker: {str(d)[:120]}")
        now = self._wall()
        return SpotQuote(sym, price, _f((row.get("b") or [None])[0]), _f((row.get("a") or [None])[0]),
                         now, "kraken", now)

    @staticmethod
    def _kraken_result(d: Any) -> Any:
        if not isinstance(d, dict):
            raise ValueError("kraken: expected an object")
        if d.get("error"):
            raise ValueError(f"kraken error: {d['error']}")
        return d.get("result") or {}

    # -- candles -------------------------------------------------------------------

    async def candles(self, symbol: str = "BTC", minutes: int = 60, *,
                      max_age_s: float | None = None) -> list[SpotCandle]:
        """The last ``minutes`` 1-minute bars, oldest first (cached ``candle_ttl_s``)."""
        sym = symbol.upper()
        minutes = max(1, int(minutes))
        key = (sym, minutes)
        ttl = self.candle_ttl_s if max_age_s is None else max_age_s
        hit = self._candles.get(key)
        if hit is not None and self._clock() - hit[0] < ttl:
            return hit[1]
        async with self._lock(("candles", key)):
            hit = self._candles.get(key)
            if hit is not None and self._clock() - hit[0] < ttl:
                return hit[1]
            errors: list[str] = []
            for src in self.sources:
                try:
                    cs = await (self._coinbase_candles(sym, minutes) if src == "coinbase"
                                else self._kraken_candles(sym, minutes))
                except Exception as e:
                    errors.append(f"{src}: {type(e).__name__}: {e}")
                    continue
                if not cs:
                    errors.append(f"{src}: no candles")
                    continue
                self._candles[key] = (self._clock(), cs)
                return cs
            msg = "; ".join(errors)
            log.warning("crypto candles %s unavailable: %s", sym, msg)
            raise FeedError(f"candles {sym} unavailable ({msg})")

    def _finish(self, rows: dict[int, SpotCandle], minutes: int) -> list[SpotCandle]:
        now = self._wall()
        out = []
        for t in sorted(rows)[-minutes:]:
            c = rows[t]
            out.append(SpotCandle(c.ts, c.open, c.high, c.low, c.close, c.volume, complete=c.end <= now,
                                  source=c.source))
        return out

    async def _coinbase_candles(self, sym: str, minutes: int) -> list[SpotCandle]:
        product = self.coinbase_product(sym)
        end = self._wall().replace(second=0, microsecond=0) + timedelta(minutes=1)
        rows: dict[int, SpotCandle] = {}
        remaining = minutes
        while remaining > 0:
            n = min(COINBASE_MAX_CANDLES, remaining)
            start = end - timedelta(minutes=n)
            data = await self._get(f"{self.coinbase_url}/products/{product}/candles",
                                   {"granularity": 60, "start": _iso(start), "end": _iso(end)})
            if not isinstance(data, list):
                raise ValueError(f"bad coinbase candles: {str(data)[:120]}")
            for r in data:
                if not isinstance(r, list | tuple) or len(r) < 6:
                    continue
                t = int(r[0])
                lo, hi, op, cl, vol = (_f(x) for x in r[1:6])
                if None in (lo, hi, op, cl):
                    continue
                rows[t] = SpotCandle(datetime.fromtimestamp(t, tz=UTC), op, hi, lo, cl,  # type: ignore[arg-type]
                                     vol or 0.0, source="coinbase")
            if not data:
                break
            remaining -= n
            end = start
        return self._finish(rows, minutes)

    async def _kraken_candles(self, sym: str, minutes: int) -> list[SpotCandle]:
        since = int((self._wall() - timedelta(minutes=minutes + 1)).timestamp())
        d = await self._get(f"{self.kraken_url}/0/public/OHLC",
                            {"pair": self.kraken_pair(sym), "interval": 1, "since": since})
        res = self._kraken_result(d)
        series = next((v for k, v in res.items() if k != "last" and isinstance(v, list)), None)
        if series is None:
            raise ValueError(f"bad kraken OHLC: {str(d)[:120]}")
        rows: dict[int, SpotCandle] = {}
        for r in series:
            if not isinstance(r, list | tuple) or len(r) < 7:
                continue
            t = int(r[0])
            op, hi, lo, cl = (_f(x) for x in r[1:5])
            vol = _f(r[6])
            if None in (op, hi, lo, cl):
                continue
            rows[t] = SpotCandle(datetime.fromtimestamp(t, tz=UTC), op, hi, lo, cl,  # type: ignore[arg-type]
                                 vol or 0.0, source="kraken")
        return self._finish(rows, minutes)

    # -- status --------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        now = self._wall()
        return {
            "symbols": self.symbols,
            "sources": self.sources,
            "requests": self.request_count,
            "last": {s: {**q.to_json(), "age_s": round((now - q.fetched_at).total_seconds(), 1)}
                     for s, q in self.last.items()},
            "errors": dict(self.errors),
        }
