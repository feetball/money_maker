"""Mutually-exclusive NO-basket arbitrage. Risk-free when it fires, and it almost never fires.

Evidence is in ``research/arbitrage/REPORT.md`` (``scanner.py``: ``walk_no_basket``;
``analyze.py``) and ``research/FINDINGS.md``. In one full snapshot (4,986 mutually-exclusive
events), no NO basket was positive after fees. The best was -$0.0003 per unit basket, the p99
was -$0.011 and the p90 was -$0.035. This strategy is cheap to run and waits for the rare
moment the sum of NO asks dips far enough.

Rule
----
Take an event with ``mutually_exclusive = true`` and pick a subset S (|S| >= 2) of its active
legs. A *unit basket* is one NO contract on every leg of S. Buy U unit baskets at once, all or
none, when the **guaranteed** profit per unit basket, after taker fees and rounding, is at
least ``min_profit_per_basket``::

    (|S| - 1) - sum_i L_i - sum_i fee_i(U @ L_i) / U - |S| * $0.01 / U  >=  min_profit_per_basket

``L_i`` is leg i's limit: the deepest NO-ask level the depth walk needs for U contracts.
``fee_i`` is Kalshi's taker fee for the leg's order, using the leg's own fee parameters
(series fee type and multiplier, the event's override, scheduled fee changes; all through
``ctx.fee``) and rounded up. The extra $0.01 per leg is Kalshi's per-order balance rounding.

This is a worst case. Every contract is costed at its leg's limit, as if the book moved up to
the limit before the fill. That is still an upper bound on the real cost: the all-in cost of a
contract, ``p + k*p*(1-p)``, rises with ``p`` for any fee coefficient ``k < 1``, so a fill at a
better price never costs more. With S = every leg and one price level, the rule reduces to the
research rule ``(N - 1) - sum NO_ask - sum fee >= threshold``.

Why a NO basket pays at least |S| - 1, with no exhaustiveness needed
----------------------------------------------------------------------
*Mutually exclusive* means at most one of the event's markets resolves YES. So at most one leg
of S resolves YES, and every other leg's NO pays $1. Each unit basket pays |S| - 1, or |S| when
the winner is outside S or nobody wins. The listed outcomes do not need to cover every
possibility. That requirement belongs to the YES basket, which pays only if a listed outcome
wins. The research's "profitable" YES baskets were almost all non-exhaustive traps
(KXTOPMODEL, KXBILLSCOUNT, the Grammys), so this strategy never trades YES baskets.

The traps, reasoned through
---------------------------
* **Missing legs do not break the bound.** The bound holds for *any* subset S of a
  mutually-exclusive event, so legs outside the universe, inactive, one-sided, too wide, too
  thin or beyond ``max_legs`` are simply left out. Leaving out leg i gives up its contribution
  ``1 - NO_ask_i - fee_i`` (which is > 0), so it lowers the edge but never the payout floor.
  S is chosen as the best prefix of the legs ranked by top-of-book contribution. Dropping a
  thin longshot often allows a much larger U. Set ``allow_partial_baskets = false`` to require
  every active leg of the event to be in the basket.
* **A leg that closes or settles on its own does not break the bound either.** The bound is a
  property of the event, not of timing. If a leg settles NO early, we collect $1 early. If it
  settles YES, every other leg must settle NO. Events whose active legs have **different close
  times** are still skipped by default (``allow_mixed_close_times``). The research flagged 370
  such events as "date bucket" structures with early-close and timing traps. Capital stays
  locked until the last leg settles. And a leg crossing ``risk.min_seconds_to_close`` before
  the others would get the whole basket rejected.
* **A leg already resolved YES**: the event is skipped. Every other leg must then resolve NO, so
  a NO ask below $1 there means stale data, not an arbitrage.
* **Multivariate (MVE) legs**: the universe excludes them. An event with any ``KXMVE*`` or
  ``mve_*`` leg is skipped.
* **Void or cancelled events (residual risk, not riskless).** Kalshi settles each market as
  ``scalar`` at a "fair price" v_i. NO then pays ``1 - v_i``, so the basket pays
  ``|S| - sum v_i``. That is at least |S| - 1 only if the legs' fair prices sum to at most 1,
  and no documented rule guarantees it. 0.7% of finalized markets settled ``scalar``, mostly
  cancelled sports props. The same goes for rule errors and disputed outcomes.
* **Fees, depth and staleness.** Sizes come from **fresh** order books, using whole contracts
  per level exactly as the broker walks them. The universe snapshot (up to ~2 minutes old) is
  only a prescreen that decides which books to fetch; depth can only make a basket worse than
  the top of the book. The broker then re-reads every market and book itself and fills every
  leg completely at or below its limit, or none of them.
* **Risk limits.** The engine rejects the whole basket if the risk manager trims any leg. So
  the basket is sized to ``max_basket_cost``, ``max_leg_cost`` and ``cash_reserve``, net of
  existing exposure. Keep those below ``risk.max_exposure_per_event``,
  ``risk.max_position_cost_per_market`` and ``risk.min_cash_reserve``. Legs whose book spread
  is above ``max_leg_spread`` (``risk.max_spread``) or that close within
  ``min_minutes_to_close`` are left out. At most ``max_legs`` legs are used, because
  ``risk.max_orders_per_minute`` counts every leg. At most 50 intents go out per tick (the
  engine's ``max_intents_per_tick``), so a basket is never split.

Routing (paper only)
--------------------
The strategy emits one ``OrderIntent`` per leg: buy NO, IOC, ``limit_price = L_i``,
``count = U``, and one ``group_id`` for the whole basket. From there:

1. ``Engine.execute_intents`` groups the intents by ``group_id`` and calls
   ``Engine._execute_basket``.
2. ``RiskManager.check_basket`` checks each leg against the portfolio plus the legs before it.
   If any leg is trimmed, the whole basket is rejected.
3. ``PaperBroker.place_basket(legs, all_or_none=True)`` fills every leg in full or rejects
   every leg.

``expected_edge`` on every leg is the guaranteed profit per contract (the basket's guaranteed
profit / (U x |S|)), so the legs' expected edges add up to the basket's floor. ``fair_value``
is None, because there is no model.

Request budget
--------------
* Mutually-exclusive flags come from ``ctx.events`` (no request) or a lazy ``ctx.event(...)``.
  At most ``max_event_fetches_per_tick`` lazy fetches happen per tick, and only for events that
  pass the prescreen. Flags are remembered while the event stays in the universe. Nested
  threshold ladders (two "above X" legs of one entity: never mutually exclusive) are not looked
  up at all. Live on 2026-09-27, they were 986 of the 1,029 snapshot candidates in a 14-day
  window, and a sample of 40 of them were all confirmed not mutually exclusive. Events of series
  that already have a known mutually-exclusive event are looked up first.
* Books are fetched in one batch (``ctx.orderbooks``, 100 tickers per request), at most
  ``max_books_per_tick`` legs per tick, best prescreen margin first.
* The strategy logs one compact summary per tick: events scanned, mutually-exclusive counts,
  best margins, and whether a basket fired. It does not log per event.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import TYPE_CHECKING, Any, ClassVar

from kalshibot.fees import fee_rate
from kalshibot.kalshi.client import KalshiNotFound
from kalshibot.money import CENT, ONE, ZERO, D
from kalshibot.strategies.base import OrderIntent, Strategy, StrategyContext, UniverseSpec

if TYPE_CHECKING:
    from kalshibot.kalshi.models import Event, Market, Orderbook

__all__ = ["BasketPlan", "Leg", "NoBasketArb", "best_plan", "is_mve", "nested_ladder", "no_ladder", "plan_basket",
           "top_margin"]

#: Allowance per leg for Kalshi's per-order balance rounding ($0.01 at the default FCM precision;
#: conservative for direct members' $0.0001).
ROUNDING_SLACK = CENT
#: Per-contract taker-fee coefficient the risk manager charges when sizing (``risk.TAKER_FEE_BOUND``).
RISK_FEE_BOUND = Decimal("0.07")
#: The engine drops intents beyond ``max_intents_per_tick`` (default 50); never split a basket.
ENGINE_MAX_INTENTS = 50
_HALF = Decimal("0.5")
_BIG = 1_000_000
_Q6 = Decimal("0.000001")
_UP = frozenset({"greater", "greater_or_equal"})
_DOWN = frozenset({"less", "less_or_equal"})
#: "exactly N" markets are sometimes mislabelled greater/less (research/arbitrage/scanner.py)
_EXACTLY = re.compile(r"\bexactly\b", re.I)

FeeFn = Callable[["Market", Decimal, int], Decimal]


def is_mve(m: Market) -> bool:
    """Multivariate combo leg (``KXMVE*`` or any non-empty ``mve_*`` field)."""
    if m.ticker.upper().startswith("KXMVE"):
        return True
    raw = m.raw or {}
    return any(str(k).startswith("mve_") and raw.get(k) for k in raw)


def nested_ladder(markets: Iterable[Market]) -> bool:
    """Two legs of one entity (``custom_strike``) are one-sided thresholds in the same direction at
    different strikes. "Above 10" and "above 20" can both resolve YES, so such an event cannot be
    mutually exclusive. Live, this is about 96% of the snapshot candidates (sports totals and spreads,
    price ladders). It is used only to skip the event lookup, never to trade."""
    seen: dict[tuple[str, str], set[Decimal]] = {}
    for m in markets:
        if m.strike_type in _UP:
            direction, strike = "up", m.floor_strike
        elif m.strike_type in _DOWN:
            direction, strike = "down", m.cap_strike
        else:
            continue
        if strike is None or _EXACTLY.search(m.rules_primary or ""):
            continue
        key = (direction, json.dumps(m.custom_strike, sort_keys=True, default=str))
        strikes = seen.setdefault(key, set())
        strikes.add(strike)
        if len(strikes) >= 2:
            return True
    return False


def no_ladder(book: Orderbook) -> tuple[tuple[Decimal, int], ...]:
    """NO asks best-first as ``(price, whole contracts)``: the broker takes ``int(size)`` per level."""
    out = []
    for lv in book.no_asks:
        n = int(lv.size)
        if n >= 1 and ZERO < lv.price < ONE:
            out.append((lv.price, n))
    return tuple(out)


def _ceil_cent(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_CEILING)


# --------------------------------------------------------------------------- basket math


@dataclass(frozen=True)
class Leg:
    """One NO leg: the market, its fresh NO-ask ladder and its taker-fee coefficient ``k``
    (fee = k x C x P x (1 - P))."""

    market: Market
    ladder: tuple[tuple[Decimal, int], ...]
    rate: Decimal

    @property
    def ticker(self) -> str:
        return self.market.ticker

    def unit_cost(self, p: Decimal) -> Decimal:
        """All-in cost of one contract at ``p`` before rounding (increasing in ``p`` for k < 1)."""
        return p + self.rate * p * (ONE - p)

    @property
    def contribution(self) -> Decimal:
        """``1 - NO_ask - fee`` at the top of the book: what the leg adds to a unit basket's edge."""
        return ONE - self.unit_cost(self.ladder[0][0]) if self.ladder else -ONE


@dataclass(frozen=True)
class BasketPlan:
    event_ticker: str
    legs: tuple[Leg, ...]
    units: int
    limits: tuple[Decimal, ...]
    leg_costs: tuple[Decimal, ...]  # worst-case debit per leg: U @ limit + fee + rounding slack
    guaranteed_profit: Decimal  # (|S| - 1) x U - sum(leg_costs)
    expected_profit: Decimal  # if the legs fill at the walked levels of the current books

    @property
    def payout_floor(self) -> Decimal:
        return D(len(self.legs) - 1) * self.units

    @property
    def cost_bound(self) -> Decimal:
        return sum(self.leg_costs, ZERO)

    @property
    def profit_per_basket(self) -> Decimal:
        return self.guaranteed_profit / self.units

    @property
    def edge_per_contract(self) -> Decimal:
        return (self.guaranteed_profit / (self.units * len(self.legs))).quantize(_Q6)


def _walk_cost(leg: Leg, units: int) -> Decimal:
    """Debit if ``units`` fill best-first on the current ladder (fees cumulative per order,
    rounded up to the cent like the broker's accumulator at FCM precision)."""
    left, cost, fee = units, ZERO, ZERO
    for p, n in leg.ladder:
        q = min(left, n)
        cost += p * q
        fee += leg.rate * q * p * (ONE - p)
        left -= q
        if left <= 0:
            break
    return cost + _ceil_cent(fee)


def _units_cap(legs: Sequence[Leg], limits: Sequence[Decimal], basket_room: Decimal | None,
               leg_rooms: Sequence[Decimal | None] | None, cash_room: Decimal | None) -> int | None:
    """Largest U whose cost (as the risk manager prices it: limit + 0.07 x P(1-P) per contract,
    plus the rounding slack) fits the dollar rooms. ``None`` = uncapped."""
    per = [lim + max(leg.rate, RISK_FEE_BOUND) * lim * (ONE - lim) for leg, lim in zip(legs, limits, strict=True)]
    total = sum(per, ZERO)
    caps: list[int] = []
    slack = ROUNDING_SLACK * len(legs)
    for room in (basket_room, cash_room):
        if room is not None:
            caps.append(max(0, math.floor((room - slack) / total)) if total > 0 else 0)
    for i, room in enumerate(leg_rooms or ()):
        if room is not None:
            caps.append(max(0, math.floor((room - ROUNDING_SLACK) / per[i])) if per[i] > 0 else 0)
    return min(caps) if caps else None


def _evaluate(event_ticker: str, legs: Sequence[Leg], limits: Sequence[Decimal], units: int,
              fee: FeeFn) -> BasketPlan:
    leg_costs = tuple(units * lim + D(fee(leg.market, lim, units)) + ROUNDING_SLACK
                      for leg, lim in zip(legs, limits, strict=True))
    payout = D(len(legs) - 1) * units
    expected = payout - sum((_walk_cost(leg, units) for leg in legs), ZERO)
    return BasketPlan(event_ticker, tuple(legs), units, tuple(limits), leg_costs,
                      payout - sum(leg_costs, ZERO), expected)


def plan_basket(event_ticker: str, legs: Sequence[Leg], fee: FeeFn, *, min_profit: Decimal,
                max_units: int, basket_room: Decimal | None = None,
                leg_rooms: Sequence[Decimal | None] | None = None,
                cash_room: Decimal | None = None) -> BasketPlan | None:
    """Size a NO basket over exactly ``legs`` by walking their ladders together.

    The walk moves through *segments* (runs of units during which no leg changes level). At
    each segment's end, and at the dollar/units cap, it prices U units with every contract at
    the legs' current limits (the worst case). It returns the plan with the largest guaranteed
    profit whose profit per unit basket is ``>= min_profit``, or ``None``. The walk stops as soon
    as one more unit at the current levels would earn less than ``min_profit``. Deeper levels
    only cost more, and the worst case re-prices every unit at the deeper limit.
    """
    k = len(legs)
    if k < 2 or any(not leg.ladder for leg in legs) or max_units < 1:
        return None
    payout = D(k - 1)
    min_profit = D(min_profit)
    idx = [0] * k
    rem = [leg.ladder[0][1] for leg in legs]
    done = 0  # units covered by the segments already walked
    best: BasketPlan | None = None
    while True:
        limits = [leg.ladder[idx[i]][0] for i, leg in enumerate(legs)]
        marginal = payout - sum((leg.unit_cost(lim) for leg, lim in zip(legs, limits, strict=True)), ZERO)
        if marginal < min_profit:
            break
        end = done + min(rem)
        cap = _units_cap(legs, limits, basket_room, leg_rooms, cash_room)
        u = min(end, max_units, cap if cap is not None else end)
        if u > done:
            plan = _evaluate(event_ticker, legs, limits, u, fee)
            if plan.guaranteed_profit >= min_profit * u and (
                    best is None or plan.guaranteed_profit > best.guaranteed_profit):
                best = plan
        if u < end:
            break  # capped inside this segment
        step = end - done
        done = end
        for i, leg in enumerate(legs):
            rem[i] -= step
            if rem[i] == 0:
                idx[i] += 1
                if idx[i] >= len(leg.ladder):
                    return best  # a leg's displayed depth is used up
                rem[i] = leg.ladder[idx[i]][1]
    return best


def best_plan(event_ticker: str, legs: Sequence[Leg], fee: FeeFn, *, min_profit: Decimal, max_units: int,
              max_legs: int, partial: bool = True, basket_room: Decimal | None = None,
              leg_rooms: Mapping[str, Decimal | None] | None = None,
              cash_room: Decimal | None = None) -> BasketPlan | None:
    """Best basket over prefixes of ``legs`` ranked by top-of-book contribution (all of them
    when ``partial`` is false). Dropping low-contribution legs is always allowed for a NO basket;
    it trades edge for depth."""
    ranked = sorted((leg for leg in legs if leg.ladder), key=lambda leg: (-leg.contribution, leg.ticker))
    ranked = [leg for leg in ranked if leg.contribution > 0][:max_legs]
    if not partial:
        if len(ranked) != len(legs):
            return None
        sizes: Iterable[int] = (len(ranked),)
    else:
        sizes = range(2, len(ranked) + 1)
    best: BasketPlan | None = None
    for n in sizes:
        sub = ranked[:n]
        rooms = [leg_rooms.get(leg.ticker) for leg in sub] if leg_rooms is not None else None
        plan = plan_basket(event_ticker, sub, fee, min_profit=min_profit, max_units=max_units,
                           basket_room=basket_room, leg_rooms=rooms, cash_room=cash_room)
        if plan is not None and (best is None or plan.guaranteed_profit > best.guaranteed_profit):
            best = plan
    return best


def top_margin(contributions: Iterable[Decimal], max_legs: int) -> Decimal | None:
    """Per-unit top-of-book margin of the best subset: sum of the positive contributions (at
    most ``max_legs``) minus 1. ``None`` with fewer than two contributing legs."""
    pos = sorted((c for c in contributions if c > 0), reverse=True)[:max_legs]
    return sum(pos, ZERO) - ONE if len(pos) >= 2 else None


# --------------------------------------------------------------------------- strategy


@dataclass
class _Scan:
    markets: int = 0
    multi: int = 0
    me: int = 0
    not_me: int = 0
    unknown: int = 0
    nested: int = 0  # unknown flag, but a nested threshold ladder: not looked up
    skipped: int = 0  # held / cooling down
    rejected: dict[str, int] = field(default_factory=dict)
    candidates: int = 0
    fetched_events: int = 0
    priced: int = 0
    books: int = 0
    best_snap: tuple[Decimal, str] | None = None
    best_book: tuple[Decimal, str] | None = None
    fired: list[str] = field(default_factory=list)

    def note(self, what: str) -> None:
        self.rejected[what] = self.rejected.get(what, 0) + 1

    @staticmethod
    def better(cur: tuple[Decimal, str] | None, margin: Decimal | None, et: str) -> tuple[Decimal, str] | None:
        if margin is None or (cur is not None and cur[0] >= margin):
            return cur
        return (margin, et)


class NoBasketArb(Strategy):
    name: ClassVar[str] = "no_basket_arb"
    description: ClassVar[str] = (
        "Risk-free structural arbitrage that almost never fires. On a mutually-exclusive event at most "
        "one market resolves YES, so one NO on each of |S| legs pays at least |S|-1. When the NO asks plus "
        "taker fees and rounding, priced at the worst case on fresh order books, leave at least "
        "min_profit_per_basket per unit basket, it buys every leg at once, all or none (IOC, one group_id). "
        "It needs no exhaustiveness and never trades YES baskets. It skips events with mixed close times "
        "or an already-resolved leg. Residual risk: a voided event settles at Kalshi's 'fair prices'. "
        "Evidence: 0 of 4,986 events positive at REST speed (best -$0.0003/unit)."
    )
    backtestable: ClassVar[bool] = False  # needs simultaneous multi-leg depth; candles don't have it
    #: 10% of a $1,000 account covers max_basket_cost ($90); no daily loss pause (hedged baskets)
    risk_defaults: ClassVar[dict[str, Any]] = {"max_allocation_pct": 10, "daily_loss_limit": 0}
    #: cheap to run and riskless when it fires (which is almost never)
    enabled_by_default: ClassVar[bool] = True
    default_params: ClassVar[dict[str, Any]] = {
        # 3 days, like ladder_favorite / maker_favorite: the engine scans one close-time window, the
        # widest of all enabled strategies, and a 14-day window hits universe_max_pages every rescan
        "max_days_to_close": 3.0,
        "min_profit_per_basket": 0.01,
        "max_baskets": 100,
        "max_basket_cost": 90.0,
        "max_leg_cost": 45.0,
        "cash_reserve": 50.0,
        "max_legs": 20,
        "max_leg_spread": 0.10,
        "min_minutes_to_close": 10.0,
        "allow_mixed_close_times": False,
        "allow_partial_baskets": True,
        "prescreen_margin": -0.02,
        "discovery_margin": -0.10,
        "max_event_fetches_per_tick": 10,
        "max_books_per_tick": 300,
        "cooldown_s": 300,
        "max_trades_per_tick": 1,
    }
    param_schema: ClassVar[dict[str, dict[str, Any]]] = {
        "max_days_to_close": {"type": "float", "min": 0.1, "max": 60,
                              "help": "Universe: markets closing within this many days. Wider windows make the "
                                      "engine's universe scan much more expensive"},
        "min_profit_per_basket": {"type": "float", "min": 0.0001, "max": 1,
                                  "help": "Guaranteed $ per unit basket (one NO per leg) after taker fees, "
                                          "per-order rounding and worst-case fills at the limits"},
        "max_baskets": {"type": "int", "min": 1, "max": 100000,
                        "help": "Most unit baskets per trade (contracts per leg)"},
        "max_basket_cost": {"type": "float", "min": 1, "max": 100000,
                            "help": "Dollar cap on a basket's cost plus the event's existing exposure. Keep it "
                                    "below risk.max_exposure_per_event: a trimmed leg rejects the whole basket"},
        "max_leg_cost": {"type": "float", "min": 1, "max": 100000,
                         "help": "Dollar cap per leg plus the market's existing exposure. Keep it below "
                                 "risk.max_position_cost_per_market"},
        "cash_reserve": {"type": "float", "min": 0, "max": 1000000,
                         "help": "Cash left untouched after a basket. Keep it at or above "
                                 "risk.min_cash_reserve"},
        "max_legs": {"type": "int", "min": 2, "max": ENGINE_MAX_INTENTS,
                     "help": "Most legs per basket. Every leg is one order toward risk.max_orders_per_minute"},
        "max_leg_spread": {"type": "float", "min": 0.01, "max": 1,
                           "help": "Leave out legs whose book spread is wider than this (risk.max_spread "
                                   "would reject them, and with them the whole basket)"},
        "min_minutes_to_close": {"type": "float", "min": 0, "max": 10080,
                                 "help": "Leave out legs closing sooner than this (risk.min_seconds_to_close)"},
        "allow_mixed_close_times": {"type": "bool",
                                    "help": "Also trade events whose active legs close at different times. The "
                                            "payout floor still holds, but capital is locked until the last "
                                            "leg settles, and 'date bucket' events carry timing traps"},
        "allow_partial_baskets": {"type": "bool",
                                  "help": "Allow a basket on a subset of the event's legs (the payout floor "
                                          "|S|-1 holds for any subset). If false, every active leg is required"},
        "prescreen_margin": {"type": "float", "min": -1, "max": 1,
                             "help": "Fetch books only for events whose top-of-book margin per unit basket, "
                                     "from the universe snapshot, is at least this. The snapshot is up to "
                                     "~2 minutes old"},
        "discovery_margin": {"type": "float", "min": -1, "max": 1,
                             "help": "With lookup budget left over, also look up events of unknown type whose "
                                     "snapshot margin is at least this (but below the prescreen), closest first. "
                                     "Nothing is traded from these; the summary's best margin then covers the "
                                     "events nearest to firing"},
        "max_event_fetches_per_tick": {"type": "int", "min": 0, "max": 200,
                                       "help": "Lazy GET /events/{ticker} lookups (mutually_exclusive flag) "
                                               "per tick, only for events that pass the prescreen"},
        "max_books_per_tick": {"type": "int", "min": 2, "max": 5000,
                               "help": "Most order books fetched per tick (100 per batch request)"},
        "cooldown_s": {"type": "int", "min": 0, "max": 86400,
                       "help": "Seconds before retrying an event after a basket was sent for it"},
        "max_trades_per_tick": {"type": "int", "min": 1, "max": 10,
                                "help": "Most baskets sent per tick, best guaranteed profit first"},
    }

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        super().__init__(params)
        self._me: dict[str, bool] = {}  # event ticker -> mutually_exclusive (while in the universe)
        self._cooldown: dict[str, datetime] = {}  # event ticker -> last basket sent
        self.last_scan: _Scan | None = None

    def universe(self) -> UniverseSpec:
        return UniverseSpec(max_days_to_close=float(self.params["max_days_to_close"]))

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def fee_coefficient(ctx: StrategyContext, market: Market) -> Decimal:
        """Taker fee coefficient k (fee = k x C x P(1-P)) in effect for ``market``."""
        fp = getattr(ctx, "fee_params", None)
        if callable(fp):
            try:
                ft, mult = fp(market)
                return fee_rate(ft, mult, is_taker=True)
            except Exception:  # fall back to inferring it from ctx.fee
                pass
        # ctx.fee for a huge order at P = 0.5 is k x C / 4, rounded up to the balance precision
        return D(ctx.fee(market, _HALF, _BIG, True)) * 4 / _BIG

    def _leg_ok(self, m: Market, now: datetime) -> bool:
        """Snapshot-level leg filter (tradable, not MVE, closes late enough, two-sided and not
        too wide per the snapshot quote)."""
        if not m.is_tradable(now) or is_mve(m):
            return False
        if m.close_time is not None and (m.close_time - now).total_seconds() < \
                float(self.params["min_minutes_to_close"]) * 60:
            return False
        if m.yes_bid is None or m.yes_ask is None:
            return False
        return m.yes_ask - m.yes_bid <= D(self.params["max_leg_spread"])

    def _book_ok(self, book: Orderbook | None) -> bool:
        if book is None or not book.yes_bids or not book.no_bids:
            return False
        spread = book.spread
        return spread is not None and spread <= D(self.params["max_leg_spread"])

    def _known_me(self, ctx: StrategyContext, et: str) -> bool | None:
        if et in self._me:
            return self._me[et]
        ev = ctx.events.get(et) if ctx.events is not None else None
        if ev is not None:
            self._me[et] = bool(ev.mutually_exclusive)
            return self._me[et]
        return None

    def _event_problem(self, ev: Event, legs: Sequence[Market]) -> str | None:
        """Why this event may not be traded (``None`` = fine). ``legs``: its eligible universe legs."""
        if not ev.mutually_exclusive:
            return "not mutually exclusive"
        markets = list(ev.markets) or list(legs)
        if any(is_mve(m) for m in markets):
            return "MVE leg"
        if any(m.result == "yes" for m in markets):
            return "a leg already resolved YES"
        active = [m for m in markets if m.is_open]
        if not self.params["allow_mixed_close_times"] and len({m.close_time for m in active}) > 1:
            return "legs close at different times"
        if not self.params["allow_partial_baskets"]:
            eligible = {m.ticker for m in legs}
            if any(m.ticker not in eligible for m in active):
                return "not every active leg is tradable"
        return None

    def _held_events(self, ctx: StrategyContext) -> set[str]:
        pv = ctx.portfolio
        out = {p.event_ticker for p in getattr(pv, "positions", ()) or ()
               if p.strategy == self.name and p.count > 0 and p.event_ticker}
        for o in getattr(pv, "open_orders", ()) or ():
            if o.strategy == self.name:
                et = getattr(o, "event_ticker", "") or ""
                m = ctx.markets.get(o.ticker)
                out.add(et or (m.event_ticker if m is not None else ""))
        out.discard("")
        return out

    @staticmethod
    def _exposure(ctx: StrategyContext, **flt: str) -> Decimal:
        fn = getattr(ctx.portfolio, "exposure", None)
        try:
            return D(fn(**flt)) if callable(fn) else ZERO
        except Exception:
            return ZERO

    async def _event(self, ctx: StrategyContext, et: str, scan: _Scan) -> Event | None:
        ev = ctx.events.get(et) if ctx.events is not None else None
        if ev is not None:
            return ev
        fetch = getattr(ctx, "event", None)
        if not callable(fetch) or scan.fetched_events >= int(self.params["max_event_fetches_per_tick"]):
            return None
        scan.fetched_events += 1
        try:
            ev = await fetch(et)
        except KalshiNotFound:
            self._me[et] = False  # no such event: never a basket
            return None
        except Exception:
            return None  # network trouble: try again next tick
        if ev is not None:
            self._me[et] = bool(ev.mutually_exclusive)
        return ev

    async def _books(self, ctx: StrategyContext, tickers: list[str]) -> dict[str, Orderbook]:
        if not tickers:
            return {}
        batch = getattr(ctx, "orderbooks", None)
        if callable(batch):
            return dict(await batch(tickers))
        res = await asyncio.gather(*(ctx.orderbook(t) for t in tickers), return_exceptions=True)
        return {t: b for t, b in zip(tickers, res, strict=True) if not isinstance(b, BaseException)}

    # ------------------------------------------------------------------ tick

    async def on_tick(self, ctx: StrategyContext) -> list[OrderIntent]:
        p = self.params
        now = ctx.now
        scan = _Scan(markets=len(ctx.markets))
        max_legs = int(p["max_legs"])
        prescreen = D(p["prescreen_margin"])

        by_event: dict[str, list[Market]] = {}
        for m in ctx.markets.values():
            if m.event_ticker:
                by_event.setdefault(m.event_ticker, []).append(m)
        for et in [et for et in self._me if et not in by_event]:
            del self._me[et]
        cooldown = timedelta(seconds=int(p["cooldown_s"]))
        for et in [et for et, ts in self._cooldown.items() if now - ts >= cooldown]:
            del self._cooldown[et]
        held = self._held_events(ctx)

        # 1. prescreen on the universe snapshot (no requests)
        rates: dict[tuple[str, str], Decimal] = {}  # fee parameters depend on (series, event) only

        def rate(m: Market) -> Decimal:
            key = (m.series_ticker, m.event_ticker)
            r = rates.get(key)
            if r is None:
                r = rates[key] = self.fee_coefficient(ctx, m)
            return r

        cands: list[tuple[Decimal, str, list[Market]]] = []
        near: list[tuple[Decimal, str]] = []  # unknown flag, below the prescreen but within discovery range
        discovery = D(p["discovery_margin"])
        for et in sorted(by_event):
            ms = by_event[et]
            if len(ms) < 2:
                continue
            scan.multi += 1
            known = self._known_me(ctx, et)
            if known is False:
                scan.not_me += 1
                continue
            if known:
                scan.me += 1
            elif nested_ladder(ms):
                scan.nested += 1
                continue
            else:
                scan.unknown += 1
            if et in held or et in self._cooldown:
                scan.skipped += 1
                continue
            legs = [m for m in ms if self._leg_ok(m, now)]
            contribs = []
            for m in legs:
                yb = m.yes_bid  # NO ask = 1 - YES bid; contribution = 1 - NO ask - fee = yb - k*yb*(1-yb)
                contribs.append(yb - rate(m) * yb * (ONE - yb))  # type: ignore[operator]
            margin = top_margin(contribs, max_legs)
            if known:
                scan.best_snap = scan.better(scan.best_snap, margin, et)
            if margin is not None and margin >= prescreen:
                cands.append((margin, et, legs))
            elif known is None and margin is not None and margin >= discovery:
                near.append((margin, et))
        # known mutually-exclusive events need no lookup; then series with a known ME event, then the rest
        me_series = {ms[0].series_ticker for et, ms in by_event.items() if self._me.get(et)}
        cands.sort(key=lambda c: (self._me.get(c[1]) is not True, c[2][0].series_ticker not in me_series,
                                  -c[0], c[1]))
        scan.candidates = len(cands)

        # 2. event checks (lazy event fetches) and the book budget
        chosen: list[tuple[str, list[Market]]] = []
        n_active: dict[str, int] = {}  # active legs per event (for the reason text)
        n_books = 0
        for _, et, legs in cands:
            ev = await self._event(ctx, et, scan)
            if ev is None:
                scan.note("event not looked up yet")
                continue
            why = self._event_problem(ev, legs)
            if why is not None:
                scan.note(why)
                continue
            if n_books + len(legs) > int(p["max_books_per_tick"]):
                scan.note("book budget")
                continue
            n_books += len(legs)
            chosen.append((et, legs))
            n_active[et] = sum(1 for m in ev.markets if m.is_open) or len(legs)
        # spare lookup budget: learn the flags of the unknown events closest to the prescreen, so the
        # summary's best margin covers the events nearest to firing (nothing is traded from these)
        near.sort(key=lambda c: (-c[0], c[1]))
        for _, et in near:
            if scan.fetched_events >= int(p["max_event_fetches_per_tick"]):
                break
            await self._event(ctx, et, scan)

        # 3. fresh books, then size baskets
        plans: list[BasketPlan] = []
        if chosen:
            for s in sorted({m.series_ticker for _, legs in chosen for m in legs if m.series_ticker}):
                try:  # warm the series cache so ctx.fee uses the real fee parameters
                    await ctx.series(s)
                except Exception:
                    pass  # ctx.fee falls back to conservative parameters
            rates.clear()
            try:
                books = await self._books(ctx, [m.ticker for _, legs in chosen for m in legs])
            except Exception as e:
                books = {}
                scan.note(f"books unavailable ({type(e).__name__})")
            scan.books = len(books)
            cash_room = D(getattr(ctx.portfolio, "cash", ZERO)) - D(p["cash_reserve"])
            fee = self._fee_fn(ctx)
            for et, legs in chosen:
                built = [Leg(m, no_ladder(books[m.ticker]), rate(m))
                         for m in legs if self._book_ok(books.get(m.ticker))]
                built = [leg for leg in built if leg.ladder]
                if not built:
                    continue
                scan.priced += 1
                book_margin = top_margin((lg.contribution for lg in built), max_legs)
                scan.best_book = scan.better(scan.best_book, book_margin, et)
                if not p["allow_partial_baskets"] and len(built) != len(legs):
                    scan.note("not every active leg is tradable")
                    continue
                plan = best_plan(
                    et, built, fee, min_profit=D(p["min_profit_per_basket"]), max_units=int(p["max_baskets"]),
                    max_legs=max_legs, partial=bool(p["allow_partial_baskets"]),
                    basket_room=D(p["max_basket_cost"]) - self._exposure(ctx, event_ticker=et),
                    leg_rooms={lg.ticker: D(p["max_leg_cost"]) - self._exposure(ctx, ticker=lg.ticker)
                               for lg in built},
                    cash_room=cash_room)
                if plan is not None:
                    plans.append(plan)

        # 4. intents: best baskets first, within the cash room and the engine's intent cap
        intents: list[OrderIntent] = []
        plans.sort(key=lambda pl: (-pl.guaranteed_profit, pl.event_ticker))
        cash_left = D(getattr(ctx.portfolio, "cash", ZERO)) - D(p["cash_reserve"])
        for plan in plans:
            if len(scan.fired) >= int(p["max_trades_per_tick"]):
                break
            if len(intents) + len(plan.legs) > ENGINE_MAX_INTENTS or plan.cost_bound > cash_left:
                continue
            cash_left -= plan.cost_bound
            intents.extend(self._intents(plan, now, n_active=n_active.get(plan.event_ticker, len(plan.legs))))
            self._cooldown[plan.event_ticker] = now
            scan.fired.append(plan.event_ticker)
            ctx.log(f"NO basket {plan.event_ticker}: {len(plan.legs)} legs x{plan.units}, guaranteed "
                    f"${plan.guaranteed_profit:.4f} (${plan.profit_per_basket:.4f}/basket), "
                    f"${plan.expected_profit:.4f} on current books",
                    event_ticker=plan.event_ticker, legs=len(plan.legs), units=plan.units,
                    guaranteed_profit=float(plan.guaranteed_profit), expected_profit=float(plan.expected_profit),
                    limits={lg.ticker: float(lim) for lg, lim in zip(plan.legs, plan.limits, strict=True)})

        self.last_scan = scan
        self._log_summary(ctx, scan, prescreen)
        return intents

    @staticmethod
    def _fee_fn(ctx: StrategyContext) -> FeeFn:
        def fee(market: Market, price: Decimal, count: int) -> Decimal:
            return D(ctx.fee(market, price, count, True))
        return fee

    def _intents(self, plan: BasketPlan, now: datetime, n_active: int) -> list[OrderIntent]:
        k = len(plan.legs)
        gid = f"{self.name}:{plan.event_ticker}:{now.strftime('%Y%m%dT%H%M%S')}"
        base = (f"NO basket on {k} of {max(n_active, k)} legs of mutually-exclusive {plan.event_ticker}: pays >= "
                f"${k - 1}/basket; worst-case cost ${plan.cost_bound / plan.units:.4f}/basket incl. fees+rounding "
                f"-> >= ${plan.profit_per_basket:.4f}/basket x{plan.units} = ${plan.guaranteed_profit:.2f} "
                f"guaranteed (${plan.expected_profit:.2f} on current books)")
        return [OrderIntent(ticker=leg.ticker, side="no", action="buy", count=plan.units, limit_price=lim,
                            tif="ioc", strategy=self.name, reason=f"{base}; this leg NO <= {lim}",
                            fair_value=None, expected_edge=plan.edge_per_contract, group_id=gid)
                for leg, lim in zip(plan.legs, plan.limits, strict=True)]

    def _log_summary(self, ctx: StrategyContext, s: _Scan, prescreen: Decimal) -> None:
        def fmt(x: tuple[Decimal, str] | None) -> str:
            return f"{x[0]:+.4f}/unit ({x[1]})" if x else "n/a"

        skipped = ", ".join(f"{v} {k}" for k, v in sorted(s.rejected.items()))
        msg = (f"scan: {s.multi} multi-leg events in {s.markets} markets ({s.me} mutually exclusive, "
               f"{s.not_me} not, {s.nested} nested ladders, {s.unknown} unknown; {s.skipped} held/cooling); "
               f"{s.candidates} >= prescreen "
               f"{prescreen:+.4f}, {s.fetched_events} events fetched, {s.priced} priced on {s.books} fresh books"
               f"{f' (skipped: {skipped})' if skipped else ''}; best book margin {fmt(s.best_book)}, "
               f"best snapshot margin {fmt(s.best_snap)}; "
               f"{('FIRED ' + ', '.join(s.fired)) if s.fired else 'no basket'}")
        ctx.log(msg, events_multi=s.multi, events_me=s.me, events_not_me=s.not_me, events_nested=s.nested,
                events_unknown=s.unknown,
                candidates=s.candidates, events_fetched=s.fetched_events, events_priced=s.priced,
                best_book_margin=float(s.best_book[0]) if s.best_book else None,
                best_book_event=s.best_book[1] if s.best_book else None,
                best_snapshot_margin=float(s.best_snap[0]) if s.best_snap else None,
                best_snapshot_event=s.best_snap[1] if s.best_snap else None, fired=list(s.fired))
