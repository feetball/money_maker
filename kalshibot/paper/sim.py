"""In-memory, deterministic market data for the paper broker (tests and backtests).

:class:`StaticMarketData` implements :class:`kalshibot.paper.broker.MarketDataProvider` from
data you set explicitly - no network. It records every call in ``calls`` so tests can
assert, e.g., that the broker fetched a fresh book with ``max_age_s=2``.
"""

from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from kalshibot.kalshi.client import KalshiNotFound
from kalshibot.kalshi.models import Event, Market, Orderbook, Series, Trade
from kalshibot.money import ONE, D, Number

__all__ = ["ManualClock", "StaticMarketData", "make_market", "make_series", "make_trade"]

_trade_ids = itertools.count(1)


class ManualClock:
    """Injectable clock for deterministic tests/backtests: ``clock()`` returns ``clock.now``."""

    def __init__(self, start: datetime) -> None:
        self.now = start if start.tzinfo else start.replace(tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float = 0, **kw: float) -> datetime:
        self.now += timedelta(seconds=seconds, **kw)
        return self.now

    def set(self, when: datetime) -> datetime:
        self.now = when
        return self.now


def make_market(
    ticker: str,
    *,
    event_ticker: str | None = None,
    series_ticker: str | None = None,
    status: str = "active",
    close_time: datetime | None = None,
    open_time: datetime | None = None,
    yes_bid: Number | None = None,
    yes_ask: Number | None = None,
    result: str = "",
    settlement_value: Number | None = None,
    price_ranges: Iterable[Mapping[str, str]] | None = None,
    exchange_index: int | None = 0,
    title: str = "",
    **extra: Any,
) -> Market:
    """Build a :class:`Market` through ``Market.from_api`` (same parsing as live data)."""
    event_ticker = event_ticker or ticker.rsplit("-", 1)[0]
    d: dict[str, Any] = {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "title": title or ticker,
        "status": status,
        "market_type": "binary",
        "close_time": (close_time or datetime(2100, 1, 1, tzinfo=UTC)).isoformat().replace("+00:00", "Z"),
        "result": result,
        "exchange_index": exchange_index,
        "price_ranges": list(price_ranges) if price_ranges is not None
        else [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}],
    }
    if series_ticker:
        d["series_ticker"] = series_ticker
    if open_time is not None:
        d["open_time"] = open_time.isoformat().replace("+00:00", "Z")
    if yes_bid is not None:
        d["yes_bid_dollars"] = str(D(yes_bid))
        d["no_ask_dollars"] = str(ONE - D(yes_bid))
    if yes_ask is not None:
        d["yes_ask_dollars"] = str(D(yes_ask))
        d["no_bid_dollars"] = str(ONE - D(yes_ask))
    if settlement_value is not None:
        d["settlement_value_dollars"] = str(D(settlement_value))
    d.update(extra)
    return Market.from_api(d)


def make_series(ticker: str, fee_type: str = "quadratic", fee_multiplier: Number = 1) -> Series:
    return Series.from_api({"ticker": ticker, "title": ticker, "fee_type": fee_type,
                            "fee_multiplier": float(D(fee_multiplier))})


def make_trade(ticker: str, yes_price: Number, count: Number, ts: datetime, *, taker_side: str = "no",
               is_block_trade: bool = False, trade_id: str | None = None) -> Trade:
    """A public trade print. ``taker_side`` is the taker's outcome side (``taker_outcome_side``)."""
    yp = D(yes_price)
    return Trade(trade_id=trade_id or f"t{next(_trade_ids):08d}", ticker=ticker, ts=ts, yes_price=yp,
                 no_price=ONE - yp, count=D(count), taker_side=taker_side,
                 taker_book_side="bid" if taker_side == "yes" else "ask", is_block_trade=is_block_trade)


class StaticMarketData:
    """Deterministic :class:`~kalshibot.paper.broker.MarketDataProvider` backed by dicts."""

    def __init__(self, markets: Iterable[Market] = (), *, clock: Any = None) -> None:
        self.markets: dict[str, Market] = {m.ticker: m for m in markets}
        self.books: dict[str, Orderbook] = {}
        self.trades: dict[str, list[Trade]] = {}
        self.series_map: dict[str, Series] = {}
        self.events: dict[str, Event] = {}
        self.exchange: dict[str, Any] | None = None
        self.clock = clock or (lambda: datetime.now(UTC))
        self.calls: list[tuple[Any, ...]] = []
        self.fail: set[str] = set()  # method names that should raise (outage simulation)

    # -- setup ---------------------------------------------------------------------

    def set_market(self, market: Market) -> Market:
        self.markets[market.ticker] = market
        return market

    def set_book(self, ticker: str, yes_bids: Iterable[tuple[Number, Number]] = (),
                 no_bids: Iterable[tuple[Number, Number]] = ()) -> Orderbook:
        """Set the book from YES bids and NO bids as (price, size) pairs (any order)."""
        book = Orderbook.from_levels(ticker, yes_bids=yes_bids, no_bids=no_bids, ts=self.clock())
        self.books[ticker] = book
        return book

    def add_trade(self, ticker: str, yes_price: Number, count: Number, ts: datetime | None = None,
                  **kw: Any) -> Trade:
        t = make_trade(ticker, yes_price, count, ts or self.clock(), **kw)
        self.trades.setdefault(ticker, []).append(t)
        return t

    def set_series(self, ticker: str, fee_type: str = "quadratic", fee_multiplier: Number = 1) -> Series:
        s = make_series(ticker, fee_type, fee_multiplier)
        self.series_map[ticker] = s
        return s

    def _check(self, name: str) -> None:
        if name in self.fail:
            raise RuntimeError(f"simulated {name} outage")

    # -- provider API --------------------------------------------------------------

    async def orderbook(self, ticker: str, max_age_s: float = 5) -> Orderbook:
        """The book as set, stamped with the current clock (static data is the live state)."""
        self.calls.append(("orderbook", ticker, max_age_s))
        self._check("orderbook")
        book = self.books.get(ticker)
        if book is None:
            return Orderbook.from_levels(ticker, ts=self.clock())
        return dataclasses.replace(book, ts=self.clock())

    async def trades_since(self, ticker: str, since: datetime) -> list[Trade]:
        """Trades at or after ``since`` (whole seconds, like ``min_ts``), newest first like the API."""
        self.calls.append(("trades_since", ticker, since))
        self._check("trades_since")
        floor = since.replace(microsecond=0) if since.microsecond else since
        out = [t for t in self.trades.get(ticker, []) if t.ts >= floor]
        return sorted(out, key=lambda t: t.ts, reverse=True)

    async def series(self, series_ticker: str) -> Series:
        self.calls.append(("series", series_ticker))
        self._check("series")
        return self.series_map.get(series_ticker) or make_series(series_ticker)

    def known_market(self, ticker: str) -> Market | None:
        """Cached snapshot without a "request" (like ``MarketDataService.known_market``)."""
        return self.markets.get(ticker)

    async def market(self, ticker: str, fresh: bool = False) -> Market:
        self.calls.append(("market", ticker, fresh))
        self._check("market")
        try:
            return self.markets[ticker]
        except KeyError:
            raise KalshiNotFound(404, f"market {ticker} not found", f"/markets/{ticker}") from None

    async def event(self, event_ticker: str) -> Event | None:
        self.calls.append(("event", event_ticker))
        return self.events.get(event_ticker)

    async def exchange_status(self) -> dict[str, Any] | None:
        self.calls.append(("exchange_status",))
        return self.exchange
