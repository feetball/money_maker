"""SpotPaperBroker: honest spot fill simulation for the Coinbase venue (contract §6) - PAPER ONLY.

Nothing here talks to an order endpoint or uses credentials. Market data comes from an
injected :class:`SpotMarketDataProvider` (``kalshibot.coinbase.marketdata.SpotMarketData`` in
the app, a static fake in tests); given the same clock and data every result is
deterministic. All ledger math is ``Decimal``: prices in USD per unit of base, quantities in
base units, cash in USD.

How each contract §6 rule is implemented
----------------------------------------
1. **Fresh book at execution.** ``place_order`` reads the product (``md.product``), then
   fetches the book **last** (``md.book(pid, max_age_s=2)``); the strategy's snapshot is never
   used. A book whose exchange ``time`` is older than ``book_max_age_s + BOOK_LAG_S`` (2 + 3 s)
   is re-fetched once with ``max_age_s=0``, else the order is rejected as stale.
2. **Market / IOC** orders walk the opposite side best-first: limit orders only at prices no
   worse than the limit; market orders (no limit) only within ``max_slippage_bps`` (default
   100) of the displayed best price (buys: ``<= best_ask x (1 + bps/1e4)``, sells:
   ``>= best_bid x (1 - bps/1e4)``). Buys by ``quote_size`` spend until the quote **including
   the fee** is exhausted (largest ``base_increment`` multiple per level that still fits);
   sells (and base-sized limit buys) by ``base_size``. The unfilled remainder is cancelled.
   Fees: taker rate. Status ``filled`` when the size/quote was used up, else ``cancelled``
   (``decision`` = ``partial`` / ``unfilled``).
3. **Consumed liquidity.** Paper fills do not remove real liquidity, so
   ``consumed[(pid, "ask"|"bid", price)] -> qty`` is subtracted from the displayed size on
   later walks (and on liquidation marks). Others' trades/cancels come out of the part we did
   not take: a level seen smaller than ``qty`` shrinks ``qty`` to it, and a vanished level drops
   the entry. An entry is dropped wholesale only after ``consumed_liquidity_ttl_s`` since our
   last take **and** once the level has been seen larger than right after that take (the
   makers re-quoted); entries not looked at for ``consumed_gc_s`` (1 day) are forgotten.
4. **Resting GTC** limit orders (``post_only``: rejected if it would cross - buy limit >= best
   ask, sell limit <= best bid). A non-post-only GTC first executes its marketable part as a
   taker (within the limit), and the rest rests with ``queue_ahead`` = the displayed size at
   its price on its own side (0 when it improves the best price). Resting orders fill only
   from **later public trades** (``md.trades_since``, strictly after placement, each print
   once via a persisted per-product trade-id cursor): a resting BUY at P fills from prints with
   ``maker_side == "buy"`` (a resting bid was hit) at price < P (trade-through, up to the print
   size) or at P after ``queue_ahead`` is consumed; a resting SELL symmetrically from
   ``maker_side == "sell"`` prints at price > P / at P. One print is shared by our orders in
   price-time priority (best limit, then time); prints at a price share one ``queue_ahead``
   burn. ``queue_ahead`` starts from a book fetched with ``max_age_s=0`` at placement and only
   shrinks: when a later book shows less at P it is lowered to that size and the book's time
   is remembered with the pre-bound value as a *shadow* queue (persisted). The tape (fetched
   first, CDN-cached up to ~6 s) usually lags that book, so at-price prints stamped at or
   before the bound's time are already reflected in it: they burn only the shadow queue and
   fill us only with what exceeds it; trade-through prints always fill. Book crossing without
   a print (asks <= P for a buy, bids >= P for a sell) fills **at the limit** only with
   ``coinbase.paper.fill_on_book_cross`` (default **off**: docs/coinbase_api_notes.md §6.1 rule
   4 - never fill from book movement alone). All resting fills pay the **maker** rate and fill
   at the limit price. A pass
   whose trade read failed processes neither the book nor expiries for that product (the book
   already reflects prints not yet seen), except that an order more than ``expiry_grace_s``
   past its expiry is expired anyway. Orders expire at ``expires_at`` (``expires_in_s``, else
   ``default_gtc_expiry_s`` = 3600 s); a ``delisted`` product cancels them.
5. **Constraints / rejections**: bad side / type / tif / sizes; product lookup failure; product
   not ``tradable`` (status, ``trading_disabled``, ``cancel_only``); ``limit_only`` products
   reject market orders; ``post_only`` products reject non-post-only orders; ``base_size``
   rounded **down** to ``base_increment``, ``quote_size`` down to ``quote_increment``, limit
   prices to ``quote_increment`` (buys down, sells up); zero after rounding -> reject; notional
   (``quote_size``, or ``base x limit`` / ``base x best bid``) < ``min_market_funds`` -> reject;
   **no shorting** (a sell may not exceed the strategy's held quantity minus what its resting
   sells already commit); insufficient free cash (buys by quote: ``quote_size``; base-sized
   buys: ``base x limit + taker fee``; GTC: the marketable part's cost + the resting reserve).
6. **Positions** per ``(strategy, product)``: quantity, ``cost_basis`` = USD paid **including
   buy fees** (so ``avg_cost`` is fee-inclusive); a sell realizes ``notional - fee - cost_basis
   x q / quantity`` (the whole cost basis when it empties the position). One shared USD cash
   pool; resting buys reserve ``remaining x limit + maker_fee`` (released on fill / cancel /
   expiry). Fees are cumulative per order and liquidity type: after each fill the order has
   paid exactly ``fee_for(cumulative notional)`` (cent rounding happens once per order, not per
   price level).
7. **Marks.** Every book the broker sees updates the product's mark (top ``MARK_LEVELS`` bid
   levels, best bid/ask). ``liquidation_value`` walks the bid ladder, net of bids we consumed,
   for the total quantity held by all strategies and shares it pro rata; quantity beyond the
   stored depth is valued at the deepest stored bid (a ladder-less mark uses best bid x qty).
   An empty bid side keeps the previous mark. A product never marked is valued at its cost
   basis. ``liquidation_value`` is **net of the exit taker fee** (``fee_for`` of each
   strategy's share of the ladder proceeds; reported as ``exit_fee``), so equity, unrealized
   P&L, drawdown, the daily-loss kill switch and risk see what selling would actually return.
   ``mid_value`` = quantity x mid (no fee). Equity = cash + reserved + liquidation value.
8. **Persistence.** Every order / fill / position change is written in one store transaction
   with the account row and the working state (consumed liquidity, trade cursors, marks,
   day-start equity), so a restart restores exactly. If the transaction fails (disk full,
   database locked) the in-memory ledger is restored: ``place_order`` returns the order as
   ``rejected`` ("not recorded"), the other mutations re-raise.

Concurrency: network reads happen without any lock; results are applied in short synchronous
sections under a ``threading.RLock`` (safe from the event loop and FastAPI worker threads).
``maintain`` passes are serialized with an ``asyncio.Lock``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from kalshibot.coinbase.fees import DEFAULT_TIER, FeeTier, fee_for, get_tier, resolve_tier
from kalshibot.coinbase.models import BookLevel, OrderBook, Product, Trade
from kalshibot.coinbase.paper import (
    VENUE,
    SpotAccountState,
    SpotFill,
    SpotOrder,
    SpotPortfolioView,
    SpotPosition,
    f8,
    iso,
    parse_iso,
)
from kalshibot.money import ONE, ZERO, D, ceil_to, floor_to

if TYPE_CHECKING:
    from kalshibot.coinbase.store import SpotStore

__all__ = [
    "BOOK_LAG_S",
    "MARK_LEVELS",
    "STORE_ERRORS",
    "OrderNotFoundError",
    "OrderNotOpenError",
    "SpotMark",
    "SpotMarketDataProvider",
    "SpotPaperBroker",
]

log = logging.getLogger(__name__)

#: Extra seconds a book may be older than ``book_max_age_s`` before it is re-fetched / refused.
BOOK_LAG_S = 3.0
#: Bid levels kept per product for liquidation marks.
MARK_LEVELS = 50
STORE_ERRORS: tuple[type[BaseException], ...] = (sqlite3.Error, OSError)
_Q12 = Decimal("0.000000000001")
_BPS = Decimal(10_000)


class OrderNotFoundError(KeyError):
    """No order with that id (API: 404)."""


class OrderNotOpenError(ValueError):
    """The order is no longer open (API: 409). ``.order`` is its final state."""

    def __init__(self, order: SpotOrder) -> None:
        super().__init__(f"order {order.id} is {order.status}, not open")
        self.order = order


@runtime_checkable
class SpotMarketDataProvider(Protocol):
    """What the broker needs from market data. Each method may be sync or ``async``.

    * ``book(product_id, max_age_s=2)`` -> :class:`OrderBook` (level 2, best-first) no older
      than ``max_age_s`` (``0`` = fetch now).
    * ``trades_since(product_id, since_trade_id)`` -> public trades with ``trade_id >
      since_trade_id`` (any order); ``since_trade_id=None`` -> the most recent page.
    * ``product(product_id)`` -> :class:`Product` (cached is fine).
    """

    def book(self, product_id: str, max_age_s: float = ...) -> Any: ...

    def trades_since(self, product_id: str, since_trade_id: int | None) -> Any: ...

    def product(self, product_id: str) -> Any: ...


async def _call(fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
    res = fn(*args, **kw)
    return await res if inspect.isawaitable(res) else res


def _q12(x: Decimal) -> Decimal:
    return x.quantize(_Q12, rounding=ROUND_HALF_EVEN)


def _size_at(levels: Sequence[BookLevel], price: Decimal, *, descending: bool) -> Decimal:
    """Displayed size at ``price`` in best-first ``levels`` (bids descending, asks ascending)."""
    for lv in levels:
        if lv.price == price:
            return lv.size
        if (lv.price < price) if descending else (lv.price > price):
            break
    return ZERO


# --------------------------------------------------------------------------- working state


@dataclass(slots=True)
class _Consumed:
    qty: Decimal  # our take that is still displayed
    ts: datetime  # our last take
    shown: Decimal  # displayed size right after that take
    grown: bool = False  # seen larger than ``shown`` since (re-quoted)
    seen: datetime | None = None  # last observation


@dataclass(frozen=True, slots=True)
class SpotMark:
    """Last observed book of a product: top bid ladder + best bid/ask."""

    product_id: str
    bids: tuple[tuple[Decimal, Decimal], ...]
    best_bid: Decimal | None
    best_ask: Decimal | None
    ts: datetime

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return self.best_bid

    def to_state(self) -> dict[str, Any]:
        return {"bids": [[str(p), str(s)] for p, s in self.bids],
                "bid": str(self.best_bid) if self.best_bid is not None else None,
                "ask": str(self.best_ask) if self.best_ask is not None else None, "ts": iso(self.ts)}

    @classmethod
    def from_state(cls, product_id: str, st: Mapping[str, Any]) -> SpotMark:
        return cls(product_id=product_id, bids=tuple((D(p), D(s)) for p, s in st.get("bids") or ()),
                   best_bid=D(st["bid"]) if st.get("bid") is not None else None,
                   best_ask=D(st["ask"]) if st.get("ask") is not None else None,
                   ts=parse_iso(st.get("ts")) or datetime.now(UTC))


@dataclass
class _Batch:
    orders: dict[int, SpotOrder] = field(default_factory=dict)
    fills: list[SpotFill] = field(default_factory=list)
    positions: dict[tuple[str, str], SpotPosition] = field(default_factory=dict)
    new_orders: set[int] = field(default_factory=set)


@dataclass
class _PollData:
    product: Product | None = None
    trades: list[Trade] | None = None  # None = the read failed
    book: OrderBook | None = None


_EMPTY_STATS: dict[str, Any] = {"orders": 0, "fills": 0, "fees": ZERO, "realized_pnl": ZERO, "trades": 0, "wins": 0}


def _copy_order(o: SpotOrder) -> SpotOrder:
    return dataclasses.replace(o)


def _cb_section(settings: Any) -> Any:
    """The ``coinbase`` section from a full Settings, a CoinbaseSettings, or None."""
    if settings is None:
        return None
    cb = getattr(settings, "coinbase", None)
    return cb if cb is not None else settings


def _resolve_fee_tier(cb: Any, fee_tier: FeeTier | str | None) -> FeeTier:
    if fee_tier is not None:
        return get_tier(fee_tier)
    if cb is None:
        return DEFAULT_TIER
    tier_fn = getattr(cb, "tier", None)
    if callable(tier_fn):
        t = tier_fn()
        if isinstance(t, FeeTier):
            return t
    name, rates = getattr(cb, "fee_tier", None), getattr(cb, "fee_rates", None)
    if name is None and rates is None:
        return DEFAULT_TIER
    return resolve_tier(name if isinstance(name, str | FeeTier) else None, rates)


# --------------------------------------------------------------------------- broker


class SpotPaperBroker:
    """Simulated Coinbase spot account. See the module docstring for the rules.

    ``settings`` is a :class:`~kalshibot.coinbase.config.CoinbaseSettings` (or a full
    ``Settings``: its ``coinbase`` section is used); explicit keyword arguments win.
    """

    def __init__(
        self,
        md: SpotMarketDataProvider,
        store: SpotStore | None = None,
        *,
        settings: Any = None,
        clock: Callable[[], datetime] | None = None,
        starting_balance: Any = None,
        fee_tier: FeeTier | str | None = None,
        max_slippage_bps: float | None = None,
        consumed_liquidity_ttl_s: float | None = None,
        default_gtc_expiry_s: float | None = None,
        book_max_age_s: float = 2.0,
        mark_max_age_s: float = 10.0,
        consumed_gc_s: float = 86400.0,
        expiry_grace_s: float = 300.0,
        fill_on_book_cross: bool | None = None,
        log_to_store: bool = True,
    ) -> None:
        cb = _cb_section(settings)
        paper = getattr(cb, "paper", None)
        self.md = md
        self.store = store
        md_clock = getattr(md, "clock", None)
        self.clock: Callable[[], datetime] = clock or (md_clock if callable(md_clock) else None) or (
            lambda: datetime.now(UTC))
        self.tier: FeeTier = _resolve_fee_tier(cb, fee_tier)
        self.max_slippage_bps = D(max_slippage_bps if max_slippage_bps is not None
                                  else getattr(paper, "max_slippage_bps", 100))
        self.ttl_s = float(consumed_liquidity_ttl_s if consumed_liquidity_ttl_s is not None
                           else getattr(paper, "consumed_liquidity_ttl_s", 300))
        self.default_gtc_expiry_s = float(default_gtc_expiry_s if default_gtc_expiry_s is not None
                                          else getattr(paper, "default_gtc_expiry_s", 3600))
        self.book_max_age_s = float(book_max_age_s)
        self.mark_max_age_s = float(mark_max_age_s)
        self.consumed_gc_s = max(float(consumed_gc_s), self.ttl_s)
        self.expiry_grace_s = float(expiry_grace_s)
        self.fill_on_book_cross = bool(fill_on_book_cross if fill_on_book_cross is not None
                                       else getattr(paper, "fill_on_book_cross", False))
        self.log_to_store = log_to_store
        default_start = starting_balance if starting_balance is not None else getattr(cb, "starting_balance", 1000)
        self._mu = threading.RLock()
        self._poll_lock = asyncio.Lock()
        self._listeners: list[Callable[[str, Any], None]] = []
        self._tiers: dict[tuple[str, Decimal, Decimal], FeeTier] = {}
        self._products: dict[str, Product] = {}
        self._load(D(default_start))

    # ------------------------------------------------------------------ state / restore

    def _init_state(self, starting_balance: Decimal) -> None:
        self.starting_balance = starting_balance
        self.cash = starting_balance  # free USD (reservations excluded)
        self.realized_pnl = ZERO
        self.fees_paid = ZERO
        self._open: dict[int, SpotOrder] = {}
        self._positions: dict[tuple[str, str], SpotPosition] = {}
        self._consumed: dict[tuple[str, str, Decimal], _Consumed] = {}
        self._cursors: dict[str, int] = {}  # product -> last public trade id processed
        #: order id -> (book time, shadow queue): ``queue_ahead`` was lowered to a book's displayed
        #: size at that time. At-price prints up to then are already reflected in the bound; they
        #: burn only the shadow queue (the queue as the prints alone would leave it).
        self._qbound: dict[int, tuple[datetime, Decimal]] = {}
        self._marks: dict[str, SpotMark] = {}
        self._day_start: tuple[str, Decimal] | None = None
        self._peak_equity: Decimal | None = None
        self._max_dd_pct = ZERO
        self._trades = 0
        self._wins = 0
        self._fill_count = 0
        self._stats: dict[str, dict[str, Any]] = {}
        self._persisted: dict[str, Any] = {}
        self._ids = {"order": 1, "fill": 1}

    def _load(self, default_start: Decimal) -> None:
        self._init_state(default_start)
        st = self.store
        if st is None:
            return
        acct = st.get_account()
        if acct is None:
            st.save_account(starting_balance=default_start, cash=default_start, ts=self._now())
            self._persisted = self._state_values()
            return
        self.starting_balance = acct["starting_balance"]
        self.cash = acct["cash"]
        self.realized_pnl = acct["realized_pnl"]
        self.fees_paid = acct["fees_paid"]
        self._peak_equity = acct["peak_equity"]
        self._max_dd_pct = acct["max_drawdown_pct"]
        self._open = {o.id: o for o in st.open_orders()}
        self._positions = {p.key: p for p in st.list_positions(open_only=True)}
        for row in st.get_kv("broker.consumed", []) or []:
            pid, side, price, qty, ts, shown, grown = row[:7]
            seen = parse_iso(row[7]) if len(row) > 7 else None
            when = parse_iso(ts) or self._now()
            self._consumed[(pid, side, D(price))] = _Consumed(D(qty), when, D(shown), bool(grown), seen or when)
        self._cursors = {str(k): int(v) for k, v in (st.get_kv("broker.cursors", {}) or {}).items()}
        for oid, (ts, shadow) in (st.get_kv("broker.queue_bounds", {}) or {}).items():
            when = parse_iso(ts)
            if when is not None and int(oid) in self._open:
                self._qbound[int(oid)] = (when, D(shadow))
        for pid, m in (st.get_kv("broker.marks", {}) or {}).items():
            self._marks[pid] = SpotMark.from_state(pid, m)
        ds = st.get_kv("broker.day_start")
        if ds:
            self._day_start = (str(ds[0]), D(ds[1]))
        self._trades, self._wins = st.trade_counts()
        self._fill_count = st.count("fills")
        self._stats = {k: dict(v) for k, v in st.strategy_summary().items()}
        self._ids = {"order": st.max_id("orders") + 1, "fill": st.max_id("fills") + 1}
        self._persisted = self._state_values()

    def _state_values(self) -> dict[str, Any]:
        """The working state as stored (``account`` row + ``broker.*`` kv)."""
        out: dict[str, Any] = {
            "account": (self.starting_balance, self.cash, self.realized_pnl, self.fees_paid, self._peak_equity,
                        self._max_dd_pct),
            "broker.consumed": [[p, s, str(px), str(e.qty), iso(e.ts), str(e.shown), e.grown, iso(e.seen)]
                                for (p, s, px), e in sorted(self._consumed.items())],
            "broker.cursors": dict(sorted(self._cursors.items())),
            "broker.queue_bounds": {str(i): [iso(ts), str(q)] for i, (ts, q) in sorted(self._qbound.items())
                                    if i in self._open},
            "broker.marks": {p: m.to_state() for p, m in sorted(self._marks.items())},
        }
        if self._day_start is not None:
            out["broker.day_start"] = [self._day_start[0], str(self._day_start[1])]
        return out

    def _write_state(self) -> dict[str, Any]:
        """Write changed working state inside the caller's transaction; returns what was written."""
        st = self.store
        assert st is not None
        written: dict[str, Any] = {}
        for key, value in self._state_values().items():
            if self._persisted.get(key) == value:
                continue
            if key == "account":
                st.save_account(starting_balance=value[0], cash=value[1], realized_pnl=value[2], fees_paid=value[3],
                                peak_equity=value[4], max_drawdown_pct=value[5], ts=self._now())
            else:
                st.set_kv(key, value)
            written[key] = value
        return written

    def _next(self, kind: str) -> int:
        with self._mu:
            i = self._ids[kind]
            self._ids[kind] = i + 1
            return i

    def _now(self) -> datetime:
        now = self.clock()
        return now if now.tzinfo else now.replace(tzinfo=UTC)

    def _snapshot(self) -> tuple[Any, ...]:
        return (self.starting_balance, self.cash, self.realized_pnl, self.fees_paid,
                {k: _copy_order(o) for k, o in self._open.items()},
                {k: dataclasses.replace(p) for k, p in self._positions.items()},
                {k: dataclasses.replace(e) for k, e in self._consumed.items()},
                dict(self._cursors), dict(self._marks), self._day_start, self._peak_equity, self._max_dd_pct,
                self._trades, self._wins, self._fill_count, {k: dict(v) for k, v in self._stats.items()},
                dict(self._persisted), dict(self._qbound))

    def _restore(self, snap: tuple[Any, ...]) -> None:
        (self.starting_balance, self.cash, self.realized_pnl, self.fees_paid, self._open, self._positions,
         self._consumed, self._cursors, self._marks, self._day_start, self._peak_equity, self._max_dd_pct,
         self._trades, self._wins, self._fill_count, self._stats, self._persisted, self._qbound) = snap

    @contextlib.contextmanager
    def _atomic(self) -> Iterator[None]:
        """Run a mutation; if anything raises (e.g. the store transaction), memory is restored."""
        snap = self._snapshot()
        try:
            yield
        except BaseException:
            self._restore(snap)
            raise

    def _commit(self, batch: _Batch) -> None:
        now = self._now()
        closed_sells: list[SpotOrder] = []
        for o in batch.orders.values():
            was_open = o.id in self._open or o.id in batch.new_orders
            if o.is_open:
                self._open[o.id] = o
            else:
                self._open.pop(o.id, None)
                if was_open and o.side == "sell" and o.filled_base > 0 and o.status != "rejected":
                    closed_sells.append(o)
        for key, p in batch.positions.items():
            if p.quantity > 0:
                self._positions[key] = p
            else:
                self._positions.pop(key, None)
        self._gc(now)
        written: dict[str, Any] = {}
        if self.store is not None:
            with self.store.transaction():
                for o in batch.orders.values():
                    self.store.upsert_order(o)
                for f in batch.fills:
                    self.store.insert_fill(f)
                for p in batch.positions.values():
                    self.store.upsert_position(p)
                written = self._write_state()
        # committed: nothing below can fail the ledger
        self._persisted.update(written)
        self._count_stats(batch, closed_sells)
        for f in batch.fills:
            self._emit("fill", f)
        for o in batch.orders.values():
            self._emit("order", o)

    def _count_stats(self, batch: _Batch, closed_sells: Sequence[SpotOrder]) -> None:
        def row(name: str) -> dict[str, Any]:
            return self._stats.setdefault(name, dict(_EMPTY_STATS))

        for oid in batch.new_orders:
            o = batch.orders.get(oid)
            if o is not None and o.status != "rejected":
                row(o.strategy)["orders"] += 1
        for f in batch.fills:
            r = row(f.strategy)
            r["fills"] += 1
            r["fees"] += f.fee
            if f.side == "sell":
                r["realized_pnl"] += f.realized_pnl
        self._fill_count += len(batch.fills)
        for o in closed_sells:
            r = row(o.strategy)
            r["trades"] += 1
            self._trades += 1
            if o.realized_pnl > 0:
                r["wins"] += 1
                self._wins += 1

    def _gc(self, now: datetime) -> None:
        """Forget consumed levels nobody looked at for ``consumed_gc_s`` and marks of products
        neither held nor with an open order."""
        for k in [k for k, e in self._consumed.items()
                  if (now - (e.seen or e.ts)).total_seconds() >= self.consumed_gc_s]:
            del self._consumed[k]
        keep = self._live_products()
        for pid in [p for p in self._marks if p not in keep]:
            del self._marks[pid]
        for oid in [i for i in self._qbound if i not in self._open]:
            del self._qbound[oid]

    def _live_products(self) -> set[str]:
        return {p.product_id for p in self._positions.values() if p.quantity > 0} | {
            o.product_id for o in self._open.values()}

    # ------------------------------------------------------------------ events / logging

    def subscribe(self, fn: Callable[[str, Any], None]) -> Callable[[], None]:
        """Register ``fn(kind, obj)`` for kinds ``order`` / ``fill`` (called after commit)."""
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def _emit(self, kind: str, obj: Any) -> None:
        for fn in list(self._listeners):
            try:
                fn(kind, obj)
            except Exception:  # listeners must never break the ledger
                log.exception("coinbase broker listener failed for %s", kind)

    def _log(self, level: str, kind: str, message: str, **data: Any) -> None:
        log.log(getattr(logging, level.upper(), logging.INFO), "coinbase %s: %s", kind, message)
        if self.store is not None and self.log_to_store:
            try:
                self.store.insert_log(level, kind, message, {"venue": VENUE, **data}, ts=self._now())
            except Exception:  # logging must not fail trading
                log.exception("failed to write coinbase log row")

    # ------------------------------------------------------------------ fees

    def _order_tier(self, o: SpotOrder) -> FeeTier:
        key = (o.fee_tier, o.maker_rate, o.taker_rate)
        t = self._tiers.get(key)
        if t is None:
            t = FeeTier(o.fee_tier or "custom", o.maker_rate, o.taker_rate)
            self._tiers[key] = t
        return t

    def _fill_fee(self, o: SpotOrder, notional: Decimal, *, is_taker: bool) -> Decimal:
        """Fee for the next fill: ``fee_for(cumulative notional) - fees charged so far``."""
        tier = self._order_tier(o)
        if is_taker:
            return fee_for(o.taker_notional + notional, is_taker=True, tier=tier) - o.taker_fees
        return fee_for(o.maker_notional + notional, is_taker=False, tier=tier) - o.maker_fees

    # ------------------------------------------------------------------ consumed liquidity

    def _observe(self, key: tuple[str, str, Decimal], displayed: Decimal, now: datetime) -> Decimal:
        """Reconcile a consumed entry with the level's displayed size; returns what is still ours."""
        e = self._consumed.get(key)
        if e is None:
            return ZERO
        if displayed < e.qty:
            e.qty = displayed  # others' trades / cancels: the rest of our take has left the book
        if displayed > e.shown:
            e.grown = True
        e.seen = now
        if e.qty <= 0 or (e.grown and (now - e.ts).total_seconds() >= self.ttl_s):
            del self._consumed[key]
            return ZERO
        return e.qty

    def _available(self, pid: str, side: str, price: Decimal, displayed: Decimal, now: datetime) -> Decimal:
        return max(ZERO, displayed - self._observe((pid, side, price), displayed, now))

    def _consume(self, pid: str, side: str, price: Decimal, qty: Decimal, displayed: Decimal, now: datetime) -> None:
        key = (pid, side, price)
        e = self._consumed.get(key)
        if e is None:
            self._consumed[key] = _Consumed(qty, now, displayed, False, now)
        else:
            e.qty += qty
            e.ts = now
            e.shown = displayed
            e.grown = False
            e.seen = now

    def _reconcile(self, pid: str, book: OrderBook, now: datetime) -> None:
        """Observe every consumed level of ``pid`` in a fresh book (vanished levels drop)."""
        keys = [k for k in self._consumed if k[0] == pid]
        if not keys:
            return
        asks = {lv.price: lv.size for lv in book.asks}
        bids = {lv.price: lv.size for lv in book.bids}
        for key in keys:
            _, side, price = key
            self._observe(key, (asks if side == "ask" else bids).get(price, ZERO), now)

    def consumed(self, product_id: str | None = None) -> dict[tuple[str, str, Decimal], Decimal]:
        """Consumed-liquidity entries ``{(product_id, "ask"|"bid", price): qty}`` (UI / tests)."""
        with self._mu:
            return {k: e.qty for k, e in self._consumed.items() if product_id is None or k[0] == product_id}

    # ------------------------------------------------------------------ marks

    def _book_age_s(self, book: OrderBook, now: datetime) -> float:
        t = getattr(book, "time", None)
        if t is None:
            return 0.0
        t = t if t.tzinfo else t.replace(tzinfo=UTC)
        return max(0.0, (now - t).total_seconds())

    def _observe_book(self, pid: str, book: OrderBook, now: datetime) -> None:
        """A fresh book: reconcile consumed levels and update the mark (an empty bid side keeps
        the previous ladder)."""
        self._reconcile(pid, book, now)
        t = book.time if book.time is not None else now
        t = min(t if t.tzinfo else t.replace(tzinfo=UTC), now)
        prev = self._marks.get(pid)
        if book.bids:
            bids = tuple((lv.price, lv.size) for lv in book.bids[:MARK_LEVELS])
            self._marks[pid] = SpotMark(pid, bids, book.bids[0].price, book.best_ask, t)
        elif prev is not None and book.best_ask is not None:
            self._marks[pid] = dataclasses.replace(prev, best_ask=book.best_ask)

    def _liquidation(self, pid: str, qty: Decimal, mk: SpotMark) -> Decimal:
        """Proceeds (before fees) of selling ``qty`` into the marked bids, net of bids we consumed."""
        if qty <= 0:
            return ZERO
        if not mk.bids:
            return qty * mk.best_bid if mk.best_bid is not None else ZERO
        left, value, last = qty, ZERO, mk.bids[0][0]
        for price, size in mk.bids:
            e = self._consumed.get((pid, "bid", price))
            avail = size - min(e.qty, size) if e is not None else size
            last = price
            if avail <= 0:
                continue
            take = min(left, avail)
            value += take * price
            left -= take
            if left <= 0:
                return value
        return value + left * last  # beyond the stored depth: the deepest stored bid

    def _position_values(self) -> dict[tuple[str, str], tuple[Decimal | None, Decimal | None]]:
        """``{(strategy, pid): (liquidation value | None, mid value | None)}``; ``None`` = never marked.
        All strategies holding a product walk its ladder together and share the result pro rata.
        The liquidation value is net of the exit (taker) fee - see :meth:`_exit_fees`."""
        return {k: (lv, mv) for k, (lv, _fee, mv) in self._position_marks().items()}

    def _position_marks(self) -> dict[tuple[str, str], tuple[Decimal | None, Decimal | None, Decimal | None]]:
        """``{(strategy, pid): (net liquidation value, exit fee, mid value)}`` (``None`` = never marked)."""
        by_pid: dict[str, list[SpotPosition]] = {}
        for p in self._positions.values():
            if p.quantity > 0:
                by_pid.setdefault(p.product_id, []).append(p)
        out: dict[tuple[str, str], tuple[Decimal | None, Decimal | None, Decimal | None]] = {}
        for pid, ps in by_pid.items():
            mk = self._marks.get(pid)
            if mk is None:
                for p in ps:
                    out[p.key] = (None, None, None)
                continue
            ps.sort(key=lambda p: p.key)
            total = sum((p.quantity for p in ps), ZERO)
            gross_total = self._liquidation(pid, total, mk)
            mid = mk.mid
            given = ZERO
            for i, p in enumerate(ps):
                if i == len(ps) - 1:
                    gross = gross_total - given  # the last one takes the remainder: parts sum exactly
                else:
                    gross = _q12(gross_total * p.quantity / total)
                    given += gross
                # each strategy sells its own holding in its own order: its own cent-rounded fee
                fee = fee_for(gross, is_taker=True, tier=self.tier) if gross > 0 else ZERO
                out[p.key] = (gross - fee, fee, p.quantity * mid if mid is not None else None)
        return out

    def mark_of(self, product_id: str) -> SpotMark | None:
        with self._mu:
            return self._marks.get(product_id)

    # ------------------------------------------------------------------ order construction

    def _new_order(self, intent: Any, now: datetime) -> SpotOrder:
        def g(name: str, default: Any = None) -> Any:
            v = getattr(intent, name, None)
            if v is None and isinstance(intent, Mapping):
                v = intent.get(name)
            return default if v is None else v

        def dec(name: str) -> Decimal | None | str:
            v = g(name)
            if v is None:
                return None
            try:
                d = D(v)
            except (TypeError, ValueError, ArithmeticError):
                return f"{name} is not a number: {v!r}"
            return d if d.is_finite() else f"{name} is not finite: {v!r}"

        side = str(g("side", "")).lower()
        otype = str(g("order_type", "market")).lower()
        tif = str(g("tif", "ioc")).lower()
        post_only = bool(g("post_only", False))
        tw, eb = g("target_weight"), g("expected_edge_bps")
        o = SpotOrder(
            id=self._next("order"), product_id=str(g("product_id", "")), side=side,  # type: ignore[arg-type]
            order_type=otype, tif=tif, post_only=post_only,  # type: ignore[arg-type]
            strategy=str(g("strategy", "")), reason=str(g("reason", "")),
            target_weight=float(tw) if tw is not None else None,
            expected_edge_bps=float(eb) if eb is not None else None,
            created_at=now, updated_at=now, fee_tier=self.tier.name, maker_rate=self.tier.maker_rate,
            taker_rate=self.tier.taker_rate)
        values = {k: dec(k) for k in ("quote_size", "base_size", "limit_price")}
        nums: dict[str, Decimal | None] = {}
        for k, v in values.items():
            if isinstance(v, str):
                return self._rejected(o, v, now)
            nums[k] = v
        o.quote_size, o.base_size, o.limit_price = nums["quote_size"], nums["base_size"], nums["limit_price"]
        why = self._validate_shape(o)
        if why:
            return self._rejected(o, why, now)
        if o.order_type == "market":
            o.limit_price = None
        if o.tif == "gtc":
            exp = g("expires_in_s")
            secs = float(exp) if exp is not None and float(exp) > 0 else self.default_gtc_expiry_s
            o.expires_at = now + timedelta(seconds=secs)
        return o

    @staticmethod
    def _validate_shape(o: SpotOrder) -> str | None:
        if not o.product_id:
            return "product_id is required"
        if o.side not in ("buy", "sell"):
            return f"side must be 'buy' or 'sell', not {o.side!r}"
        if o.order_type not in ("market", "limit"):
            return f"order_type must be 'market' or 'limit', not {o.order_type!r}"
        if o.tif not in ("ioc", "gtc"):
            return f"tif must be 'ioc' or 'gtc', not {o.tif!r}"
        if o.order_type == "market" and o.tif == "gtc":
            return "market orders are immediate-or-cancel (tif='ioc')"
        if o.order_type == "limit" and (o.limit_price is None or o.limit_price <= 0):
            return "limit orders need a positive limit_price"
        if o.post_only and o.tif != "gtc":
            return "post_only needs a GTC limit order"
        if o.quote_size is not None and o.base_size is not None:
            return "give quote_size or base_size, not both"
        for name, v in (("quote_size", o.quote_size), ("base_size", o.base_size)):
            if v is not None and v <= 0:
                return f"{name} must be positive"
        if o.side == "sell":
            if o.base_size is None:
                return "sells are sized by base_size"
        elif o.order_type == "market":
            if o.quote_size is None:
                return "market buys are sized by quote_size (USD incl. fee)"
        elif o.quote_size is None and o.base_size is None:
            return "limit buys need quote_size or base_size"
        return None

    def _rejected(self, o: SpotOrder, reason: str, now: datetime) -> SpotOrder:
        o.status = "rejected"
        o.status_reason = reason
        o.updated_at = now
        o.reserved = ZERO
        return o

    @staticmethod
    def _check_product(o: SpotOrder, product: Product) -> str | None:
        if not product.tradable:
            return (f"product {product.product_id} is not tradable (status={product.status}, "
                    f"trading_disabled={product.trading_disabled}, cancel_only={product.cancel_only})")
        if product.limit_only and o.order_type == "market":
            return f"product {product.product_id} is limit-only: market orders are rejected"
        if product.post_only and not o.post_only:
            return f"product {product.product_id} is post-only: only post-only limit orders are accepted"
        return None

    def _round(self, o: SpotOrder, product: Product) -> str | None:
        """Round sizes down to the product's increments and the limit to ``quote_increment``
        (buys down, sells up); a size that rounds to zero is rejected."""
        if o.base_size is not None:
            b = floor_to(o.base_size, product.base_increment)
            if b <= 0:
                return f"base_size {o.base_size} is below base_increment {product.base_increment}"
            o.base_size = b
        if o.quote_size is not None:
            q = floor_to(o.quote_size, product.quote_increment)
            if q <= 0:
                return f"quote_size {o.quote_size} is below quote_increment {product.quote_increment}"
            o.quote_size = q
        if o.limit_price is not None:
            lp = (floor_to if o.side == "buy" else ceil_to)(o.limit_price, product.quote_increment)
            if lp <= 0:
                return f"limit_price {o.limit_price} is below quote_increment {product.quote_increment}"
            o.limit_price = lp
        return None

    # ------------------------------------------------------------------ helpers

    def _get_position(self, strategy: str, product: Product | None, pid: str, now: datetime) -> SpotPosition:
        key = (strategy, pid)
        p = self._positions.get(key)
        if p is None:
            stored = self.store.get_position(strategy, pid) if self.store is not None else None
            base = product.base_currency if product is not None else pid.split("-")[0]
            p = stored if stored is not None else SpotPosition(product_id=pid, strategy=strategy, base_currency=base)
            p.quantity, p.cost_basis = ZERO, ZERO  # a closed row keeps its realized P&L / fee history
            p.opened_at = now
            self._positions[key] = p
        return p

    def available_to_sell(self, strategy: str, product_id: str) -> Decimal:
        """Held quantity of ``strategy`` minus what its resting sells already commit."""
        with self._mu:
            p = self._positions.get((strategy, product_id))
            held = p.quantity if p is not None else ZERO
            committed = sum((o.remaining_base for o in self._open.values()
                             if o.side == "sell" and o.strategy == strategy and o.product_id == product_id), ZERO)
            return max(ZERO, held - committed)

    @staticmethod
    def _units(qty: Decimal, inc: Decimal) -> int:
        return int(qty / inc) if qty > 0 else 0  # int() truncates toward 0: floor for positives

    def _fill(self, o: SpotOrder, price: Decimal, qty: Decimal, *, is_taker: bool, ts: datetime,
              batch: _Batch, product: Product | None = None, from_reserve: bool = False,
              trade_id: int | None = None) -> SpotFill:
        """Apply one fill of ``qty`` at ``price`` to the order, position, cash and fees."""
        notional = price * qty
        fee = self._fill_fee(o, notional, is_taker=is_taker)
        rate = o.taker_rate if is_taker else o.maker_rate
        p = self._get_position(o.strategy, product or self._products.get(o.product_id), o.product_id, ts)
        realized = ZERO
        if o.side == "buy":
            cost = notional + fee
            if from_reserve:
                o.reserved -= cost
            else:
                self.cash -= cost
            p.quantity += qty
            p.cost_basis += cost
        else:
            if qty >= p.quantity:
                removed = p.cost_basis
            else:
                removed = _q12(p.cost_basis * qty / p.quantity)
            realized = notional - fee - removed
            p.quantity -= qty
            p.cost_basis -= removed
            p.realized_pnl += realized
            self.cash += notional - fee
            self.realized_pnl += realized
            o.realized_pnl += realized
        p.fees_paid += fee
        p.updated_at = ts
        self.fees_paid += fee
        o.filled_base += qty
        o.filled_quote += notional
        o.fees += fee
        if is_taker:
            o.taker_notional += notional
            o.taker_fees += fee
        else:
            o.maker_notional += notional
            o.maker_fees += fee
        o.updated_at = max(ts, o.updated_at) if o.updated_at is not None else ts
        f = SpotFill(id=self._next("fill"), order_id=o.id, product_id=o.product_id, side=o.side, base_size=qty,
                     price=price, notional=notional, fee=fee, fee_rate=rate, is_taker=is_taker, ts=ts,
                     strategy=o.strategy, realized_pnl=realized, trade_id=trade_id)
        batch.fills.append(f)
        batch.positions[p.key] = p
        batch.orders[o.id] = o
        return f

    def _finish(self, o: SpotOrder, status: str, reason: str, now: datetime, batch: _Batch) -> None:
        """Terminal state for an open order; releases its reserved cash."""
        if o.reserved:
            self.cash += o.reserved
            o.reserved = ZERO
        o.status = status  # type: ignore[assignment]
        o.status_reason = reason
        o.updated_at = now
        batch.orders[o.id] = o

    # ------------------------------------------------------------------ public: placement

    async def _fresh_book(self, pid: str, *, max_age_s: float | None = None) -> OrderBook | None:
        book = await _call(self.md.book, pid, max_age_s=self.book_max_age_s if max_age_s is None else max_age_s)
        if self._book_age_s(book, self._now()) > self.book_max_age_s + BOOK_LAG_S:
            book = await _call(self.md.book, pid, max_age_s=0)
            if self._book_age_s(book, self._now()) > self.book_max_age_s + BOOK_LAG_S:
                return None
        return book

    async def place_order(self, intent: Any) -> SpotOrder:
        """Simulate one order from a :class:`SpotOrderIntent` (or anything with its attributes).

        Always returns the :class:`SpotOrder`: rejections have ``status == "rejected"`` and
        ``status_reason`` (also when the store failed - then nothing was executed or recorded)."""
        now = self._now()
        order = self._new_order(intent, now)
        err: str | None = None
        product: Product | None = None
        book: OrderBook | None = None
        if order.status != "rejected":
            try:
                product = await _call(self.md.product, order.product_id)
            except Exception as e:
                err = f"product {order.product_id} unavailable: {e}"
            if product is not None:
                self._products[order.product_id] = product
                err = self._check_product(order, product)
            if err is None:
                try:
                    # last: as fresh as possible; a resting order's queue_ahead needs the current book
                    book = await self._fresh_book(order.product_id,
                                                  max_age_s=0 if order.tif == "gtc" else None)
                except Exception as e:
                    err = f"order book unavailable: {e}"
                else:
                    if book is None:
                        err = f"order book is stale (older than {self.book_max_age_s + BOOK_LAG_S:g}s)"
        pristine = _copy_order(order)
        with self._mu:
            try:
                with self._atomic():
                    batch = _Batch(new_orders={order.id})
                    now = self._now()
                    if order.status != "rejected":
                        if err is not None or product is None or book is None:
                            self._rejected(order, err or "not prepared", now)
                        else:
                            self._execute_new(order, product, book, now, batch)
                    batch.orders[order.id] = order
                    self._commit(batch)
            except STORE_ERRORS as e:
                why = f"not recorded: store error ({type(e).__name__}: {e}); nothing was executed"
                log.error("coinbase paper order %s: %s", pristine.id, why)
                return self._rejected(pristine, why, self._now())
        if order.status == "rejected":
            self._log("info", "order", f"order {order.id} {order.side} {order.product_id} rejected: "
                      f"{order.status_reason}", order_id=order.id, strategy=order.strategy,
                      product_id=order.product_id)
        return _copy_order(order)

    def _execute_new(self, o: SpotOrder, product: Product, book: OrderBook, now: datetime, batch: _Batch) -> None:
        why = self._round(o, product)
        if why:
            self._rejected(o, why, now)
            return
        self._observe_book(o.product_id, book, now)
        if o.tif == "gtc":
            self._place_resting(o, product, book, now, batch)
        elif o.side == "buy" and o.quote_size is not None:
            self._take_by_quote(o, product, book, now, batch)
        else:
            self._take_by_base(o, product, book, now, batch)

    def _walk_base(self, o: SpotOrder, product: Product, levels: Sequence[BookLevel], book_side: str,
                   bound: Decimal | None, qty: Decimal, now: datetime) -> list[tuple[Decimal, Decimal]]:
        """Best-first plan for up to ``qty`` within ``bound`` (buys: price <= bound, sells: >= bound)."""
        plan: list[tuple[Decimal, Decimal]] = []
        left = qty
        inc = product.base_increment
        for lv in levels:
            if left <= 0:
                break
            if bound is not None and ((lv.price > bound) if o.side == "buy" else (lv.price < bound)):
                break
            avail = self._available(o.product_id, book_side, lv.price, lv.size, now)
            take = floor_to(min(left, avail), inc) if avail > 0 else ZERO
            if take > 0:
                plan.append((lv.price, take))
                left -= take
        return plan

    def _market_bound(self, o: SpotOrder, book: OrderBook) -> Decimal | None:
        """Walk bound: the limit, or the slippage cap around the displayed best price."""
        if o.limit_price is not None:
            return o.limit_price
        slip = self.max_slippage_bps / _BPS
        if o.side == "buy":
            return book.best_ask * (ONE + slip) if book.best_ask is not None else None
        return book.best_bid * (ONE - slip) if book.best_bid is not None else None

    def _bound_text(self, o: SpotOrder) -> str:
        return f"limit {o.limit_price}" if o.limit_price is not None else f"{self.max_slippage_bps} bps slippage cap"

    def _execute_plan(self, o: SpotOrder, product: Product, plan: Sequence[tuple[Decimal, Decimal]],
                      book: OrderBook, book_side: str, now: datetime, batch: _Batch) -> None:
        levels = book.asks if book_side == "ask" else book.bids
        shown = {lv.price: lv.size for lv in levels}
        for price, qty in plan:
            self._consume(o.product_id, book_side, price, qty, shown.get(price, qty), now)
            self._fill(o, price, qty, is_taker=True, ts=now, batch=batch, product=product)

    def _take_by_quote(self, o: SpotOrder, product: Product, book: OrderBook, now: datetime, batch: _Batch) -> None:
        """Buy spending ``quote_size`` USD including the taker fee."""
        q = o.quote_size
        assert q is not None
        if q < product.min_market_funds:
            self._rejected(o, f"quote_size {q} is below min_market_funds {product.min_market_funds}", now)
            return
        if q > self.cash:
            self._rejected(o, f"insufficient cash: need {q}, free {self.cash}", now)
            return
        bound = self._market_bound(o, book)
        tier = self._order_tier(o)
        inc = product.base_increment
        plan: list[tuple[Decimal, Decimal]] = []
        spent = ZERO  # planned notional
        quote_bound = False
        if bound is not None:
            for lv in book.asks:
                if lv.price > bound:
                    break
                avail = self._available(o.product_id, "ask", lv.price, lv.size, now)
                max_units = self._units(avail, inc)
                if max_units <= 0:
                    continue
                unit_cost = inc * lv.price
                hi = min(max_units, self._units(q - spent, unit_cost))
                lo = 0
                while lo < hi:  # largest k in [0, hi] with cost(spent + k units) <= q (monotone)
                    mid = (lo + hi + 1) // 2
                    n = spent + mid * unit_cost
                    if n + fee_for(n, is_taker=True, tier=tier) <= q:
                        lo = mid
                    else:
                        hi = mid - 1
                if lo > 0:
                    plan.append((lv.price, lo * inc))
                    spent += lo * unit_cost
                if lo < max_units:
                    quote_bound = True  # the quote ran out at this level
                    break
        self._execute_plan(o, product, plan, book, "ask", now, batch)
        if quote_bound and o.filled_base > 0:
            self._finish(o, "filled", "", now, batch)
        elif o.filled_base > 0:
            self._finish(o, "cancelled", f"ioc remainder cancelled: no more asks within {self._bound_text(o)}",
                         now, batch)
        elif quote_bound:
            self._finish(o, "cancelled", "quote_size too small for one base_increment at the best ask", now, batch)
        else:
            self._finish(o, "cancelled", f"no asks within {self._bound_text(o)}", now, batch)

    def _take_by_base(self, o: SpotOrder, product: Product, book: OrderBook, now: datetime, batch: _Batch) -> None:
        """IOC by ``base_size``: sells (market / limit) and base-sized limit buys."""
        base = o.base_size
        assert base is not None
        ref = o.limit_price if o.limit_price is not None else book.best_bid
        if ref is not None and base * ref < product.min_market_funds:
            self._rejected(o, f"notional {base * ref} is below min_market_funds {product.min_market_funds}", now)
            return
        if o.side == "sell":
            avail = self.available_to_sell(o.strategy, o.product_id)
            if base > avail:
                self._rejected(o, f"no shorting: sell {base} > {avail} held by {o.strategy or '-'} "
                               f"(net of resting sells)", now)
                return
        else:
            assert o.limit_price is not None
            notional = base * o.limit_price
            need = notional + fee_for(notional, is_taker=True, tier=self._order_tier(o))
            if need > self.cash:
                self._rejected(o, f"insufficient cash: need {need}, free {self.cash}", now)
                return
        bound = self._market_bound(o, book)
        side = "ask" if o.side == "buy" else "bid"
        levels = book.asks if o.side == "buy" else book.bids
        plan = self._walk_base(o, product, levels, side, bound, base, now) if bound is not None else []
        self._execute_plan(o, product, plan, book, side, now, batch)
        if o.filled_base >= base:
            self._finish(o, "filled", "", now, batch)
        elif o.filled_base > 0:
            self._finish(o, "cancelled", f"ioc remainder cancelled: no more {side}s within {self._bound_text(o)}",
                         now, batch)
        else:
            self._finish(o, "cancelled", f"no {side}s within {self._bound_text(o)}", now, batch)

    def _gtc_base_from_quote(self, o: SpotOrder, product: Product) -> Decimal:
        """Largest ``base_increment`` multiple whose cost at the limit (+ fee) fits ``quote_size``."""
        assert o.quote_size is not None and o.limit_price is not None
        tier = self._order_tier(o)
        is_taker = not o.post_only  # a non-post-only order may execute as taker: size it for that fee
        unit = product.base_increment * o.limit_price
        lo, hi = 0, self._units(o.quote_size, unit)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            n = mid * unit
            if n + fee_for(n, is_taker=is_taker, tier=tier) <= o.quote_size:
                lo = mid
            else:
                hi = mid - 1
        return lo * product.base_increment

    def _place_resting(self, o: SpotOrder, product: Product, book: OrderBook, now: datetime, batch: _Batch) -> None:
        assert o.limit_price is not None
        lim = o.limit_price
        if o.base_size is None:
            o.base_size = self._gtc_base_from_quote(o, product)
            if o.base_size <= 0:
                self._rejected(o, f"quote_size {o.quote_size} buys less than one base_increment at {lim}", now)
                return
        base = o.base_size
        if base * lim < product.min_market_funds:
            self._rejected(o, f"notional {base * lim} is below min_market_funds {product.min_market_funds}", now)
            return
        crosses = ((book.best_ask is not None and lim >= book.best_ask) if o.side == "buy"
                   else (book.best_bid is not None and lim <= book.best_bid))
        if o.post_only and crosses:
            best = book.best_ask if o.side == "buy" else book.best_bid
            self._rejected(o, f"post-only order would cross the book (limit {lim}, best "
                           f"{'ask' if o.side == 'buy' else 'bid'} {best})", now)
            return
        if o.side == "sell":
            avail = self.available_to_sell(o.strategy, o.product_id)
            if base > avail:
                self._rejected(o, f"no shorting: sell {base} > {avail} held by {o.strategy or '-'} "
                               f"(net of resting sells)", now)
                return
        side = "ask" if o.side == "buy" else "bid"
        levels = book.asks if o.side == "buy" else book.bids
        plan = self._walk_base(o, product, levels, side, lim, base, now) if crosses else []
        tier = self._order_tier(o)
        taken = sum((q for _, q in plan), ZERO)
        rest = base - taken
        reserve = ZERO
        if o.side == "buy":
            taker_n = sum((p * q for p, q in plan), ZERO)
            taker_cost = taker_n + fee_for(taker_n, is_taker=True, tier=tier)
            rest_n = rest * lim
            reserve = rest_n + fee_for(rest_n, is_taker=False, tier=tier) if rest > 0 else ZERO
            if taker_cost + reserve > self.cash:
                self._rejected(o, f"insufficient cash: need {taker_cost + reserve}, free {self.cash}", now)
                return
        self._execute_plan(o, product, plan, book, side, now, batch)
        if rest <= 0:
            self._finish(o, "filled", "", now, batch)
            return
        own = book.bids if o.side == "buy" else book.asks
        o.queue_ahead = _size_at(own, lim, descending=o.side == "buy")
        if reserve > 0:
            self.cash -= reserve
            o.reserved = reserve
        o.status = "partially_filled" if o.filled_base > 0 else "open"
        o.updated_at = now
        batch.orders[o.id] = o

    # ------------------------------------------------------------------ public: cancel

    async def cancel_order(self, order_id: int, reason: str = "", *, sync: bool = True) -> SpotOrder:
        """Cancel a resting order. With ``sync`` (default) the product's public trades and book
        are processed first, so prints that reached the exchange before the cancel still fill
        it (the result may then be ``filled``). Raises :class:`OrderNotFoundError` for an
        unknown id and :class:`OrderNotOpenError` when the order was not open."""
        with self._mu:
            o = self._open.get(order_id)
        if o is None:
            stored = self.get_order(order_id)
            if stored is None:
                raise OrderNotFoundError(order_id)
            raise OrderNotOpenError(stored)
        if sync:
            try:
                await self.maintain(product_ids=[o.product_id], expire=False)
            except STORE_ERRORS:
                raise
            except Exception as e:  # the cancel itself must still go through
                log.warning("coinbase: syncing resting orders before a cancel failed: %s", e)
        with self._mu, self._atomic():
            cur = self._open.get(order_id)
            if cur is None:  # filled during the sync
                return self.get_order(order_id) or o
            batch = _Batch()
            self._finish(cur, "cancelled", reason or "cancelled", self._now(), batch)
            self._commit(batch)
            return _copy_order(cur)

    async def cancel_all(self, *, strategy: str | None = None, product_id: str | None = None,
                         side: str | None = None, reason: str = "cancelled") -> list[SpotOrder]:
        """Cancel every matching resting order at once (no trade sync), e.g. ``side="buy"`` when
        the kill switch engages. Returns the cancelled orders."""
        with self._mu, self._atomic():
            batch = _Batch()
            now = self._now()
            out = []
            for o in sorted(self._open.values(), key=lambda o: o.id):
                if ((strategy is None or o.strategy == strategy) and (product_id is None or o.product_id == product_id)
                        and (side is None or o.side == side)):
                    self._finish(o, "cancelled", reason, now, batch)
                    out.append(o)
            if out:
                self._commit(batch)
            return [_copy_order(o) for o in out]

    # ------------------------------------------------------------------ public: resting orders

    async def maintain(self, product_ids: Iterable[str] | None = None, *, expire: bool = True) -> list[SpotFill]:
        """Resting orders: fills from later public trades, book crossing, queue bound, expiry
        (engine: every ``maintenance_s``). Network reads first (product, trades, then books),
        then one atomic apply. A store failure restores memory and re-raises."""
        async with self._poll_lock:
            with self._mu:
                pids = sorted({o.product_id for o in self._open.values()})
                cursors = dict(self._cursors)
            if product_ids is not None:
                want = set(product_ids)
                pids = [p for p in pids if p in want]
            if not pids:
                return []
            data: dict[str, _PollData] = {p: _PollData() for p in pids}
            for pid in pids:
                d = data[pid]
                try:
                    d.product = await _call(self.md.product, pid)
                except Exception as e:
                    log.warning("coinbase resting orders: product %s unavailable: %s", pid, e)
                try:
                    d.trades = list(await _call(self.md.trades_since, pid, cursors.get(pid)))
                except Exception as e:
                    log.warning("coinbase resting orders: trades for %s unavailable: %s", pid, e)
            for pid in pids:  # books last: as fresh as possible
                try:
                    data[pid].book = await _call(self.md.book, pid, max_age_s=self.book_max_age_s)
                except Exception as e:
                    log.warning("coinbase resting orders: book for %s unavailable: %s", pid, e)
            with self._mu, self._atomic():
                now = self._now()
                batch = _Batch()
                for pid in pids:
                    d = data[pid]
                    if d.product is not None:
                        self._products[pid] = d.product
                    if d.book is not None and self._book_age_s(d.book, now) > self.book_max_age_s + BOOK_LAG_S:
                        d.book = None  # too old to cross against (prints are still processed)
                    orders = [o for o in self._open.values() if o.product_id == pid]
                    if orders:
                        self._process_product(pid, orders, d, now, batch, expire=expire)
                self._commit(batch)
            return list(batch.fills)

    @staticmethod
    def _priority(o: SpotOrder) -> tuple[Any, ...]:
        """Price-time priority among our resting orders of one side (best limit first)."""
        lim = o.limit_price or ZERO
        return (-lim if o.side == "buy" else lim, o.created_at or datetime.min.replace(tzinfo=UTC), o.id)

    def _process_product(self, pid: str, orders: list[SpotOrder], d: _PollData, now: datetime,
                         batch: _Batch, *, expire: bool = True) -> None:
        product = d.product or self._products.get(pid)
        inc = product.base_increment if product is not None else Decimal("0.00000001")
        # 1) later public trades (ascending id), shared by our orders in price-time priority
        if d.trades is not None:
            cur = self._cursors.get(pid)
            new = sorted((t for t in d.trades if cur is None or t.trade_id > cur), key=lambda t: t.trade_id)
            for t in new:
                elig = sorted((o for o in orders if o.is_open and o.side == t.maker_side
                               and o.created_at is not None and t.time > o.created_at
                               and (o.expires_at is None or t.time < o.expires_at)), key=self._priority)
                left = t.size
                burned = ZERO  # real size at the print's price that this print went through
                for o in elig:
                    if left <= 0:
                        break
                    lim = o.limit_price
                    assert lim is not None
                    through = t.price < lim if o.side == "buy" else t.price > lim
                    if not through and t.price != lim:
                        break  # sorted best-first: the rest are worse than the print too
                    if through:
                        o.queue_ahead = ZERO  # the print cleared our price level
                        self._qbound.pop(o.id, None)
                    else:
                        qb = self._qbound.get(o.id)
                        # a print from before the book that bounded queue_ahead is already in that
                        # bound: it burns only the shadow queue (and fills us only past it)
                        pre = qb is not None and t.time <= qb[0]
                        qa = (qb[1] if pre and qb is not None else o.queue_ahead) or ZERO
                        burn = min(max(ZERO, qa - burned), left)
                        left -= burn
                        burned += burn
                        if pre and qb is not None:
                            self._qbound[o.id] = (qb[0], max(ZERO, qa - burned))
                        else:
                            o.queue_ahead = max(ZERO, qa - burned)
                    take = floor_to(min(left, o.remaining_base), inc)
                    if take <= 0:
                        continue
                    left -= take
                    self._fill(o, lim, take, is_taker=False, ts=t.time, batch=batch, product=product,
                               from_reserve=o.side == "buy", trade_id=t.trade_id)
                    if o.remaining_base <= 0:
                        self._finish(o, "filled", "", t.time, batch)
                    else:
                        o.status = "partially_filled"
            if new:
                self._cursors[pid] = new[-1].trade_id
            elif cur is None and d.trades:
                self._cursors[pid] = max(t.trade_id for t in d.trades)
        # 2) the live book: queue bound and crossing fills - only after this pass read the prints
        #    (the book already reflects prints; crossing first would let them fill us again)
        book = d.book
        if book is not None and d.trades is not None:
            self._observe_book(pid, book, now)
            tradable = product is not None and product.tradable
            for o in sorted((o for o in orders if o.is_open), key=lambda o: (o.side, self._priority(o))):
                lim = o.limit_price
                assert lim is not None
                own = book.bids if o.side == "buy" else book.asks
                shown = _size_at(own, lim, descending=o.side == "buy")
                if o.queue_ahead is None or shown < o.queue_ahead:
                    if o.queue_ahead is not None:
                        # the book may already reflect prints the (CDN-cached) tape has not delivered:
                        # remember when, so those prints are not burned against the bound again
                        bt = book.time or now
                        bt = bt if bt.tzinfo else bt.replace(tzinfo=UTC)
                        prev = self._qbound.get(o.id)
                        self._qbound[o.id] = ((bt, o.queue_ahead) if prev is None
                                              else (max(prev[0], bt), max(prev[1], o.queue_ahead)))
                    o.queue_ahead = shown  # people ahead of us can only leave
                if (not self.fill_on_book_cross or not tradable
                        or (o.expires_at is not None and now >= o.expires_at)):
                    continue
                side = "ask" if o.side == "buy" else "bid"
                crossed = False
                for lv in (book.asks if o.side == "buy" else book.bids):
                    if o.remaining_base <= 0 or ((lv.price > lim) if o.side == "buy" else (lv.price < lim)):
                        break
                    take = floor_to(min(o.remaining_base, self._available(pid, side, lv.price, lv.size, now)), inc)
                    if take > 0:
                        crossed = True
                        self._consume(pid, side, lv.price, take, lv.size, now)
                        self._fill(o, lim, take, is_taker=False, ts=now, batch=batch, product=product,
                                   from_reserve=o.side == "buy")
                if crossed:
                    o.queue_ahead = ZERO
                    self._qbound.pop(o.id, None)
                    if o.remaining_base <= 0:
                        self._finish(o, "filled", "", now, batch)
                    else:
                        o.status = "partially_filled"
        # 3) expiry - once the prints before it were read (or long past it: the tape keeps failing)
        for o in orders:
            if not o.is_open:
                batch.orders[o.id] = o
                continue
            if expire and o.expires_at is not None and now >= o.expires_at and (
                    d.trades is not None or (now - o.expires_at).total_seconds() >= self.expiry_grace_s):
                self._finish(o, "expired", "gtc expiry reached", now, batch)
                self._log("info", "order", f"order {o.id} {o.side} {o.product_id} expired "
                          f"(filled {o.filled_base} of {o.base_size})", order_id=o.id, strategy=o.strategy)
            elif product is not None and product.status == "delisted":
                self._finish(o, "cancelled", "product delisted", now, batch)
            batch.orders[o.id] = o

    # ------------------------------------------------------------------ public: marks / snapshots

    async def mark(self) -> None:
        """Refresh the marks of every held product (and products with resting orders) from
        books no older than ``mark_max_age_s`` and persist them."""
        with self._mu:
            pids = sorted(self._live_products())
        books: dict[str, OrderBook] = {}
        for pid in pids:
            try:
                books[pid] = await _call(self.md.book, pid, max_age_s=self.mark_max_age_s)
            except Exception as e:
                log.warning("coinbase mark: book for %s unavailable: %s", pid, e)
        with self._mu, self._atomic():
            now = self._now()
            for pid, b in books.items():
                self._observe_book(pid, b, now)
            self._gc(now)
            if self.store is not None:
                with self.store.transaction():
                    written = self._write_state()
                self._persisted.update(written)

    def equity_snapshot(self) -> dict[str, Any]:
        """Write an ``equity_snapshots`` row from the current marks (call :meth:`mark` first),
        update the peak / max drawdown, and return the row as JSON (``GET equity`` shape)."""
        with self._mu, self._atomic():
            a = self.account()
            self._peak_equity = max(self._peak_equity, a.equity) if self._peak_equity is not None else a.equity
            self._max_dd_pct = a.max_drawdown_pct
            if self.store is not None:
                with self.store.transaction():
                    self.store.insert_equity_snapshot(
                        ts=a.ts, equity=a.equity, equity_mid=a.equity_mid, cash=a.cash,
                        reserved_cash=a.reserved_cash, positions_value=a.positions_liquidation_value,
                        positions_mid_value=a.positions_mid_value, realized_pnl=a.realized_pnl,
                        unrealized_pnl=a.unrealized_pnl)
                    written = self._write_state()
                self._persisted.update(written)
        return {"venue": VENUE, "ts": iso(a.ts), "equity": f8(a.equity), "equity_mid": f8(a.equity_mid),
                "cash": f8(a.cash), "reserved_cash": f8(a.reserved_cash),
                "positions_value": f8(a.positions_liquidation_value), "realized_pnl": f8(a.realized_pnl),
                "unrealized_pnl": f8(a.unrealized_pnl)}

    # ------------------------------------------------------------------ public: views

    @property
    def reserved_cash(self) -> Decimal:
        with self._mu:
            return sum((o.reserved for o in self._open.values()), ZERO)

    def get_order(self, order_id: int) -> SpotOrder | None:
        with self._mu:
            o = self._open.get(order_id)
            if o is not None:
                return _copy_order(o)
        return self.store.get_order(order_id) if self.store is not None else None

    def open_orders(self, strategy: str | None = None, *, product_id: str | None = None) -> list[SpotOrder]:
        with self._mu:
            return [_copy_order(o) for o in sorted(self._open.values(), key=lambda o: o.id)
                    if (strategy is None or o.strategy == strategy)
                    and (product_id is None or o.product_id == product_id)]

    def _marked(self, marks: Mapping[tuple[str, str], tuple[Decimal | None, Decimal | None, Decimal | None]],
                strategy: str | None) -> list[SpotPosition]:
        out = []
        for p in sorted(self._positions.values(), key=lambda p: (p.product_id, p.strategy)):
            if p.quantity <= 0 or (strategy is not None and p.strategy != strategy):
                continue
            c = dataclasses.replace(p)
            lv, fee, mv = marks.get(p.key, (None, None, None))
            mk = self._marks.get(p.product_id)
            c.liquidation_value, c.exit_fee, c.mid_value = lv, fee, mv
            if mk is not None:
                c.best_bid, c.mid_price, c.mark_ts = mk.best_bid, mk.mid, mk.ts
            out.append(c)
        return out

    def positions(self, strategy: str | None = None) -> list[SpotPosition]:
        """Open positions (quantity > 0) with their marks filled in (copies)."""
        with self._mu:
            return self._marked(self._position_marks(), strategy)

    def _touch_day(self, equity: Decimal, now: datetime) -> Decimal:
        day = now.astimezone(UTC).date().isoformat()
        if self._day_start is None or self._day_start[0] != day:
            self._day_start = (day, equity)
        return self._day_start[1]

    def account(self) -> SpotAccountState:
        """Account snapshot from the current marks (``mark()`` refreshes them)."""
        with self._mu:
            now = self._now()
            marks = self._position_marks()
            values = {k: (lv_, mv_) for k, (lv_, _f, mv_) in marks.items()}
            exit_fee = sum((f for _l, f, _m in marks.values() if f is not None), ZERO)
            lv = mv = cost = ZERO
            n = 0
            for p in self._positions.values():
                if p.quantity <= 0:
                    continue
                n += 1
                plv, pmv = values.get(p.key, (None, None))
                lv += plv if plv is not None else p.cost_basis
                mv += pmv if pmv is not None else (plv if plv is not None else p.cost_basis)
                cost += p.cost_basis
            reserved = sum((o.reserved for o in self._open.values()), ZERO)
            equity = self.cash + reserved + lv
            equity_mid = self.cash + reserved + mv
            total = equity - self.starting_balance
            day_start = self._touch_day(equity, now)
            peak = max(self._peak_equity, equity) if self._peak_equity is not None else equity
            dd = (peak - equity) / peak * 100 if peak > 0 else ZERO
            return SpotAccountState(
                ts=now, starting_balance=self.starting_balance, cash=self.cash, reserved_cash=reserved,
                positions_liquidation_value=lv, positions_mid_value=mv, positions_cost_basis=cost,
                equity=equity, equity_mid=equity_mid, realized_pnl=self.realized_pnl,
                unrealized_pnl=lv - cost, unrealized_pnl_mid=mv - cost, fees_paid=self.fees_paid,
                total_pnl=total,
                total_return_pct=(total / self.starting_balance * 100) if self.starting_balance else ZERO,
                todays_pnl=equity - day_start, day_start_equity=day_start,
                max_drawdown_pct=max(self._max_dd_pct, dd), open_positions=n, open_orders=len(self._open),
                trades=self._trades, wins=self._wins, fills=self._fill_count, positions_exit_fee=exit_fee)

    def portfolio_view(self, strategy: str | None, *, allocation_pct: float | None = None) -> SpotPortfolioView:
        """Read-only view for ``strategy`` (``None`` = the whole account). ``alloc_equity`` =
        ``allocation_pct`` % of equity (100 % when not given)."""
        with self._mu:
            a = self.account()
            all_pos = tuple(self._marked(self._position_marks(), None))
            all_orders = tuple(self.open_orders())
            pct = allocation_pct if allocation_pct is not None else 100.0
            bids = {p: m.best_bid for p, m in self._marks.items() if m.best_bid is not None}
            asks = {p: m.best_ask for p, m in self._marks.items() if m.best_ask is not None}
            mids = {p: m.mid for p, m in self._marks.items() if m.mid is not None}
            return SpotPortfolioView(
                ts=a.ts, strategy=strategy, starting_balance=a.starting_balance, cash=a.cash,
                reserved_cash=a.reserved_cash, equity=a.equity, equity_mid=a.equity_mid,
                realized_pnl=a.realized_pnl, unrealized_pnl=a.unrealized_pnl, fees_paid=a.fees_paid,
                day_start_equity=a.day_start_equity, allocation_pct=allocation_pct,
                # sized on equity before exit fees: the planner values holdings at mids and charges
                # fees per trade; a net-of-exit-fee base would make a 100% target look overweight
                alloc_equity=(a.equity + a.positions_exit_fee) * D(pct) / 100,
                positions=tuple(p for p in all_pos if strategy is None or p.strategy == strategy),
                open_orders=tuple(o for o in all_orders if strategy is None or o.strategy == strategy),
                all_positions=all_pos, all_open_orders=all_orders, best_bids=bids, best_asks=asks,
                mids=mids)  # type: ignore[arg-type]

    def positions_json(self, alloc_equity: Mapping[str, Decimal] | None = None) -> list[dict[str, Any]]:
        """``GET /api/coinbase/positions`` rows. ``weight_of_strategy`` = liquidation value /
        ``alloc_equity[strategy]`` when given, else / the strategy's total held value."""
        ps = self.positions()
        totals: dict[str, Decimal] = {}
        for p in ps:
            totals[p.strategy] = totals.get(p.strategy, ZERO) + p.value
        out = []
        for p in ps:
            denom = (alloc_equity or {}).get(p.strategy) or totals.get(p.strategy)
            out.append(p.to_json(weight_of_strategy=p.value / D(denom) if denom else None))
        return out

    def strategy_stats(self) -> dict[str, dict[str, Any]]:
        """Per strategy (Decimal values): orders, fills, fees, realized_pnl, trades, wins,
        win_rate, open_positions, open_orders, unrealized_pnl, value, exposure (value + reserved)."""
        with self._mu:
            out: dict[str, dict[str, Any]] = {k: dict(v) for k, v in self._stats.items()}
            for p in self._marked(self._position_marks(), None):
                d = out.setdefault(p.strategy, dict(_EMPTY_STATS))
                d["open_positions"] = d.get("open_positions", 0) + 1
                d["unrealized_pnl"] = d.get("unrealized_pnl", ZERO) + p.unrealized_pnl
                d["value"] = d.get("value", ZERO) + p.value
                d["exposure"] = d.get("exposure", ZERO) + p.value
            for o in self._open.values():
                d = out.setdefault(o.strategy, dict(_EMPTY_STATS))
                d["open_orders"] = d.get("open_orders", 0) + 1
                if o.side == "buy":
                    d["exposure"] = d.get("exposure", ZERO) + o.reserved
            for d in out.values():
                for k, v in (("open_positions", 0), ("open_orders", 0), ("unrealized_pnl", ZERO), ("value", ZERO),
                             ("exposure", ZERO)):
                    d.setdefault(k, v)
                d["win_rate"] = d["wins"] / d["trades"] if d["trades"] else None
            return out

    def fee_tier_json(self) -> dict[str, Any]:
        """``fee_tier`` block of ``GET /api/coinbase/status``."""
        return self.tier.as_dict()

    # ------------------------------------------------------------------ public: reset

    def reset(self, starting_balance: Any = None) -> SpotAccountState:
        """Wipe the paper account (store tables included) and start over (one transaction).
        Stop the engine first."""
        with self._mu:
            start = D(starting_balance) if starting_balance is not None else self.starting_balance
            if self.store is not None:
                with self.store.transaction():
                    self.store.reset_paper_state()
                    self.store.save_account(starting_balance=start, cash=start, ts=self._now())
            self._init_state(start)
            self._persisted = self._state_values()
            self._log("info", "account", f"coinbase paper account reset to ${start}")
            return self.account()
