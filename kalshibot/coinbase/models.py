"""Immutable views of Coinbase Exchange public API objects (docs/COINBASE_CONTRACT.md §3).

Built from raw JSON with ``Model.from_api(...)``; each keeps the original payload as
``.raw``. Prices and sizes are parsed from strings to ``Decimal`` (never float).

Verified shapes (api.exchange.coinbase.com, 2026-09-27):

* ``GET /products/{id}`` -> ``{"id": "BTC-USD", "base_currency", "quote_currency",
  "quote_increment": "0.01", "base_increment": "0.00000001", "min_market_funds": "1",
  "post_only", "limit_only", "cancel_only", "status": "online", "trading_disabled", ...}``
* ``GET /products/{id}/book?level=2`` -> ``{"bids": [[price, size, num_orders], ...],
  "asks": [...], "sequence", "time"}`` - bids best-first (descending), asks best-first
  (ascending).
* ``GET /products/{id}/trades`` -> newest first ``[{"trade_id", "side", "size", "price",
  "time"}]``; pagination via the ``cb-after`` / ``cb-before`` response headers.
  **``side`` is the MAKER's side**: ``"buy"`` = a resting buy was hit (the taker sold;
  prints at the bid), ``"sell"`` = a resting sell was lifted (the taker bought; prints at
  the ask).
* ``GET /products/{id}/candles?granularity=&start=&end=`` -> newest first
  ``[[time, low, high, open, close, volume], ...]`` with ``time`` = bar OPEN (unix s, UTC).
* ``GET /products/{id}/ticker`` -> ``{"ask", "bid", "price", "size", "volume", "time", ...}``
* ``GET /products/{id}/stats`` -> ``{"open", "high", "low", "last", "volume", "volume_30day"}``
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, NamedTuple

__all__ = [
    "BookLevel",
    "Candle",
    "OrderBook",
    "Product",
    "SpotSide",
    "Stats",
    "Ticker",
    "Trade",
    "dec",
    "parse_time",
]

SpotSide = Literal["buy", "sell"]
Raw = Mapping[str, Any]


def dec(value: Any, default: Decimal | None = None) -> Decimal | None:
    """Parse an API number/string to Decimal; ``""``/``None``/garbage -> ``default``."""
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def parse_time(value: Any) -> datetime | None:
    """ISO-8601 string (any precision, ``Z`` suffix) or unix seconds -> aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Python < 3.11 cannot parse nanoseconds; trim the fraction to microseconds.
    if "." in s:
        head, _, rest = s.partition(".")
        digits = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(digits):]
        s = f"{head}.{digits[:6]}{tz}"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Product:
    product_id: str
    base_currency: str
    quote_currency: str
    base_increment: Decimal
    quote_increment: Decimal
    min_market_funds: Decimal
    status: str
    trading_disabled: bool
    post_only: bool
    limit_only: bool
    cancel_only: bool
    display_name: str = ""
    raw: Raw = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Raw) -> Product:
        return cls(
            product_id=str(d["id"]),
            base_currency=str(d.get("base_currency", "")),
            quote_currency=str(d.get("quote_currency", "")),
            base_increment=dec(d.get("base_increment"), Decimal("0.00000001")),
            quote_increment=dec(d.get("quote_increment"), Decimal("0.01")),
            min_market_funds=dec(d.get("min_market_funds"), Decimal("1")),
            status=str(d.get("status", "")),
            trading_disabled=bool(d.get("trading_disabled", False)),
            post_only=bool(d.get("post_only", False)),
            limit_only=bool(d.get("limit_only", False)),
            cancel_only=bool(d.get("cancel_only", False)),
            display_name=str(d.get("display_name") or d.get("id", "")),
            raw=d,
        )

    @property
    def tradable(self) -> bool:
        """Online and accepting new orders (limit-only products still accept limits)."""
        return self.status == "online" and not self.trading_disabled and not self.cancel_only


class BookLevel(NamedTuple):
    price: Decimal
    size: Decimal
    num_orders: int = 0


@dataclass(frozen=True)
class OrderBook:
    """Level-2 book. ``bids`` descending and ``asks`` ascending (both best-first)."""

    product_id: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    sequence: int | None = None
    time: datetime | None = None
    raw: Raw = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, product_id: str, d: Raw) -> OrderBook:
        def levels(rows: Sequence[Sequence[Any]], descending: bool) -> tuple[BookLevel, ...]:
            out = []
            for row in rows or ():
                p, s = dec(row[0]), dec(row[1])
                if p is None or s is None or p <= 0 or s <= 0:
                    continue
                n = int(row[2]) if len(row) > 2 and str(row[2]).isdigit() else 0
                out.append(BookLevel(p, s, n))
            out.sort(key=lambda lv: lv.price, reverse=descending)
            return tuple(out)

        seq = d.get("sequence")
        return cls(
            product_id=product_id,
            bids=levels(d.get("bids", ()), descending=True),
            asks=levels(d.get("asks", ()), descending=False),
            sequence=int(seq) if isinstance(seq, int | str) and str(seq).isdigit() else None,
            time=parse_time(d.get("time")),
            raw=d,
        )

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread_bps(self) -> Decimal | None:
        m = self.mid
        if m is None or m <= 0:
            return None
        return (self.best_ask - self.best_bid) / m * Decimal(10_000)  # type: ignore[operator]


@dataclass(frozen=True)
class Trade:
    trade_id: int
    product_id: str
    price: Decimal
    size: Decimal
    maker_side: SpotSide
    time: datetime
    raw: Raw = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, product_id: str, d: Raw) -> Trade:
        return cls(
            trade_id=int(d["trade_id"]),
            product_id=product_id,
            price=dec(d["price"]),  # type: ignore[arg-type]
            size=dec(d["size"]),  # type: ignore[arg-type]
            maker_side="buy" if d.get("side") == "buy" else "sell",
            time=parse_time(d["time"]),  # type: ignore[arg-type]
            raw=d,
        )

    @property
    def taker_side(self) -> SpotSide:
        return "sell" if self.maker_side == "buy" else "buy"


@dataclass(frozen=True)
class Candle:
    """One OHLCV bar. ``start`` is the bar OPEN time; ``end = start + granularity``."""

    product_id: str
    start: datetime
    granularity_s: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @classmethod
    def from_api(cls, product_id: str, granularity_s: int, row: Sequence[Any]) -> Candle:
        t, low, high, open_, close, vol = row[:6]
        return cls(
            product_id=product_id,
            start=datetime.fromtimestamp(int(t), tz=UTC),
            granularity_s=int(granularity_s),
            open=dec(open_),  # type: ignore[arg-type]
            high=dec(high),  # type: ignore[arg-type]
            low=dec(low),  # type: ignore[arg-type]
            close=dec(close),  # type: ignore[arg-type]
            volume=dec(vol, Decimal(0)),  # type: ignore[arg-type]
        )

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.granularity_s)


@dataclass(frozen=True)
class Ticker:
    product_id: str
    bid: Decimal | None
    ask: Decimal | None
    price: Decimal | None
    volume_24h: Decimal | None
    time: datetime | None
    raw: Raw = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, product_id: str, d: Raw) -> Ticker:
        return cls(product_id=product_id, bid=dec(d.get("bid")), ask=dec(d.get("ask")),
                   price=dec(d.get("price")), volume_24h=dec(d.get("volume")),
                   time=parse_time(d.get("time")), raw=d)


@dataclass(frozen=True)
class Stats:
    product_id: str
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    last: Decimal | None
    volume_24h: Decimal | None
    volume_30d: Decimal | None
    raw: Raw = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, product_id: str, d: Raw) -> Stats:
        return cls(product_id=product_id, open=dec(d.get("open")), high=dec(d.get("high")),
                   low=dec(d.get("low")), last=dec(d.get("last")),
                   volume_24h=dec(d.get("volume")), volume_30d=dec(d.get("volume_30day")), raw=d)
