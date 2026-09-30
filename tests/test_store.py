"""SQLite store: schema/migrations, exact round-trips, transactions, thread safety, CRUD helpers."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kalshibot.money import D
from kalshibot.paper.models import Fill, Order, Position, Settlement
from kalshibot.store import SCHEMA_VERSION, SchemaVersionError, Store

T0 = datetime(2026, 9, 26, 12, 0, 0, 123456, tzinfo=UTC)


def sample_order(i: int = 1, **kw) -> Order:
    base = dict(id=i, ticker="KX-A", side="no", action="sell", count=7, limit_price=D("0.0550"), tif="gtc",
                status="partially_filled", filled_count=2, avg_fill_price=D("0.055"), strategy="s",
                reason="why", expected_edge=D("0.0123"), fair_value=0.61, group_id="g1",
                queue_ahead=D("12.34"), created_at=T0, updated_at=T0 + timedelta(seconds=1),
                expires_at=T0 + timedelta(hours=1), fees=D("0.010000"), event_ticker="KX",
                status_reason="", reserved=D("0.2751"), filled_notional=D("0.110"), fill_credit=D("0.25"),
                fee_state={"precision": "0.01", "accumulator": "0.003508", "total_fee": "0.01"},
                taker_filled_count=1)
    base.update(kw)
    return Order(**base)


def test_schema_version_wal_and_reopen(tmp_path):
    p = tmp_path / "sub" / "db.sqlite3"
    s = Store(p)  # creates the parent directory
    assert s.schema_version == SCHEMA_VERSION
    assert s._one("PRAGMA journal_mode")[0] == "wal"
    tables = {r["name"] for r in s._all("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"orders", "fills", "positions", "settlements", "equity_snapshots", "signals", "logs",
            "strategy_state", "risk_limits", "backtests", "account", "kv", "meta"} <= tables
    s.set_kv("x", {"a": 1})
    s.close()
    s2 = Store(p)  # idempotent migration
    assert s2.get_kv("x") == {"a": 1} and s2.schema_version == SCHEMA_VERSION
    s2._exec("UPDATE meta SET value='999' WHERE key='schema_version'")
    s2.close()
    with pytest.raises(SchemaVersionError):
        Store(p)


def test_migrates_old_database(tmp_path):
    p = tmp_path / "old.sqlite3"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO meta VALUES('schema_version','0')")
    con.commit()
    con.close()
    s = Store(p)
    assert s.schema_version == SCHEMA_VERSION and s.list_orders() == []


def test_ledger_round_trips_are_exact():
    s = Store(":memory:")
    o = sample_order()
    s.upsert_order(o)
    assert s.get_order(1) == o
    o.status, o.filled_count = "filled", 7
    s.upsert_order(o)
    assert s.get_order(1).status == "filled" and s.open_orders() == []
    f = Fill(id=1, order_id=1, ticker="KX-A", side="no", action="sell", count=2, price=D("0.0550"),
             fee=D("0.000174"), is_taker=False, ts=T0, strategy="s", event_ticker="KX")
    s.insert_fill(f)
    assert s.list_fills() == [f]
    p = Position(ticker="KX-A", strategy="s", event_ticker="KX", side="yes", count=3, cost_basis=D("1.2345"),
                 realized_pnl=D("-0.000001"), fees_paid=D("0.03"), opened_at=T0, expected_edge_total=D("0.09"),
                 open_fees=D("0.02"), fv_sum=1.5, fv_weight=3.0, updated_at=T0)
    s.upsert_position(p)
    assert s.get_position("s", "KX-A") == p and s.list_positions() == [p]
    p.count = 0
    s.upsert_position(p)
    assert s.list_positions() == [] and s.list_positions(open_only=False) == [p]
    st = Settlement(id=1, ticker="KX-A", result="scalar", side="yes", count=3, payout=D("0.99"),
                    cost_basis=D("1.2345"), pnl=D("-0.2645"), ts=T0, strategy="s", event_ticker="KX",
                    kind="settlement", fees=D("0.02"), expected_edge=D("0.09"), fair_value=0.5,
                    settlement_value=D("0.3333"), opened_at=T0)
    s.insert_settlement(st)
    assert s.list_settlements() == [st]
    assert s.settlement_counts() == (1, 0)
    assert s.max_id("orders") == 1 and s.max_id("fills") == 1


def test_list_orders_filters():
    s = Store(":memory:")
    for i, (st, strat) in enumerate([("open", "a"), ("filled", "a"), ("rejected", "b"), ("partially_filled", "b")], 1):
        s.upsert_order(sample_order(i, status=st, strategy=strat))
    assert [o.id for o in s.list_orders("open")] == [4, 1]
    assert [o.id for o in s.list_orders("all", limit=2)] == [4, 3]
    assert [o.id for o in s.list_orders("filled")] == [2]
    assert [o.id for o in s.list_orders(strategy="b")] == [4, 3]
    summary = s.strategy_summary()
    assert summary["a"]["orders"] == 2 and summary["b"]["orders"] == 1  # rejected not counted


def test_transaction_rolls_back():
    s = Store(":memory:")
    with pytest.raises(RuntimeError), s.transaction():
        s.upsert_order(sample_order(1))
        with s.transaction():  # nested joins the outer one
            s.set_kv("k", 1)
        raise RuntimeError("boom")
    assert s.get_order(1) is None and s.get_kv("k") is None
    with s.transaction():
        s.upsert_order(sample_order(2))
    assert s.get_order(2) is not None


def test_thread_safety(tmp_path):
    s = Store(tmp_path / "t.sqlite3")
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(50):
                with s.transaction():
                    s.insert_log("info", "t", f"w{n}-{i}", {"i": i})
                    s.set_kv(f"w{n}", i)
                s.list_logs(limit=5)
        except BaseException as e:  # pragma: no cover - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert s.count("logs") == 400
    assert all(s.get_kv(f"w{n}") == 49 for n in range(8))


def test_account_signals_logs_state_limits_backtests_kv():
    s = Store(":memory:")
    assert s.get_account() is None
    s.save_account(starting_balance=D(1000), cash=D("990.5"), realized_pnl=D("-1.25"), fees_paid=D("0.2"),
                   peak_equity=D("1001"), max_drawdown_pct=D("0.5"), ts=T0)
    s.save_account(starting_balance=D(1000), cash=D("991"), ts=T0 + timedelta(seconds=5))
    a = s.get_account()
    assert a["cash"] == D("991") and a["created_at"] == T0 and a["updated_at"] == T0 + timedelta(seconds=5)

    sid = s.insert_signal(ts=T0, strategy="s", ticker="KX-A", title="t", side="yes", count=3,
                          limit_price=D("0.41"), fair_value=0.5, expected_edge=D("0.02"), reason="r",
                          decision="rejected", decision_reason="max_spread", extra_field=1)
    [sig] = s.list_signals()
    assert sig["id"] == sid and sig["limit_price"] == D("0.41") and sig["data"] == {"extra_field": 1}
    assert s.list_signals(decision="executed") == []

    s.insert_log("warning", "risk", "hello", {"x": Decimal("1.5")}, ts=T0)
    assert s.list_logs(kind="risk")[0]["data"] == {"x": "1.5"}

    s.save_strategy_state("s", enabled=True, params={"a": 1})
    s.save_strategy_state("s", state={"cursor": 5})  # keeps enabled/params
    st = s.get_strategy_state("s")
    assert (st["enabled"], st["params"], st["state"]) == (True, {"a": 1}, {"cursor": 5})
    assert set(s.list_strategy_states()) == {"s"}

    s.save_risk_limits({"max_spread": 0.05, "max_orders_per_minute": 10})
    s.save_risk_limits({"max_spread": 0.07})
    assert s.get_risk_limits() == {"max_spread": 0.07, "max_orders_per_minute": 10}
    s.save_risk_limits({"daily_loss_limit": 5}, replace=True)
    assert s.get_risk_limits() == {"daily_loss_limit": 5}

    bid = s.create_backtest("strat", {"p": 1}, start="2026-01-01", end="2026-02-01", starting_balance=D(100))
    s.update_backtest(bid, status="done", metrics={"total_pnl": 1.5}, equity_curve=[{"ts": "x", "equity": 1}],
                      finished_at=T0, end="2026-03-01")
    bt = s.get_backtest(bid)
    assert (bt["status"], bt["metrics"], bt["end"], bt["finished_at"]) == ("done", {"total_pnl": 1.5},
                                                                          "2026-03-01", T0)
    assert "equity_curve" not in s.list_backtests()[0]
    with pytest.raises(ValueError):
        s.update_backtest(bid, bogus=1)

    s.set_kv("broker.x", [1, 2])
    s.set_kv("other", "keep")
    s.delete_kv("missing")
    s.reset_paper_state()
    assert s.get_account() is None and s.get_kv("broker.x") is None and s.get_kv("other") == "keep"
    assert s.list_signals() == [] and s.list_logs() and s.get_backtest(bid) is not None


def test_equity_series_thinning_and_prune():
    s = Store(":memory:")
    for i in range(100):
        s.insert_equity_snapshot(ts=T0 + timedelta(minutes=i), equity=D(1000 + i), equity_mid=D(1000 + i),
                                 cash=D(1000))
    rows = s.list_equity(since=T0 + timedelta(minutes=50))
    assert len(rows) == 50 and rows[0]["equity"] == D(1050)
    thin = s.list_equity(max_points=10)
    assert len(thin) <= 11 and thin[-1]["equity"] == D(1099) and thin[0]["equity"] == D(1000)
    assert s.prune("equity_snapshots", keep_last=30) == 70
    assert s.count("equity_snapshots") == 30
    with pytest.raises(ValueError):
        s.prune("orders", 1)
