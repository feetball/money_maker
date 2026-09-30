"""btc_trend - BTC-USD 100-day moving-average trend filter with a +/-2% hysteresis buffer.

PAPER TRADING ONLY. Risk overlay, not an edge (research/coinbase/FINDINGS.md): in the
2024-01-01 -> 2026-09-25 out-of-sample test it returned 29.1%/yr against 28.6%/yr for simply
holding BTC (difference not significant: excess +0.5%/yr, 95% CI [-27, +42]) with about half
the drawdown (-32% vs -53%), ~3 round trips per year, after the 0.90% Intro taker fee. Fragile:
neighbouring parameter settings did mostly worse. Those are research figures for 100% of the
strategy's own money; the app's backtest (starts in cash, marks holdings after the exit fee)
shows ~28.3%/yr vs ~27.7%/yr for BTC over the same window, and live each figure applies only to
this strategy's allocation (a slice of the account).

Rule (exactly the researched configuration ``A|BTC-USD|sma(100, 0.02)``,
``research/coinbase/strategies/common.py::trend_state``/``hysteresis``)
    On every daily UTC bar close (00:00 UTC), with ``SMA`` = mean of the last ``sma_n``
    closes including today's:

    * ``close > (1 + buffer) x SMA``  -> hold BTC: target 100% of the allocation;
    * ``close < (1 - buffer) x SMA``  -> go to cash: target 0%;
    * otherwise (inside the band)     -> keep the previous state.

    Research decided at the close of day t and filled at the OPEN of day t+1, i.e. a few
    seconds after 00:00 UTC. The live engine runs the strategy ``coinbase.engine.bar_delay_s``
    (default 60 s) after the daily close, close enough (the backtester fills at the open itself,
    ``fill_price="pessimistic"`` bounds the difference). **Timing matters**: acting one day late cut
    the test return from 29.1% to 22%/yr (-7 pts/yr). Late decisions are still taken (a late
    trade beats a skipped one), but every target's reason says how late it was when the
    decision comes more than :attr:`BtcTrend.late_after_s` after the close and acts on that
    close's signal (a first entry on an older signal, e.g. right after enabling the strategy,
    is not "late" in that sense and carries no note).

Missing final bar
    If the candle of the bar that just closed is not published (``candles[-1].end <
    ctx.bar_end``: the engine gave up waiting for it), the strategy makes **no change** and logs
    which bar is missing and which close it would otherwise have used - it never silently
    decides on the previous day's close. Gaps further back in the history are tolerated (the
    SMA uses the candles that exist; research forward-filled up to ``MAX_STALE_BARS`` days).

State without memory (restart-consistent)
    The in/out state is recomputed on every bar from candles alone: the hysteresis is replayed
    over the last ``history_bars`` closes (``sma_n + 150``; in 2015-2026 BTC never stayed
    inside the +/-2% band for more than 20 consecutive days), so the state is the direction of
    the most recent close outside the band. Only if no close in that window left the band does
    the strategy fall back to the current holdings (>= 50% of the allocation in BTC = "in").
    Nothing is persisted, so a restart, a missed bar or a parameter edit gives the same answer
    as an uninterrupted run over the same candles.

Orders: entries target 100% of the allocation, exits 0%. While "in" and holding any BTC (at
least the minimum trade, :func:`holds_nothing`), ``on_bar`` returns ``None`` (no resize): the
research traded only on entries and exits (after entry its weight stays 1), and topping up /
trimming against the engine's allocation equity (a share of the whole account, which moves
with the other strategies) would churn fees. So the planner only ever enters from nothing or
fully exits. Consequences: a partly filled entry is not topped up, and a changed
``max_allocation_pct`` applies at the next entry (a held position is never resized). While
"out" it returns a 0% target (sells any BTC it holds; nothing to do when flat).

Execution: ``taker`` (market orders, as tested) by default; ``maker_then_taker`` (post-only at
the touch for ``coinbase.engine.maker_timeout_s``, then taker) is an option - the research
estimates maker fills at 0.50% would add ~3 pts/yr, but they were not tested with real
queue positions.

Every target carries a plain reason, e.g.
``BTC close 86,000 (2024-04-10) > 1.02xSMA100 84,762 (SMA100 83,100) -> hold BTC``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

from kalshibot.coinbase.strategies.base import SpotContext, SpotStrategy, TargetWeight

__all__ = [
    "BTC",
    "BtcTrend",
    "TrendState",
    "annualized_vol",
    "closes_of",
    "current_weight",
    "final_bar_problem",
    "fmt_px",
    "held_value",
    "holds_nothing",
    "late_note",
    "trend_state",
]

BTC = "BTC-USD"
DAY_S = 86400
#: how far behind ``bar_end`` the newest candle may be before it is reported as "stale" rather
#: than "not published yet" (research: ``load_daily(fill_gaps=3)``). Either way the strategies
#: make no change when the final bar is missing (module doc); gaps further back are tolerated.
MAX_STALE_BARS = 3
#: a holding worth less than this (USD) counts as "holds nothing" - the default
#: ``coinbase.risk.min_trade_usd`` (the planner treats anything below its minimum trade as an
#: entry from zero, too); a product's ``min_market_funds`` raises it
DUST_USD = Decimal(10)
#: lateness notes (see :func:`late_note`)
BTC_DELAY_NOTE = "research: acting a day late cost ~7 pts/yr"


# --------------------------------------------------------------------------- shared helpers


def closes_of(candles: Sequence[Any]) -> list[float]:
    """Closing prices as floats (oldest first)."""
    return [float(c.close) for c in candles]


def fmt_px(x: float) -> str:
    """Human price: ``84,470`` (>= 1000), ``2,512.35`` (>= 1), else 6 significant digits."""
    if not math.isfinite(x):
        return "n/a"
    if abs(x) >= 1000:
        return f"{x:,.0f}"
    if abs(x) >= 1:
        return f"{x:,.2f}"
    return f"{x:.6g}"


def _mult(x: float) -> str:
    """``1.02``, ``0.98``, ``1`` - the band multiplier as written in reasons."""
    return f"{x:.4f}".rstrip("0").rstrip(".")


class TrendState:
    """Result of :func:`trend_state`.

    ``state``: True (in the market), False (cash) or None (no close left the band inside the
    window - undetermined). ``signal_index``: index into the closes of the most recent close
    outside the band (None if none). ``sma``: the SMA at the last close (NaN if too short).
    """

    __slots__ = ("signal_index", "sma", "state")

    def __init__(self, state: bool | None, signal_index: int | None, sma: float) -> None:
        self.state = state
        self.signal_index = signal_index
        self.sma = sma

    def __repr__(self) -> str:
        return f"TrendState(state={self.state}, signal_index={self.signal_index}, sma={self.sma:.6g})"


def trend_state(closes: Sequence[float], n: int, buffer: float) -> TrendState:
    """Replay the SMA-hysteresis state machine over ``closes`` (oldest first).

    At every index ``i >= n - 1``: ``sma_i`` = mean of ``closes[i-n+1 .. i]``; enter when
    ``close_i > (1 + buffer) * sma_i``, exit when ``close_i < (1 - buffer) * sma_i``, keep the
    state otherwise (``buffer = 0``: a plain close-vs-SMA signal; a close exactly on the SMA
    keeps the state). Identical to ``research/coinbase/strategies/common.py::hysteresis``
    except that the state before the first decisive close is "unknown" (None) instead of 0,
    so the caller can fall back to its holdings instead of assuming cash.
    """
    n = int(n)
    m = len(closes)
    if n < 1 or m < n:
        return TrendState(None, None, math.nan)
    up, dn = 1.0 + buffer, 1.0 - buffer
    state: bool | None = None
    sig: int | None = None
    s = math.fsum(closes[:n])
    sma = math.nan
    for i in range(n - 1, m):
        if i >= n:
            s += closes[i] - closes[i - n]
        sma = s / n
        c = closes[i]
        if c > up * sma:
            state, sig = True, i
        elif c < dn * sma:
            state, sig = False, i
    # re-sum the final window exactly (the running sum accumulates float error)
    sma = math.fsum(closes[m - n:]) / n
    return TrendState(state, sig, sma)


def annualized_vol(closes: Sequence[float], window: int = 30, min_periods: int = 20,
                   periods_per_year: float = 365.0) -> float:
    """Sample stdev (ddof=1) of the last ``window`` close-to-close returns x sqrt(365).

    Same as research ``ret_cc.rolling(30, min_periods=20).std() * sqrt(365)``: NaN when fewer
    than ``min_periods`` returns exist.
    """
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(max(1, len(closes) - window), len(closes))
            if closes[i - 1] > 0]
    if len(rets) < max(2, min_periods):
        return math.nan
    mu = math.fsum(rets) / len(rets)
    var = math.fsum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(max(0.0, var)) * math.sqrt(periods_per_year)


def held_value(ctx: SpotContext, product_id: str) -> Decimal:
    """USD value of this strategy's ``product_id`` holding (0 when unknown)."""
    try:
        pf = ctx.portfolio
        values = getattr(pf, "values", None)
        val = values.get(product_id) if values is not None else None
        if val is None:
            qty = Decimal(pf.quantity(product_id)) if hasattr(pf, "quantity") else Decimal(0)
            if qty <= 0:
                return Decimal(0)
            px = pf.price(product_id) if hasattr(pf, "price") else None
            if px is None:
                bars = ctx.candles(product_id, 1)
                px = bars[-1].close if bars else None
            val = qty * Decimal(px) if px is not None else Decimal(0)
        val = Decimal(val)
        return val if val.is_finite() and val > 0 else Decimal(0)
    except (ArithmeticError, AttributeError, TypeError, ValueError):
        return Decimal(0)


def current_weight(ctx: SpotContext, product_id: str) -> float:
    """This strategy's holding of ``product_id`` as a fraction of its allocation equity."""
    try:
        alloc = Decimal(ctx.portfolio.alloc_equity)
        if alloc <= 0:
            return 0.0
        return float(held_value(ctx, product_id) / alloc)
    except (ArithmeticError, AttributeError, TypeError, ValueError):
        return 0.0


def holds_nothing(ctx: SpotContext, product_id: str) -> bool:
    """True when the holding is below the minimum trade (:data:`DUST_USD`, or the product's
    ``min_market_funds`` if larger): the planner would treat a buy as an entry from zero."""
    floor = DUST_USD
    try:
        mmf = getattr(ctx.products.get(product_id), "min_market_funds", None)
        if mmf is not None and Decimal(mmf) > floor:
            floor = Decimal(mmf)
    except (ArithmeticError, AttributeError, TypeError, ValueError):
        pass
    return held_value(ctx, product_id) < floor


def _bar_label(start: datetime, granularity_s: int) -> str:
    return start.date().isoformat() if granularity_s >= DAY_S else f"{start:%Y-%m-%d %H:%M} UTC"


def final_bar_problem(bars: Sequence[Any], bar_end: datetime, granularity_s: int = DAY_S,
                      max_stale_bars: int = MAX_STALE_BARS) -> str | None:
    """Why the strategy must not decide on these candles (None = the bar closing at
    ``bar_end`` is the newest candle): no candles, or the final bar is not published (the
    engine gave up waiting), or the candles are stale (more than ``max_stale_bars`` behind)."""
    if not bars:
        return "no candles"
    last = bars[-1]
    if last.end >= bar_end:
        return None
    kind = "daily" if granularity_s >= DAY_S else "hourly"
    want = _bar_label(bar_end - timedelta(seconds=granularity_s), granularity_s)
    have = _bar_label(last.start, granularity_s)
    if last.end < bar_end - timedelta(seconds=granularity_s * max_stale_bars):
        return (f"candles stale: latest candle: the {have} close, more than {max_stale_bars} bars before "
                f"the {want} {kind} bar")
    return (f"the {want} {kind} bar (closed {bar_end:%Y-%m-%d %H:%M} UTC) is not published; "
            f"latest candle: the {have} close")


def stale_reason(bars: Sequence[Any], bar_end: datetime, granularity_s: int = DAY_S,
                 max_stale_bars: int = MAX_STALE_BARS) -> str | None:
    """Why the candles are too old to decide on (None = fresh enough)."""
    if not bars:
        return "no candles"
    last_end = bars[-1].end
    if last_end < bar_end - timedelta(seconds=granularity_s * max_stale_bars):
        return f"latest bar closed {last_end.isoformat()}, more than {max_stale_bars} bars before {bar_end.isoformat()}"
    return None


def late_note(ctx: SpotContext, late_after_s: float, cost_note: str = BTC_DELAY_NOTE) -> str:
    """``" [decided 5.2 h after the ... close; <cost_note>]"`` when the decision comes more than
    ``late_after_s`` after the bar close, else ``""``. ``cost_note`` says what research
    measured for *this* strategy (each strategy passes its own)."""
    try:
        lag = (ctx.now - ctx.bar_end).total_seconds()
    except (AttributeError, TypeError):
        return ""
    if lag <= late_after_s:
        return ""
    tail = f"; {cost_note}" if cost_note else ""
    return f" [decided {lag / 3600:.1f} h after the {ctx.bar_end:%Y-%m-%d %H:%M} UTC close{tail}]"


# --------------------------------------------------------------------------- strategy


class BtcTrend(SpotStrategy):
    name: ClassVar[str] = "btc_trend"
    description: ClassVar[str] = (
        "Risk overlay, not an edge: in 2024–26 testing it matched BTC buy-and-hold's return "
        "(29.1% vs 28.6%/yr, difference not significant) with about half the drawdown "
        "(−32% vs −53%) (research figures on the strategy's own money; the app's backtest from "
        "cash: ~28.3% vs 27.7%/yr). Fragile: neighbouring parameter settings did mostly worse. "
        "Holds 100% BTC-USD of its allocation while the daily close is above 1.02×SMA100, moves "
        "to cash when it closes below 0.98×SMA100, otherwise keeps its position; decides right "
        "after the 00:00 UTC daily close (acting a day late cost ~7 pts/yr in testing). Only "
        "enters or fully exits: an allocation change applies at the next entry."
    )
    experimental: ClassVar[bool] = False
    #: on unless the dashboard or the config turns it off (like the Kalshi research strategies)
    enabled_by_default: ClassVar[bool] = True
    #: 25% of the Coinbase account: with btc_hold (15%) both BTC sleeves stay 10 pts under the
    #: default 50% per-product cap (room for btc_hold's BTC to appreciate before it crowds out an
    #: entry here), and all three strategies use 65% of the 90% total-exposure cap
    risk_defaults: ClassVar[dict[str, Any]] = {"max_allocation_pct": 25}
    default_params: ClassVar[dict[str, Any]] = {"sma_n": 100, "buffer": 0.02, "execution": "taker"}
    param_schema: ClassVar[dict[str, dict[str, Any]]] = {
        "sma_n": {"type": "int", "min": 10, "max": 200,
                  "help": "SMA length in daily bars (researched: 100; neighbours tested mostly worse)"},
        "buffer": {"type": "float", "min": 0.0, "max": 0.2,
                   "help": "hysteresis band: enter above (1+buffer)xSMA, exit below (1-buffer)xSMA (researched: 0.02)"},
        "execution": {"type": "enum", "choices": ["taker", "maker_then_taker"],
                      "help": "taker = market orders (as tested); maker_then_taker = post-only first, "
                              "then taker for the rest (maker fees ~0.50% vs 0.90%, untested fills)"},
    }
    bar_granularity_s: ClassVar[int] = DAY_S  # UTC days: decide right after the 00:00 UTC close
    #: SMA warm-up + 150 bars of hysteresis replay (instance value: ``sma_n + REPLAY_BARS``)
    REPLAY_BARS: ClassVar[int] = 150
    history_bars: ClassVar[int] = 100 + 150
    execution: ClassVar[str] = "taker"
    #: 0/1 signal: only entries/exits trade; the band stops fee-residue top-ups
    rebalance_band: ClassVar[float] = 0.05
    backtestable: ClassVar[bool] = True
    #: a decision later than this after the bar close is flagged in its reason (see module doc)
    late_after_s: ClassVar[float] = 3600.0
    #: "in" by holdings (only when no close in the window left the band): at least this share
    #: of the allocation is in BTC. The "already in -> no trade" check uses
    #: :func:`holds_nothing` instead, so a held position is never topped up.
    HOLDINGS_IN_THRESHOLD: ClassVar[float] = 0.5

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        super().__init__(params)
        self.history_bars = int(self.params["sma_n"]) + self.REPLAY_BARS  # type: ignore[misc]
        self.execution = str(self.params.get("execution") or "taker")  # type: ignore[misc]

    def universe(self, products: Mapping[str, Any]) -> list[str]:
        return [BTC] if BTC in products else []

    def on_bar(self, ctx: SpotContext) -> list[TargetWeight] | None:
        n = int(self.params["sma_n"])
        b = float(self.params["buffer"])
        if BTC not in ctx.products:
            ctx.log(f"{BTC} not tradable now; no change")
            return None
        bars = ctx.candles(BTC, self.history_bars)
        if len(bars) < n:
            ctx.log(f"need {n} daily {BTC} bars for SMA{n}, have {len(bars)}; no change")
            return None
        problem = final_bar_problem(bars, ctx.bar_end, self.bar_granularity_s)
        if problem:
            ctx.log(f"{BTC}: {problem}; not deciding on an older close → no change this bar")
            return None
        closes = closes_of(bars)
        ts = trend_state(closes, n, b)
        close, sma = closes[-1], ts.sma
        upper, lower = (1 + b) * sma, (1 - b) * sma
        held_w = current_weight(ctx, BTC)
        flat = holds_nothing(ctx, BTC)
        sma_lbl = f"SMA{n}"
        px = f"BTC close {fmt_px(close)} ({_bar_label(bars[-1].start, self.bar_granularity_s)})"
        fresh = ts.signal_index == len(closes) - 1  # today's close is outside the band
        if fresh:
            if ts.state:
                why = f"{px} > {_mult(1 + b)}×{sma_lbl} {fmt_px(upper)} ({sma_lbl} {fmt_px(sma)}) → hold BTC"
            else:
                why = f"{px} < {_mult(1 - b)}×{sma_lbl} {fmt_px(lower)} ({sma_lbl} {fmt_px(sma)}) → cash"
            state = bool(ts.state)
        else:
            inside = (f"{px} inside {_mult(1 - b)}–{_mult(1 + b)}×{sma_lbl} "
                      f"({fmt_px(lower)}–{fmt_px(upper)}; {sma_lbl} {fmt_px(sma)})")
            if ts.state is not None and ts.signal_index is not None:
                state = ts.state
                day = bars[ts.signal_index].start.date().isoformat()
                side = f"above {_mult(1 + b)}×{sma_lbl}" if state else f"below {_mult(1 - b)}×{sma_lbl}"
                why = (f"{inside} → {'keep holding BTC' if state else 'stay in cash'} "
                       f"(last signal: {day} close {fmt_px(closes[ts.signal_index])} {side})")
            else:
                state = held_w >= self.HOLDINGS_IN_THRESHOLD
                why = (f"{inside} → {'keep holding BTC' if state else 'stay in cash'} "
                       f"(no close outside the band in the last {len(closes)} bars; following current "
                       f"holdings, {held_w:.0%} BTC)")
        if fresh or not flat:  # a first entry on an older signal is not a late reaction
            why += late_note(ctx, self.late_after_s, BTC_DELAY_NOTE)
        score = close / sma - 1.0 if sma > 0 else None
        data = {"state": "in" if state else "out", "close": close, "sma": sma, "upper": upper,
                "lower": lower, "held_weight": round(held_w, 4)}
        if state and not flat:
            # already in: no resize (research trades only on entries/exits; resizing against the
            # account-wide allocation equity would churn fees as other sleeves move)
            ctx.log(f"{why}; already holding BTC ({held_w:.0%} of the allocation) → no trade "
                    f"(entries/exits only; an allocation change applies at the next entry)", **data)
            return None
        ctx.log(why, **data)
        return [TargetWeight(BTC, 1.0 if state else 0.0, why, score=score)]
