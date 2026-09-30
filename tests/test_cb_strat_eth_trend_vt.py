"""eth_trend_vt Coinbase spot strategy (research/coinbase/FINDINGS.md) on synthetic candles.

PAPER ONLY. SMA200 signal, 50% volatility target math, band, no look-ahead, restart consistency.
"""

from __future__ import annotations

import math
import random
import statistics
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from kalshibot.coinbase.backtest import SpotDataset, run_spot_backtest
from kalshibot.coinbase.models import Candle
from kalshibot.coinbase.strategies import REGISTRY, normalize_targets
from kalshibot.coinbase.strategies.btc_trend import annualized_vol
from kalshibot.coinbase.strategies.eth_trend_vt import (
    ETH,
    EthTrendVolTarget,
    vol_target_weight,
)

DAY = 86400
T0 = datetime(2024, 1, 1, tzinfo=UTC)


class FakePortfolio:
    def __init__(self, held: float = 0.0) -> None:
        self.alloc_equity = Decimal(1000)
        self.values = {ETH: Decimal(str(1000 * held))} if held else {}

    def quantity(self, pid: str) -> Decimal:
        return Decimal(1) if pid in self.values else Decimal(0)

    def price(self, pid: str) -> Decimal | None:
        return self.values.get(pid)


class FakeCtx:
    def __init__(self, bars: list[Candle], k: int, *, held: float = 0.0, products: tuple[str, ...] = (ETH,)) -> None:
        self._bars = bars
        self.bar_end = bars[k].end
        self.now = self.bar_end + timedelta(seconds=30)
        self.products = {p: object() for p in products}
        self.params: dict[str, Any] = {}
        self.portfolio = FakePortfolio(held)
        self.logs: list[tuple[str, dict[str, Any]]] = []

    def candles(self, pid: str, n: int) -> list[Candle]:
        if pid != ETH:
            return []
        ok = [c for c in self._bars if c.end <= self.bar_end]
        return ok[-n:] if n > 0 else []

    def stats(self, pid: str) -> None:
        return None

    def log(self, msg: str, **data: Any) -> None:
        self.logs.append((msg, data))


def mk_bars(closes: list[float]) -> list[Candle]:
    d = lambda x: Decimal(repr(round(x, 8)))
    return [Candle(product_id=ETH, start=T0 + timedelta(days=i), granularity_s=DAY, open=d(c), high=d(c),
                   low=d(c), close=d(c), volume=Decimal(1)) for i, c in enumerate(closes)]


def zigzag(n: int, p0: float, a: float, drift: float = 0.0) -> list[float]:
    """Closes whose daily returns alternate (1+drift)(1+a) / (1+drift)(1-a)."""
    out, p = [], p0
    for i in range(n):
        p *= (1 + drift) * (1 + (a if i % 2 == 0 else -a))
        out.append(p)
    return out


def weight(res: Any) -> float | None:
    if res is None:
        return None
    tws, problems = normalize_targets(res, [ETH])
    assert not problems and tws is not None and len(tws) == 1
    return tws[0].weight


def test_metadata_and_defaults() -> None:
    assert REGISTRY["eth_trend_vt"] is EthTrendVolTarget
    s = EthTrendVolTarget()
    assert s.experimental and s.backtestable and s.bar_granularity_s == 86400 and s.execution == "taker"
    assert s.rebalance_band == 0.10
    assert s.params == {"sma_n": 200, "target_vol": 0.50, "vol_window": 30}
    assert s.history_bars >= 200
    assert s.description.startswith(
        "Risk strategy: beat holding ETH but trailed holding BTC by ~8–10 pts/yr in testing")
    assert EthTrendVolTarget.class_problems() == []
    assert s.universe({ETH: 1, "BTC-USD": 1}) == [ETH]


def test_vol_target_math() -> None:
    assert vol_target_weight(False, 0.5, 0.8) == 0.0
    assert vol_target_weight(True, 0.5, 1.0) == pytest.approx(0.5)
    assert vol_target_weight(True, 0.5, 0.25) == 1.0  # capped: no leverage
    assert vol_target_weight(True, 0.5, 0.0) == 1.0
    assert math.isnan(vol_target_weight(True, 0.5, math.nan))
    closes = zigzag(40, 100.0, 0.04)
    rets = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - 30, len(closes))]
    assert annualized_vol(closes) == pytest.approx(statistics.stdev(rets) * math.sqrt(365))
    assert math.isnan(annualized_vol(closes[:20]))  # 19 returns < 20
    assert not math.isnan(annualized_vol(closes[:21]))


def test_uptrend_holds_eth_sized_to_target_vol() -> None:
    closes = zigzag(260, 1000.0, 0.05, drift=0.003)  # rising, ~95% annualized vol
    bars = mk_bars(closes)
    res = EthTrendVolTarget().on_bar(FakeCtx(bars, len(bars) - 1))
    vol = annualized_vol(closes, 30, 20)
    assert vol > 0.5
    assert weight(res) == pytest.approx(0.5 / vol)
    why = res[0].reason
    assert why.startswith("ETH close ") and "> SMA200" in why and "→ hold ETH at" in why
    assert f"{0.5 / vol:.0%} = min(1, 50% target vol / {vol:.0%} 30-day vol)" in why


def test_low_vol_uptrend_is_capped_at_full_allocation() -> None:
    closes = zigzag(260, 1000.0, 0.005, drift=0.002)
    res = EthTrendVolTarget().on_bar(FakeCtx(mk_bars(closes), 259))
    assert weight(res) == 1.0 and "(capped at 100%)" in res[0].reason


def test_below_sma_goes_to_cash() -> None:
    closes = zigzag(260, 1000.0, 0.03, drift=-0.002)
    res = EthTrendVolTarget().on_bar(FakeCtx(mk_bars(closes), 259, held=0.4))
    assert weight(res) == 0.0 and "< SMA200" in res[0].reason and "→ cash" in res[0].reason


def test_signal_flip_on_the_bar_it_crosses() -> None:
    closes = [100.0] * 200 + [101.0]
    s = EthTrendVolTarget()
    w_up = weight(s.on_bar(FakeCtx(mk_bars(closes), 200)))
    assert w_up is not None and w_up == 1.0  # tiny vol -> capped at 1
    w_dn = weight(s.on_bar(FakeCtx(mk_bars(closes + [99.0]), 201)))
    assert w_dn == 0.0
    # exactly on the SMA: previous signal kept (research hysteresis with buffer 0)
    closes = [100.0] * 200 + [299.0]
    closes.append(sum(closes[-199:]) / 199)  # 101.0 == SMA200 of the last 200 closes incl. itself
    assert closes[-1] == 101.0 and sum(closes[-200:]) / 200 == 101.0
    res = s.on_bar(FakeCtx(mk_bars(closes), len(closes) - 1))
    assert weight(res) is not None and weight(res) > 0 and "= SMA200" in res[0].reason
    res = s.on_bar(FakeCtx(mk_bars([100.0] * 200 + [50.0, 95.25]), 201))  # still below the SMA: cash
    assert weight(res) == 0.0


def test_vol_not_available_means_no_change() -> None:
    s = EthTrendVolTarget({"sma_n": 20})
    closes = [100.0 + i for i in range(20)]  # 19 returns
    ctx = FakeCtx(mk_bars(closes), 19)
    assert s.on_bar(ctx) is None and "volatility not available" in ctx.logs[-1][0]
    assert EthTrendVolTarget().on_bar(FakeCtx(mk_bars(closes), 19)) is None  # < 200 bars
    assert EthTrendVolTarget().on_bar(FakeCtx(mk_bars([100.0] * 260), 259, products=())) is None


def walk(n: int, seed: int) -> list[float]:
    rng = random.Random(seed)
    out, p = [], 2000.0
    for _ in range(n):
        p *= math.exp(rng.gauss(0.0004, 0.045))
        out.append(p)
    return out


def ref_weights(closes: list[float], n: int = 200, tv: float = 0.5) -> list[float | None]:
    """Research tgt_voltarget (before the shift to the next open); None = hold (NaN)."""
    out: list[float | None] = []
    cur = 0
    for t, c in enumerate(closes):
        if t < n - 1:
            out.append(0.0)
            continue
        m = sum(closes[t - n + 1:t + 1]) / n
        cur = 1 if c > m else 0 if c < m else cur
        rets = [closes[i] / closes[i - 1] - 1 for i in range(max(1, t - 29), t + 1)]
        vol = statistics.stdev(rets) * math.sqrt(365) if len(rets) >= 20 else math.nan
        w = min(1.0, tv / vol) if math.isfinite(vol) else math.nan
        v = cur * w
        out.append(None if math.isnan(v) else v)
    return out


def test_matches_research_formula_and_has_no_look_ahead() -> None:
    closes = walk(520, 21)
    bars = mk_bars(closes)
    ref = ref_weights(closes)
    perturbed = mk_bars(closes[:400] + [c * 5 for c in closes[400:]])
    s = EthTrendVolTarget()
    for k in range(199, 400):
        got = weight(s.on_bar(FakeCtx(bars, k)))
        assert got == pytest.approx(ref[k], rel=1e-9, abs=1e-12), k
        assert weight(s.on_bar(FakeCtx(perturbed, k))) == got
        restarted = EthTrendVolTarget()
        assert weight(restarted.on_bar(FakeCtx(bars, k))) == got


def test_backtest_runs_on_synthetic_data() -> None:
    closes = walk(400, 4)
    rows = [(int((T0 + timedelta(days=i)).timestamp()), c, c, c, c, 10.0) for i, c in enumerate(closes)]
    r = run_spot_backtest(EthTrendVolTarget, dataset=SpotDataset.from_rows({ETH: rows}), benchmarks=False,
                          slippage=0)
    assert r["strategy"] == "eth_trend_vt"
    for t in r["trades"]:
        assert t["reason"].startswith("ETH close ")
