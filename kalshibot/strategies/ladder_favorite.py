"""Ladder favourites near expiry: research rule **B4-ladder-72h**. EXPERIMENTAL.

It failed an independent holdout, so it is meant for forward paper-testing only, at small size.
Evidence is in ``research/FINDINGS.md`` and ``research/calibration/``: ``strategies.py`` and
``oos_eval.py`` (rule B4), ``verify_leakage/pooled.py`` and ``verify_stats/repro.py`` (the
ladder/72h cut) and ``verify_stats/VERDICT.md`` (the holdout).

Rule
----
* **Universe**: markets closing within 3 days (``UniverseSpec(max_days_to_close=3)``) that are
  ``active`` and before their close.
* **Excluded markets**:
  * multivariate (MVE) combo markets (``KXMVE*`` or an ``mve_collection_ticker``);
  * the series the research flagged as outcome-timing dependent (:data:`OUTCOME_TIMING_SERIES`,
    from ``research/calibration/series_flags.csv``, where ``outcome_dep`` is true);
  * anything listed in ``exclude_series``.
* **Category**: must be in ``categories``, by default Commodities, Financials, Crypto and
  Economics. It is the **series** category (``ctx.series``), which is what the research
  dataset used (``GET /series``). The cached event's category (``ctx.events``) is the fallback.
* **When**: on the research's grid. The rule was researched and backtested on hourly candles:
  it enters at the first UTC hour boundary whose *closing* quote qualifies. So the trigger is
  evaluated only on the first tick at or after each multiple of ``decision_grid_s`` (3600 s,
  UTC), on the book at that moment, and only when that tick is at most ``decision_window_s``
  (120 s) late (an engine that starts at :37 waits for the next hour). Triggers that did not
  fit ``max_intents_per_tick`` are carried to the next ticks of the same slot (re-checked on a
  fresh book); everything else waits for the next boundary. Between boundaries the strategy
  only resolves series categories (no books, no orders). ``decision_grid_s: 0`` evaluates
  every tick (continuous) - **untested**: intra-hour touches of 0.97 that fell back below it
  before the hour closed resolved YES only ~92% of the time (279 markets, 22 losers), against
  99.7% for the researched triggers, and ~23% of markets would be entered an hour earlier than
  the research did. Backtest a continuous variant before enabling it.
* **Trigger**: the *first* decision time at which a market meets all of the following. Each
  market is entered at most once, ever.
  * The order book is two-sided.
  * The best YES bid is at least ``min_yes_bid`` (0.97).
  * The best YES ask is below 1.00 (and above the bid).
  * ``expected_expiration_time - now`` is under ``max_hours_to_expiry`` (72h). A market past
    its EET but still open counts, as it did in the research.
* **Action**: buy YES at the best YES ask as a taker (IOC). The limit is that ask, so the order
  never walks the book. The position is held to settlement, with no exits.
  * A market counts as used once an intent for it is emitted. It is not retried after an
    unfilled or partial IOC or a risk rejection, because the research enters each market only
    at its first trigger.
* **Size**: ``floor(max_position_cost / ask)`` contracts (default $10: 10 contracts at
  0.97-0.99), cut further by
  * the displayed size at the ask;
  * what is left of the per-**event** cap ``max_event_cost`` ($20);
  * what is left of the per-**underlying** cap ``max_group_cost`` ($40). Many events settle on
    the same print: the national AAA gas series and ~15 state series, KXINXU / KXNASDAQ100U /
    KXDJI, KXWTI and KXBRENT, gold ladders on adjacent days. :data:`UNDERLYING_GROUPS` maps
    series prefixes to one group (other series are their own group);
  * what is left of the strategy total ``max_total_cost`` ($150).

  Each cap counts this strategy's open cost (positions + resting orders, ``ctx.portfolio``)
  plus the intents earlier in the same tick. The risk manager's limits (including this
  strategy's allocation, ``risk_defaults``) still apply on top.

  When several markets trigger in the same tick, the cheapest ask goes first, then the highest
  bid. If the result is under ``min_contracts`` (10), nothing is sent this tick and the market stays
  eligible. The reason is fee rounding: a 1-contract order at 0.98–0.99 pays a whole cent.
* **Model**: ``fair_value`` is a constant, the empirical win rate of this exact rule's trades.
  ``expected_edge`` is ``fair_value - ask - fee/contract``.

  | sample | trades | win rate |
  |---|---|---|
  | author window (Jul 28–Sep 25) | 2,949 | 0.998 |
  | independent holdout (May 20–Jul 27) | 824 | 0.977 |
  | pooled (the default) | 3,773 | **0.993** (25 losses in 12 events) |

  Break-even is about 0.991 at a 0.99 ask and 0.982 at a 0.98 ask (C=10 fee).

Why the event, group and total caps
-----------------------------------
Losses come in whole ladders, and ladders on one underlying lose together. In the holdout, one
gold ladder (KXGOLDD-26JUN1017) lost 10 strikes that had all been bought at 97-99c; that single
event cost as much as about 700 average winning trades, and gold ladders on consecutive days
(KXGOLDD-26JUN0917 and -26JUN1017, -26JUN1717 and -26JUN1817) lost together. In the
default-parameter backtest the gas family alone reached $446 open at once, $281 of it settling in
one hour. Evidence accrues per trade, so smaller positions buy more trades per dollar of tail
risk.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar

from kalshibot.money import ONE, ZERO, D
from kalshibot.strategies.base import OrderIntent, Strategy, StrategyContext, UniverseSpec

if TYPE_CHECKING:
    from kalshibot.kalshi.models import Market, Orderbook

__all__ = ["OUTCOME_TIMING_SERIES", "UNDERLYING_GROUPS", "LadderFavorite", "underlying_group"]

#: Series whose close timing depends on the outcome (research/calibration/series_flags.csv,
#: ``outcome_dep``: in these series, YES and NO markets close more than 12h apart relative to
#: the EET, or markets of one event close more than 12h apart). The research excluded them, so
#: the rule is not validated there. Only KX10YRDIRHM is in a default category, but the whole
#: list applies, so changing ``categories`` cannot bring these series back in.
OUTCOME_TIMING_SERIES: frozenset[str] = frozenset({
    "KX10YRDIRHM", "KXALLINMENTION", "KXARTISTSTREAMSY", "KXATP", "KXATPADVANCE", "KXCARNEYMENTION",
    "KXCHAMPTOUR", "KXCLAUDE", "KXDPWORLDTOUR", "KXHORMUZWEEKLY", "KXLASTWORDMENTION",
    "KXLIUSAELIMINATIONW", "KXLIUSAWINNERS", "KXLOVEISLANDUSARANK", "KXLOVEISLMENTION", "KXMAMDANIEO",
    "KXMCDONALDCONF", "KXMEDIARELEASEDATEAHS", "KXMLBMENTION", "KXMLBPLAYOFFS", "KXMLBWINS-AZ",
    "KXMLBWINS-PIT", "KXNASCARTOP10", "KXNBAPICKTRADE", "KXNBATEAMANNOUNCE", "KXNFLWINSWEEK",
    "KXPGABOGEYFREE", "KXPGAHOLESCORE", "KXPGAR3TOP10", "KXPGATOP10", "KXPGATOP20", "KXPGATOP5",
    "KXPGATOUR", "KXPLATNERREPLACE", "KXPRIMARYMOV", "KXPRIMARYPLACE", "KXPSAKIMENTION",
    "KXSECPRESSMENTION", "KXTRUMPMENTION", "KXTRUMPMENTIONB", "KXTRUMPSAY", "KXTRUMPTIME",
    "KXTRUTHSOCIAL", "KXUAPFILES", "KXURYPDSPREAD", "KXVANCEMENTION", "KXWCGOALCOUNT", "KXWCMENTION",
    "KXWNBAMENTION", "KXWNBAWINS", "KXWNBAWORSTREC", "KXWORLDNEWSMENTION", "KXWTA", "KXWTAADVANCE",
    "KXYTVIEWSW",
})

LADDER_CATEGORIES = ["Commodities", "Financials", "Crypto", "Economics"]

#: Series prefixes whose markets settle on the same underlying price (first match wins); they
#: share ``max_group_cost``. Any other series is a group of its own.
UNDERLYING_GROUPS: tuple[tuple[str, str], ...] = (
    ("KXAAAGAS", "gas"), ("KXWTI", "oil"), ("KXBRENT", "oil"), ("KXNATGAS", "natgas"), ("KXGOLD", "gold"),
    ("KXSILVER", "silver"), ("KXCOPPER", "copper"), ("KXINX", "us_equity"), ("KXNASDAQ100", "us_equity"),
    ("KXDJI", "us_equity"), ("KXBTC", "btc"), ("KXETH", "eth"), ("KXUST", "rates"),
)


def underlying_group(ticker: str) -> str:
    """The underlying group of a series / event / market ticker (e.g. ``KXAAAGASDFL-26SEP05`` -> ``gas``)."""
    series = str(ticker or "").split("-")[0].upper()
    for prefix, group in UNDERLYING_GROUPS:
        if series.startswith(prefix):
            return group
    return series

#: new series-category lookups per tick (each may be one request; the public API budget is ~3/s)
MAX_SERIES_LOOKUPS_PER_TICK = 25
#: wait before retrying a series whose category could not be resolved
SERIES_RETRY = timedelta(minutes=10)
#: forget "already entered" markets this long after entry once they left the universe
ENTERED_TTL = timedelta(days=14)


def _is_mve(m: Market) -> bool:
    raw = m.raw if isinstance(m.raw, Mapping) else {}
    return (m.ticker.upper().startswith("KXMVE") or m.series_ticker.upper().startswith("KXMVE")
            or bool(raw.get("mve_collection_ticker")))


def _parse_ts(s: Any) -> datetime | None:
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class LadderFavorite(Strategy):
    name: ClassVar[str] = "ladder_favorite"
    description: ClassVar[str] = (
        "EXPERIMENTAL: failed an independent holdout, so forward paper-test only. "
        "Research rule B4-ladder-72h. At each UTC hour boundary (the research's hourly grid), the first time a "
        "Commodities, Financials, Crypto or Economics market (not MVE, not an outcome-timing-dependent series) "
        "has a two-sided book with YES bid >= 0.97 and YES ask < 1.00 within 72h of its expected expiration, "
        "buy YES at the ask as a taker (at least 10 contracts) and hold to settlement. Each market is entered "
        "once. Dollar caps per position, per event, per underlying (gas, oil, gold, US equity indexes, ...) "
        "and in total apply because losses cluster within a ladder and across ladders on one underlying. "
        "Evidence: in the author's window, TRAIN +1.08 and TEST +1.07 c/contract after fees; in the "
        "independent May-Jul holdout, -0.9 (CI -3.8 to +1.0); pooled +0.6 (CI -0.04 to +1.1)."
    )
    backtestable: ClassVar[bool] = True
    experimental: ClassVar[bool] = True
    #: forward paper-test at small size (the dollar caps below) unless turned off
    enabled_by_default: ClassVar[bool] = True
    risk_defaults: ClassVar[dict[str, Any]] = {"max_allocation_pct": 15, "daily_loss_limit": 45}
    default_params: ClassVar[dict[str, Any]] = {
        "min_yes_bid": 0.97,
        "max_hours_to_expiry": 72.0,
        "categories": list(LADDER_CATEGORIES),
        "exclude_series": [],
        "fair_value": 0.993,
        "max_position_cost": 10.0,
        "max_event_cost": 20.0,
        "max_group_cost": 40.0,
        "max_total_cost": 150.0,
        "min_contracts": 10,
        "max_intents_per_tick": 5,
        "decision_grid_s": 3600.0,
        "decision_window_s": 120.0,
    }
    param_schema: ClassVar[dict[str, dict[str, Any]]] = {
        "min_yes_bid": {"type": "float", "min": 0.5, "max": 0.999,
                        "help": "Trigger when the best YES bid is at least this and the YES ask is below 1.00 "
                                "(research: 0.97)"},
        "max_hours_to_expiry": {"type": "float", "min": 0.5, "max": 72,
                                "help": "Only markets whose expected_expiration_time is less than this many "
                                        "hours away (research: 72; the universe is limited to 3 days)"},
        "categories": {"type": "list",
                       "help": "Series categories to trade (research: Commodities, Financials, Crypto, Economics)"},
        "exclude_series": {"type": "list",
                           "help": "Extra series tickers to skip (the research's outcome-timing-dependent "
                                   "series are always skipped)"},
        "fair_value": {"type": "float", "min": 0.5, "max": 0.9999,
                       "help": "P(YES wins) used for fair_value and expected_edge. The default 0.993 is the "
                               "pooled empirical win rate of this rule: 0.998 in the author window (n=2,949) "
                               "and 0.977 in the independent holdout (n=824)"},
        "max_position_cost": {"type": "float", "min": 1, "max": 1000,
                              "help": "Dollar cap per market (contracts = floor(cap / ask))"},
        "max_event_cost": {"type": "float", "min": 1, "max": 10000,
                           "help": "Dollar cap on this strategy's open cost per event, because losses cluster "
                                   "within a ladder"},
        "max_group_cost": {"type": "float", "min": 1, "max": 1000000,
                           "help": "Dollar cap on this strategy's open cost per underlying (all AAA gas series, "
                                   "WTI + Brent, gold, US equity indexes, ...), because ladders on one "
                                   "underlying lose together"},
        "max_total_cost": {"type": "float", "min": 1, "max": 1000000,
                           "help": "Dollar cap on this strategy's total open cost (positions + resting orders)"},
        "min_contracts": {"type": "int", "min": 1, "max": 1000,
                          "help": "Smallest order sent. Smaller orders lose to the per-order fee rounding "
                                  "at 98-99c"},
        "max_intents_per_tick": {"type": "int", "min": 1, "max": 50,
                                 "help": "Most entries per tick. The rest are sent on the next ticks of the same "
                                         "decision slot (re-checked on a fresh book) and are not used up"},
        "decision_grid_s": {"type": "float", "min": 0, "max": 86400,
                            "help": "Evaluate the trigger only on the first tick at/after each multiple of this "
                                    "many seconds (UTC), like the research's hourly candle close (3600). 0 = "
                                    "every tick (continuous: UNTESTED, intra-hour touches of 0.97 resolved YES "
                                    "only ~92% of the time)"},
        "decision_window_s": {"type": "float", "min": 1, "max": 3600,
                              "help": "A first tick more than this many seconds after the grid time skips that "
                                      "decision slot (the engine started or ran late)"},
    }

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        super().__init__(params)
        self._entered: dict[str, datetime] = {}  # ticker -> first-trigger time (one entry ever)
        self._category: dict[str, str] = {}  # series ticker -> category
        self._category_fail: dict[str, datetime] = {}
        self._noted: set[tuple[str, str]] = set()  # (kind, key) already logged
        self._slot: int | None = None  # current decision slot (epoch s of its grid time)
        self._carry: list[str] = []  # triggers of the current slot deferred by max_intents_per_tick

    def universe(self) -> UniverseSpec:
        return UniverseSpec(max_days_to_close=3)

    # ------------------------------------------------------------------ state

    def dump_state(self) -> Any:
        # a fresh dict every time: the engine saves only when the state compares unequal
        return {"entered": {t: ts.isoformat() for t, ts in sorted(self._entered.items())}}

    def load_state(self, state: Any) -> None:
        entered = state.get("entered") if isinstance(state, Mapping) else None
        if isinstance(entered, Mapping):
            for t, s in entered.items():
                ts = _parse_ts(s)
                if t and ts is not None:
                    self._entered[str(t)] = ts

    def has_entered(self, ticker: str) -> bool:
        return ticker in self._entered

    # ------------------------------------------------------------------ tick

    async def on_tick(self, ctx: StrategyContext) -> list[OrderIntent]:
        now = ctx.now
        self._prune(now, ctx.markets)
        grid = float(self.params["decision_grid_s"])
        only: set[str] | None = None
        if grid > 0:
            ts = now.timestamp()
            slot = int(ts // grid * grid)
            if slot != self._slot:
                self._slot, self._carry = slot, []
                late = ts - slot
                if late > float(self.params["decision_window_s"]):
                    at = datetime.fromtimestamp(slot, tz=UTC).strftime("%H:%M")
                    nxt = datetime.fromtimestamp(slot + grid, tz=UTC).strftime("%H:%M")
                    self._note(ctx, "late", str(slot), f"first tick {late:.0f}s after the {at} UTC decision time "
                               f"(window {float(self.params['decision_window_s']):g}s): slot skipped; next "
                               f"decision at {nxt} UTC")
                    await self._scan(ctx, None, prewarm=True)
                    return []
            elif self._carry:
                only = set(self._carry)
            else:
                await self._scan(ctx, None, prewarm=True)  # categories only: no books, no orders
                return []
        intents, deferred = await self._scan(ctx, only)
        self._carry = deferred if grid > 0 else []
        return intents

    async def _scan(self, ctx: StrategyContext, only: set[str] | None, *, prewarm: bool = False
                    ) -> tuple[list[OrderIntent], list[str]]:
        """Steps 1-4 on ``ctx`` (restricted to ``only`` when given). ``prewarm``: steps 1-2 only
        (resolve series categories ahead of the next decision time). Returns (intents, triggers
        deferred by ``max_intents_per_tick``)."""
        p = self.params
        now = ctx.now
        min_bid = D(p["min_yes_bid"])
        max_h = float(p["max_hours_to_expiry"])
        cats = {str(c).strip().casefold() for c in p["categories"] or () if str(c).strip()}
        excluded = OUTCOME_TIMING_SERIES | {str(s).strip().upper() for s in p["exclude_series"] or ()}
        pos_cap = D(p["max_position_cost"])
        event_cap = D(p["max_event_cost"])
        group_cap = D(p["max_group_cost"])
        total_cap = D(p["max_total_cost"])
        min_n = int(p["min_contracts"])
        portfolio = ctx.portfolio

        # 1. cheap filters on the market snapshot (no requests)
        pre: list[Market] = []
        for t in sorted(ctx.markets):
            if only is not None and t not in only:
                continue
            m = ctx.markets[t]
            if t in self._entered:
                continue
            if portfolio is not None and (portfolio.holds(t, self.name) or portfolio.has_open_order(t, self.name)):
                self._entered.setdefault(t, now)  # restarted without saved state
                continue
            if _is_mve(m) or m.series_ticker.upper() in excluded or not m.is_tradable(now):
                continue
            if m.expected_expiration_time is None:
                continue
            h = (m.expected_expiration_time - now).total_seconds() / 3600
            if not h < max_h:
                continue
            if m.yes_bid is None or m.yes_bid < min_bid or m.yes_ask is None or not m.yes_ask < ONE:
                continue
            pre.append(m)
        if not pre:
            return [], []

        # 2. category from the series (the research's source), falling back to the cached event
        series_cat = await self._categories(ctx, {m.series_ticker for m in pre})
        if prewarm:
            return [], []
        cands: list[tuple[Market, str]] = []
        for m in pre:
            cat = series_cat.get(m.series_ticker) or self._event_category(ctx, m)
            if cat and cat.casefold() in cats:
                cands.append((m, cat))
        if not cands:
            return [], []

        # 3. skip triggers whose caps have no room for a minimum order (before fetching books)
        exposure: dict[str, Decimal] = {}
        for m, _ in cands:
            if m.event_ticker not in exposure:
                exposure[m.event_ticker] = (portfolio.exposure(event_ticker=m.event_ticker, strategy=self.name)
                                            if portfolio is not None else ZERO)
        groups = self._group_exposure(portfolio)
        total = portfolio.exposure(strategy=self.name) if portfolio is not None else ZERO
        live: list[tuple[Market, str]] = []
        for m, cat in cands:
            need = min_n * (m.yes_ask or ONE)
            g = underlying_group(m.event_ticker or m.ticker)
            if event_cap - exposure[m.event_ticker] < need:
                self._note(ctx, "event_cap", m.event_ticker,
                           f"event cap ${event_cap} reached for {m.event_ticker}; skipping its other triggers",
                           event_ticker=m.event_ticker, exposure=float(exposure[m.event_ticker]))
                continue
            if group_cap - groups.get(g, ZERO) < need:
                self._note(ctx, "group_cap", g, f"underlying cap ${group_cap} reached for {g}; skipping its "
                           "other triggers", group=g, exposure=float(groups.get(g, ZERO)))
                continue
            if total_cap - total < need:
                self._note(ctx, "total_cap", "", f"strategy total cap ${total_cap} reached; no new entries",
                           exposure=float(total))
                continue
            live.append((m, cat))
        if not live:
            return [], []

        # 4. confirm the trigger on the order book and size the orders
        books = await self._books(ctx, [m.ticker for m, _ in live])
        found: list[tuple[Decimal, Decimal, str, Market, str, int]] = []
        for m, cat in live:
            book = books.get(m.ticker)
            if book is None:
                continue
            bid, ask_lv = book.best_yes_bid, book.best_ask("yes")
            if bid is None or ask_lv is None:  # one-sided book
                continue
            ask = ask_lv.price
            if bid < min_bid or not ask < ONE or not bid < ask or not m.is_valid_price(ask):
                continue
            found.append((ask, bid, m.ticker, m, cat, int(ask_lv.size)))
        found.sort(key=lambda x: (x[0], -x[1], x[2]))  # cheapest ask, then the deepest bid

        intents: list[OrderIntent] = []
        deferred: list[str] = []
        pending: dict[str, Decimal] = {}
        pending_g: dict[str, Decimal] = {}
        pending_total = ZERO
        for ask, bid, ticker, m, cat, depth in found:
            if len(intents) >= int(p["max_intents_per_tick"]):
                deferred.append(ticker)  # sent on the next ticks of this decision slot
                continue
            g = underlying_group(m.event_ticker or m.ticker)
            used = exposure[m.event_ticker] + pending.get(m.event_ticker, ZERO)
            used_g = groups.get(g, ZERO) + pending_g.get(g, ZERO)
            used_t = total + pending_total
            rooms = {"event": event_cap - used, "group": group_cap - used_g, "total": total_cap - used_t}
            by_position = int(pos_cap / ask)
            count = min(by_position, depth, *(int(r / ask) if r > 0 else 0 for r in rooms.values()))
            if count < min_n:
                tight = min(rooms, key=lambda k: rooms[k])
                if by_position < min_n:
                    self._note(ctx, "position_cap", "", f"max_position_cost ${pos_cap} buys fewer than "
                               f"min_contracts ({min_n}) at {ask}; no orders")
                elif rooms[tight] < min_n * ask and tight == "event":
                    self._note(ctx, "event_cap", m.event_ticker,
                               f"event cap ${event_cap} reached for {m.event_ticker}; skipping its other triggers",
                               event_ticker=m.event_ticker, exposure=float(used))
                elif rooms[tight] < min_n * ask and tight == "group":
                    self._note(ctx, "group_cap", g, f"underlying cap ${group_cap} reached for {g}; skipping its "
                               "other triggers", group=g, exposure=float(used_g))
                elif rooms[tight] < min_n * ask:
                    self._note(ctx, "total_cap", "", f"strategy total cap ${total_cap} reached; no new entries",
                               exposure=float(used_t))
                else:
                    self._note(ctx, "thin", ticker, f"{ticker}: only {depth} contracts at the {ask} ask "
                               f"(min {min_n}); waiting", ticker=ticker)
                continue
            cost = count * ask
            caps = (f"event {m.event_ticker} ${used + cost:.2f} of ${event_cap}; {g} ${used_g + cost:.2f} of "
                    f"${group_cap}; strategy ${used_t + cost:.2f} of ${total_cap}")
            intents.append(self._intent(ctx, m, cat, ask, bid, count, caps))
            pending[m.event_ticker] = pending.get(m.event_ticker, ZERO) + cost
            pending_g[g] = pending_g.get(g, ZERO) + cost
            pending_total += cost

        for it in intents:  # commit only once the whole tick went through: one entry per market, ever
            self._entered[it.ticker] = now
        return intents, deferred

    def _group_exposure(self, portfolio: Any) -> dict[str, Decimal]:
        """This strategy's open cost (positions + resting orders) per underlying group."""
        out: dict[str, Decimal] = {}
        if portfolio is None:
            return out
        for pos in getattr(portfolio, "positions", ()) or ():
            if pos.strategy == self.name and pos.count > 0:
                g = underlying_group(pos.event_ticker or pos.ticker)
                out[g] = out.get(g, ZERO) + D(pos.cost_basis)
        for o in getattr(portfolio, "open_orders", ()) or ():
            if o.strategy == self.name:
                g = underlying_group(getattr(o, "event_ticker", "") or o.ticker)
                out[g] = out.get(g, ZERO) + D(getattr(o, "reserved", ZERO) or ZERO)
        return out

    # ------------------------------------------------------------------ helpers

    def _intent(self, ctx: StrategyContext, m: Market, cat: str, ask: Decimal, bid: Decimal, count: int,
                caps: str) -> OrderIntent:
        fv = float(self.params["fair_value"])
        fee = D(ctx.fee(m, ask, count, is_taker=True))
        edge = (D(fv) - ask - fee / count).quantize(Decimal("0.000001"))
        eet = m.expected_expiration_time
        h = (eet - ctx.now).total_seconds() / 3600 if eet is not None else float("nan")
        reason = (
            f"ladder favourite (B4-ladder-72h, experimental): YES bid {bid} >= {D(self.params['min_yes_bid'])}, "
            f"ask {ask} < 1, EET in {h:.1f}h; {cat} {m.series_ticker}; buy {count} YES @ {ask}, hold to "
            f"settlement; fair {fv:g} (pooled empirical win rate), fee ${fee}; {caps}"
        )
        return OrderIntent(
            ticker=m.ticker, side="yes", action="buy", count=count, limit_price=ask, tif="ioc",
            strategy=self.name, reason=reason, fair_value=fv, expected_edge=edge,
        )

    async def _categories(self, ctx: StrategyContext, series: Iterable[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        todo: list[str] = []
        for s in sorted(x for x in series if x):
            if s in self._category:
                out[s] = self._category[s]
                continue
            failed = self._category_fail.get(s)
            if failed is None or ctx.now - failed >= SERIES_RETRY:
                todo.append(s)
        todo = todo[:MAX_SERIES_LOOKUPS_PER_TICK]
        if todo:
            got = await asyncio.gather(*(ctx.series(s) for s in todo), return_exceptions=True)
            for s, r in zip(todo, got, strict=True):
                cat = "" if isinstance(r, BaseException) else str(getattr(r, "category", "") or "")
                if cat:
                    self._category[s] = out[s] = cat
                    self._category_fail.pop(s, None)
                else:
                    self._category_fail[s] = ctx.now
        return out

    @staticmethod
    def _event_category(ctx: StrategyContext, m: Market) -> str:
        events = getattr(ctx, "events", None) or {}
        ev = events.get(m.event_ticker)
        return str(getattr(ev, "category", "") or "") if ev is not None else ""

    @staticmethod
    async def _books(ctx: StrategyContext, tickers: list[str]) -> dict[str, Orderbook]:
        batch = getattr(ctx, "orderbooks", None)
        if callable(batch):
            try:
                return dict(await batch(tickers))
            except Exception as e:  # next tick retries
                ctx.log(f"order books unavailable: {type(e).__name__}: {e}")
                return {}
        got = await asyncio.gather(*(ctx.orderbook(t) for t in tickers), return_exceptions=True)
        return {t: b for t, b in zip(tickers, got, strict=True) if not isinstance(b, BaseException)}

    def _note(self, ctx: StrategyContext, kind: str, key: str, msg: str, **data: Any) -> None:
        if (kind, key) in self._noted:
            return
        self._noted.add((kind, key))
        ctx.log(msg, **data)

    def _prune(self, now: datetime, markets: Mapping[str, Any]) -> None:
        for t in [t for t, ts in self._entered.items() if now - ts > ENTERED_TTL and t not in markets]:
            del self._entered[t]
        if len(self._noted) > 10_000:
            self._noted.clear()
