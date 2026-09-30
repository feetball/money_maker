"""btc_hold - buy BTC-USD with the whole allocation once, then hold it. The benchmark.

PAPER TRADING ONLY. research/coinbase/FINDINGS.md: nothing tested beat simply holding BTC after
retail fees (test 2024-01-01 -> 2026-09-25: 28.6%/yr, Sharpe 0.76, max drawdown -53%; the app's
backtest, which starts in cash and marks holdings after the exit fee, shows ~27.7%/yr). Running
this next to the other Coinbase strategies shows, on the same paper fills and fees, whether
they earn their extra trading.

Rule: target weight 1.0 in BTC-USD, once. It buys only when it holds essentially nothing
(BTC worth less than the minimum trade, :func:`~kalshibot.coinbase.strategies.btc_trend.
holds_nothing`: the first bar, or after its BTC was sold / the account was reset) and otherwise
returns ``None`` (no change): it never sells, never trims and never tops up - not after a crash
(its holding falls far below its slice of the account) and not when other strategies' gains
grow that slice - so the only trade is the initial buy (like the backtester's ``btc``
benchmark). A changed ``max_allocation_pct`` therefore applies only after a reset. Stateless:
the decision depends only on the current holdings, so restarts change nothing.

It shares the default 50% per-product cap with btc_trend (both hold BTC-USD); the default
allocations (15% here, 25% for btc_trend) leave room for both.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from kalshibot.coinbase.strategies.base import SpotContext, SpotStrategy, TargetWeight
from kalshibot.coinbase.strategies.btc_trend import BTC, DAY_S, current_weight, fmt_px, held_value, holds_nothing

__all__ = ["BtcHold"]


class BtcHold(SpotStrategy):
    name: ClassVar[str] = "btc_hold"
    description: ClassVar[str] = (
        "Benchmark: buys BTC once and holds — the bar every other Coinbase strategy has to beat. "
        "Target 100% BTC-USD of its allocation; never sells, never tops up (2024–26 test: 28.6%/yr, "
        "max drawdown −53%; the app's backtest from cash: ~27.7%/yr). Buys only when it holds no "
        "BTC, so an allocation change applies at the next entry (after a reset)."
    )
    experimental: ClassVar[bool] = False
    #: on unless the dashboard or the config turns it off (the benchmark should always run)
    enabled_by_default: ClassVar[bool] = True
    #: 15% of the Coinbase account (see btc_trend.risk_defaults: both BTC sleeves together stay
    #: 10 pts under the default 50% per-product cap)
    risk_defaults: ClassVar[dict[str, Any]] = {"max_allocation_pct": 15}
    default_params: ClassVar[dict[str, Any]] = {}
    param_schema: ClassVar[dict[str, dict[str, Any]]] = {}
    bar_granularity_s: ClassVar[int] = DAY_S
    history_bars: ClassVar[int] = 1  # only the last close, for the reason text
    execution: ClassVar[str] = "taker"
    rebalance_band: ClassVar[float] = 0.10
    backtestable: ClassVar[bool] = True

    def universe(self, products: Mapping[str, Any]) -> list[str]:
        return [BTC] if BTC in products else []

    def on_bar(self, ctx: SpotContext) -> list[TargetWeight] | None:
        if BTC not in ctx.products:
            ctx.log(f"{BTC} not tradable now; no change")
            return None
        held_w = current_weight(ctx, BTC)
        if not holds_nothing(ctx, BTC):
            ctx.log(f"holding ${float(held_value(ctx, BTC)):,.2f} of BTC ({held_w:.0%} of the allocation); "
                    f"buy-and-hold never sells or tops up → no trade", held_weight=round(held_w, 4))
            return None
        bars = ctx.candles(BTC, 1)
        px = f" at ~{fmt_px(float(bars[-1].close))}" if bars else ""
        why = f"Benchmark buy-and-hold: no BTC held → buy BTC{px} with 100% of the allocation, then hold"
        ctx.log(why, held_weight=round(held_w, 4))
        return [TargetWeight(BTC, 1.0, why)]
