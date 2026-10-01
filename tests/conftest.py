"""Shared test helpers: an in-memory fake ``KalshiClient`` and dummy strategies.

``FakeKalshiClient`` implements the methods of :class:`kalshibot.kalshi.client.KalshiClient`
that the market-data service uses (including the generic ``get`` for paginated ``/markets``
scans), backed by raw API-shaped dicts, so the real ``MarketDataService``, paper broker,
risk manager, store, engine and API run end-to-end without network access.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kalshibot.config import Settings
from kalshibot.kalshi.client import KalshiAPIError, KalshiNotFound
from kalshibot.kalshi.models import Event, Market, Orderbook, Series, Trade
from kalshibot.money import ONE, D
from kalshibot.strategies.base import OrderIntent, Strategy, UniverseSpec


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def raw_market(ticker: str, *, close_time: datetime, status: str = "active", event_ticker: str | None = None,
               yes_bid: Any = None, yes_ask: Any = None, volume_24h: Any = 0, title: str = "",
               result: str = "", settlement_value: Any = None, yes_sub_title: str = "",
               open_interest: Any = 0, series_ticker: str | None = None) -> dict[str, Any]:
    et = event_ticker or ticker.rsplit("-", 1)[0]
    d: dict[str, Any] = {
        "ticker": ticker, "event_ticker": et, "title": title or f"Title of {ticker}", "status": status,
        "market_type": "binary", "close_time": iso(close_time), "open_time": iso(close_time - timedelta(days=1)),
        "result": result, "exchange_index": 0, "yes_sub_title": yes_sub_title,
        "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}],
        "volume_24h_fp": str(D(volume_24h)), "open_interest_fp": str(D(open_interest)),
        "yes_bid_dollars": str(D(yes_bid)) if yes_bid is not None else "0.0000",
        "yes_ask_dollars": str(D(yes_ask)) if yes_ask is not None else "1.0000",
        "no_bid_dollars": str(ONE - D(yes_ask)) if yes_ask is not None else "0.0000",
        "no_ask_dollars": str(ONE - D(yes_bid)) if yes_bid is not None else "1.0000",
        "last_price_dollars": "0.5000",
    }
    if series_ticker:
        d["series_ticker"] = series_ticker
    if settlement_value is not None:
        d["settlement_value_dollars"] = str(D(settlement_value))
    return d


class FakeKalshiClient:
    """In-memory stand-in for ``KalshiClient`` (records every call in ``calls``)."""

    def __init__(self, page_size: int = 2) -> None:
        self.page_size = page_size
        self.trade_page_size = 1000
        self.markets: dict[str, dict[str, Any]] = {}
        self.books: dict[str, tuple[list[tuple[Any, Any]], list[tuple[Any, Any]]]] = {}
        self.trades: dict[str, list[Trade]] = {}
        self.series: dict[str, dict[str, Any]] = {}
        self.events: dict[str, dict[str, Any]] = {}
        self.historical: dict[str, dict[str, Any]] = {}
        self.exchange: dict[str, Any] = {"exchange_active": True, "trading_active": True,
                                         "exchange_index_statuses": []}
        self.calls: list[tuple[Any, ...]] = []
        self.fail: set[str] = set()  # method names that raise a network-type error
        self.omit_from_batch: set[str] = set()  # tickers the batch-orderbook endpoint "forgets"
        self.event_fee_changes: list[dict[str, Any]] = []  # GET /events/fee_changes rows
        self.series_fee_changes: list[dict[str, Any]] = []  # GET /series/fee_changes rows
        self.request_count = 0
        self.success_count = 0  # like KalshiClient.success_count
        self.closed = False

    # -- setup -------------------------------------------------------------------------

    def add_market(self, ticker: str, **kw: Any) -> dict[str, Any]:
        kw.setdefault("close_time", datetime.now(UTC) + timedelta(days=1))
        d = raw_market(ticker, **kw)
        self.markets[ticker] = d
        return d

    def update_market(self, ticker: str, **fields: Any) -> None:
        self.markets[ticker].update(fields)

    def set_book(self, ticker: str, yes: Iterable[tuple[Any, Any]] = (), no: Iterable[tuple[Any, Any]] = ()) -> None:
        self.books[ticker] = (list(yes), list(no))

    def add_trade(self, ticker: str, yes_price: Any, count: Any, ts: datetime, taker: str = "no",
                  trade_id: str | None = None) -> Trade:
        yp = D(yes_price)
        n = sum(len(v) for v in self.trades.values())
        t = Trade(trade_id=trade_id or f"tr{n + 1:06d}", ticker=ticker, ts=ts, yes_price=yp, no_price=ONE - yp,
                  count=D(count), taker_side=taker, taker_book_side="bid" if taker == "yes" else "ask",
                  is_block_trade=False)
        self.trades.setdefault(ticker, []).append(t)
        return t

    def set_series(self, ticker: str, fee_type: str = "quadratic", fee_multiplier: Any = 1,
                   category: str = "Testing") -> None:
        self.series[ticker] = {"ticker": ticker, "title": f"Series {ticker}", "category": category,
                               "frequency": "daily", "fee_type": fee_type, "fee_multiplier": float(fee_multiplier)}

    def set_event(self, event_ticker: str, *, mutually_exclusive: bool = False, category: str = "Testing",
                  title: str = "") -> None:
        self.events[event_ticker] = {"event_ticker": event_ticker, "series_ticker": event_ticker.split("-")[0],
                                     "title": title or f"Event {event_ticker}", "category": category,
                                     "mutually_exclusive": mutually_exclusive}

    # -- plumbing ----------------------------------------------------------------------

    def _rec(self, *call: Any) -> None:
        self.calls.append(call)
        self.request_count += 1
        if call[0] in self.fail:
            raise KalshiAPIError(None, f"simulated outage of {call[0]}", str(call[0]))
        self.success_count += 1

    def count(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)

    def call_counts(self) -> Counter[str]:
        return Counter(c[0] for c in self.calls)

    async def get(self, path: str, params: dict[str, Any] | None = None, *, repeat: Iterable[str] = ()) -> dict:
        params = dict(params or {})
        self._rec("get", path, params)
        if path.startswith("/historical/markets/"):
            t = path.rsplit("/", 1)[-1]
            if t not in self.historical:
                raise KalshiNotFound(404, "not found", path)
            return {"market": self.historical[t]}
        if path == "/events/fee_changes":
            rows = [r for r in self.event_fee_changes
                    if not params.get("event_ticker") or r["event_ticker"] == params["event_ticker"]]
            return {"event_fee_changes": rows, "cursor": ""}
        if path == "/series/fee_changes":
            return {"series_fee_change_arr": list(self.series_fee_changes)}
        if path != "/markets":
            raise KalshiNotFound(404, "not found", path)
        items = sorted(self.markets.values(), key=lambda m: m["ticker"])
        if "tickers" in params:
            want = params["tickers"]
            want = want.split(",") if isinstance(want, str) else list(want)
            items = [m for m in items if m["ticker"] in want]
        if "series_ticker" in params:
            items = [m for m in items if m["ticker"].split("-")[0] == params["series_ticker"]]
        if params.get("status") == "open":
            items = [m for m in items if m["status"] == "active"]
        if "min_close_ts" in params:
            lo, hi = int(params["min_close_ts"]), int(params.get("max_close_ts", 2**40))
            items = [m for m in items
                     if lo <= int(datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp()) <= hi]
        start = int(params.get("cursor") or 0)
        page = items[start: start + self.page_size]
        nxt = start + self.page_size
        return {"markets": page, "cursor": str(nxt) if nxt < len(items) else ""}

    async def get_market(self, ticker: str) -> Market:
        self._rec("get_market", ticker)
        if ticker not in self.markets:
            raise KalshiNotFound(404, "market not found", f"/markets/{ticker}")
        return Market.from_api(self.markets[ticker])

    async def get_event(self, event_ticker: str, *, with_nested_markets: bool | None = None) -> Event:
        self._rec("get_event", event_ticker)
        if event_ticker not in self.events:
            raise KalshiNotFound(404, "event not found", f"/events/{event_ticker}")
        markets = [m for m in self.markets.values() if m["event_ticker"] == event_ticker]
        return Event.from_api({"event": self.events[event_ticker], "markets": markets})

    async def get_series(self, series_ticker: str) -> Series:
        self._rec("get_series", series_ticker)
        if series_ticker not in self.series:
            raise KalshiNotFound(404, "series not found", f"/series/{series_ticker}")
        return Series.from_api({"series": self.series[series_ticker]})

    def _book(self, ticker: str) -> Orderbook:
        yes, no = self.books.get(ticker, ([], []))
        return Orderbook.from_levels(ticker, yes_bids=yes, no_bids=no)

    async def get_orderbook(self, ticker: str, depth: int = 0) -> Orderbook:
        self._rec("get_orderbook", ticker)
        if ticker not in self.markets:
            raise KalshiNotFound(404, "market not found", f"/markets/{ticker}/orderbook")
        return self._book(ticker)

    async def get_orderbooks(self, tickers: Iterable[str]) -> dict[str, Orderbook]:
        tickers = list(tickers)
        self._rec("get_orderbooks", tuple(tickers))
        return {t: self._book(t) for t in tickers if t in self.markets and t not in self.omit_from_batch}

    async def get_trades(self, ticker: str | None = None, min_ts: Any = None, limit: int = 1000,
                         cursor: str | None = None, *, max_ts: Any = None) -> tuple[list[Trade], str | None]:
        self._rec("get_trades", ticker, min_ts, cursor)
        rows = sorted(self.trades.get(ticker or "", []), key=lambda t: t.ts, reverse=True)
        if min_ts is not None:
            rows = [t for t in rows if t.ts.timestamp() >= int(min_ts)]
        size = min(limit, self.trade_page_size)
        start = int(cursor or 0)
        page = rows[start: start + size]
        nxt = start + size
        return page, (str(nxt) if nxt < len(rows) else None)

    async def get_exchange_status(self) -> dict[str, Any]:
        self._rec("get_exchange_status")
        return dict(self.exchange)

    async def aclose(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- strategies


class DummyStrategy(Strategy):
    """Buys YES at the ask in every universe market priced at or below ``max_price``."""

    name = "dummy"
    description = "Test strategy: buy YES at the ask when it is cheap."
    default_params = {"max_price": 0.6, "count": 2, "days": 7.0, "tif": "ioc", "series": []}
    param_schema = {
        "max_price": {"type": "float", "min": 0.01, "max": 0.99, "help": "max YES ask"},
        "count": {"type": "int", "min": 1, "max": 100, "help": "contracts per order"},
        "days": {"type": "float", "min": 0, "max": 30, "help": "universe window"},
        "tif": {"type": "enum", "choices": ["ioc", "gtc"], "help": "time in force"},
        "series": {"type": "list", "help": "extra series"},
    }
    backtestable = True

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.fills: list[Any] = []
        self.settlements: list[Any] = []
        self.ticks = 0

    def universe(self) -> UniverseSpec:
        return UniverseSpec(max_days_to_close=self.params["days"], series_tickers=list(self.params["series"]))

    async def on_tick(self, ctx: Any) -> list[OrderIntent]:
        self.ticks += 1
        out = []
        for t, m in sorted(ctx.markets.items()):
            if ctx.portfolio.holds(t, self.name) or ctx.portfolio.has_open_order(t, self.name):
                continue
            ask = m.yes_ask
            if ask is None or ask > D(str(self.params["max_price"])):
                continue
            fee = ctx.fee(m, ask, self.params["count"])
            out.append(OrderIntent(ticker=t, side="yes", count=self.params["count"], limit_price=ask,
                                   tif=self.params["tif"], reason=f"ask {ask} <= {self.params['max_price']}",
                                   fair_value=0.7, expected_edge=D("0.7") - ask - fee / self.params["count"]))
        return out

    def on_fill(self, fill: Any) -> None:
        self.fills.append(fill)

    def on_settlement(self, s: Any) -> None:
        self.settlements.append(s)

    def dump_state(self) -> Any:
        return {"ticks": self.ticks}

    def load_state(self, state: Any) -> None:
        self.ticks = int((state or {}).get("ticks", 0))


class BoomStrategy(Strategy):
    """Always raises in ``on_tick``."""

    name = "boom"
    description = "Test strategy that always fails."

    def universe(self) -> UniverseSpec:
        return UniverseSpec(max_days_to_close=1)

    async def on_tick(self, ctx: Any) -> list[OrderIntent]:
        raise RuntimeError("kaboom")


class ScriptedStrategy(Strategy):
    """Returns whatever is queued in ``self.queue`` (list of intents per tick)."""

    name = "scripted"
    description = "Test strategy returning queued intents."

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.queue: list[list[Any]] = []

    async def on_tick(self, ctx: Any) -> list[Any]:
        return self.queue.pop(0) if self.queue else []


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def fake_client() -> FakeKalshiClient:
    return FakeKalshiClient()


@pytest.fixture
def settings(tmp_path: Any) -> Settings:
    s = Settings()
    s.storage.path = str(tmp_path / "test.sqlite3")
    s.engine.autostart = False
    #: most tests assert absolute cash/equity figures; the profit-sweep feature is tested on its own
    s.account.profit_sweep_pct = 0
    return s


def standard_market(fc: FakeKalshiClient, ticker: str = "KXTEST-26SEP27-A", *, yes_bid: Any = "0.40",
                    yes_ask: Any = "0.45", depth: Any = 100, **kw: Any) -> dict[str, Any]:
    """A two-sided market whose book matches its quote (YES ask = 1 - best NO bid)."""
    d = fc.add_market(ticker, yes_bid=yes_bid, yes_ask=yes_ask, **kw)
    fc.set_book(ticker, yes=[(yes_bid, depth)], no=[(ONE - D(yes_ask), depth)])
    fc.set_series(ticker.split("-")[0])
    return d
