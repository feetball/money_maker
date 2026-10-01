"""Paper-trading ledger models (ARCHITECTURE.md §6).

All money is ``Decimal``. Prices are dollars per contract **for the side named** on the
record (a NO fill at 0.38 cost $0.38 per NO contract). JSON views (``to_json``) follow
§2: floats rounded to 4 dp, ISO-8601 UTC timestamps ending in ``Z``.

Accounting conventions (the broker enforces them; the tests check the identities):

* ``Position.cost_basis`` is the principal paid for the *open* contracts, excluding fees.
  Fees paid to open them sit in ``Position.open_fees`` until the contracts are closed or
  settled, so ``unrealized = liquidation_value - cost_basis - open_fees``.
* P&L is **realized only when contracts leave the position**: by settlement, or by
  Kalshi-style netting (buying the opposite side, or ``sell``). Each such event writes a
  :class:`Settlement` (``kind="settlement"`` or ``kind="close"``), so
  ``account.realized_pnl == sum(settlement.pnl)``.
* Equity identity: ``cash + reserved_cash + positions_liquidation_value ==
  starting_balance + realized_pnl + unrealized_pnl``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Literal, Protocol

from kalshibot.money import ONE, ZERO, D, f4

__all__ = [
    "OPEN_ORDER_STATUSES",
    "Q6",
    "AccountState",
    "Action",
    "Fill",
    "OrderIntentLike",
    "Order",
    "OrderStatus",
    "PortfolioView",
    "Position",
    "Settlement",
    "Side",
    "Tif",
    "iso",
    "opposite",
    "parse_iso",
    "portfolio_exposure",
    "q6",
]

Side = Literal["yes", "no"]
Action = Literal["buy", "sell"]
Tif = Literal["ioc", "gtc"]
OrderStatus = Literal["open", "partially_filled", "filled", "cancelled", "expired", "rejected"]

#: Statuses of a live (resting) order. ``partially_filled`` is only used while the
#: order is still resting; a terminal order with some fills is ``cancelled``/``expired``
#: with ``filled_count > 0``.
OPEN_ORDER_STATUSES = frozenset({"open", "partially_filled"})

#: Ledger granularity for pro-rata allocations (fees are 6-dp amounts).
Q6 = Decimal("0.000001")


def q6(x: Decimal) -> Decimal:
    return x.quantize(Q6, rounding=ROUND_HALF_EVEN)


def opposite(side: str) -> Side:
    return "no" if side == "yes" else "yes"


def iso(dt: datetime | None) -> str | None:
    """UTC ISO-8601 with microseconds and ``Z`` (lexicographically sortable)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_iso(s: str | datetime | None) -> datetime | None:
    if s is None or s == "":
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=UTC)
    txt = s[:-1] + "+00:00" if s.endswith("Z") else s
    dt = datetime.fromisoformat(txt)
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def _dec(x: Decimal | None) -> float | None:
    return f4(x) if x is not None else None


class OrderIntentLike(Protocol):
    """What the broker/risk manager read from an intent (``strategies.base.OrderIntent``).

    Only ``ticker``, ``side``, ``count`` and ``limit_price`` are required; the rest are
    read with ``getattr`` defaults, so any object with these attributes works.
    """

    ticker: str
    side: str
    count: int
    limit_price: Decimal


# --------------------------------------------------------------------------- Order


@dataclass(slots=True)
class Order:
    id: int
    ticker: str
    side: Side
    action: Action
    count: int
    limit_price: Decimal
    tif: Tif = "ioc"
    status: OrderStatus = "open"
    filled_count: int = 0
    avg_fill_price: Decimal | None = None
    strategy: str = ""
    reason: str = ""
    expected_edge: Decimal | None = None
    fair_value: float | None = None
    group_id: str | None = None
    queue_ahead: Decimal | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    fees: Decimal = ZERO
    # ---- extras (not in the §6 list; additive) ----
    event_ticker: str = ""
    #: Why the order was rejected / cancelled / expired (``reason`` is the strategy's text).
    status_reason: str = ""
    #: Cash currently reserved for the resting remainder (principal + fee budget).
    reserved: Decimal = ZERO
    #: Sum of fill price x count in order-side terms (for ``avg_fill_price``).
    filled_notional: Decimal = ZERO
    #: Fractional maker volume credited from fractional trade prints, not yet filled.
    fill_credit: Decimal = ZERO
    #: ``fees.OrderFeeAccumulator.state`` (the per-order rounding accumulator).
    fee_state: dict[str, str] = field(default_factory=dict)
    taker_filled_count: int = 0

    # -- derived -------------------------------------------------------------------

    @property
    def remaining(self) -> int:
        return self.count - self.filled_count

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_ORDER_STATUSES

    @property
    def buy_side(self) -> Side:
        """The side actually bought: ``sell yes @ p`` is ``buy no @ 1 - p`` (§6 rule 5)."""
        return self.side if self.action == "buy" else opposite(self.side)

    @property
    def buy_limit(self) -> Decimal:
        """Limit price for :attr:`buy_side`."""
        return self.limit_price if self.action == "buy" else ONE - self.limit_price

    @property
    def yes_price(self) -> Decimal:
        """YES-denominated order price (what the tick grid applies to)."""
        return self.limit_price if self.side == "yes" else ONE - self.limit_price

    def order_price(self, buy_price: Decimal) -> Decimal:
        """Convert a price for :attr:`buy_side` into this order's side terms."""
        return buy_price if self.action == "buy" else ONE - buy_price

    @property
    def decision(self) -> str:
        """Signal-feed decision: ``executed`` | ``partial`` | ``rejected`` | ``unfilled``.

        A GTC order that is resting without fills counts as ``executed`` (accepted).
        """
        if self.status == "rejected":
            return "rejected"
        if self.filled_count >= self.count:
            return "executed"
        if self.filled_count > 0:
            return "partial"
        if self.is_open:
            return "executed"
        return "unfilled"

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "event_ticker": self.event_ticker,
            "side": self.side,
            "action": self.action,
            "count": self.count,
            "filled_count": self.filled_count,
            "remaining": self.remaining if self.is_open else 0,
            "limit_price": _dec(self.limit_price),
            "avg_fill_price": _dec(self.avg_fill_price),
            "tif": self.tif,
            "status": self.status,
            "status_reason": self.status_reason,
            "strategy": self.strategy,
            "reason": self.reason,
            "expected_edge": _dec(self.expected_edge),
            "fair_value": self.fair_value,
            "group_id": self.group_id,
            "queue_ahead": _dec(self.queue_ahead),
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
            "expires_at": iso(self.expires_at),
            "fees": _dec(self.fees),
            "reserved": _dec(self.reserved),
        }


# --------------------------------------------------------------------------- Fill


@dataclass(frozen=True, slots=True)
class Fill:
    id: int
    order_id: int
    ticker: str
    side: Side
    action: Action
    count: int
    price: Decimal  # in the order's side terms
    fee: Decimal  # net fee charged for this fill (Kalshi rounding, per-order accumulator)
    is_taker: bool
    ts: datetime
    strategy: str = ""
    event_ticker: str = ""

    @property
    def buy_side(self) -> Side:
        return self.side if self.action == "buy" else opposite(self.side)

    @property
    def buy_price(self) -> Decimal:
        return self.price if self.action == "buy" else ONE - self.price

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "order_id": self.order_id,
            "ticker": self.ticker,
            "event_ticker": self.event_ticker,
            "side": self.side,
            "action": self.action,
            "count": self.count,
            "price": _dec(self.price),
            "fee": _dec(self.fee),
            "is_taker": self.is_taker,
            "ts": iso(self.ts),
            "strategy": self.strategy,
        }


# --------------------------------------------------------------------------- Position


@dataclass(slots=True)
class Position:
    """Net position of one strategy in one market (at most one side is held)."""

    ticker: str
    strategy: str = ""
    event_ticker: str = ""
    side: Side = "yes"
    count: int = 0
    cost_basis: Decimal = ZERO  # principal of the open contracts (excl. fees)
    realized_pnl: Decimal = ZERO  # cumulative, from closes and settlements (incl. fees)
    fees_paid: Decimal = ZERO  # cumulative fees on this (strategy, ticker)
    opened_at: datetime | None = None
    expected_edge_total: Decimal | None = None  # sum of intent.expected_edge x contracts
    # ---- extras ----
    open_fees: Decimal = ZERO  # fees paid for the currently open contracts
    fv_sum: float = 0.0  # sum of fair_value x contracts (opening fills with a model)
    fv_weight: float = 0.0  # contracts carrying a fair_value
    updated_at: datetime | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.strategy, self.ticker)

    @property
    def is_open(self) -> bool:
        return self.count > 0

    @property
    def avg_price(self) -> Decimal | None:
        if self.count <= 0:
            return None
        return q6(self.cost_basis / self.count)

    @property
    def fair_value(self) -> float | None:
        """Contract-weighted average model probability of the opening intents."""
        return self.fv_sum / self.fv_weight if self.fv_weight else None

    def to_json(self, *, mark_price: Decimal | None = None, liquidation_value: Decimal | None = None,
                mid_value: Decimal | None = None) -> dict[str, Any]:
        lv = liquidation_value
        out = {
            "ticker": self.ticker,
            "event_ticker": self.event_ticker,
            "side": self.side,
            "count": self.count,
            "avg_price": _dec(self.avg_price),
            "cost_basis": _dec(self.cost_basis),
            "open_fees": _dec(self.open_fees),
            "realized_pnl": _dec(self.realized_pnl),
            "fees_paid": _dec(self.fees_paid),
            "strategy": self.strategy,
            "opened_at": iso(self.opened_at),
            "expected_edge_total": _dec(self.expected_edge_total),
            "fair_value": self.fair_value,
            "mark_price": _dec(mark_price),
            "liquidation_value": _dec(lv),
            "mid_value": _dec(mid_value),
            "unrealized_pnl": _dec(lv - self.cost_basis - self.open_fees) if lv is not None else None,
        }
        return out


# --------------------------------------------------------------------------- Settlement


@dataclass(frozen=True, slots=True)
class Settlement:
    """Contracts leaving a position with realized P&L.

    ``kind="settlement"``: the market was finalized (``result`` = yes | no | scalar; a
    voided/cancelled market settles as ``scalar`` at ``settlement_value``).
    ``kind="close"``: netted out before settlement (bought the opposite side / sold);
    ``result="closed"``, ``payout`` = exit proceeds, ``exit_price`` = per-contract price
    of the held side. Analytics should exclude ``close`` rows from outcome calibration.
    """

    id: int
    ticker: str
    result: str
    side: Side
    count: int
    payout: Decimal
    cost_basis: Decimal
    pnl: Decimal  # payout - cost_basis - fees
    ts: datetime
    strategy: str = ""
    # ---- extras ----
    event_ticker: str = ""
    kind: Literal["settlement", "close"] = "settlement"
    fees: Decimal = ZERO  # opening fees (+ closing fee for kind=close) allocated to these contracts
    expected_edge: Decimal | None = None  # expected $ edge of the opening intents for these contracts
    fair_value: float | None = None
    settlement_value: Decimal | None = None  # YES payout per contract (settlement kind)
    exit_price: Decimal | None = None  # held-side price per contract (close kind)
    opened_at: datetime | None = None

    @property
    def won(self) -> bool:
        return self.pnl > 0

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "event_ticker": self.event_ticker,
            "kind": self.kind,
            "result": self.result,
            "side": self.side,
            "count": self.count,
            "payout": _dec(self.payout),
            "cost_basis": _dec(self.cost_basis),
            "fees": _dec(self.fees),
            "pnl": _dec(self.pnl),
            "expected_edge": _dec(self.expected_edge),
            "fair_value": self.fair_value,
            "settlement_value": _dec(self.settlement_value),
            "exit_price": _dec(self.exit_price),
            "opened_at": iso(self.opened_at),
            "ts": iso(self.ts),
            "strategy": self.strategy,
        }


# --------------------------------------------------------------------------- Account / portfolio


@dataclass(frozen=True, slots=True)
class AccountState:
    ts: datetime
    starting_balance: Decimal
    cash: Decimal  # free cash (excludes reservations)
    reserved_cash: Decimal
    positions_liquidation_value: Decimal
    positions_mid_value: Decimal
    positions_cost_basis: Decimal
    open_fees: Decimal
    equity: Decimal
    equity_mid: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    unrealized_pnl_mid: Decimal
    fees_paid: Decimal
    #: profit swept out of ``cash``/``equity`` (``AccountSettings.profit_sweep_pct``); never spent
    #: on new orders and excluded from the equity used for position sizing / risk limits.
    reserved_profit: Decimal
    #: equity + reserved_profit: true account value including profit set aside.
    net_worth: Decimal
    #: whether/how much of each trade's profit is swept into reserved_profit (``PATCH /api/account``)
    profit_sweep_enabled: bool
    profit_sweep_pct: Decimal
    total_pnl: Decimal
    total_return_pct: Decimal
    todays_pnl: Decimal
    day_start_equity: Decimal | None
    max_drawdown_pct: Decimal
    open_positions: int
    open_orders: int
    settled_trades: int
    wins: int

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.settled_trades if self.settled_trades else None

    def to_json(self) -> dict[str, Any]:
        return {
            "starting_balance": f4(self.starting_balance),
            "cash": f4(self.cash),
            "reserved_cash": f4(self.reserved_cash),
            "positions_liquidation_value": f4(self.positions_liquidation_value),
            "positions_mid_value": f4(self.positions_mid_value),
            "equity": f4(self.equity),
            "equity_mid": f4(self.equity_mid),
            "realized_pnl": f4(self.realized_pnl),
            "unrealized_pnl": f4(self.unrealized_pnl),
            "fees_paid": f4(self.fees_paid),
            "reserved_profit": f4(self.reserved_profit),
            "net_worth": f4(self.net_worth),
            "profit_sweep_enabled": self.profit_sweep_enabled,
            "profit_sweep_pct": f4(self.profit_sweep_pct),
            "total_pnl": f4(self.total_pnl),
            "total_return_pct": f4(self.total_return_pct),
            "todays_pnl": f4(self.todays_pnl),
            "max_drawdown_pct": f4(self.max_drawdown_pct),
            "open_positions": self.open_positions,
            "open_orders": self.open_orders,
            "settled_trades": self.settled_trades,
            "win_rate": self.win_rate,
            "ts": iso(self.ts),
        }


@dataclass(frozen=True, slots=True)
class PortfolioView:
    """Read-only snapshot for strategies (``ctx.portfolio``) and the risk manager.

    ``exposure`` = cost basis of open positions + cash reserved by open orders.
    """

    ts: datetime
    starting_balance: Decimal
    cash: Decimal
    reserved_cash: Decimal
    equity: Decimal
    equity_mid: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees_paid: Decimal
    day_start_equity: Decimal | None
    positions: tuple[Position, ...] = ()
    open_orders: tuple[Order, ...] = ()
    #: each strategy's P&L since the UTC day started (realized + change in unrealized); used by
    #: the per-strategy daily loss limit (``RiskManager``). Empty when unknown.
    strategy_daily_pnl: Mapping[str, Decimal] = field(default_factory=dict)

    @property
    def daily_pnl(self) -> Decimal:
        return self.equity - self.day_start_equity if self.day_start_equity is not None else ZERO

    def position(self, ticker: str, strategy: str | None = None) -> Position | None:
        """Open position in ``ticker`` (of ``strategy`` if given; else the first found)."""
        for p in self.positions:
            if p.ticker == ticker and (strategy is None or p.strategy == strategy) and p.count > 0:
                return p
        return None

    def positions_for(self, ticker: str) -> tuple[Position, ...]:
        return tuple(p for p in self.positions if p.ticker == ticker and p.count > 0)

    def holds(self, ticker: str, strategy: str | None = None) -> bool:
        return self.position(ticker, strategy) is not None

    def orders_for(self, ticker: str | None = None, strategy: str | None = None) -> tuple[Order, ...]:
        return tuple(
            o for o in self.open_orders
            if (ticker is None or o.ticker == ticker) and (strategy is None or o.strategy == strategy)
        )

    def has_open_order(self, ticker: str, strategy: str | None = None) -> bool:
        return bool(self.orders_for(ticker, strategy))

    def exposure(self, *, ticker: str | None = None, event_ticker: str | None = None,
                 strategy: str | None = None) -> Decimal:
        return portfolio_exposure(self.positions, self.open_orders, ticker=ticker,
                                  event_ticker=event_ticker, strategy=strategy)

    @property
    def total_exposure(self) -> Decimal:
        return self.exposure()


def portfolio_exposure(positions: Iterable[Any], orders: Iterable[Any], *, ticker: str | None = None,
                       event_ticker: str | None = None, strategy: str | None = None) -> Decimal:
    """Cost basis of open positions + reserved cash of open orders, filtered (duck-typed)."""

    def ok(x: Any) -> bool:
        return ((ticker is None or getattr(x, "ticker", None) == ticker)
                and (event_ticker is None or getattr(x, "event_ticker", None) == event_ticker)
                and (strategy is None or getattr(x, "strategy", None) == strategy))

    total = ZERO
    for p in positions:
        if getattr(p, "count", 0) > 0 and ok(p):
            total += D(getattr(p, "cost_basis", ZERO))
    for o in orders:
        if ok(o):
            total += D(getattr(o, "reserved", ZERO) or ZERO)
    return total

