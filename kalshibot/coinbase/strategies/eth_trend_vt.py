"""eth_trend_vt - ETH-USD 200-day trend filter with a 50% volatility target. EXPERIMENTAL.

PAPER TRADING ONLY. Risk strategy, not an edge (research/coinbase/FINDINGS.md): in the
2024-01-01 -> 2026-09-25 test it returned 18.0%/yr (max drawdown -33%) - it beat holding ETH
(6.2%/yr) but trailed simply holding BTC by ~8-10 pts/yr. The app's backtest (from cash) shows
~17.1%/yr. Timing: research did not measure what a delayed decision costs this strategy (only
btc_trend's), so a late decision's note says just that.

Rule (exactly the researched configuration ``B|ETH-USD|sma200|tv0.5|band0.1``,
``research/coinbase/strategies/run_daily.py::tgt_voltarget``)
    On every daily UTC close (00:00 UTC; fills at the next open in research):

    * ``signal`` = 1 when ``close > SMA200`` (mean of the last 200 closes incl. today's), 0 when
      ``close < SMA200`` (a close exactly on the SMA keeps the previous signal);
    * ``vol`` = sample stdev (ddof=1) of the last 30 daily close-to-close returns x sqrt(365)
      (at least 20 returns);
    * ``weight = signal x min(1, target_vol / vol)`` with ``target_vol = 0.50``.

    Signal off -> 0% (sell everything; not banded). Signal on -> the weight above; the planner
    skips resizes smaller than ``rebalance_band`` = 0.10 of the allocation (entries and full
    exits are never banded), as in the research simulation. If the volatility cannot be
    estimated yet the strategy makes no change.

    If the final bar (the one closing at ``ctx.bar_end``) is not published, the strategy makes
    no change and logs which bar is missing (as btc_trend does).

Stateless: everything is recomputed from the candles each bar (restart-consistent).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, ClassVar

from kalshibot.coinbase.strategies.base import SpotContext, SpotStrategy, TargetWeight
from kalshibot.coinbase.strategies.btc_trend import (
    DAY_S,
    _bar_label,
    annualized_vol,
    closes_of,
    current_weight,
    final_bar_problem,
    fmt_px,
    holds_nothing,
    late_note,
    trend_state,
)

__all__ = ["ETH", "EthTrendVolTarget", "vol_target_weight"]

ETH = "ETH-USD"
#: lateness note (research measured a delay cost for btc_trend only)
ETH_DELAY_NOTE = "timing-sensitive; delay cost not measured in research"


def vol_target_weight(signal: bool, target_vol: float, vol: float) -> float:
    """``signal x min(1, target_vol / vol)`` (``vol <= 0`` -> 1 when the signal is on)."""
    if not signal:
        return 0.0
    if not math.isfinite(vol):
        return math.nan
    if vol <= 0:
        return 1.0
    return min(1.0, target_vol / vol)


class EthTrendVolTarget(SpotStrategy):
    name: ClassVar[str] = "eth_trend_vt"
    description: ClassVar[str] = (
        "Risk strategy: beat holding ETH but trailed holding BTC by ~8–10 pts/yr in testing "
        "(2024–26: 18.0%/yr, max drawdown −33%; ETH buy-and-hold 6.2%/yr). Holds ETH-USD while "
        "the daily close is above its 200-day SMA, sized to 50% annualized volatility "
        "(weight = min(1, 0.50 / 30-day vol)); cash otherwise. App backtest from cash: ~17.1%/yr."
    )
    experimental: ClassVar[bool] = True
    #: on by default as a forward paper-test (experimental badge); the dashboard/config can turn it off
    enabled_by_default: ClassVar[bool] = True
    #: 25% of the Coinbase account (65% in all with btc_trend 25% + btc_hold 15%)
    risk_defaults: ClassVar[dict[str, Any]] = {"max_allocation_pct": 25}
    default_params: ClassVar[dict[str, Any]] = {"sma_n": 200, "target_vol": 0.50, "vol_window": 30}
    param_schema: ClassVar[dict[str, dict[str, Any]]] = {
        "sma_n": {"type": "int", "min": 20, "max": 250,
                  "help": "trend SMA length in daily bars (researched: 200)"},
        "target_vol": {"type": "float", "min": 0.05, "max": 2.0,
                       "help": "annualized volatility target; weight = min(1, target_vol / realized vol) (researched: 0.50)"},
        "vol_window": {"type": "int", "min": 10, "max": 120,
                       "help": "daily returns in the realized-volatility estimate (researched: 30)"},
    }
    bar_granularity_s: ClassVar[int] = DAY_S
    #: SMA warm-up + 60 bars (a close exactly on the SMA looks back for the previous signal)
    REPLAY_BARS: ClassVar[int] = 60
    history_bars: ClassVar[int] = 200 + 60
    execution: ClassVar[str] = "taker"
    rebalance_band: ClassVar[float] = 0.10
    backtestable: ClassVar[bool] = True
    late_after_s: ClassVar[float] = 3600.0

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        super().__init__(params)
        need = max(int(self.params["sma_n"]), int(self.params["vol_window"]) + 1)
        self.history_bars = need + self.REPLAY_BARS  # type: ignore[misc]

    def universe(self, products: Mapping[str, Any]) -> list[str]:
        return [ETH] if ETH in products else []

    def on_bar(self, ctx: SpotContext) -> list[TargetWeight] | None:
        n = int(self.params["sma_n"])
        tv = float(self.params["target_vol"])
        win = int(self.params["vol_window"])
        if ETH not in ctx.products:
            ctx.log(f"{ETH} not tradable now; no change")
            return None
        bars = ctx.candles(ETH, self.history_bars)
        if len(bars) < n:
            ctx.log(f"need {n} daily {ETH} bars for SMA{n}, have {len(bars)}; no change")
            return None
        problem = final_bar_problem(bars, ctx.bar_end, self.bar_granularity_s)
        if problem:
            ctx.log(f"{ETH}: {problem}; not deciding on an older close → no change this bar")
            return None
        closes = closes_of(bars)
        ts = trend_state(closes, n, 0.0)
        close, sma = closes[-1], ts.sma
        held_w = current_weight(ctx, ETH)
        if ts.state is None:  # every close in the window exactly on the SMA: follow the holdings
            signal = held_w > 0
        else:
            signal = ts.state
        rel = ">" if close > sma else "<" if close < sma else "="
        head = f"ETH close {fmt_px(close)} ({_bar_label(bars[-1].start, self.bar_granularity_s)}) {rel} SMA{n} {fmt_px(sma)}"
        # a first entry on an older signal (e.g. right after enabling) is not a late reaction
        crossed = ts.state is not None and trend_state(closes[:-1], n, 0.0).state != ts.state
        late = late_note(ctx, self.late_after_s, ETH_DELAY_NOTE) if crossed or not holds_nothing(ctx, ETH) else ""
        if not signal:
            why = f"{head} → cash (trend down){late}"
            ctx.log(why, signal=0, close=close, sma=sma, held_weight=round(held_w, 4))
            return [TargetWeight(ETH, 0.0, why, score=close / sma - 1.0 if sma > 0 else None)]
        min_periods = max(2, (win * 2 + 2) // 3)  # research: 20 of 30
        vol = annualized_vol(closes, window=win, min_periods=min_periods)
        w = vol_target_weight(True, tv, vol)
        if not math.isfinite(w):
            ctx.log(f"{head}, but {win}-day volatility not available yet; no change", signal=1, close=close, sma=sma)
            return None
        cap = " (capped at 100%)" if w >= 1.0 else ""
        why = (f"{head} → hold ETH at {w:.0%} = min(1, {tv:.0%} target vol / {vol:.0%} {win}-day vol){cap}"
               f"{late}")
        ctx.log(why, signal=1, close=close, sma=sma, vol=vol, weight=w, held_weight=round(held_w, 4))
        return [TargetWeight(ETH, w, why, score=close / sma - 1.0 if sma > 0 else None)]
