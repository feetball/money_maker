"""Coinbase spot backtester (contract §12): replay, fills, fees, benchmarks, metrics, no look-ahead.

The two tiny example strategies below (SMA trend, momentum rotation) exist only to exercise
the machinery; real strategies live in ``kalshibot/coinbase/strategies/``.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest

from kalshibot.coinbase import backtest as bt
from kalshibot.coinbase.backtest import (
    DEFAULT_SLIPPAGE_BPS,
    RESEARCH_DATA_DIR,
    BarSeries,
    SpotBacktestError,
    SpotDataset,
    compute_spot_metrics,
    load_research_products,
    run_spot_backtest,
)
from kalshibot.coinbase.fees import DEFAULT_TIER, FEE_TIERS, fee_for, notional_for_budget
from kalshibot.coinbase.models import Product
from kalshibot.coinbase.strategies import ParamError, SpotContext, SpotStrategy, TargetWeight, register, unregister

DAY = 86400
T0 = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")


Row = tuple[int, float, float, float, float, float]


def walk(n: int, *, seed: int, p0: float = 100.0, start: int = T0, g: int = DAY, drift: float = 0.0,
         vol: float = 0.03, skip: set[int] | frozenset[int] = frozenset()) -> list[Row]:
    """Random-walk OHLCV bars; each open differs from the previous close (overnight gap)."""
    rng = random.Random(seed)
    rows, px = [], p0
    for i in range(n):
        o = px
        c = o * math.exp(drift + vol * rng.gauss(0, 1))
        rows.append((start + i * g, o, max(o, c) * 1.01, min(o, c) * 0.99, c, 1000.0 + i))
        px = c * (1 + 0.002 * rng.gauss(0, 1))
    return [r for i, r in enumerate(rows) if i not in skip]


# --------------------------------------------------------------------------- example strategies (tests only)


class SmaTrend(SpotStrategy):
    """Hold the product while its close is above its N-bar SMA, else cash."""

    name = "cb_test_sma_trend"
    description = "test: SMA trend on one product"
    default_params = {"n": 10, "product": "BTC-USD"}
    param_schema = {"n": {"type": "int", "min": 2, "max": 400}, "product": {"type": "str"}}
    history_bars = 10

    def universe(self, products: Any) -> list[str]:
        return [self.params["product"]] if self.params["product"] in products else []

    def on_bar(self, ctx: SpotContext) -> list[TargetWeight] | None:
        pid, n = self.params["product"], self.params["n"]
        c = ctx.candles(pid, n)
        if len(c) < n:
            return None
        closes = [float(x.close) for x in c]
        return [TargetWeight(pid, 1.0, f"close above SMA{n}")] if closes[-1] > sum(closes) / n else []


class MomentumRotation(SpotStrategy):
    """Hold the top-k products by trailing return (only positive ones), equal weight."""

    name = "cb_test_momentum"
    description = "test: momentum rotation"
    default_params = {"lookback": 10, "top": 1}
    param_schema = {"lookback": {"type": "int", "min": 1}, "top": {"type": "int", "min": 1}}
    history_bars = 11
    rebalance_band = 0.05

    def universe(self, products: Any) -> list[str]:
        return sorted(products)

    def on_bar(self, ctx: SpotContext) -> list[TargetWeight] | None:
        lb = self.params["lookback"]
        scores = {}
        for pid in sorted(ctx.products):
            c = ctx.candles(pid, lb + 1)
            if len(c) == lb + 1:
                scores[pid] = float(c[-1].close / c[0].close) - 1
        if not scores:
            return None
        best = sorted(scores, key=lambda p: (-scores[p], p))[: self.params["top"]]
        best = [p for p in best if scores[p] > 0]
        return [TargetWeight(p, 1 / len(best), "top momentum", score=scores[p]) for p in best]


def two_asset_dataset(n: int = 120, **kw: Any) -> SpotDataset:
    return SpotDataset.from_rows({"BTC-USD": walk(n, seed=1, p0=40000, **kw),
                                  "ETH-USD": walk(n, seed=2, p0=2000, **kw)})


# --------------------------------------------------------------------------- shape


def test_output_shape_and_metrics() -> None:
    ds = two_asset_dataset()
    r = run_spot_backtest(MomentumRotation, {"lookback": 5}, dataset=ds, slippage=5)
    assert r["venue"] == "coinbase" and r["strategy"] == "cb_test_momentum"
    for key in ("metrics", "equity_curve", "benchmarks", "trades", "by_year", "by_month", "signals", "params",
                "start", "end", "starting_balance", "granularity_s"):
        assert key in r
    m = r["metrics"]
    for key in ("total_return_pct", "cagr_pct", "vol_pct", "sharpe", "sortino", "max_drawdown_pct",
                "turnover_per_year", "fees_paid", "pct_time_invested", "trades", "win_rate", "final_equity",
                "total_pnl", "n_trades", "fees", "max_drawdown", "excess_return_vs_btc_pct", "benchmarks", "details"):
        assert key in m, key
    assert set(m["benchmarks"]) == {"btc", "equal_weight"}
    assert m["benchmarks"]["btc"]["trades"] == 1
    assert m["trades"] == len(r["trades"]) > 0
    assert m["fees_paid"] == pytest.approx(sum(t["fee"] for t in r["trades"]), abs=0.01)
    # default start = first bar + history_bars (warm-up); curve: start point + one point per bar
    assert r["start"] == iso(T0 + 11 * DAY) and r["end"] == iso(T0 + 120 * DAY)
    curve = r["equity_curve"]
    assert len(curve) == 120 - 11 + 1 and curve[0]["equity"] == 1000.0
    assert curve[-1]["equity"] == pytest.approx(m["final_equity"], abs=1e-3)
    for key in ("btc", "equal_weight"):
        assert [p["ts"] for p in r["benchmarks"][key]] == [p["ts"] for p in curve]
    for p in curve:
        assert set(p) == {"ts", "equity", "equity_mid", "cash", "invested", "exposure_pct", "drawdown_pct"}
        assert p["ts"].endswith("Z")
    t = r["trades"][0]
    assert set(t) >= {"ts", "product_id", "side", "base_size", "price", "notional", "fee", "fee_rate", "is_taker",
                      "realized_pnl", "reason", "target_weight"}
    assert [row["year"] for row in r["by_year"]] == ["2024"]
    assert [row["month"] for row in r["by_month"]] == ["2024-01", "2024-02", "2024-03", "2024-04"]
    assert sum(row["trades"] for row in r["by_month"]) == m["trades"]
    total = 1.0
    for row in r["by_month"]:
        total *= 1 + row["return_pct"] / 100
    assert (total - 1) * 100 == pytest.approx(m["total_return_pct"], abs=1e-3)
    d = m["details"]
    assert d["universe"] == ["BTC-USD", "ETH-USD"] and d["fee_tier"]["name"] == DEFAULT_TIER.name
    assert {k: d["slippage"][k] for k in ("mode", "default_bps", "by_product_bps", "multiplier", "fallback_fills")} == {
        "mode": "bps", "default_bps": 5.0, "by_product_bps": {}, "multiplier": 1.0, "fallback_fills": 0}
    assert d["participation"] == {"max_participation": 0.1, "capped_fills": 0} and d["fill_price"] == "open"
    assert d["signals"].get("executed") == m["trades"]
    assert all(s["decision"] in ("executed", "partial", "rejected", "unfilled") for s in r["signals"])
    import json

    json.dumps(r)  # JSON-safe


# --------------------------------------------------------------------------- fills and fees


class BuyAt(SpotStrategy):
    """Buy ``product`` with everything at the first bar whose close is >= ``at``; sell at ``sell_at``."""

    name = "cb_test_buy_at"
    default_params = {"at": 0, "sell_at": 0, "product": "BTC-USD", "weight": 1.0}
    param_schema = {"at": {"type": "int"}, "sell_at": {"type": "int"}, "product": {"type": "str"},
                    "weight": {"type": "float"}}
    history_bars = 1
    rebalance_band = 0.0

    def universe(self, products: Any) -> list[str]:
        return [p for p in products]

    def on_bar(self, ctx: SpotContext) -> list[TargetWeight] | None:
        t = int(ctx.bar_end.timestamp())
        if self.params["sell_at"] and t >= self.params["sell_at"]:
            return []
        if t >= self.params["at"]:
            return [TargetWeight(self.params["product"], self.params["weight"], "buy")]
        return None


def test_fill_at_next_open_with_slippage_and_taker_fee() -> None:
    rows = [(T0 + i * DAY, 100.0 + i, 130.0, 90.0, 110.0 + i, 5e4) for i in range(10)]  # open != prev close
    ds = SpotDataset.from_rows({"BTC-USD": rows})
    decide = T0 + 5 * DAY
    r = run_spot_backtest(BuyAt, {"at": decide, "sell_at": decide + 2 * DAY}, dataset=ds, start=iso(T0 + DAY),
                          slippage=10, benchmarks=False)
    buy, sell = r["trades"]
    tier = DEFAULT_TIER
    # decided at the close of bar 4 (== decide), filled at the OPEN of bar 5 (105) + 10 bps
    assert buy["ts"] == iso(decide) and buy["open_price"] == 105.0
    px = Decimal("105") * (1 + Decimal("0.001"))
    assert buy["price"] == pytest.approx(float(px))
    notional = notional_for_budget(Decimal("1000"), is_taker=True, tier=tier)
    base = (notional / px) // Decimal("0.00000001") * Decimal("0.00000001")
    assert buy["base_size"] == pytest.approx(float(base))
    assert buy["fee"] == float(fee_for(base * px, is_taker=True, tier=tier))
    assert buy["fee_rate"] == float(tier.taker_rate) and buy["is_taker"] is True
    # sold at the open of bar 7 (107) - 10 bps, realized = proceeds - fee - cost incl. buy fee
    assert sell["ts"] == iso(decide + 2 * DAY) and sell["open_price"] == 107.0
    spx = Decimal("107") * (1 - Decimal("0.001"))
    s_notional = base * spx
    s_fee = fee_for(s_notional, is_taker=True, tier=tier)
    cost = base * px + fee_for(base * px, is_taker=True, tier=tier)
    assert sell["realized_pnl"] == pytest.approx(float(s_notional - s_fee - cost), abs=1e-6)
    # marks: the bar-5 close point (ts = decide + 1 day) values the holding at close x (1 - slip),
    # net of the exit taker fee
    point = next(p for p in r["equity_curve"] if p["ts"] == iso(decide + DAY))
    cash_after = Decimal(1000) - base * px - fee_for(base * px, is_taker=True, tier=tier)
    assert point["cash"] == pytest.approx(float(cash_after), abs=1e-4)
    exit_keep = 1 - float(tier.taker_rate)
    assert point["equity"] == pytest.approx(float(cash_after) + float(base) * 115.0 * 0.999 * exit_keep, abs=1e-4)
    assert point["equity_mid"] == pytest.approx(float(cash_after) + float(base) * 115.0, abs=1e-4)
    final = r["metrics"]["final_equity"]
    assert final == pytest.approx(1000 + sell["realized_pnl"], abs=1e-4)
    assert r["metrics"]["round_trips"] == 1
    assert r["metrics"]["win_rate"] == (1.0 if sell["realized_pnl"] > 0 else 0.0)  # +1.7% gross vs. the round-trip fees


def test_fee_tier_and_slippage_options() -> None:
    ds = SpotDataset.from_rows({"BTC-USD": walk(30, seed=3)}, spreads_bps={"BTC-USD": 8.0})
    other = next(t for t in FEE_TIERS.values() if t.taker_rate != DEFAULT_TIER.taker_rate)
    r = run_spot_backtest(BuyAt, {"at": T0 + 3 * DAY}, dataset=ds, fee_tier=other.name, benchmarks=False)
    t = r["trades"][0]
    assert t["fee_rate"] == float(other.taker_rate) and t["slippage_bps"] == 4.0
    assert r["metrics"]["details"]["fee_tier"]["name"] == other.name  # half the 8 bps snapshot spread
    assert r["metrics"]["details"]["slippage"]["by_product_bps"] == {"BTC-USD": 4.0}
    r = run_spot_backtest(BuyAt, {"at": T0 + 3 * DAY, "product": "ETH-USD"},
                          dataset=SpotDataset.from_rows({"ETH-USD": walk(30, seed=4)}), benchmarks=False,
                          fee_tier={"maker": 0.001, "taker": 0.003})
    t = r["trades"][0]
    # no snapshot: point-in-time estimate from the trailing 30-day USD volume (3 bars of ~$100k)
    eth = SpotDataset.from_rows({"ETH-USD": walk(30, seed=4)}).bars["ETH-USD"]
    est = bt.fallback_slippage_bps(eth, T0 + 3 * DAY, DEFAULT_SLIPPAGE_BPS)
    assert t["fee_rate"] == 0.003 and t["slippage_bps"] == pytest.approx(est, abs=1e-3) and est > DEFAULT_SLIPPAGE_BPS
    assert r["metrics"]["details"]["slippage"]["fallback_fills"] >= 1
    assert r["metrics"]["benchmarks"]["btc"] is None  # no BTC data in this dataset
    with pytest.raises(ValueError, match="slippage"):
        run_spot_backtest(BuyAt, dataset=ds, slippage="wide")
    with pytest.raises(KeyError):
        run_spot_backtest(BuyAt, dataset=ds, fee_tier="platinum")


def test_min_market_funds_and_base_increment_from_metadata() -> None:
    p = Product(product_id="DOGE-USD", base_currency="DOGE", quote_currency="USD", base_increment=Decimal("1"),
                quote_increment=Decimal("0.00001"), min_market_funds=Decimal("5"), status="online",
                trading_disabled=False, post_only=False, limit_only=False, cancel_only=False)
    rows = [(T0 + i * DAY, 0.1234, 0.13, 0.12, 0.125, 1e6) for i in range(10)]
    ds = SpotDataset.from_rows({"DOGE-USD": rows}, products={"DOGE-USD": p})
    r = run_spot_backtest(BuyAt, {"at": T0 + 3 * DAY, "product": "DOGE-USD", "weight": 0.5}, dataset=ds,
                          benchmarks=False, slippage=0)
    t = r["trades"][0]
    assert t["base_size"] == float(int(t["base_size"]))  # whole DOGE
    assert t["notional"] + t["fee"] <= 500.0


def test_unfilled_when_no_bar_at_fill_time() -> None:
    rows = walk(12, seed=5, skip={5})  # no trades on day 5
    ds = SpotDataset.from_rows({"BTC-USD": rows})
    r = run_spot_backtest(BuyAt, {"at": T0 + 5 * DAY}, dataset=ds, start=iso(T0 + DAY), benchmarks=False)
    assert r["signals"][0]["decision"] == "unfilled" and r["signals"][0]["ts"] == iso(T0 + 5 * DAY)
    assert r["trades"][0]["ts"] == iso(T0 + 6 * DAY)  # re-planned and filled at the next bar


# --------------------------------------------------------------------------- look-ahead


class Spy(SpotStrategy):
    """Checks everything visible at each bar close; records violations instead of raising."""

    name = "cb_test_spy"
    history_bars = 3
    violations: ClassVar[list[str]] = []
    seen: ClassVar[list[tuple[int, tuple[str, ...]]]] = []

    def universe(self, products: Any) -> list[str]:
        return sorted(products)

    def on_bar(self, ctx: SpotContext) -> list[TargetWeight] | None:
        assert isinstance(ctx, SpotContext)
        be = ctx.bar_end
        if ctx.now != be + timedelta(seconds=bt.DEFAULT_OPTIONS["bar_delay_s"]):
            self.violations.append(f"now {ctx.now} != bar_end + bar_delay_s")
        Spy.seen.append((int(be.timestamp()), tuple(sorted(ctx.products))))
        for pid in ["BTC-USD", "ETH-USD", "NEW-USD"]:
            cs = ctx.candles(pid, 10**6)
            if any(c.end > be for c in cs):
                self.violations.append(f"{pid}: candle ending after {be}")
            if cs and pid in ("BTC-USD", "ETH-USD") and cs[-1].end != be:  # these trade every day
                self.violations.append(f"{pid}: last closed bar ends {cs[-1].end}, expected {be}")
            if [c.start for c in cs] != sorted(c.start for c in cs):
                self.violations.append(f"{pid}: candles not oldest first")
            st = ctx.stats(pid)
            if cs and (st is None or st.last != cs[-1].close):
                self.violations.append(f"{pid}: stats.last is not the last closed close")
            if not cs and pid in ctx.products:
                self.violations.append(f"{pid}: visible without a closed bar")
        if ctx.candles("BTC-USD", 3) != ctx.candles("BTC-USD", 10**6)[-3:]:
            self.violations.append("candles(n) is not the tail of the history")
        return [TargetWeight(p, 1 / len(ctx.products), "spy") for p in sorted(ctx.products)] or None


def test_spy_sees_only_closed_bars() -> None:
    Spy.violations, Spy.seen = [], []
    listing = T0 + 20 * DAY
    ds = SpotDataset.from_rows({"BTC-USD": walk(40, seed=1), "ETH-USD": walk(40, seed=2),
                                "NEW-USD": walk(15, seed=3, start=listing)})
    r = run_spot_backtest(Spy, dataset=ds, start=iso(T0 + 5 * DAY), slippage=0)
    assert Spy.violations == []
    assert r["metrics"]["details"]["strategy_errors"] == 0
    seen = dict(Spy.seen)
    # NEW-USD lists at `listing`: invisible until its first bar has CLOSED (listing + 1 day)
    assert "NEW-USD" not in seen[listing] and "NEW-USD" in seen[listing + DAY]
    assert not any(t["product_id"] == "NEW-USD" and t["ts"] < iso(listing + DAY) for t in r["trades"])


class Recorder(MomentumRotation):
    name = "cb_test_recorder"
    log: ClassVar[list[tuple[int, Any, Any]]] = []

    def on_bar(self, ctx: SpotContext) -> list[TargetWeight] | None:
        visible = tuple((pid, tuple((c.start, c.close) for c in ctx.candles(pid, 50))) for pid in sorted(ctx.products))
        out = super().on_bar(ctx)
        Recorder.log.append((int(ctx.bar_end.timestamp()), visible,
                             None if out is None else [t.to_json() for t in out]))
        return out


def _perturbed(cut: int, favour: str) -> SpotDataset:
    """Same bars before ``cut``; from ``cut`` on, different prices where ``favour`` jumps x3."""
    out = {}
    for pid, seed in (("BTC-USD", 11), ("ETH-USD", 12)):
        base = walk(90, seed=seed, p0=100.0)
        future = walk(90, seed=seed + (100 if favour == "BTC-USD" else 200), p0=100.0)
        rows = []
        for a, b in zip(base, future, strict=True):
            if a[0] < cut:
                rows.append(a)
            else:
                k = 3.0 if pid == favour else 1.0
                rows.append((b[0], b[1] * k, b[2] * k, b[3] * k, b[4] * k, b[5]))
        out[pid] = rows
    return SpotDataset.from_rows(out)


def _run_recorded(ds: SpotDataset) -> tuple[list[Any], dict[str, Any]]:
    Recorder.log = []
    r = run_spot_backtest(Recorder, {"lookback": 3}, dataset=ds, start=iso(T0 + 10 * DAY), slippage=5)
    return list(Recorder.log), r


def _invariant_up_to(cut: int) -> tuple[bool, str]:
    """Decisions/fills/equity up to ``cut`` must not depend on bars that close after ``cut``."""
    log_a, ra = _run_recorded(_perturbed(cut, "BTC-USD"))
    log_b, rb = _run_recorded(_perturbed(cut, "ETH-USD"))
    da = [x for x in log_a if x[0] <= cut]
    db = [x for x in log_b if x[0] <= cut]
    if da != db:
        return False, "decisions differ"
    if [t for t in ra["trades"] if t["ts"] < iso(cut)] != [t for t in rb["trades"] if t["ts"] < iso(cut)]:
        return False, "fills differ"
    if [p for p in ra["equity_curve"] if p["ts"] <= iso(cut)] != [p for p in rb["equity_curve"] if p["ts"] <= iso(cut)]:
        return False, "equity differs"
    if [s for s in ra["signals"] if s["ts"] < iso(cut)] != [s for s in rb["signals"] if s["ts"] < iso(cut)]:
        return False, "signals differ"
    # sanity: the futures really differ (otherwise the check proves nothing)
    assert [x for x in log_a if x[0] > cut + DAY] != [x for x in log_b if x[0] > cut + DAY]
    return True, ""


def test_no_look_ahead_future_bars_cannot_change_the_past() -> None:
    for cut in (T0 + 30 * DAY, T0 + 55 * DAY):
        ok, why = _invariant_up_to(cut)
        assert ok, why


def test_look_ahead_check_catches_a_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: if the context exposed bar t+1, the invariant above must fail."""
    orig = BarSeries.n_closed

    def leaky(self: BarSeries, t: int) -> int:
        return orig(self, t + self.granularity_s)  # also counts the bar that is still open

    monkeypatch.setattr(BarSeries, "n_closed", leaky)
    ok, why = _invariant_up_to(T0 + 30 * DAY)
    assert not ok and why == "decisions differ"


def test_strategy_cannot_mutate_history() -> None:
    class Mutator(SmaTrend):
        name = "cb_test_mutator"

        def on_bar(self, ctx: SpotContext) -> list[TargetWeight] | None:
            cs = ctx.candles("BTC-USD", 5)
            cs.clear()  # a copy: the replay's history is untouched
            with pytest.raises(TypeError):
                ctx.products["X-USD"] = None  # type: ignore[index]
            return super().on_bar(ctx)

    ds = SpotDataset.from_rows({"BTC-USD": walk(60, seed=9)})
    a = run_spot_backtest(Mutator, dataset=ds, benchmarks=False)
    b = run_spot_backtest(SmaTrend, dataset=SpotDataset.from_rows({"BTC-USD": walk(60, seed=9)}), benchmarks=False)
    assert a["metrics"]["details"]["strategy_errors"] == 0
    assert a["trades"] == [dict(t, strategy="cb_test_mutator") for t in b["trades"]]


# --------------------------------------------------------------------------- benchmarks


def test_benchmarks_btc_hold_and_equal_weight() -> None:
    ds = two_asset_dataset(95)
    r = run_spot_backtest(SmaTrend, dataset=ds, slippage=0)
    btc = r["metrics"]["benchmarks"]["btc"]
    assert btc["trades"] == 1 and btc["pct_time_invested"] == pytest.approx(100 * (1 - 1 / len(r["equity_curve"])))
    rows = ds.bars["BTC-USD"]
    first = int((T0 + 10 * DAY - T0) / DAY)  # default start = first bar + history (10)
    px = Decimal(repr(float(rows.open[first])))
    base = (notional_for_budget(Decimal(1000), is_taker=True, tier=DEFAULT_TIER) / px
            // Decimal("0.00000001") * Decimal("0.00000001"))
    cash = 1000 - base * px - fee_for(base * px, is_taker=True, tier=DEFAULT_TIER)
    last = r["benchmarks"]["btc"][-1]["equity"]
    keep = 1 - float(DEFAULT_TIER.taker_rate)  # marked net of the exit fee
    assert last == pytest.approx(float(cash) + float(base) * float(rows.close[-1]) * keep, abs=1e-3)
    ew = r["metrics"]["benchmarks"]["equal_weight"]
    assert ew["trades"] == 1  # universe = BTC only: equal weight == one buy, monthly re-checks inside the band
    r2 = run_spot_backtest(MomentumRotation, dataset=ds, slippage=0)
    ew2 = r2["metrics"]["benchmarks"]["equal_weight"]
    assert ew2["buys"] >= 2  # both products bought at the start, then monthly rebalances
    months = {row["month"] for row in r2["by_month"]}
    assert all(row["equal_weight_return_pct"] is not None and row["btc_return_pct"] is not None
               for row in r2["by_month"]) and len(months) >= 3
    no_bench = run_spot_backtest(SmaTrend, dataset=ds, benchmarks=False)
    assert no_bench["benchmarks"] == {"btc": [], "equal_weight": []}
    assert no_bench["metrics"]["benchmarks"] == {"btc": None, "equal_weight": None}


def test_limits_option_caps_weights() -> None:
    ds = SpotDataset.from_rows({"BTC-USD": walk(30, seed=6)})
    r = run_spot_backtest(BuyAt, {"at": T0 + 3 * DAY}, dataset=ds, slippage=0,
                          limits={"max_position_pct_per_product": 50})
    t = r["trades"][0]
    assert t["notional"] + t["fee"] == pytest.approx(500.0, abs=0.01)
    assert r["metrics"]["benchmarks"]["btc"]["avg_exposure_pct"] > 90  # benchmarks are never capped
    r = run_spot_backtest(BuyAt, {"at": T0 + 3 * DAY}, dataset=ds, slippage=0, allocation_pct=25)
    assert r["trades"][0]["notional"] + r["trades"][0]["fee"] == pytest.approx(250.0, abs=0.01)


# --------------------------------------------------------------------------- metrics


def test_compute_spot_metrics_known_series() -> None:
    curve = [(T0 + i * DAY, e, e, 0.0, e) for i, e in enumerate([100.0, 110.0, 99.0, 121.0])]
    m = compute_spot_metrics(curve, [], starting_balance=100, fees_paid=1.5, traded_notional=300.0,
                             stats={"buys": 2, "sells": 1, "round_trips": 1, "wins": 1})
    assert m["total_return_pct"] == pytest.approx(21.0)
    assert m["max_drawdown_pct"] == pytest.approx(10.0) and m["max_drawdown"] == pytest.approx(11.0)
    years = 3 / 365.25
    assert m["cagr_pct"] == pytest.approx((1.21 ** (1 / years) - 1) * 100, rel=1e-4)
    r = [0.1, -0.1, 121 / 99 - 1]
    mean = sum(r) / 3
    sd = math.sqrt(sum((x - mean) ** 2 for x in r) / 2)
    assert m["sharpe"] == pytest.approx(mean / sd * math.sqrt(365), rel=1e-3)
    assert m["vol_pct"] == pytest.approx(sd * math.sqrt(365) * 100, rel=1e-3)
    dd = math.sqrt(0.01 / 3)
    assert m["sortino"] == pytest.approx(mean / dd * math.sqrt(365), rel=1e-3)
    assert m["turnover_per_year"] == pytest.approx(300 / 107.5 / years, rel=1e-4)
    assert (m["trades"], m["win_rate"], m["pct_time_invested"], m["fees_paid"]) == (3, 1.0, 100.0, 1.5)


def test_flat_market_loses_exactly_the_costs() -> None:
    rows = [(T0 + i * DAY, 100.0, 100.0, 100.0, 100.0, 1e5) for i in range(20)]
    ds = SpotDataset.from_rows({"BTC-USD": rows})
    r = run_spot_backtest(BuyAt, {"at": T0 + 3 * DAY, "sell_at": T0 + 8 * DAY}, dataset=ds, slippage=0)
    m = r["metrics"]
    assert m["total_pnl"] == pytest.approx(-m["fees_paid"], abs=1e-6)
    assert m["win_rate"] == 0.0 and m["round_trips"] == 1


# --------------------------------------------------------------------------- errors and robustness


def test_errors() -> None:
    ds = SpotDataset.from_rows({"BTC-USD": walk(30, seed=1)})
    with pytest.raises(ValueError, match="unknown coinbase strategy"):
        run_spot_backtest("nope", dataset=ds)
    with pytest.raises(ParamError):
        run_spot_backtest(SmaTrend, {"n": 1}, dataset=ds)
    with pytest.raises(ParamError, match="unknown parameter"):
        run_spot_backtest(SmaTrend, {"bogus": 1}, dataset=ds)

    class NotBt(SmaTrend):
        name = "cb_test_notbt"
        backtestable = False

    with pytest.raises(ValueError, match="not backtestable"):
        run_spot_backtest(NotBt, dataset=ds)
    with pytest.raises(SpotBacktestError, match="empty period"):
        run_spot_backtest(SmaTrend, dataset=ds, start="2030-01-01")
    with pytest.raises(ValueError, match="end must be after start"):
        run_spot_backtest(SmaTrend, dataset=ds, start="2024-02-01", end="2024-01-01")
    with pytest.raises(SpotBacktestError, match="universe"):
        run_spot_backtest(SmaTrend, {"product": "ETH-USD"}, dataset=ds)
    with pytest.raises(TypeError, match="unknown backtest option"):
        run_spot_backtest(SmaTrend, dataset=ds, fill="next")
    with pytest.raises(ValueError, match="starting_balance"):
        run_spot_backtest(SmaTrend, dataset=ds, starting_balance=0)
    with pytest.raises(SpotBacktestError, match="does not exist"):
        run_spot_backtest(SmaTrend, data_dir="/nonexistent/cb-data")

    class Hourly(SmaTrend):
        name = "cb_test_hourly"
        bar_granularity_s = 3600

    with pytest.raises(SpotBacktestError, match="needs 3600s"):
        run_spot_backtest(Hourly, dataset=ds)


def test_strategy_exceptions_do_not_stop_the_replay() -> None:
    class Flaky(SmaTrend):
        name = "cb_test_flaky"
        calls = 0

        def on_bar(self, ctx: SpotContext) -> list[TargetWeight] | None:
            Flaky.calls += 1
            if Flaky.calls % 3 == 0:
                raise RuntimeError("boom")
            ctx.log("hello", kind="note")
            return [TargetWeight("BTC-USD", 2.0, "too much"), TargetWeight("XXX-USD", 0.1)]

    ds = SpotDataset.from_rows({"BTC-USD": walk(40, seed=1)})
    r = run_spot_backtest(Flaky, dataset=ds, benchmarks=False)
    d = r["metrics"]["details"]
    assert d["strategy_errors"] > 0 and "boom" in d["errors"][0]
    assert d["strategy_logs"]["note"] > 0 and d["strategy_log_samples"]
    assert any("no leverage" in k for k in d["target_problems"])
    assert any("not an available product" in k for k in d["target_problems"])
    assert r["trades"] and r["trades"][0]["target_weight"] == 1.0


def test_registered_name_and_progress() -> None:
    register(SmaTrend)
    seen: list[float] = []
    try:
        r = run_spot_backtest("cb_test_sma_trend", {"n": 5}, dataset=two_asset_dataset(), progress=seen.append)
    finally:
        unregister("cb_test_sma_trend")
    assert r["params"] == {"n": 5, "product": "BTC-USD"} and seen and 0 <= min(seen) <= max(seen) < 1


def test_delisted_product_is_reported_stuck() -> None:
    ds = SpotDataset.from_rows({"BTC-USD": walk(60, seed=1), "DEAD-USD": walk(20, seed=2)})
    r = run_spot_backtest(BuyAt, {"at": T0 + 5 * DAY, "product": "DEAD-USD"}, dataset=ds, benchmarks=False)
    stuck = r["metrics"]["details"]["stuck_positions"]
    assert [s["product_id"] for s in stuck] == ["DEAD-USD"]
    assert any(s["decision"] == "unfilled" for s in r["signals"]) or r["metrics"]["sells"] == 0


def test_hourly_granularity() -> None:
    class HourlySma(SmaTrend):
        name = "cb_test_hourly_sma"
        bar_granularity_s = 3600
        history_bars = 10

    ds = SpotDataset.from_rows({"BTC-USD": walk(24 * 20, seed=4, g=3600, vol=0.01)}, granularity_s=3600)
    r = run_spot_backtest(HourlySma, dataset=ds)
    assert r["granularity_s"] == 3600
    assert len(r["equity_curve"]) <= 21  # one point per UTC day
    assert r["metrics"]["bars"] == 24 * 20 - 10


# --------------------------------------------------------------------------- research data layout


def _write_research(tmp: Path) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    rows = {"BTC-USD": walk(60, seed=1, p0=40000), "ETH-USD": walk(60, seed=2, p0=2000),
            "USDT-USD": [(T0 + i * DAY, 1.0, 1.0, 1.0, 1.0, 1.0) for i in range(60)]}
    prod, ts, o, h, lo, c, v = [], [], [], [], [], [], []
    for pid, rs in rows.items():
        for r in rs:
            prod.append(pid)
            ts.append(datetime.fromtimestamp(r[0], tz=UTC))
            o.append(r[1])
            h.append(r[2])
            lo.append(r[3])
            c.append(r[4])
            v.append(r[5])
    table = pa.table({"product": pa.array(prod).dictionary_encode(),
                      "ts": pa.array(ts, type=pa.timestamp("ms", tz="UTC")), "open": o, "high": h, "low": lo,
                      "close": c, "volume": v, "usd_volume_est": v})
    pq.write_table(table, tmp / "daily.parquet")
    (tmp / "hourly").mkdir()
    hr = walk(24 * 15, seed=5, g=3600, p0=40000, vol=0.005)
    pq.write_table(pa.table({"product": ["BTC-USD"] * len(hr),
                             "ts": pa.array([datetime.fromtimestamp(r[0], tz=UTC) for r in hr],
                                            type=pa.timestamp("ms", tz="UTC")),
                             "open": [r[1] for r in hr], "high": [r[2] for r in hr], "low": [r[3] for r in hr],
                             "close": [r[4] for r in hr], "volume": [r[5] for r in hr],
                             "usd_volume_est": [r[5] for r in hr]}), tmp / "hourly" / "BTC-USD.parquet")
    meta = [("BTC-USD", "BTC", "USD", "online", "0.00000001", "0.01", "1", False),
            ("ETH-USD", "ETH", "USD", "delisted", "0.001", "0.01", "1", False),
            ("USDT-USD", "USDT", "USD", "online", "0.01", "0.00001", "1", True),
            ("BTC-EUR", "BTC", "EUR", "online", "0.00000001", "0.01", "1", False)]
    pq.write_table(pa.table({
        "product": [m[0] for m in meta], "base": [m[1] for m in meta], "quote": [m[2] for m in meta],
        "status": [m[3] for m in meta], "base_increment_str": [m[4] for m in meta],
        "quote_increment_str": [m[5] for m in meta], "min_market_funds_str": [m[6] for m in meta],
        "is_stablecoin": [m[7] for m in meta], "is_pegged_derivative": [False] * len(meta)}),
        tmp / "products_raw.parquet")
    pq.write_table(pa.table({"product": ["BTC-USD", "BTC-USD", "BTC-USD", "ETH-USD"],
                             "ts": pa.array([datetime.fromtimestamp(T0, tz=UTC)] * 4,
                                            type=pa.timestamp("us", tz="UTC")),
                             "spread_bps": [0.2, 0.4, 10.0, 3.0]}), tmp / "book_snapshot.parquet")


def test_research_data_adapter(tmp_path: Path) -> None:
    _write_research(tmp_path)
    products = load_research_products(tmp_path)
    assert set(products) == {"BTC-USD", "ETH-USD", "USDT-USD"}  # USD quotes only
    assert products["ETH-USD"].status == "online" and products["ETH-USD"].raw["status_now"] == "delisted"
    assert products["USDT-USD"].raw["is_stablecoin"] is True
    r = run_spot_backtest(MomentumRotation, {"lookback": 5}, data_dir=tmp_path, start="2024-01-15", end="2024-02-20")
    d = r["metrics"]["details"]
    assert d["universe"] == ["BTC-USD", "ETH-USD", "USDT-USD"]
    assert d["slippage"]["by_product_bps"] == {"BTC-USD": 0.2, "ETH-USD": 1.5}  # half the median spread
    assert r["start"] == "2024-01-15T00:00:00Z" and r["end"] == "2024-02-21T00:00:00Z"  # end date inclusive
    eth = [t for t in r["trades"] if t["product_id"] == "ETH-USD"]
    for t in eth:
        assert Decimal(str(t["base_size"])) % Decimal("0.001") == 0  # metadata base increment
    assert d["dataset"]["source"] == str(tmp_path)
    # history before `start` is loaded as warm-up: a decision is possible on the first bar
    assert r["signals"] and r["signals"][0]["ts"] == "2024-01-15T00:00:00Z"
    h = run_spot_backtest(type("H", (SmaTrend,), {"name": "cb_test_h", "bar_granularity_s": 3600}),
                          data_dir=tmp_path)
    assert h["granularity_s"] == 3600 and h["metrics"]["bars"] > 0


def test_research_data_dir_default() -> None:
    assert RESEARCH_DATA_DIR == Path(bt.__file__).resolve().parents[2] / "research" / "coinbase" / "data"


@pytest.mark.skipif(not (RESEARCH_DATA_DIR / "daily.parquet").exists(), reason="research dataset not built")
def test_real_research_dataset_smoke() -> None:
    r = run_spot_backtest(SmaTrend, {"n": 50}, start="2024-01-01", end="2024-12-31")
    m = r["metrics"]
    assert r["start"] == "2024-01-01T00:00:00Z" and len(r["equity_curve"]) == 367
    assert m["final_equity"] > 0 and m["benchmarks"]["btc"]["trades"] == 1
    assert m["details"]["universe"] == ["BTC-USD"]
