"""Regressions from the analytics / backtester review (2026-09-27).

* readiness: a tail check (the percentile bootstrap cannot see a loss it has not seen), per-strategy
  trade minimums, drawdown against the strategy's own allocation;
* the headline go-live verdict is per strategy, never pooled;
* backtester: the risk check runs at the decision time (not on the book at t + latency), and the
  clock never runs ahead of the tick (no look-ahead with ``latency_s`` > the data step);
* the hourly adapter replays the archived-era holdout candles.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest

from kalshibot.analytics import (
    bootstrap_ratio_ci,
    clopper_pearson_upper,
    compute_analytics,
    readiness,
    tail_stats,
    to_trade,
    trade_stats,
)
from kalshibot.backtest.data import ReplayDataset
from kalshibot.backtest.runner import run_backtest
from kalshibot.config import Settings
from kalshibot.money import D
from kalshibot.paper.models import Settlement
from kalshibot.strategies.base import OrderIntent, Strategy, UniverseSpec

T0 = datetime(2026, 8, 1, tzinfo=UTC)
M0 = 1_790_000_000 - 1_790_000_000 % 86400  # a UTC midnight (epoch s)


def st(i: int, pnl: str, *, count: int = 15, payout: str = "15", strategy: str = "ladder", event: str | None = None,
       fees: str = "0.03", ts: datetime | None = None) -> Settlement:
    # one trade per UTC day by default, so the day clusters of the bootstrap equal the events
    return Settlement(id=i, ticker=f"KX-{i}", result="yes" if D(payout) > 0 else "no", side="yes", count=count,
                      payout=D(payout), cost_basis=D(payout) - D(pnl) - D(fees), pnl=D(pnl),
                      ts=ts or T0 + timedelta(days=i), strategy=strategy, event_ticker=event or f"EV-{i}",
                      kind="settlement", fees=D(fees))


# --------------------------------------------------------------------------- readiness tail check (15)


def test_clopper_pearson_upper_bound() -> None:
    assert clopper_pearson_upper(0, 200) == pytest.approx(1 - 0.05 ** (1 / 200), rel=1e-6)  # 1.49%
    assert clopper_pearson_upper(2, 100) == pytest.approx(0.0612, abs=5e-4)
    assert clopper_pearson_upper(5, 5) == 1.0 and clopper_pearson_upper(0, 0) == 1.0


def test_a_rare_loss_rule_is_not_ready_on_200_wins() -> None:
    wins = [st(i, "0.17") for i in range(200)]  # ladder-like: +$0.17 per win, a loss costs ~$14.83
    s = trade_stats([to_trade(x) for x in wins], min_settled_trades=200, n_boot=300)
    assert s["ci_low"] > 0  # the bootstrap sees no risk at all ...
    t = s["tail"]
    assert t["loss_events"] == 0 and t["loss_rate_upper"] == pytest.approx(0.0149, abs=1e-4)
    assert t["break_even_loss_rate"] == pytest.approx(0.17 / 15.0, abs=1e-4)  # ~1.13%
    assert s["readiness"]["ready"] is False  # ... but 0 losses in 200 cannot rule out a 1.13% loss rate
    assert any(r.startswith("tail check failed") for r in s["readiness"]["reasons"])
    many = [st(i, "0.17") for i in range(2000)]  # 0 losses in 2,000: upper bound 0.15%
    assert trade_stats([to_trade(x) for x in many], min_settled_trades=200, n_boot=100)["readiness"]["ready"]
    # a whole ladder event lost (10 strikes, one event) counts once but at its full size
    lost = many[:400] + [st(3000 + k, "-14.83", payout="0", event="EV-LADDER") for k in range(10)]
    t = tail_stats([to_trade(x) for x in lost])
    assert t["loss_events"] == 1 and t["avg_loss_event"] == pytest.approx(148.3)
    assert not trade_stats([to_trade(x) for x in lost], n_boot=100)["readiness"]["ready"]


def _btc15m_like(lose_on: Any) -> list[Settlement]:
    """300 windows, three a day (100 UTC days); 55 contracts at 0.90: win +$5.10, loss -$49.90."""
    rows = []
    for i in range(300):
        lose = lose_on(i)
        rows.append(st(i, "-49.90" if lose else "5.10", count=55, payout="0" if lose else "55", strategy="btc",
                       fees="0.40", ts=T0 + timedelta(days=i // 3, hours=i % 3)))
    return rows


def test_readiness_passes_the_tail_check_for_a_btc15m_like_record() -> None:
    # 18 losses (6%) spread over 18 different days: the day-clustered CI is still above zero
    rows = _btc15m_like(lambda i: i % 50 in (0, 17, 34))
    s = trade_stats([to_trade(x) for x in rows], min_settled_trades=300, n_boot=500)
    assert s["tail"]["loss_events"] == 18 and s["tail"]["loss_rate_upper"] < s["tail"]["break_even_loss_rate"]
    assert s["readiness"]["ready"] is True and s["ci_low"] > 0


def test_the_same_losses_arriving_three_to_a_day_are_not_ready() -> None:
    # the same 18 losses in 300 trades, but whole days are lost (6 of 100 days, 3 windows each): one
    # regime moves all of a day's windows together, so the per-event CI was far too tight
    rows = _btc15m_like(lambda i: (i // 3) % 17 == 0)
    s = trade_stats([to_trade(x) for x in rows], min_settled_trades=300, n_boot=500)
    assert s["tail"]["loss_events"] == 18 and s["tail"]["loss_rate_upper"] < s["tail"]["break_even_loss_rate"]
    assert s["ci_low"] < 0 and s["readiness"]["ready"] is False
    assert any("must be > 0" in r for r in s["readiness"]["reasons"])
    # resampling the 300 windows as independent events (the old clusters) would have passed them
    events = [t.event for t in (to_trade(x) for x in rows)]
    pnl = [float(x.pnl) for x in rows]
    lo, _ = bootstrap_ratio_ci(pnl, None, events, n_boot=500)
    assert lo is not None and lo > 0


def test_per_strategy_minimums_and_drawdown_against_the_allocation() -> None:
    rows = [st(i, "0.17", strategy="ladder_favorite") for i in range(2000)]
    rows += [st(5000 + k, "-4.0", payout="11", strategy="ladder_favorite", event=f"L{k}") for k in range(10)]
    out = compute_analytics(rows, starting_balance=1000, n_boot=100,
                            min_settled_trades_by_strategy={"ladder_favorite": 3000})
    r = out["by_strategy"]["ladder_favorite"]["readiness"]
    assert not r["ready"] and "need >= 3000" in r["reasons"][0]
    # the -$40 at the end is 3% of the account's peak but 9% of a $100 allocation's: the allocation
    # figure is reported, the gate uses the account (integration change: see analytics docstring)
    out = compute_analytics(rows, starting_balance=1000, n_boot=100, strategy_capital={"ladder_favorite": 100})
    st_ = out["by_strategy"]["ladder_favorite"]
    assert st_["allocation"] == 100 and st_["max_drawdown_pct_of_allocation"] > 8
    assert st_["starting_capital"] == 1000 and st_["max_drawdown_pct"] < 5
    assert out["overall"]["max_drawdown_pct"] < 5  # the account sees a small dent


def test_primary_rule_with_a_losing_streak_passes_the_drawdown_gate() -> None:
    """btc15m-like: $50 stakes against a $100 allocation. An early streak of 3 losses (-$150) is
    150% of the allocation but 15% of the $1,000 account; the rule is +$4.16/trade over 400 trades."""
    rows = []
    for i in range(400):
        lose = i in (3, 4, 5) or i % 40 == 39
        rows.append(st(i, "-49.90" if lose else "5.10", count=55, payout="0" if lose else "55",
                       strategy="btc15m_favorite"))
    out = compute_analytics(rows, starting_balance=1000, n_boot=400, strategy_capital={"btc15m_favorite": 100},
                            min_settled_trades_by_strategy={"btc15m_favorite": 300})
    b = out["by_strategy"]["btc15m_favorite"]
    assert b["max_drawdown_pct_of_allocation"] > 100 and b["max_drawdown_pct"] < 20
    assert not any("drawdown" in r for r in b["readiness"]["reasons"] if "exceeds" in r)
    assert b["readiness"]["ready"] is True, b["readiness"]["reasons"]


# --------------------------------------------------------------------------- headline per strategy (16)


def test_headline_is_ready_only_when_a_single_strategy_is() -> None:
    rows = []
    for i in range(183):  # btc15m-like: +5.1 / -49.9, 4% losses
        lose = i % 50 < 2
        rows.append(st(i, "-49.90" if lose else "5.10", count=55, payout="0" if lose else "55", strategy="btc15m"))
    for i in range(167):
        rows.append(st(1000 + i, "0.17", strategy="ladder"))
    out = compute_analytics(rows, starting_balance=1000, min_settled_trades=200, n_boot=400)
    assert out["overall"]["count"] == 350 and out["overall"]["readiness"]["ready"]  # pooled: was the headline
    assert out["readiness"]["ready"] is False and out["readiness"]["ready_strategies"] == []
    assert out["readiness"]["basis"] == "per_strategy"
    assert [r.split(":")[0] for r in out["readiness"]["reasons"]] == ["btc15m", "ladder"]
    assert all("not ready" in r for r in out["readiness"]["reasons"])


# --------------------------------------------------------------------------- backtester look-ahead (5, 6)


class Once(Strategy):
    name: ClassVar[str] = "once"
    backtestable: ClassVar[bool] = True

    def universe(self) -> UniverseSpec:
        return UniverseSpec(series_tickers=["KXP"])

    async def on_tick(self, ctx: Any) -> list[OrderIntent]:
        if ctx.now.timestamp() != M0 + 120:
            return []
        m = ctx.markets["KXP-A"]
        return [OrderIntent(ticker="KXP-A", side="yes", count=10, limit_price=m.yes_ask, strategy="once",
                            reason="probe", expected_edge=Decimal("0"))]


def _minute_ds(candles: list[tuple[int, float, float]]) -> ReplayDataset:
    markets = [{"ticker": "KXP-A", "event_ticker": "KXP-E", "series_ticker": "KXP", "open_ts": M0,
                "close_ts": M0 + 3600, "settle_ts": M0 + 3610, "result": "yes", "category": "Crypto"}]
    return ReplayDataset.from_records(markets, {"KXP-A": candles}, kind="minute")


def test_risk_is_checked_on_the_decision_time_book() -> None:
    # decision at 00:02 on 0.89/0.90; the 00:03 candle (the fill time) blows out to 0.70/0.95
    ds = _minute_ds([(M0 + 60, 0.89, 0.90), (M0 + 120, 0.89, 0.90), (M0 + 180, 0.70, 0.95), (M0 + 240, 0.89, 0.90)])
    kw = dict(start=datetime.fromtimestamp(M0, UTC), end=datetime.fromtimestamp(M0 + 600, UTC), settings=Settings())
    same = run_backtest(strategy_cls=Once, dataset=ds, fill="same", **kw)
    nxt = run_backtest(strategy_cls=Once, dataset=_minute_ds([(M0 + 60, 0.89, 0.90), (M0 + 120, 0.89, 0.90),
                                                               (M0 + 180, 0.70, 0.95), (M0 + 240, 0.89, 0.90)]),
                       fill="next_ask", **kw)
    assert same.metrics["n_trades"] == 1
    assert nxt.metrics["n_trades"] == 1 and nxt.metrics["risk_rejected"] == 0  # was rejected: "spread > max_spread"
    assert nxt.trades[0]["price"] == 0.95  # filled at the 00:03 ask (next_ask), decided on the 00:02 book


SEEN: list[tuple[int, str, str]] = []


class Probe(Strategy):
    name: ClassVar[str] = "probe"
    backtestable: ClassVar[bool] = True

    def universe(self) -> UniverseSpec:
        return UniverseSpec(series_tickers=["KXP"])

    async def on_tick(self, ctx: Any) -> list[OrderIntent]:
        m = ctx.markets["KXP-A"]
        book = await ctx.orderbook("KXP-A")
        pos = ctx.portfolio.position("KXP-A", "probe")
        SEEN.append((int(ctx.now.timestamp()), str(m.yes_ask), str(book.best_yes_ask), pos.count if pos else 0))
        if ctx.now.timestamp() == M0 + 120:
            return [OrderIntent(ticker="KXP-A", side="yes", count=10, limit_price=m.yes_ask, strategy="probe",
                                reason="probe", expected_edge=Decimal("0"))]
        return []


def test_latency_longer_than_the_step_never_shows_the_future() -> None:
    SEEN.clear()
    ds = _minute_ds([(M0 + 60 * k, 0.48 + k / 100, 0.49 + k / 100) for k in range(1, 15)])  # ask +1c a minute
    r = run_backtest(strategy_cls=Probe, start=datetime.fromtimestamp(M0, UTC),
                     end=datetime.fromtimestamp(M0 + 600, UTC), settings=Settings(), dataset=ds, fill="next_ask",
                     latency_s=180, risk=False)
    assert SEEN and all(snap == book for _, snap, book, _ in SEEN)  # was: the book 3 minutes ahead
    held = {ts: n for ts, _, _, n in SEEN}
    assert held[M0 + 240] == 0 and held[M0 + 300] == 10  # the order reaches the book at 00:05, not before
    assert r.trades[0]["price"] == 0.54  # the 00:05 ask


# --------------------------------------------------------------------------- hourly holdout candles (3)


def test_hourly_adapter_replays_the_archived_holdout_candles(tmp_path: Path) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    from kalshibot.backtest.data import load_hourly

    H = 3600
    root = tmp_path / "research"
    (root / "data").mkdir(parents=True)
    hist = root / "calibration" / "verify_leakage"
    hist.mkdir(parents=True)
    ts = pa.timestamp("us", tz="UTC")
    rows = [  # a live-era event and an archived-era (holdout) event whose second market traded no candles
        ("KXWTI-L1-A", "KXWTI-L1", M0 + 30 * H, "yes"),
        ("KXGOLDD-H1-A", "KXGOLDD-H1", M0 + 20 * H, "no"),
        ("KXGOLDD-H1-B", "KXGOLDD-H1", M0 + 20 * H, "no"),  # fetched, empty candle list
    ]
    n = len(rows)
    us = [r[2] * 10**6 for r in rows]
    pq.write_table(pa.table({
        "ticker": [r[0] for r in rows], "event_ticker": [r[1] for r in rows],
        "series_ticker": [r[0].split("-")[0] for r in rows], "title": [r[0] for r in rows],
        "strike_type": ["greater"] * n, "floor_strike": [1.0] * n, "cap_strike": pa.array([None] * n, type=pa.float64()),
        "open_time": pa.array([M0 * 10**6] * n, type=ts), "close_time": pa.array(us, type=ts),
        "expected_expiration_time": pa.array([u + 300 * 10**6 for u in us], type=ts), "can_close_early": [True] * n,
        "settlement_ts": pa.array([u + 600 * 10**6 for u in us], type=ts), "result": [r[3] for r in rows],
        "settlement_value": [1.0 if r[3] == "yes" else 0.0 for r in rows], "expiration_value": ["1"] * n,
        "price_level_structure": ["linear_cent"] * n,
        "price_ranges": [json.dumps([{"start": "0.0000", "end": "1.0000", "step": "0.0100"}])] * n,
        "category": ["Commodities"] * n, "fee_type": ["quadratic"] * n, "fee_multiplier": [1.0] * n,
        "series_title": ["x"] * n, "frequency": ["daily"] * n,
    }), root / "data" / "markets.parquet")

    def candles(recs: list[tuple[str, int, float, float]]) -> Any:
        return pa.table({"ticker": [r[0] for r in recs], "period": [60] * len(recs),
                         "end_period_ts": pa.array([r[1] for r in recs], type=pa.int64()),
                         "yes_bid_close": pa.array([r[2] for r in recs], type=pa.float32()),
                         "yes_ask_close": pa.array([r[3] for r in recs], type=pa.float32())})

    pq.write_table(candles([("KXWTI-L1-A", M0 + H, 0.97, 0.98)]), root / "data" / "candles_hourly.parquet")
    pq.write_table(candles([("KXGOLDD-H1-A", M0 + H, 0.97, 0.98)]), hist / "candles_hist_hourly.parquet")
    pq.write_table(pa.table({"ticker": ["KXGOLDD-H1-A", "KXGOLDD-H1-B"]}), hist / "hist_fetched_tickers.parquet")
    ds = load_hourly(root)
    assert sorted(ds.tickers) == ["KXGOLDD-H1-A", "KXWTI-L1-A"]  # was: the holdout market was never replayed
    assert ds.info["hist_fetched_tickers"] == 2
    (hist / "hist_fetched_tickers.parquet").unlink()  # without the fetch list the event is incomplete
    assert load_hourly(root).tickers == ["KXWTI-L1-A"]


def test_readiness_helper_accepts_tail_stats() -> None:
    ok = {"count": 250, "ci_low": 0.01, "max_drawdown_pct": 5.0,
          "tail": {"events": 250, "loss_events": 0, "loss_rate_upper": 0.012, "break_even_loss_rate": 0.011,
                   "avg_win_event": 0.17, "avg_loss_event": 15.0, "loss_basis": "no loss yet"}}
    r = readiness(ok, min_settled_trades=200)
    assert not r["ready"] and "tail check failed" in r["reasons"][0]


@pytest.mark.skipif(not os.environ.get("KALSHIBOT_RESEARCH_TESTS"),
                    reason="replays the real research data (~1 min, needs pyarrow): set KALSHIBOT_RESEARCH_TESTS=1")
def test_ladder_holdout_replay_reproduces_the_refutation() -> None:
    pytest.importorskip("pyarrow")
    from kalshibot.backtest.data import RESEARCH_DIR

    if not (RESEARCH_DIR / "calibration" / "verify_leakage" / "candles_hist_hourly.parquet").exists():
        pytest.skip("research data not present")
    research = {"max_position_cost": 100, "max_event_cost": 10000, "max_group_cost": 1e6, "max_total_cost": 1e6,
                "max_intents_per_tick": 50}
    r = run_backtest("ladder_favorite", research, "2026-05-20", "2026-07-27", 1e7, Settings(), risk=False,
                     fill="same", book_size=100)
    m = r.metrics
    # the archived-era holdout (research: -0.2c on 1,424 trades / 187 events with this candle file, -0.9c on
    # the 824-trade arch fetch): about zero with a CI that includes it - not the +1.0c of the selection window
    assert m["n_trades"] > 1400 and m["events"] > 180
    assert m["ev_ci_low"] < 0 < m["ev_ci_high"] and m["ev_per_contract"] < 0.005
    assert sum(t["pnl"] < 0 for t in r.trades) >= 15
