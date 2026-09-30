"""Rebalance planner (docs/COINBASE_CONTRACT.md §9): target weights -> spot order intents.

PAPER TRADING ONLY. ``plan_rebalance`` is a pure, deterministic function used identically by
the engine and the backtester: same inputs -> the same intents in the same order.

Inputs
    ``targets``      the strategy's :class:`~kalshibot.coinbase.strategies.base.TargetWeight`
                     list (or ``{product_id: weight | TargetWeight}``), validated with
                     :func:`~kalshibot.coinbase.strategies.base.normalize_targets` against
                     ``products`` (weights clamped to [0, 1], sum scaled to <= 1);
    ``holdings``     ``{product_id: base quantity}`` held by this strategy;
    ``prices``       ``{product_id: USD per unit}`` used to value holdings and size sells
                     (the engine passes mids, the backtester the last close);
    ``alloc_equity`` the strategy's allocation equity (USD) the weights are fractions of;
    ``products``     ``{product_id: Product}`` (increments, ``min_market_funds``, ``tradable``).

Rules (per product in ``targets`` or ``holdings``; products not in ``targets`` go to 0)
    * ``target_value = weight x alloc_equity``, ``current = quantity x price``,
      ``delta = target_value - current``. A target below the minimum trade
      ``min_usd = max(min_trade_usd, product.min_market_funds)`` counts as 0 (never leave or
      build dust).
    * **Band**: a *resize* (current and target both >= ``min_usd``) is skipped when
      ``|delta| < band x alloc_equity``. Entries (current below ``min_usd``) and full exits
      (target 0) are not banded - they are decisions, not drift - but still need
      ``>= min_usd``.
    * Every trade must be ``>= min_usd`` (sells: ``base_size x price``; buys: ``quote_size``).
    * **Sells** (market, by ``base_size``): ``-delta / price`` (the whole quantity on a full
      exit), capped at the held quantity (no shorting) and rounded **down** to
      ``base_increment``.
    * **Buys** (market, by ``quote_size`` = USD to spend including the fee): ``delta`` rounded
      down to the cent. With ``cash`` given, the buys are scaled down pro rata to fit
      ``cash + sum(sell notional) x (1 - fee_rate)`` (the estimated proceeds).
    * Products without a positive price or missing from ``products`` are skipped, as are
      trades in products that are not ``tradable``.
    * Order: all sells first, then all buys; each group sorted by product id.

:func:`plan_rebalance_detailed` returns the same intents plus what was skipped and why, and
the current / target weights (for the signals feed and the UI). :func:`plan_from_view` derives
every input from the strategy's :class:`~kalshibot.coinbase.paper.SpotPortfolioView` - the
engine and the backtester both call it, so they plan identically.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from kalshibot.coinbase.models import Product
from kalshibot.coinbase.paper import SpotOrderIntent, SpotPortfolioView
from kalshibot.coinbase.strategies.base import TargetWeight, normalize_targets

__all__ = [
    "CENT",
    "RebalancePlan",
    "floor_to",
    "plan_from_view",
    "plan_rebalance",
    "plan_rebalance_detailed",
    "strategy_cash",
]

ZERO = Decimal(0)
CENT = Decimal("0.01")


def _dec(x: Any) -> Decimal | None:
    """Decimal from Decimal/int/float/str (floats via their shortest repr); non-finite -> None."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = x if isinstance(x, Decimal) else Decimal(repr(x)) if isinstance(x, float) else Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return v if v.is_finite() else None


def floor_to(x: Decimal, increment: Decimal) -> Decimal:
    """Round ``x >= 0`` down to a multiple of ``increment`` (``increment <= 0``: unchanged)."""
    if increment is None or increment <= 0:
        return x
    if x <= 0:
        return ZERO
    return (x // increment) * increment


def _pct(w: float) -> str:
    return f"{w * 100:.1f}%"


@dataclass
class RebalancePlan:
    """What :func:`plan_rebalance_detailed` decided."""

    intents: list[SpotOrderIntent] = field(default_factory=list)
    #: ``{product_id, side, reason, delta_usd}`` for every trade that was not planned
    skipped: list[dict[str, Any]] = field(default_factory=list)
    #: current weight of each involved product (fraction of ``alloc_equity``)
    current_weights: dict[str, float] = field(default_factory=dict)
    #: effective target weight (after validation and the dust rule) of each involved product
    target_weights: dict[str, float] = field(default_factory=dict)
    #: corrections made to the strategy's targets (:func:`normalize_targets`)
    problems: list[str] = field(default_factory=list)

    @property
    def sells(self) -> list[SpotOrderIntent]:
        return [i for i in self.intents if i.side == "sell"]

    @property
    def buys(self) -> list[SpotOrderIntent]:
        return [i for i in self.intents if i.side == "buy"]


def plan_rebalance(
    targets: Iterable[TargetWeight] | Mapping[str, Any] | None,
    holdings: Mapping[str, Any],
    prices: Mapping[str, Any],
    alloc_equity: Any,
    products: Mapping[str, Product],
    *,
    band: float,
    min_trade_usd: Any,
    strategy: str,
    cash: Any = None,
    fee_rate: Any = 0,
) -> list[SpotOrderIntent]:
    """Order intents that move ``holdings`` to ``targets`` (see the module doc).

    ``targets=None`` means "no change" and returns ``[]``; ``[]`` sells everything held.
    """
    return plan_rebalance_detailed(targets, holdings, prices, alloc_equity, products, band=band,
                                   min_trade_usd=min_trade_usd, strategy=strategy, cash=cash,
                                   fee_rate=fee_rate).intents


def plan_rebalance_detailed(
    targets: Iterable[TargetWeight] | Mapping[str, Any] | None,
    holdings: Mapping[str, Any],
    prices: Mapping[str, Any],
    alloc_equity: Any,
    products: Mapping[str, Product],
    *,
    band: float,
    min_trade_usd: Any,
    strategy: str,
    cash: Any = None,
    fee_rate: Any = 0,
) -> RebalancePlan:
    """:func:`plan_rebalance` with the reasons for everything it did not plan."""
    plan = RebalancePlan()
    if targets is None:
        return plan
    tws, plan.problems = normalize_targets(targets, products)
    by_pid: dict[str, TargetWeight] = {t.product_id: t for t in tws or ()}
    alloc = _dec(alloc_equity) or ZERO
    if alloc < 0:
        alloc = ZERO
    band_f = float(band) if band is not None and math.isfinite(float(band)) else 0.0
    band_usd = Decimal(repr(max(0.0, band_f))) * alloc
    min_trade = max(ZERO, _dec(min_trade_usd) or ZERO)
    fr = min(max(_dec(fee_rate) or ZERO, ZERO), Decimal("0.5"))

    held: dict[str, Decimal] = {}
    for pid, q in holdings.items():
        qd = _dec(q)
        if qd is not None and qd > 0:
            held[str(pid).upper()] = qd

    sells: list[SpotOrderIntent] = []
    sell_notional = ZERO
    buys: list[tuple[SpotOrderIntent, Decimal, Product]] = []

    def skip(pid: str, side: str, why: str, delta: Decimal | None = None) -> None:
        plan.skipped.append({"product_id": pid, "side": side, "reason": why,
                             "delta_usd": float(delta) if delta is not None else None})

    for pid in sorted(set(by_pid) | set(held)):
        tw = by_pid.get(pid)
        w = float(tw.weight) if tw is not None else 0.0
        qty = held.get(pid, ZERO)
        price = _dec(prices.get(pid))
        prod = products.get(pid)
        if price is None or price <= 0:
            plan.target_weights[pid] = w
            if qty > 0 or w > 0:
                skip(pid, "sell" if w == 0 else "buy", "no price")
            continue
        cur_val = qty * price
        cur_w = float(cur_val / alloc) if alloc > 0 else 0.0
        plan.current_weights[pid] = cur_w
        if prod is None:
            plan.target_weights[pid] = w
            skip(pid, "sell" if w * float(alloc) < float(cur_val) else "buy", "unknown product")
            continue
        min_usd = max(min_trade, prod.min_market_funds or ZERO)
        tgt_val = Decimal(repr(w)) * alloc
        if tgt_val < min_usd:  # dust target -> flat
            tgt_val = ZERO
        tgt_w = float(tgt_val / alloc) if alloc > 0 else 0.0
        plan.target_weights[pid] = tgt_w
        delta = tgt_val - cur_val
        label = tw.reason if tw is not None and tw.reason else ""
        if delta < 0:  # ---------------------------------------------------------- sell
            full_exit = tgt_val == 0
            if not full_exit and -delta < band_usd:
                skip(pid, "sell", f"inside the {band_f:.1%} rebalance band", delta)
                continue
            base = qty if full_exit else min(qty, -delta / price)
            base = min(qty, floor_to(base, prod.base_increment))
            if base <= 0:
                skip(pid, "sell", "below the base increment", delta)
                continue
            notional = base * price
            if notional < min_usd:
                skip(pid, "sell", f"below the minimum trade (${min_usd})", delta)
                continue
            if not prod.tradable:
                skip(pid, "sell", f"product not tradable ({prod.status})", delta)
                continue
            if tw is None:
                why = f"exit: not in targets ({_pct(cur_w)} -> 0%)"
            else:
                why = f"{label + '; ' if label else ''}rebalance {_pct(cur_w)} -> {_pct(tgt_w)}"
            sell_notional += notional
            sells.append(SpotOrderIntent(
                product_id=pid, side="sell", base_size=base, order_type="market", tif="ioc",
                strategy=strategy, reason=why, target_weight=tgt_w,
                expected_edge_bps=tw.expected_edge_bps if tw is not None else None))
        elif delta > 0:  # -------------------------------------------------------- buy
            entry = cur_val < min_usd
            if not entry and delta < band_usd:
                skip(pid, "buy", f"inside the {band_f:.1%} rebalance band", delta)
                continue
            quote = floor_to(delta, CENT)
            if quote < min_usd:
                skip(pid, "buy", f"below the minimum trade (${min_usd})", delta)
                continue
            if not prod.tradable:
                skip(pid, "buy", f"product not tradable ({prod.status})", delta)
                continue
            why = f"{label + '; ' if label else ''}{'enter' if entry else 'rebalance'} {_pct(cur_w)} -> {_pct(tgt_w)}"
            buys.append((SpotOrderIntent(
                product_id=pid, side="buy", quote_size=quote, order_type="market", tif="ioc",
                strategy=strategy, reason=why, target_weight=tgt_w,
                expected_edge_bps=tw.expected_edge_bps if tw is not None else None), min_usd, prod))

    plan.intents.extend(sells)
    cash_d = _dec(cash)
    if cash_d is not None and buys:
        budget = max(ZERO, cash_d) + sell_notional * (1 - fr)
        want = sum((b.quote_size or ZERO for b, _, _ in buys), ZERO)
        if want > budget:
            scale = budget / want
            kept: list[tuple[SpotOrderIntent, Decimal, Product]] = []
            for b, min_usd, prod in buys:
                q = floor_to((b.quote_size or ZERO) * scale, CENT)
                if q < min_usd:
                    skip(b.product_id, "buy", "not enough cash", q)
                    continue
                b.quote_size = q
                b.reason += f" (scaled to cash: ${q})"
                kept.append((b, min_usd, prod))
            buys = kept
    plan.intents.extend(b for b, _, _ in buys)
    return plan


def strategy_cash(view: SpotPortfolioView) -> Decimal:
    """USD this strategy may spend: the shared cash pool, capped by what is left of its
    allocation (``alloc_equity`` - its holdings' value - cash its resting buys reserve)."""
    room = view.alloc_equity - view.strategy_value - view.strategy_reserved
    return max(ZERO, min(view.cash, room))


def plan_from_view(
    targets: Iterable[TargetWeight] | Mapping[str, Any] | None,
    view: SpotPortfolioView,
    products: Mapping[str, Product],
    *,
    band: float,
    min_trade_usd: Any,
    strategy: str,
    fee_rate: Any = 0,
    prices: Mapping[str, Any] | None = None,
) -> RebalancePlan:
    """:func:`plan_rebalance_detailed` with its inputs taken from ``view`` (this strategy's
    portfolio): ``holdings = view.holdings``, ``prices = view.mids`` (unless given),
    ``alloc_equity = view.alloc_equity`` and ``cash =`` :func:`strategy_cash`."""
    return plan_rebalance_detailed(
        targets, view.holdings, view.mids if prices is None else prices, view.alloc_equity, products,
        band=band, min_trade_usd=min_trade_usd, strategy=strategy, cash=strategy_cash(view), fee_rate=fee_rate)
