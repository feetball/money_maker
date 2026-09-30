"""Stale-quote taker on the thin 15-minute alt-coin markets (DOGE, SOL, XRP) - **EXPERIMENTAL**.

Idea: the alts' Kalshi books are thin and slow to follow Coinbase. A live 2-second study
(``research/crypto_fv/latency_analysis.py``, ~1 h, 3 series) found the spot model beating the
book by more than 2c after fees in roughly 1-30% of samples, and about half of those prices were
still available 2-4 s later. That is reachable with public REST polling, unlike BTC where Kalshi
absorbs spot moves in ~1.5 s. The 1-minute-candle backtest (``weekly.py``) found +3..+5c only when
filled at the same minute's quote and ~0 a minute later, so this can only be judged forward.

Rule: every ``tick_interval_s`` (2 s), for each open DOGE/SOL/XRP 15-minute window with
``minutes_min`` < minutes-to-close <= ``minutes_max``, fetch a fresh book and a fresh (<= 1 s) Coinbase
quote. Fair value is the research's *stacked* model ``q = sigmoid(a + b*logit(mid) + c*logit(p_model))``
(walk-forward coefficients per series, ``research/crypto_fv/stacked.py``). It shrinks the spot model
toward the market mid, so it only fires when spot has moved and the book has not. Buy the side whose
``q - ask - fee`` is at least ``min_model_edge`` (default 2c), ask in [``price_min``, ``price_max``],
taker IOC at the ask for at most the depth shown at the touch. At most ``max_entries_per_market``
entries per window; hold to settlement.

Not a proven edge. It is a forward paper-test whose signals log the stale-quote evidence, with
tight caps (size and allocation); the paper broker re-reads the book at execution, so fills are as
late as REST polling really is.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from kalshibot.money import ZERO, D
from kalshibot.strategies.base import OrderIntent, Strategy, StrategyContext, UniverseSpec
from kalshibot.strategies.btc15m_favorite import (
    ModelUnavailable,
    _fresh_book,
    ewma_variance,
    horizon_minutes,
    minute_grid,
    prob_above,
)

__all__ = ["Alt15mStale", "AltSpec", "SPECS", "stacked_probability"]


@dataclass(frozen=True)
class AltSpec:
    symbol: str
    a: float
    b: float  # weight on logit(market mid)
    c: float  # weight on logit(spot model)
    halflife: float  # EWMA vol half-life, minutes
    nu: float  # Student-t degrees of freedom
    vol_mult: float


#: coefficients are the research's walk-forward stacked fits (``latency_analysis.py``)
SPECS: dict[str, AltSpec] = {
    "KXDOGE15M": AltSpec("DOGE", -0.005, 0.619, 0.458, 10, 3.5, 1.1),
    "KXSOL15M": AltSpec("SOL", -0.013, 0.642, 0.412, 30, 5.0, 1.0),
    "KXXRP15M": AltSpec("XRP", 0.074, 0.800, 0.273, 10, 3.5, 1.1),
}
VOL_LOOKBACK_MIN = 240
VOL_MIN_RETURNS = 60


def _logit(x: float) -> float:
    x = min(max(x, 1e-3), 1 - 1e-3)
    return math.log(x / (1 - x))


def stacked_probability(spec: AltSpec, p_model: float, mid: float) -> float:
    """P(YES) from the market mid and the spot model (logistic stack)."""
    z = spec.a + spec.b * _logit(mid) + spec.c * _logit(p_model)
    return 1.0 / (1.0 + math.exp(-z))


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


class Alt15mStale(Strategy):
    name: ClassVar[str] = "alt15m_stale"
    tick_interval_s: ClassVar[float | None] = 2.0
    risk_defaults: ClassVar[dict[str, Any]] = {"max_allocation_pct": 10, "daily_loss_limit": 20}
    enabled_by_default: ClassVar[bool] = True
    backtestable: ClassVar[bool] = False
    description: ClassVar[str] = (
        "EXPERIMENTAL forward test. DOGE/SOL/XRP 15-minute markets: poll every 2 s and buy as a taker when a "
        "fresh Coinbase quote, stacked with the market mid, says a side is worth at least 2c/contract more than "
        "its ask plus fee. Research: 2c+ mispricings appeared in 1-30% of 2-second samples and about half "
        "were still there 2-4 s later, but the minute-candle backtest could not confirm the edge (+3..+5c "
        "at the same minute's quote, ~0 a minute later). No proven edge; small size, hold to settlement."
    )
    default_params: ClassVar[dict[str, Any]] = {
        "minutes_min": 4.0,
        "minutes_max": 12.5,
        "min_model_edge": 0.02,
        "price_min": 0.10,
        "price_max": 0.90,
        "min_contracts": 5,
        "max_contracts": 25,
        "max_cost_per_trade": 12.0,
        "max_entries_per_market": 1,
        "max_spot_age_s": 1.5,
        "max_candle_age_s": 180.0,
        "max_spread": 0.12,
    }
    param_schema: ClassVar[dict[str, dict[str, Any]]] = {
        "minutes_min": {"type": "float", "min": 1, "max": 14, "help": "No entries closer to close than this (minutes)"},
        "minutes_max": {"type": "float", "min": 2, "max": 14.5, "help": "No entries earlier than this (minutes to close)"},
        "min_model_edge": {"type": "float", "min": 0, "max": 0.3,
                           "help": "Required stacked P(side) - ask - fee, dollars per contract (research: 0.02)"},
        "price_min": {"type": "float", "min": 0.01, "max": 0.99, "help": "Lowest ask traded"},
        "price_max": {"type": "float", "min": 0.01, "max": 0.99, "help": "Highest ask traded"},
        "min_contracts": {"type": "int", "min": 1, "max": 1000, "help": "Smallest order sent (1-lots lose to fee rounding)"},
        "max_contracts": {"type": "int", "min": 1, "max": 10000, "help": "Most contracts per entry (also capped by depth at the ask)"},
        "max_cost_per_trade": {"type": "float", "min": 1, "max": 10000, "help": "Dollar cap per entry including fee"},
        "max_entries_per_market": {"type": "int", "min": 1, "max": 10, "help": "Entries per 15-minute window"},
        "max_spot_age_s": {"type": "float", "min": 0.5, "max": 60, "help": "Skip when the Coinbase quote is older than this"},
        "max_candle_age_s": {"type": "float", "min": 60, "max": 1800, "help": "Skip when the newest 1-min bar ended longer ago"},
        "max_spread": {"type": "float", "min": 0.01, "max": 1, "help": "Skip books whose YES spread is wider than this"},
    }

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        super().__init__(params)
        self._entries: dict[str, int] = {}  # ticker -> entries sent

    def universe(self) -> UniverseSpec:
        return UniverseSpec(series_tickers=list(SPECS), refresh_s=20)

    # ------------------------------------------------------------------ state

    def dump_state(self) -> Any:
        return {"entries": dict(self._entries)}

    def load_state(self, state: Any) -> None:
        ent = state.get("entries") if isinstance(state, Mapping) else None
        if isinstance(ent, Mapping):
            for t, n in ent.items():
                try:
                    self._entries[str(t)] = int(n)
                except (TypeError, ValueError):
                    continue

    # ------------------------------------------------------------------ tick

    async def on_tick(self, ctx: StrategyContext) -> list[OrderIntent]:
        now = ctx.now
        lo, hi = float(self.params["minutes_min"]), float(self.params["minutes_max"])
        cap = int(self.params["max_entries_per_market"])
        due = []
        for ticker in sorted(ctx.markets):
            m = ctx.markets[ticker]
            if m.series_ticker not in SPECS or m.close_time is None or not m.is_tradable(now):
                continue
            mtc = (m.close_time - now).total_seconds() / 60.0
            if not lo < mtc <= hi or self._entries.get(ticker, 0) >= cap:
                continue
            pf = ctx.portfolio
            if pf is not None and pf.has_open_order(ticker, self.name):
                continue
            due.append((m, mtc))
        if not due:
            return []
        books = await asyncio.gather(*(_fresh_book(ctx, m.ticker) for m, _ in due), return_exceptions=True)
        out: list[OrderIntent] = []
        for (m, mtc), book in zip(due, books, strict=True):
            if isinstance(book, BaseException):
                continue
            try:
                intent = await self._evaluate(ctx, m, mtc, book)
            except ModelUnavailable as e:
                ctx.log(f"{m.ticker}: no model ({e})", ticker=m.ticker, skip="no_model")
                continue
            if intent is not None:
                self._entries[m.ticker] = self._entries.get(m.ticker, 0) + 1
                out.append(intent)
        return out

    async def _evaluate(self, ctx: StrategyContext, m: Any, mtc: float, book: Any) -> OrderIntent | None:
        p = self.params
        spec = SPECS[m.series_ticker]
        yb, ya = book.best_yes_bid, book.best_yes_ask
        if yb is None or ya is None or not (ZERO < yb < ya < 1) or float(ya - yb) > float(p["max_spread"]):
            return None
        if m.strike_type not in ("greater", "greater_or_equal") or m.floor_strike is None:
            return None
        strike = float(m.floor_strike)
        p_model, spot, sigma_min = await self._spot_model(ctx, spec, strike, mtc)
        mid = float(yb + ya) / 2
        q_yes = stacked_probability(spec, p_model, mid)

        best: tuple[float, str, Decimal, int, float] | None = None  # edge, side, ask, n, fv
        for side, fv in (("yes", q_yes), ("no", 1.0 - q_yes)):
            lvl = book.best_ask(side)
            if lvl is None or not float(p["price_min"]) <= float(lvl.price) <= float(p["price_max"]):
                continue
            n = min(int(lvl.size), int(p["max_contracts"]))
            cost_cap = D(p["max_cost_per_trade"])
            while n > 0 and n * lvl.price + D(ctx.fee(m, lvl.price, n, is_taker=True)) > cost_cap:
                n -= 1
            if n < int(p["min_contracts"]):
                continue
            edge = fv - float(lvl.price) - float(D(ctx.fee(m, lvl.price, n, is_taker=True)) / n)
            if edge >= float(p["min_model_edge"]) and (best is None or edge > best[0]):
                best = (edge, side, lvl.price, n, fv)
        if best is None:
            return None
        edge, side, ask, n, fv = best
        fee = D(ctx.fee(m, ask, n, is_taker=True))
        ctx.log(f"{m.ticker}: {spec.symbol} spot {spot:g} vs strike {strike:g}, {mtc:.2f} min left: buy {n} "
                f"{side.upper()} @ {ask} (stacked P={fv:.4f}, edge {100 * edge:+.2f}c)", ticker=m.ticker,
                trade=side, edge=edge)
        return OrderIntent(
            ticker=m.ticker, side=side, action="buy", count=n, limit_price=ask, tif="ioc", strategy=self.name,
            reason=(f"{spec.symbol} stale quote: {side.upper()} ask {ask} vs stacked P={fv:.4f} "
                    f"(spot {spot:g}, strike {strike:g}, mid {mid:.3f}, spot-model P(YES)={p_model:.4f}, "
                    f"vol {100 * sigma_min:.4f}%/min, {mtc:.2f} min left); edge {100 * edge:+.2f}c after "
                    f"${fee} fee; buy {n} IOC at the ask, hold to settlement"),
            fair_value=round(fv, 6),
            expected_edge=(D(fv) - ask - fee / n).quantize(Decimal("0.000001")),
        )

    async def _spot_model(self, ctx: StrategyContext, spec: AltSpec, strike: float, mtc: float
                          ) -> tuple[float, float, float]:
        """(P(YES) from the spot model, spot, per-minute vol). Raises :class:`ModelUnavailable`."""
        feed = getattr(ctx, "feeds", {}).get("crypto") if isinstance(getattr(ctx, "feeds", None), Mapping) else None
        if feed is None:
            raise ModelUnavailable("crypto feed missing")
        now = ctx.now
        try:
            raw = await feed.candles(spec.symbol, VOL_LOOKBACK_MIN)
            q = await feed.spot(spec.symbol, max_age_s=float(self.params["max_spot_age_s"]) * 0.5)
        except Exception as e:
            raise ModelUnavailable(f"{spec.symbol} feed: {type(e).__name__}: {e}") from None
        bars = [c for c in raw if getattr(c, "complete", True) and c.end <= now]
        if not bars:
            raise ModelUnavailable("no complete bars")
        if (now - bars[-1].end).total_seconds() > float(self.params["max_candle_age_s"]):
            raise ModelUnavailable("candles stale")
        if any(getattr(c, "source", "coinbase") not in ("coinbase", "") for c in bars) or q.source != "coinbase":
            raise ModelUnavailable("not Coinbase data")
        # last-trade time is old on quiet coins; what matters is when the quote was fetched
        age = (now - (getattr(q, "fetched_at", None) or q.ts)).total_seconds()
        if age > float(self.params["max_spot_age_s"]):
            raise ModelUnavailable(f"spot stale ({age:.1f}s)")
        cut = _epoch(now) - VOL_LOOKBACK_MIN * 60
        var = ewma_variance([v for t, v in minute_grid(bars) if t >= cut], spec.halflife, VOL_MIN_RETURNS)
        spot = float(q.price)
        if var is None or var <= 0 or not math.isfinite(spot) or spot <= 0:
            raise ModelUnavailable("no vol / bad spot")
        sigma = spec.vol_mult * math.sqrt(var * horizon_minutes(mtc))
        return prob_above(spot, strike, sigma, spec.nu), spot, math.sqrt(var)
