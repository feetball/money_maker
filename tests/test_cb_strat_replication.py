"""Replication of the Coinbase research results by the shipped strategies + backtester.

PAPER / RESEARCH ONLY. Opt-in: replays the real research candles (``research/coinbase/data``),
so it runs only with ``KALSHIBOT_RESEARCH_TESTS=1`` (a few seconds).

What is checked
1. **Rules, every day.** ``btc_trend`` and ``eth_trend_vt`` ``on_bar`` against an independent
   numpy port of the research signal code (``research/coinbase/strategies/common.py``:
   ``sma``/``hysteresis``/``trend_state``, ``run_daily.py::tgt_voltarget``) on every daily close
   of the full history: BTC in/out state and ETH target weight must be identical.
2. **Backtester vs the research simulator.** A numpy port of ``common.simulate`` (single asset)
   run under the backtester's conventions - start in cash on 2024-01-01 (the published research
   run started in 2015, so it is already invested on the first test day and pays no entry cost)
   and the final holding marked net of the exit cost (the backtester marks holdings net of the
   exit taker fee + slippage) - must give the same trade days and, within a small tolerance, the
   same return as ``run_spot_backtest`` at the research cost convention (taker fee + 2 bps).
3. **Published numbers.** The backtester stays within the documented gap of the published
   TEST figures (``research/coinbase/strategies/selected_sensitivity.csv``). At 0.90% the gap is
   ~0.85 pt/yr: ~0.43 entry cost (cash start) + ~0.43 exit cost (net-of-fee mark), plus ~0.13 for
   ETH because the planner's buy ``quote_size`` includes the fee (fractional targets land ~0.9%
   under weight). At 1.20% each of those grows (~1.1-1.4 pt/yr).
"""

from __future__ import annotations

import csv
import math
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from kalshibot.coinbase.backtest import RESEARCH_DATA_DIR, load_research_dataset, run_spot_backtest
from kalshibot.coinbase.strategies.btc_trend import BtcTrend
from kalshibot.coinbase.strategies.eth_trend_vt import EthTrendVolTarget

pytestmark = [
    pytest.mark.skipif(not os.environ.get("KALSHIBOT_RESEARCH_TESTS"),
                       reason="replays the real research data: set KALSHIBOT_RESEARCH_TESTS=1"),
    pytest.mark.skipif(not (RESEARCH_DATA_DIR / "daily.parquet").exists(), reason="research dataset not built"),
]

DAY = 86400
BTC, ETH = "BTC-USD", "ETH-USD"
TEST_START, TEST_END = "2024-01-01", "2026-09-25"  # research common.TEST (end inclusive)
SLIP_BPS = 2.0  # research: max(2 bps floor, median $10k book cost) = 2.0 for BTC and ETH
SENS_CSV = Path(__file__).resolve().parents[1] / "research" / "coinbase" / "strategies" / "selected_sensitivity.csv"
RESEARCH_CFG = {"btc_trend": "A|BTC-USD|sma(100, 0.02)", "btc_hold": "E|buyhold BTC",
                "eth_trend_vt": "B|ETH-USD|sma200|tv0.5|band0.1"}
TIERS = {"intro": 0.009, "intro_pre_2026_09": 0.012}


# --------------------------------------------------------------------------- data


def _ts(day: str) -> int:
    return int(np.datetime64(day, "s").astype(np.int64))


@pytest.fixture(scope="module")
def ds() -> Any:
    return load_research_dataset(product_ids=[BTC, ETH])


def _series(ds: Any, pid: str) -> dict[str, Any]:
    """Contiguous daily arrays of ``pid`` (ETH: after its 2-day gap of May 2016, which the
    research loader fills with flat bars and the backtester does not)."""
    b = ds.bars[pid]
    st = b.start
    gaps = np.nonzero(np.diff(st) != DAY)[0]
    i0 = int(gaps[-1]) + 1 if len(gaps) else 0
    assert i0 == 0 or st[i0] < _ts("2016-07-01"), f"{pid}: unexpected gap in the daily bars"
    return {"i0": i0, "start": st[i0:], "open": b.open[i0:], "close": b.close[i0:],
            "candles": b.candle_list()[i0:]}


# --------------------------------------------------------------------------- research port


def _sma(c: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(c), np.nan)
    cs = np.concatenate([[0.0], np.cumsum(c)])
    out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def _hysteresis(c: np.ndarray, m: np.ndarray, buffer: float) -> np.ndarray:
    """research common.hysteresis(c > m(1+b), c < m(1-b), avail & m.notna()): state at close t."""
    st = np.zeros(len(c))
    cur = 0.0
    for t in range(len(c)):
        if np.isnan(m[t]):
            cur = 0.0
        elif c[t] > m[t] * (1 + buffer):
            cur = 1.0
        elif c[t] < m[t] * (1 - buffer):
            cur = 0.0
        st[t] = cur
    return st


def _vol30(c: np.ndarray) -> np.ndarray:
    """research: ret_cc.rolling(30, min_periods=20).std() * sqrt(365)."""
    r = np.full(len(c), np.nan)
    r[1:] = c[1:] / c[:-1] - 1
    out = np.full(len(c), np.nan)
    for t in range(len(c)):
        w = r[max(0, t - 29):t + 1]
        w = w[~np.isnan(w)]
        if len(w) >= 20:
            out[t] = w.std(ddof=1) * math.sqrt(365)
    return out


def _research_weights(s: dict[str, Any], strat: str) -> np.ndarray:
    """Target weight decided at close t (index t); the research trades it at open t+1."""
    c = s["close"]
    if strat == "btc_hold":
        return np.ones(len(c))
    if strat == "btc_trend":
        return _hysteresis(c, _sma(c, 100), 0.02)
    st = _hysteresis(c, _sma(c, 200), 0.0)
    w = np.minimum(1.0, 0.5 / _vol30(c))
    out = st * w
    return np.where(np.isnan(out), 0.0, out)  # research: .shift(1).fillna(0.0)


def _research_sim(s: dict[str, Any], strat: str, taker: float, *, fresh: bool) -> dict[str, Any]:
    """Port of research ``common.simulate`` for one asset over the TEST window.

    ``fresh``: flat before TEST start (the backtester's convention). Returns equity (open-to-open
    compounding), the drifted weight at the end, the cost rate and the fill days."""
    band = 0.10 if strat == "eth_trend_vt" else 0.0
    cr = taker + SLIP_BPS / 1e4
    o = s["open"]
    R = np.zeros(len(o))
    R[:-1] = o[1:] / o[:-1] - 1
    tgt = np.zeros(len(o))
    tgt[1:] = _research_weights(s, strat)[:-1]
    t0, t1 = _ts(TEST_START), _ts(TEST_END)
    days = s["start"]
    if fresh:
        tgt[days < t0] = 0.0
    w, eq, fills = 0.0, 1.0, []
    for t in range(len(o)):
        if days[t] > t1:
            break
        tg = tgt[t]
        d = tg - w
        trade = (tg == 0 and w > 0) or (w == 0 and tg > 0) or abs(d) > band
        trade = trade and (abs(d) >= 1e-3 or tg == 0)
        dw = d if trade else 0.0
        if dw > 0:  # buys paid from cash incl. costs
            dw *= min(1.0, max(0.0, (1.0 - w) / (dw + dw * cr)))
        w += dw
        cost = abs(dw) * cr
        r = w * R[t] - cost
        cash = 1.0 - w - cost
        hv = w * (1 + R[t])
        w = hv / (hv + cash) if hv + cash > 0 else 0.0
        if w < 1e-12:
            w = 0.0
        if days[t] >= t0:
            eq *= 1 + r
            if dw != 0:
                fills.append(str(np.datetime64(int(days[t]), "s"))[:10])
    return {"equity": eq, "w_end": w, "cr": cr, "fills": fills}


# --------------------------------------------------------------------------- 1. rules, every day


class _Ctx:
    """Minimal SpotContext: closed bars up to index ``k``, an empty portfolio."""

    def __init__(self, pid: str, candles: list[Any], k: int, params: dict[str, Any], product: Any) -> None:
        self._pid, self._candles, self._k = pid, candles, k
        self.bar_end = candles[k - 1].end
        self.now = self.bar_end + timedelta(seconds=60)  # engine bar_delay_s default
        self.products = {pid: product}
        self.params = params
        self.portfolio = SimpleNamespace(alloc_equity=Decimal(0))  # current_weight -> 0.0

    def candles(self, pid: str, n: int) -> list[Any]:
        return self._candles[max(0, self._k - n):self._k] if pid == self._pid else []

    def stats(self, pid: str) -> None:
        return None

    def log(self, msg: str, **data: Any) -> None:
        pass


@pytest.mark.parametrize(("strat", "cls", "pid"), [("btc_trend", BtcTrend, BTC), ("eth_trend_vt", EthTrendVolTarget, ETH)])
def test_rule_matches_research_every_day(ds: Any, strat: str, cls: type, pid: str) -> None:
    s = _series(ds, pid)
    ref = _research_weights(s, strat)
    st = cls(cls.resolve_params(None, strict=True))
    n = int(st.params["sma_n"])
    first = n + 60  # SMA + volatility warm-up
    mism: list[str] = []
    checked = 0
    for k in range(first, len(s["candles"]) + 1):
        out = st.on_bar(_Ctx(pid, s["candles"], k, st.params, ds.products[pid]))
        day = str(s["candles"][k - 1].start.date())
        want = float(ref[k - 1])
        if out is None:
            mism.append(f"{day}: None, research {want:.6f}")
            continue
        got = out[0].weight if out else 0.0
        checked += 1
        if abs(got - want) > 1e-9:
            mism.append(f"{day}: strategy {got:.9f}, research {want:.9f}")
    assert checked > 3000
    assert not mism, f"{len(mism)} days differ from the research rule, e.g. {mism[:5]}"


# --------------------------------------------------------------------------- 2./3. backtests


@pytest.fixture(scope="module")
def runs() -> dict[tuple[str, str], dict[str, Any]]:
    out = {}
    for tier in TIERS:
        for strat in RESEARCH_CFG:
            out[(tier, strat)] = run_spot_backtest(strat, start=TEST_START, end=TEST_END, fee_tier=tier,
                                                   slippage=SLIP_BPS, starting_balance=100_000)
    return out


def _published() -> dict[tuple[str, float], dict[str, float]]:
    if not SENS_CSV.exists():
        pytest.skip(f"{SENS_CSV} missing")
    with SENS_CSV.open() as f:
        return {(r["config"], round(float(r["taker"]), 4)): {k: float(r[k]) for k in ("cagr", "mdd", "sharpe")}
                for r in csv.DictReader(f) if r.get("taker")}


def _cagr(total: float, years: float) -> float:
    return (total ** (1 / years) - 1) * 100


@pytest.mark.parametrize("tier", list(TIERS))
@pytest.mark.parametrize("strat", list(RESEARCH_CFG))
def test_backtester_matches_research_simulator(ds: Any, runs: dict, tier: str, strat: str) -> None:
    res = runs[(tier, strat)]
    m = res["metrics"]
    assert res["start"] == "2024-01-01T00:00:00Z" and res["end"] == "2026-09-26T00:00:00Z"
    assert m["details"]["fill_price"] == "open"
    pid = ETH if strat == "eth_trend_vt" else BTC
    ref = _research_sim(_series(ds, pid), strat, TIERS[tier], fresh=True)
    ours_days = sorted({t["ts"][:10] for t in res["trades"]})
    if strat == "eth_trend_vt" and tier != "intro":
        # at 1.20% one band-edge resize moves by a day (the fee-inclusive buy sizing leaves the
        # weight slightly lower, so the 10% band is crossed a day later)
        assert len(set(ours_days) ^ set(ref["fills"])) <= 2, (ours_days, ref["fills"])
    else:
        assert ours_days == ref["fills"]
    years = m["years"]  # 999 days / 365.25 (research: / 365 - a 0.02 pt/yr difference)
    ref_total = ref["equity"] * (1 - ref["w_end"] * ref["cr"])  # exit cost on the final holding
    tol = 0.5 if strat == "eth_trend_vt" else 0.05
    assert m["cagr_pct"] == pytest.approx(_cagr(ref_total, years), abs=tol)
    assert m["benchmarks"]["btc"]["cagr_pct"] == pytest.approx(
        _cagr(_research_sim(_series(ds, BTC), "btc_hold", TIERS[tier], fresh=True)["equity"]
              * (1 - TIERS[tier] - SLIP_BPS / 1e4), years), abs=0.05)


@pytest.mark.parametrize("tier", list(TIERS))
@pytest.mark.parametrize("strat", list(RESEARCH_CFG))
def test_backtester_within_documented_gap_of_published(runs: dict, tier: str, strat: str) -> None:
    pub = _published()[(RESEARCH_CFG[strat], TIERS[tier])]
    m = runs[(tier, strat)]["metrics"]
    gap = pub["cagr"] - m["cagr_pct"]
    # always below: entry cost (cash start) + exit cost (net-of-fee mark) are not in the published run
    hi = 1.0 if tier == "intro" else 1.5
    assert 0.3 < gap < hi, f"{strat} @ {tier}: published {pub['cagr']:.2f}, backtester {m['cagr_pct']:.2f}"
    assert abs(m["max_drawdown_pct"] + pub["mdd"]) < 1.0  # ours positive, research negative
    assert m["sharpe"] == pytest.approx(pub["sharpe"], abs=0.05)


def test_research_port_reproduces_published(ds: Any) -> None:
    """Sanity of the port itself: the warm (2015-start) run gives the published TEST CAGR."""
    pub = _published()
    for strat, cfg in RESEARCH_CFG.items():
        pid = ETH if strat == "eth_trend_vt" else BTC
        for tier, taker in TIERS.items():
            ref = _research_sim(_series(ds, pid), strat, taker, fresh=False)
            assert _cagr(ref["equity"], 999 / 365) == pytest.approx(pub[(cfg, taker)]["cagr"], abs=0.02), (strat, tier)


def test_maker_then_taker_is_recorded_but_filled_as_taker(runs: dict) -> None:
    """The backtester has no maker model: ``maker_then_taker`` only changes the recorded execution."""
    r = run_spot_backtest("btc_trend", {"execution": "maker_then_taker"}, start=TEST_START, end=TEST_END,
                          fee_tier="intro", slippage=SLIP_BPS, starting_balance=100_000)
    assert r["metrics"]["details"]["options"]["strategy_execution"] == "maker_then_taker"
    assert all(t["is_taker"] for t in r["trades"])
    assert r["metrics"]["cagr_pct"] == runs[("intro", "btc_trend")]["metrics"]["cagr_pct"]
