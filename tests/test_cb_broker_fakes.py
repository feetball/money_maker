"""Shared fakes for the Coinbase broker / store / risk tests (no test functions here).

``FakeSpotMD`` implements the broker's market-data protocol (``book``, ``trades_since``,
``product``) from in-memory state, with a controllable clock, so every scenario is
deterministic. ``make_product`` / ``make_book`` / ``make_trade`` build the verified model
objects from ``kalshibot.coinbase.models``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from kalshibot.coinbase.fees import FeeTier
from kalshibot.coinbase.models import BookLevel, OrderBook, Product, Trade

T0 = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
#: Round test rates: maker 0.5 %, taker 1 % (fees stay hand-computable).
TIER = FeeTier("test", Decimal("0.005"), Decimal("0.01"))
PID = "TST-USD"


def D(x: Any) -> Decimal:
    return Decimal(str(x))


def make_product(pid: str = PID, *, base_increment: str = "0.01", quote_increment: str = "0.01",
                 min_market_funds: str = "1", status: str = "online", trading_disabled: bool = False,
                 post_only: bool = False, limit_only: bool = False, cancel_only: bool = False) -> Product:
    base, quote = pid.split("-")
    return Product.from_api({
        "id": pid, "base_currency": base, "quote_currency": quote, "base_increment": base_increment,
        "quote_increment": quote_increment, "min_market_funds": min_market_funds, "status": status,
        "trading_disabled": trading_disabled, "post_only": post_only, "limit_only": limit_only,
        "cancel_only": cancel_only})


def make_book(pid: str = PID, *, bids: Sequence[tuple[Any, Any]] = (), asks: Sequence[tuple[Any, Any]] = (),
              time: datetime | None = None) -> OrderBook:
    return OrderBook(
        product_id=pid,
        bids=tuple(sorted((BookLevel(D(p), D(s), 1) for p, s in bids), key=lambda lv: lv.price, reverse=True)),
        asks=tuple(sorted((BookLevel(D(p), D(s), 1) for p, s in asks), key=lambda lv: lv.price)),
        time=time)


def make_trade(trade_id: int, price: Any, size: Any, maker_side: str, time: datetime, pid: str = PID) -> Trade:
    return Trade(trade_id=trade_id, product_id=pid, price=D(price), size=D(size),
                 maker_side=maker_side, time=time)  # type: ignore[arg-type]


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


@dataclass
class FakeSpotMD:
    """In-memory market data. ``books[pid]`` is returned stamped with the clock's time unless
    ``stale_books`` holds an explicit timestamp; ``trades[pid]`` is the public tape."""

    clock: Clock = field(default_factory=Clock)
    products: dict[str, Product] = field(default_factory=dict)
    books: dict[str, OrderBook] = field(default_factory=dict)
    trades: dict[str, list[Trade]] = field(default_factory=dict)
    stale_books: dict[str, datetime] = field(default_factory=dict)
    fresh_on_refetch: bool = True
    fail_trades: set[str] = field(default_factory=set)
    fail_products: set[str] = field(default_factory=set)
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def set_book(self, pid: str = PID, *, bids: Sequence[tuple[Any, Any]] = (),
                 asks: Sequence[tuple[Any, Any]] = ()) -> None:
        self.books[pid] = make_book(pid, bids=bids, asks=asks)

    def add_trades(self, trades: Iterable[Trade]) -> None:
        for t in trades:
            self.trades.setdefault(t.product_id, []).append(t)

    async def book(self, product_id: str, max_age_s: float = 2) -> OrderBook:
        self.calls.append(("book", product_id, max_age_s))
        b = self.books[product_id]
        when = self.stale_books.get(product_id)
        if when is not None and max_age_s == 0 and self.fresh_on_refetch:
            when = None
        return OrderBook(product_id=b.product_id, bids=b.bids, asks=b.asks, sequence=b.sequence,
                         time=when if when is not None else self.clock.now)

    async def trades_since(self, product_id: str, since_trade_id: int | None) -> list[Trade]:
        self.calls.append(("trades_since", product_id, since_trade_id))
        if product_id in self.fail_trades:
            raise RuntimeError("tape unavailable")
        tape = self.trades.get(product_id, [])
        if since_trade_id is None:
            return sorted(tape, key=lambda t: -t.trade_id)[:100]  # newest page, newest first
        return sorted((t for t in tape if t.trade_id > since_trade_id), key=lambda t: -t.trade_id)

    async def product(self, product_id: str) -> Product:
        self.calls.append(("product", product_id))
        if product_id in self.fail_products:
            raise RuntimeError("products endpoint down")
        return self.products[product_id]


def standard_md(*, clock: Clock | None = None, **product_kw: Any) -> FakeSpotMD:
    """TST-USD (base increment 0.01, quote increment 0.01, min funds $1) with the book
    asks 100 x 1, 101 x 2, 105 x 10 / bids 99 x 1, 98 x 2, 90 x 10."""
    md = FakeSpotMD(clock=clock or Clock())
    md.products[PID] = make_product(PID, **product_kw)
    md.set_book(PID, bids=[(99, 1), (98, 2), (90, 10)], asks=[(100, 1), (101, 2), (105, 10)])
    return md
