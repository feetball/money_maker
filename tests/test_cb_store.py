"""SpotStore (contract §7): separate SQLite file, own ProcessLock, exact round trips, tables."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from kalshibot.coinbase.paper import SpotFill, SpotOrder, SpotPosition
from kalshibot.coinbase.store import SCHEMA_VERSION, SchemaVersionError, SpotStore, StoreLockedError
from kalshibot.store import Store

T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def D(x: object) -> Decimal:
    return Decimal(str(x))


def order(i: int = 1, **kw: object) -> SpotOrder:
    base = dict(id=i, product_id="BTC-USD", side="buy", order_type="limit", tif="gtc", post_only=True,
                base_size=D("0.01234567"), limit_price=D("84475.95"), status="open", strategy="s",
                reason="why", target_weight=0.5, expected_edge_bps=12.5, created_at=T0, updated_at=T0,
                expires_at=T0 + timedelta(hours=1), queue_ahead=D("0.5"), reserved=D("1049.18"),
                fee_tier="intro", maker_rate=D("0.005"), taker_rate=D("0.009"))
    base.update(kw)
    return SpotOrder(**base)  # type: ignore[arg-type]


def test_tables_schema_version_and_separate_file(tmp_path: Path) -> None:
    path = tmp_path / "coinbase.sqlite3"
    s = SpotStore(path)
    names = {r["name"] for r in s._all("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names >= {"account", "orders", "fills", "positions", "equity_snapshots", "signals", "logs",
                     "strategy_state", "risk_limits", "backtests", "schema_version", "kv"}
    assert s.schema_version == SCHEMA_VERSION
    assert s._one("PRAGMA journal_mode")[0] == "wal"
    s.close()
    SpotStore(path).close()  # reopening is idempotent
    con = sqlite3.connect(path)
    con.execute("UPDATE schema_version SET version = 99")
    con.commit()
    con.close()
    with pytest.raises(SchemaVersionError):
        SpotStore(path)
    # never the Kalshi schema: a Kalshi Store on the same directory is a different file
    k = Store(tmp_path / "kalshibot.sqlite3")
    assert {r["name"] for r in k._all("SELECT name FROM sqlite_master WHERE type='table'")} >= {"settlements"}
    k.close()


def test_process_lock_is_per_file(tmp_path: Path) -> None:
    a = SpotStore(tmp_path / "coinbase.sqlite3", exclusive=True)
    assert Path(str(tmp_path / "coinbase.sqlite3") + ".lock").exists()
    with pytest.raises(StoreLockedError):
        SpotStore(tmp_path / "coinbase.sqlite3", exclusive=True)
    other = Store(tmp_path / "kalshibot.sqlite3", exclusive=True)  # the Kalshi db is unaffected
    other.close()
    a.close()
    SpotStore(tmp_path / "coinbase.sqlite3", exclusive=True).close()  # released on close


def test_order_fill_position_round_trip_exactly() -> None:
    s = SpotStore(":memory:")
    o = order(filled_base=D("0.00000001"), filled_quote=D("0.0008447595"), fees=D("0.01"),
              taker_notional=D("1.5"), maker_notional=D("0.0008447595"), taker_fees=D("0.02"), maker_fees=D("0.01"),
              realized_pnl=D("-0.123456789012"), status="partially_filled")
    s.upsert_order(o)
    assert s.get_order(1) == o
    o.status = "cancelled"
    o.status_reason = "bye"
    s.upsert_order(o)
    assert s.get_order(1) == o and s.open_orders() == []
    f = SpotFill(id=1, order_id=1, product_id="BTC-USD", side="sell", base_size=D("0.01234567"),
                 price=D("84475.95"), notional=D("1042.906218"), fee=D("9.39"), fee_rate=D("0.009"), is_taker=True,
                 ts=T0, strategy="s", realized_pnl=D("-3.5"), trade_id=123)
    s.insert_fill(f)
    assert s.list_fills() == [f]
    p = SpotPosition(product_id="BTC-USD", strategy="s", base_currency="BTC", quantity=D("0.01234567"),
                     cost_basis=D("1052.296218"), realized_pnl=D("1.2"), fees_paid=D("9.39"), opened_at=T0,
                     updated_at=T0)
    s.upsert_position(p)
    assert s.get_position("s", "BTC-USD") == p
    assert s.list_positions() == [p]
    p.quantity = D(0)
    s.upsert_position(p)
    assert s.list_positions() == [] and s.list_positions(open_only=False) == [p]


def test_list_orders_filters_and_trade_counts() -> None:
    s = SpotStore(":memory:")
    s.upsert_order(order(1))
    s.upsert_order(order(2, side="sell", status="filled", filled_base=D(1), realized_pnl=D(5), strategy="a"))
    s.upsert_order(order(3, side="sell", status="cancelled", filled_base=D("0.5"), realized_pnl=D(-1)))
    s.upsert_order(order(4, side="sell", status="cancelled", filled_base=D(0)))  # no fills: not a trade
    s.upsert_order(order(5, status="rejected"))
    assert [o.id for o in s.list_orders()] == [5, 4, 3, 2, 1]
    assert [o.id for o in s.list_orders("open")] == [1]
    assert [o.id for o in s.list_orders("cancelled", limit=1)] == [4]
    assert [o.id for o in s.list_orders(strategy="a")] == [2]
    assert s.trade_counts() == (2, 1)
    summ = s.strategy_summary()
    assert summ["a"]["trades"] == 1 and summ["a"]["wins"] == 1 and summ["s"]["orders"] == 3


def test_account_signals_logs_prune() -> None:
    s = SpotStore(":memory:")
    assert s.get_account() is None
    s.save_account(starting_balance=D(1000), cash=D("850.03"), fees_paid=D("1.49"), peak_equity=D(1000), ts=T0)
    acct = s.get_account()
    assert acct is not None and acct["cash"] == D("850.03") and acct["peak_equity"] == D(1000)
    sid = s.insert_signal(ts=T0, strategy="trend", product_id="BTC-USD", side="buy", target_weight=0.4,
                          quote_size=D("123.45"), base_size=None, limit_price=None, expected_edge_bps=30.0,
                          reason="above 200d MA", decision="executed", decision_reason="ok", order_id=7,
                          bar_end="2026-09-27T00:00:00Z")
    (sig,) = s.list_signals()
    assert sig["id"] == sid and sig["quote_size"] == D("123.45") and sig["target_weight"] == 0.4
    assert sig["data"] == {"bar_end": "2026-09-27T00:00:00Z"} and sig["ts"] == T0
    for i in range(5):
        s.insert_log("info", "test", f"m{i}", {"i": i})
    assert [r["message"] for r in s.list_logs(limit=2)] == ["m4", "m3"]
    assert s.prune("logs", 2) == 3 and s.count("logs") == 2
    with pytest.raises(ValueError):
        s.prune("orders", 1)


def test_equity_snapshots_thin_drawdown_downsample() -> None:
    s = SpotStore(":memory:")
    for i, e in enumerate([1000, 1010, 990, 1005, 980, 1020]):
        s.insert_equity_snapshot(ts=T0 + timedelta(minutes=i), equity=D(e), equity_mid=D(e) + 1, cash=D(500),
                                 positions_value=D(e) - 500, realized_pnl=D(0), unrealized_pnl=D(e) - 1000)
    rows = s.list_equity()
    assert [r["equity"] for r in rows] == [D(x) for x in (1000, 1010, 990, 1005, 980, 1020)]
    assert rows[0]["positions_mid_value"] == 0 and rows[1]["equity_mid"] == D(1011)
    assert len(s.list_equity(max_points=3)) <= 4 and s.list_equity(max_points=3)[-1]["equity"] == D(1020)
    assert len(s.list_equity(since=T0 + timedelta(minutes=4))) == 2
    dd, pct = s.equity_drawdown()
    assert dd == 30.0 and abs(pct - 30 / 1010 * 100) < 1e-9  # peak 1010 -> trough 980
    deleted = s.downsample_equity(T0 + timedelta(hours=2))
    # one hour bucket of 6 rows keeps first (1000), lowest (980) and highest = last (1020): 3 deleted
    assert deleted == 3
    assert [r["equity"] for r in s.list_equity()] == [D(1000), D(980), D(1020)]


def test_strategy_state_risk_limits_backtests_kv_and_reset() -> None:
    s = SpotStore(":memory:")
    s.save_strategy_state("trend", enabled=True, params={"n": 200})
    s.save_strategy_state("trend", state={"x": 1})
    st = s.get_strategy_state("trend")
    assert st is not None and st["enabled"] is True and st["params"] == {"n": 200} and st["state"] == {"x": 1}
    assert set(s.list_strategy_states()) == {"trend"}
    s.save_risk_limits({"max_spread_bps": 30, "min_trade_usd": 5.0})
    assert s.get_risk_limits() == {"max_spread_bps": 30, "min_trade_usd": 5.0}
    s.save_risk_limits({"max_spread_bps": 40}, replace=True)
    assert s.get_risk_limits() == {"max_spread_bps": 40}
    bt = s.create_backtest("trend", {"n": 200}, start="2024-01-01", end="2026-01-01", starting_balance=D(1000),
                           fee_tier="intro")
    s.update_backtest(bt, status="done", metrics={"sharpe": 1.1}, equity_curve=[{"ts": "a", "equity": 1}],
                      benchmarks={"btc": [{"ts": "a", "equity": 1}], "equal_weight": []}, by_year=[{"year": 2025}],
                      finished_at=datetime(2026, 9, 27, tzinfo=UTC))
    full = s.get_backtest(bt)
    assert full is not None and full["benchmarks"]["btc"][0]["equity"] == 1 and full["by_year"] == [{"year": 2025}]
    assert full["fee_tier"] == "intro" and full["metrics"] == {"sharpe": 1.1}
    (row,) = s.list_backtests()
    assert "equity_curve" not in row and row["status"] == "done"
    with pytest.raises(ValueError):
        s.update_backtest(bt, bogus=1)
    s.set_kv("broker.cursors", {"BTC-USD": 5})
    s.set_kv("risk.kill_switch", {"on": True})
    s.save_account(starting_balance=D(1000), cash=D(1))
    s.upsert_order(order(1))
    s.insert_log("info", "x", "kept")
    s.reset_paper_state()
    assert s.get_account() is None and s.list_orders() == [] and s.get_kv("broker.cursors") is None
    assert s.get_kv("risk.kill_switch") == {"on": True}  # the kill switch survives an account reset
    assert s.count("logs") == 1 and s.get_risk_limits() and s.list_backtests()


def test_reader_and_threads(tmp_path: Path) -> None:
    s = SpotStore(tmp_path / "cb.sqlite3")
    errors: list[BaseException] = []

    def work(k: int) -> None:
        try:
            for i in range(20):
                s.insert_log("info", "t", f"{k}-{i}")
                with s.transaction():
                    s.set_kv(f"k{k}", i)
        except BaseException as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=work, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and s.count("logs") == 80
    with s.reader() as r:
        assert r.count("logs") == 80
        with pytest.raises(sqlite3.OperationalError):
            r.insert_log("info", "t", "read-only")
    s.close()
