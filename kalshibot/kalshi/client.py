"""Async client for Kalshi's public, unauthenticated market-data REST API (ARCHITECTURE.md §3).

PAPER TRADING ONLY: this client issues GET requests to public endpoints. It has no
credential handling and no order endpoints, by design.

* Token-bucket rate limiter (``max_rps``, default 3/s; the unauthenticated limit is
  undocumented and IP-shared, so stay low).
* Retries with exponential backoff + jitter on 429, 5xx, timeouts and transport errors
  (``max_tries`` attempts in total, default 5). 429s carry no ``Retry-After`` today; if one
  appears it is honoured. Each retry also goes through the rate limiter.
* Cursor pagination: an empty string or ``null`` cursor means done (both occur).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

import httpx

from kalshibot.kalshi.models import Candle, Event, Market, Orderbook, Series, Trade

__all__ = [
    "DEFAULT_BASE_URL",
    "KalshiAPIError",
    "KalshiClient",
    "KalshiNotFound",
    "KalshiRateLimited",
    "TokenBucket",
]

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
MAX_BATCH_ORDERBOOKS = 100
MAX_BATCH_CANDLE_TICKERS = 100
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

SleepFn = Callable[[float], Awaitable[Any]]


class KalshiAPIError(Exception):
    """Non-retryable API error, or retries exhausted."""

    def __init__(self, status: int | None, message: str, path: str = ""):
        super().__init__(f"{status} {path}: {message}" if status else f"{path}: {message}")
        self.status = status
        self.message = message
        self.path = path


class KalshiNotFound(KalshiAPIError):
    """HTTP 404 (unknown ticker, or a market archived behind the historical cutoff)."""


class KalshiRateLimited(KalshiAPIError):
    """Still HTTP 429 after all retries."""


class TokenBucket:
    """Async token bucket: ``rate`` tokens/s, holding at most ``capacity`` tokens.

    Callers reserve a token immediately (the balance may go negative) and then sleep
    for their share of the deficit, so concurrent callers are spaced ``1/rate`` apart
    without busy-waiting. ``clock``/``sleep`` are injectable for tests.
    """

    def __init__(
        self,
        rate: float,
        capacity: float = 1.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: SleepFn = asyncio.sleep,
    ):
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate = float(rate)
        self.capacity = max(1.0, float(capacity))
        self._clock = clock
        self._sleep = sleep
        self._tokens = self.capacity
        self._updated = clock()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> float:
        """Take ``tokens``; returns the seconds waited."""
        async with self._lock:
            now = self._clock()
            self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
            self._updated = now
            self._tokens -= tokens
            wait = -self._tokens / self.rate if self._tokens < 0 else 0.0
        if wait > 0:
            await self._sleep(wait)
        return wait


def _param(v: Any) -> Any:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, datetime):
        return int(v.timestamp())
    return v


def _clean_params(params: Mapping[str, Any] | None, *, repeat: Iterable[str] = ()) -> dict[str, Any]:
    """Drop ``None``s, stringify bools, datetimes -> epoch seconds, lists -> comma lists
    (except keys in ``repeat``, which become repeated query parameters)."""
    rep = set(repeat)
    out: dict[str, Any] = {}
    for k, v in (params or {}).items():
        if v is None:
            continue
        if isinstance(v, list | tuple | set | frozenset):
            vals = [str(_param(x)) for x in v]
            out[k] = vals if k in rep else ",".join(vals)
        else:
            out[k] = _param(v)
    return out


def _cursor(d: Mapping[str, Any]) -> str | None:
    c = d.get("cursor")
    return c or None


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    err = body.get("error") if isinstance(body, Mapping) else None
    if isinstance(err, Mapping):
        return str(err.get("message") or err.get("code") or err)
    if err:
        return str(err)
    return str(body)[:200]


def _ts(v: int | float | datetime | None) -> int | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return int(v.timestamp())
    return int(v)


class KalshiClient:
    """Async public REST client. Use as ``async with KalshiClient() as c: ...`` or call ``aclose()``."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        max_rps: float = 3.0,
        timeout: float = 15.0,
        *,
        burst: float = 1.0,
        max_tries: int = 5,
        backoff_base: float = 0.5,
        backoff_max: float = 20.0,
        user_agent: str = "kalshibot-paper/0.1 (paper trading; public market data)",
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: SleepFn = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.base_url = base_url.rstrip("/")
        self.max_tries = max(1, int(max_tries))
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._sleep = sleep
        self.limiter = TokenBucket(max_rps, burst, clock=clock, sleep=sleep)
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            transport=transport,
        )
        self.request_count = 0  # HTTP requests actually sent (incl. retries)
        self.retry_count = 0
        self.success_count = 0  # requests that returned a usable answer (incl. 404): Kalshi is reachable

    async def __aenter__(self) -> KalshiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- transport ---------------------------------------------------------------

    def _backoff(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after:
            try:
                return min(self.backoff_max, max(0.0, float(retry_after)))
            except ValueError:
                pass
        base = min(self.backoff_max, self.backoff_base * (2**attempt))
        return base * (0.5 + random.random() / 2)  # jitter in [base/2, base)

    async def get(
        self, path: str, params: Mapping[str, Any] | None = None, *, repeat: Iterable[str] = ()
    ) -> dict[str, Any]:
        """GET ``path`` (relative to base_url) and return the decoded JSON object."""
        q = _clean_params(params, repeat=repeat)
        last_status: int | None = None
        last_msg = ""
        for attempt in range(self.max_tries):
            await self.limiter.acquire()
            self.request_count += 1
            try:
                resp = await self._http.get(path, params=q)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_status, last_msg = None, f"{type(e).__name__}: {e}"
                if attempt + 1 < self.max_tries:
                    self.retry_count += 1
                    delay = self._backoff(attempt)
                    log.warning("GET %s failed (%s); retry in %.2fs", path, last_msg, delay)
                    await self._sleep(delay)
                continue
            if resp.status_code in RETRY_STATUSES:
                last_status, last_msg = resp.status_code, _error_message(resp)
                if attempt + 1 < self.max_tries:
                    self.retry_count += 1
                    delay = self._backoff(attempt, resp.headers.get("Retry-After"))
                    log.warning("GET %s -> %s; retry in %.2fs", path, resp.status_code, delay)
                    await self._sleep(delay)
                continue
            if resp.status_code == 404:
                self.success_count += 1
                raise KalshiNotFound(404, _error_message(resp), path)
            if resp.status_code >= 400:
                raise KalshiAPIError(resp.status_code, _error_message(resp), path)
            try:
                data = resp.json()
            except ValueError as e:
                raise KalshiAPIError(resp.status_code, f"invalid JSON: {e}", path) from e
            if not isinstance(data, dict):
                raise KalshiAPIError(resp.status_code, "expected a JSON object", path)
            self.success_count += 1
            return data
        if last_status == 429:
            raise KalshiRateLimited(429, last_msg or "too many requests", path)
        raise KalshiAPIError(last_status, f"failed after {self.max_tries} tries: {last_msg}", path)

    async def _paginate(
        self, path: str, params: Mapping[str, Any], key: str, max_pages: int | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        q = dict(params)
        seen: set[str] = set()
        pages = 0
        while True:
            d = await self.get(path, q)
            pages += 1
            for item in d.get(key) or ():
                yield item
            cur = _cursor(d)
            if not cur or cur in seen or (max_pages is not None and pages >= max_pages):
                return
            seen.add(cur)
            q["cursor"] = cur

    # -- markets -----------------------------------------------------------------

    async def get_markets(self, **filters: Any) -> tuple[list[Market], str | None]:
        """One page of ``GET /markets``. ``tickers`` may be a list (sent comma-separated)."""
        d = await self.get("/markets", filters)
        return [Market.from_api(m) for m in d.get("markets") or ()], _cursor(d)

    async def iter_markets(self, *, max_pages: int | None = None, **filters: Any) -> AsyncIterator[Market]:
        """All pages of ``/markets`` (limit=1000; ``mve_filter="exclude"`` unless overridden,
        pass ``mve_filter=None`` to send none)."""
        filters.setdefault("limit", 1000)
        filters.setdefault("mve_filter", "exclude")
        async for m in self._paginate("/markets", filters, "markets", max_pages):
            yield Market.from_api(m)

    async def get_market(self, ticker: str) -> Market:
        d = await self.get(f"/markets/{ticker}")
        return Market.from_api(d.get("market") or {})

    # -- events / series ---------------------------------------------------------

    async def get_events(self, **filters: Any) -> tuple[list[Event], str | None]:
        """One page of ``GET /events`` (``with_nested_markets=True`` fills ``Event.markets``)."""
        d = await self.get("/events", filters)
        return [Event.from_api(e) for e in d.get("events") or ()], _cursor(d)

    async def iter_events(self, *, max_pages: int | None = None, **filters: Any) -> AsyncIterator[Event]:
        """All pages of ``/events`` (limit=200, the endpoint maximum)."""
        filters.setdefault("limit", 200)
        async for e in self._paginate("/events", filters, "events", max_pages):
            yield Event.from_api(e)

    async def get_event(self, event_ticker: str, *, with_nested_markets: bool | None = None) -> Event:
        """``GET /events/{t}`` -> Event with ``markets`` populated from the response."""
        d = await self.get(f"/events/{event_ticker}", {"with_nested_markets": with_nested_markets})
        return Event.from_api(d)

    async def get_series(self, series_ticker: str) -> Series:
        d = await self.get(f"/series/{series_ticker}")
        return Series.from_api(d)

    # -- order books -------------------------------------------------------------

    async def get_orderbook(self, ticker: str, depth: int = 0) -> Orderbook:
        """``GET /markets/{t}/orderbook``; ``depth=0`` means all levels."""
        d = await self.get(f"/markets/{ticker}/orderbook", {"depth": depth or None})
        return Orderbook.from_api(d, ticker=ticker)

    async def get_orderbooks(self, tickers: Sequence[str]) -> dict[str, Orderbook]:
        """Batch books via ``GET /markets/orderbooks?tickers=A&tickers=B`` (chunks of 100).

        Returns ``{ticker: Orderbook}`` for the books the API returned.
        """
        out: dict[str, Orderbook] = {}
        uniq = list(dict.fromkeys(tickers))
        for i in range(0, len(uniq), MAX_BATCH_ORDERBOOKS):
            chunk = uniq[i : i + MAX_BATCH_ORDERBOOKS]
            d = await self.get("/markets/orderbooks", {"tickers": chunk}, repeat=("tickers",))
            for entry in d.get("orderbooks") or ():
                ob = Orderbook.from_api(entry)
                if ob.ticker:
                    out[ob.ticker] = ob
        return out

    # -- trades / candles --------------------------------------------------------

    async def get_trades(
        self,
        ticker: str | None = None,
        min_ts: int | float | datetime | None = None,
        limit: int = 1000,
        cursor: str | None = None,
        *,
        max_ts: int | float | datetime | None = None,
    ) -> tuple[list[Trade], str | None]:
        """One page of ``GET /markets/trades`` (newest first)."""
        d = await self.get(
            "/markets/trades",
            {"ticker": ticker, "min_ts": _ts(min_ts), "max_ts": _ts(max_ts), "limit": limit, "cursor": cursor},
        )
        return [Trade.from_api(t) for t in d.get("trades") or ()], _cursor(d)

    async def iter_trades(
        self,
        ticker: str | None = None,
        min_ts: int | float | datetime | None = None,
        *,
        max_ts: int | float | datetime | None = None,
        limit: int = 1000,
        max_pages: int | None = None,
    ) -> AsyncIterator[Trade]:
        """All trades matching the filters, newest first."""
        params = {"ticker": ticker, "min_ts": _ts(min_ts), "max_ts": _ts(max_ts), "limit": limit}
        async for t in self._paginate("/markets/trades", params, "trades", max_pages):
            yield Trade.from_api(t)

    async def get_candlesticks(
        self,
        series: str,
        ticker: str,
        start_ts: int | float | datetime,
        end_ts: int | float | datetime,
        period: int = 60,
    ) -> list[Candle]:
        """``GET /series/{s}/markets/{t}/candlesticks``; ``period`` in minutes (1, 60 or 1440)."""
        d = await self.get(
            f"/series/{series}/markets/{ticker}/candlesticks",
            {"start_ts": _ts(start_ts), "end_ts": _ts(end_ts), "period_interval": period},
        )
        return [Candle.from_api(c) for c in d.get("candlesticks") or ()]

    async def get_candlesticks_batch(
        self,
        tickers: Sequence[str],
        start_ts: int | float | datetime,
        end_ts: int | float | datetime,
        period: int = 60,
    ) -> dict[str, list[Candle]]:
        """Batch ``GET /markets/candlesticks?market_tickers=A,B`` (comma list, chunks of 100;
        the API caps a response at 10,000 candles). Returns ``{ticker: [Candle, ...]}``."""
        out: dict[str, list[Candle]] = {}
        uniq = list(dict.fromkeys(tickers))
        for i in range(0, len(uniq), MAX_BATCH_CANDLE_TICKERS):
            chunk = uniq[i : i + MAX_BATCH_CANDLE_TICKERS]
            d = await self.get(
                "/markets/candlesticks",
                {"market_tickers": chunk, "start_ts": _ts(start_ts), "end_ts": _ts(end_ts),
                 "period_interval": period},
            )
            for entry in d.get("markets") or ():
                t = entry.get("market_ticker") or entry.get("ticker")
                if t:
                    out[t] = [Candle.from_api(c) for c in entry.get("candlesticks") or ()]
        return out

    async def get_historical_candlesticks(
        self,
        ticker: str,
        start_ts: int | float | datetime,
        end_ts: int | float | datetime,
        period: int = 60,
    ) -> list[Candle]:
        """``GET /historical/markets/{t}/candlesticks`` (markets settled before the cutoff)."""
        d = await self.get(
            f"/historical/markets/{ticker}/candlesticks",
            {"start_ts": _ts(start_ts), "end_ts": _ts(end_ts), "period_interval": period},
        )
        return [Candle.from_api(c) for c in d.get("candlesticks") or ()]

    # -- exchange ----------------------------------------------------------------

    async def get_event_fee_changes(self, event_ticker: str | None = None, *, max_pages: int = 20
                                    ) -> list[dict[str, Any]]:
        """``GET /events/fee_changes`` (upcoming per-event fee overrides; all pages)."""
        out: list[dict[str, Any]] = []
        async for row in self._paginate("/events/fee_changes", {"event_ticker": event_ticker, "limit": 1000},
                                        "event_fee_changes", max_pages):
            if isinstance(row, Mapping):
                out.append(dict(row))
        return out

    async def get_series_fee_changes(self, *, show_historical: bool = False) -> list[dict[str, Any]]:
        """``GET /series/fee_changes`` (upcoming series fee changes; all when ``show_historical``)."""
        d = await self.get("/series/fee_changes", {"show_historical": show_historical or None})
        return [dict(r) for r in d.get("series_fee_change_arr") or () if isinstance(r, Mapping)]

    async def get_exchange_status(self) -> dict[str, Any]:
        """``GET /exchange/status`` (``trading_active``, per-shard ``exchange_index_statuses``)."""
        return await self.get("/exchange/status")

    async def get_historical_cutoff(self) -> dict[str, Any]:
        """``GET /historical/cutoff`` (live vs historical data boundary timestamps)."""
        return await self.get("/historical/cutoff")
