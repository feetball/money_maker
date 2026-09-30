"""Recently settled Kalshi markets of a series (``ctx.feeds.kalshi_settled``).

Strategies that calibrate against past outcomes need settled markets, which are not part of
the open universe (``ctx.markets``). This feed lists them from the public, unauthenticated
REST API::

    feed = KalshiSettledFeed()                       # or KalshiSettledFeed(client=engine_client)
    ms = await feed.settled_markets("KXBTC15M", since=now - timedelta(hours=14))
    # -> list[Market], newest close first; each has result, expiration_value, settlement_ts, ...

One request is ``GET /markets?series_ticker=S&status=settled&min_settled_ts=...&limit=1000``
(finalized markets only: REST ``status=settled`` means ``finalized``). Results are cached for
``ttl_s`` per series, and a cached answer is reused for any ``since`` at or after the one it was
fetched with. The feed is lazy: it makes no request (and opens no connection) until a strategy
asks.

Pass ``client`` (a :class:`~kalshibot.kalshi.client.KalshiClient`, or anything with the same
``async get_markets(**filters) -> (markets, cursor)``) to share the engine's rate limiter;
otherwise the feed creates its own public client with a small budget (``max_rps``, default
0.5 req/s). PAPER TRADING ONLY: read endpoints, no credentials.

The backtest counterpart with the same interface is
:class:`kalshibot.feeds.replay.ReplaySettledFeed`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kalshibot.kalshi.models import Market

__all__ = ["KalshiSettledFeed"]

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
MAX_PAGES = 5


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def _close_key(m: Market) -> float:
    return m.close_time.timestamp() if m.close_time is not None else float("-inf")


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z") if dt is not None else None


async def _markets_page(client: Any, filters: dict[str, Any]) -> tuple[list[Market], str | None]:
    """One ``/markets`` page via ``client.get_markets`` (or a bare ``client.get``)."""
    from kalshibot.kalshi.models import Market

    fn = getattr(client, "get_markets", None)
    if callable(fn):
        page, cursor = await fn(**filters)
        return list(page), (cursor or None)
    d = await client.get("/markets", dict(filters))
    cursor = d.get("cursor") if isinstance(d, dict) else None
    rows = d.get("markets") if isinstance(d, dict) else None
    return [Market.from_api(r) for r in rows or ()], (str(cursor) if cursor else None)


class KalshiSettledFeed:
    """Finalized markets of a series, newest first, TTL-cached per series."""

    name = "kalshi_settled"

    def __init__(
        self,
        client: Any = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        max_rps: float = 0.5,
        timeout: float = 15.0,
        ttl_s: float = 60.0,
        page_limit: int = 1000,
        clock: Callable[[], float] = time.monotonic,
        wallclock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._own_client = client is None
        self.base_url = base_url
        self.max_rps = float(max_rps)
        self.timeout = float(timeout)
        self.ttl_s = float(ttl_s)
        self.page_limit = max(1, min(1000, int(page_limit)))
        self._clock = clock
        self._wall = wallclock or (lambda: datetime.now(UTC))
        # series -> (fetched at (monotonic), min_settled_ts used (epoch s or None), markets newest first)
        self._cache: dict[str, tuple[float, int | None, list[Market]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.request_count = 0
        self.errors: dict[str, str] = {}
        self.last_fetch: dict[str, datetime] = {}

    def _get_client(self) -> Any:
        if self._client is None:
            from kalshibot.kalshi.client import KalshiClient

            self._client = KalshiClient(self.base_url, max_rps=self.max_rps, timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def settled_markets(self, series_ticker: str, *, since: datetime | None = None) -> list[Market]:
        """Finalized markets of ``series_ticker`` settled at/after ``since`` (all if None), newest close first.

        Raises whatever the client raises (e.g. ``KalshiAPIError``) when Kalshi is unreachable.
        """
        series = str(series_ticker).upper()
        want = _epoch(since) if since is not None else None
        hit = self._fresh(series, want)
        if hit is not None:
            return hit
        lock = self._locks.setdefault(series, asyncio.Lock())
        async with lock:
            hit = self._fresh(series, want)
            if hit is not None:
                return hit
            try:
                markets = await self._fetch(series, want)
            except Exception as e:
                self.errors[series] = f"{type(e).__name__}: {e}"
                log.warning("settled markets of %s unavailable: %s", series, self.errors[series])
                raise
            self.errors.pop(series, None)
            self._cache[series] = (self._clock(), want, markets)
            self.last_fetch[series] = self._wall()
            return self._filter(markets, want)

    def _fresh(self, series: str, want: int | None) -> list[Market] | None:
        hit = self._cache.get(series)
        if hit is None or self._clock() - hit[0] >= self.ttl_s:
            return None
        _, got_since, markets = hit
        if got_since is not None and (want is None or want < got_since):
            return None  # the cached answer does not reach back far enough
        return self._filter(markets, want)

    @staticmethod
    def _filter(markets: list[Market], want: int | None) -> list[Market]:
        if want is None:
            return list(markets)
        return [m for m in markets if m.settlement_ts is None or m.settlement_ts.timestamp() >= want]

    async def _fetch(self, series: str, want: int | None) -> list[Market]:
        client = self._get_client()
        filters: dict[str, Any] = {"series_ticker": series, "status": "settled", "limit": self.page_limit}
        if want is not None:
            filters["min_settled_ts"] = want
        out: dict[str, Market] = {}
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            if cursor:
                filters["cursor"] = cursor
            self.request_count += 1
            page, cursor = await _markets_page(client, filters)
            for m in page:
                if m.ticker:
                    out[m.ticker] = m
            if not cursor:
                break
        return sorted(out.values(), key=_close_key, reverse=True)

    def status(self) -> dict[str, Any]:
        return {
            "requests": self.request_count,
            "series": {s: {"markets": len(v[2]), "fetched_at": _iso(self.last_fetch.get(s))}
                       for s, v in self._cache.items()},
            "errors": dict(self.errors),
        }
