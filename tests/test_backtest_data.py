"""Backtest data layer: replay datasets, the time-aware sim market data, and the two research loaders."""

from __future__ import annotations

import asyncio
import csv
import gzip
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kalshibot.backtest.data import (
    BacktestDataError,
    ReplayDataset,
    ReplayMarketData,
    load_minute,
    minute_series_available,
)
from kalshibot.kalshi.client import KalshiNotFound
from kalshibot.money import D
from kalshibot.paper.sim import ManualClock
from kalshibot.strategies.base import UniverseSpec

H = 3600
T0 = 1_790_000_000 - 1_790_000_000 % 86400  # a UTC midnight


def dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC)


def small_dataset(**kw: Any) -> ReplayDataset:
    markets = [
        # normal market: closes at its schedule, 5 minutes before the EET
        {"ticker": "KXGOLD-E1-A", "event_ticker": "KXGOLD-E1", "series_ticker": "KXGOLD", "open_ts": T0,
         "close_ts": T0 + 10 * H, "eet_ts": T0 + 10 * H + 300, "settle_ts": T0 + 10 * H + 600, "result": "yes",
         "can_close_early": True, "category": "Commodities", "expiration_value": "4100.5", "floor_strike": 4000},
        # closed early (outcome known): the scheduled close is unknown -> shows the EET while active
        {"ticker": "KXGOLD-E1-B", "event_ticker": "KXGOLD-E1", "series_ticker": "KXGOLD", "open_ts": T0,
         "close_ts": T0 + 3 * H, "eet_ts": T0 + 10 * H + 300, "settle_ts": T0 + 3 * H + 60, "result": "no",
         "can_close_early": True, "category": "Commodities"},
        # far future close (outside a 1-day universe)
        {"ticker": "KXRAIN-E2-A", "event_ticker": "KXRAIN-E2", "series_ticker": "KXRAIN", "open_ts": T0,
         "close_ts": T0 + 5 * 86400, "settle_ts": T0 + 5 * 86400 + 60, "result": "no", "category": "Weather",
         "fee_type": "quadratic", "fee_multiplier": 0.5},
    ]
    candles = {
        "KXGOLD-E1-A": [(T0 + H, 0.50, 0.52), (T0 + 2 * H, 0.97, 0.98), (T0 + 5 * H, 0.0, 0.40)],
        "KXGOLD-E1-B": [(T0 + H, 0.10, 1.0)],
        "KXRAIN-E2-A": [(T0 + 2 * H, 0.20, 0.25)],
    }
    return ReplayDataset.from_records(markets, candles, **kw)


def view(ds: ReplayDataset, ts: int, book_size: int = 100) -> tuple[ReplayMarketData, ManualClock]:
    clock = ManualClock(dt(ts))
    return ReplayMarketData(ds, clock, book_size=book_size), clock


def test_quotes_use_only_candles_ended_by_now() -> None:
    ds = small_dataset()
    md, clock = view(ds, T0 + H - 1)
    spec = UniverseSpec(max_days_to_close=1)
    assert md.snapshot(spec) == {}  # opened, but no candle has ended yet -> not in the decision set
    clock.set(dt(T0 + H))  # the first candle ends exactly now: visible
    snap = md.snapshot(spec)
    assert set(snap) == {"KXGOLD-E1-A", "KXGOLD-E1-B"}
    a = snap["KXGOLD-E1-A"]
    assert (a.yes_bid, a.yes_ask, a.no_bid, a.no_ask) == (D("0.5"), D("0.52"), D("0.48"), D("0.5"))
    assert snap["KXGOLD-E1-B"].yes_ask is None and snap["KXGOLD-E1-B"].yes_bid == D("0.1")  # ask 1 = no ask
    clock.set(dt(T0 + 2 * H - 1))  # the 2h candle has not ended: still the 1h quote (forward-filled)
    assert md.snapshot(spec)["KXGOLD-E1-A"].yes_bid == D("0.5")
    clock.set(dt(T0 + 4 * H))
    a = md.snapshot(spec)["KXGOLD-E1-A"]
    assert (a.yes_bid, a.yes_ask) == (D("0.97"), D("0.98"))
    clock.set(dt(T0 + 5 * H))
    a = md.snapshot(spec)["KXGOLD-E1-A"]
    assert a.yes_bid is None and a.yes_ask == D("0.4")  # bid 0 = no bid


def test_outcome_is_hidden_until_settlement() -> None:
    ds = small_dataset()
    md, clock = view(ds, T0 + 2 * H)
    i = ds.index["KXGOLD-E1-A"]
    m = md.market_at(i)
    assert m.status == "active" and m.result == "" and m.settlement_value is None and m.expiration_value == ""
    assert m.settlement_ts is None and m.floor_strike == D(4000)
    assert md.market_at(i, T0 + 10 * H).status == "closed" and md.market_at(i, T0 + 10 * H).result == ""
    f = md.market_at(i, T0 + 10 * H + 600)
    assert f.status == "finalized" and f.result == "yes" and f.settlement_value == D(1)
    assert f.expiration_value == "4100.5" and f.close_time == dt(T0 + 10 * H)


def test_close_time_shown_while_active() -> None:
    ds = small_dataset()
    md, _ = view(ds, T0 + 2 * H)
    snap = md.snapshot(UniverseSpec(max_days_to_close=1))
    assert snap["KXGOLD-E1-A"].close_time == dt(T0 + 10 * H)  # scheduled == actual (EET 5 min later)
    assert snap["KXGOLD-E1-B"].close_time == dt(T0 + 10 * H + 300)  # early close not revealed: shows the EET
    closed = md.market_at(ds.index["KXGOLD-E1-B"], T0 + 3 * H)
    assert closed.status == "closed" and closed.close_time == dt(T0 + 3 * H)
    md2, _ = view(ds, T0 + 3 * H)
    assert "KXGOLD-E1-B" not in md2.snapshot(UniverseSpec(max_days_to_close=1))


def test_universe_spec_windows() -> None:
    ds = small_dataset()
    md, clock = view(ds, T0 + 2 * H)
    assert set(md.snapshot(UniverseSpec(max_days_to_close=1))) == {"KXGOLD-E1-A", "KXGOLD-E1-B"}
    assert set(md.snapshot(UniverseSpec(max_days_to_close=6))) == {"KXGOLD-E1-A", "KXGOLD-E1-B", "KXRAIN-E2-A"}
    assert set(md.snapshot(UniverseSpec(series_tickers=["KXRAIN"]))) == {"KXRAIN-E2-A"}
    assert md.snapshot(UniverseSpec()) == {}
    clock.set(dt(T0 + 4 * 86400 + 1))  # the rain market now closes within a day
    assert set(md.snapshot(UniverseSpec(max_days_to_close=1))) == {"KXRAIN-E2-A"}


def test_synthetic_book_and_provider_api() -> None:
    ds = small_dataset()
    md, clock = view(ds, T0 + 2 * H, book_size=40)

    async def go() -> None:
        b = await md.orderbook("KXGOLD-E1-A")
        assert b.best_yes_bid == D("0.97") and b.best_yes_ask == D("0.98")
        assert b.yes_bids[0].size == D(40) and b.no_bids[0].size == D(40) and b.ts == clock.now
        one = await md.orderbook("KXGOLD-E1-B")
        assert one.best_yes_bid == D("0.1") and one.best_yes_ask is None
        both = await md.orderbooks(["KXGOLD-E1-A", "nope", "KXRAIN-E2-A"])
        assert set(both) == {"KXGOLD-E1-A", "KXRAIN-E2-A"}
        with pytest.raises(KalshiNotFound):
            await md.market("nope")
        s = await md.series("KXRAIN")
        assert s.category == "Weather" and s.fee_multiplier == D("0.5")
        assert md.fee_params(await md.market("KXRAIN-E2-A")) == ("quadratic", D("0.5"))
        assert await md.trades_since("KXGOLD-E1-A", clock.now) == []
        clock.set(dt(T0 + 10 * H))
        assert (await md.orderbook("KXGOLD-E1-A")).is_empty  # closed: no book
        assert md.calls == []  # a replay does not record provider calls

    asyncio.run(go())


def test_snapshot_reuses_market_objects_until_the_quote_changes() -> None:
    ds = small_dataset()
    md, clock = view(ds, T0 + 2 * H)
    spec = UniverseSpec(max_days_to_close=1)
    a1 = md.snapshot(spec)["KXGOLD-E1-A"]
    clock.set(dt(T0 + 3 * H))
    assert md.snapshot(spec)["KXGOLD-E1-A"] is a1
    clock.set(dt(T0 + 5 * H))
    assert md.snapshot(spec)["KXGOLD-E1-A"] is not a1
    clock.set(dt(T0 + 3 * 86400))
    md.forget()
    assert not md._quoted and not md._base


def test_duplicate_candles_first_source_wins() -> None:
    markets = [{"ticker": "X-1", "event_ticker": "X", "series_ticker": "X", "open_ts": T0, "close_ts": T0 + 5 * H,
                "settle_ts": T0 + 5 * H, "result": "no"}]
    ds = ReplayDataset.from_records(markets, {"X-1": [(T0 + H, 0.3, 0.4), (T0 + H, 0.6, 0.7)]})
    md, _ = view(ds, T0 + H)
    assert md.snapshot(UniverseSpec(max_days_to_close=1))["X-1"].yes_bid == D("0.3")


# --------------------------------------------------------------------------- minute loader


def write_csv_gz(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def minute_research_dir(tmp_path: Path) -> Path:
    root = tmp_path / "research"
    cf = root / "crypto_fv"
    base = T0 + 10 * H

    def mrow(k: int, result: str) -> dict[str, Any]:
        close = base + 900 * (k + 1)
        return {"series": "KXBTC15M", "event_ticker": f"KXBTC15M-W{k}", "ticker": f"KXBTC15M-W{k}-00",
                "strike_type": "greater_or_equal", "floor_strike": "60000", "cap_strike": "",
                "open_ts": close - 900, "close_ts": close, "result": result, "expiration_value": "60,010.5",
                "settlement_value": "1.0000" if result == "yes" else "0.0000", "pls": "tapered_deci_cent",
                "settlement_ts": datetime.fromtimestamp(close + 6, tz=UTC).isoformat().replace("+00:00", "Z")}

    write_csv_gz(cf / "data" / "markets_KXBTC15M.csv.gz", [mrow(1, "yes"), mrow(2, "no")])
    write_csv_gz(cf / "data" / "candles_KXBTC15M.csv.gz",
                 [{"ticker": "KXBTC15M-W1-00", "ts": base + 900 + 60, "yes_bid": "0.45", "yes_ask": "0.46"},
                  {"ticker": "KXBTC15M-W2-00", "ts": base + 1800 + 60, "yes_bid": "0.55", "yes_ask": "0.56"}])
    # a holdout fetch: W1 again (ignored, the first source wins) and an older window W0
    write_csv_gz(cf / "verify_stats" / "out" / "hist_markets_KXBTC15M.csv.gz", [mrow(1, "no"), mrow(0, "no")])
    write_csv_gz(cf / "verify_stats" / "out" / "hist_candles_KXBTC15M.csv.gz",
                 [{"ticker": "KXBTC15M-W1-00", "ts": base + 900 + 60, "yes_bid": "0.10", "yes_ask": "0.11"},
                  {"ticker": "KXBTC15M-W0-00", "ts": base + 60, "yes_bid": "0.30", "yes_ask": "0.31"}])
    (cf / "data" / "spot_BTC-USD.csv").write_text(
        "ts,low,high,open,close,volume\n" + "".join(
            f"{base + 60 * k},{59990 + k},{60010 + k},{60000 + k},{60001 + k},1.5\n" for k in range(0, 50)))
    (cf / "cache").mkdir(parents=True, exist_ok=True)
    (cf / "cache" / "series_crypto.json").write_text(json.dumps({"series": [
        {"ticker": "KXBTC15M", "category": "Crypto", "fee_type": "quadratic", "fee_multiplier": 1,
         "title": "Bitcoin price up down", "frequency": "fifteen_min"}]}))
    return root


def test_load_minute_merges_sources_and_wires_feeds(tmp_path: Path) -> None:
    root = minute_research_dir(tmp_path)
    assert minute_series_available(["KXBTC15M"], root) and not minute_series_available(["KXETH15M"], root)
    ds = load_minute(["KXBTC15M"], root)
    assert ds.kind == "minute" and ds.step_s == 60 and ds.default_fill == "next_ask"
    assert sorted(ds.tickers) == ["KXBTC15M-W0-00", "KXBTC15M-W1-00", "KXBTC15M-W2-00"]
    i = ds.index["KXBTC15M-W1-00"]
    assert ds.result[i] == "yes"  # in-sample row wins over the holdout copy
    base = T0 + 10 * H
    md, clock = view(ds, base + 900 + 60)
    snap = md.snapshot(UniverseSpec(series_tickers=["KXBTC15M"]))
    assert list(snap) == ["KXBTC15M-W1-00"] and snap["KXBTC15M-W1-00"].yes_bid == D("0.45")
    m = snap["KXBTC15M-W1-00"]
    assert m.floor_strike == D(60000) and m.strike_type == "greater_or_equal" and m.is_valid_price(D("0.905"))
    assert ds.series_info["KXBTC15M"].category == "Crypto"
    feeds = ds.make_feeds(clock)  # type: ignore[misc]

    async def go() -> None:
        q = await feeds["crypto"].spot("BTC")
        # the bar that ENDED at now (started 60 s earlier) - never a later one
        assert q.ts == clock.now and q.price == 60001 + 15 and q.source == "coinbase"
        bars = await feeds["crypto"].candles("BTC", 5)
        assert bars[-1].end == clock.now and len(bars) == 5
        settled = await feeds["kalshi_settled"].settled_markets("KXBTC15M")
        assert [s.ticker for s in settled] == ["KXBTC15M-W0-00"]  # W1/W2 not settled yet
        assert settled[0].expiration_value == "60,010.5"
        clock.set(dt(base + 1800 + 6))
        assert {s.ticker for s in await feeds["kalshi_settled"].settled_markets("KXBTC15M")} == {
            "KXBTC15M-W0-00", "KXBTC15M-W1-00"}

    asyncio.run(go())


def test_load_minute_missing_series(tmp_path: Path) -> None:
    root = minute_research_dir(tmp_path)
    with pytest.raises(BacktestDataError):
        load_minute(["KXETH15M"], root)


# --------------------------------------------------------------------------- hourly loader


def test_load_hourly_research_universe(tmp_path: Path) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    from kalshibot.backtest.data import load_hourly

    root = tmp_path / "research"
    (root / "data").mkdir(parents=True)
    (root / "calibration").mkdir(parents=True)
    ts = pa.timestamp("us", tz="UTC")

    def us(x: int | None) -> int | None:
        return None if x is None else x * 10**6

    rows = [
        # ok: fully covered event E1 (both markets have candles)
        ("KXWTI-E1-A", "KXWTI-E1", "KXWTI", T0 + 30 * H, T0 + 30 * H + 300, "yes", "Commodities"),
        ("KXWTI-E1-B", "KXWTI-E1", "KXWTI", T0 + 30 * H, T0 + 30 * H + 300, "no", "Commodities"),
        # event E2: one market lacks candles -> the whole event is out of the research universe
        ("KXWTI-E2-A", "KXWTI-E2", "KXWTI", T0 + 30 * H, T0 + 30 * H + 300, "yes", "Commodities"),
        ("KXWTI-E2-B", "KXWTI-E2", "KXWTI", T0 + 30 * H, T0 + 30 * H + 300, "no", "Commodities"),
        # outcome-timing-dependent series -> out
        ("KXSAY-E3-A", "KXSAY-E3", "KXSAY", T0 + 30 * H, T0 + 30 * H + 300, "yes", "Mentions"),
        # EET after the end of the data -> out
        ("KXWTI-E4-A", "KXWTI-E4", "KXWTI", T0 + 30 * H, T0 + 90 * H, "no", "Commodities"),
    ]
    n = len(rows)
    table = pa.table({
        "ticker": [r[0] for r in rows], "event_ticker": [r[1] for r in rows], "series_ticker": [r[2] for r in rows],
        "title": [f"t {r[0]}" for r in rows], "strike_type": ["greater"] * n, "floor_strike": [70.0] * n,
        "cap_strike": pa.array([None] * n, type=pa.float64()),
        "open_time": pa.array([us(T0)] * n, type=ts), "close_time": pa.array([us(r[3]) for r in rows], type=ts),
        "expected_expiration_time": pa.array([us(r[4]) for r in rows], type=ts),
        "can_close_early": [True] * n,
        "settlement_ts": pa.array([us(r[3] + 600) for r in rows], type=ts),
        "result": [r[5] for r in rows], "settlement_value": [1.0 if r[5] == "yes" else 0.0 for r in rows],
        "expiration_value": ["71.2"] * n, "price_level_structure": ["linear_cent"] * n,
        "price_ranges": [json.dumps([{"start": "0.0000", "end": "1.0000", "step": "0.0100"}])] * n,
        "category": [r[6] for r in rows], "fee_type": ["quadratic"] * n, "fee_multiplier": [1.0] * n,
        "series_title": [r[2] for r in rows], "frequency": ["daily"] * n,
    })
    pq.write_table(table, root / "data" / "markets.parquet")

    def candles(recs: list[tuple[str, int, float, float]]) -> Any:
        return pa.table({"ticker": [r[0] for r in recs], "period": [60] * len(recs),
                         "end_period_ts": pa.array([r[1] for r in recs], type=pa.int64()),
                         "yes_bid_close": pa.array([r[2] for r in recs], type=pa.float32()),
                         "yes_ask_close": pa.array([r[3] for r in recs], type=pa.float32())})

    pq.write_table(candles([("KXWTI-E1-A", T0 + H, 0.97, 0.98), ("KXWTI-E2-A", T0 + H, 0.5, 0.6),
                            ("KXSAY-E3-A", T0 + H, 0.5, 0.6), ("KXWTI-E4-A", T0 + H, 0.5, 0.6)]),
                   root / "data" / "candles_hourly.parquet")
    pq.write_table(candles([("KXWTI-E1-B", T0 + H, 0.01, 0.02), ("KXWTI-E1-A", T0 + H, 0.10, 0.20),
                            ("KXWTI-E1-A", T0 + 2 * H, 0.98, 0.99)]),
                   root / "calibration" / "candles_fill_hourly.parquet")
    (root / "calibration" / "series_flags.csv").write_text("series_ticker,category,outcome_dep\nKXSAY,Mentions,True\n"
                                                           "KXWTI,Commodities,False\n")
    # the data ends at the midnight after the last close; E4's EET is after it
    ds = load_hourly(root)
    assert sorted(ds.tickers) == ["KXWTI-E1-A", "KXWTI-E1-B"]
    assert ds.series_info["KXWTI"].category == "Commodities" and ds.info["outcome_dep_series"] == 1
    md, clock = view(ds, T0 + H)
    snap = md.snapshot(UniverseSpec(max_days_to_close=3))
    assert snap["KXWTI-E1-A"].yes_bid == D("0.97")  # the hourly file wins over the fill file on duplicates
    clock.set(dt(T0 + 2 * H))
    assert md.snapshot(UniverseSpec(max_days_to_close=3))["KXWTI-E1-A"].yes_bid == D("0.98")
    everything = load_hourly(root, universe="all")
    assert len(everything.tickers) == 5  # E2-B has no candles
    only = load_hourly(root, universe="all", categories=["Mentions"])
    assert only.tickers == ["KXSAY-E3-A"]
    with pytest.raises(BacktestDataError):
        load_hourly(tmp_path / "nowhere")
