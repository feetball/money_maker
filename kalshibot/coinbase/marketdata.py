"""SpotMarketData: cached public Coinbase market data for the engine, broker and API (contract §10).

PAPER TRADING ONLY - public, unauthenticated REST (``kalshibot.coinbase.client``); nothing
here places orders or uses keys. One instance per venue, shared by the engine, the paper
broker (it implements the broker's ``SpotMarketDataProvider``: ``book``, ``trades_since``,
``product``, ``clock``) and the ``/api/coinbase/*`` routes.

What is cached, and for how long
    * **products** (``GET /products``): refreshed by the engine every
      ``coinbase.engine.products_refresh_s`` (1 h); a product not in the cache is fetched on
      demand (``GET /products/{id}``).
    * **24 h stats** of every product in one call (``GET /products/stats``, ~110 KB), at most
      every ``stats_ttl_s`` (60 s): price, 24 h change, 24 h volume for the products page and
      ``ctx.stats``.
    * **level-2 books** per product for ``max_age_s`` (the caller's freshness bound; 0 = fetch
      now). A BTC-USD L2 book is ~355 KB / ~40k levels, so books are parsed in a worker
      thread (never on the event loop the Kalshi venue shares) and trimmed to the levels
      within ``book_band_pct`` (15 %) of the best price (at least ``book_min_levels`` per
      side) - far beyond the paper broker's slippage cap (1 %) and mark depth. Unused books
      are dropped after ``book_keep_s``.
    * **quotes** (best bid / ask) from every book seen plus cheap level-1 books fetched for
      the products page (:meth:`refresh_quotes`).
    * **candles** per ``(product, granularity)``: :meth:`candles` returns the last ``n``
      CLOSED bars with ``end <= bar_end`` (oldest first). The first call fetches the whole
      lookback; later calls fetch from the newest cached bar on (one bar of overlap, so a bar
      cached while Coinbase was still aggregating it is replaced by its final version at no
      extra request). A newest bar fetched less than ``PROVISIONAL_S`` (120 s) after its end is
      provisional: another call for the same ``bar_end`` at least ``PROVISIONAL_REFETCH_S``
      (15 s) later re-fetches it; a final bar is never re-fetched. At most
      ``n + candle_slack`` bars are kept per series.

``reachable`` is ``None`` until the first request, then whether the last request that
reached a verdict got an answer from Coinbase (network errors, timeouts, 5xx/429 after
retries -> ``False``).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any, TypeVar

import httpx

from kalshibot.coinbase.client import CoinbaseAPIError, CoinbaseClient
from kalshibot.coinbase.models import (
    BookLevel,
    Candle,
    OrderBook,
    Product,
    Stats,
    Trade,
    dec,
    parse_time,
)

__all__ = ["PROVISIONAL_REFETCH_S", "PROVISIONAL_S", "SpotMarketData", "is_network_error", "parse_book",
           "trim_book"]

#: a candle fetched less than this long after its end may still be missing its last trades
#: (Coinbase aggregates the newest bucket with up to ~60 s lag)
PROVISIONAL_S = 120.0
#: minimum spacing of re-fetches of a provisional final bar
PROVISIONAL_REFETCH_S = 15.0

#: book parsing runs here, not in asyncio's shared default executor (which also resolves DNS
#: for the Kalshi venue's HTTP clients); parsing is short and never blocks on I/O
_PARSE_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cb-parse")

log = logging.getLogger(__name__)

T = TypeVar("T")

_HUNDRED = Decimal(100)


def is_network_error(e: BaseException) -> bool:
    """Coinbase unreachable / overloaded (retry later) rather than a bad request."""
    if isinstance(e, CoinbaseAPIError):
        return e.status is None or e.status == 429 or e.status >= 500
    return isinstance(e, httpx.HTTPError | OSError | TimeoutError)


def _levels(rows: Any, *, descending: bool, band: Decimal | None, min_levels: int) -> tuple[BookLevel, ...]:
    """Parse ``[[price, size, num_orders], ...]`` (API order: best first), keeping the levels
    within ``band`` (a fraction) of the best price, and at least ``min_levels`` levels."""
    out: list[BookLevel] = []
    limit: Decimal | None = None
    for row in rows or ():
        try:
            p, s = dec(row[0]), dec(row[1])
        except (IndexError, TypeError):
            continue
        if p is None or s is None or p <= 0 or s <= 0:
            continue
        if band is not None:
            if limit is None:
                limit = p * (1 - band) if descending else p * (1 + band)
            elif len(out) >= min_levels and ((descending and p < limit) or (not descending and p > limit)):
                break
        n = 0
        if len(row) > 2:
            with contextlib.suppress(TypeError, ValueError):
                n = int(row[2])
        out.append(BookLevel(p, s, n))
    out.sort(key=lambda lv: lv.price, reverse=descending)
    return tuple(out)


def parse_book(product_id: str, raw: Mapping[str, Any], *, band_pct: float | None = 15.0,
               min_levels: int = 200) -> OrderBook:
    """``GET /products/{id}/book`` JSON -> :class:`OrderBook` (trimmed; the raw payload is not kept)."""
    band = Decimal(str(band_pct)) / _HUNDRED if band_pct is not None else None
    seq = raw.get("sequence")
    return OrderBook(
        product_id=product_id,
        bids=_levels(raw.get("bids"), descending=True, band=band, min_levels=min_levels),
        asks=_levels(raw.get("asks"), descending=False, band=band, min_levels=min_levels),
        sequence=int(seq) if isinstance(seq, int | str) and str(seq).isdigit() else None,
        time=parse_time(raw.get("time")),
    )


def trim_book(book: OrderBook, *, band_pct: float | None = 15.0, min_levels: int = 200) -> OrderBook:
    """Drop levels beyond ``band_pct`` of the best price (keeping ``min_levels``) and the raw payload."""
    if band_pct is None:
        return dataclasses.replace(book, raw={})
    band = Decimal(str(band_pct)) / _HUNDRED

    def cut(levels: tuple[BookLevel, ...], descending: bool) -> tuple[BookLevel, ...]:
        if len(levels) <= min_levels:
            return levels
        best = levels[0].price
        lim = best * (1 - band) if descending else best * (1 + band)
        keep = [lv for i, lv in enumerate(levels)
                if i < min_levels or (lv.price >= lim if descending else lv.price <= lim)]
        return tuple(keep)

    return dataclasses.replace(book, bids=cut(book.bids, True), asks=cut(book.asks, False), raw={})


def _stats_from_bulk(pid: str, d: Mapping[str, Any]) -> Stats | None:
    s24 = d.get("stats_24hour") if isinstance(d, Mapping) else None
    if not isinstance(s24, Mapping):
        return None
    s30 = d.get("stats_30day") if isinstance(d.get("stats_30day"), Mapping) else {}
    return Stats.from_api(pid, {**s24, "volume_30day": s30.get("volume")})


class SpotMarketData:
    """Cached public market data (see the module docstring). ``client``: a
    :class:`~kalshibot.coinbase.client.CoinbaseClient` (or a fake with the same methods)."""

    def __init__(
        self,
        client: Any,
        settings: Any = None,
        *,
        clock: Callable[[], datetime] | None = None,
        mono: Callable[[], float] = time.monotonic,
        stats_ttl_s: float = 60.0,
        book_band_pct: float | None = 15.0,
        book_min_levels: int = 200,
        book_keep_s: float = 120.0,
        quote_keep_s: float = 3600.0,
        candle_slack: int = 5,
        trades_max_pages: int = 5,
        parse_in_thread: bool = True,
    ) -> None:
        self.client = client
        self.settings = settings
        self.clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self.mono = mono
        self.stats_ttl_s = float(stats_ttl_s)
        self.book_band_pct = book_band_pct
        self.book_min_levels = int(book_min_levels)
        self.book_keep_s = float(book_keep_s)
        self.quote_keep_s = float(quote_keep_s)
        self.candle_slack = int(candle_slack)
        self.trades_max_pages = int(trades_max_pages)
        self.parse_in_thread = parse_in_thread

        self._products: dict[str, Product] = {}
        self.products_refreshed_at: datetime | None = None
        self._stats: dict[str, Stats] = {}
        self.stats_refreshed_at: datetime | None = None
        self._stats_mono: float | None = None
        self._stats_lock = asyncio.Lock()
        self._books: dict[str, tuple[float, OrderBook]] = {}
        self._book_locks: dict[str, asyncio.Lock] = {}
        #: pid -> (as of, best bid, best ask)
        self._quotes: dict[str, tuple[datetime, Decimal | None, Decimal | None]] = {}
        self._quote_task: asyncio.Task[Any] | None = None
        self._candles: dict[tuple[str, int], list[Candle]] = {}
        #: earliest bar start already requested per series (history before it does not exist)
        self._candle_from: dict[tuple[str, int], datetime] = {}
        #: per series: (start of the newest cached bar, when it was fetched)
        self._candle_seen: dict[tuple[str, int], tuple[datetime, datetime]] = {}

        self.reachable: bool | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.last_ok_at: datetime | None = None

    # ------------------------------------------------------------------ plumbing

    async def _net(self, fn: Callable[..., Awaitable[T]], *args: Any, **kw: Any) -> T:
        """Call the client, tracking reachability."""
        try:
            out = await fn(*args, **kw)
        except Exception as e:
            if is_network_error(e):
                self.reachable = False
            elif isinstance(e, CoinbaseAPIError):
                self.reachable = True  # Coinbase answered (a 4xx)
            self.last_error = f"{type(e).__name__}: {e}"
            self.last_error_at = self.clock()
            raise
        self.reachable = True
        self.last_ok_at = self.clock()
        return out

    async def _parse(self, fn: Callable[..., T], *args: Any, **kw: Any) -> T:
        if self.parse_in_thread:
            return await asyncio.get_running_loop().run_in_executor(_PARSE_POOL, functools.partial(fn, *args, **kw))
        return fn(*args, **kw)

    # ------------------------------------------------------------------ products

    async def refresh_products(self) -> int:
        """Reload every product (``GET /products``). Returns the number of tradable USD products."""
        products = await self._net(self.client.get_products)
        self._products = {p.product_id: p for p in products}
        self.products_refreshed_at = self.clock()
        return self.products_loaded

    @property
    def products(self) -> Mapping[str, Product]:
        """Every product loaded (all quote currencies and statuses), read-only."""
        return MappingProxyType(self._products)

    def usd_products(self, *, tradable_only: bool = True) -> dict[str, Product]:
        """``{product_id: Product}`` of the USD-quoted products (tradable ones by default), sorted."""
        return {pid: p for pid, p in sorted(self._products.items())
                if p.quote_currency.upper() == "USD" and (p.tradable or not tradable_only)}

    @property
    def products_loaded(self) -> int:
        return sum(1 for p in self._products.values() if p.quote_currency.upper() == "USD" and p.tradable)

    def known_product(self, product_id: str) -> Product | None:
        return self._products.get(product_id)

    async def product(self, product_id: str) -> Product:
        """The cached product, fetched on demand when unknown (broker protocol)."""
        p = self._products.get(product_id)
        if p is None:
            p = await self._net(self.client.get_product, product_id)
            self._products[product_id] = p
        return p

    # ------------------------------------------------------------------ stats

    async def refresh_stats(self, max_age_s: float | None = None) -> dict[str, Stats]:
        """24 h stats of every product in one request (cached ``stats_ttl_s``)."""
        ttl = self.stats_ttl_s if max_age_s is None else float(max_age_s)
        async with self._stats_lock:
            if self._stats_mono is not None and self.mono() - self._stats_mono <= ttl and self._stats:
                return self._stats
            getter = getattr(self.client, "get", None)
            if callable(getter):
                raw = await self._net(getter, "/products/stats")
                stats: dict[str, Stats] = {}
                if isinstance(raw, Mapping):
                    for pid, d in raw.items():
                        with contextlib.suppress(Exception):
                            s = _stats_from_bulk(str(pid), d)
                            if s is not None:
                                stats[str(pid)] = s
            else:  # a fake client without raw GET: per-product stats of the loaded USD products
                stats = {}
                for pid in list(self.usd_products())[:50]:
                    with contextlib.suppress(Exception):
                        stats[pid] = await self._net(self.client.get_stats, pid)
            self._stats = stats
            self._stats_mono = self.mono()
            self.stats_refreshed_at = self.clock()
            return self._stats

    def stats(self, product_id: str) -> Stats | None:
        return self._stats.get(product_id)

    # ------------------------------------------------------------------ books / quotes

    def _lock(self, product_id: str) -> asyncio.Lock:
        lk = self._book_locks.get(product_id)
        if lk is None:
            lk = self._book_locks[product_id] = asyncio.Lock()
        return lk

    def _note_quote(self, book: OrderBook) -> None:
        self._quotes[book.product_id] = (book.time or self.clock(), book.best_bid, book.best_ask)

    async def _fetch_book(self, product_id: str, level: int = 2) -> OrderBook:
        if isinstance(self.client, CoinbaseClient):
            raw = await self._net(self.client.get, f"/products/{product_id}/book", {"level": level})
            if not isinstance(raw, Mapping):
                raise CoinbaseAPIError(200, "expected a JSON object", f"/products/{product_id}/book")
            return await self._parse(parse_book, product_id, raw, band_pct=self.book_band_pct,
                                     min_levels=self.book_min_levels)
        book = await self._net(self.client.get_book, product_id, level)
        return trim_book(book, band_pct=self.book_band_pct, min_levels=self.book_min_levels)

    async def book(self, product_id: str, max_age_s: float = 2) -> OrderBook:
        """Level-2 book no older than ``max_age_s`` seconds (0 = fetch now). Broker protocol."""
        async with self._lock(product_id):
            hit = self._books.get(product_id)
            if hit is not None and max_age_s > 0 and self.mono() - hit[0] <= max_age_s:
                return hit[1]
            book = await self._fetch_book(product_id, 2)
            self._books[product_id] = (self.mono(), book)
            self._note_quote(book)
            return book

    def known_book(self, product_id: str, max_age_s: float | None = None) -> OrderBook | None:
        """The cached book (no request); ``None`` if absent or older than ``max_age_s``."""
        hit = self._books.get(product_id)
        if hit is None or (max_age_s is not None and self.mono() - hit[0] > max_age_s):
            return None
        return hit[1]

    def quote(self, product_id: str) -> tuple[datetime, Decimal | None, Decimal | None] | None:
        """``(as_of, best_bid, best_ask)`` from the latest book seen, if any."""
        return self._quotes.get(product_id)

    async def refresh_quotes(self, product_ids: Iterable[str], *, max_age_s: float = 60.0,
                             max_requests: int = 10) -> int:
        """Fetch level-1 books for up to ``max_requests`` of ``product_ids`` whose quote is
        older than ``max_age_s`` (cheap: ~200 bytes each). Returns the number fetched."""
        now = self.clock()
        n = 0
        for pid in product_ids:
            if n >= max_requests:
                break
            q = self._quotes.get(pid)
            if q is not None and (now - q[0]).total_seconds() <= max_age_s:
                continue
            try:
                book = await self._fetch_book(pid, 1)
            except Exception as e:
                log.info("coinbase quote for %s unavailable: %s", pid, e)
                if is_network_error(e):
                    break
                continue
            n += 1
            self._quotes[pid] = (now, book.best_bid, book.best_ask)
        return n

    def refresh_quotes_soon(self, product_ids: Iterable[str], **kw: Any) -> None:
        """Background :meth:`refresh_quotes` (one at a time; never blocks the caller)."""
        if self._quote_task is not None and not self._quote_task.done():
            return
        ids = list(product_ids)
        if not ids:
            return
        try:
            self._quote_task = asyncio.get_running_loop().create_task(self.refresh_quotes(ids, **kw),
                                                                      name="coinbase-quotes")
        except RuntimeError:
            return

    # ------------------------------------------------------------------ trades

    async def trades_since(self, product_id: str, since_trade_id: int | None) -> list[Trade]:
        """Public trades after ``since_trade_id`` (oldest first); ``None`` = the newest page. Broker protocol."""
        return await self._net(self.client.trades_since, product_id, since_trade_id, self.trades_max_pages)

    # ------------------------------------------------------------------ candles

    async def candles(self, product_id: str, granularity_s: int, n: int, *, bar_end: datetime) -> list[Candle]:
        """The last ``n`` closed bars with ``end <= bar_end``, oldest first (see the module doc)."""
        g = int(granularity_s)
        n = max(1, int(n))
        key = (product_id, g)
        step = timedelta(seconds=g)
        last_needed = bar_end - step  # open time of the bar that closed at bar_end
        want_from = bar_end - step * n
        cached = self._candles.get(key) or []
        floor = self._candle_from.get(key)
        incremental = bool(cached) and floor is not None and floor <= want_from
        now = self.clock()
        fetch_from: datetime | None
        if incremental:
            newest = cached[-1]
            if newest.start < last_needed:
                fetch_from = newest.start  # one bar of overlap: replaces a provisional bar for free
            elif newest.start == last_needed and self._provisional(key, newest, now):
                fetch_from = newest.start
            else:
                fetch_from = None
        else:
            fetch_from, cached = want_from, []
        if fetch_from is not None and fetch_from <= last_needed:
            new = await self._net(self.client.get_candles, product_id, g, fetch_from, last_needed)
            merged = {c.start: c for c in cached}
            for c in new:
                if c.end <= bar_end:
                    merged[c.start] = c
            cached = [merged[k] for k in sorted(merged)]
            if cached and any(c.start == cached[-1].start for c in new):
                self._candle_seen[key] = (cached[-1].start, now)
            if not incremental:
                self._candle_from[key] = want_from
        keep = n + self.candle_slack
        if len(cached) > keep:
            cached = cached[-keep:]
            self._candle_from[key] = cached[0].start  # older history was dropped
        self._candles[key] = cached
        out = [c for c in cached if c.end <= bar_end]
        return out[-n:]

    def _provisional(self, key: tuple[str, int], bar: Candle, now: datetime) -> bool:
        """``bar`` (the newest cached) was fetched soon after its end and is due a re-fetch."""
        seen = self._candle_seen.get(key)
        if seen is None or seen[0] != bar.start:
            return False
        fetched = seen[1]
        return ((fetched - bar.end).total_seconds() < PROVISIONAL_S
                and (now - fetched).total_seconds() >= PROVISIONAL_REFETCH_S)

    def cached_candles(self, product_id: str, granularity_s: int) -> list[Candle]:
        return list(self._candles.get((product_id, int(granularity_s))) or [])

    # ------------------------------------------------------------------ housekeeping / status

    def gc(self) -> None:
        """Drop books not used for ``book_keep_s`` and quotes older than ``quote_keep_s``."""
        now_m = self.mono()
        for pid in [p for p, (t, _) in self._books.items() if now_m - t > self.book_keep_s]:
            del self._books[pid]
            lk = self._book_locks.get(pid)
            if lk is not None and not lk.locked():
                del self._book_locks[pid]
        now = self.clock()
        for pid in [p for p, q in self._quotes.items() if (now - q[0]).total_seconds() > self.quote_keep_s]:
            del self._quotes[pid]

    def status(self) -> dict[str, Any]:
        c = self.client
        return {
            "products_loaded": self.products_loaded,
            "products_total": len(self._products),
            "products_refreshed_at": self.products_refreshed_at,
            "stats_refreshed_at": self.stats_refreshed_at,
            "books_cached": len(self._books),
            "candle_series": len(self._candles),
            "reachable": self.reachable,
            "last_ok_at": self.last_ok_at,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "requests": getattr(c, "request_count", None),
            "retries": getattr(c, "retry_count", None),
            "max_rps": getattr(getattr(c, "limiter", None), "rate", None),
        }
