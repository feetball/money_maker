"""Maker favourite harvest - **EXPERIMENTAL** (literature rule M1, ``research/FINDINGS.md``).

Idea: on fee-free series, rest small bids on the heavy-favourite side (best bid 0.85-0.96)
and collect the maker premium the literature measures (makers +1.1% vs takers -1.1% per
trade over 72M Kalshi trades, Becker; makers buying at >= 50c +1.9% after fees,
Burgi-Deng-Whelan 2026). **This band rests on the literature only; the repo's own data is
negative here.** The research's candle maker model (rest at the favourite's bid, filled when a
later candle trades through it) on this strategy's filters (favourite bid 0.85-0.96, either
side, non-Sports, quadratic fee type, spread <= 3c) gives -1.15c per filled contract
(-0.74c per signal) in TRAIN and -2.36c (-1.36c per signal) in TEST; ``oos_headline.csv``
agrees (maker P&L per fill: bid >= 0.85 -2.00 / -1.76c, >= 0.90 -1.38 / -1.13c; it only turns
non-negative at >= 0.95: +0.53c TEST). That fill model counts only trade-through fills, so it
leans adverse, but it is the only in-repo evidence. (The "+0.2...+0.4c per signal" figure in
``verify_costs/VERDICT.md`` is the B4ns maker variant, favourite bids >= 0.97 - not this band.)
So this runs as a forward paper test only, at small size, with a **zero** prior edge by
default. Positions are held to settlement.

Entries (every tick)
--------------------
1. **Snapshot filter** (``ctx.markets``, no requests): binary, not a ``KXMVE*`` combo; close
   time in [now + ``min_hours_to_close``, now + ``max_days_to_close``]; the series is not in
   :data:`OUTCOME_TIMING_SERIES` (close timing depends on the outcome, from
   ``research/calibration/series_flags.csv``) nor ``exclude_series``; ``volume_24h >=
   min_volume_24h``; two-sided with YES ask - YES bid <= ``max_spread``; a favourite side:
   YES if the best YES bid is in [``min_bid``, ``max_bid``], else NO if the best NO bid is.
2. **Series / event check** (``ctx.series`` and the event; cached here for an hour; at most
   ``max_lookups_per_tick`` lookups per tick): the effective fee type (the event's
   ``fee_type_override`` beats the series) is in ``fee_types`` (plain ``quadratic``: makers
   pay 0), and the category (series, else event) is known and not in ``exclude_categories``
   (Sports: in-game jumps). ``ctx.fee`` must also charge makers nothing on a probe order
   (100 @ 0.50), which catches scheduled fee changes the cached series does not show yet.
3. **Fresh book** (one batched fetch for the resting orders and the best ``2 x free slots``
   candidates): step 1's quote conditions are re-checked on the live book, then a GTC
   **buy** of the favourite side rests at its current best bid (joins the queue, never
   improves or crosses) for ``count = floor(order_dollars / bid)`` contracts, expiring after
   ``min(expires_in_s, time until close - min_hours_to_close)``: nothing rests into the last
   6 h even if a cancel is missed.
4. **Caps**: at most ``max_resting_orders`` (8) of this strategy's orders rest at once (each
   costs a trade-tape poll every ``order_poll_s`` against the shared ~3 req/s budget), ranked
   by ``volume_24h``; at most ``max_markets_per_event`` markets per event (positions plus
   resting orders); one position per market (a market that is held or has a resting order
   gets no new order, so a filled or partly filled market is never topped up); and the
   strategy's open cost - filled positions (held to settlement) **plus** resting bids
   (``ctx.portfolio.exposure(strategy=...)``) - stays within ``max_open_cost`` ($100), so fills
   cannot pile up while each fill frees a resting slot. No exits. Crypto (short-volatility
   bids on the same BTC/ETH prices the other strategies trade; Kalshi absorbs spot moves in
   ~1.5 s, so resting favourite bids there are the easiest to pick off) and Mentions (tend to
   close early on YES) are excluded by default, next to Sports.

Resting orders (every tick, when the context can cancel)
--------------------------------------------------------
Each resting order is re-checked against the fresh book:

* the market closes within ``min_hours_to_close`` -> ``CancelIntent``;
* the best bid on our side is within ``stale_move`` (1c) of our price -> keep (queue kept);
* it moved further (either way) and the market still qualifies -> cancel/replace at the new
  best bid (``OrderIntent(replaces=old_id)``, the old order's remaining count);
* it moved and no longer qualifies (bid out of range, favourite flipped, spread too wide,
  side empty) -> ``CancelIntent``. A cancelled market is not re-entered for
  ``BOOK_COOLDOWN_S``.

Cancels need an engine that applies them: the strategy uses them only when the context has
``ctx.cancel`` (the engine's context advertises cancel support that way, see
``strategies/base.py``). Otherwise (older engine, other contexts) it cannot cancel: a stale
order is logged once and left to expire, and the lifetime is capped at
``NO_CANCEL_EXPIRY_S`` (900 s) so quotes are refreshed at least that often (each refresh
loses the queue position). ``replaces`` is never sent to such a context, because an engine
that ignores it would leave two orders resting in one market.

Intents carry ``expected_edge = prior_edge - maker fee per contract`` (the fee is 0 on these
series apart from sub-cent principal rounding at cent balance precision) and
``fair_value = bid + prior_edge``. ``prior_edge`` defaults to **0**: the literature suggests
+0.5...1c, the in-repo candle model -1...-2c per fill, so the dashboard's expected-vs-realized
view does not start from an optimistic expectation.

Backtesting
-----------
``backtestable = False``. The backtester (section 10) replays hourly candles with a
synthetic book: it has no trade tape, no queue position and no intra-hour path, so it cannot
say whether a resting bid at the best bid would have been filled, or whether fills came from
queue turnover (benign) or from the price trading through (adverse selection). Those are the
whole question for a maker strategy. The research's candle "maker fill" (a later trade below
the bid, no queue, full size) was explicitly labelled optimistic. Evidence has to come from
forward paper trading, where the broker fills resting orders only from real prints after
the displayed queue ahead of us.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar

from kalshibot.fees import resolve_fee_params
from kalshibot.money import ONE, ZERO, D
from kalshibot.paper.models import iso
from kalshibot.strategies.base import CancelIntent, OrderIntent, Strategy, StrategyContext, UniverseSpec

if TYPE_CHECKING:
    from kalshibot.kalshi.models import Event, Market, Orderbook, Series

__all__ = ["OUTCOME_TIMING_SERIES", "MakerFavoriteHarvest", "Quote", "pick_favourite"]

#: Series whose close timing depends on the outcome (``outcome_dep`` in
#: ``research/calibration/series_flags.csv``: YES and NO markets close > 12 h apart relative
#: to the expected expiration, or markets of one event close > 12 h apart, 90th pct). Their
#: close time leaks or tracks the result, so a "closes in 6 h-7 d" favourite is not what it seems.
OUTCOME_TIMING_SERIES: frozenset[str] = frozenset({
    "KX10YRDIRHM", "KXALLINMENTION", "KXARTISTSTREAMSY", "KXATP", "KXATPADVANCE", "KXCARNEYMENTION",
    "KXCHAMPTOUR", "KXCLAUDE", "KXDPWORLDTOUR", "KXHORMUZWEEKLY", "KXLASTWORDMENTION", "KXLIUSAELIMINATIONW",
    "KXLIUSAWINNERS", "KXLOVEISLANDUSARANK", "KXLOVEISLMENTION", "KXMAMDANIEO", "KXMCDONALDCONF",
    "KXMEDIARELEASEDATEAHS", "KXMLBMENTION", "KXMLBPLAYOFFS", "KXMLBWINS-AZ", "KXMLBWINS-PIT", "KXNASCARTOP10",
    "KXNBAPICKTRADE", "KXNBATEAMANNOUNCE", "KXNFLWINSWEEK", "KXPGABOGEYFREE", "KXPGAHOLESCORE", "KXPGAR3TOP10",
    "KXPGATOP10", "KXPGATOP20", "KXPGATOP5", "KXPGATOUR", "KXPLATNERREPLACE", "KXPRIMARYMOV", "KXPRIMARYPLACE",
    "KXPSAKIMENTION", "KXSECPRESSMENTION", "KXTRUMPMENTION", "KXTRUMPMENTIONB", "KXTRUMPSAY", "KXTRUMPTIME",
    "KXTRUTHSOCIAL", "KXUAPFILES", "KXURYPDSPREAD", "KXVANCEMENTION", "KXWCGOALCOUNT", "KXWCMENTION",
    "KXWNBAMENTION", "KXWNBAWINS", "KXWNBAWORSTREC", "KXWORLDNEWSMENTION", "KXWTA", "KXWTAADVANCE",
    "KXYTVIEWSW",
})

#: A market whose fresh book failed the checks (or whose order was cancelled) is not
#: considered for a new order for this long (s).
BOOK_COOLDOWN_S = 300
#: A ticker we sent an intent for is retried after this long (s) when neither an order nor
#: a position appeared (risk or broker rejection), capped at the order lifetime.
RETRY_S = 300
#: Lifetime of the series/event metadata cached by the strategy (s).
META_TTL_S = 3600
#: A failed series/event lookup is retried after this long (s).
META_RETRY_S = 600
#: Orders that would expire sooner than this are not placed (s).
MIN_EXPIRY_S = 60
#: Cap on the order lifetime when the context cannot cancel (s).
NO_CANCEL_EXPIRY_S = 900
#: Probe order for "makers pay nothing here": whole cents, so no principal rounding.
_PROBE_PRICE, _PROBE_COUNT = Decimal("0.50"), 100

_BUDGET = object()  # lookup budget for this tick is spent
_FAILED = object()  # lookup failed recently


@dataclass(frozen=True, slots=True)
class Quote:
    """The favourite side of a two-sided book and its best bid."""

    side: str  # "yes" | "no"
    bid: Decimal
    spread: Decimal  # YES ask - YES bid


def pick_favourite(yes_bid: Decimal | None, no_bid: Decimal | None, *, min_bid: Decimal, max_bid: Decimal,
                   max_spread: Decimal) -> tuple[Quote | None, str]:
    """``(Quote, "")`` for the favourite side, or ``(None, why)``.

    Needs both bids (two-sided), ``0 < spread <= max_spread`` (spread = YES ask - YES bid
    = 1 - NO bid - YES bid) and a best bid in [min_bid, max_bid]: YES first, then NO.
    """
    if yes_bid is None or no_bid is None:
        return None, "one-sided book"
    spread = ONE - no_bid - yes_bid
    if spread <= 0:
        return None, "locked or crossed book"
    if spread > max_spread:
        return None, "spread too wide"
    if min_bid <= yes_bid <= max_bid:
        return Quote("yes", yes_bid, spread), ""
    if min_bid <= no_bid <= max_bid:
        return Quote("no", no_bid, spread), ""
    return None, "no favourite bid in range"


@dataclass(frozen=True, slots=True)
class _Meta:
    fee_type: str
    fee_multiplier: Decimal
    category: str


def _num(x: Decimal) -> str:
    """Plain decimal text without trailing zeros or exponents (``Decimal("500.00")`` -> ``500``)."""
    return f"{D(x).normalize():f}"


def _is_mve(m: Market) -> bool:
    return (m.ticker.upper().startswith("KXMVE") or m.series_ticker.upper().startswith("KXMVE")
            or bool(m.raw.get("mve_collection_ticker")))


class MakerFavoriteHarvest(Strategy):
    """EXPERIMENTAL maker favourite harvest (see the module docstring for the full rule)."""

    name = "maker_favorite"
    description = (
        "EXPERIMENTAL - maker favourite harvest (literature M1). Rests a small GTC bid at the best bid of the "
        "0.85-0.96 favourite side in fee-free ('quadratic') markets closing in 6 h-3 d, excluding Sports, "
        "Crypto and Mentions (hard caps on resting orders and on open cost, highest 24 h volume first), "
        "re-quotes when the bid moves > 1c, cancels inside 6 h of the close, and holds fills to settlement. "
        "Rests on the literature only: the repo's own candle maker model is NEGATIVE in this band (-1.2c "
        "TRAIN / -2.4c TEST per filled contract), so the default prior edge is 0. Not backtestable: maker "
        "fills need trade-tape data.")
    experimental: ClassVar[bool] = True
    #: forward paper-test at small size (max_open_cost, max_resting_orders) unless turned off
    enabled_by_default: ClassVar[bool] = True
    risk_defaults: ClassVar[dict[str, Any]] = {"max_allocation_pct": 10, "daily_loss_limit": 30}
    backtestable = False
    default_params: ClassVar[dict[str, Any]] = {
        "order_dollars": 10.0,
        "min_bid": 0.85,
        "max_bid": 0.96,
        "max_spread": 0.03,
        "min_hours_to_close": 6.0,
        "max_days_to_close": 3.0,
        "max_resting_orders": 8,
        "max_open_cost": 100.0,
        "max_markets_per_event": 2,
        "min_volume_24h": 10.0,
        "expires_in_s": 3600,
        "stale_move": 0.01,
        "prior_edge": 0.0,
        "fee_types": ["quadratic"],
        "exclude_categories": ["Sports", "Crypto", "Mentions"],
        "exclude_series": [],
        "max_lookups_per_tick": 20,
    }
    param_schema: ClassVar[dict[str, dict[str, Any]]] = {
        "order_dollars": {"type": "float", "min": 1, "max": 1000,
                          "help": "dollars per order; count = floor(order_dollars / bid)"},
        "min_bid": {"type": "float", "min": 0.5, "max": 0.99, "help": "favourite side's best bid at least this"},
        "max_bid": {"type": "float", "min": 0.5, "max": 0.99, "help": "favourite side's best bid at most this"},
        "max_spread": {"type": "float", "min": 0.001, "max": 0.2,
                       "help": "max YES ask - YES bid (two-sided book required)"},
        "min_hours_to_close": {"type": "float", "min": 0, "max": 168,
                               "help": "no order rests closer to the close than this (cancel + expiry cap)"},
        "max_days_to_close": {"type": "float", "min": 0.25, "max": 30,
                              "help": "universe window: markets closing within N days"},
        "max_resting_orders": {"type": "int", "min": 0, "max": 100,
                               "help": "hard cap on concurrent resting orders (each costs trade-tape polling)"},
        "max_open_cost": {"type": "float", "min": 0, "max": 100000,
                          "help": "dollar cap on this strategy's open cost: filled positions (held to settlement) "
                                  "plus resting bids; no new entries beyond it"},
        "max_markets_per_event": {"type": "int", "min": 1, "max": 50,
                                  "help": "max markets per event (positions + resting orders)"},
        "min_volume_24h": {"type": "float", "min": 0, "max": 1e9,
                           "help": "skip markets with fewer contracts traded in 24 h (bids fill only from trades)"},
        "expires_in_s": {"type": "int", "min": 60, "max": 86400,
                         "help": f"GTC lifetime (capped at {NO_CANCEL_EXPIRY_S} s when the engine cannot cancel)"},
        "stale_move": {"type": "float", "min": 0, "max": 0.1,
                       "help": "cancel/replace when the best bid moved further than this from our price"},
        "prior_edge": {"type": "float", "min": -0.1, "max": 0.1,
                       "help": "assumed edge $/contract before maker fees (default 0: literature +0.005...0.01, "
                               "in-repo candle maker model -0.01...-0.02 per fill in this band)"},
        "fee_types": {"type": "list", "help": "allowed effective fee types (maker-free: quadratic)"},
        "exclude_categories": {"type": "list", "help": "series/event categories to skip"},
        "exclude_series": {"type": "list", "help": "extra series tickers to skip (outcome-timing ones always are)"},
        "max_lookups_per_tick": {"type": "int", "min": 0, "max": 500,
                                 "help": "series/event metadata lookups per tick (API budget)"},
    }

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        super().__init__(params)
        self._series: dict[str, tuple[datetime, Series]] = {}
        self._events: dict[str, tuple[datetime, Event | None]] = {}
        self._meta_fail: dict[str, datetime] = {}
        self._cooldown: dict[str, datetime] = {}  # ticker -> no new order before
        self._attempted: dict[str, datetime] = {}  # ticker -> last entry intent sent
        self._flagged: set[int] = set()  # stale resting orders already logged (no-cancel mode)
        #: Diagnostics of the last tick (slots, candidates, intents, cancels, skip reasons).
        self.last_scan: dict[str, Any] = {}

    def universe(self) -> UniverseSpec:
        return UniverseSpec(max_days_to_close=float(self.params["max_days_to_close"]))

    # ------------------------------------------------------------------ tick

    async def on_tick(self, ctx: StrategyContext) -> list[OrderIntent | CancelIntent]:
        p = self.params
        now = ctx.now
        self._prune(now)
        can_cancel = callable(getattr(ctx, "cancel", None))
        pf = ctx.portfolio
        mine = list(pf.orders_for(strategy=self.name))
        free = max(0, int(p["max_resting_orders"]) - len(mine))
        room = D(p["max_open_cost"]) - pf.exposure(strategy=self.name)  # positions + resting bids
        stats: Counter[str] = Counter()
        if room <= 0 and free > 0:
            stats["open cost cap"] += 1
            free = 0
        self.last_scan = {"ts": now, "can_cancel": can_cancel, "resting": len(mine), "free": free,
                          "open_cost_room": float(room), "candidates": 0, "intents": 0, "cancels": 0,
                          "replaces": 0, "skipped": stats}
        if not can_cancel:
            self._review_resting(ctx, mine)
        if free == 0 and not (can_cancel and mine):
            return []

        busy: set[str] = set()  # held or resting: one position per market
        by_event: dict[str, set[str]] = {}
        for x in [*mine, *(pos for pos in pf.positions if pos.strategy == self.name and pos.count > 0)]:
            busy.add(x.ticker)
            by_event.setdefault(self._event_of(ctx, x), set()).add(x.ticker)
        cap = int(p["max_markets_per_event"])
        budget = [int(p["max_lookups_per_tick"])]

        # entry candidates with their series/event metadata, best first, up to 2x the free slots
        ready: list[tuple[Market, _Meta]] = []
        if free > 0:
            cands = self._candidates(ctx, busy, stats)
            self.last_scan["candidates"] = len(cands)
            planned = {e: set(ts) for e, ts in by_event.items()}
            for m in cands:
                if len(ready) >= 2 * free:
                    break
                if len(planned.get(m.event_ticker, ())) >= cap:
                    stats["event cap"] += 1
                    continue
                meta = await self._meta(ctx, m, budget, now)
                if isinstance(meta, str):
                    stats[meta] += 1
                    continue
                ready.append((m, meta))
                planned.setdefault(m.event_ticker, set()).add(m.ticker)

        # one batched book fetch for the resting orders (if they can be cancelled) and the candidates
        want = [o.ticker for o in mine] if can_cancel else []
        books = await self._books(ctx, list(dict.fromkeys(want + [m.ticker for m, _ in ready])))

        out: list[OrderIntent | CancelIntent] = []
        if can_cancel:
            upkeep = [2 * len(mine)]  # own lookup budget: a moved quote is never dropped for lack of it
            for o in mine:
                act = await self._maintain(ctx, o, books.get(o.ticker), upkeep, now)
                if act is None:
                    continue
                out.append(act)
                if isinstance(act, CancelIntent):
                    self.last_scan["cancels"] += 1
                    self._cooldown[o.ticker] = now + timedelta(seconds=BOOK_COOLDOWN_S)
                else:  # if the replacement is rejected, the market waits like any unfilled entry
                    self.last_scan["replaces"] += 1
                    self._attempted[o.ticker] = now

        lo, hi, mx = D(p["min_bid"]), D(p["max_bid"]), D(p["max_spread"])
        placed = 0
        for m, meta in ready:
            if placed >= free:
                break
            ev = by_event.setdefault(m.event_ticker, set())
            if len(ev) >= cap:
                stats["event cap"] += 1
                continue
            book = books.get(m.ticker)
            if book is None:
                stats["book unavailable"] += 1
                continue
            q, why = pick_favourite(book.best_yes_bid, book.best_no_bid, min_bid=lo, max_bid=hi, max_spread=mx)
            if q is None:
                stats[f"book: {why}"] += 1
                self._cooldown[m.ticker] = now + timedelta(seconds=BOOK_COOLDOWN_S)
                continue
            intent = self._intent(ctx, m, q, book, meta, now, can_cancel=can_cancel)
            if isinstance(intent, str):
                stats[intent] += 1
                self._cooldown[m.ticker] = now + timedelta(seconds=BOOK_COOLDOWN_S)
                continue
            cost = intent.count * intent.buy_price
            if cost > room:
                stats["open cost cap"] += 1
                break
            room -= cost
            out.append(intent)
            placed += 1
            ev.add(m.ticker)
            self._attempted[m.ticker] = now
        self.last_scan["intents"] = placed
        return out

    # ------------------------------------------------------------------ entries

    def _candidates(self, ctx: StrategyContext, busy: set[str], stats: Counter[str]) -> list[Market]:
        """Step 1 on the universe snapshot (no requests), best ``volume_24h`` first."""
        p = self.params
        now = ctx.now
        earliest = now + timedelta(hours=float(p["min_hours_to_close"]))
        latest = now + timedelta(days=float(p["max_days_to_close"]))
        excluded = OUTCOME_TIMING_SERIES | {str(s).upper() for s in p["exclude_series"]}
        min_vol = D(p["min_volume_24h"])
        lo, hi, mx = D(p["min_bid"]), D(p["max_bid"]), D(p["max_spread"])
        retry = timedelta(seconds=min(RETRY_S, int(p["expires_in_s"])))
        out: list[Market] = []
        for t, m in ctx.markets.items():
            if t in busy:
                continue
            if t in self._cooldown or (t in self._attempted and now - self._attempted[t] < retry):
                stats["cooldown"] += 1
                continue
            if not m.is_open or m.market_type not in ("binary", "") or _is_mve(m):
                continue
            if m.series_ticker.upper() in excluded:
                stats["excluded series"] += 1
                continue
            if m.close_time is None or not earliest <= m.close_time <= latest:
                continue
            if m.volume_24h < min_vol:
                continue
            q, _ = pick_favourite(m.yes_bid, m.no_bid, min_bid=lo, max_bid=hi, max_spread=mx)
            if q is None:
                continue
            out.append(m)
        out.sort(key=lambda m: (-m.volume_24h, m.ticker))
        return out

    async def _meta(self, ctx: StrategyContext, m: Market, budget: list[int], now: datetime) -> _Meta | str:
        """Step 2: fee type (event override first) and category; a skip reason otherwise."""
        p = self.params
        series = await self._lookup_series(ctx, m.series_ticker, budget, now)
        if series is _BUDGET:
            return "lookup budget spent"
        if series is _FAILED:
            return "series unavailable"
        event = await self._lookup_event(ctx, m.event_ticker, budget, now)
        if event is _BUDGET:
            return "lookup budget spent"
        if event is _FAILED:
            return "event unavailable"
        category = getattr(series, "category", "") or getattr(event, "category", "") or ""
        if not category:
            return "unknown category"
        if category.strip().lower() in {str(c).strip().lower() for c in p["exclude_categories"]}:
            return "excluded category"
        fee_type, mult = resolve_fee_params(series, event)
        if fee_type not in {str(f) for f in p["fee_types"]}:
            return "fee type charges makers"
        return _Meta(fee_type, mult, category)

    def _intent(self, ctx: StrategyContext, m: Market, q: Quote, book: Orderbook, meta: _Meta, now: datetime,
                *, can_cancel: bool, count: int | None = None, replaces: Any = None) -> OrderIntent | str:
        """The resting bid at the favourite side's best bid (``replaces`` = the order it moves),
        or a skip reason."""
        p = self.params
        yes_px = q.bid if q.side == "yes" else ONE - q.bid
        if not m.is_valid_price(yes_px):
            return "price off the tick grid"
        n = int(D(p["order_dollars"]) // q.bid)
        if count is not None:
            n = min(n, count)
        if n < 1:
            return "order_dollars below one contract"
        if m.close_time is None:
            return "no close time"
        left_s = (m.close_time - now).total_seconds() - float(p["min_hours_to_close"]) * 3600
        life = float(p["expires_in_s"]) if can_cancel else min(float(p["expires_in_s"]), NO_CANCEL_EXPIRY_S)
        ttl = int(min(life, left_s))
        if ttl < MIN_EXPIRY_S:
            return "too close to the close cut-off"
        try:
            probe = D(ctx.fee(m, _PROBE_PRICE, _PROBE_COUNT, is_taker=False))
            fee = D(ctx.fee(m, q.bid, n, is_taker=False))  # 0, or sub-cent principal rounding
        except Exception:
            return "fee unavailable"
        if probe > ZERO:
            return "maker fee is not zero"
        prior = D(p["prior_edge"])
        edge = prior - fee / n
        fair = min(max(float(q.bid + prior), 0.0), 1.0)
        queue = book.size_at(q.side, q.bid)  # type: ignore[arg-type]
        hours = (m.close_time - now).total_seconds() / 3600
        close_txt = f"{hours:.1f} h" if hours < 48 else f"{hours / 24:.1f} d"
        sign = "+" if prior >= 0 else ""
        head = f"re-quote of order {replaces}: " if replaces is not None else ""
        reason = (f"EXPERIMENTAL maker favourite: {head}join the {q.side.upper()} best bid {q.bid} "
                  f"({_num(queue)} ahead), {meta.fee_type} series {m.series_ticker} ({meta.category}, "
                  f"maker fee 0), spread {_num(q.spread * 100)}c, closes in {close_txt}, vol24h "
                  f"{_num(m.volume_24h)}; edge {sign}{_num(prior * 100)}c/contract is a PRIOR, not measured here "
                  f"(literature +0.5...1c; the repo's candle maker model is negative in this band); hold to "
                  f"settlement")
        return OrderIntent(ticker=m.ticker, side=q.side, action="buy", count=n,  # type: ignore[arg-type]
                           limit_price=q.bid, tif="gtc", expires_in_s=ttl, strategy=self.name, reason=reason,
                           fair_value=fair, expected_edge=edge, replaces=replaces)

    # ------------------------------------------------------------------ resting orders

    async def _maintain(self, ctx: StrategyContext, o: Any, book: Orderbook | None, budget: list[int],
                        now: datetime) -> OrderIntent | CancelIntent | None:
        """Keep (None), cancel, or cancel/replace one resting order (context can cancel)."""
        p = self.params
        m = ctx.markets.get(o.ticker)
        if m is None:
            return None  # left the universe: the broker expires it at the close / when inactive
        min_h = float(p["min_hours_to_close"])
        if m.close_time is not None and m.close_time - now < timedelta(hours=min_h):
            hours = (m.close_time - now).total_seconds() / 3600
            return self._cancel(o, f"closes in {hours:.1f} h (< {min_h:g} h)")
        if book is None:
            return None
        side = o.buy_side
        lv = book.best_bid(side)
        if lv is None:
            return self._cancel(o, f"no {side.upper()} bid left")
        if abs(lv.price - o.buy_limit) <= D(p["stale_move"]):
            return None
        moved = f"best {side.upper()} bid moved {o.buy_limit} -> {lv.price}"
        q, why = pick_favourite(book.best_yes_bid, book.best_no_bid, min_bid=D(p["min_bid"]),
                                max_bid=D(p["max_bid"]), max_spread=D(p["max_spread"]))
        if q is None or q.side != side:
            return self._cancel(o, f"{moved}; {why or 'the favourite flipped'}")
        meta = await self._meta(ctx, m, budget, now)
        if isinstance(meta, str):
            return self._cancel(o, f"{moved}; cannot re-quote: {meta}")
        intent = self._intent(ctx, m, q, book, meta, now, can_cancel=True, count=o.remaining, replaces=o.id)
        if isinstance(intent, str):
            return self._cancel(o, f"{moved}; cannot re-quote: {intent}")
        return intent

    def _cancel(self, o: Any, why: str) -> CancelIntent:
        return CancelIntent(order_id=o.id, reason=f"maker favourite: {why}", strategy=self.name)

    def _review_resting(self, ctx: StrategyContext, orders: Iterable[Any]) -> None:
        """No-cancel mode: log (once per order) resting orders the rule would cancel or move."""
        orders = list(orders)
        self._flagged &= {o.id for o in orders}
        for o in orders:
            if o.id in self._flagged:
                continue
            why = self._stale_reason(ctx, o)
            if why:
                self._flagged.add(o.id)
                ctx.log(f"order {o.id} {o.ticker} {o.side} @ {o.limit_price} is stale ({why}); this context "
                        f"cannot cancel, so it rests until it expires at {iso(o.expires_at)}",
                        order_id=o.id, ticker=o.ticker, why=why)

    def _stale_reason(self, ctx: StrategyContext, o: Any) -> str:
        m = ctx.markets.get(o.ticker)
        if m is None:
            return ""
        p = self.params
        if m.close_time is not None and m.close_time - ctx.now < timedelta(hours=float(p["min_hours_to_close"])):
            return f"closes in {(m.close_time - ctx.now).total_seconds() / 3600:.1f} h"
        side = o.buy_side
        bid = m.bid(side)
        if bid is None:
            return f"no {side.upper()} bid"
        if abs(bid - o.buy_limit) > D(p["stale_move"]):
            return f"best {side.upper()} bid {bid} vs our {o.buy_limit}"
        return ""

    # ------------------------------------------------------------------ data access

    async def _lookup_series(self, ctx: StrategyContext, ticker: str, budget: list[int], now: datetime) -> Any:
        hit = self._series.get(ticker)
        if hit is not None and (now - hit[0]).total_seconds() <= META_TTL_S:
            return hit[1]
        failed = self._meta_fail.get("series:" + ticker)
        if failed is not None and (now - failed).total_seconds() < META_RETRY_S:
            return _FAILED
        if budget[0] <= 0:
            return _BUDGET
        budget[0] -= 1
        try:
            s = await ctx.series(ticker)
        except Exception:
            self._meta_fail["series:" + ticker] = now
            return _FAILED
        self._series[ticker] = (now, s)
        self._meta_fail.pop("series:" + ticker, None)
        return s

    async def _lookup_event(self, ctx: StrategyContext, event_ticker: str, budget: list[int], now: datetime) -> Any:
        """The event (fee overrides, category): the context's cache, ours, else ``ctx.event``.
        ``None`` when the context cannot fetch events (then the series alone decides)."""
        ev = (ctx.events or {}).get(event_ticker)
        if ev is not None:
            return ev
        hit = self._events.get(event_ticker)
        if hit is not None and (now - hit[0]).total_seconds() <= META_TTL_S:
            return hit[1]
        fetch = getattr(ctx, "event", None)
        if not callable(fetch):
            return None
        failed = self._meta_fail.get("event:" + event_ticker)
        if failed is not None and (now - failed).total_seconds() < META_RETRY_S:
            return _FAILED
        if budget[0] <= 0:
            return _BUDGET
        budget[0] -= 1
        try:
            ev = await fetch(event_ticker)
        except Exception:
            self._meta_fail["event:" + event_ticker] = now
            return _FAILED
        self._events[event_ticker] = (now, ev)
        self._meta_fail.pop("event:" + event_ticker, None)
        return ev

    @staticmethod
    async def _books(ctx: StrategyContext, tickers: list[str]) -> dict[str, Orderbook]:
        if not tickers:
            return {}
        batch = getattr(ctx, "orderbooks", None)
        if callable(batch):
            try:
                return dict(await batch(tickers))
            except Exception as e:
                ctx.log(f"order books unavailable: {type(e).__name__}: {e}")
                return {}
        res = await asyncio.gather(*(ctx.orderbook(t) for t in tickers), return_exceptions=True)
        return {t: b for t, b in zip(tickers, res, strict=True) if not isinstance(b, BaseException)}

    @staticmethod
    def _event_of(ctx: StrategyContext, x: Any) -> str:
        et = getattr(x, "event_ticker", "") or ""
        if et:
            return et
        m = ctx.markets.get(x.ticker)
        return m.event_ticker if m is not None else x.ticker

    def _prune(self, now: datetime) -> None:
        self._cooldown = {t: until for t, until in self._cooldown.items() if until > now}
        keep = timedelta(seconds=max(RETRY_S, int(self.params["expires_in_s"])))
        self._attempted = {t: ts for t, ts in self._attempted.items() if now - ts < keep}
        old = timedelta(seconds=2 * META_TTL_S)
        self._series = {k: v for k, v in self._series.items() if now - v[0] < old}
        self._events = {k: v for k, v in self._events.items() if now - v[0] < old}
        self._meta_fail = {k: ts for k, ts in self._meta_fail.items() if now - ts < old}
