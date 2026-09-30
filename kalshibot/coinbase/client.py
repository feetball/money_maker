"""Async client for Coinbase Exchange's public market-data REST API (docs/COINBASE_CONTRACT.md §4).

PAPER TRADING ONLY: GET requests to public, unauthenticated endpoints. No credential
handling and no order endpoints, by design.

* Token-bucket rate limiter (``max_rps``, default 3/s; Coinbase's public limit is about
  10 req/s per IP, shared with everything else on this host). The bucket is
  :class:`kalshibot.kalshi.client.TokenBucket` (imported, not modified).
* Retries with exponential backoff + jitter on 429, 5xx, timeouts and transport errors
  (``max_tries`` attempts in total, default 5); a ``Retry-After`` header is honoured.
  Every retry goes through the rate limiter again.
* Trades paginate with the ``cb-after`` response header: pass it back as ``after=`` to
  get the next OLDER page (verified live 2026-09-27: page 1 ids 1099147579..575 with
  ``cb-after: 1099147575``; ``after=1099147575`` returned 574..570).
* Candles: the API returns newest first, at most ~300 bars per request, and its
  ``[start, end]`` window is inclusive at both ends (a 24 h window of 1 h bars returned
  25 bars). :meth:`CoinbaseClient.get_candles` requests windows of <= 300 bars and
  returns them oldest first, deduplicated by bar start. Bars without trades are simply
  absent (Coinbase does not send empty bars). The in-progress bar is included unless
  ``closed_only=True``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from kalshibot.coinbase.models import Candle, OrderBook, Product, Stats, Ticker, Trade, parse_time
from kalshibot.kalshi.client import TokenBucket

__all__ = [
    "CANDLE_GRANULARITIES",
    "DEFAULT_BASE_URL",
    "MAX_CANDLES_PER_REQUEST",
    "CoinbaseAPIError",
    "CoinbaseClient",
    "CoinbaseNotFound",
    "CoinbaseRateLimited",
    "TokenBucket",
]

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.exchange.coinbase.com"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
#: granularities (seconds) the candles endpoint accepts
CANDLE_GRANULARITIES = frozenset({60, 300, 900, 3600, 21600, 86400})
MAX_CANDLES_PER_REQUEST = 300
MAX_TRADES_PER_PAGE = 1000

SleepFn = Callable[[float], Awaitable[Any]]
TimeArg = datetime | int | float


class CoinbaseAPIError(Exception):
    """Non-retryable API error, or retries exhausted."""

    def __init__(self, status: int | None, message: str, path: str = ""):
        super().__init__(f"{status} {path}: {message}" if status else f"{path}: {message}")
        self.status = status
        self.message = message
        self.path = path


class CoinbaseNotFound(CoinbaseAPIError):
    """HTTP 404 (unknown product id)."""


class CoinbaseRateLimited(CoinbaseAPIError):
    """Still HTTP 429 after all retries."""


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, Mapping) and body.get("message"):
        return str(body["message"])
    return str(body)[:200]


def _as_datetime(v: TimeArg) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    return datetime.fromtimestamp(float(v), tz=UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class CoinbaseClient:
    """Async public REST client. Use as ``async with CoinbaseClient() as c: ...`` or ``aclose()``.

    Counters for status pages: ``request_count`` (HTTP requests sent, incl. retries),
    ``retry_count``, ``success_count`` (usable answers incl. 404 - Coinbase reachable),
    ``last_success_at`` / ``last_error`` / ``last_error_at``.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        max_rps: float = 3.0,
        timeout: float = 10.0,
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
        # Coinbase rejects requests without a User-Agent.
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            transport=transport,
        )
        self.request_count = 0
        self.retry_count = 0
        self.success_count = 0
        self.last_success_at: datetime | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None

    @classmethod
    def from_settings(cls, cb: Any, **kwargs: Any) -> CoinbaseClient:
        """Build from ``settings.coinbase`` (``base_url``, ``max_rps``, ``timeout``);
        ``kwargs`` (e.g. ``transport``) are passed through."""
        return cls(getattr(cb, "base_url", DEFAULT_BASE_URL), max_rps=float(getattr(cb, "max_rps", 3.0)),
                   timeout=float(getattr(cb, "timeout", 10.0)), **kwargs)

    async def __aenter__(self) -> CoinbaseClient:
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

    def _ok(self) -> None:
        self.success_count += 1
        self.last_success_at = datetime.now(UTC)

    def _fail(self, msg: str) -> None:
        self.last_error = msg
        self.last_error_at = datetime.now(UTC)

    async def request(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> tuple[Any, httpx.Headers]:
        """GET ``path`` (relative to base_url) -> (decoded JSON, response headers)."""
        q = {k: v for k, v in (params or {}).items() if v is not None}
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
                    log.warning("coinbase GET %s failed (%s); retry in %.2fs", path, last_msg, delay)
                    await self._sleep(delay)
                continue
            if resp.status_code in RETRY_STATUSES:
                last_status, last_msg = resp.status_code, _error_message(resp)
                if attempt + 1 < self.max_tries:
                    self.retry_count += 1
                    delay = self._backoff(attempt, resp.headers.get("Retry-After"))
                    log.warning("coinbase GET %s -> %s; retry in %.2fs", path, resp.status_code, delay)
                    await self._sleep(delay)
                continue
            if resp.status_code == 404:
                self._ok()
                raise CoinbaseNotFound(404, _error_message(resp), path)
            if resp.status_code >= 400:
                err = CoinbaseAPIError(resp.status_code, _error_message(resp), path)
                self._fail(str(err))
                raise err
            try:
                data = resp.json()
            except ValueError as e:
                err = CoinbaseAPIError(resp.status_code, f"invalid JSON: {e}", path)
                self._fail(str(err))
                raise err from e
            self._ok()
            return data, resp.headers
        final = (CoinbaseRateLimited(429, last_msg or "too many requests", path) if last_status == 429
                 else CoinbaseAPIError(last_status, f"failed after {self.max_tries} tries: {last_msg}", path))
        self._fail(str(final))
        raise final

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """GET ``path`` and return the decoded JSON (object or list)."""
        data, _ = await self.request(path, params)
        return data

    async def _get_obj(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        d = await self.get(path, params)
        if not isinstance(d, dict):
            raise CoinbaseAPIError(200, "expected a JSON object", path)
        return d

    async def _get_list(self, path: str, params: Mapping[str, Any] | None = None) -> tuple[list[Any], httpx.Headers]:
        d, headers = await self.request(path, params)
        if not isinstance(d, list):
            raise CoinbaseAPIError(200, "expected a JSON array", path)
        return d, headers

    # -- products ----------------------------------------------------------------

    async def get_products(self) -> list[Product]:
        """``GET /products``: every product (all quote currencies and statuses; ~840 today)."""
        rows, _ = await self._get_list("/products")
        out: list[Product] = []
        for r in rows:
            if isinstance(r, Mapping) and r.get("id"):
                try:
                    out.append(Product.from_api(r))
                except (KeyError, TypeError, ValueError) as e:  # one odd row must not hide the rest
                    log.warning("coinbase: skipping unparsable product %r: %s", r.get("id"), e)
        return out

    async def get_product(self, pid: str) -> Product:
        return Product.from_api(await self._get_obj(f"/products/{pid}"))

    # -- market data -------------------------------------------------------------

    async def get_book(self, pid: str, level: int = 2) -> OrderBook:
        """``GET /products/{pid}/book?level=`` (1 = best bid/ask, 2 = aggregated full depth)."""
        return OrderBook.from_api(pid, await self._get_obj(f"/products/{pid}/book", {"level": level}))

    async def get_ticker(self, pid: str) -> Ticker:
        return Ticker.from_api(pid, await self._get_obj(f"/products/{pid}/ticker"))

    async def get_stats(self, pid: str) -> Stats:
        """24 h stats (``open/high/low/last/volume`` + ``volume_30day``)."""
        return Stats.from_api(pid, await self._get_obj(f"/products/{pid}/stats"))

    async def get_time(self) -> datetime | None:
        """``GET /time`` (server clock; a cheap reachability probe)."""
        d = await self._get_obj("/time")
        return parse_time(d.get("iso")) or parse_time(d.get("epoch"))

    # -- trades ------------------------------------------------------------------

    async def get_trades(
        self, pid: str, *, after: str | int | None = None, limit: int = 100
    ) -> tuple[list[Trade], str | None]:
        """One page of ``GET /products/{pid}/trades``, newest first.

        Returns ``(trades, cursor)``; ``cursor`` is the ``cb-after`` header: pass it as
        ``after=`` for the next OLDER page. ``None`` when the page is empty.
        ``Trade.maker_side`` is the API's ``side`` (the resting order's side).
        """
        limit = max(1, min(MAX_TRADES_PER_PAGE, int(limit)))
        rows, headers = await self._get_list(f"/products/{pid}/trades", {"after": after, "limit": limit})
        trades: list[Trade] = []
        for r in rows:
            try:
                trades.append(Trade.from_api(pid, r))
            except (KeyError, TypeError, ValueError) as e:
                log.warning("coinbase: skipping unparsable %s trade %r: %s", pid, r, e)
        cursor = headers.get("cb-after") if trades else None
        return trades, (cursor or None)

    async def trades_since(
        self, pid: str, since_trade_id: int | None, max_pages: int = 5, *, limit: int = MAX_TRADES_PER_PAGE
    ) -> list[Trade]:
        """Trades with ``trade_id > since_trade_id``, **oldest first** (ascending id, deduped).

        Pages back from the newest trade (``after=`` cursor) until a page reaches
        ``since_trade_id + 1`` - ids are per product and contiguous (notes §1.6) - or the
        tape ends, or ``max_pages`` pages were read (then the oldest missing trades are not
        returned and a warning is logged). ``since_trade_id=None`` -> just the newest page
        (a starting point for a new high-water mark). Note the first page can be up to ~6 s
        old (CDN cache).
        """
        seen: dict[int, Trade] = {}
        after: str | None = None
        reached = since_trade_id is None
        cursors: set[str] = set()
        for _ in range(max(1, int(max_pages))):
            page, cursor = await self.get_trades(pid, after=after, limit=limit)
            for t in page:
                if since_trade_id is None or t.trade_id > since_trade_id:
                    seen.setdefault(t.trade_id, t)
            # trade ids are per product and contiguous: a page reaching since + 1 closes the gap
            if since_trade_id is None or not page or min(t.trade_id for t in page) <= since_trade_id + 1:
                reached = True
                break
            if not cursor or cursor in cursors:
                reached = True  # the tape ended
                break
            cursors.add(cursor)
            after = cursor
        if not reached:
            log.warning("coinbase: %s trades_since(%s) stopped after %d pages; older trades skipped",
                        pid, since_trade_id, max_pages)
        return sorted(seen.values(), key=lambda t: t.trade_id)

    # -- candles -----------------------------------------------------------------

    async def get_candles(
        self,
        pid: str,
        granularity_s: int,
        start: TimeArg,
        end: TimeArg,
        *,
        closed_only: bool = False,
    ) -> list[Candle]:
        """Bars of ``granularity_s`` seconds whose OPEN time is in ``[start, end]``, oldest
        first, deduplicated by bar start. ``start`` is aligned down to the granularity.
        Long ranges are fetched in windows of <= 300 bars (one request each).
        ``closed_only`` drops the in-progress bar (``end`` in the future)."""
        g = int(granularity_s)
        if g not in CANDLE_GRANULARITIES:
            raise ValueError(f"unsupported granularity {granularity_s}; one of {sorted(CANDLE_GRANULARITIES)}")
        t0, t1 = _as_datetime(start), _as_datetime(end)
        if t1 < t0:
            return []
        epoch0 = int(t0.timestamp()) // g * g
        t0 = datetime.fromtimestamp(epoch0, tz=UTC)
        step = timedelta(seconds=g)
        bars: dict[datetime, Candle] = {}
        chunk_start = t0
        while chunk_start <= t1:
            # the API window is inclusive at both ends: 300 bar starts = start .. start + 299 g
            chunk_end = min(t1, chunk_start + step * (MAX_CANDLES_PER_REQUEST - 1))
            rows, _ = await self._get_list(
                f"/products/{pid}/candles",
                {"granularity": g, "start": _iso(chunk_start), "end": _iso(chunk_end)},
            )
            for row in rows:
                try:
                    c = Candle.from_api(pid, g, row)
                except (TypeError, ValueError, IndexError) as e:
                    log.warning("coinbase: skipping unparsable %s candle %r: %s", pid, row, e)
                    continue
                if c.open is None or c.close is None:
                    continue
                if t0 <= c.start <= t1:
                    bars.setdefault(c.start, c)
            chunk_start = chunk_start + step * MAX_CANDLES_PER_REQUEST
        out = [bars[k] for k in sorted(bars)]
        if closed_only:
            now = datetime.now(UTC)
            out = [c for c in out if c.end <= now]
        return out
