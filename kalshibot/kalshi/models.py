"""Immutable views of Kalshi public API objects (ARCHITECTURE.md §3).

Every model is built from raw API JSON with ``Model.from_api(d)`` and keeps the
original dict as ``.raw``. Prices come from the ``*_dollars`` fixed-point strings and
sizes from the ``*_fp`` strings, both parsed to ``Decimal``; legacy integer-cent /
integer-count fields are used only when the fixed-point fields are absent.

Normalization rules
-------------------
* Top-of-book prices (market ``yes_bid``/``yes_ask``/``no_bid``/``no_ask`` and candle
  bid/ask OHLC) outside the open interval (0, 1) are empty-side sentinels and become
  ``None``. Kalshi uses bid ``0.0000`` / ask ``1.0000`` for an empty side, and for a
  completely empty book also reports ``yes_ask = 0`` / ``no_bid = 1`` (seen live).
* Order-book levels are stored **best-first** (descending price); the API sends them
  ascending. Levels with non-positive size are dropped.
* Timestamps are timezone-aware UTC; ``0001-01-01T00:00:00Z`` means "unset" -> ``None``.
* Historical candles (``/historical/markets/{t}/candlesticks``) use unsuffixed keys
  (``open``, ``volume``, ``open_interest``); both shapes parse into :class:`Candle`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, NamedTuple

from kalshibot.money import (
    DEFAULT_PRICE_RANGES,
    ONE,
    ZERO,
    D,
    Number,
    PriceRange,
    RoundingMode,
    is_valid_price,
    snap_price,
    tick_at,
)

__all__ = [
    "OHLC",
    "Candle",
    "Event",
    "Level",
    "Market",
    "Orderbook",
    "Series",
    "Side",
    "Trade",
    "parse_dec",
    "parse_price_ranges",
    "parse_ts",
    "series_from_event_ticker",
]

Side = Literal["yes", "no"]
Raw = Mapping[str, Any]

#: REST statuses in which a market trades. ("open" is the list-filter alias.)
OPEN_STATUSES = frozenset({"active", "open"})
#: Terminal/paid status. ("settled" is the list-filter alias.)
FINAL_STATUSES = frozenset({"finalized", "settled"})


# --------------------------------------------------------------------------- parsing helpers


def parse_ts(value: Any) -> datetime | None:
    """ISO-8601 string or epoch seconds -> aware UTC datetime; ``None`` for unset."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float) and not isinstance(value, bool):
        if value <= 0:
            return None
        return datetime.fromtimestamp(value, tz=UTC)
    s = str(value).strip()
    if s.startswith("0001-01-01"):
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Python 3.11 fromisoformat accepts any number of fractional digits.
    dt = datetime.fromisoformat(s)
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_dec(value: Any) -> Decimal | None:
    """Fixed-point string / number -> Decimal; ``None``/"" -> ``None``."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return D(value)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _dollars(d: Raw, name: str) -> Decimal | None:
    """Read ``{name}_dollars`` (preferred) or the legacy integer-cent field ``{name}``."""
    v = parse_dec(d.get(f"{name}_dollars"))
    if v is not None:
        return v
    cents = d.get(name)
    if isinstance(cents, int | float) and not isinstance(cents, bool):
        return D(cents) / 100
    return None


def _fp(d: Raw, name: str) -> Decimal | None:
    """Read ``{name}_fp`` (preferred) or the legacy integer field ``{name}``."""
    v = parse_dec(d.get(f"{name}_fp"))
    if v is not None:
        return v
    return parse_dec(d.get(name))


def _quote(p: Decimal | None) -> Decimal | None:
    """Empty-side sentinel normalization: anything outside (0, 1) -> None."""
    if p is None or p <= ZERO or p >= ONE:
        return None
    return p


def _str(d: Raw, key: str, default: str = "") -> str:
    v = d.get(key)
    return default if v is None else str(v)


def series_from_event_ticker(event_ticker: str) -> str:
    """Best-effort series ticker from an event ticker (``KXBTCD-26SEP2620`` -> ``KXBTCD``)."""
    return event_ticker.split("-", 1)[0] if event_ticker else ""


def parse_price_ranges(value: Any) -> tuple[PriceRange, ...]:
    """``[{start, end, step}, ...]`` -> sorted tuple of :class:`PriceRange` (default: cent grid)."""
    out: list[PriceRange] = []
    for r in value or ():
        try:
            if isinstance(r, Mapping):
                start, end, step = D(r["start"]), D(r["end"]), D(r["step"])
            else:
                start, end, step = (D(x) for x in r)
        except (KeyError, InvalidOperation, TypeError, ValueError):
            continue
        if step > 0 and end > start:
            out.append(PriceRange(start, end, step))
    return tuple(sorted(out, key=lambda r: r.start)) if out else DEFAULT_PRICE_RANGES


# --------------------------------------------------------------------------- Market


@dataclass(frozen=True, slots=True)
class Market:
    ticker: str
    event_ticker: str
    series_ticker: str
    title: str
    yes_sub_title: str
    no_sub_title: str
    status: str
    market_type: str
    open_time: datetime | None
    close_time: datetime | None
    expected_expiration_time: datetime | None
    latest_expiration_time: datetime | None
    can_close_early: bool
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    no_bid: Decimal | None
    no_ask: Decimal | None
    yes_bid_size: Decimal
    yes_ask_size: Decimal
    last_price: Decimal | None
    volume: Decimal
    volume_24h: Decimal
    open_interest: Decimal
    liquidity: Decimal
    result: str
    settlement_value: Decimal | None
    settlement_ts: datetime | None
    expiration_value: str
    price_level_structure: str
    price_ranges: tuple[PriceRange, ...]
    rules_primary: str
    rules_secondary: str
    strike_type: str
    floor_strike: Decimal | None
    cap_strike: Decimal | None
    custom_strike: Any
    early_close_condition: str
    exchange_index: int | None
    fee_waiver_expiration_time: datetime | None
    updated_time: datetime | None
    raw: Raw = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Raw, *, series_ticker: str | None = None) -> Market:
        """Parse a market dict (``/markets`` item, ``/markets/{t}`` ``market``, nested event market)."""
        if "market" in d and isinstance(d["market"], Mapping) and "ticker" not in d:
            d = d["market"]
        event_ticker = _str(d, "event_ticker")
        yes_bid = _quote(_dollars(d, "yes_bid"))
        yes_ask = _quote(_dollars(d, "yes_ask"))
        no_bid = _quote(_dollars(d, "no_bid"))
        no_ask = _quote(_dollars(d, "no_ask"))
        last = _dollars(d, "last_price")
        idx = d.get("exchange_index")
        return cls(
            ticker=_str(d, "ticker"),
            event_ticker=event_ticker,
            series_ticker=_str(d, "series_ticker") or series_ticker or series_from_event_ticker(event_ticker),
            title=_str(d, "title"),
            yes_sub_title=_str(d, "yes_sub_title") or _str(d, "subtitle"),
            no_sub_title=_str(d, "no_sub_title"),
            status=_str(d, "status"),
            market_type=_str(d, "market_type", "binary"),
            open_time=parse_ts(d.get("open_time")),
            close_time=parse_ts(d.get("close_time")),
            expected_expiration_time=parse_ts(d.get("expected_expiration_time")),
            latest_expiration_time=parse_ts(d.get("latest_expiration_time")),
            can_close_early=bool(d.get("can_close_early", False)),
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            yes_bid_size=(_fp(d, "yes_bid_size") or ZERO) if yes_bid is not None else ZERO,
            yes_ask_size=(_fp(d, "yes_ask_size") or ZERO) if yes_ask is not None else ZERO,
            last_price=last if last is not None and last > ZERO else None,
            volume=_fp(d, "volume") or ZERO,
            volume_24h=_fp(d, "volume_24h") or ZERO,
            open_interest=_fp(d, "open_interest") or ZERO,
            liquidity=_dollars(d, "liquidity") or ZERO,
            result=_str(d, "result"),
            settlement_value=_dollars(d, "settlement_value"),
            settlement_ts=parse_ts(d.get("settlement_ts")),
            expiration_value=_str(d, "expiration_value"),
            price_level_structure=_str(d, "price_level_structure"),
            price_ranges=parse_price_ranges(d.get("price_ranges")),
            rules_primary=_str(d, "rules_primary"),
            rules_secondary=_str(d, "rules_secondary"),
            strike_type=_str(d, "strike_type"),
            floor_strike=parse_dec(d.get("floor_strike")),
            cap_strike=parse_dec(d.get("cap_strike")),
            custom_strike=d.get("custom_strike"),
            early_close_condition=_str(d, "early_close_condition"),
            exchange_index=idx if isinstance(idx, int) and not isinstance(idx, bool) else None,
            fee_waiver_expiration_time=parse_ts(d.get("fee_waiver_expiration_time")),
            updated_time=parse_ts(d.get("updated_time")),
            raw=d,
        )

    # -- helpers ------------------------------------------------------------------

    @property
    def mid(self) -> Decimal | None:
        """YES mid price, or None if either side is empty."""
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def spread(self) -> Decimal | None:
        """YES ask - YES bid, or None if either side is empty."""
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid

    @property
    def is_open(self) -> bool:
        """Status-based: the market is ``active`` (see :meth:`is_tradable` for a time check)."""
        return self.status in OPEN_STATUSES

    @property
    def is_final(self) -> bool:
        """Paid out (``finalized``); ``result`` is authoritative."""
        return self.status in FINAL_STATUSES

    @property
    def is_determined(self) -> bool:
        """A result is known (``determined``/``disputed``/``amended``/``finalized``)."""
        return self.result != "" and self.status not in OPEN_STATUSES

    def is_tradable(self, now: datetime | None = None) -> bool:
        """Active and ``now`` before ``close_time``."""
        if not self.is_open:
            return False
        now = now or datetime.now(UTC)
        return self.close_time is None or now < self.close_time

    def bid(self, side: Side) -> Decimal | None:
        return self.yes_bid if side == "yes" else self.no_bid

    def ask(self, side: Side) -> Decimal | None:
        return self.yes_ask if side == "yes" else self.no_ask

    def tick_at(self, price: Number) -> Decimal:
        """Tick size of the price band containing ``price``."""
        return tick_at(price, self.price_ranges)

    def round_price(self, x: Number, mode: RoundingMode = "nearest", *, clamp: bool = True) -> Decimal:
        """Snap a (model) price onto this market's grid, clamped to valid order prices."""
        return snap_price(x, self.price_ranges, mode, clamp=clamp)

    def is_valid_price(self, price: Number) -> bool:
        """On this market's grid and strictly inside (0, 1)."""
        return is_valid_price(price, self.price_ranges)

    def payout_per_contract(self, side: Side) -> Decimal | None:
        """Settlement payout per contract of ``side`` once determined, else None.

        yes/no results pay 1/0; ``scalar`` pays ``settlement_value`` to YES and ``1 - value`` to NO.
        """
        if self.result == "yes":
            return ONE if side == "yes" else ZERO
        if self.result == "no":
            return ZERO if side == "yes" else ONE
        if self.result and self.settlement_value is not None:
            v = self.settlement_value
            return v if side == "yes" else ONE - v
        return None


# --------------------------------------------------------------------------- Event / Series


@dataclass(frozen=True, slots=True)
class Event:
    event_ticker: str
    series_ticker: str
    title: str
    sub_title: str
    category: str
    mutually_exclusive: bool
    collateral_return_type: str
    strike_date: datetime | None
    strike_period: str
    exchange_index: int | None
    fee_type_override: str | None
    fee_multiplier_override: Decimal | None
    markets: list[Market] = field(default_factory=list)
    raw: Raw = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Raw, markets: Iterable[Raw | Market] | None = None) -> Event:
        """Parse an event dict or a ``GET /events/{t}`` response (``{"event", "markets"}``).

        Nested ``markets`` (``with_nested_markets=true``) or the explicit ``markets``
        argument populate :attr:`markets`.
        """
        if "event" in d and isinstance(d["event"], Mapping):
            if markets is None:
                markets = d.get("markets")
            d = d["event"]
        if markets is None:
            markets = d.get("markets")
        series = _str(d, "series_ticker") or series_from_event_ticker(_str(d, "event_ticker"))
        parsed = [m if isinstance(m, Market) else Market.from_api(m, series_ticker=series) for m in markets or ()]
        idx = d.get("exchange_index")
        fto = d.get("fee_type_override")
        return cls(
            event_ticker=_str(d, "event_ticker"),
            series_ticker=series,
            title=_str(d, "title"),
            sub_title=_str(d, "sub_title"),
            category=_str(d, "category"),
            mutually_exclusive=bool(d.get("mutually_exclusive", False)),
            collateral_return_type=_str(d, "collateral_return_type"),
            strike_date=parse_ts(d.get("strike_date")),
            strike_period=_str(d, "strike_period"),
            exchange_index=idx if isinstance(idx, int) and not isinstance(idx, bool) else None,
            fee_type_override=str(fto) if fto else None,
            fee_multiplier_override=parse_dec(d.get("fee_multiplier_override")),
            markets=parsed,
            raw=d,
        )


@dataclass(frozen=True, slots=True)
class Series:
    ticker: str
    title: str
    category: str
    frequency: str
    fee_type: str
    fee_multiplier: Decimal
    tags: tuple[str, ...]
    exchange_index: int | None
    raw: Raw = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Raw) -> Series:
        """Parse a series dict or a ``GET /series/{t}`` response (``{"series": {...}}``)."""
        if "series" in d and isinstance(d["series"], Mapping):
            d = d["series"]
        mult = parse_dec(d.get("fee_multiplier"))
        idx = d.get("exchange_index")
        return cls(
            ticker=_str(d, "ticker"),
            title=_str(d, "title"),
            category=_str(d, "category"),
            frequency=_str(d, "frequency"),
            fee_type=_str(d, "fee_type") or "quadratic",
            fee_multiplier=ONE if mult is None else mult,
            tags=tuple(str(t) for t in d.get("tags") or ()),
            exchange_index=idx if isinstance(idx, int) and not isinstance(idx, bool) else None,
            raw=d,
        )


# --------------------------------------------------------------------------- Orderbook


class Level(NamedTuple):
    price: Decimal
    size: Decimal


def _levels(rows: Any, *, cents: bool = False) -> tuple[Level, ...]:
    out: list[Level] = []
    for row in rows or ():
        try:
            p, s = row[0], row[1]
        except (IndexError, KeyError, TypeError):
            continue
        price = parse_dec(p)
        size = parse_dec(s)
        if price is None or size is None or size <= 0:
            continue
        if cents:
            price = price / 100
        out.append(Level(price, size))
    out.sort(key=lambda lv: lv.price, reverse=True)
    return tuple(out)


def _mirror(levels: Sequence[Level]) -> tuple[Level, ...]:
    return tuple(Level(ONE - lv.price, lv.size) for lv in levels)


@dataclass(frozen=True, slots=True)
class Orderbook:
    """Bids for both sides, best-first. Asks are derived: a NO bid at p is a YES ask at 1 - p."""

    ticker: str
    ts: datetime
    yes_bids: tuple[Level, ...]
    no_bids: tuple[Level, ...]
    raw: Raw = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Raw, ticker: str | None = None, ts: datetime | None = None) -> Orderbook:
        """Parse ``{"orderbook_fp": {"yes_dollars": [[p, s], ...], "no_dollars": [...]}}``.

        Also accepts a batch entry (``{"ticker", "orderbook_fp"}``) and the legacy
        ``{"orderbook": {"yes": [[cents, n]], ...}}`` shape. ``null`` sides are empty.
        """
        tkr = ticker or _str(d, "ticker")
        book = d.get("orderbook_fp")
        if isinstance(book, Mapping):
            yes, no = _levels(book.get("yes_dollars")), _levels(book.get("no_dollars"))
        else:
            legacy = d.get("orderbook") if isinstance(d.get("orderbook"), Mapping) else d
            if "yes_dollars" in legacy or "no_dollars" in legacy:
                yes, no = _levels(legacy.get("yes_dollars")), _levels(legacy.get("no_dollars"))
            else:
                yes = _levels(legacy.get("yes"), cents=True)
                no = _levels(legacy.get("no"), cents=True)
        return cls(ticker=tkr, ts=ts or datetime.now(UTC), yes_bids=yes, no_bids=no, raw=d)

    @classmethod
    def from_levels(
        cls,
        ticker: str,
        yes_bids: Iterable[tuple[Number, Number]] = (),
        no_bids: Iterable[tuple[Number, Number]] = (),
        ts: datetime | None = None,
    ) -> Orderbook:
        """Build a book from (price, size) pairs in any order (tests, backtests)."""
        return cls(
            ticker=ticker,
            ts=ts or datetime.now(UTC),
            yes_bids=_levels([(D(p), D(s)) for p, s in yes_bids]),
            no_bids=_levels([(D(p), D(s)) for p, s in no_bids]),
        )

    # -- derived views ------------------------------------------------------------

    @property
    def yes_asks(self) -> tuple[Level, ...]:
        """YES asks, ascending price (best-first), mirrored from NO bids."""
        return _mirror(self.no_bids)

    @property
    def no_asks(self) -> tuple[Level, ...]:
        """NO asks, ascending price (best-first), mirrored from YES bids."""
        return _mirror(self.yes_bids)

    def bids(self, side: Side) -> tuple[Level, ...]:
        return self.yes_bids if side == "yes" else self.no_bids

    def asks(self, side: Side) -> tuple[Level, ...]:
        return self.yes_asks if side == "yes" else self.no_asks

    def best_bid(self, side: Side) -> Level | None:
        lv = self.bids(side)
        return lv[0] if lv else None

    def best_ask(self, side: Side) -> Level | None:
        lv = self.asks(side)
        return lv[0] if lv else None

    @property
    def best_yes_bid(self) -> Decimal | None:
        return self.yes_bids[0].price if self.yes_bids else None

    @property
    def best_no_bid(self) -> Decimal | None:
        return self.no_bids[0].price if self.no_bids else None

    @property
    def best_yes_ask(self) -> Decimal | None:
        return ONE - self.no_bids[0].price if self.no_bids else None

    @property
    def best_no_ask(self) -> Decimal | None:
        return ONE - self.yes_bids[0].price if self.yes_bids else None

    @property
    def mid(self) -> Decimal | None:
        """YES mid, or None if either side is empty."""
        b, a = self.best_yes_bid, self.best_yes_ask
        return None if b is None or a is None else (b + a) / 2

    @property
    def spread(self) -> Decimal | None:
        b, a = self.best_yes_bid, self.best_yes_ask
        return None if b is None or a is None else a - b

    @property
    def is_empty(self) -> bool:
        return not self.yes_bids and not self.no_bids

    def size_at(self, side: Side, price: Number, *, book: Literal["bid", "ask"] = "bid") -> Decimal:
        """Displayed size at exactly ``price`` on the ``side`` bid (or ask) ladder."""
        p = D(price)
        levels = self.bids(side) if book == "bid" else self.asks(side)
        return sum((lv.size for lv in levels if lv.price == p), ZERO)


# --------------------------------------------------------------------------- Trade / Candle


@dataclass(frozen=True, slots=True)
class Trade:
    trade_id: str
    ticker: str
    ts: datetime
    yes_price: Decimal
    no_price: Decimal
    count: Decimal
    taker_side: str  # outcome side of the taker: "yes" | "no"
    taker_book_side: str  # "bid" | "ask" (YES book)
    is_block_trade: bool
    raw: Raw = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Raw) -> Trade:
        yes = _dollars(d, "yes_price")
        no = _dollars(d, "no_price")
        if yes is None and no is not None:
            yes = ONE - no
        if no is None and yes is not None:
            no = ONE - yes
        ts = parse_ts(d.get("created_time"))
        return cls(
            trade_id=_str(d, "trade_id"),
            ticker=_str(d, "ticker"),
            ts=ts or datetime.fromtimestamp(0, tz=UTC),
            yes_price=yes if yes is not None else ZERO,
            no_price=no if no is not None else ZERO,
            count=_fp(d, "count") or ZERO,
            taker_side=_str(d, "taker_outcome_side") or _str(d, "taker_side"),
            taker_book_side=_str(d, "taker_book_side"),
            is_block_trade=bool(d.get("is_block_trade", False)),
            raw=d,
        )

    def price(self, side: Side) -> Decimal:
        """Trade price for ``side``."""
        return self.yes_price if side == "yes" else self.no_price


@dataclass(frozen=True, slots=True)
class OHLC:
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None

    @classmethod
    def from_api(cls, d: Raw | None, *, sentinel: Literal["bid", "ask", "none"] = "none") -> OHLC:
        """Parse ``{open_dollars, ...}`` or historical ``{open, ...}``; normalize empty-side sentinels."""
        if not isinstance(d, Mapping):
            return cls()

        def get(k: str) -> Decimal | None:
            v = parse_dec(d.get(f"{k}_dollars"))
            if v is None:
                v = parse_dec(d.get(k))
            if v is not None and sentinel != "none":
                v = _quote(v)
            return v

        return cls(open=get("open"), high=get("high"), low=get("low"), close=get("close"))


@dataclass(frozen=True, slots=True)
class Candle:
    """One candlestick. ``price`` OHLC fields are None when nothing traded in the period;
    bid/ask OHLC values are None where that side of the book was empty."""

    end_ts: datetime
    yes_bid: OHLC
    yes_ask: OHLC
    price: OHLC
    price_mean: Decimal | None
    price_previous: Decimal | None
    volume: Decimal
    open_interest: Decimal
    raw: Raw = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Raw) -> Candle:
        price = d.get("price") if isinstance(d.get("price"), Mapping) else {}

        def pget(k: str) -> Decimal | None:
            v = parse_dec(price.get(f"{k}_dollars"))
            return v if v is not None else parse_dec(price.get(k))

        end = parse_ts(d.get("end_period_ts"))
        return cls(
            end_ts=end or datetime.fromtimestamp(0, tz=UTC),
            yes_bid=OHLC.from_api(d.get("yes_bid"), sentinel="bid"),
            yes_ask=OHLC.from_api(d.get("yes_ask"), sentinel="ask"),
            price=OHLC.from_api(price),
            price_mean=pget("mean"),
            price_previous=pget("previous"),
            volume=_fp(d, "volume") or ZERO,
            open_interest=_fp(d, "open_interest") or ZERO,
            raw=d,
        )
