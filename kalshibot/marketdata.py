"""MarketDataService (ARCHITECTURE.md §4): universe cache, order-book cache, trade tape.

Implements the paper broker's :class:`~kalshibot.paper.broker.MarketDataProvider`
(``orderbook``, ``trades_since``, ``series``, ``market``) plus the optional ``event`` and
``exchange_status`` hooks, on top of :class:`~kalshibot.kalshi.client.KalshiClient`
(public REST, rate limited by the client's token bucket).

Universe
--------
The open universe is > 120,000 markets, so it is never scanned in full. Each enabled
strategy declares a :class:`~kalshibot.strategies.base.UniverseSpec`; the universe is the
union of:

* ``max_days_to_close``: ``GET /markets`` with **no status**, ``min_close_ts=now``,
  ``max_close_ts=now + N*86400``, ``mve_filter=exclude`` (the close-ts filters only
  combine with an empty status), for the widest window of all specs. The API lists a window
  **latest close first**, so the window is read in ascending chunks (half days up to a day, then
  whole days: :func:`window_chunks`) that share one page budget, nearest first: a capped scan
  loses the far end of the window, never the markets closing soonest.
  Measured live 2026-09-26: a 24 h window is > 60 pages of 1000 (~50 s at 2 req/s) of
  which only ~3% are ``active`` (the rest are pre-created ``initialized`` crypto
  markets). So raw items are filtered by status *before* parsing, the number of pages
  is capped (``engine.universe_max_pages``, default 150, for all chunks together; truncation
  is reported; a 3-day window was 116 pages on 2026-09-27, most of them not-yet-open crypto
  markets closing within the next day), and
  when the window is dominated by a few series, the full scan runs only every
  ``engine.universe_window_rescan_s`` (900 s) or when the window grows; refreshes in
  between re-read just those series (``series_ticker=…&status=open``, filtered to the
  window), and a brand-new series inside the window is picked up by the next full scan.
  (Measured: a 12 h window was ~35 pages, 3,600 active markets in 122 series; there the
  full scan is cheaper in requests and is simply repeated.)
* ``series_tickers``: ``GET /markets?series_ticker=…&status=open`` per series.

Series-scoped refresh: :meth:`MarketDataService.refresh_series` re-reads just some series
(one request each) and merges them into the universe (adds newly listed markets, drops
closed ones) without the full refresh; the engine runs it every ``UniverseSpec.refresh_s``
for specs that ask for it (e.g. 15-minute crypto windows). A full refresh that started
before such a series read keeps the newer series data.

The universe is
filtered to ``status == "active"`` and ``close_time > now``. Refreshes are at least
``min_refresh_interval_s`` (60 s) apart. If a query fails, markets from the previous
universe that are still plausibly open are kept, and ``last_error`` is set.

A baseline "scanner" window (``engine.scanner_days_to_close``, extra config key, default
0.5 day, 0 disables) keeps the Markets page populated even with no strategy enabled.

Caches
------
* ``orderbook(ticker, max_age_s)``: TTL cache. Concurrent requests are **coalesced** into
  ``GET /markets/orderbooks?tickers=…`` batch calls (≤ 100 per call), so
  ``asyncio.gather(*(md.orderbook(t) for t in many))`` costs one request per 100 books.
* ``series``: 24 h (fee parameters); ``event``: ~1 h, fetched lazily, ``.markets`` filled
  from the universe (one ``by_event`` index per refresh); events whose markets left the
  universe are evicted once their TTL expires; ``exchange_status``: 15 s, never raises.
* Fee schedule: ``GET /events/fee_changes`` and ``GET /series/fee_changes`` (upcoming
  changes, e.g. every MLB event switches from M=0.5 to 1 at first pitch) are re-read every
  ``fee_schedule_ttl_s`` (30 min). :meth:`MarketDataService.fee_params` resolves the
  effective ``(fee_type, multiplier)`` **at a given time**: the series (as fetched), its
  scheduled changes since that fetch, the event override (as fetched) and the event's
  scheduled changes since that fetch, whichever is latest and not in the future. Events
  used for fees are also re-read every ``fee_event_max_age_s`` (5 min), which bounds the lag
  of an override that was never scheduled (or while the schedule endpoint is down).
* ``market(ticker, fresh)``: cached snapshot; ``fresh=True`` requires one younger than
  ``fresh_max_age_s`` (5 s). ``refresh_markets(tickers)`` batch-primes that cache via
  ``GET /markets?tickers=…`` so settlement polling of many held markets is cheap. A market
  archived behind the historical cutoff (404) is read from ``/historical/markets/{t}``.
* ``trades_since(ticker, since)`` returns **every** trade at or after ``since`` (whole
  seconds, like ``min_ts``), following pagination (a very large backlog is capped at
  ``max_trade_pages`` pages and logged as an error).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from kalshibot.kalshi.client import KalshiNotFound
from kalshibot.kalshi.models import OPEN_STATUSES, Event, Market, Orderbook, Series, Trade, parse_dec, parse_ts
from kalshibot.money import ONE, D

if TYPE_CHECKING:
    from kalshibot.kalshi.client import KalshiClient
    from kalshibot.strategies.base import UniverseSpec

__all__ = [
    "SCANNER_SPEC_NAME",
    "MarketDataService",
    "display_title",
    "market_url",
]

log = logging.getLogger(__name__)

SCANNER_SPEC_NAME = "__scanner__"
BATCH_BOOKS = 100
BATCH_MARKETS = 100
#: Close-time window scan, split into ascending chunks (days from now). ``GET /markets`` returns a
#: close-time window **latest close first**, so one query cut off at ``universe_max_pages`` loses the
#: markets closing *soonest* - measured 2026-09-27: a 3-day window was 116 pages (26k active of
#: 116k), and the cut at 100 pages dropped every market closing in the next ~7 h (3,447 active).
#: Scanning the chunks nearest-first confines a cut to the far end of the window.
WINDOW_CHUNK_EDGES_DAYS = (0.5, 1.0)  # then one chunk per further day


def window_chunks(window_days: float) -> list[tuple[float, float]]:
    """``[(lo, hi), ...]`` day offsets covering ``[0, window_days]`` nearest-first: half days up to a
    day, then whole days. A window of at most half a day is one chunk."""
    edges = [0.0]
    for e in WINDOW_CHUNK_EDGES_DAYS:
        if e < window_days - 1e-9:
            edges.append(e)
    nxt = max(edges[-1], WINDOW_CHUNK_EDGES_DAYS[-1]) + 1.0
    while nxt < window_days - 1e-9:
        edges.append(nxt)
        nxt += 1.0
    edges.append(float(window_days))
    return list(zip(edges[:-1], edges[1:], strict=True))


_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")


def plain_text(s: str) -> str:
    """Kalshi titles may carry Markdown bold (``Will average **gas prices** ...``): strip it."""
    return _MD_BOLD.sub(r"\1", s) if s and "**" in s else s


def display_title(m: Market | None) -> str:
    """Human title: ``title`` plus the YES sub-title when it adds information (plain text)."""
    if m is None:
        return ""
    t = plain_text(m.title or m.ticker)
    sub = plain_text((m.yes_sub_title or "").strip())
    if sub and sub.lower() not in t.lower():
        return f"{t} — {sub}"
    return t


def market_url(series_ticker: str | None) -> str | None:
    """Best-effort kalshi.com link (§12)."""
    return f"https://kalshi.com/markets/{series_ticker.lower()}" if series_ticker else None


def _cfg(section: Any, key: str, default: Any) -> Any:
    if section is None:
        return default
    v = getattr(section, key, None)
    if v is None:
        extra = getattr(section, "model_extra", None) or {}
        v = extra.get(key)
    return default if v is None else v


class MarketDataService:
    """Market data for the engine, strategies, broker and API (one per process)."""

    def __init__(
        self,
        client: KalshiClient | Any,
        settings: Any = None,
        *,
        clock: Callable[[], datetime] | None = None,
        mono: Callable[[], float] = time.monotonic,
        min_refresh_interval_s: float = 60.0,
        series_ttl_s: float = 24 * 3600.0,
        event_ttl_s: float = 3600.0,
        market_max_age_s: float = 300.0,
        fresh_max_age_s: float = 5.0,
        exchange_ttl_s: float = 15.0,
        scanner_days_to_close: float | None = None,
        universe_max_pages: int | None = None,
        window_rescan_s: float | None = None,
        max_trade_pages: int = 100,
        page_delay_s: float = 0.0,
        fee_schedule_ttl_s: float = 1800.0,
        fee_event_max_age_s: float = 300.0,
        force_refresh_min_s: float = 5.0,
    ) -> None:
        eng = getattr(settings, "engine", None)
        self.client = client
        self.clock = clock or (lambda: datetime.now(UTC))
        self.mono = mono
        self.min_refresh_interval_s = float(min_refresh_interval_s)
        self.series_ttl_s = float(series_ttl_s)
        self.event_ttl_s = float(event_ttl_s)
        self.market_max_age_s = float(market_max_age_s)
        self.fresh_max_age_s = float(fresh_max_age_s)
        self.exchange_ttl_s = float(exchange_ttl_s)
        self.scanner_days_to_close = float(
            scanner_days_to_close if scanner_days_to_close is not None
            else _cfg(eng, "scanner_days_to_close", 0.5))
        self.universe_max_pages = int(
            universe_max_pages if universe_max_pages is not None else _cfg(eng, "universe_max_pages", 150))
        self.window_rescan_s = float(
            window_rescan_s if window_rescan_s is not None else _cfg(eng, "universe_window_rescan_s", 900))
        self.max_trade_pages = int(max_trade_pages)
        self.page_delay_s = float(page_delay_s)
        self.fee_schedule_ttl_s = float(fee_schedule_ttl_s)
        self.fee_event_max_age_s = float(fee_event_max_age_s)
        self.force_refresh_min_s = float(force_refresh_min_s)

        # universe
        self.markets: dict[str, Market] = {}
        self.events: dict[str, Event] = {}
        self.specs: dict[str, UniverseSpec] = {}
        self.last_refresh: datetime | None = None
        self.last_refresh_duration_s: float | None = None
        self.last_error: str | None = None
        self.refresh_count = 0
        self.truncated: list[str] = []  # queries cut off at universe_max_pages in the last refresh
        self._last_refresh_mono: float | None = None
        self._refresh_lock = asyncio.Lock()
        self.last_refresh_kind: str | None = None  # "full" (window scanned) | "series"
        self.last_refresh_queries = 0
        self.last_window_scan: datetime | None = None
        self._last_window_scan_mono: float | None = None
        self._window_series: set[str] = set()  # series with active markets in the window (last full scan)
        self._window_days = 0.0
        self._window_pages = 0  # pages the last full window scan took
        self._pages_read: dict[str, int] = {}
        self._last_union: tuple[float, tuple[str, ...]] | None = None  # spec union of the last refresh
        #: series -> (mono, markets) of the last series-scoped refresh (merged by a full refresh
        #: that started earlier)
        self._series_fresh: dict[str, tuple[float, dict[str, Market]]] = {}
        self.last_series_refresh: datetime | None = None

        # caches
        self._market_cache: dict[str, tuple[float, Market]] = {}
        self._books: dict[str, tuple[float, Orderbook]] = {}
        self._book_waiters: dict[str, asyncio.Future[Orderbook]] = {}
        self._book_pending: list[str] = []
        self._book_flush: asyncio.Task[None] | None = None
        self._series: dict[str, tuple[float, Series]] = {}
        self._series_fail: dict[str, float] = {}
        self._event_ts: dict[str, float] = {}
        self._event_fail: dict[str, float] = {}
        self._event_fetched_at: dict[str, datetime] = {}  # wall clock of the cached copy
        self._series_fetched_at: dict[str, datetime] = {}
        # fee schedule: id -> (ticker, scheduled_ts, fee_type or None, multiplier or None)
        self._event_fee_rows: dict[str, tuple[str, datetime, str | None, Decimal | None]] = {}
        self._series_fee_rows: dict[str, tuple[str, datetime, str | None, Decimal | None]] = {}
        self._event_fee_changes: dict[str, list[tuple[datetime, str | None, Decimal | None]]] = {}
        self._series_fee_changes: dict[str, list[tuple[datetime, str | None, Decimal | None]]] = {}
        self._fee_schedule_mono: float | None = None
        self._fee_event_missing: dict[str, float] = {}  # event -> mono of its last 404 (fee lookups)
        self.fee_schedule_ok = False
        self.fee_schedule_error: str | None = None
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._exchange: dict[str, Any] | None = None
        self._exchange_ts: float | None = None
        self.exchange_error: str | None = None

    # ------------------------------------------------------------------ helpers

    def _now(self) -> datetime:
        n = self.clock()
        return n if n.tzinfo else n.replace(tzinfo=UTC)

    def _lock(self, kind: str, key: str) -> asyncio.Lock:
        lk = self._locks.get((kind, key))
        if lk is None:
            lk = self._locks[(kind, key)] = asyncio.Lock()
        return lk

    @property
    def universe_size(self) -> int:
        return len(self.markets)

    # ------------------------------------------------------------------ universe

    def set_specs(self, specs: Mapping[str, UniverseSpec]) -> bool:
        """Set the strategy specs (name -> spec). Returns True if the union changed."""
        new = {k: v for k, v in specs.items() if v is not None}
        changed = self._union_key(new) != self._union_key(self.specs)
        self.specs = new
        return changed

    def all_specs(self) -> dict[str, UniverseSpec]:
        from kalshibot.strategies.base import UniverseSpec  # local: avoid an import cycle

        specs = dict(self.specs)
        if self.scanner_days_to_close > 0:
            specs[SCANNER_SPEC_NAME] = UniverseSpec(max_days_to_close=self.scanner_days_to_close)
        return specs

    @staticmethod
    def _union_key(specs: Mapping[str, UniverseSpec]) -> tuple[float, tuple[str, ...]]:
        window = max((s.max_days_to_close or 0.0 for s in specs.values()), default=0.0)
        series = tuple(sorted({t for s in specs.values() for t in s.series_tickers}))
        return window, series

    def refresh_due(self) -> bool:
        return (self._last_refresh_mono is None
                or self.mono() - self._last_refresh_mono >= self.min_refresh_interval_s)

    async def _pages(self, params: Mapping[str, Any], label: str, *,
                     record: bool = True, max_pages: int | None = None) -> AsyncIterator[dict[str, Any]]:
        """Raw ``/markets`` items across pages (capped at ``max_pages``, default
        ``universe_max_pages``; ``record``: count pages / truncation in the full refresh's
        bookkeeping)."""
        q = dict(params)
        seen: set[str] = set()
        pages = 0
        cap = self.universe_max_pages if max_pages is None else max(1, int(max_pages))
        while True:
            d = await self.client.get("/markets", q)
            pages += 1
            if record:
                self._pages_read[label] = pages
            for item in d.get("markets") or ():
                if isinstance(item, Mapping):
                    yield dict(item)
            cur = d.get("cursor") or None
            if not cur or cur in seen:
                return
            if pages >= cap:
                if record:
                    self.truncated.append(label)
                log.warning("universe query %s truncated at %d pages (universe_max_pages)", label, pages)
                return
            seen.add(cur)
            q["cursor"] = cur
            if self.page_delay_s > 0:
                await asyncio.sleep(self.page_delay_s)

    def _window_scan_due(self, window: float) -> bool:
        """Full window scan needed: never done / stale / window grew / cheaper than per-series reads."""
        return (self._last_window_scan_mono is None
                or self.mono() - self._last_window_scan_mono >= self.window_rescan_s
                or window > self._window_days + 1e-9
                or len(self._window_series) > self._window_pages // 2)

    async def refresh_universe(self, *, force: bool = False) -> bool:
        """Re-fetch the union of the specs. Returns False if skipped (too soon / nothing to do).

        The close-time window is scanned in full every ``window_rescan_s`` (and whenever
        the window grows); in between, only the series that had active markets in the
        window at the last full scan are re-read (``series_ticker=…&status=open``, filtered
        to the window) - but only when that takes at most half as many requests as the
        full scan did (few series, many pre-created markets); otherwise the full scan is
        repeated, since the request budget is the scarce resource.
        """
        if not force and not self.refresh_due():
            return False
        if force and not self.force_refresh_allowed():
            return False
        async with self._refresh_lock:
            t0 = self.mono()
            now = self._now()
            specs = self.all_specs()
            window, series = self._union_key(specs)
            self._last_union = (window, series)
            explicit = set(series)
            full = window > 0 and self._window_scan_due(window)
            horizon = now + timedelta(days=window)
            self.truncated = []
            self._pages_read = {}
            new: dict[str, Market] = {}
            errors: list[str] = []
            window_failed = False
            found_series: set[str] = set()
            queries: list[tuple[str, str, dict[str, Any]]] = []
            if full:
                # nearest chunk first: a page-capped scan then loses the far end of the window, not
                # the markets closing soonest (the API lists a window latest close first)
                ts = int(now.timestamp())
                chunks = window_chunks(window)
                for lo_d, hi_d in chunks:
                    label = f"close<{window:g}d" if len(chunks) == 1 else f"close<{window:g}d[{lo_d:g}-{hi_d:g}d]"
                    queries.append(("window", label, {
                        "limit": 1000, "mve_filter": "exclude", "min_close_ts": ts + int(lo_d * 86400),
                        "max_close_ts": ts + int(hi_d * 86400)}))
            series_q = set(explicit)
            if window > 0 and not full:
                series_q |= self._window_series
            for s in sorted(series_q):
                queries.append(("series", f"series={s}", {"limit": 1000, "series_ticker": s, "status": "open"}))
            window_pages_left = self.universe_max_pages  # one page budget for all window chunks
            skipped_from: float | None = None  # first window chunk (days) left unread by the page cap
            for kind, label, params in queries:
                if kind == "window" and window_pages_left <= 0:
                    if skipped_from is None:
                        skipped_from = (int(params["min_close_ts"]) - int(now.timestamp())) / 86400
                    continue
                try:
                    async for raw in self._pages(params, label,
                                                 max_pages=window_pages_left if kind == "window" else None):
                        if raw.get("status") not in OPEN_STATUSES:
                            continue
                        m = Market.from_api(raw)
                        if not m.ticker or (m.close_time is not None and m.close_time <= now):
                            continue
                        if kind == "window":
                            found_series.add(m.series_ticker)
                        elif m.series_ticker not in explicit and (
                                m.close_time is None or m.close_time > horizon):
                            continue  # window-derived series: keep only markets inside the window
                        new[m.ticker] = m
                except Exception as e:  # keep going; merge with the previous universe below
                    errors.append(f"{label}: {type(e).__name__}: {e}")
                    log.warning("universe query %s failed: %s", label, e)
                    window_failed = window_failed or kind == "window"
                if kind == "window":
                    window_pages_left -= self._pages_read.get(label, 0)
            if skipped_from is not None:
                self.truncated.append(f"close<{window:g}d[{skipped_from:g}-{window:g}d]")
                log.warning("universe window: markets closing %g-%g days out not read (universe_max_pages %d "
                            "used up by nearer ones)", skipped_from, window, self.universe_max_pages)
            if errors:
                for t, m in self.markets.items():
                    if t not in new and m.is_open and (m.close_time is None or m.close_time > now):
                        new[t] = m
            self._merge_fresh_series(new, since=t0, series=explicit)
            mono_now = self.mono()
            if full and not window_failed:
                self._window_pages = sum(n for lbl, n in self._pages_read.items() if lbl.startswith("close<"))
                self._window_series = found_series
                self._window_days = window
                self._last_window_scan_mono = mono_now
                self.last_window_scan = now
            elif window <= 0:
                self._window_series = set()
                self._window_days = 0.0
            self.markets = new
            for t, m in new.items():
                self._market_cache[t] = (mono_now, m)
            self._evict_events(mono_now)
            self._refill_events()
            self._prune_caches(mono_now)
            self.last_refresh = now
            self.last_refresh_duration_s = round(mono_now - t0, 3)
            self.last_refresh_kind = "full" if full else "series"
            self.last_refresh_queries = len(queries)
            self._last_refresh_mono = mono_now
            self.refresh_count += 1
            self.last_error = "; ".join(errors) if errors else None
            log.info("universe refreshed (%s): %d markets (%d queries, %.1fs)%s", self.last_refresh_kind, len(new),
                     len(queries), self.last_refresh_duration_s, f"; errors: {self.last_error}" if errors else "")
            return True

    def _merge_fresh_series(self, new: dict[str, Market], *, since: float, series: Iterable[str]) -> None:
        """Series read by :meth:`refresh_series` after ``since`` replace their (older) markets in ``new``."""
        now = self._now()
        for s in series:
            hit = self._series_fresh.get(s)
            if hit is None or hit[0] <= since:
                continue
            for t in [t for t, m in new.items() if m.series_ticker == s and t not in hit[1]]:
                del new[t]
            for t, m in hit[1].items():
                if m.close_time is None or m.close_time > now:
                    new[t] = m

    async def refresh_series(self, series_tickers: Iterable[str]) -> int:
        """Series-scoped refresh: ``GET /markets?series_ticker=S&status=open`` for each series
        (one request each; no minimum interval) merged into the universe - newly listed active
        markets are added, markets of the series that are no longer active/open are dropped.
        A failing series keeps its markets. Returns the number of markets added."""
        added = 0
        changed = False
        for s in dict.fromkeys(t for t in series_tickers if t):
            now = self._now()
            fetched: dict[str, Market] = {}
            try:
                async for raw in self._pages({"limit": 1000, "series_ticker": s, "status": "open"},
                                             f"series={s}", record=False):
                    if raw.get("status") not in OPEN_STATUSES:
                        continue
                    m = Market.from_api(raw)
                    if m.ticker and (m.close_time is None or m.close_time > now):
                        fetched[m.ticker] = m
            except Exception as e:
                log.warning("series refresh %s failed: %s", s, e)
                continue
            mono_now = self.mono()
            self._series_fresh[s] = (mono_now, fetched)
            for t in [t for t, m in self.markets.items() if m.series_ticker == s and t not in fetched]:
                del self.markets[t]
                changed = True
            for t, m in fetched.items():
                if t not in self.markets:
                    added += 1
                    changed = True
                self.markets[t] = m
                self._market_cache[t] = (mono_now, m)
        if changed:
            self._refill_events()
        self.last_series_refresh = self._now()
        if added:
            log.info("series refresh: %d new market(s)", added)
        return added

    def force_refresh_allowed(self) -> bool:
        """A forced refresh runs unless one just finished (``force_refresh_min_s``) with the
        same spec union: a refresh that started before a strategy was enabled never counts."""
        if self._last_refresh_mono is None or self.mono() - self._last_refresh_mono >= self.force_refresh_min_s:
            return True
        return self._last_union != self._union_key(self.all_specs())

    def force_refresh_wait_s(self) -> float:
        """Seconds until :meth:`force_refresh_allowed` (0 if allowed now)."""
        if self.force_refresh_allowed() or self._last_refresh_mono is None:
            return 0.0
        return max(0.0, self.force_refresh_min_s - (self.mono() - self._last_refresh_mono))

    def markets_for(self, spec: UniverseSpec | None, now: datetime | None = None) -> dict[str, Market]:
        """Universe markets matching one strategy's spec (not yet past ``close_time``: a market
        that closed since the last refresh is left out rather than shown as open)."""
        if spec is None or spec.is_empty:
            return {}
        now = now or self._now()
        return {t: m for t, m in self.markets.items()
                if (m.close_time is None or m.close_time > now) and spec.matches(m, now)}

    def _prune_caches(self, mono_now: float) -> None:
        for t in [t for t, (ts, _) in self._books.items() if mono_now - ts > 600]:
            del self._books[t]
        keep = set(self.markets)
        for t in [t for t, (ts, _) in self._market_cache.items() if t not in keep and mono_now - ts > 3600]:
            del self._market_cache[t]
        for t in [t for t, ts in self._fee_event_missing.items() if mono_now - ts > self.event_ttl_s]:
            del self._fee_event_missing[t]
        cached = {"market": self._market_cache, "event": self.events, "series": self._series}
        for k in [k for k, lk in self._locks.items()
                  if not lk.locked() and k[0] in cached and k[1] not in cached[k[0]]]:
            del self._locks[k]

    # ------------------------------------------------------------------ markets

    def _store_market(self, m: Market, ts: float | None = None) -> None:
        if not m.ticker:
            return
        ts = self.mono() if ts is None else ts
        self._market_cache[m.ticker] = (ts, m)
        if m.ticker in self.markets:
            now = self._now()
            if m.is_open and (m.close_time is None or m.close_time > now):
                self.markets[m.ticker] = m
            else:
                del self.markets[m.ticker]

    def known_market(self, ticker: str) -> Market | None:
        """Any cached snapshot (universe or single fetch) without a request."""
        m = self.markets.get(ticker)
        if m is not None:
            return m
        hit = self._market_cache.get(ticker)
        return hit[1] if hit else None

    async def market(self, ticker: str, fresh: bool = False) -> Market:
        """Market snapshot; ``fresh=True`` requires one younger than ``fresh_max_age_s``."""
        max_age = self.fresh_max_age_s if fresh else self.market_max_age_s
        hit = self._market_cache.get(ticker)
        if hit is not None and self.mono() - hit[0] <= max_age:
            return hit[1]
        async with self._lock("market", ticker):
            hit = self._market_cache.get(ticker)
            if hit is not None and self.mono() - hit[0] <= max_age:
                return hit[1]
            try:
                m = await self.client.get_market(ticker)
            except KalshiNotFound:
                m = await self._historical_market(ticker)
            self._store_market(m)
            return m

    async def _historical_market(self, ticker: str) -> Market:
        """A market archived behind the historical cutoff (live endpoint 404s)."""
        try:
            d = await self.client.get(f"/historical/markets/{ticker}")
        except Exception:
            raise KalshiNotFound(404, f"market {ticker} not found (live or historical)",
                                 f"/markets/{ticker}") from None
        m = Market.from_api(d.get("market") or d)
        if not m.ticker:
            raise KalshiNotFound(404, f"market {ticker} not found", f"/historical/markets/{ticker}")
        return m

    async def refresh_markets(self, tickers: Iterable[str]) -> dict[str, Market]:
        """Batch-refresh snapshots via ``GET /markets?tickers=…`` (primes ``market(fresh=True)``)."""
        uniq = list(dict.fromkeys(t for t in tickers if t))
        out: dict[str, Market] = {}
        for i in range(0, len(uniq), BATCH_MARKETS):
            chunk = uniq[i: i + BATCH_MARKETS]
            d = await self.client.get("/markets", {"tickers": chunk, "limit": 1000})
            ts = self.mono()
            for raw in d.get("markets") or ():
                m = Market.from_api(raw)
                if m.ticker:
                    self._store_market(m, ts)
                    out[m.ticker] = m
        return out

    # ------------------------------------------------------------------ order books

    async def orderbook(self, ticker: str, max_age_s: float = 5) -> Orderbook:
        """Order book no older than ``max_age_s`` (concurrent misses share batch requests)."""
        hit = self._books.get(ticker)
        if hit is not None and self.mono() - hit[0] <= max_age_s:
            return hit[1]
        fut = self._book_waiters.get(ticker)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            fut.add_done_callback(_consume_exception)
            self._book_waiters[ticker] = fut
            self._book_pending.append(ticker)
            if self._book_flush is None:
                self._book_flush = asyncio.ensure_future(self._flush_books())
        return await asyncio.shield(fut)

    async def orderbooks(self, tickers: Iterable[str], max_age_s: float = 5) -> dict[str, Orderbook]:
        """Many books at once (batched); tickers whose fetch failed are omitted."""
        uniq = list(dict.fromkeys(tickers))
        res = await asyncio.gather(*(self.orderbook(t, max_age_s) for t in uniq), return_exceptions=True)
        out: dict[str, Orderbook] = {}
        for t, r in zip(uniq, res, strict=True):
            if isinstance(r, Orderbook):
                out[t] = r
            elif isinstance(r, BaseException):
                log.debug("orderbook %s failed: %s", t, r)
        return out

    def cached_orderbook(self, ticker: str) -> Orderbook | None:
        hit = self._books.get(ticker)
        return hit[1] if hit else None

    def _resolve_book(self, ticker: str, book: Orderbook | None, exc: BaseException | None) -> None:
        fut = self._book_waiters.pop(ticker, None)
        if book is not None:
            book = dataclasses.replace(book, ts=self._now())  # received now (this service's clock)
            self._books[ticker] = (self.mono(), book)
        if fut is None or fut.done():
            return
        if exc is not None:
            fut.set_exception(exc)
        else:
            fut.set_result(book)  # type: ignore[arg-type]

    async def _flush_books(self) -> None:
        # Let every coroutine scheduled in this loop iteration register first.
        for _ in range(3):
            await asyncio.sleep(0)
        tickers, self._book_pending = self._book_pending, []
        self._book_flush = None
        for i in range(0, len(tickers), BATCH_BOOKS):
            chunk = tickers[i: i + BATCH_BOOKS]
            if len(chunk) == 1:
                t = chunk[0]
                try:
                    self._resolve_book(t, await self.client.get_orderbook(t), None)
                except Exception as e:
                    self._resolve_book(t, None, e)
                continue
            try:
                books = await self.client.get_orderbooks(chunk)
            except Exception as e:
                for t in chunk:
                    self._resolve_book(t, None, e)
                continue
            for t in chunk:
                b = books.get(t)
                if b is not None:
                    self._resolve_book(t, b, None)
                    continue
                try:  # missing from the batch answer: ask for it alone (proper 404 etc.)
                    self._resolve_book(t, await self.client.get_orderbook(t), None)
                except Exception as e:
                    self._resolve_book(t, None, e)

    # ------------------------------------------------------------------ trades

    async def trades_since(self, ticker: str, since: datetime) -> list[Trade]:
        """Every trade at or after ``since`` (``min_ts`` = whole seconds), newest first."""
        min_ts = int(since.timestamp())
        out: list[Trade] = []
        cursor: str | None = None
        seen: set[str] = set()
        for page in range(self.max_trade_pages):
            trades, cursor = await self.client.get_trades(ticker, min_ts=min_ts, limit=1000, cursor=cursor)
            out.extend(trades)
            if not cursor or cursor in seen:
                break
            seen.add(cursor)
            if page + 1 == self.max_trade_pages:
                log.error("trades_since(%s, %s): backlog exceeds %d pages; oldest trades not fetched",
                          ticker, since.isoformat(), self.max_trade_pages)
        return out

    # ------------------------------------------------------------------ series / events

    async def series(self, series_ticker: str) -> Series:
        """Series (fee parameters, category), cached ``series_ttl_s``; stale value on failure."""
        hit = self._series.get(series_ticker)
        if hit is not None and self.mono() - hit[0] <= self.series_ttl_s:
            return hit[1]
        async with self._lock("series", series_ticker):
            hit = self._series.get(series_ticker)
            if hit is not None and self.mono() - hit[0] <= self.series_ttl_s:
                return hit[1]
            try:
                s = await self.client.get_series(series_ticker)
            except Exception:
                self._series_fail[series_ticker] = self.mono()
                if hit is not None:
                    log.warning("series %s refresh failed; using the cached copy", series_ticker)
                    return hit[1]
                raise
            self._series[series_ticker] = (self.mono(), s)
            self._series_fetched_at[series_ticker] = self._now()
            self._series_fail.pop(series_ticker, None)
            return s

    def cached_series(self, series_ticker: str) -> Series | None:
        hit = self._series.get(series_ticker)
        return hit[1] if hit else None

    async def prefetch_series(self, limit: int = 20, tickers: Iterable[str] | None = None) -> int:
        """Fetch up to ``limit`` uncached series of the universe (most markets first)."""
        if tickers is None:
            counts = Counter(m.series_ticker for m in self.markets.values() if m.series_ticker)
            tickers = [s for s, _ in counts.most_common()]
        done = 0
        now = self.mono()
        for s in tickers:
            if done >= limit:
                break
            if s in self._series or now - self._series_fail.get(s, -1e18) < 600:
                continue
            try:
                await self.series(s)
            except Exception as e:
                log.info("series %s unavailable: %s", s, e)
            done += 1
        return done

    def _by_event(self) -> dict[str, list[Market]]:
        idx: dict[str, list[Market]] = {}
        for t in sorted(self.markets):
            m = self.markets[t]
            idx.setdefault(m.event_ticker, []).append(m)
        return idx

    def _fill_event(self, ev: Event, by_event: Mapping[str, list[Market]] | None = None) -> Event:
        """``ev`` with ``.markets`` = its own markets (refreshed from the universe) + universe
        markets of the event it did not list."""
        if by_event is None:
            by_event = {ev.event_ticker: [m for t, m in sorted(self.markets.items())
                                          if m.event_ticker == ev.event_ticker]}
        markets = [self.markets.get(m.ticker, m) for m in ev.markets]
        known = {m.ticker for m in markets}
        markets.extend(m for m in by_event.get(ev.event_ticker, ()) if m.ticker not in known)
        return dataclasses.replace(ev, markets=markets)

    def _refill_events(self) -> None:
        """Re-point cached events at the fresh universe: one O(markets) index, then O(1) per event."""
        by_event = self._by_event()
        for t, ev in list(self.events.items()):
            self.events[t] = self._fill_event(ev, by_event)

    def _evict_events(self, mono_now: float) -> None:
        """Forget expired events none of whose markets is in the universe any more (they are
        re-fetched on demand), and failure/timestamp entries of events no longer cached."""
        live = {m.event_ticker for m in self.markets.values()}
        for t in [t for t in self.events
                  if t not in live and mono_now - self._event_ts.get(t, -1e18) > self.event_ttl_s]:
            del self.events[t]
        for d in (self._event_ts, self._event_fetched_at):
            for t in [t for t in d if t not in self.events]:
                del d[t]
        for t in [t for t, ts in self._event_fail.items() if mono_now - ts > self.event_ttl_s]:
            del self._event_fail[t]

    async def event(self, event_ticker: str, *, max_age_s: float | None = None) -> Event | None:
        """Event (``mutually_exclusive``, category, fee overrides), cached ``event_ttl_s``
        (or ``max_age_s``). Returns the stale copy if a refresh fails; raises only when
        nothing is cached."""
        ttl = self.event_ttl_s if max_age_s is None else max_age_s
        ts = self._event_ts.get(event_ticker)
        if ts is not None and self.mono() - ts <= ttl:
            return self.events.get(event_ticker)
        async with self._lock("event", event_ticker):
            ts = self._event_ts.get(event_ticker)
            if ts is not None and self.mono() - ts <= ttl:
                return self.events.get(event_ticker)
            try:
                ev = await self.client.get_event(event_ticker)
            except Exception:
                self._event_fail[event_ticker] = self.mono()
                if event_ticker in self.events:
                    return self.events[event_ticker]
                raise
            self.events[event_ticker] = self._fill_event(ev)
            self._event_ts[event_ticker] = self.mono()
            self._event_fetched_at[event_ticker] = self._now()
            return self.events[event_ticker]

    # ------------------------------------------------------------------ fees

    @staticmethod
    def _fee_row(d: Mapping[str, Any], kind: str) -> tuple[str, datetime, str | None, Decimal | None] | None:
        try:
            ts = parse_ts(d.get("scheduled_ts"))
        except (TypeError, ValueError):
            return None
        if ts is None:
            return None
        if kind == "event":
            ticker, ft, m = d.get("event_ticker"), d.get("fee_type_override"), d.get("fee_multiplier_override")
        else:
            ticker, ft, m = d.get("series_ticker"), d.get("fee_type"), d.get("fee_multiplier")
        if not ticker:
            return None
        return str(ticker), ts, (str(ft) if ft else None), parse_dec(m)

    def _merge_fee_rows(self, rows: Iterable[Mapping[str, Any]], kind: str, now: datetime,
                        known: dict[str, tuple[str, datetime, str | None, Decimal | None]]) -> None:
        """The endpoints list *upcoming* changes only: keep changes we saw that have since
        taken effect (for 2 days), drop upcoming ones that were withdrawn."""
        fresh: dict[str, tuple[str, datetime, str | None, Decimal | None]] = {}
        for i, d in enumerate(rows):
            if isinstance(d, Mapping):
                r = self._fee_row(d, kind)
                if r is not None:
                    fresh[str(d.get("id") or f"{kind}-{r[0]}-{r[1].isoformat()}-{i}")] = r
        horizon = now - timedelta(days=2)
        for k, r in list(known.items()):
            if k not in fresh and (r[1] > now or r[1] < horizon):
                del known[k]
        known.update(fresh)

    @staticmethod
    def _by_ticker(rows: Mapping[str, tuple[str, datetime, str | None, Decimal | None]]
                   ) -> dict[str, list[tuple[datetime, str | None, Decimal | None]]]:
        out: dict[str, list[tuple[datetime, str | None, Decimal | None]]] = {}
        for ticker, ts, ft, m in rows.values():
            out.setdefault(ticker, []).append((ts, ft, m))
        for v in out.values():
            v.sort(key=lambda x: x[0])
        return out

    async def refresh_fee_schedule(self, *, force: bool = False) -> bool:
        """Re-read the scheduled fee changes (every ``fee_schedule_ttl_s``; 60 s after a failure).
        Never raises; returns True when refreshed."""
        def due() -> bool:
            return (force or self._fee_schedule_mono is None
                    or self.mono() - self._fee_schedule_mono >= self.fee_schedule_ttl_s)

        if not due():
            return False
        async with self._lock("fee_schedule", ""):
            if not due():
                return False
            try:
                ev_rows: list[Any] = []
                params: dict[str, Any] = {"limit": 1000}
                seen: set[str] = set()
                for _ in range(20):
                    d = await self.client.get("/events/fee_changes", params)
                    ev_rows.extend(d.get("event_fee_changes") or ())
                    cur = d.get("cursor") or None
                    if not cur or cur in seen:
                        break
                    seen.add(cur)
                    params = {"limit": 1000, "cursor": cur}
                sd = await self.client.get("/series/fee_changes", {})
                s_rows = list(sd.get("series_fee_change_arr") or ())
            except Exception as e:
                self.fee_schedule_error = f"{type(e).__name__}: {e}"
                # retry in a minute; meanwhile fees fall back to short-lived event reads
                self._fee_schedule_mono = self.mono() - self.fee_schedule_ttl_s + 60.0
                self.fee_schedule_ok = False
                log.warning("fee schedule unavailable: %s", e)
                return False
            now = self._now()
            self._merge_fee_rows(ev_rows, "event", now, self._event_fee_rows)
            self._merge_fee_rows(s_rows, "series", now, self._series_fee_rows)
            self._event_fee_changes = self._by_ticker(self._event_fee_rows)
            self._series_fee_changes = self._by_ticker(self._series_fee_rows)
            self._fee_schedule_mono = self.mono()
            self.fee_schedule_ok = True
            self.fee_schedule_error = None
            return True

    #: A scheduled change this close before a fetch may not be visible in the fetched copy yet.
    FEE_FETCH_GRACE = timedelta(seconds=60)

    def _resolve_fees(self, market: Market, series: Any, event: Any, at: datetime) -> tuple[str, Decimal]:
        """Series values, then the series' scheduled changes since that copy was fetched, then
        the event override (as fetched), then the event's scheduled changes since that fetch;
        only changes whose ``scheduled_ts <= at`` count."""
        s_ft = getattr(series, "fee_type", None) or "quadratic"
        s_m = getattr(series, "fee_multiplier", None)
        s_m = ONE if s_m is None else D(s_m)
        s_at = self._series_fetched_at.get(market.series_ticker)
        for ts, ft, m in self._series_fee_changes.get(market.series_ticker, ()):
            if ts <= at and (s_at is None or ts > s_at - self.FEE_FETCH_GRACE):
                s_ft = ft or s_ft
                s_m = m if m is not None else s_m
        o_ft = getattr(event, "fee_type_override", None) if event is not None else None
        o_m = getattr(event, "fee_multiplier_override", None) if event is not None else None
        e_at = self._event_fetched_at.get(market.event_ticker) if event is not None else None
        for ts, ft, m in self._event_fee_changes.get(market.event_ticker, ()):
            if ts <= at and (e_at is None or ts > e_at - self.FEE_FETCH_GRACE):
                o_ft, o_m = ft, m  # a null pair clears the override
        return (o_ft or s_ft), (D(o_m) if o_m is not None else s_m)

    async def fee_params(self, market: Market, at: datetime | None = None) -> tuple[str, Decimal]:
        """Effective ``(fee_type, fee_multiplier)`` for ``market`` at time ``at`` (default now).

        Raises if the series cannot be fetched and none is cached (the broker then charges
        its conservative fallback)."""
        at = at or self._now()
        await self.refresh_fee_schedule()
        series = await self.series(market.series_ticker)
        event: Event | None = None
        et = market.event_ticker
        missing = self._fee_event_missing.get(et)
        if missing is None or self.mono() - missing > self.fee_event_max_age_s:
            try:
                event = await self.event(et, max_age_s=self.fee_event_max_age_s)
                self._fee_event_missing.pop(et, None)
            except KalshiNotFound:  # no such event: do not ask again for a while
                self._fee_event_missing[et] = self.mono()
            except Exception as e:
                log.warning("event %s unavailable for fees: %s", et, e)
        return self._resolve_fees(market, series, event, at)

    def fee_params_cached(self, market: Market, at: datetime | None = None) -> tuple[str, Decimal] | None:
        """:meth:`fee_params` from cached data only (no request); ``None`` if the series is not cached."""
        series = self.cached_series(market.series_ticker)
        if series is None:
            return None
        return self._resolve_fees(market, series, self.events.get(market.event_ticker), at or self._now())

    def category(self, m: Market) -> str:
        """Best-effort category from cached series/event data ("" if unknown)."""
        s = self.cached_series(m.series_ticker)
        if s is not None and s.category:
            return s.category
        ev = self.events.get(m.event_ticker)
        return ev.category if ev is not None and ev.category else ""

    # ------------------------------------------------------------------ exchange

    async def exchange_status(self, max_age_s: float | None = None) -> dict[str, Any] | None:
        """``GET /exchange/status`` (cached); never raises (last known value, or None)."""
        ttl = self.exchange_ttl_s if max_age_s is None else max_age_s
        if self._exchange_ts is not None and self.mono() - self._exchange_ts <= ttl:
            return self._exchange
        async with self._lock("exchange", ""):
            if self._exchange_ts is not None and self.mono() - self._exchange_ts <= ttl:
                return self._exchange
            try:
                self._exchange = await self.client.get_exchange_status()
                self.exchange_error = None
            except Exception as e:
                self.exchange_error = f"{type(e).__name__}: {e}"
                log.warning("exchange status unavailable: %s", e)
            self._exchange_ts = self.mono()
            return self._exchange

    @property
    def trading_active(self) -> bool | None:
        """Exchange-level ``trading_active`` from the last status (None if never fetched)."""
        st = self._exchange
        if not isinstance(st, Mapping):
            return None
        v = st.get("trading_active")
        return bool(v) if v is not None else None

    # ------------------------------------------------------------------ views

    def search(self, *, search: str = "", category: str = "", sort: str = "volume_24h",
               limit: int = 100) -> list[Market]:
        """Universe markets for the scanner (``/api/markets``)."""
        q = search.strip().lower()
        cat = category.strip().lower()
        rows = []
        for m in self.markets.values():
            if q and not any(q in plain_text(x or "").lower() for x in (m.ticker, m.event_ticker, m.title,
                                                                            m.yes_sub_title, m.series_ticker)):
                continue
            if cat and self.category(m).lower() != cat:
                continue
            rows.append(m)
        far = datetime.max.replace(tzinfo=UTC)
        if sort == "close_time":
            rows.sort(key=lambda m: (m.close_time or far, m.ticker))
        elif sort == "spread":
            rows.sort(key=lambda m: (m.spread is None, m.spread if m.spread is not None else 0,
                                     -m.volume_24h, m.ticker))
        else:
            rows.sort(key=lambda m: (-m.volume_24h, m.ticker))
        return rows[: max(0, limit)]

    def status(self) -> dict[str, Any]:
        return {
            "universe_size": self.universe_size,
            "last_refresh": self.last_refresh,
            "last_refresh_duration_s": self.last_refresh_duration_s,
            "last_error": self.last_error,
            "refresh_count": self.refresh_count,
            "last_refresh_kind": self.last_refresh_kind,
            "last_refresh_queries": self.last_refresh_queries,
            "last_window_scan": self.last_window_scan,
            "last_series_refresh": self.last_series_refresh,
            "window_series": len(self._window_series),
            "window_pages": self._window_pages,
            "truncated": list(self.truncated),
            "specs": sorted(self.specs),
            "scanner_days_to_close": self.scanner_days_to_close,
            "cached": {"books": len(self._books), "markets": len(self._market_cache),
                       "series": len(self._series), "events": len(self.events)},
            "requests": getattr(self.client, "request_count", None),
        }


def _consume_exception(fut: asyncio.Future[Any]) -> None:
    """Mark a future's exception as retrieved (avoid 'never retrieved' noise)."""
    if not fut.cancelled():
        fut.exception()
