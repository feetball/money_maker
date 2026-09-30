"""Backtest runner: the real strategies, risk manager and paper broker replayed over synthetic history.

Covers look-ahead (quotes/outcomes only as of each snapshot), the fill modes (same / next /
next_ask), settlement at the recorded result, metrics, the ladder and BTC15m strategies end to
end, the CLI (``--param``, ``--opt``, ``--save``, ``--trades``) and the API route with the real runner.
"""

from __future__ import annotations

import csv
import gzip
import json
import math
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest
from conftest import DummyStrategy, FakeKalshiClient
from fastapi.testclient import TestClient

from kalshibot.backtest.data import ReplayDataset
from kalshibot.backtest.runner import (
    BacktestResult,
    _options,
    _parse_when,
    compute_metrics,
    run_backtest,
    run_backtest_async,
)
from kalshibot.cli import main
from kalshibot.config import Settings
from kalshibot.feeds import FeedRegistry
from kalshibot.fees import trading_fee
from kalshibot.money import D
from kalshibot.store import Store
from kalshibot.strategies.base import OrderIntent, Strategy, UniverseSpec

H = 3600
T0 = 1_790_000_000 - 1_790_000_000 % 86400  # a UTC midnight


def dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC)


def iso(ts: int) -> str:
    return dt(ts).isoformat().replace("+00:00", "Z")


def day(ts: int) -> str:
    return dt(ts).date().isoformat()


def quiet_settings(**risk: Any) -> Settings:
    s = Settings()
    for k, v in risk.items():
        setattr(s.risk, k, v)
    return s


# --------------------------------------------------------------------------- hourly ladder data


#: the ladder's pre-review sizing ($15 positions, $30 per event), which these scenarios were built on
LADDER15 = {"max_position_cost": 15, "max_event_cost": 30}


def ladder_dataset() -> ReplayDataset:
    def m(ticker: str, series: str, cat: str, eet: int, result: str, **kw: Any) -> dict[str, Any]:
        close = eet - 300
        return {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "series_ticker": series,
                "open_ts": T0, "close_ts": close, "eet_ts": eet, "settle_ts": close + 600, "result": result,
                "category": cat, "can_close_early": True, **kw}

    markets = [
        m("KXWTI-E1-A", "KXWTI", "Commodities", T0 + 60 * H, "yes"),
        m("KXWTI-E1-B", "KXWTI", "Commodities", T0 + 100 * H, "no"),  # 0.97 early, but EET > 72h away
        m("KXNBAGAME-E2-C", "KXNBAGAME", "Sports", T0 + 30 * H, "yes"),  # wrong category
        m("KX10YRDIRHM-E3-D", "KX10YRDIRHM", "Financials", T0 + 30 * H, "yes"),  # outcome-timing series
        m("KXWTI-E4-E", "KXWTI", "Commodities", T0 + 30 * H, "yes"),  # one-sided book
    ]
    candles = {
        "KXWTI-E1-A": [(T0 + H, 0.95, 0.96), (T0 + 10 * H, 0.97, 0.98)],
        "KXWTI-E1-B": [(T0 + 2 * H, 0.97, 0.98)],
        "KXNBAGAME-E2-C": [(T0 + 2 * H, 0.98, 0.99)],
        "KX10YRDIRHM-E3-D": [(T0 + 2 * H, 0.98, 0.99)],
        "KXWTI-E4-E": [(T0 + 2 * H, 0.97, 1.0)],
    }
    return ReplayDataset.from_records(markets, candles)


def test_ladder_favorite_end_to_end() -> None:
    ds = ladder_dataset()
    r = run_backtest("ladder_favorite", LADDER15, day(T0), day(T0 + 5 * 86400), 1000, quiet_settings(), dataset=ds)
    assert isinstance(r, BacktestResult)
    trades = {t["ticker"]: t for t in r.trades}
    assert set(trades) == {"KXWTI-E1-A", "KXWTI-E1-B"}
    fee = trading_fee(D("0.98"), 15, is_taker=True)
    assert fee == D("0.03")
    a, b = trades["KXWTI-E1-A"], trades["KXWTI-E1-B"]
    assert a["ts"] == iso(T0 + 10 * H)  # first hour with bid >= 0.97 (EET 50h away)
    assert b["ts"] == iso(T0 + 29 * H)  # the first hour with EET - t < 72h
    for t in (a, b):
        assert t["count"] == 15 and t["price"] == 0.98 and t["fee"] == 0.03 and t["side"] == "yes"
        assert t["category"] == "Commodities" and "B4-ladder-72h" in t["reason"]
    assert a["result"] == "yes" and a["pnl"] == pytest.approx(15 * 0.02 - 0.03)
    assert a["settled_at"] == iso(T0 + 60 * H - 300 + 600)  # at the recorded settlement time
    assert b["result"] == "no" and b["pnl"] == pytest.approx(-15 * 0.98 - 0.03)
    m = r.metrics
    total = 15 * 0.02 - 0.03 - 15 * 0.98 - 0.03
    assert m["n_trades"] == 2 and m["contracts"] == 30 and m["events"] == 1
    assert m["total_pnl"] == pytest.approx(total) and m["final_equity"] == pytest.approx(1000 + total)
    assert m["ev_per_contract"] == pytest.approx(total / 30, abs=1e-6)
    assert m["hit_rate"] == 0.5 and m["fees"] == pytest.approx(0.06)
    assert m["ev_ci_low"] is None  # one event: no clustered CI
    assert m["expected_edge_per_contract"] == pytest.approx(0.993 - 0.98 - 0.002, abs=1e-6)
    # peak: A settled (+0.27) while B is marked at its 0.97 bid (-0.18); then B's payout of 0
    assert m["max_drawdown"] == pytest.approx(0.27 - 0.18 - total) and m["data"] == "hourly"
    assert m["fill_mode"] == "same"
    assert m["details"]["strategy_errors"] == 0 and m["details"]["ticks"] > 0
    assert r.by_month == [{"month": day(T0)[:7], "pnl": pytest.approx(total), "trades": 2, "contracts": 30,
                           "win_rate": 0.5, "ev_per_contract": pytest.approx(total / 30, abs=1e-6)}]
    curve = r.equity_curve
    assert curve[0]["equity"] == 1000 and curve[-1]["equity"] == pytest.approx(1000 + total)
    assert all(p["ts"] <= q["ts"] for p, q in zip(curve, curve[1:]))
    out = r.to_json()
    assert set(out) == {"metrics", "equity_curve", "trades", "by_month"}
    json.dumps(out)  # JSON-safe


def test_ladder_risk_limits_apply() -> None:
    ds = ladder_dataset()
    r = run_backtest("ladder_favorite", LADDER15, None, None, 1000, quiet_settings(max_position_cost_per_market=5),
                     dataset=ds)
    assert [t["count"] for t in r.trades] == [5, 5]
    assert r.metrics["details"]["risk_trimmed"] == 2 and r.metrics["details"]["signals"]["partial"] == 2
    off = run_backtest("ladder_favorite", LADDER15, None, None, 1000, quiet_settings(max_position_cost_per_market=5),
                       dataset=ds, risk=False)
    assert [t["count"] for t in off.trades] == [15, 15]


def test_kill_switch_trips_and_is_released_the_next_day() -> None:
    def m(k: int, eet_h: int, result: str) -> dict[str, Any]:
        close = T0 + eet_h * H - 300
        return {"ticker": f"KXWTI-E{k}-X", "event_ticker": f"KXWTI-E{k}", "series_ticker": "KXWTI", "open_ts": T0,
                "close_ts": close, "eet_ts": T0 + eet_h * H, "settle_ts": close + 60, "result": result,
                "category": "Commodities"}

    ds = ReplayDataset.from_records(
        [m(1, 5, "no"), m(2, 20, "yes"), m(3, 40, "yes")],
        {"KXWTI-E1-X": [(T0 + H, 0.97, 0.98)], "KXWTI-E2-X": [(T0 + 6 * H, 0.97, 0.98)],
         "KXWTI-E3-X": [(T0 + 30 * H, 0.97, 0.98)]})
    r = run_backtest("ladder_favorite", LADDER15, None, None, 1000, quiet_settings(daily_loss_limit=10), dataset=ds)
    # E1 loses $14.73 on day 0 -> the kill switch blocks E2 (same day); E3 trades on day 1
    assert [t["ticker"] for t in r.trades] == ["KXWTI-E1-X", "KXWTI-E3-X"]
    d = r.metrics["details"]
    assert d["kill_switch_days"] == 1 and r.metrics["risk_rejected"] == 1
    assert any("kill" in k for k in d["rejection_reasons"]) or "risk" in d["rejection_reasons"]
    assert any(sg["decision"] == "rejected" and "kill switch" in sg["decision_reason"] for sg in r.signals)


def test_period_bounds_and_hold_to_settlement() -> None:
    ds = ladder_dataset()
    # decisions stop at the end of Day 0 (T0 + 24h): only A (10h) trades; it is held to its settlement
    r = run_backtest("ladder_favorite", {}, day(T0), day(T0), 1000, quiet_settings(), dataset=ds)
    assert [t["ticker"] for t in r.trades] == ["KXWTI-E1-A"]
    assert r.metrics["details"]["settled_after_end"] == 1 and r.metrics["details"]["open_at_end"] == 1
    assert r.metrics["details"]["period"]["end"] == iso(T0 + 86400)
    # a run starting later sees A still at 0.97/0.98 and enters it at its first tick (fresh strategy)
    r2 = run_backtest("ladder_favorite", {}, day(T0 + 86400), None, 1000, quiet_settings(), dataset=ds)
    assert [(t["ticker"], t["ts"]) for t in r2.trades] == [("KXWTI-E1-A", iso(T0 + 86400)),
                                                           ("KXWTI-E1-B", iso(T0 + 29 * H))]
    with pytest.raises(ValueError):
        run_backtest("ladder_favorite", {}, "2030-01-01", "2030-01-02", 1000, quiet_settings(), dataset=ds)


# --------------------------------------------------------------------------- look-ahead


class Recorder(Strategy):
    name: ClassVar[str] = "recorder"
    backtestable: ClassVar[bool] = True

    def __init__(self, params: Any = None) -> None:
        super().__init__(params)
        self.seen: list[tuple[int, str, Any, Any, str, Any, Any, Any]] = []

    def universe(self) -> UniverseSpec:
        return UniverseSpec(max_days_to_close=10)

    async def on_tick(self, ctx: Any) -> list[OrderIntent]:
        now = int(ctx.now.timestamp())
        for t, m in sorted(ctx.markets.items()):
            b = await ctx.orderbook(t)
            self.seen.append((now, t, m.yes_bid, m.yes_ask, m.result, m.settlement_value, b.best_yes_bid,
                              b.best_yes_ask))
        return []


def test_no_look_ahead_in_snapshots() -> None:
    ds = ladder_dataset()
    candles = {"KXWTI-E1-A": [(T0 + H, 0.95, 0.96), (T0 + 10 * H, 0.97, 0.98)],
               "KXWTI-E1-B": [(T0 + 2 * H, 0.97, 0.98)]}
    rec: list[Recorder] = []

    class Rec(Recorder):
        def __init__(self, params: Any = None) -> None:
            super().__init__(params)
            rec.append(self)

    run_backtest("recorder", {}, None, None, 1000, quiet_settings(), dataset=ds, strategy_cls=Rec)
    seen = rec[0].seen
    assert seen
    for now, t, bid, ask, result, value, bbid, bask in seen:
        assert result == "" and value is None  # the outcome never leaks into a snapshot
        if t in candles:
            ended = [c for c in candles[t] if c[0] <= now]
            assert ended, "a market is only shown once a candle has ended"
            _, eb, ea = ended[-1]
            assert (bid, ask) == (D(str(eb)), D(str(ea))) == (bbid, bask)
    times = {now for now, *_ in seen}
    assert min(times) == T0 + H and all(x % H == 0 for x in times)  # hourly grid
    # A is never shown after its actual close
    a_close = T0 + 60 * H - 300
    assert max(now for now, t, *_ in seen if t == "KXWTI-E1-A") < a_close


# --------------------------------------------------------------------------- minute BTC15M data


def btc_research_dir(root: Path, n_windows: int = 14) -> tuple[Path, int]:
    """research/crypto_fv-shaped files: KXBTC15M windows (strike far below spot -> favourite YES at
    0.90, 0.91 one minute after the decision), Coinbase bars and settled windows for the basis."""
    cf = root / "research" / "crypto_fv"
    (cf / "data").mkdir(parents=True, exist_ok=True)
    base = T0 + 12 * H
    first = base - 180 * 60
    last = base + 900 * n_windows + 3600
    closes = {}
    lines = ["ts,low,high,open,close,volume"]
    prev = 60000.0
    for k, ts in enumerate(range(first, last, 60)):
        c = 60000.0 + 20 * math.sin(k / 5) + 3 * math.cos(k / 2)
        lines.append(f"{ts},{min(prev, c) - 1:.2f},{max(prev, c) + 1:.2f},{prev:.2f},{c:.2f},2.0")
        closes[ts + 60] = c
        prev = c
    (cf / "data" / "spot_BTC-USD.csv").write_text("\n".join(lines) + "\n")
    mrows, crows = [], []
    for k in range(1, n_windows + 1):
        close = base + 900 * k
        opened = close - 900
        t = f"KXBTC15M-W{k:02d}-00"
        mrows.append({"series": "KXBTC15M", "event_ticker": f"KXBTC15M-W{k:02d}", "cadence": "fifteen_min",
                      "ticker": t, "strike_type": "greater_or_equal", "floor_strike": "59000.00", "cap_strike": "",
                      "open_ts": opened, "close_ts": close, "result": "yes",
                      "expiration_value": f"{closes[close] + 5:.2f}", "settlement_value": "1.0000",
                      "volume": "1000", "open_interest": "10", "pls": "tapered_deci_cent",
                      "settlement_ts": iso(close + 6)})
        for j in range(1, 16):
            ts = opened + 60 * j
            bid, ask = ("0.9000", "0.9100") if ts == close - 540 else ("0.8900", "0.9000")
            crows.append({"ticker": t, "ts": ts, "yes_bid": bid, "yes_ask": ask, "volume": "5", "price_close": ask,
                          "oi": "5"})
    for name, rows in (("markets", mrows), ("candles", crows)):
        with gzip.open(cf / "data" / f"{name}_KXBTC15M.csv.gz", "wt", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    return root / "research", base


FIXED = {"sizing": "fixed", "contracts": 20}


@pytest.mark.parametrize("fill,price,n", [("next_ask", 0.91, 3), ("same", 0.90, 3), ("next", None, 0)])
def test_btc15m_end_to_end_fill_modes(tmp_path: Path, fill: str, price: float | None, n: int) -> None:
    root, base = btc_research_dir(tmp_path)
    r = run_backtest("btc15m_favorite", FIXED, None, None, 1000, quiet_settings(), data_dir=str(root), fill=fill)
    m = r.metrics
    assert m["data"] == "minute" and m["fill_mode"] == fill
    # windows 1..11 lack 10 settled windows for the basis: the model is unavailable -> no trade
    assert m["details"]["strategy_logs"].get("no_model") == 11
    assert m["n_trades"] == n
    if n:
        assert [t["ticker"] for t in r.trades] == [f"KXBTC15M-W{k:02d}-00" for k in (12, 13, 14)]
        lat = 0 if fill == "same" else 60
        for k, t in zip((12, 13, 14), r.trades, strict=True):
            close = base + 900 * k
            assert t["ts"] == iso(close - 600 + lat)  # decided at exactly 10:00 before close
            assert t["price"] == price and t["count"] == 20 and t["side"] == "yes" and t["result"] == "yes"
            fee = float(trading_fee(D(str(price)), 20, is_taker=True))
            assert t["fee"] == fee and t["pnl"] == pytest.approx(20 * (1 - price) - fee)
            assert t["fair_value"] > 0.99 and "model-confirmed" in t["reason"]
    else:  # the IOC at the decision ask (0.90) meets a 0.91 ask one minute later
        assert m["unfilled_signals"] == 3 and m["details"]["signals"] == {"unfilled": 3}


def test_btc15m_blind_mode_and_async_entry(tmp_path: Path) -> None:
    import asyncio

    root, _ = btc_research_dir(tmp_path)
    r = asyncio.run(run_backtest_async("btc15m_favorite", {**FIXED, "use_model": False}, None, None, 1000,
                                       quiet_settings(), data_dir=str(root)))
    assert r.metrics["n_trades"] == 14  # the blind favourite needs no basis history


# --------------------------------------------------------------------------- options / metrics


def test_options_precedence_and_validation() -> None:
    s = Settings.model_validate({"backtest": {"book_size": 7, "fill": "next", "bogus": 1}})
    o = _options(s, {})
    assert o["book_size"] == 7 and o["fill"] == "next" and "bogus" not in o
    assert _options(s, {"book_size": "9", "risk": "false"})["book_size"] == 9
    assert _options(s, {"risk": "false"})["risk"] is False
    assert _options(None, {"categories": "Crypto, Financials"})["categories"] == ["Crypto", "Financials"]
    with pytest.raises(ValueError):
        _options(None, {"nope": 1})
    with pytest.raises(ValueError):
        _options(None, {"fill": "later"})
    with pytest.raises(ValueError):
        _options(None, {"book_size": 0})
    assert _parse_when("2026-09-03") == datetime(2026, 9, 3, tzinfo=UTC)
    assert _parse_when("2026-09-03", end=True) == datetime(2026, 9, 4, tzinfo=UTC)  # inclusive end day
    assert _parse_when("2026-09-03T12:00:00Z", end=True) == datetime(2026, 9, 3, 12, tzinfo=UTC)
    with pytest.raises(ValueError):
        _parse_when("tomorrow")


def test_not_backtestable_is_refused() -> None:
    class Live(Strategy):
        name: ClassVar[str] = "live_only"

        async def on_tick(self, ctx: Any) -> list[OrderIntent]:
            return []

    with pytest.raises(ValueError, match="not backtestable"):
        run_backtest("live_only", {}, dataset=ladder_dataset(), strategy_cls=Live)
    r = run_backtest("live_only", {}, dataset=ladder_dataset(), strategy_cls=Live, force=True)
    assert r.metrics["n_trades"] == 0


def test_compute_metrics() -> None:
    def tr(ts: str, ev: str, pnl: float, count: int, price: float = 0.9) -> dict[str, Any]:
        return {"ts": ts, "ticker": ev + "-X", "event_ticker": ev, "pnl": pnl, "count": count, "price": price,
                "expected_edge": 0.01}

    trades = [tr("2026-07-01T00:00:00Z", "E1", 1.0, 10), tr("2026-07-02T00:00:00Z", "E2", -9.0, 10),
              tr("2026-08-01T00:00:00Z", "E3", 2.0, 20), tr("2026-08-02T00:00:00Z", "E3", 1.0, 10)]
    curve = [{"ts": f"2026-07-0{d}T00:00:00Z", "equity": e} for d, e in ((1, 100.0), (2, 110.0), (3, 99.0),
                                                                         (4, 104.0))]
    m, months = compute_metrics(trades, curve, starting_balance=100, final_equity=95, fees=0.5, n_boot=500)
    assert m["n_trades"] == 4 and m["contracts"] == 50 and m["events"] == 3
    assert m["ev_per_contract"] == pytest.approx(-5 / 50) and m["hit_rate"] == 0.75
    assert m["ev_per_trade"] == pytest.approx((0.1 - 0.9 + 0.1 + 0.1) / 4)
    assert m["ev_ci_low"] <= m["ev_per_contract"] <= m["ev_ci_high"]
    assert m["total_pnl"] == -5 and m["total_return_pct"] == -5 and m["realized_pnl"] == -5
    assert m["ev_per_trade_ci"][0] <= m["ev_per_trade"] <= m["ev_per_trade_ci"][1]
    assert m["max_drawdown"] == 11 and m["max_drawdown_pct"] == pytest.approx(10.0)
    assert m["sharpe"] is not None and m["expected_edge_per_contract"] == pytest.approx(0.01)
    assert [x["month"] for x in months] == ["2026-07", "2026-08"]
    assert months[0] == {"month": "2026-07", "pnl": -8.0, "trades": 2, "contracts": 20, "win_rate": 0.5,
                         "ev_per_contract": -0.4}
    empty, none = compute_metrics([], [], starting_balance=100, final_equity=100, fees=0)
    assert empty["n_trades"] == 0 and empty["ev_per_contract"] is None and none == []


# --------------------------------------------------------------------------- CLI


def write_cfg(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"account: {{starting_balance: 1000}}\nstorage: {{path: {tmp_path / 'db.sqlite3'}}}\n")
    return cfg


def test_cli_backtest_save_and_trades(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, _ = btc_research_dir(tmp_path)
    cfg = write_cfg(tmp_path)
    out_csv = tmp_path / "trades.csv"
    rc = main(["-c", str(cfg), "backtest", "--strategy", "btc15m_favorite", "--param", "sizing=fixed",
               "--param", "contracts=20", "--opt", f"data_dir={root}", "--fill", "same", "--no-risk", "--save",
               "--trades", str(out_csv)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["metrics"]["n_trades"] == 3 and out["metrics"]["fill_mode"] == "same" and out["trades"] == 3
    assert out["metrics"]["details"]["options"]["risk"] is False
    with open(out_csv, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 3 and rows[0]["price"] == "0.9"
    store = Store(tmp_path / "db.sqlite3")
    try:
        bt = store.get_backtest(out["id"])
    finally:
        store.close()
    assert bt is not None and bt["status"] == "done" and bt["strategy"] == "btc15m_favorite"
    assert bt["params"]["contracts"] == 20 and len(bt["trades"]) == 3 and bt["metrics"]["n_trades"] == 3


def test_cli_backtest_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, _ = btc_research_dir(tmp_path)
    cfg = write_cfg(tmp_path)
    base = ["-c", str(cfg), "backtest", "--strategy", "btc15m_favorite", "--opt", f"data_dir={root}"]
    assert main([*base, "--param", "bogus=1"]) == 2
    assert main([*base, "--param", "novalue"]) == 2
    assert main([*base, "--opt", "nope=1"]) == 2
    assert main([*base, "--start", "2031-01-01", "--end", "2031-01-02"]) == 2  # outside the data
    assert main(["-c", str(cfg), "backtest", "--strategy", "maker_favorite"]) == 2  # not backtestable
    err = capsys.readouterr().err
    assert "bogus" in err and "nope" in err and "not backtestable" in err


# --------------------------------------------------------------------------- API


def test_api_runs_the_real_runner(tmp_path: Path) -> None:
    from kalshibot.api.server import build_services, create_app

    root, _ = btc_research_dir(tmp_path, n_windows=4)
    s = Settings()
    s.storage.path = str(tmp_path / "api.sqlite3")
    s.engine.autostart = False
    s.backtest = {"data_dir": str(root), "risk": False}  # type: ignore[attr-defined]
    svc = build_services(s, client=FakeKalshiClient(), strategies={"dummy": DummyStrategy}, feeds=FeedRegistry())
    app = create_app(s, services=svc, autostart=False, frontend_dist=tmp_path / "nodist")
    with TestClient(app) as c:
        r = c.post("/api/backtests", json={"strategy": "dummy", "params": {"days": 0, "series": ["KXBTC15M"],
                                                                           "max_price": 0.95, "count": 3}})
        assert r.status_code == 202
        bt_id = r.json()["id"]
        d: dict[str, Any] = {}
        for _ in range(300):
            d = c.get(f"/api/backtests/{bt_id}").json()
            if d["status"] != "running":
                break
            time.sleep(0.05)
        assert d["status"] == "done", d.get("error")
        assert d["metrics"]["n_trades"] == 4 and d["metrics"]["fill_mode"] == "next_ask"
        assert len(d["trades"]) == 4 and d["trades"][0]["count"] == 3 and d["trades"][0]["price"] == 0.9
        assert d["equity_curve"] and d["by_month"][0]["trades"] == 4
        rows = c.get("/api/backtests").json()
        assert rows[0]["metrics"]["n_trades"] == 4
        assert Decimal(str(d["metrics"]["fees"])) > 0
