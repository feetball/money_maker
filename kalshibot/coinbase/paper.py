"""Spot paper-ledger models for the Coinbase venue (docs/COINBASE_CONTRACT.md §6) - PAPER ONLY.

All money, prices and quantities are ``Decimal``: prices in USD per 1 unit of base,
quantities in base units (``0.01234567`` BTC). JSON views (``to_json``) use floats with up
to 8 dp and ISO-8601 UTC timestamps ending in ``Z``; every top-level object carries
``"venue": "coinbase"``.

Accounting conventions (the broker enforces them; tests check the identities):

* ``SpotPosition.cost_basis`` is the USD paid for the *held* quantity **including buy
  fees**, so ``avg_cost = cost_basis / quantity`` is fee-inclusive.
* A sell of ``q`` realizes ``proceeds - sell_fee - cost_basis x q / quantity`` (the whole
  cost basis when the position is closed), so ``account.realized_pnl == sum(sell realized)``.
* ``liquidation_value`` is **net of the exit (taker) fee**: what selling the quantity into the
  bid ladder now would add to cash. ``unrealized_pnl = liquidation_value - cost_basis`` (both
  fees included, so an open position shows what closing it would realize); ``exit_fee`` is
  reported separately (gross ladder proceeds = ``liquidation_value + exit_fee``).
* Equity identity: ``cash + reserved_cash + positions_liquidation_value ==
  starting_balance + realized_pnl + unrealized_pnl``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Literal

from kalshibot.coinbase.models import SpotSide
from kalshibot.money import ZERO, D

__all__ = [
    "OPEN_STATUSES",
    "VENUE",
    "SpotAccountState",
    "SpotFill",
    "SpotOrder",
    "SpotOrderIntent",
    "SpotOrderStatus",
    "SpotPortfolioView",
    "SpotPosition",
    "f8",
    "iso",
    "parse_iso",
    "product_url",
    "q8",
    "spot_exposure",
]

VENUE = "coinbase"

SpotOrderType = Literal["market", "limit"]
SpotTif = Literal["ioc", "gtc"]
SpotOrderStatus = Literal["open", "partially_filled", "filled", "cancelled", "expired", "rejected"]

#: Statuses of a live (resting GTC) order.
OPEN_STATUSES = frozenset({"open", "partially_filled"})

Q8 = Decimal("0.00000001")


def q8(x: Decimal) -> Decimal:
    return x.quantize(Q8, rounding=ROUND_HALF_EVEN)


def f8(x: Decimal | float | int | None) -> float | None:
    """JSON number with up to 8 dp (``None`` stays ``None``)."""
    if x is None:
        return None
    return float(round(D(x), 8))


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


def product_url(product_id: str) -> str:
    return f"https://www.coinbase.com/advanced-trade/spot/{product_id}"


# --------------------------------------------------------------------------- intent


@dataclass
class SpotOrderIntent:
    """What a strategy / the rebalance planner asks the broker for (contract §6).

    * market buy: ``quote_size`` = USD to spend **including the fee**.
    * market sell: ``base_size``.
    * limit buy: ``quote_size`` (fee included) or ``base_size``; limit sell: ``base_size``.
    * ``tif="gtc"`` rests (limit only); ``post_only`` GTC orders are rejected if they would cross.
    """

    product_id: str
    side: Literal["buy", "sell"]
    quote_size: Decimal | None = None
    base_size: Decimal | None = None
    order_type: Literal["market", "limit"] = "market"
    limit_price: Decimal | None = None
    tif: Literal["ioc", "gtc"] = "ioc"
    post_only: bool = False
    expires_in_s: int | None = None
    strategy: str = ""
    reason: str = ""
    target_weight: float | None = None
    expected_edge_bps: float | None = None


# --------------------------------------------------------------------------- order


@dataclass(slots=True)
class SpotOrder:
    id: int
    product_id: str
    side: SpotSide
    order_type: SpotOrderType = "market"
    tif: SpotTif = "ioc"
    post_only: bool = False
    quote_size: Decimal | None = None  # buys by funds: USD incl. fee (as requested, rounded)
    base_size: Decimal | None = None  # quantity (for a GTC buy by funds: the resting size)
    limit_price: Decimal | None = None
    status: SpotOrderStatus = "open"
    status_reason: str = ""  # why rejected / cancelled / expired (``reason`` is the strategy's text)
    filled_base: Decimal = ZERO
    filled_quote: Decimal = ZERO  # notional of the fills (price x size), excl. fees
    fees: Decimal = ZERO
    strategy: str = ""
    reason: str = ""
    target_weight: float | None = None
    expected_edge_bps: float | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    #: resting: displayed size ahead of us at our price (real orders, not ours)
    queue_ahead: Decimal | None = None
    #: USD held for a resting buy's remainder (principal + maker fee), released on fill/cancel/expiry
    reserved: Decimal = ZERO
    #: sells: realized P&L of this order's fills
    realized_pnl: Decimal = ZERO
    # per-order fee accumulators: fee charged so far == fee_for(cumulative notional) per liquidity type
    taker_notional: Decimal = ZERO
    maker_notional: Decimal = ZERO
    taker_fees: Decimal = ZERO
    maker_fees: Decimal = ZERO
    #: the fee tier at placement (a resting order keeps its rates across restarts / config changes)
    fee_tier: str = ""
    maker_rate: Decimal = ZERO
    taker_rate: Decimal = ZERO

    # -- derived -------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def remaining_base(self) -> Decimal:
        """Unfilled quantity of a base-sized order (0 for a funds-sized IOC buy)."""
        if self.base_size is None:
            return ZERO
        return max(ZERO, self.base_size - self.filled_base)

    @property
    def avg_fill_price(self) -> Decimal | None:
        return self.filled_quote / self.filled_base if self.filled_base > 0 else None

    @property
    def decision(self) -> str:
        """Signal-feed decision: ``executed`` | ``partial`` | ``rejected`` | ``unfilled`` | ``resting``."""
        if self.status == "rejected":
            return "rejected"
        if self.is_open:
            return "resting"
        if self.status == "filled":
            return "executed"
        return "partial" if self.filled_base > 0 else "unfilled"

    def to_json(self) -> dict[str, Any]:
        return {
            "venue": VENUE,
            "id": self.id,
            "product_id": self.product_id,
            "side": self.side,
            "order_type": self.order_type,
            "tif": self.tif,
            "post_only": self.post_only,
            "quote_size": f8(self.quote_size),
            "base_size": f8(self.base_size),
            "limit_price": f8(self.limit_price),
            "filled_base": f8(self.filled_base),
            "filled_quote": f8(self.filled_quote),
            "avg_fill_price": f8(self.avg_fill_price),
            "fees": f8(self.fees),
            "status": self.status,
            "status_reason": self.status_reason,
            "strategy": self.strategy,
            "reason": self.reason,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
            "expires_at": iso(self.expires_at),
            # extras
            "remaining_base": f8(self.remaining_base) if self.is_open else 0.0,
            "queue_ahead": f8(self.queue_ahead),
            "reserved": f8(self.reserved),
            "realized_pnl": f8(self.realized_pnl) if self.side == "sell" else None,
            "target_weight": self.target_weight,
            "expected_edge_bps": self.expected_edge_bps,
            "decision": self.decision,
        }


# --------------------------------------------------------------------------- fill


@dataclass(frozen=True, slots=True)
class SpotFill:
    id: int
    order_id: int
    product_id: str
    side: SpotSide
    base_size: Decimal
    price: Decimal
    notional: Decimal  # price x base_size (USD, excl. fee)
    fee: Decimal  # USD charged for this fill (per-order cumulative cent rounding)
    fee_rate: Decimal
    is_taker: bool
    ts: datetime
    strategy: str = ""
    realized_pnl: Decimal = ZERO  # sells: proceeds - fee - cost of the quantity sold
    trade_id: int | None = None  # the public trade that filled a resting order (maker fills)

    @property
    def cash_delta(self) -> Decimal:
        """Change of the account's USD from this fill (buys: -(notional + fee))."""
        return self.notional - self.fee if self.side == "sell" else -(self.notional + self.fee)

    def to_json(self) -> dict[str, Any]:
        return {
            "venue": VENUE,
            "id": self.id,
            "order_id": self.order_id,
            "product_id": self.product_id,
            "side": self.side,
            "base_size": f8(self.base_size),
            "price": f8(self.price),
            "notional": f8(self.notional),
            "fee": f8(self.fee),
            "fee_rate": f8(self.fee_rate),
            "is_taker": self.is_taker,
            "ts": iso(self.ts),
            "strategy": self.strategy,
            "realized_pnl": f8(self.realized_pnl) if self.side == "sell" else None,
            "trade_id": self.trade_id,
        }


# --------------------------------------------------------------------------- position


@dataclass(slots=True)
class SpotPosition:
    """One strategy's holding of one product (spot: long only)."""

    product_id: str
    strategy: str = ""
    base_currency: str = ""
    quantity: Decimal = ZERO
    cost_basis: Decimal = ZERO  # USD paid for the held quantity, buy fees included
    realized_pnl: Decimal = ZERO  # cumulative, from sells (fees included)
    fees_paid: Decimal = ZERO  # cumulative buy + sell fees on this (strategy, product)
    opened_at: datetime | None = None
    updated_at: datetime | None = None
    # ---- marks: filled in by the broker's views, never persisted ----
    liquidation_value: Decimal | None = None  # selling the quantity into the bid ladder now, net of the fee
    exit_fee: Decimal | None = None  # the taker fee that sale would pay (included in liquidation_value)
    mid_value: Decimal | None = None
    best_bid: Decimal | None = None
    mid_price: Decimal | None = None
    mark_ts: datetime | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.strategy, self.product_id)

    @property
    def is_open(self) -> bool:
        return self.quantity > 0

    @property
    def avg_cost(self) -> Decimal | None:
        """USD per unit including buy fees."""
        return self.cost_basis / self.quantity if self.quantity > 0 else None

    @property
    def value(self) -> Decimal:
        """Liquidation value, or the cost basis while the product has never been marked."""
        return self.liquidation_value if self.liquidation_value is not None else self.cost_basis

    @property
    def mark_price(self) -> Decimal | None:
        """Average exit price per unit (before the fee) when selling the whole quantity into the bids."""
        if self.liquidation_value is None or self.quantity <= 0:
            return None
        return (self.liquidation_value + (self.exit_fee or ZERO)) / self.quantity

    @property
    def unrealized_pnl(self) -> Decimal:
        return self.value - self.cost_basis

    @property
    def unrealized_pnl_pct(self) -> Decimal | None:
        return self.unrealized_pnl / self.cost_basis * 100 if self.cost_basis > 0 else None

    def to_json(self, *, weight_of_strategy: Decimal | float | None = None) -> dict[str, Any]:
        return {
            "venue": VENUE,
            "product_id": self.product_id,
            "base_currency": self.base_currency,
            "strategy": self.strategy,
            "quantity": f8(self.quantity),
            "avg_cost": f8(self.avg_cost),
            "cost_basis": f8(self.cost_basis),
            "mark_price": f8(self.mark_price),
            "best_bid": f8(self.best_bid),
            "mid_price": f8(self.mid_price),
            "liquidation_value": f8(self.liquidation_value),
            "liquidation_value_gross": (f8(self.liquidation_value + (self.exit_fee or ZERO))
                                        if self.liquidation_value is not None else None),
            "exit_fee": f8(self.exit_fee),
            "mid_value": f8(self.mid_value),
            "unrealized_pnl": f8(self.unrealized_pnl) if self.liquidation_value is not None else None,
            "unrealized_pnl_pct": (f8(self.unrealized_pnl_pct)
                                   if self.liquidation_value is not None and self.unrealized_pnl_pct is not None
                                   else None),
            "realized_pnl": f8(self.realized_pnl),
            "fees_paid": f8(self.fees_paid),
            "weight_of_strategy": f8(weight_of_strategy) if weight_of_strategy is not None else None,
            "opened_at": iso(self.opened_at),
            "mark_ts": iso(self.mark_ts),
            "url": product_url(self.product_id),
        }


# --------------------------------------------------------------------------- account


@dataclass(frozen=True, slots=True)
class SpotAccountState:
    """The Coinbase paper account (``GET /api/coinbase/account``)."""

    ts: datetime
    starting_balance: Decimal
    cash: Decimal  # free USD (reservations excluded)
    reserved_cash: Decimal
    positions_liquidation_value: Decimal
    positions_mid_value: Decimal
    positions_cost_basis: Decimal
    equity: Decimal  # cash + reserved + liquidation value
    equity_mid: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    unrealized_pnl_mid: Decimal
    fees_paid: Decimal
    total_pnl: Decimal
    total_return_pct: Decimal
    todays_pnl: Decimal
    day_start_equity: Decimal | None
    max_drawdown_pct: Decimal
    open_positions: int
    open_orders: int
    trades: int  # closed trades: sell orders that realized P&L
    wins: int  # ... of which realized P&L > 0
    fills: int = 0
    #: exit fees already deducted from ``positions_liquidation_value`` (and so from equity)
    positions_exit_fee: Decimal = ZERO

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.trades if self.trades else None

    def to_json(self) -> dict[str, Any]:
        return {
            "venue": VENUE,
            "starting_balance": f8(self.starting_balance),
            "cash": f8(self.cash),
            "reserved_cash": f8(self.reserved_cash),
            "positions_liquidation_value": f8(self.positions_liquidation_value),
            "positions_mid_value": f8(self.positions_mid_value),
            "equity": f8(self.equity),
            "equity_mid": f8(self.equity_mid),
            "realized_pnl": f8(self.realized_pnl),
            "unrealized_pnl": f8(self.unrealized_pnl),
            "fees_paid": f8(self.fees_paid),
            "total_pnl": f8(self.total_pnl),
            "total_return_pct": f8(self.total_return_pct),
            "todays_pnl": f8(self.todays_pnl),
            "max_drawdown_pct": f8(self.max_drawdown_pct),
            "open_positions": self.open_positions,
            "open_orders": self.open_orders,
            "trades": self.trades,
            "win_rate": self.win_rate,
            # extras
            "positions_cost_basis": f8(self.positions_cost_basis),
            "positions_exit_fee": f8(self.positions_exit_fee),
            "wins": self.wins,
            "fills": self.fills,
            "ts": iso(self.ts),
        }


# --------------------------------------------------------------------------- portfolio view


def spot_exposure(positions: Any, orders: Any, *, product_id: str | None = None,
                  strategy: str | None = None) -> Decimal:
    """Held value (liquidation value; cost basis when unmarked) + USD reserved by open buys."""
    total = ZERO
    for p in positions:
        if (p.quantity > 0 and (product_id is None or p.product_id == product_id)
                and (strategy is None or p.strategy == strategy)):
            total += p.value
    for o in orders:
        if (o.side == "buy" and (product_id is None or o.product_id == product_id)
                and (strategy is None or o.strategy == strategy)):
            total += o.reserved
    return total


@dataclass(frozen=True, slots=True)
class SpotPortfolioView:
    """Read-only snapshot for a strategy (``ctx.portfolio``) and :class:`SpotRiskManager`.

    ``positions`` / ``open_orders`` are the strategy's own (every strategy's when
    ``strategy`` is None); ``all_positions`` / ``all_open_orders`` always hold the whole
    account (the risk manager's per-product and total limits need them). Cash is one shared
    USD pool. ``alloc_equity`` = ``allocation_pct`` % of the account equity (100 % when the
    caller gave no allocation) - what the strategy's target weights are fractions of.
    """

    ts: datetime
    strategy: str | None
    starting_balance: Decimal
    cash: Decimal
    reserved_cash: Decimal
    equity: Decimal
    equity_mid: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees_paid: Decimal
    day_start_equity: Decimal | None
    allocation_pct: float | None
    alloc_equity: Decimal
    positions: tuple[SpotPosition, ...] = ()
    open_orders: tuple[SpotOrder, ...] = ()
    all_positions: tuple[SpotPosition, ...] = ()
    all_open_orders: tuple[SpotOrder, ...] = ()
    #: latest mark per product: best bid / best ask / mid (products seen in a book so far)
    best_bids: Mapping[str, Decimal] = field(default_factory=dict)
    best_asks: Mapping[str, Decimal] = field(default_factory=dict)
    mids: Mapping[str, Decimal] = field(default_factory=dict)

    @property
    def daily_pnl(self) -> Decimal:
        return self.equity - self.day_start_equity if self.day_start_equity is not None else ZERO

    @property
    def holdings(self) -> dict[str, Decimal]:
        """``{product_id: quantity}`` of this view's positions."""
        out: dict[str, Decimal] = {}
        for p in self.positions:
            if p.quantity > 0:
                out[p.product_id] = out.get(p.product_id, ZERO) + p.quantity
        return out

    @property
    def values(self) -> dict[str, Decimal]:
        """``{product_id: liquidation value}`` of this view's positions."""
        out: dict[str, Decimal] = {}
        for p in self.positions:
            if p.quantity > 0:
                out[p.product_id] = out.get(p.product_id, ZERO) + p.value
        return out

    @property
    def strategy_value(self) -> Decimal:
        return sum(self.values.values(), ZERO)

    @property
    def strategy_reserved(self) -> Decimal:
        return sum((o.reserved for o in self.open_orders if o.side == "buy"), ZERO)

    @property
    def strategy_cost_basis(self) -> Decimal:
        return sum((p.cost_basis for p in self.positions if p.quantity > 0), ZERO)

    def position(self, product_id: str, strategy: str | None = None) -> SpotPosition | None:
        strat = self.strategy if strategy is None else strategy
        for p in self.all_positions or self.positions:
            if p.product_id == product_id and p.quantity > 0 and (strat is None or p.strategy == strat):
                return p
        return None

    def quantity(self, product_id: str) -> Decimal:
        return self.holdings.get(product_id, ZERO)

    def orders_for(self, product_id: str | None = None, *, side: str | None = None) -> tuple[SpotOrder, ...]:
        return tuple(o for o in self.open_orders
                     if (product_id is None or o.product_id == product_id) and (side is None or o.side == side))

    def exposure(self, *, product_id: str | None = None, strategy: str | None = None) -> Decimal:
        """Account-wide exposure (all strategies unless ``strategy``): value held + reserved buys."""
        return spot_exposure(self.all_positions or self.positions, self.all_open_orders or self.open_orders,
                             product_id=product_id, strategy=strategy)

    @property
    def total_exposure(self) -> Decimal:
        return self.exposure()

    def price(self, product_id: str) -> Decimal | None:
        """Mid if known, else the best bid."""
        return self.mids.get(product_id) or self.best_bids.get(product_id)
