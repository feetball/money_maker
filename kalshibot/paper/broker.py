"""PaperBroker: honest fill simulation, Kalshi-style positions, cash and settlement (ARCHITECTURE.md §6).

PAPER TRADING ONLY. Nothing here talks to an order endpoint; all market data comes from an
injected :class:`MarketDataProvider` (``kalshibot.marketdata.MarketDataService`` in the app,
:class:`kalshibot.paper.sim.StaticMarketData` in tests/backtests). Given the same clock and
data, every result is deterministic. All ledger math is ``Decimal``.

How each §6 rule is implemented
-------------------------------
1. **Fresh data at execution.** Every placement re-reads the market with
   ``marketdata.market(ticker, fresh=True)`` (status / close time no older than ~5 s),
   resolves the exchange status and the fee parameters, and only *then* fetches the book
   (``marketdata.orderbook(ticker, max_age_s=2)``; ``book_max_age_s``), so no network wait
   sits between reading the book and walking it. A basket fetches all of its books together,
   after every other lookup. A book older than ``book_max_age_s + BOOK_LAG_S`` when the walk
   starts is re-fetched once, else the order is rejected as stale. The strategy's snapshot is
   never used. **Latency:** an engine order carries ``decided_at`` (when the strategy's tick
   returned); it reaches the paper exchange at ``decided_at + taker_latency_s``
   (``paper.taker_latency_s``, 0.25 s) and walks only a book received at or after that moment -
   never the book the decision was made on, whatever the caches hold. That book is fetched
   concurrently with the market/fee lookups, so slow lookups do not add to the simulated latency.
2. **Taker (ioc).** Walk the opposite side's asks best-first (YES asks are mirrored NO bids),
   only at prices <= limit, whole contracts, up to ``count``; the remainder is cancelled.
   Fees: one :class:`~kalshibot.fees.OrderFeeAccumulator` per order (Kalshi's per-order
   rounding accumulator, docs.kalshi.com/getting_started/fee_rounding), ``is_taker=True``.
3. **Consumed liquidity.** Paper fills do not remove real liquidity, so for every ask level
   we took from, ``consumed[(ticker, side, ask_price)]`` keeps ``qty`` = contracts we took
   that are still displayed, and what we can take later is ``displayed - qty``. Others'
   trades and cancels at that level come out of the part we did *not* take, so whenever the
   level is seen smaller than ``qty``, ``qty`` shrinks to the displayed size (the difference
   has left the book for good) and the entry disappears when the level does. Contracts that
   join the level are new liquidity. An entry is dropped wholesale only when
   ``consumed_liquidity_ttl_s`` has passed since our last take **and** the level has since been
   seen larger than right after that take (evidence the makers re-quoted); a byte-for-byte
   unchanged stale level is never re-harvested. Entries of closed/settled markets, and entries
   not looked at for ``consumed_gc_s`` (1 day), are garbage-collected.
4. **Resting (gtc).** A marketable part executes as taker first (Kalshi semantics); the rest
   rests with ``queue_ahead`` = displayed size at our price on our side (0 when we improve
   the best bid). Fills come only from later real trades (``marketdata.trades_since``;
   block trades excluded; each print is used once per ticker via a persisted cursor), and
   only from prints whose taker **sold our side** into the bids (``taker_outcome_side`` is
   the other side; for a YES bid that is ``taker_book_side == "ask"``): prints where the
   taker *bought* our side lifted offers - possibly the very offers we already took - and
   never fill a bid. Each such print is shared by our orders in price-time priority (best
   limit first, then time): a print strictly through an order's price fills it up to what is
   left of the print; at the order's price, what is left first burns ``queue_ahead`` (the
   real contracts ahead of it - shared by our orders at that price) and only the excess
   fills it. If the live book crosses our limit, we fill at our limit (price-time priority,
   after consumed liquidity; only while the market is tradable). Maker fills pay
   ``is_taker=False`` fees. Orders expire at ``expires_at`` (strategy ``expires_in_s``,
   default ``default_gtc_expiry_s``), at market close, or when the market stops being
   active. ``queue_ahead`` is also capped at the currently displayed size at our price
   (people ahead of us can only leave). Fractional prints accumulate in
   ``Order.fill_credit`` so whole-contract fills stay exact. Reading a market's trade tape
   costs one request, so a pass reads at most ``max_trade_polls_per_pass`` (8) of them:
   markets with an order at/after its expiry or close first, then the least recently read;
   a skipped market loses no prints (cursor) and its expiries wait for the pass that reads
   them, and so do its book-crossing fills and the ``queue_ahead`` bound from the book (applied
   before the prints that preceded that book, either would let those prints fill us again after
   ``queue_ahead`` was zeroed). ``cancel_orders``
   (strategy cancels) syncs the orders' markets the same way first.
5. **Positions** are netted per (strategy, ticker) - one Kalshi "subaccount" per strategy.
   Buying the opposite side first closes held contracts (a YES+NO pair redeems for $1):
   realized = m x (1 - avg_cost - price) - fees; ``sell`` = buy the other side at 1 - price.
   Each close writes a ``Settlement(kind="close", result="closed")``.
6. **Cash.** IOC: rejected unless free cash covers ``max(count x limit + fee(count, limit),
   actual cost)`` (net of contracts it closes). GTC: the resting remainder reserves
   ``remaining x limit + maker_fee(remaining, limit) + precision`` (the extra precision unit
   covers the accumulator's per-fill rounding); released on fill/cancel/expiry.
7. **Settlement.** ``check_settlements`` polls ``marketdata.market(ticker, fresh=True)`` for
   held markets and settles at ``status == finalized`` (or ``determined`` with
   ``settle_on_determined``). YES pays ``settlement_value`` (1 / 0 for yes / no), NO pays
   ``1 - value``; ``scalar`` is Kalshi's void / fair-price result. The total payout is
   floored to the balance precision (``paper.fee_precision``; $0.01 by default).
8. **Marks.** ``liquidation_value`` walks the side's bid ladder (net of liquidity we already
   consumed) for the contracts held - all strategies' positions in the same market and side
   walk it together and share the result pro rata; contracts beyond the displayed depth are
   worth 0. ``mid_value`` uses the mid. Equity = cash + reserved + liquidation value.
   Determined markets are marked at their payout. A market that has closed without a
   result keeps the last ladder observed **before** ``close_time``, frozen net of the bids we
   had consumed (``Mark.stale``; ``mark_stale`` in ``positions_json``), so it is never worth
   more than selling into that pre-close book would have paid; books seen at/after close are
   ignored. A completely empty book before close keeps the previous mark.
9. **Baskets.** ``place_basket(intents, all_or_none=True)``: every leg is validated and
   priced against fresh books with a shared consumed-liquidity overlay and a combined cash
   check; either every leg fills completely or every leg is rejected. This is **optimistic**:
   Kalshi has no atomic multi-market order, so real legs are independent IOCs and a basket
   some of whose legs are gone would leave a partial, unhedged position. Such baskets
   ("would have legged": at least one leg fully fillable, another not) are logged
   (``kind="basket_legged"``) and counted per strategy (``legged_baskets`` in
   ``strategy_stats``) so the arb's paper P&L can be read as the best case it is.
10. **Rejections.** Market not active, ``now >= close_time``, exchange or shard
    ``trading_active == false``, price off the tick grid (YES-denominated) or not in (0, 1),
    bad side/action/tif/count, insufficient cash, stale book.

Atomicity: every public mutation snapshots the in-memory ledger first and restores it if the
store transaction fails (disk full, database locked), so memory never runs ahead of the
database. ``place_order``/``place_basket`` then return the orders as ``rejected`` ("not
recorded"); the other methods re-raise after restoring.

Concurrency: public coroutines do their network I/O *without* holding the ledger lock and
take the ``asyncio.Lock`` only to apply the result (so API cancels never wait on Kalshi);
use the broker from one event loop (the engine's; FastAPI ``async`` endpoints run there too).
The :class:`Store` itself is thread-safe.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import logging
import math
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from kalshibot.fees import PRECISION_FCM, FillFee, OrderFeeAccumulator, resolve_fee_params, trading_fee
from kalshibot.kalshi.models import Market, Orderbook, Series, Trade
from kalshibot.money import ONE, ZERO, D, floor_to, is_valid_price
from kalshibot.paper.models import (
    AccountState,
    Fill,
    Order,
    PortfolioView,
    Position,
    Settlement,
    Side,
    iso,
    opposite,
    parse_iso,
    q6,
)

if TYPE_CHECKING:
    from kalshibot.store import Store

__all__ = ["BOOK_LAG_S", "CLOSED_STATUSES", "FALLBACK_FEE_PARAMS", "Mark", "MarketDataProvider", "PaperBroker"]

log = logging.getLogger(__name__)

#: Used when the series cannot be fetched: charge like a maker-fee series (conservative).
FALLBACK_FEE_PARAMS = ("quadratic_with_maker_fees", ONE)
#: Extra age (beyond ``book_max_age_s``) a fetched book may reach before the walk starts.
BOOK_LAG_S = 3.0
#: Bid levels kept per side in a mark (memory) and persisted per side (kv).
MARK_LEVELS = 100
MARK_STATE_LEVELS = 10
#: Stand-in size for marks restored from the legacy top-of-book-only state.
_LEGACY_SIZE = Decimal(10) ** 9
#: Default cap on trade-tape reads per resting-order pass (``paper.max_trade_polls_per_pass``):
#: at the default ``order_poll_s`` of 15 s that is at most ~0.5 request/s.
DEFAULT_TRADE_POLLS = 8
#: Market statuses after trading has ended (``closed`` = awaiting determination).
CLOSED_STATUSES = frozenset({"closed", "determined", "disputed", "amended", "finalized", "settled"})
#: Store errors that make a mutation fail cleanly (memory restored).
STORE_ERRORS: tuple[type[BaseException], ...] = (sqlite3.Error, OSError)


@runtime_checkable
class MarketDataProvider(Protocol):
    """What the broker needs from market data (``MarketDataService`` implements it).

    Optional extras the broker uses when present: ``async fee_params(market, at=...)``
    (scheduled fee changes), else ``async event(event_ticker) -> Event`` (or an ``events``
    mapping) for fee overrides; ``async exchange_status() -> dict`` (or a mapping attribute)
    for the trading-active check; ``async orderbooks(tickers, max_age_s)`` and
    ``async refresh_markets(tickers)`` for batched polling.
    """

    async def orderbook(self, ticker: str, max_age_s: float = 5) -> Orderbook: ...

    async def trades_since(self, ticker: str, since: datetime) -> list[Trade]: ...

    async def series(self, series_ticker: str) -> Series: ...

    async def market(self, ticker: str, fresh: bool = False) -> Market: ...


Ladder = tuple[tuple[Decimal, Decimal], ...]


@dataclass(slots=True)
class Mark:
    """Last observed bids for a market (or its determined payout).

    ``stale``: the market has closed (or the last book was seen at/after ``close_time``) and
    has no result yet, so the ladder is frozen at the last **pre-close** observation, net of
    the bids we had consumed ourselves; later books (empty, or re-populated after close)
    are ignored until the market re-opens or is determined.
    """

    yes_bid: Decimal | None
    no_bid: Decimal | None
    yes_mid: Decimal | None
    ts: datetime  # when the marked book was observed
    settled_yes: Decimal | None = None  # YES payout per contract once determined
    yes_bids: Ladder = ()  # best-first (price, size)
    no_bids: Ladder = ()
    stale: bool = False  # frozen at the last pre-close ladder (net of our consumption)
    close_time: datetime | None = None  # market close time, when known

    def ladder(self, side: str) -> Ladder:
        return self.yes_bids if side == "yes" else self.no_bids

    def liquidation_price(self, side: str) -> Decimal:
        """Top-of-book exit price per contract (the payout once determined)."""
        if self.settled_yes is not None:
            return self.settled_yes if side == "yes" else ONE - self.settled_yes
        b = self.yes_bid if side == "yes" else self.no_bid
        return b if b is not None else ZERO

    def mid_price(self, side: str) -> Decimal:
        if self.settled_yes is not None or self.yes_mid is None:
            return self.liquidation_price(side)
        return self.yes_mid if side == "yes" else ONE - self.yes_mid

    @property
    def yes_ask(self) -> Decimal | None:
        return None if self.no_bid is None else ONE - self.no_bid

    def is_stale(self, now: datetime) -> bool:
        """Marked from a frozen pre-close ladder (closed, no result yet)."""
        if self.settled_yes is not None:
            return False
        return self.stale or (self.close_time is not None and now >= self.close_time)

    def to_state(self) -> list[Any]:
        keep = MARK_LEVELS if self.stale else MARK_STATE_LEVELS  # a frozen ladder is kept whole

        def lad(x: Ladder) -> list[list[str]]:
            return [[str(p), str(s)] for p, s in x[:keep]]

        return [_s(self.yes_bid), _s(self.no_bid), _s(self.yes_mid), iso(self.ts), _s(self.settled_yes),
                lad(self.yes_bids), lad(self.no_bids), self.stale, iso(self.close_time)]

    @classmethod
    def from_state(cls, st: Sequence[Any]) -> Mark:
        yes_bid, no_bid = _d(st[0]), _d(st[1])
        if len(st) > 6:
            yb: Ladder = tuple((D(p), D(s)) for p, s in st[5])
            nb: Ladder = tuple((D(p), D(s)) for p, s in st[6])
        else:  # legacy state: top of book only (depth unknown until the next refresh)
            yb = ((yes_bid, _LEGACY_SIZE),) if yes_bid is not None else ()
            nb = ((no_bid, _LEGACY_SIZE),) if no_bid is not None else ()
        stale = bool(st[7]) if len(st) > 7 else False
        close = parse_iso(st[8]) if len(st) > 8 and st[8] else None
        return cls(yes_bid, no_bid, _d(st[2]), parse_iso(st[3]) or datetime.now(UTC), _d(st[4]), yb, nb,
                   stale, close)


def _s(x: Decimal | None) -> str | None:
    return None if x is None else str(x)


def _d(x: Any) -> Decimal | None:
    return None if x is None else D(x)


@dataclass(slots=True)
class _Consumed:
    """Liquidity we took from one ask level that is still displayed (rule 3)."""

    qty: Decimal
    ts: datetime  # last take
    shown: Decimal  # displayed size right after the last take
    grown: bool = False  # seen larger than ``shown`` since the last take (makers re-quoted)
    seen: datetime | None = None  # last observation (garbage collection only; not persisted)


@dataclass(frozen=True, slots=True)
class _Meta:
    market: Market
    fee_type: str
    fee_mult: Decimal


@dataclass(frozen=True, slots=True)
class _Prepared:
    market: Market
    book: Orderbook
    fee_type: str
    fee_mult: Decimal


@dataclass(frozen=True, slots=True)
class _Planned:
    price: Decimal  # buy-side price (= ask level price)
    count: int
    fee: FillFee


@dataclass(slots=True)
class _Batch:
    orders: dict[int, Order] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    positions: dict[tuple[str, str], Position] = field(default_factory=dict)
    settlements: list[Settlement] = field(default_factory=list)
    new_orders: set[int] = field(default_factory=set)


@dataclass(slots=True)
class _PollData:
    market: Market | None = None
    trades: list[Trade] | None = None
    book: Orderbook | None = None
    fee_type: str = FALLBACK_FEE_PARAMS[0]
    fee_mult: Decimal = FALLBACK_FEE_PARAMS[1]
    active: bool = False
    polled: bool = False  # trade tape read this pass (else prints and expiries wait for the next one)


def _opt_dec(x: Any) -> Decimal | None:
    """Finite Decimal or None."""
    if x is None:
        return None
    try:
        v = D(x)
    except Exception:
        return None
    return v if v.is_finite() else None


def _opt_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _hit_side(t: Trade) -> str | None:
    """The side whose **bids** a print hit: a taker buying YES matched resting NO bids, a taker
    buying NO matched resting YES bids. ``None`` when the print does not say (never fills)."""
    taker = (t.taker_side or "").lower()
    if taker not in ("yes", "no"):
        book_side = (t.taker_book_side or "").lower()
        taker = "yes" if book_side == "bid" else "no" if book_side == "ask" else ""
    if taker not in ("yes", "no"):
        return None
    return opposite(taker)


def _copy_order(o: Order) -> Order:
    return dataclasses.replace(o, fee_state=dict(o.fee_state))


_EMPTY_STATS = {"orders": 0, "fills": 0, "settled": 0, "wins": 0, "realized_pnl": ZERO, "fees": ZERO}


class PaperBroker:
    """Simulated Kalshi account. See the module docstring for the rules.

    Parameters default from ``settings`` (a full :class:`~kalshibot.config.Settings` or a
    :class:`~kalshibot.config.PaperSettings`); explicit keyword arguments win.
    """

    def __init__(
        self,
        marketdata: MarketDataProvider,
        store: Store | None = None,
        *,
        settings: Any = None,
        starting_balance: Any = None,
        profit_sweep_pct: Any = None,
        profit_sweep_enabled: Any = None,
        fee_precision: Any = None,
        consumed_liquidity_ttl_s: float | None = None,
        default_gtc_expiry_s: float | None = None,
        book_max_age_s: float = 2.0,
        mark_max_age_s: float = 30.0,
        settle_on_determined: bool = False,
        consumed_gc_s: float = 86400.0,
        max_trade_polls_per_pass: int | None = None,
        taker_latency_s: float | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Any] | None = None,
        log_to_store: bool = True,
    ) -> None:
        paper = getattr(settings, "paper", settings)
        account = getattr(settings, "account", None)
        self.md = marketdata
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.precision = D(fee_precision if fee_precision is not None
                           else getattr(paper, "fee_precision", PRECISION_FCM))
        self.ttl_s = float(consumed_liquidity_ttl_s if consumed_liquidity_ttl_s is not None
                           else getattr(paper, "consumed_liquidity_ttl_s", 300))
        self.default_gtc_expiry_s = float(default_gtc_expiry_s if default_gtc_expiry_s is not None
                                          else getattr(paper, "default_gtc_expiry_s", 3600))
        self.book_max_age_s = book_max_age_s
        self.mark_max_age_s = mark_max_age_s
        self.settle_on_determined = settle_on_determined
        self.consumed_gc_s = max(float(consumed_gc_s), self.ttl_s)
        #: trade-tape reads (one request each) per resting-order pass; 0 = no cap
        polls = (max_trade_polls_per_pass if max_trade_polls_per_pass is not None
                 else getattr(paper, "max_trade_polls_per_pass", None))
        self.max_trade_polls_per_pass: int | None = int(polls) if polls is not None else DEFAULT_TRADE_POLLS
        if self.max_trade_polls_per_pass <= 0:
            self.max_trade_polls_per_pass = None
        self._trade_polled: dict[str, datetime] = {}  # ticker -> last trade-tape read (fair polling)
        #: an engine order (``decided_at``) reaches the book this long after the decision
        self.taker_latency_s = max(0.0, float(taker_latency_s if taker_latency_s is not None
                                              else getattr(paper, "taker_latency_s", 0.25)))
        self._sleep = sleep or asyncio.sleep
        #: baskets rejected although some legs were fully fillable (real legs would have legged)
        self.legged_baskets: dict[str, int] = {}
        self.log_to_store = log_to_store
        default_start = starting_balance if starting_balance is not None else getattr(
            account, "starting_balance", 1000)
        #: fraction of each trade's realized profit moved to ``reserved_profit`` instead of ``cash``
        #: (defaults below are only used until an account row exists; after that the stored value wins,
        #: same as ``starting_balance``/``cash`` - see ``_load``)
        self._default_sweep_pct = D(profit_sweep_pct if profit_sweep_pct is not None
                                    else getattr(account, "profit_sweep_pct", 100))
        self._default_sweep_enabled = bool(profit_sweep_enabled if profit_sweep_enabled is not None
                                           else getattr(account, "profit_sweep_enabled", True))
        self._lock = asyncio.Lock()
        self._poll_lock = asyncio.Lock()
        self._listeners: list[Callable[[str, Any], None]] = []
        self._exchange_status: dict[str, Any] | None = None
        self._load(D(default_start))

    # ------------------------------------------------------------------ state / restore

    def _init_state(self, starting_balance: Decimal) -> None:
        self.starting_balance = starting_balance
        self.cash = starting_balance  # free cash (reservations excluded)
        self.realized_pnl = ZERO
        self.fees_paid = ZERO
        #: profit swept out of cash (never spent on new orders); see ``_sweep_profit``
        self.reserved_profit = ZERO
        self.profit_sweep_enabled = self._default_sweep_enabled
        self.profit_sweep_pct = self._default_sweep_pct
        self._open: dict[int, Order] = {}
        self._positions: dict[tuple[str, str], Position] = {}
        self._consumed: dict[tuple[str, str, Decimal], _Consumed] = {}
        self._cursors: dict[str, tuple[datetime, frozenset[str]]] = {}
        self._marks: dict[str, Mark] = {}
        self._day_start: tuple[str, Decimal] | None = None
        #: (UTC day, {strategy: realized + unrealized P&L at the day's first observation})
        self._strategy_day: tuple[str, dict[str, Decimal]] | None = None
        self._last_strategy_daily: dict[str, Decimal] = {}
        self._peak_equity: Decimal | None = None
        self._max_dd_pct = ZERO
        self._settled = 0
        self._wins = 0
        self._stats: dict[str, dict[str, Any]] = {}
        self._persisted: dict[str, Any] = {}  # last value written per state key (skip no-op writes)
        self._ids = {"order": 1, "fill": 1, "settlement": 1}

    def _load(self, default_start: Decimal) -> None:
        self._init_state(default_start)
        st = self.store
        if st is None:
            return
        acct = st.get_account()
        if acct is None:
            st.save_account(starting_balance=default_start, cash=default_start,
                           profit_sweep_enabled=self.profit_sweep_enabled, profit_sweep_pct=self.profit_sweep_pct,
                           ts=self._now())
            return
        self.starting_balance = acct["starting_balance"]
        self.cash = acct["cash"]
        self.realized_pnl = acct["realized_pnl"]
        self.fees_paid = acct["fees_paid"]
        self.reserved_profit = acct.get("reserved_profit", ZERO)
        self.profit_sweep_enabled = acct.get("profit_sweep_enabled", self.profit_sweep_enabled)
        self.profit_sweep_pct = acct.get("profit_sweep_pct", self.profit_sweep_pct)
        self._peak_equity = acct["peak_equity"]
        self._max_dd_pct = acct["max_drawdown_pct"]
        self._open = {o.id: o for o in st.open_orders()}
        self._positions = {p.key: p for p in st.list_positions(open_only=True)}
        for row in st.get_kv("broker.consumed", []) or []:
            t, side, price, qty, ts = row[:5]
            when = parse_iso(ts) or self._now()
            shown = D(row[5]) if len(row) > 5 else D(qty)  # legacy rows: size at the take unknown
            grown = bool(row[6]) if len(row) > 6 else False
            self._consumed[(t, side, D(price))] = _Consumed(D(qty), when, shown, grown, when)
        for t, (ts, ids) in (st.get_kv("broker.cursors", {}) or {}).items():
            self._cursors[t] = (parse_iso(ts), frozenset(ids))
        for t, m in (st.get_kv("broker.marks", {}) or {}).items():
            self._marks[t] = Mark.from_state(m)
        ds = st.get_kv("broker.day_start")
        if ds:
            self._day_start = (ds[0], D(ds[1]))
        sd = st.get_kv("broker.strategy_day_start")
        if sd and isinstance(sd, list | tuple) and len(sd) == 2 and isinstance(sd[1], Mapping):
            self._strategy_day = (str(sd[0]), {str(k): D(v) for k, v in sd[1].items()})
        self._settled, self._wins = st.settlement_counts()
        self._stats = {k: dict(v) for k, v in st.strategy_summary().items()}
        self._ids = {"order": st.max_id("orders") + 1, "fill": st.max_id("fills") + 1,
                     "settlement": st.max_id("settlements") + 1}
        self._persisted = self._state_values()

    def _state_values(self) -> dict[str, Any]:
        """The broker's working state as stored (``account`` row + ``broker.*`` kv)."""
        held = {p.ticker for p in self._positions.values() if p.count > 0}
        out: dict[str, Any] = {
            "account": (self.starting_balance, self.cash, self.realized_pnl, self.fees_paid, self.reserved_profit,
                        self.profit_sweep_enabled, self.profit_sweep_pct, self._peak_equity, self._max_dd_pct),
            "broker.consumed": [[t, s, str(p), str(e.qty), iso(e.ts), str(e.shown), e.grown]
                                for (t, s, p), e in sorted(self._consumed.items())],
            "broker.cursors": {t: [iso(ts), sorted(ids)] for t, (ts, ids) in sorted(self._cursors.items())},
            "broker.marks": {t: m.to_state() for t, m in sorted(self._marks.items()) if t in held},
        }
        if self._day_start is not None:
            out["broker.day_start"] = [self._day_start[0], str(self._day_start[1])]
        if self._strategy_day is not None:
            out["broker.strategy_day_start"] = [self._strategy_day[0],
                                                {k: str(v) for k, v in sorted(self._strategy_day[1].items())}]
        return out

    def _write_state(self) -> dict[str, Any]:
        """Write changed state inside the caller's transaction; returns what was written
        (recorded as persisted only once the transaction commits)."""
        st = self.store
        assert st is not None
        written: dict[str, Any] = {}
        for key, value in self._state_values().items():
            if self._persisted.get(key) == value:
                continue
            if key == "account":
                st.save_account(starting_balance=value[0], cash=value[1], realized_pnl=value[2], fees_paid=value[3],
                                reserved_profit=value[4], profit_sweep_enabled=value[5], profit_sweep_pct=value[6],
                                peak_equity=value[7], max_drawdown_pct=value[8], ts=self._now())
            else:
                st.set_kv(key, value)
            written[key] = value
        return written

    def _next(self, kind: str) -> int:
        i = self._ids[kind]
        self._ids[kind] = i + 1
        return i

    def _sweep_profit(self, pnl: Decimal) -> None:
        """Move a fraction of a closed/settled trade's profit out of ``cash`` into
        ``reserved_profit`` (``AccountSettings.profit_sweep_pct``/``profit_sweep_enabled``), so it
        is never put back in the tradeable pool. Losses are left alone - only realized gains are swept."""
        if not self.profit_sweep_enabled or pnl <= 0 or self.profit_sweep_pct <= 0:
            return
        amount = floor_to(pnl * self.profit_sweep_pct / 100, self.precision)
        amount = min(amount, self.cash)
        if amount <= 0:
            return
        self.cash -= amount
        self.reserved_profit += amount

    def _now(self) -> datetime:
        now = self.clock()
        return now if now.tzinfo else now.replace(tzinfo=UTC)

    # -- atomicity ----------------------------------------------------------------------

    def _snapshot(self) -> tuple[Any, ...]:
        return (self.starting_balance, self.cash, self.realized_pnl, self.fees_paid, self.reserved_profit,
                self.profit_sweep_enabled, self.profit_sweep_pct,
                {k: _copy_order(o) for k, o in self._open.items()},
                {k: dataclasses.replace(p) for k, p in self._positions.items()},
                {k: dataclasses.replace(e) for k, e in self._consumed.items()},
                dict(self._cursors), {t: dataclasses.replace(m) for t, m in self._marks.items()},
                self._day_start, self._peak_equity, self._max_dd_pct, self._settled, self._wins,
                {k: dict(v) for k, v in self._stats.items()}, dict(self._persisted))

    def _restore(self, snap: tuple[Any, ...]) -> None:
        (self.starting_balance, self.cash, self.realized_pnl, self.fees_paid, self.reserved_profit,
         self.profit_sweep_enabled, self.profit_sweep_pct, self._open,
         self._positions, self._consumed, self._cursors, self._marks, self._day_start, self._peak_equity,
         self._max_dd_pct, self._settled, self._wins, self._stats, self._persisted) = snap

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
        for o in batch.orders.values():
            if o.is_open:
                self._open[o.id] = o
            else:
                self._open.pop(o.id, None)
        for key, p in batch.positions.items():
            if p.count > 0 or self.store is None:
                self._positions[key] = p
            else:
                self._positions.pop(key, None)
        live = {o.ticker for o in self._open.values()}
        for t in [t for t in self._cursors if t not in live]:
            del self._cursors[t]
        for t in [t for t in self._trade_polled if t not in live]:
            del self._trade_polled[t]
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
                for s in batch.settlements:
                    self.store.insert_settlement(s)
                written = self._write_state()
        # committed: from here on nothing can fail the ledger
        self._persisted.update(written)
        self._count_stats(batch)
        for f in batch.fills:
            self._emit("fill", f)
        for s in batch.settlements:
            self._emit("settlement", s)
        for o in batch.orders.values():
            self._emit("order", o)

    def _count_stats(self, batch: _Batch) -> None:
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
        for s in batch.settlements:
            r = row(s.strategy)
            r["settled"] += 1
            r["realized_pnl"] += s.pnl
            r["wins"] += s.pnl > 0

    def _gc(self, now: datetime) -> None:
        """Bound the working state: forget consumed levels nobody looked at for ``consumed_gc_s``
        and marks of markets no longer held."""
        for k in [k for k, e in self._consumed.items()
                  if (now - (e.seen or e.ts)).total_seconds() >= self.consumed_gc_s]:
            del self._consumed[k]
        held = {p.ticker for p in self._positions.values() if p.count > 0}
        for t in [t for t in self._marks if t not in held]:
            del self._marks[t]

    def _forget_market(self, ticker: str) -> None:
        """A market that closed or settled: its consumed levels no longer matter."""
        for k in [k for k in self._consumed if k[0] == ticker]:
            del self._consumed[k]

    # ------------------------------------------------------------------ events / logging

    def subscribe(self, fn: Callable[[str, Any], None]) -> Callable[[], None]:
        """Register ``fn(kind, obj)`` for kinds ``order``/``fill``/``settlement`` (called after commit)."""
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def _emit(self, kind: str, obj: Any) -> None:
        for fn in list(self._listeners):
            try:
                fn(kind, obj)
            except Exception:  # listeners must never break the ledger
                log.exception("broker listener failed for %s", kind)

    def _log(self, level: str, kind: str, message: str, **data: Any) -> None:
        log.log(getattr(logging, level.upper(), logging.INFO), "%s: %s", kind, message)
        if self.store is not None and self.log_to_store:
            try:
                self.store.insert_log(level, kind, message, data or None, ts=self._now())
            except Exception:  # logging must not fail trading
                log.exception("failed to write log row")

    # ------------------------------------------------------------------ exchange status

    def set_exchange_status(self, status: Mapping[str, Any] | None) -> None:
        """Feed ``GET /exchange/status`` (the engine polls it); ``None`` clears it."""
        self._exchange_status = dict(status) if status else None

    async def _trading_active(self, market: Market) -> bool:
        st: Any = self._exchange_status
        if st is None:
            src = getattr(self.md, "exchange_status", None)
            try:
                if callable(src):
                    res = src()
                    st = await res if inspect.isawaitable(res) else res
                elif isinstance(src, Mapping):
                    st = src
            except Exception as e:
                log.warning("exchange status unavailable: %s", e)
                st = None
        if not isinstance(st, Mapping) or not st:
            return True
        if st.get("trading_active") is False or st.get("exchange_active") is False:
            return False
        for s in st.get("exchange_index_statuses") or ():
            if (isinstance(s, Mapping) and s.get("exchange_index") == market.exchange_index
                    and (s.get("trading_active") is False or s.get("exchange_active") is False)):
                return False
        return True

    # ------------------------------------------------------------------ fees

    async def _fee_params(self, market: Market, at: datetime | None = None) -> tuple[str, Decimal]:
        """Effective (fee_type, multiplier) at ``at`` (fill time): series, event override and
        scheduled fee changes (``marketdata.fee_params`` when available)."""
        at = at or self._now()
        fn = getattr(self.md, "fee_params", None)
        if callable(fn):
            try:
                res = fn(market, at=at)
                return await res if inspect.isawaitable(res) else res
            except Exception as e:
                self._log("warning", "fees", f"fee parameters for {market.ticker} unavailable ({e}); "
                          f"charging {FALLBACK_FEE_PARAMS[0]} x{FALLBACK_FEE_PARAMS[1]}")
                return FALLBACK_FEE_PARAMS
        series: Any = None
        try:
            series = await self.md.series(market.series_ticker)
        except Exception as e:
            self._log("warning", "fees", f"series {market.series_ticker} unavailable ({e}); "
                      f"charging {FALLBACK_FEE_PARAMS[0]} x{FALLBACK_FEE_PARAMS[1]}")
        event: Any = None
        ev_fn = getattr(self.md, "event", None)
        try:
            if callable(ev_fn):
                res = ev_fn(market.event_ticker)
                event = await res if inspect.isawaitable(res) else res
            else:
                evs = getattr(self.md, "events", None)
                if isinstance(evs, Mapping):
                    event = evs.get(market.event_ticker)
        except Exception:
            event = None
        if series is None:
            series = SimpleNamespace(fee_type=FALLBACK_FEE_PARAMS[0], fee_multiplier=FALLBACK_FEE_PARAMS[1])
        return resolve_fee_params(series, event)

    def _reserve_amount(self, remaining: int, limit: Decimal, fee_type: str, mult: Decimal) -> Decimal:
        """Cash held for a resting remainder: principal + maker-fee estimate + one precision unit."""
        if remaining <= 0:
            return ZERO
        fee = trading_fee(limit, remaining, is_taker=False, fee_type=fee_type, fee_multiplier=mult,
                          precision=self.precision)
        return limit * remaining + fee + self.precision

    def _acc(self, order: Order) -> OrderFeeAccumulator:
        if order.fee_state:
            return OrderFeeAccumulator.from_state(order.fee_state)
        return OrderFeeAccumulator(precision=self.precision)

    # ------------------------------------------------------------------ consumed liquidity

    def _observe(self, key: tuple[str, str, Decimal], displayed: Decimal, now: datetime) -> Decimal:
        """Reconcile a consumed entry with the level's displayed size; returns what is still ours."""
        e = self._consumed.get(key)
        if e is None:
            return ZERO
        if displayed < e.qty:
            e.qty = displayed  # others' trades/cancels: the rest of our take has left the book
        if displayed > e.shown:
            e.grown = True
        e.seen = now
        if e.qty <= 0 or (e.grown and (now - e.ts).total_seconds() >= self.ttl_s):
            del self._consumed[key]
            return ZERO
        return e.qty

    def _available(self, ticker: str, side: str, price: Decimal, displayed: Decimal, now: datetime,
                   overlay: Mapping[tuple[str, str, Decimal], Decimal] | None = None) -> Decimal:
        key = (ticker, side, price)
        used = self._observe(key, displayed, now)
        if overlay:
            used += overlay.get(key, ZERO)
        return max(ZERO, displayed - used)

    def _consume(self, ticker: str, side: str, price: Decimal, qty: int, displayed: Decimal, now: datetime) -> None:
        key = (ticker, side, price)
        e = self._consumed.get(key)
        if e is None:
            self._consumed[key] = _Consumed(D(qty), now, displayed, False, now)
        else:
            e.qty += qty
            e.ts = now
            e.shown = displayed
            e.grown = False
            e.seen = now

    def _reconcile(self, ticker: str, book: Orderbook, now: datetime) -> None:
        """Observe every consumed level of ``ticker`` in a fresh full book (vanished levels drop)."""
        for key in [k for k in self._consumed if k[0] == ticker]:
            _, side, price = key
            self._observe(key, book.size_at(side, price, book="ask"), now)  # type: ignore[arg-type]

    def consumed(self, ticker: str | None = None) -> dict[tuple[str, str, Decimal], Decimal]:
        """Current consumed-liquidity entries ``{(ticker, side, ask_price): qty}`` (UI/debugging)."""
        return {k: e.qty for k, e in self._consumed.items() if ticker is None or k[0] == ticker}

    def _walk(self, ticker: str, book: Orderbook, side: str, limit: Decimal, max_count: int, now: datetime,
              overlay: Mapping[tuple[str, str, Decimal], Decimal] | None = None) -> list[tuple[Decimal, int]]:
        out: list[tuple[Decimal, int]] = []
        remaining = max_count
        for lv in book.asks(side):  # best (lowest) first
            if remaining <= 0 or lv.price > limit:
                break
            n = min(remaining, int(self._available(ticker, side, lv.price, lv.size, now, overlay)))
            if n > 0:
                out.append((lv.price, n))
                remaining -= n
        return out

    # ------------------------------------------------------------------ marks

    def _seen_at(self, book: Orderbook, now: datetime) -> datetime:
        ts = book.ts if book.ts.tzinfo else book.ts.replace(tzinfo=UTC)
        return min(ts, now)

    def _known_market(self, ticker: str) -> Market | None:
        """The provider's cached snapshot (no request), when it offers ``known_market``."""
        fn = getattr(self.md, "known_market", None)
        if not callable(fn):
            return None
        try:
            m = fn(ticker)
        except Exception:
            return None
        return m if isinstance(m, Market) else None

    def _update_mark(self, ticker: str, book: Orderbook, now: datetime, *,
                     close_time: datetime | None = None) -> None:
        """Mark ``ticker`` from a book seen before its close (a frozen mark is never replaced;
        an empty book keeps the previous mark; a book seen at/after close freezes it)."""
        prev = self._marks.get(ticker)
        close = close_time or (prev.close_time if prev is not None else None)
        if close is None and prev is None:
            known = self._known_market(ticker)
            close = known.close_time if known is not None else None
        seen = self._seen_at(book, now)
        if prev is not None:
            if prev.stale:
                return
            if close is not None and seen >= close:
                prev.close_time = close
                self._freeze(ticker, prev)
                return
            if book.is_empty:
                return
        elif close is not None and seen >= close:
            return  # never observed before close: nothing honest to mark from
        self._marks[ticker] = Mark(
            book.best_yes_bid, book.best_no_bid, book.mid, seen, prev.settled_yes if prev else None,
            tuple((lv.price, lv.size) for lv in book.yes_bids[:MARK_LEVELS]),
            tuple((lv.price, lv.size) for lv in book.no_bids[:MARK_LEVELS]), False, close)

    def _observe_book(self, ticker: str, book: Orderbook, now: datetime) -> None:
        """A fresh full book: reconcile consumed liquidity and update the mark - unless the
        mark is frozen or the book was seen at/after close (then it only freezes the mark,
        so the consumption we netted out before close is not forgotten first)."""
        mk = self._marks.get(ticker)
        if mk is not None and (mk.stale or (mk.close_time is not None and self._seen_at(book, now) >= mk.close_time)):
            self._freeze(ticker, mk)
            return
        self._reconcile(ticker, book, now)
        self._update_mark(ticker, book, now)

    def _freeze(self, ticker: str, mk: Mark) -> None:
        """Freeze a mark at its pre-close ladder, net of the bids we consumed ourselves (those
        entries are forgotten once the market closes), so it never rises above what selling
        into the last pre-close book would have paid."""
        if mk.stale:
            return

        def net(side: str) -> Ladder:
            opp = opposite(side)
            out: list[tuple[Decimal, Decimal]] = []
            for price, size in mk.ladder(side):
                e = self._consumed.get((ticker, opp, ONE - price))
                avail = size - (e.qty if e is not None else ZERO)
                if avail > 0:
                    out.append((price, avail))
            return tuple(out)

        mk.yes_bids, mk.no_bids = net("yes"), net("no")
        mk.stale = True

    def _note_market(self, m: Market, now: datetime) -> None:
        """Track a marked market's close time and status: freeze the mark once it has closed
        (``now >= close_time`` or a post-trading status); thaw it if it trades again."""
        mk = self._marks.get(m.ticker)
        if mk is None:
            return
        if m.close_time is not None:
            mk.close_time = m.close_time
        if m.status in CLOSED_STATUSES or (mk.close_time is not None and now >= mk.close_time):
            self._freeze(m.ticker, mk)
        elif mk.stale and m.is_open:
            mk.stale = False  # re-opened (e.g. close time extended): the next book re-marks it

    def mark(self, ticker: str) -> Mark | None:
        return self._marks.get(ticker)

    async def _fetch_books(self, tickers: Sequence[str], max_age_s: float) -> dict[str, Orderbook]:
        """Books for many tickers (one batched request when the provider supports it)."""
        tickers = list(dict.fromkeys(tickers))
        if not tickers:
            return {}
        batch = getattr(self.md, "orderbooks", None)
        if callable(batch) and len(tickers) > 1:
            try:
                return dict(await batch(tickers, max_age_s=max_age_s))
            except Exception as e:
                log.warning("batched order books failed (%s); fetching one by one", e)
        out: dict[str, Orderbook] = {}
        for t in tickers:
            try:
                out[t] = await self.md.orderbook(t, max_age_s=max_age_s)
            except Exception as e:
                log.warning("order book %s unavailable: %s", t, e)
        return out

    async def _fetch_markets(self, tickers: Sequence[str]) -> dict[str, Market]:
        """Fresh market snapshots (batch-primed with ``refresh_markets`` when available)."""
        tickers = list(dict.fromkeys(tickers))
        prime = getattr(self.md, "refresh_markets", None)
        if callable(prime) and len(tickers) > 1:
            try:
                await prime(tickers)
            except Exception as e:
                log.warning("batched market refresh failed (%s); fetching one by one", e)
        out: dict[str, Market] = {}
        for t in tickers:
            try:
                out[t] = await self.md.market(t, fresh=True)
            except Exception as e:
                log.warning("market %s unavailable: %s", t, e)
        return out

    async def refresh_marks(self, tickers: Iterable[str] | None = None) -> None:
        """Re-read books for held markets (``max_age_s=mark_max_age_s``) to update marks."""
        ts = sorted(set(tickers) if tickers is not None else self._held_tickers())
        books = await self._fetch_books(ts, self.mark_max_age_s)
        async with self._lock:
            self._apply_books(books, self._now())

    def _apply_books(self, books: Mapping[str, Orderbook], now: datetime) -> None:
        for t, book in books.items():
            known = self._known_market(t)
            mk = self._marks.get(t)
            if known is not None and mk is not None:
                if known.close_time is not None:
                    mk.close_time = known.close_time
                if known.status in CLOSED_STATUSES:
                    self._freeze(t, mk)
            self._observe_book(t, book, now)

    def _held_tickers(self) -> set[str]:
        return {p.ticker for p in self._positions.values() if p.count > 0}

    def _liquidation(self, ticker: str, side: str, count: Decimal, mk: Mark) -> Decimal:
        """What selling ``count`` contracts of ``side`` into the marked bids would bring
        (net of bids we already consumed ourselves; beyond the displayed depth: 0)."""
        if mk.settled_yes is not None:
            return mk.liquidation_price(side) * count
        opp = opposite(side)
        remaining = count
        value = ZERO
        for price, size in mk.ladder(side):
            if remaining <= 0:
                break
            e = None if mk.stale else self._consumed.get((ticker, opp, ONE - price))  # frozen: already net
            avail = size - (e.qty if e is not None else ZERO)
            if avail <= 0:
                continue
            n = min(remaining, avail)
            value += n * price
            remaining -= n
        return value

    def _position_values(self) -> dict[tuple[str, str], tuple[Decimal, Decimal]]:
        """``{(strategy, ticker): (liquidation value, mid value)}`` for open positions.

        Positions of several strategies in one market/side walk the bid ladder together and
        share the proceeds pro rata (the book is not there twice). Never marked: cost basis.
        """
        groups: dict[tuple[str, str], list[Position]] = {}
        for p in self._positions.values():
            if p.count > 0:
                groups.setdefault((p.ticker, p.side), []).append(p)
        out: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
        for (ticker, side), ps in groups.items():
            ps.sort(key=lambda p: p.strategy)
            mk = self._marks.get(ticker)
            if mk is None:
                for p in ps:
                    out[p.key] = (p.cost_basis, p.cost_basis)
                continue
            total = sum(p.count for p in ps)
            liq = self._liquidation(ticker, side, D(total), mk)
            mid = mk.mid_price(side)
            left = liq
            for i, p in enumerate(ps):
                share = left if i == len(ps) - 1 else q6(liq * p.count / total)
                left -= share
                out[p.key] = (share, mid * p.count)
        return out

    # ------------------------------------------------------------------ order construction

    def _new_order(self, intent: Any, count_override: Any, now: datetime) -> Order:
        def g(name: str, default: Any = None) -> Any:
            v = getattr(intent, name, default)
            if v is None and isinstance(intent, Mapping):
                v = intent.get(name, default)
            return default if v is None else v

        side = str(g("side", "")).lower()
        action = str(g("action", "buy")).lower()
        tif = str(g("tif", "ioc")).lower()
        problems: list[str] = []
        if side not in ("yes", "no"):
            problems.append(f"invalid side {side!r}")
        if action not in ("buy", "sell"):
            problems.append(f"invalid action {action!r}")
        if tif not in ("ioc", "gtc"):
            problems.append(f"invalid tif {tif!r}")
        raw_count = count_override if count_override is not None else g("count", 1)
        count = 0
        try:
            c = D(raw_count)
            if c != c.to_integral_value() or c <= 0:
                problems.append(f"count must be a positive whole number, got {raw_count!r}")
            else:
                count = int(c)
        except Exception:
            problems.append(f"invalid count {raw_count!r}")
        limit = _opt_dec(g("limit_price"))
        if limit is None:
            problems.append("missing/invalid limit_price")
        order = Order(
            id=self._next("order"), ticker=str(g("ticker", "")),
            side=side if side in ("yes", "no") else "yes",  # type: ignore[arg-type]
            action=action if action in ("buy", "sell") else "buy",  # type: ignore[arg-type]
            count=count, limit_price=limit if limit is not None else ZERO,
            tif=tif if tif in ("ioc", "gtc") else "ioc",  # type: ignore[arg-type]
            strategy=str(g("strategy", "")), reason=str(g("reason", "")),
            expected_edge=_opt_dec(g("expected_edge")), fair_value=_opt_float(g("fair_value")),
            group_id=g("group_id"), created_at=now, updated_at=now,
        )
        if order.tif == "gtc":
            exp = g("expires_in_s")
            order.expires_at = now + timedelta(seconds=float(exp if exp else self.default_gtc_expiry_s))
        if not order.ticker:
            problems.append("missing ticker")
        if problems:
            self._set_rejected(order, "; ".join(problems), now)
        return order

    def _set_rejected(self, order: Order, reason: str, now: datetime) -> None:
        order.status = "rejected"
        order.status_reason = reason
        order.updated_at = now
        order.reserved = ZERO
        order.queue_ahead = None

    def _check_market(self, order: Order, market: Market, now: datetime) -> str | None:
        if not market.is_open:
            return f"market not active (status={market.status or 'unknown'})"
        if market.close_time is not None and now >= market.close_time:
            return f"market closed at {iso(market.close_time)}"
        if not is_valid_price(order.yes_price, market.price_ranges):
            return (f"limit price {order.limit_price} ({order.side}) is off the market's tick grid "
                    f"or not strictly inside (0, 1)")
        return None

    async def _meta(self, order: Order) -> _Meta | str:
        """Everything except the book: fresh market status, exchange status, fee parameters."""
        try:
            market = await self.md.market(order.ticker, fresh=True)
        except Exception as e:
            return f"market lookup failed: {e}"
        order.event_ticker = market.event_ticker
        reason = self._check_market(order, market, self._now())
        if reason:
            return reason
        if not await self._trading_active(market):
            return "trading paused (exchange or shard trading_active=false)"
        fee_type, mult = await self._fee_params(market, self._now())
        return _Meta(market, fee_type, mult)

    def _finish_prepare(self, order: Order, meta: _Meta, book: Orderbook, now: datetime) -> _Prepared | str:
        """The order reaches the (simulated) exchange now: re-check the close time, stamp it."""
        reason = self._check_market(order, meta.market, now)
        if reason:
            return reason
        if order.tif == "gtc" and order.expires_at is not None and order.created_at is not None:
            order.expires_at = now + (order.expires_at - order.created_at)
        order.created_at = order.updated_at = now
        return _Prepared(meta.market, book, meta.fee_type, meta.fee_mult)

    def _book_age_s(self, book: Orderbook, now: datetime) -> float:
        ts = book.ts if book.ts.tzinfo else book.ts.replace(tzinfo=UTC)
        return (now - ts).total_seconds()

    def not_before(self, decided_at: datetime | None) -> datetime | None:
        """When an order decided at ``decided_at`` reaches the (paper) exchange."""
        if decided_at is None:
            return None
        d = decided_at if decided_at.tzinfo else decided_at.replace(tzinfo=UTC)
        return d + timedelta(seconds=self.taker_latency_s)

    async def wait_until(self, when: datetime | None) -> None:
        """Sleep until ``when`` on the broker's clock (no-op when it has passed)."""
        if when is None:
            return
        delay = (when - self._now()).total_seconds()
        if delay > 0:
            await self._sleep(delay)

    def _received_before(self, book: Orderbook, when: datetime) -> bool:
        ts = book.ts if book.ts.tzinfo else book.ts.replace(tzinfo=UTC)
        return ts < when

    async def _arrival_book(self, ticker: str, not_before: datetime | None) -> Orderbook:
        """The book the order meets: without latency the usual fresh read; with ``not_before``,
        (after waiting for it) only a book received at or after that moment."""
        if not_before is None:
            return await self.md.orderbook(ticker, max_age_s=self.book_max_age_s)
        await self.wait_until(not_before)
        book = await self.md.orderbook(ticker, max_age_s=self.book_max_age_s)
        if self._received_before(book, not_before):
            book = await self.md.orderbook(ticker, max_age_s=0)  # a new request
        return book

    async def _arrival_books(self, tickers: Sequence[str], not_before: datetime | None) -> dict[str, Orderbook]:
        if not_before is None:
            return await self._fetch_books(tickers, self.book_max_age_s)
        await self.wait_until(not_before)
        books = await self._fetch_books(tickers, self.book_max_age_s)
        old = [t for t in tickers if t not in books or self._received_before(books[t], not_before)]
        if old:
            books.update(await self._fetch_books(old, 0))
        return books

    async def _ensure_fresh(self, ticker: str, book: Orderbook) -> Orderbook | None:
        """The book to walk now: re-fetched once if it aged past ``book_max_age_s + BOOK_LAG_S``
        (e.g. while waiting for the lock); ``None`` if it is still stale."""
        limit = self.book_max_age_s + BOOK_LAG_S
        if self._book_age_s(book, self._now()) <= limit:
            return book
        try:
            book = await self.md.orderbook(ticker, max_age_s=self.book_max_age_s)
        except Exception as e:
            log.warning("re-fetching the book of %s failed: %s", ticker, e)
            return None
        return book if self._book_age_s(book, self._now()) <= limit else None

    def _price_plan(self, order: Order, levels: Sequence[tuple[Decimal, int]], prep: _Prepared
                    ) -> tuple[list[_Planned], OrderFeeAccumulator]:
        acc = self._acc(order)
        plan = [_Planned(p, n, acc.on_fill(p, n, is_buy=True, is_taker=True, fee_type=prep.fee_type,
                                           fee_multiplier=prep.fee_mult)) for p, n in levels]
        return plan, acc

    def _closable(self, order: Order) -> int:
        pos = self._positions.get((order.strategy, order.ticker))
        if pos is None or pos.count <= 0 or pos.side == order.buy_side:
            return 0
        return pos.count

    def _required_cash(self, order: Order, plan: Sequence[_Planned], prep: _Prepared, closable: int) -> Decimal:
        """Cash the order needs now (Kalshi checks count x limit + fee at placement)."""
        lim = order.buy_limit
        taker_n = sum(x.count for x in plan)
        cost = sum((x.price * x.count + x.fee.net_fee for x in plan), ZERO)
        fee_est = trading_fee(lim, order.count, is_taker=True, fee_type=prep.fee_type,
                              fee_multiplier=prep.fee_mult, precision=self.precision)
        if order.tif == "ioc":
            est = (order.count - min(order.count, closable)) * lim + fee_est
            actual = cost - min(taker_n, closable) * ONE
        else:
            est = order.count * lim + fee_est + self.precision
            actual = (cost - min(taker_n, closable) * ONE
                      + self._reserve_amount(order.count - taker_n, lim, prep.fee_type, prep.fee_mult))
        return max(est, actual, ZERO)

    # ------------------------------------------------------------------ fills & positions

    def _get_position(self, strategy: str, ticker: str, event_ticker: str) -> Position:
        key = (strategy, ticker)
        p = self._positions.get(key)
        if p is None and self.store is not None:
            p = self.store.get_position(strategy, ticker)
        if p is None:
            p = Position(ticker=ticker, strategy=strategy, event_ticker=event_ticker)
        if event_ticker and not p.event_ticker:
            p.event_ticker = event_ticker
        self._positions[key] = p
        return p

    def _apply_fill(self, order: Order, buy_price: Decimal, n: int, ff: FillFee, *, is_taker: bool,
                    ts: datetime, batch: _Batch) -> Fill:
        fee = ff.net_fee
        price = order.order_price(buy_price)
        fill = Fill(id=self._next("fill"), order_id=order.id, ticker=order.ticker, side=order.side,
                    action=order.action, count=n, price=price, fee=fee, is_taker=is_taker, ts=ts,
                    strategy=order.strategy, event_ticker=order.event_ticker)
        order.filled_count += n
        if is_taker:
            order.taker_filled_count += n
        order.filled_notional += price * n
        order.avg_fill_price = q6(order.filled_notional / order.filled_count)
        order.fees += fee
        order.updated_at = ts
        # balance change of the fill == -(principal + net fee) (fees.round_fill)
        self.cash -= buy_price * n + fee
        self.fees_paid += fee
        self.cash += self._net_position(order, buy_price, n, fee, ts, batch)
        batch.fills.append(fill)
        batch.orders[order.id] = order
        return fill

    def _net_position(self, order: Order, buy_price: Decimal, n: int, fee: Decimal, ts: datetime,
                      batch: _Batch) -> Decimal:
        """Kalshi netting for (strategy, ticker). Returns the pair-redemption cash ($1 per closed pair)."""
        pos = self._get_position(order.strategy, order.ticker, order.event_ticker)
        side: Side = order.buy_side
        pos.fees_paid += fee
        redemption = ZERO
        m = 0
        fee_close = ZERO
        if pos.count > 0 and pos.side != side:
            m = min(n, pos.count)
            full = m == pos.count
            cost_closed = pos.cost_basis if full else q6(pos.cost_basis * m / pos.count)
            ofees_closed = pos.open_fees if full else q6(pos.open_fees * m / pos.count)
            edge_closed: Decimal | None = None
            if pos.expected_edge_total is not None:
                edge_closed = pos.expected_edge_total if full else q6(pos.expected_edge_total * m / pos.count)
            fee_close = fee if m == n else q6(fee * m / n)
            exit_price = ONE - buy_price  # per-contract price of the held side
            proceeds = exit_price * m
            fees_alloc = ofees_closed + fee_close
            pnl = proceeds - cost_closed - fees_alloc
            s = Settlement(id=self._next("settlement"), ticker=pos.ticker, result="closed", side=pos.side,
                           count=m, payout=proceeds, cost_basis=cost_closed, pnl=pnl, ts=ts,
                           strategy=pos.strategy, event_ticker=pos.event_ticker, kind="close", fees=fees_alloc,
                           expected_edge=edge_closed, fair_value=pos.fair_value, exit_price=exit_price,
                           opened_at=pos.opened_at)
            frac_left = (pos.count - m) / pos.count
            pos.count -= m
            pos.cost_basis -= cost_closed
            pos.open_fees -= ofees_closed
            if pos.expected_edge_total is not None:
                pos.expected_edge_total = None if full else pos.expected_edge_total - (edge_closed or ZERO)
            pos.fv_sum *= float(frac_left)
            pos.fv_weight *= float(frac_left)
            pos.realized_pnl += pnl
            self.realized_pnl += pnl
            self._sweep_profit(pnl)
            self._settled += 1
            self._wins += pnl > 0
            redemption = ONE * m
            batch.settlements.append(s)
        n_open = n - m
        if n_open > 0:
            if pos.count == 0:
                pos.side = side
                pos.opened_at = ts
                pos.cost_basis = ZERO
                pos.open_fees = ZERO
                pos.expected_edge_total = None
                pos.fv_sum = 0.0
                pos.fv_weight = 0.0
                pos.event_ticker = order.event_ticker or pos.event_ticker
            pos.count += n_open
            pos.cost_basis += buy_price * n_open
            pos.open_fees += fee - fee_close
            if order.expected_edge is not None:
                pos.expected_edge_total = (pos.expected_edge_total or ZERO) + order.expected_edge * n_open
            if order.fair_value is not None:
                fv = order.fair_value if order.action == "buy" else 1.0 - order.fair_value
                pos.fv_sum += fv * n_open
                pos.fv_weight += n_open
        elif pos.count == 0:
            pos.cost_basis = ZERO
            pos.open_fees = ZERO
            pos.expected_edge_total = None
            pos.fv_sum = 0.0
            pos.fv_weight = 0.0
        pos.updated_at = ts
        batch.positions[pos.key] = pos
        return redemption

    def _execute(self, order: Order, prep: _Prepared, plan: Sequence[_Planned], acc: OrderFeeAccumulator,
                 now: datetime, batch: _Batch) -> None:
        for x in plan:
            shown = prep.book.size_at(order.buy_side, x.price, book="ask")
            self._consume(order.ticker, order.buy_side, x.price, x.count, shown, now)
            self._apply_fill(order, x.price, x.count, x.fee, is_taker=True, ts=now, batch=batch)
        order.fee_state = acc.state
        order.updated_at = now
        if order.remaining <= 0:
            order.status = "filled"
        elif order.tif == "ioc":
            order.status = "cancelled"
            order.status_reason = ("ioc: no liquidity at or better than the limit" if order.filled_count == 0
                                   else f"ioc: {order.remaining} unfilled at the limit (remainder cancelled)")
        else:
            lim = order.buy_limit
            order.queue_ahead = prep.book.size_at(order.buy_side, lim)
            order.reserved = self._reserve_amount(order.remaining, lim, prep.fee_type, prep.fee_mult)
            self.cash -= order.reserved
            order.status = "partially_filled" if order.filled_count else "open"
            if order.ticker not in self._cursors:
                self._cursors[order.ticker] = (now, frozenset())
        self._note_market(prep.market, now)  # thaws a mark frozen before a close-time extension
        self._update_mark(order.ticker, prep.book, now, close_time=prep.market.close_time)
        batch.orders[order.id] = order

    def _maker_fill(self, order: Order, n: int, ts: datetime, fee_type: str, mult: Decimal, batch: _Batch) -> Fill:
        lim = order.buy_limit
        acc = self._acc(order)
        ff = acc.on_fill(lim, n, is_buy=True, is_taker=False, fee_type=fee_type, fee_multiplier=mult)
        order.fee_state = acc.state
        old = order.reserved
        fill = self._apply_fill(order, lim, n, ff, is_taker=False, ts=ts, batch=batch)
        order.reserved = self._reserve_amount(order.remaining, lim, fee_type, mult)
        self.cash += old - order.reserved
        order.fill_credit = min(order.fill_credit, D(order.remaining))
        if order.remaining <= 0:
            order.status = "filled"
            order.fill_credit = ZERO
            order.queue_ahead = ZERO
        else:
            order.status = "partially_filled"
        if self.cash < 0:
            log.warning("paper cash negative (%s) after maker fill on order %s", self.cash, order.id)
        return fill

    def _finish(self, order: Order, status: str, reason: str, now: datetime, batch: _Batch) -> None:
        self.cash += order.reserved
        order.reserved = ZERO
        order.fill_credit = ZERO
        order.status = status  # type: ignore[assignment]
        order.status_reason = reason
        order.updated_at = now
        batch.orders[order.id] = order

    # ------------------------------------------------------------------ public: orders

    def _not_recorded(self, orders: Sequence[Order], e: BaseException) -> None:
        now = self._now()
        why = f"not recorded: store error ({type(e).__name__}: {e}); nothing was executed"
        log.error("paper order(s) %s: %s", [o.id for o in orders], why)
        for o in orders:
            self._set_rejected(o, why, now)

    async def place_order(self, intent: Any, *, count: int | None = None,
                          decided_at: datetime | None = None) -> Order:
        """Simulate one order from an ``OrderIntent`` (``count`` overrides ``intent.count``,
        e.g. the risk-approved size). ``decided_at`` (engine orders): when the strategy decided;
        the order then meets only a book received at/after ``decided_at + taker_latency_s``.
        Always returns the :class:`Order`; rejections have ``status == "rejected"`` and
        ``status_reason`` (also when the store failed: then nothing was executed or recorded)."""
        order = self._new_order(intent, count, self._now())
        pristine = _copy_order(order)
        meta: _Meta | str | None = None
        book: Orderbook | None = None
        nb = self.not_before(decided_at)
        if order.status != "rejected" and nb is None:
            meta = await self._meta(order)  # network: no lock held
            if not isinstance(meta, str):
                try:
                    book = await self.md.orderbook(order.ticker, max_age_s=self.book_max_age_s)  # last
                except Exception as e:
                    meta = f"orderbook unavailable: {e}"
        elif order.status != "rejected":
            # the order arrives at ``nb`` whatever the lookups cost: fetch its book concurrently
            meta, got = await asyncio.gather(self._meta(order), self._arrival_book(order.ticker, nb),
                                             return_exceptions=True)
            if isinstance(meta, BaseException):
                meta = f"market lookup failed: {meta}"
            elif not isinstance(meta, str):
                if isinstance(got, BaseException):
                    meta = f"orderbook unavailable: {got}"
                else:
                    book = got
        async with self._lock:
            try:
                with self._atomic():
                    batch = _Batch(new_orders={order.id})
                    now = self._now()
                    if order.status != "rejected":
                        prep: _Prepared | str
                        if isinstance(meta, str) or meta is None:
                            prep = meta or "not prepared"
                        else:
                            fresh = await self._ensure_fresh(order.ticker, book)  # type: ignore[arg-type]
                            now = self._now()
                            prep = ("order book is stale (older than "
                                    f"{self.book_max_age_s + BOOK_LAG_S:g}s)" if fresh is None
                                    else self._finish_prepare(order, meta, fresh, now))
                        if isinstance(prep, str):
                            self._set_rejected(order, prep, now)
                        else:
                            self._reconcile(order.ticker, prep.book, now)
                            levels = self._walk(order.ticker, prep.book, order.buy_side, order.buy_limit,
                                                order.count, now)
                            plan, acc = self._price_plan(order, levels, prep)
                            need = self._required_cash(order, plan, prep, self._closable(order))
                            if need > self.cash:
                                self._set_rejected(order, f"insufficient cash: need {need}, free {self.cash}", now)
                            else:
                                self._execute(order, prep, plan, acc, now, batch)
                    batch.orders[order.id] = order
                    self._commit(batch)
            except STORE_ERRORS as e:
                self._not_recorded([pristine], e)
                return pristine
            if order.status == "rejected":
                self._log("info", "order", f"order {order.id} {order.ticker} rejected: {order.status_reason}",
                          order_id=order.id, strategy=order.strategy)
            return order

    async def place_basket(self, intents: Iterable[Any], *, all_or_none: bool = True,
                           counts: Sequence[int | None] | None = None,
                           decided_at: datetime | None = None) -> list[Order]:
        """Place several legs. With ``all_or_none`` (default) every leg must be IOC and fully
        fillable at its limit against fresh books (after consumed liquidity, legs sharing
        levels see each other's use) and the combined cash must suffice; otherwise every leg
        is rejected and nothing executes. All books are fetched together, after every leg's
        market/fee lookups (with ``decided_at``: books received at/after the order's arrival,
        see :meth:`place_order`). A rejected basket with a fully fillable leg is logged and
        counted as "would have legged" (``legged_baskets``)."""
        intents = list(intents)
        counts = list(counts) if counts is not None else [None] * len(intents)
        if len(counts) != len(intents):
            raise ValueError("counts must match intents")
        if not all_or_none:
            return [await self.place_order(i, count=c, decided_at=decided_at)
                    for i, c in zip(intents, counts, strict=True)]
        now = self._now()
        orders = [self._new_order(i, c, now) for i, c in zip(intents, counts, strict=True)]
        if not orders:
            return []
        group = next((o.group_id for o in orders if o.group_id), None) or f"basket-{orders[0].id}"
        for o in orders:
            o.group_id = o.group_id or group
        pristine = [_copy_order(o) for o in orders]
        failure: str | None = None
        for o in orders:
            if o.status == "rejected":
                failure = f"leg {o.id} ({o.ticker}): {o.status_reason}"
                break
            if o.tif != "ioc":
                failure = f"leg {o.id} ({o.ticker}): all-or-none basket legs must be ioc"
                break
        metas: list[_Meta] = []
        books: dict[str, Orderbook] = {}
        nb = self.not_before(decided_at)
        arrival = (asyncio.ensure_future(self._arrival_books([o.ticker for o in orders], nb))
                   if failure is None and nb is not None else None)  # the legs arrive at nb, lookups or not
        try:
            if failure is None:
                for o in orders:
                    m = await self._meta(o)
                    if isinstance(m, str):
                        failure = f"leg {o.id} ({o.ticker}): {m}"
                        break
                    metas.append(m)
            if failure is None:
                books = (await arrival) if arrival is not None else await self._fetch_books(
                    [o.ticker for o in orders], self.book_max_age_s)
        finally:
            if arrival is not None and not arrival.done():
                arrival.cancel()
        if failure is None:
            missing = [o for o in orders if o.ticker not in books]
            if missing:
                failure = f"leg {missing[0].id} ({missing[0].ticker}): orderbook unavailable"
        async with self._lock:
            try:
                with self._atomic():
                    batch = _Batch(new_orders={o.id for o in orders})
                    now = self._now()
                    preps: list[_Prepared] = []
                    if failure is None:
                        for o, m in zip(orders, metas, strict=True):
                            fresh = await self._ensure_fresh(o.ticker, books[o.ticker])
                            now = self._now()
                            p = ("order book is stale" if fresh is None else self._finish_prepare(o, m, fresh, now))
                            if isinstance(p, str):
                                failure = f"leg {o.id} ({o.ticker}): {p}"
                                break
                            preps.append(p)
                    plans: list[tuple[list[_Planned], OrderFeeAccumulator]] = []
                    legged: tuple[int, int] | None = None  # (fully fillable legs, legs short)
                    if failure is None:
                        overlay: dict[tuple[str, str, Decimal], Decimal] = {}
                        cash_left = self.cash
                        for o, p in zip(orders, preps, strict=True):
                            o.created_at = o.updated_at = now
                            self._reconcile(o.ticker, p.book, now)
                            levels = self._walk(o.ticker, p.book, o.buy_side, o.buy_limit, o.count, now, overlay)
                            got = sum(n for _, n in levels)
                            if got < o.count:
                                failure = (f"leg {o.id} ({o.ticker} {o.action} {o.side} @ {o.limit_price}): "
                                           f"only {got}/{o.count} fillable at the limit")
                                legged = self._legged_count(orders, preps, now)
                                break
                            plan, acc = self._price_plan(o, levels, p)
                            need = self._required_cash(o, plan, p, 0)  # no netting credit in a basket pre-check
                            if need > cash_left:
                                failure = f"insufficient cash for basket: need {need} more, free {cash_left}"
                                break
                            cash_left -= need
                            for price, n in levels:
                                key = (o.ticker, o.buy_side, price)
                                overlay[key] = overlay.get(key, ZERO) + n
                            plans.append((plan, acc))
                    if failure is not None:
                        for o in orders:
                            own = o.status == "rejected" and failure.startswith(f"leg {o.id} ")
                            self._set_rejected(o, o.status_reason if own else f"basket rejected: {failure}", now)
                            batch.orders[o.id] = o
                    else:
                        for o, p, (plan, acc) in zip(orders, preps, plans, strict=True):
                            self._execute(o, p, plan, acc, now, batch)
                    self._commit(batch)
            except STORE_ERRORS as e:
                self._not_recorded(pristine, e)
                return pristine
            if failure is not None:
                self._log("info", "order", f"basket {group} rejected: {failure}", group_id=group)
                if legged is not None and legged[0] > 0:
                    strat = orders[0].strategy
                    self.legged_baskets[strat] = self.legged_baskets.get(strat, 0) + 1
                    self._log("warning", "basket_legged",
                              f"basket {group} ({strat}): {legged[0]} of {len(orders)} legs were fully fillable "
                              f"and {legged[1]} not; real independent IOCs would have left a partial, unhedged "
                              f"position (paper all-or-none rejected it: {failure})",
                              group_id=group, strategy=strat, fillable_legs=legged[0], short_legs=legged[1])
            return orders

    def _legged_count(self, orders: Sequence[Order], preps: Sequence[_Prepared], now: datetime) -> tuple[int, int]:
        """(legs fully fillable on their own book, legs not) - what independent IOCs sent in leg
        order would have done (each leg sees the liquidity the legs before it took)."""
        overlay: dict[tuple[str, str, Decimal], Decimal] = {}
        full = short = 0
        for o, p in zip(orders, preps, strict=True):
            levels = self._walk(o.ticker, p.book, o.buy_side, o.buy_limit, o.count, now, overlay)
            if sum(n for _, n in levels) >= o.count:
                full += 1
                for price, n in levels:
                    key = (o.ticker, o.buy_side, price)
                    overlay[key] = overlay.get(key, ZERO) + n
            else:
                short += 1
        return full, short

    async def cancel_order(self, order_id: int, reason: str = "cancelled by user") -> Order:
        """Cancel a resting order (releases its reservation). Non-open orders are returned
        unchanged; unknown ids raise ``KeyError``."""
        async with self._lock:
            o = self._open.get(order_id)
            if o is None:
                stored = self.store.get_order(order_id) if self.store is not None else None
                if stored is None:
                    raise KeyError(order_id)
                return stored
            with self._atomic():
                batch = _Batch()
                self._finish(o, "cancelled", reason, self._now(), batch)
                self._commit(batch)
            return o

    async def cancel_orders(self, order_ids: Iterable[int], *, reason: str = "cancelled by strategy",
                            reasons: Mapping[int, str] | None = None, strategy: str | None = None,
                            sync: bool = True) -> list[Order]:
        """Cancel resting orders on a strategy's request (cancel / cancel-replace).

        With ``sync`` (default) the orders' markets first get a resting-order pass limited to
        them (:meth:`process_resting_orders` with ``tickers``: trade tape since the last poll,
        crossing, expiry), because prints that reached the exchange before the cancel would
        have filled them there; only what is still open is then cancelled. Ids that are not
        open (or, with ``strategy``, belong to another strategy) are skipped. Returns the
        requested orders in their final state (``cancelled``, or ``filled``/``expired`` if
        the sync got there first).
        """
        ids = [i for i in dict.fromkeys(order_ids)
               if (o := self._open.get(i)) is not None and (strategy is None or o.strategy == strategy)]
        if not ids:
            return []
        before = {i: self._open[i] for i in ids}
        if sync:
            try:
                await self.process_resting_orders({o.ticker for o in before.values()})
            except STORE_ERRORS:
                raise
            except Exception as e:  # the cancel itself must still go through
                log.warning("syncing resting orders before a cancel failed: %s", e)
        async with self._lock:
            with self._atomic():
                batch = _Batch()
                now = self._now()
                out: list[Order] = []
                for i in ids:
                    o = self._open.get(i)
                    if o is None:  # filled / expired during the sync
                        out.append(self.get_order(i) or before[i])
                        continue
                    self._finish(o, "cancelled", (reasons or {}).get(i) or reason, now, batch)
                    out.append(o)
                self._commit(batch)
            return out

    async def cancel_all(self, *, ticker: str | None = None, strategy: str | None = None,
                         reason: str = "cancelled") -> list[Order]:
        async with self._lock:
            with self._atomic():
                batch = _Batch()
                now = self._now()
                out = []
                for o in sorted(self._open.values(), key=lambda o: o.id):
                    if (ticker is None or o.ticker == ticker) and (strategy is None or o.strategy == strategy):
                        self._finish(o, "cancelled", reason, now, batch)
                        out.append(o)
                self._commit(batch)
            return out

    # ------------------------------------------------------------------ public: resting orders

    async def process_resting_orders(self, tickers: Iterable[str] | None = None, *,
                                     max_trade_polls: int | None = None) -> list[Fill]:
        """Maker-fill simulation + expiries for resting orders (engine: every ``order_poll_s``).

        All network reads happen first, without the ledger lock (markets batch-refreshed,
        trades per ticker, then every book in one batch); the lock is held only to apply them.

        ``tickers`` limits the pass to those markets (e.g. before a strategy cancel). Reading
        the trade tape costs one request per market, so a full pass reads it for at most
        ``max_trade_polls`` markets (default ``max_trade_polls_per_pass``; an explicit
        ``tickers`` list is read in full): markets with an order at/after its expiry or
        close first, then the least recently read. Prints are kept by the per-ticker cursor,
        so a market skipped this pass loses nothing; its fills just arrive a pass later (with
        the print's timestamp), and its expiries wait for the pass that reads its prints
        (a print before the expiry can still fill), and so do its book crossing and queue bound.
        """
        async with self._poll_lock:
            live = sorted({o.ticker for o in self._open.values()})
            if tickers is not None:
                want = set(tickers)
                live = [t for t in live if t in want]
            if not live:
                return []
            data: dict[str, _PollData] = {t: _PollData() for t in live}
            markets = await self._fetch_markets(live)
            if max_trade_polls is None and tickers is None:
                max_trade_polls = self.max_trade_polls_per_pass
            poll = self._trade_poll_set(live, markets, max_trade_polls)
            for t in live:
                d = data[t]
                d.market = markets.get(t)
                if t in poll:
                    d.polled = True
                    cur = self._cursors.get(t)
                    created = [o.created_at for o in self._open.values() if o.ticker == t and o.created_at]
                    since = cur[0] if cur else (min(created) if created else self._now())
                    self._trade_polled[t] = self._now()
                    try:
                        d.trades = list(await self.md.trades_since(t, since))
                    except Exception as e:
                        log.warning("resting orders: trades for %s unavailable: %s", t, e)
                if d.market is not None:
                    d.fee_type, d.fee_mult = await self._fee_params(d.market, self._now())
                    d.active = await self._trading_active(d.market)
            books = await self._fetch_books(live, self.book_max_age_s)  # last: as fresh as possible
            async with self._lock:
                now = self._now()
                with self._atomic():  # one transaction for the whole pass (all or nothing)
                    batch = _Batch()
                    for t in live:
                        d = data[t]
                        d.book = books.get(t)
                        if d.book is not None and self._book_age_s(d.book, now) > self.book_max_age_s + BOOK_LAG_S:
                            d.book = None  # too old to cross against (prints are still processed)
                        orders = [o for o in self._open.values() if o.ticker == t]  # re-read: cancels happen
                        if orders:
                            self._process_ticker(t, orders, d, now, batch)
                    self._commit(batch)
            return list(batch.fills)

    def _trade_poll_set(self, tickers: Sequence[str], markets: Mapping[str, Market], cap: int | None) -> set[str]:
        """Which markets' trade tapes to read this pass (``cap`` None = all): markets with an
        order due to expire / a closed market first, then the least recently read ones."""
        if cap is None or cap >= len(tickers):
            return set(tickers)
        if cap <= 0:
            return set()
        now = self._now()
        due: set[str] = set()
        for o in self._open.values():
            m = markets.get(o.ticker)
            if o.expires_at is not None and now >= o.expires_at:
                due.add(o.ticker)
            elif m is not None and (not m.is_open or (m.close_time is not None and now >= m.close_time)):
                due.add(o.ticker)
        never = datetime.min.replace(tzinfo=UTC)
        ranked = sorted(tickers, key=lambda t: (t not in due, self._trade_polled.get(t, never), t))
        return set(ranked[:cap])

    def _process_ticker(self, ticker: str, orders: list[Order], d: _PollData, now: datetime, batch: _Batch) -> None:
        orders = [o for o in orders if o.is_open]
        market = d.market
        close = market.close_time if market is not None else None
        fee_type, mult = d.fee_type, d.fee_mult
        if market is not None:
            self._note_market(market, now)  # freezes the mark (net of consumption) once closed

        def cutoff(o: Order) -> datetime | None:
            c = o.expires_at
            if close is not None:
                c = close if c is None else min(c, close)
            return c

        def priority(o: Order) -> tuple[Any, ...]:  # price-time priority among our bids
            return (-o.buy_limit, o.created_at or now, o.id)

        # 1) real prints after placement, from takers selling into the bids
        first = min((o.created_at for o in orders if o.created_at is not None), default=now)
        cur_ts, seen = self._cursors.get(ticker, (first, frozenset()))
        new = sorted((t for t in (d.trades or ()) if not t.is_block_trade and (
            t.ts > cur_ts or (t.ts == cur_ts and t.trade_id not in seen))), key=lambda t: (t.ts, t.trade_id))
        for t in new:
            hit = _hit_side(t)
            if hit is None:
                continue
            elig = sorted((o for o in orders if o.is_open and o.buy_side == hit and o.created_at is not None
                           and o.created_at < t.ts and not ((c := cutoff(o)) is not None and t.ts >= c)),
                          key=priority)
            tp = t.price(hit)  # type: ignore[arg-type]
            left = t.count
            burned = ZERO  # real contracts at ``tp`` this print went through (ahead of our orders there)
            for o in elig:
                lim = o.buy_limit
                if lim < tp:
                    break  # sorted best-first: the rest are below the print too
                if lim == tp:
                    qa = o.queue_ahead or ZERO
                    burn = min(max(ZERO, qa - burned), left)
                    left -= burn
                    burned += burn
                    o.queue_ahead = max(ZERO, qa - burned)
                take = max(ZERO, min(left, D(o.remaining) - o.fill_credit))
                if take <= 0:
                    continue
                o.fill_credit += take
                left -= take
                n = int(o.fill_credit)
                if n > 0:
                    o.fill_credit -= n
                    self._maker_fill(o, n, t.ts, fee_type, mult, batch)
        if new:
            last = new[-1].ts
            ids = frozenset(t.trade_id for t in new if t.ts == last)
            self._cursors[ticker] = (last, ids | seen if last == cur_ts else ids)
        elif ticker not in self._cursors:
            self._cursors[ticker] = (cur_ts, seen)
        # 2) the live book: queue bound, crossing fills (only while the market is tradable). Both
        #    wait for a pass that has read the prints first: the book already reflects prints we
        #    have not processed, so bounding the queue or crossing from it now would let those
        #    earlier prints reach the order after its queue_ahead was zeroed and fill it again.
        book = d.book
        if book is not None:
            self._observe_book(ticker, book, now)
            tradable = market is not None and d.active and market.is_tradable(now)
            for o in sorted(orders, key=priority) if d.polled else ():
                if not o.is_open:
                    continue
                lim = o.buy_limit
                shown = book.size_at(o.buy_side, lim)
                if o.queue_ahead is None or shown < o.queue_ahead:
                    o.queue_ahead = shown
                c = cutoff(o)
                if not tradable or (c is not None and now >= c):
                    continue
                crossed = False
                for lv in book.asks(o.buy_side):
                    if o.remaining <= 0 or lv.price > lim:
                        break
                    n = min(o.remaining, int(self._available(ticker, o.buy_side, lv.price, lv.size, now)))
                    if n > 0:
                        crossed = True
                        self._consume(ticker, o.buy_side, lv.price, n, lv.size, now)
                        self._maker_fill(o, n, now, fee_type, mult, batch)
                if crossed:
                    o.queue_ahead = ZERO
        # 3) expiry - only once this pass has read the prints (an earlier print can still fill)
        if not d.polled:
            for o in orders:
                batch.orders[o.id] = o
            return
        closed = market is not None and (not market.is_open or (close is not None and now >= close))
        for o in orders:
            if o.is_open:
                if o.expires_at is not None and now >= o.expires_at:
                    self._finish(o, "expired", "gtc expiry reached", now, batch)
                elif market is not None and not market.is_open:
                    self._finish(o, "expired", f"market not active (status={market.status})", now, batch)
                elif close is not None and now >= close:
                    self._finish(o, "expired", "market closed", now, batch)
            batch.orders[o.id] = o
        if closed:
            self._forget_market(ticker)

    # ------------------------------------------------------------------ public: settlement

    async def check_settlements(self) -> list[Settlement]:
        """Poll ``market(ticker, fresh=True)`` for every held market and settle finalized ones.

        A store failure restores the in-memory ledger and is re-raised (the next poll retries)."""
        markets: dict[str, Market] = {}
        for t in sorted(self._held_tickers()):
            try:
                markets[t] = await self.md.market(t, fresh=True)
            except Exception as e:
                self._log("warning", "settlement", f"settlement poll failed for {t}: {e}", ticker=t)
        async with self._lock:
            out: list[Settlement] = []
            for t in sorted(markets):
                m = markets[t]
                if not (m.is_final or (self.settle_on_determined and m.is_determined)):
                    self._settle(m, self._now())  # marks only: nothing is written
                    continue
                with self._atomic():
                    out.extend(self._settle(m, self._now()))
            return out

    async def settle_market(self, market: Market) -> list[Settlement]:
        """Apply a market's result directly (backtests / tests)."""
        async with self._lock:
            with self._atomic():
                return self._settle(market, self._now())

    def _settle(self, m: Market, now: datetime) -> list[Settlement]:
        self._note_market(m, now)  # closed, no result yet: freeze the mark before forgetting consumption
        yes_val = m.payout_per_contract("yes") if m.is_determined or m.is_final else None
        if yes_val is not None:
            mk = self._marks.get(m.ticker)
            if mk is None:
                self._marks[m.ticker] = Mark(None, None, None, now, yes_val)
            else:
                mk.settled_yes = yes_val
        if not m.is_open:
            self._forget_market(m.ticker)
        ready = m.is_final or (self.settle_on_determined and m.is_determined)
        if not ready:
            return []
        batch = _Batch()
        out: list[Settlement] = []
        for p in sorted((p for p in self._positions.values() if p.ticker == m.ticker and p.count > 0),
                        key=lambda p: p.key):
            per = m.payout_per_contract(p.side)
            if per is None:
                self._log("warning", "settlement",
                          f"{m.ticker} is {m.status} with result {m.result!r} but no payout value; not settled",
                          ticker=m.ticker)
                continue
            payout = floor_to(per * p.count, self.precision)
            pnl = payout - p.cost_basis - p.open_fees
            s = Settlement(id=self._next("settlement"), ticker=p.ticker, result=m.result, side=p.side,
                           count=p.count, payout=payout, cost_basis=p.cost_basis, pnl=pnl, ts=now,
                           strategy=p.strategy, event_ticker=p.event_ticker, kind="settlement", fees=p.open_fees,
                           expected_edge=p.expected_edge_total, fair_value=p.fair_value,
                           settlement_value=m.settlement_value if m.settlement_value is not None else yes_val,
                           opened_at=p.opened_at)
            self.cash += payout
            self.realized_pnl += pnl
            self._sweep_profit(pnl)
            self._settled += 1
            self._wins += pnl > 0
            p.realized_pnl += pnl
            p.count = 0
            p.cost_basis = ZERO
            p.open_fees = ZERO
            p.expected_edge_total = None
            p.fv_sum = 0.0
            p.fv_weight = 0.0
            p.updated_at = now
            batch.positions[p.key] = p
            batch.settlements.append(s)
            out.append(s)
        for o in sorted(self._open.values(), key=lambda o: o.id):
            if o.ticker == m.ticker:
                self._finish(o, "cancelled", "market settled", now, batch)
        self._commit(batch)
        for s in out:
            self._log("info", "settlement", f"{s.ticker} settled {s.result}: {s.count} {s.side} "
                      f"payout {s.payout} pnl {s.pnl}", ticker=s.ticker, strategy=s.strategy, pnl=str(s.pnl))
        return out

    # ------------------------------------------------------------------ public: views

    @property
    def reserved_cash(self) -> Decimal:
        return sum((o.reserved for o in self._open.values()), ZERO)

    def open_orders(self, *, ticker: str | None = None, strategy: str | None = None) -> list[Order]:
        return [o for o in sorted(self._open.values(), key=lambda o: o.id)
                if (ticker is None or o.ticker == ticker) and (strategy is None or o.strategy == strategy)]

    def get_order(self, order_id: int) -> Order | None:
        o = self._open.get(order_id)
        if o is None and self.store is not None:
            o = self.store.get_order(order_id)
        return o

    def positions(self, *, strategy: str | None = None, ticker: str | None = None) -> list[Position]:
        """Open positions (count > 0)."""
        return [p for p in sorted(self._positions.values(), key=lambda p: (p.ticker, p.strategy))
                if p.count > 0 and (strategy is None or p.strategy == strategy)
                and (ticker is None or p.ticker == ticker)]

    def position(self, ticker: str, strategy: str = "") -> Position | None:
        p = self._positions.get((strategy, ticker))
        return p if p is not None and p.count > 0 else None

    def liquidation_value(self) -> Decimal:
        return sum((v[0] for v in self._position_values().values()), ZERO)

    def mid_value(self) -> Decimal:
        return sum((v[1] for v in self._position_values().values()), ZERO)

    def _touch_day(self, equity: Decimal, now: datetime) -> Decimal:
        day = now.astimezone(UTC).date().isoformat()
        if self._day_start is None or self._day_start[0] != day:
            self._day_start = (day, equity)
        return self._day_start[1]

    def strategy_daily_pnl(self, values: Mapping[tuple[str, str], tuple[Decimal, Decimal]] | None = None
                           ) -> dict[str, Decimal]:
        """Each strategy's P&L since the UTC day started: (realized + unrealized) now minus the
        same at the day's first observation (a strategy first seen today started at 0)."""
        values = values if values is not None else self._position_values()
        cur: dict[str, Decimal] = {name: D(st.get("realized_pnl", ZERO)) for name, st in self._stats.items()}
        for p in self.positions():
            lv, _ = values[p.key]
            cur[p.strategy] = cur.get(p.strategy, ZERO) + lv - p.cost_basis - p.open_fees
        day = self._now().astimezone(UTC).date().isoformat()
        if self._strategy_day is None or self._strategy_day[0] != day:
            self._strategy_day = (day, dict(cur))
        start = self._strategy_day[1]
        return {k: v - start.get(k, ZERO) for k, v in cur.items()}

    def account(self) -> AccountState:
        """Account snapshot from cached marks (call ``refresh_marks`` / ``record_equity_snapshot`` to update)."""
        now = self._now()
        lv = mv = cost = ofees = ZERO
        pos = self.positions()
        values = self._position_values()
        for p in pos:
            lp, mp = values[p.key]
            lv += lp
            mv += mp
            cost += p.cost_basis
            ofees += p.open_fees
        reserved = self.reserved_cash
        equity = self.cash + reserved + lv
        equity_mid = self.cash + reserved + mv
        #: true account value: tradeable equity plus profit already set aside (``_sweep_profit``)
        net_worth = equity + self.reserved_profit
        total = net_worth - self.starting_balance
        day_start = self._touch_day(net_worth, now)
        #: each strategy's day starts at its first observation too (the snapshot job calls this)
        self._last_strategy_daily = self.strategy_daily_pnl(values)
        peak = max(self._peak_equity, net_worth) if self._peak_equity is not None else net_worth
        dd = (peak - net_worth) / peak * 100 if peak > 0 else ZERO
        return AccountState(
            ts=now, starting_balance=self.starting_balance, cash=self.cash, reserved_cash=reserved,
            positions_liquidation_value=lv, positions_mid_value=mv, positions_cost_basis=cost, open_fees=ofees,
            equity=equity, equity_mid=equity_mid, realized_pnl=self.realized_pnl,
            unrealized_pnl=lv - cost - ofees, unrealized_pnl_mid=mv - cost - ofees, fees_paid=self.fees_paid,
            reserved_profit=self.reserved_profit, net_worth=net_worth,
            profit_sweep_enabled=self.profit_sweep_enabled, profit_sweep_pct=self.profit_sweep_pct,
            total_pnl=total,
            total_return_pct=(total / self.starting_balance * 100) if self.starting_balance else ZERO,
            todays_pnl=net_worth - day_start, day_start_equity=day_start,
            max_drawdown_pct=max(self._max_dd_pct, dd), open_positions=len(pos), open_orders=len(self._open),
            settled_trades=self._settled, wins=self._wins,
        )

    def portfolio(self) -> PortfolioView:
        """Immutable view for strategies (``ctx.portfolio``) and ``RiskManager.check``."""
        a = self.account()
        return PortfolioView(
            ts=a.ts, starting_balance=a.starting_balance, cash=a.cash, reserved_cash=a.reserved_cash,
            equity=a.equity, equity_mid=a.equity_mid, realized_pnl=a.realized_pnl, unrealized_pnl=a.unrealized_pnl,
            fees_paid=a.fees_paid, day_start_equity=a.day_start_equity,
            positions=tuple(dataclasses.replace(p) for p in self.positions()),
            open_orders=tuple(_copy_order(o) for o in self.open_orders()),
            strategy_daily_pnl=dict(self._last_strategy_daily),  # computed by account() just now
        )

    def positions_json(self) -> list[dict[str, Any]]:
        """Positions for ``GET /api/positions`` (the API adds title/close_time/url).

        ``mark_price`` is the average exit price per contract when selling the whole position
        into the marked bids (== the best bid whenever the top level covers the position);
        ``best_bid`` is the top of book for the position's side. ``mark_stale`` is true while
        the market is closed without a result: the mark is then the last pre-close ladder
        (net of our own consumption), observed at ``mark_ts``; once determined it is the payout."""
        out = []
        values = self._position_values()
        now = self._now()
        for p in self.positions():
            lv, mv = values[p.key]
            mk = self._marks.get(p.ticker)
            d = p.to_json(mark_price=q6(lv / p.count) if mk is not None and p.count else None,
                          liquidation_value=lv, mid_value=mv)
            d["mark_stale"] = bool(mk is not None and mk.is_stale(now))
            d["mark_ts"] = iso(mk.ts) if mk is not None else None
            d["best_bid"] = float(mk.liquidation_price(p.side)) if mk is not None else None
            d["yes_bid"] = float(mk.yes_bid) if mk is not None and mk.yes_bid is not None else None
            d["yes_ask"] = float(mk.yes_ask) if mk is not None and mk.yes_ask is not None else None
            out.append(d)
        return out

    def strategy_stats(self) -> dict[str, dict[str, Any]]:
        """Per-strategy stats for ``GET /api/strategies`` (Decimal values; ``win_rate`` float|None).

        Ledger totals are kept in memory (loaded once, updated on every commit), so this never
        scans the fills/settlements tables."""
        out: dict[str, dict[str, Any]] = {name: dict(v) for name, v in self._stats.items()}
        values = self._position_values()
        for p in self.positions():
            lv, _ = values[p.key]
            d = out.setdefault(p.strategy, dict(_EMPTY_STATS))
            d["open_positions"] = d.get("open_positions", 0) + 1
            d["unrealized_pnl"] = d.get("unrealized_pnl", ZERO) + lv - p.cost_basis - p.open_fees
            d["exposure"] = d.get("exposure", ZERO) + p.cost_basis
        for o in self._open.values():
            d = out.setdefault(o.strategy, dict(_EMPTY_STATS))
            d["exposure"] = d.get("exposure", ZERO) + o.reserved
        for name, n in self.legged_baskets.items():
            out.setdefault(name, dict(_EMPTY_STATS))["legged_baskets"] = n
        for d in out.values():
            d.setdefault("open_positions", 0)
            d.setdefault("unrealized_pnl", ZERO)
            d.setdefault("exposure", ZERO)
            d.setdefault("legged_baskets", 0)
            d["win_rate"] = d["wins"] / d["settled"] if d["settled"] else None
        return out

    async def record_equity_snapshot(self, *, refresh: bool = True) -> AccountState:
        """Refresh marks, write an ``equity_snapshots`` row, update peak/drawdown (engine: every ``snapshot_s``)."""
        books = await self._fetch_books(sorted(self._held_tickers()), self.mark_max_age_s) if refresh else {}
        async with self._lock:
            with self._atomic():
                now = self._now()
                self._apply_books(books, now)
                self._gc(now)
                a = self.account()
                self._peak_equity = max(self._peak_equity, a.net_worth) if self._peak_equity is not None else a.net_worth
                self._max_dd_pct = a.max_drawdown_pct
                if self.store is not None:
                    with self.store.transaction():
                        self.store.insert_equity_snapshot(
                            ts=a.ts, equity=a.equity, equity_mid=a.equity_mid, cash=a.cash,
                            reserved_cash=a.reserved_cash, positions_value=a.positions_liquidation_value,
                            realized_pnl=a.realized_pnl, unrealized_pnl=a.unrealized_pnl)
                        written = self._write_state()
                    self._persisted.update(written)
            return a

    async def reset(self, starting_balance: Any = None) -> AccountState:
        """Wipe the paper account (store tables included) and start over (one transaction)."""
        async with self._lock:
            start = D(starting_balance) if starting_balance is not None else self.starting_balance
            sweep_enabled, sweep_pct = self.profit_sweep_enabled, self.profit_sweep_pct  # kept across reset
            if self.store is not None:
                with self.store.transaction():
                    self.store.reset_paper_state()
                    self.store.save_account(starting_balance=start, cash=start,
                                            profit_sweep_enabled=sweep_enabled, profit_sweep_pct=sweep_pct,
                                            ts=self._now())
            self._init_state(start)
            self.profit_sweep_enabled, self.profit_sweep_pct = sweep_enabled, sweep_pct
            self._persisted = self._state_values()
            self._log("info", "account", f"paper account reset to {start}")
            return self.account()

    async def set_profit_sweep(self, *, enabled: bool | None = None, pct: Any = None) -> AccountState:
        """Turn the profit sweep on/off and/or change its %% (``PATCH /api/account``); takes effect
        on the next settlement/close and persists across restarts."""
        async with self._lock:
            with self._atomic():
                if pct is not None:
                    p = D(pct)
                    if not (0 <= p <= 100):
                        raise ValueError("profit_sweep_pct must be between 0 and 100")
                    self.profit_sweep_pct = p
                if enabled is not None:
                    self.profit_sweep_enabled = bool(enabled)
                if self.store is not None:
                    with self.store.transaction():
                        written = self._write_state()
                    self._persisted.update(written)
            self._log("info", "account", f"profit sweep: enabled={self.profit_sweep_enabled} "
                      f"pct={self.profit_sweep_pct}")
            return self.account()

    async def withdraw_reserved_profit(self, *, amount: Any = None, pct: Any = None) -> Decimal:
        """Move money from ``reserved_profit`` back into tradeable ``cash`` (manual, reverses past
        sweeps). ``amount`` ($) or ``pct`` (%% of the current ``reserved_profit``); neither given
        withdraws it all. Returns the amount actually moved (capped at ``reserved_profit``)."""
        if amount is not None and pct is not None:
            raise ValueError("pass amount or pct, not both")
        async with self._lock:
            with self._atomic():
                if pct is not None:
                    p = D(pct)
                    if not (0 <= p <= 100):
                        raise ValueError("pct must be between 0 and 100")
                    moved = floor_to(self.reserved_profit * p / 100, self.precision)
                elif amount is not None:
                    moved = D(amount)
                    if moved < 0:
                        raise ValueError("amount must be >= 0")
                else:
                    moved = self.reserved_profit
                moved = min(moved, self.reserved_profit)
                self.reserved_profit -= moved
                self.cash += moved
                if self.store is not None:
                    with self.store.transaction():
                        written = self._write_state()
                    self._persisted.update(written)
            if moved > 0:
                self._log("info", "account", f"${moved} moved from reserved profit back to cash")
            return moved

