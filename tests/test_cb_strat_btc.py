"""btc_trend and btc_hold Coinbase spot strategies (research/coinbase/FINDINGS.md) on synthetic candles.

PAPER ONLY. Covers entry / exit / buffer hysteresis, no look-ahead, weights and reasons,
restart consistency (state derived from candles + holdings), parameters and a backtester run.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from kalshibot.coinbase.backtest import SpotDataset, run_spot_backtest
from kalshibot.coinbase.models import Candle
from kalshibot.coinbase.strategies import REGISTRY, ParamError, normalize_targets
from kalshibot.coinbase.strategies import btc_trend as btc_trend_module
from kalshibot.coinbase.strategies.btc_hold import BtcHold
from kalshibot.coinbase.strategies.btc_trend import (
    BTC,
    BtcTrend,
    annualized_vol,
    trend_state,
)

DAY = 86400
T0 = datetime(2024, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- fakes


class FakePortfolio:
    def __init__(self, alloc: float = 1000.0, values: dict[str, float] | None = None) -> None:
        self.alloc_equity = Decimal(str(alloc))
        self.values = {k: Decimal(str(v)) for k, v in (values or {}).items()}

    def quantity(self, pid: str) -> Decimal:
        return Decimal(1) if pid in self.values else Decimal(0)

    def price(self, pid: str) -> Decimal | None:
        return self.values.get(pid)


class FakeCtx:
    """SpotContext over a full candle list; only bars with ``end <= bar_end`` are reachable."""

    def __init__(self, bars: list[Candle], k: int, *, held: float = 0.0, delay_s: float = 30,
                 products: tuple[str, ...] = (BTC,), pid: str = BTC) -> None:
        self._bars = {pid: bars}
        self.bar_end = bars[k].end
        self.now = self.bar_end + timedelta(seconds=delay_s)
        self.products = {p: object() for p in products}
        self.params: dict[str, Any] = {}
        self.portfolio = FakePortfolio(1000.0, {pid: 1000.0 * held} if held else {})
        self.logs: list[tuple[str, dict[str, Any]]] = []

    def candles(self, pid: str, n: int) -> list[Candle]:
        ok = [c for c in self._bars.get(pid, []) if c.end <= self.bar_end]
        return ok[-n:] if n > 0 else []

    def stats(self, pid: str) -> None:
        return None

    def log(self, msg: str, **data: Any) -> None:
        self.logs.append((msg, data))


def mk_bars(closes: list[float], pid: str = BTC, start: datetime = T0) -> list[Candle]:
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        d = lambda x: Decimal(repr(round(x, 6)))
        out.append(Candle(product_id=pid, start=start + timedelta(days=i), granularity_s=DAY,
                          open=d(prev), high=d(max(prev, c) * 1.001), low=d(min(prev, c) * 0.999),
                          close=d(c), volume=Decimal(100)))
        prev = c
    return out


def walk(n: int, seed: int, p0: float = 40000.0, vol: float = 0.035) -> list[float]:
    rng = random.Random(seed)
    out, p = [], p0
    for _ in range(n):
        p *= math.exp(rng.gauss(0.0005, vol))
        out.append(p)
    return out


def ref_states(closes: list[float], n: int, b: float) -> list[int]:
    """Research ``common.hysteresis`` (state 0 until the SMA exists / before the first signal)."""
    cur, out = 0, []
    for t, c in enumerate(closes):
        if t < n - 1:
            out.append(0)
            continue
        m = sum(closes[t - n + 1:t + 1]) / n
        cur = 1 if c > m * (1 + b) else 0 if c < m * (1 - b) else cur
        out.append(cur)
    return out


def weight_of(res: list[Any] | None) -> float | None:
    if res is None:
        return None
    tws, problems = normalize_targets(res, [BTC, "ETH-USD"])
    assert not problems
    assert tws is not None and len(tws) == 1
    return tws[0].weight


# --------------------------------------------------------------------------- btc_trend metadata


def test_btc_trend_registered_with_researched_defaults() -> None:
    assert REGISTRY["btc_trend"] is BtcTrend
    s = BtcTrend()
    assert s.params == {"sma_n": 100, "buffer": 0.02, "execution": "taker"}
    assert s.execution == "taker" and s.bar_granularity_s == 86400 and s.backtestable and not s.experimental
    assert s.history_bars == 250  # SMA warm-up + hysteresis replay
    assert BtcTrend.class_problems() == []
    assert ("Risk overlay, not an edge: in 2024–26 testing it matched BTC buy-and-hold's return "
            "(29.1% vs 28.6%/yr, difference not significant) with about half the drawdown (−32% vs −53%)") \
        in s.description
    info = s.info()
    assert info["param_schema"]["sma_n"]["default"] == 100
    assert info["param_schema"]["buffer"]["default"] == 0.02
    doc = btc_trend_module.__doc__ or ""
    assert "one day late" in doc and "-7 pts/yr" in doc and "00:00 UTC" in doc


def test_btc_trend_params_execution_and_history() -> None:
    s = BtcTrend({"execution": "maker_then_taker", "sma_n": 50, "buffer": 0.0})
    assert s.execution == "maker_then_taker" and s.info()["execution"] == "maker_then_taker"
    assert s.history_bars == 200
    assert BtcTrend.execution == "taker"  # class attribute untouched
    with pytest.raises(ParamError):
        BtcTrend.resolve_params({"execution": "limit"}, strict=True)
    with pytest.raises(ParamError):
        BtcTrend.resolve_params({"sma_n": 5}, strict=True)
    with pytest.raises(ParamError):
        BtcTrend.resolve_params({"buffer": -0.01}, strict=True)
    assert BtcTrend().universe({BTC: 1, "ETH-USD": 1}) == [BTC]
    assert BtcTrend().universe({"ETH-USD": 1}) == []


# --------------------------------------------------------------------------- btc_trend rule


def flat_then(level: float, tail: list[float], n: int = 100) -> list[float]:
    return [level] * n + tail


def test_entry_above_upper_band_targets_full_allocation_with_reason() -> None:
    bars = mk_bars(flat_then(100.0, [103.0]))  # SMA100 = 100.03 -> upper 102.03
    ctx = FakeCtx(bars, len(bars) - 1)
    res = BtcTrend().on_bar(ctx)
    assert weight_of(res) == 1.0
    why = res[0].reason
    assert why.startswith("BTC close 103.00 (2024-04-10) > 1.02×SMA100 102.03 (SMA100 100.03) → hold BTC"), why
    assert res[0].score == pytest.approx(103 / 100.03 - 1)
    assert res[0].expected_edge_bps is None  # no edge claimed


def test_entry_reason_uses_thousands_separators() -> None:
    bars = mk_bars(flat_then(83100.0, [86000.0]))
    res = BtcTrend().on_bar(FakeCtx(bars, len(bars) - 1))
    assert "BTC close 86,000 (2024-04-10) > 1.02×SMA100" in res[0].reason and "(SMA100 83,129)" in res[0].reason


def test_exit_below_lower_band_goes_to_cash() -> None:
    bars = mk_bars(flat_then(100.0, [110.0] * 5 + [95.0]))
    # holding BTC: exit target 0 with a reason
    res = BtcTrend().on_bar(FakeCtx(bars, len(bars) - 1, held=0.99))
    assert weight_of(res) == 0.0
    assert "< 0.98×SMA100" in res[0].reason and res[0].reason.split(" [")[0].endswith("→ cash")


def test_buffer_hysteresis_keeps_state_inside_band() -> None:
    s = BtcTrend()
    # out -> inside the band above the SMA (but below 1.02x): no entry
    bars = mk_bars(flat_then(100.0, [101.5]))
    res = s.on_bar(FakeCtx(bars, len(bars) - 1))
    assert weight_of(res) == 0.0 and "stay in cash" in res[0].reason
    # the same close with buffer 0 enters
    assert weight_of(BtcTrend({"buffer": 0.0}).on_bar(FakeCtx(bars, len(bars) - 1))) == 1.0

    # in (entered at 110) -> falls back inside the band below the SMA: still in
    closes = flat_then(100.0, [110.0] + [99.5] * 3)
    bars = mk_bars(closes)
    k = len(bars) - 1
    res = s.on_bar(FakeCtx(bars, k))  # not holding (e.g. the entry never filled): buy
    assert weight_of(res) == 1.0
    assert "keep holding BTC" in res[0].reason and "last signal: 2024-04-10 close 110.00 above 1.02×SMA100" \
        in res[0].reason
    ctx = FakeCtx(bars, k, held=0.99)  # already holding: no resize
    assert s.on_bar(ctx) is None
    assert "no trade" in ctx.logs[-1][0]

    # out (exited at 90) -> back inside the band above the SMA: stays out
    closes = flat_then(100.0, [110.0, 90.0, 101.0])
    bars = mk_bars(closes)
    res = s.on_bar(FakeCtx(bars, len(bars) - 1, held=0.99))
    assert weight_of(res) == 0.0 and "stay in cash" in res[0].reason


def test_states_match_research_hysteresis_on_random_walks() -> None:
    for seed in range(4):
        closes = walk(700, seed)
        bars = mk_bars(closes)
        ref = ref_states(closes, 100, 0.02)
        s = BtcTrend()
        held = 0.0
        for k in range(99, len(bars)):
            res = s.on_bar(FakeCtx(bars, k, held=held))
            if res is None:  # in and already holding
                assert held >= 0.5
                state = 1
            else:
                state = int(weight_of(res) == 1.0)
            # before any signal the research state is 0; ours follows the holdings (0 then)
            assert state == ref[k], (seed, k)
            held = 0.99 if state else 0.0


def test_no_look_ahead_future_bars_do_not_change_decisions() -> None:
    closes = walk(400, 11)
    bars = mk_bars(closes)
    cut = 300
    perturbed = mk_bars(closes[:cut + 1] + [c * 3 if i % 2 else c / 3 for i, c in enumerate(closes[cut + 1:])])
    s = BtcTrend()
    for k in range(150, cut + 1):
        a = s.on_bar(FakeCtx(bars, k))
        b = s.on_bar(FakeCtx(perturbed, k))
        assert (a is None and b is None) or [(t.weight, t.reason) for t in a] == [(t.weight, t.reason) for t in b]
    # and the decision equals one made on a truncated history
    trunc = bars[:cut + 1]
    a = s.on_bar(FakeCtx(bars, cut))
    b = s.on_bar(FakeCtx(trunc, cut))
    assert [(t.weight, t.reason) for t in a] == [(t.weight, t.reason) for t in b]


def test_restart_consistency_fresh_instance_matches_long_running_one() -> None:
    closes = walk(600, 5)
    bars = mk_bars(closes)
    long_running = BtcTrend()
    held = 0.0
    for k in range(120, len(bars)):
        ctx_a, ctx_b = FakeCtx(bars, k, held=held), FakeCtx(bars, k, held=held)
        a = long_running.on_bar(ctx_a)
        restarted = BtcTrend(dict(long_running.params))
        restarted.load_state(long_running.dump_state())
        b = restarted.on_bar(ctx_b)
        wa, wb = weight_of(a), weight_of(b)
        assert wa == wb and (a is None or a[0].reason == b[0].reason), k
        if wa is not None:
            held = 0.99 if wa == 1.0 else 0.0
    assert long_running.dump_state() is None  # nothing persisted: state comes from candles + holdings


def test_missed_bars_catch_up_to_the_same_state() -> None:
    """Engine down for a few days: the next bar's decision is the one an uninterrupted run holds."""
    closes = flat_then(100.0, [110.0, 101.0, 100.5, 99.9])  # entry, then 3 in-band days
    bars = mk_bars(closes)
    res = BtcTrend().on_bar(FakeCtx(bars, len(bars) - 1))
    assert weight_of(res) == 1.0 and "keep holding" in res[0].reason


def test_holdings_fallback_when_no_close_left_the_band() -> None:
    bars = mk_bars([100.0] * 260)  # never outside +/-2%
    res = BtcTrend().on_bar(FakeCtx(bars, len(bars) - 1))
    assert weight_of(res) == 0.0 and "following current holdings, 0% BTC" in res[0].reason
    ctx = FakeCtx(bars, len(bars) - 1, held=0.99)
    assert BtcTrend().on_bar(ctx) is None
    assert "following current holdings, 99% BTC" in ctx.logs[-1][0]


def test_insufficient_or_stale_history_makes_no_change() -> None:
    bars = mk_bars([100.0] * 99 + [120.0])
    s = BtcTrend()
    ctx = FakeCtx(bars, 98)  # 99 bars < SMA100
    assert s.on_bar(ctx) is None and "need 100" in ctx.logs[-1][0]
    ctx = FakeCtx(bars, 99, products=())
    assert s.on_bar(ctx) is None
    # candles stop 5 days before bar_end
    bars = mk_bars(flat_then(100.0, [110.0]))
    ctx = FakeCtx(bars, len(bars) - 1)
    ctx.bar_end = ctx.bar_end + timedelta(days=5)
    ctx.now = ctx.bar_end + timedelta(seconds=30)
    assert s.on_bar(ctx) is None and "stale" in ctx.logs[-1][0]
    # the final bar missing (not published yet): no decision on the older close
    ctx = FakeCtx(bars, len(bars) - 1)
    ctx.bar_end += timedelta(days=1)
    ctx.now = ctx.bar_end + timedelta(seconds=30)
    assert s.on_bar(ctx) is None and "is not published" in ctx.logs[-1][0]


def test_late_decision_is_flagged_in_the_reason() -> None:
    bars = mk_bars(flat_then(100.0, [103.0]))
    on_time = BtcTrend().on_bar(FakeCtx(bars, len(bars) - 1, delay_s=30))
    late = BtcTrend().on_bar(FakeCtx(bars, len(bars) - 1, delay_s=5 * 3600))
    assert "after the" not in on_time[0].reason
    assert "decided 5.0 h after the 2024-04-11 00:00 UTC close" in late[0].reason
    assert "research: acting a day late cost ~7 pts/yr" in late[0].reason
    assert late[0].weight == 1.0  # still acts


def test_trend_state_helper() -> None:
    assert trend_state([1.0] * 5, 10, 0.02).state is None
    ts = trend_state([100.0] * 10 + [103.0, 101.0], 10, 0.02)
    assert ts.state is True and ts.signal_index == 10
    assert ts.sma == pytest.approx(sum(([100.0] * 10 + [103.0, 101.0])[-10:]) / 10)
    assert math.isnan(annualized_vol([100.0] * 5))


def test_btc_trend_backtests_on_synthetic_data() -> None:
    closes = flat_then(40000.0, [40000.0 * (1.01 ** i) for i in range(1, 30)] + [30000.0] * 20, n=260)
    rows = [(int((T0 + timedelta(days=i)).timestamp()), c, c, c, c, 10.0) for i, c in enumerate(closes)]
    ds = SpotDataset.from_rows({BTC: rows})
    r = run_spot_backtest(BtcTrend, dataset=ds, benchmarks=False, slippage=0)
    trades = r["trades"]
    assert [t["side"] for t in trades] == ["buy", "sell"]
    assert "> 1.02×SMA100" in trades[0]["reason"] and "< 0.98×SMA100" in trades[1]["reason"]
    # decided at the close of the first bar above 1.02 x SMA, filled at the next bar's open
    first_up = next(i for i, c in enumerate(closes) if i >= 99 and c > 1.02 * sum(closes[i - 99:i + 1]) / 100)
    assert trades[0]["ts"].startswith((T0 + timedelta(days=first_up + 1)).date().isoformat())


# --------------------------------------------------------------------------- btc_hold


def test_btc_hold_metadata() -> None:
    assert REGISTRY["btc_hold"] is BtcHold
    s = BtcHold()
    assert s.description.startswith(
        "Benchmark: buys BTC once and holds — the bar every other Coinbase strategy has to beat")
    assert not s.experimental and s.backtestable and s.bar_granularity_s == 86400
    assert BtcHold.class_problems() == []
    assert s.universe({BTC: 1}) == [BTC]


def test_btc_hold_buys_once_then_holds_forever() -> None:
    bars = mk_bars(walk(50, 3))
    s = BtcHold()
    res = s.on_bar(FakeCtx(bars, 10))
    assert weight_of(res) == 1.0 and "buy BTC" in res[0].reason
    for k in range(11, 50):  # held: never trades again, whatever the price does
        assert s.on_bar(FakeCtx(bars, k, held=0.99)) is None
        assert s.on_bar(FakeCtx(bars, k, held=0.6)) is None
        assert s.on_bar(FakeCtx(bars, k, held=1.4)) is None  # never trims
    assert weight_of(s.on_bar(FakeCtx(bars, 49, held=0.0))) == 1.0  # restart with no BTC: buy
    assert s.on_bar(FakeCtx(bars, 49, products=())) is None


def test_btc_hold_backtest_buys_once() -> None:
    closes = walk(120, 8)
    rows = [(int((T0 + timedelta(days=i)).timestamp()), c, c, c, c, 10.0) for i, c in enumerate(closes)]
    r = run_spot_backtest(BtcHold, dataset=SpotDataset.from_rows({BTC: rows}), benchmarks=False, slippage=0)
    assert [t["side"] for t in r["trades"]] == ["buy"]
