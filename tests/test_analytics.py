"""Analytics math: bootstrap CI (clustered), Brier, calibration, drawdown, readiness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from kalshibot.analytics import (
    bootstrap_ratio_ci,
    brier_score,
    calibration_buckets,
    compute_analytics,
    drawdown,
    readiness,
    to_trade,
    trade_stats,
)
from kalshibot.money import D
from kalshibot.paper.models import Settlement

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def st(i: int, pnl: str, *, count: int = 1, strategy: str = "s1", event: str | None = None, kind: str = "settlement",
       payout: str | None = None, fv: float | None = None, ee: str | None = None, fees: str = "0.01") -> Settlement:
    p = D(pnl)
    return Settlement(id=i, ticker=f"KX-{i}", result="yes" if kind == "settlement" else "closed", side="yes",
                      count=count, payout=D(payout) if payout is not None else (D(count) if p > 0 else D(0)),
                      cost_basis=D("0.5") * count, pnl=p, ts=T0 + timedelta(days=i), strategy=strategy,
                      event_ticker=event or f"EV-{i}", kind=kind, fees=D(fees),
                      expected_edge=D(ee) if ee is not None else None, fair_value=fv)


def test_bootstrap_mean_ci_matches_normal_theory() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(0.05, 1.0, 2000)
    lo, hi = bootstrap_ratio_ci(x, n_boot=2000, seed=0)
    se = x.std(ddof=1) / np.sqrt(x.size)
    assert lo == pytest.approx(x.mean() - 1.96 * se, abs=0.3 * se)
    assert hi == pytest.approx(x.mean() + 1.96 * se, abs=0.3 * se)
    assert bootstrap_ratio_ci(x, n_boot=500, seed=3) == bootstrap_ratio_ci(x, n_boot=500, seed=3)  # deterministic


def test_bootstrap_clustering_widens_the_interval() -> None:
    # 50 events x 10 perfectly correlated trades each: effective n is 50, not 500.
    rng = np.random.default_rng(2)
    ev = rng.normal(0, 1, 50)
    x = np.repeat(ev, 10)
    clusters = np.repeat(np.arange(50), 10)
    lo_i, hi_i = bootstrap_ratio_ci(x, seed=0)
    lo_c, hi_c = bootstrap_ratio_ci(x, None, list(clusters), seed=0)
    assert (hi_c - lo_c) > 2.5 * (hi_i - lo_i)
    assert bootstrap_ratio_ci([1.0, 2.0], None, ["a", "a"]) == (None, None)  # one cluster: no CI
    assert bootstrap_ratio_ci([], None) == (None, None)


def test_bootstrap_ratio_per_contract() -> None:
    num = [2.0, -1.0, 3.0, 0.5] * 25
    den = [4, 1, 10, 5] * 25
    lo, hi = bootstrap_ratio_ci(num, den, seed=0)
    point = sum(num) / sum(den)
    assert lo < point < hi


def test_brier_and_calibration() -> None:
    assert brier_score([], []) is None
    assert brier_score([1.0, 0.0], [1.0, 0.0]) == 0.0
    assert brier_score([0.5, 0.5], [1.0, 0.0]) == 0.25
    b = calibration_buckets([0.05, 0.15, 0.12, 0.95, 1.0], [0, 1, 0, 1, 1])
    assert [x["bucket"] for x in b] == ["0.0–0.1", "0.1–0.2", "0.9–1.0"]
    assert b[1] == {"bucket": "0.1–0.2", "lo": 0.1, "hi": 0.2, "n": 2, "mean_fair_value": 0.135,
                    "realized_rate": 0.5}
    assert b[2]["n"] == 2  # p = 1.0 lands in the last bucket


def test_drawdown() -> None:
    assert drawdown([]) == (0.0, 0.0)
    usd, pct = drawdown([100, 120, 90, 130, 117])
    assert usd == 30 and pct == pytest.approx(25.0)


def test_readiness_rules() -> None:
    ok = {"count": 250, "ci_low": 0.01, "max_drawdown_pct": 5.0}
    r = readiness(ok, min_settled_trades=200, max_drawdown_pct=20)
    assert r["ready"] and len(r["reasons"]) == 3  # no tail stats given: that check is skipped
    r = readiness({**ok, "count": 10}, min_settled_trades=200)
    assert not r["ready"] and r["reasons"] == ["only 10 settled trades (need >= 200)"]
    r = readiness({**ok, "ci_low": -0.02})
    assert not r["ready"] and "must be > 0" in r["reasons"][0]
    r = readiness({**ok, "ci_low": None})
    assert not r["ready"] and "no confidence interval" in r["reasons"][0]
    r = readiness({**ok, "max_drawdown_pct": 30.0}, max_drawdown_pct=20)
    assert not r["ready"] and "exceeds the 20% limit" in r["reasons"][0]


def test_trade_stats_numbers() -> None:
    rows = [st(1, "0.40", count=2, payout="2", fv=0.8, ee="0.10"),
            st(2, "-0.50", count=1, payout="0", fv=0.6, ee="0.05"),
            st(3, "0.20", count=1, kind="close", fv=0.7),
            st(4, "0.10", count=1, payout="1")]
    s = trade_stats([to_trade(r) for r in rows], starting_balance=100, min_settled_trades=2, n_boot=500)
    assert s["count"] == 4 and s["settled"] == 3 and s["closed"] == 1 and s["contracts"] == 5
    assert s["total_pnl"] == pytest.approx(0.2) and s["realized_pnl"] == s["total_pnl"]
    assert s["mean_pnl_per_trade"] == pytest.approx(0.05) and s["mean_pnl_per_contract"] == pytest.approx(0.04)
    assert s["wins"] == 3 and s["win_rate"] == 0.75
    assert s["expected_edge_total"] == pytest.approx(0.15) and s["trades_with_edge"] == 2
    assert s["realized_pnl_with_edge"] == pytest.approx(-0.10)
    assert s["edge_capture"] == pytest.approx(-0.10 / 0.15, abs=1e-4)
    # Brier over resolved rows with a fair value only (the close is excluded): (0.8-1)^2, (0.6-0)^2
    assert s["brier_n"] == 2 and s["brier"] == pytest.approx((0.04 + 0.36) / 2)
    assert s["fees"] == pytest.approx(0.04)
    # realized curve 100, 100.4, 99.9, 100.1, 100.2 -> max dd 0.5 from 100.4
    assert s["max_drawdown"] == pytest.approx(0.5) and s["max_drawdown_pct"] == pytest.approx(0.498, abs=1e-3)
    assert s["ci_basis"] == "trade" and s["ci_low"] is not None and s["ci_low"] <= 0.05 <= s["ci_high"]
    assert s["ci_trade_low"] == s["ci_low"]
    assert s["readiness"]["ready"] is False  # CI straddles 0


def test_compute_analytics_shape_and_readiness() -> None:
    rng = np.random.default_rng(7)
    rows = []
    for i in range(300):
        win = rng.random() < 0.6
        rows.append(st(i, "0.40" if win else "-0.30", payout="1" if win else "0", fv=0.62,
                       strategy="good" if i % 2 else "meh"))
    out = compute_analytics(rows, starting_balance=Decimal(1000), min_settled_trades=200, n_boot=800)
    assert set(out) >= {"overall", "by_strategy", "calibration", "readiness", "params"}
    assert set(out["by_strategy"]) == {"good", "meh"}
    ov = out["overall"]
    # the pooled verdict is informational only; the headline is per strategy (150 trades each < 200)
    assert ov["count"] == 300 and ov["ci_low"] > 0 and ov["readiness"]["ready"] is True
    assert out["readiness"]["ready"] is False and out["readiness"]["ready_strategies"] == []
    assert out["by_strategy"]["good"]["count"] == 150
    assert not out["by_strategy"]["good"]["readiness"]["ready"]  # < 200 trades
    assert any(r.startswith("good: not ready (only 150 settled trades") for r in out["readiness"]["reasons"])
    ok = compute_analytics(rows, starting_balance=Decimal(1000), min_settled_trades=100, n_boot=800)
    assert ok["readiness"]["ready"] is True and ok["readiness"]["ready_strategies"] == ["good", "meh"]
    assert out["calibration"] == [{"bucket": "0.6–0.7", "lo": 0.6, "hi": 0.7, "n": 300, "mean_fair_value": 0.62,
                                   "realized_rate": out["calibration"][0]["realized_rate"]}]
    assert out["by_strategy"]["good"]["calibration"][0]["n"] == 150
    # a big mark-to-market drawdown in the equity curve blocks readiness
    out2 = compute_analytics(rows, starting_balance=1000, equity_curve=[1000, 1100, 700, 1050], n_boot=200)
    assert out2["overall"]["max_drawdown_pct"] == pytest.approx(36.3636, abs=1e-3)
    assert not out2["readiness"]["ready"]
    empty = compute_analytics([])
    assert empty["overall"]["count"] == 0 and empty["overall"]["mean_pnl_per_trade"] is None
    assert empty["calibration"] == [] and empty["readiness"]["ready"] is False


def test_accepts_dict_rows() -> None:
    rows = [{"ticker": "A-1", "event_ticker": "A", "pnl": "0.1", "count": 1, "payout": "1", "strategy": "x",
             "kind": "settlement", "ts": T0, "fair_value": 0.5},
            {"ticker": "B-1", "pnl": -0.2, "count": 2, "payout": 0, "strategy": "x", "ts": T0 + timedelta(1)}]
    out = compute_analytics(rows, n_boot=100)
    assert out["overall"]["count"] == 2 and out["overall"]["total_pnl"] == pytest.approx(-0.1)
