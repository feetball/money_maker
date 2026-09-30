"""SQLite persistence for the Coinbase paper venue (docs/COINBASE_CONTRACT.md §7) - PAPER ONLY.

A **separate file** from the Kalshi database (default ``data/coinbase.sqlite3``, WAL) with
its own single-writer lock: :class:`kalshibot.store.ProcessLock` on ``<path>.lock`` (the
class is reused, not modified). Nothing here touches the Kalshi database.

Tables: ``account`` (one row: starting balance, free cash, realized P&L, fees, peak /
drawdown), ``orders``, ``fills``, ``positions``, ``equity_snapshots``, ``signals``, ``logs``,
``strategy_state``, ``risk_limits``, ``backtests``, ``schema_version``, plus ``kv`` for the
broker's / risk manager's small working state (consumed liquidity, trade cursors, marks,
day-start equity, kill switch).

Conventions (as ``kalshibot/store.py``): money/prices/sizes are TEXT (``str(Decimal)``, exact
round trip), timestamps TEXT UTC ISO-8601 with microseconds and ``Z`` (sortable), JSON as TEXT.

Thread safety (as ``kalshibot/store.py``): one connection (``check_same_thread=False``)
guarded by a re-entrant ``threading.RLock``; every public method takes it and
:meth:`SpotStore.transaction` holds it for the whole ``BEGIN IMMEDIATE ... COMMIT``, so the
broker's multi-row updates are atomic. :meth:`SpotStore.reader` opens a separate read-only
connection for heavy scans from worker threads.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshibot.coinbase.paper import SpotFill, SpotOrder, SpotPosition, iso, parse_iso
from kalshibot.money import ZERO, D
from kalshibot.store import ProcessLock, SchemaVersionError, StoreLockedError

__all__ = ["DEFAULT_PATH", "MIGRATIONS", "PAPER_TABLES", "SCHEMA_VERSION", "SchemaVersionError", "SpotStore",
           "StoreLockedError"]

DEFAULT_PATH = "data/coinbase.sqlite3"
SCHEMA_VERSION = 1

MIGRATIONS: dict[int, list[str]] = {
    1: [
        """CREATE TABLE IF NOT EXISTS schema_version (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL,
            applied_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS account (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            starting_balance TEXT NOT NULL,
            cash TEXT NOT NULL,
            realized_pnl TEXT NOT NULL DEFAULT '0',
            fees_paid TEXT NOT NULL DEFAULT '0',
            peak_equity TEXT,
            max_drawdown_pct TEXT NOT NULL DEFAULT '0',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY,
            product_id TEXT NOT NULL,
            side TEXT NOT NULL,
            order_type TEXT NOT NULL,
            tif TEXT NOT NULL,
            post_only INTEGER NOT NULL DEFAULT 0,
            quote_size TEXT,
            base_size TEXT,
            limit_price TEXT,
            status TEXT NOT NULL,
            status_reason TEXT NOT NULL DEFAULT '',
            filled_base TEXT NOT NULL DEFAULT '0',
            filled_quote TEXT NOT NULL DEFAULT '0',
            fees TEXT NOT NULL DEFAULT '0',
            strategy TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            target_weight REAL,
            expected_edge_bps REAL,
            created_at TEXT,
            updated_at TEXT,
            expires_at TEXT,
            queue_ahead TEXT,
            reserved TEXT NOT NULL DEFAULT '0',
            realized_pnl TEXT NOT NULL DEFAULT '0',
            taker_notional TEXT NOT NULL DEFAULT '0',
            maker_notional TEXT NOT NULL DEFAULT '0',
            taker_fees TEXT NOT NULL DEFAULT '0',
            maker_fees TEXT NOT NULL DEFAULT '0',
            fee_tier TEXT NOT NULL DEFAULT '',
            maker_rate TEXT NOT NULL DEFAULT '0',
            taker_rate TEXT NOT NULL DEFAULT '0'
        )""",
        "CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status)",
        "CREATE INDEX IF NOT EXISTS ix_orders_strategy ON orders(strategy)",
        "CREATE INDEX IF NOT EXISTS ix_orders_product ON orders(product_id)",
        """CREATE TABLE IF NOT EXISTS fills (
            id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL,
            product_id TEXT NOT NULL,
            side TEXT NOT NULL,
            base_size TEXT NOT NULL,
            price TEXT NOT NULL,
            notional TEXT NOT NULL,
            fee TEXT NOT NULL,
            fee_rate TEXT NOT NULL,
            is_taker INTEGER NOT NULL,
            ts TEXT NOT NULL,
            strategy TEXT NOT NULL DEFAULT '',
            realized_pnl TEXT NOT NULL DEFAULT '0',
            trade_id INTEGER
        )""",
        "CREATE INDEX IF NOT EXISTS ix_fills_order ON fills(order_id)",
        "CREATE INDEX IF NOT EXISTS ix_fills_strategy ON fills(strategy)",
        "CREATE INDEX IF NOT EXISTS ix_fills_ts ON fills(ts)",
        """CREATE TABLE IF NOT EXISTS positions (
            strategy TEXT NOT NULL,
            product_id TEXT NOT NULL,
            base_currency TEXT NOT NULL DEFAULT '',
            quantity TEXT NOT NULL,
            cost_basis TEXT NOT NULL,
            realized_pnl TEXT NOT NULL DEFAULT '0',
            fees_paid TEXT NOT NULL DEFAULT '0',
            opened_at TEXT,
            updated_at TEXT,
            PRIMARY KEY (strategy, product_id)
        )""",
        """CREATE TABLE IF NOT EXISTS equity_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            equity TEXT NOT NULL,
            equity_mid TEXT NOT NULL,
            cash TEXT NOT NULL,
            reserved_cash TEXT NOT NULL DEFAULT '0',
            positions_value TEXT NOT NULL DEFAULT '0',
            positions_mid_value TEXT NOT NULL DEFAULT '0',
            realized_pnl TEXT NOT NULL DEFAULT '0',
            unrealized_pnl TEXT NOT NULL DEFAULT '0'
        )""",
        "CREATE INDEX IF NOT EXISTS ix_equity_ts ON equity_snapshots(ts)",
        """CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            strategy TEXT NOT NULL DEFAULT '',
            product_id TEXT NOT NULL DEFAULT '',
            side TEXT,
            target_weight REAL,
            quote_size TEXT,
            base_size TEXT,
            limit_price TEXT,
            expected_edge_bps REAL,
            reason TEXT NOT NULL DEFAULT '',
            decision TEXT NOT NULL DEFAULT '',
            decision_reason TEXT NOT NULL DEFAULT '',
            order_id INTEGER,
            data TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals(ts)",
        """CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            level TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL,
            data TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS strategy_state (
            name TEXT PRIMARY KEY,
            enabled INTEGER,
            params TEXT,
            state TEXT,
            updated_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS risk_limits (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS backtests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT NOT NULL,
            params TEXT,
            start_at TEXT,
            end_at TEXT,
            starting_balance TEXT,
            fee_tier TEXT,
            status TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            metrics TEXT,
            equity_curve TEXT,
            benchmarks TEXT,
            trades TEXT,
            by_year TEXT,
            by_month TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)""",
    ],
}

#: Tables wiped by :meth:`SpotStore.reset_paper_state` (the paper account itself).
PAPER_TABLES = ("orders", "fills", "positions", "equity_snapshots", "signals", "account")


# --------------------------------------------------------------------------- conversions


def _s(x: Decimal | int | float | str | None) -> str | None:
    return None if x is None else str(D(x))


def _d(x: Any) -> Decimal | None:
    return None if x is None or x == "" else D(x)


def _dz(x: Any) -> Decimal:
    return ZERO if x is None or x == "" else D(x)


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, datetime):
        return iso(o)
    if isinstance(o, set | frozenset | tuple):
        return list(o)
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def _dumps(x: Any) -> str | None:
    return None if x is None else json.dumps(x, default=_json_default, separators=(",", ":"))


def _loads(x: str | None) -> Any:
    return None if x is None else json.loads(x)


def _now() -> datetime:
    return datetime.now(UTC)


def _ts(x: datetime | str | None) -> str | None:
    if x is None or isinstance(x, str):
        return x
    return iso(x)


def order_to_row(o: SpotOrder) -> dict[str, Any]:
    return {
        "id": o.id, "product_id": o.product_id, "side": o.side, "order_type": o.order_type, "tif": o.tif,
        "post_only": int(o.post_only), "quote_size": _s(o.quote_size), "base_size": _s(o.base_size),
        "limit_price": _s(o.limit_price), "status": o.status, "status_reason": o.status_reason,
        "filled_base": _s(o.filled_base), "filled_quote": _s(o.filled_quote), "fees": _s(o.fees),
        "strategy": o.strategy, "reason": o.reason, "target_weight": o.target_weight,
        "expected_edge_bps": o.expected_edge_bps, "created_at": iso(o.created_at), "updated_at": iso(o.updated_at),
        "expires_at": iso(o.expires_at), "queue_ahead": _s(o.queue_ahead), "reserved": _s(o.reserved),
        "realized_pnl": _s(o.realized_pnl), "taker_notional": _s(o.taker_notional),
        "maker_notional": _s(o.maker_notional), "taker_fees": _s(o.taker_fees), "maker_fees": _s(o.maker_fees),
        "fee_tier": o.fee_tier, "maker_rate": _s(o.maker_rate), "taker_rate": _s(o.taker_rate),
    }


def order_from_row(r: Mapping[str, Any] | sqlite3.Row) -> SpotOrder:
    return SpotOrder(
        id=r["id"], product_id=r["product_id"], side=r["side"], order_type=r["order_type"], tif=r["tif"],
        post_only=bool(r["post_only"]), quote_size=_d(r["quote_size"]), base_size=_d(r["base_size"]),
        limit_price=_d(r["limit_price"]), status=r["status"], status_reason=r["status_reason"],
        filled_base=_dz(r["filled_base"]), filled_quote=_dz(r["filled_quote"]), fees=_dz(r["fees"]),
        strategy=r["strategy"], reason=r["reason"], target_weight=r["target_weight"],
        expected_edge_bps=r["expected_edge_bps"], created_at=parse_iso(r["created_at"]),
        updated_at=parse_iso(r["updated_at"]), expires_at=parse_iso(r["expires_at"]),
        queue_ahead=_d(r["queue_ahead"]), reserved=_dz(r["reserved"]), realized_pnl=_dz(r["realized_pnl"]),
        taker_notional=_dz(r["taker_notional"]), maker_notional=_dz(r["maker_notional"]),
        taker_fees=_dz(r["taker_fees"]), maker_fees=_dz(r["maker_fees"]), fee_tier=r["fee_tier"],
        maker_rate=_dz(r["maker_rate"]), taker_rate=_dz(r["taker_rate"]),
    )


def fill_to_row(f: SpotFill) -> dict[str, Any]:
    return {
        "id": f.id, "order_id": f.order_id, "product_id": f.product_id, "side": f.side,
        "base_size": _s(f.base_size), "price": _s(f.price), "notional": _s(f.notional), "fee": _s(f.fee),
        "fee_rate": _s(f.fee_rate), "is_taker": int(f.is_taker), "ts": iso(f.ts), "strategy": f.strategy,
        "realized_pnl": _s(f.realized_pnl), "trade_id": f.trade_id,
    }


def fill_from_row(r: Mapping[str, Any] | sqlite3.Row) -> SpotFill:
    return SpotFill(
        id=r["id"], order_id=r["order_id"], product_id=r["product_id"], side=r["side"],
        base_size=D(r["base_size"]), price=D(r["price"]), notional=D(r["notional"]), fee=D(r["fee"]),
        fee_rate=D(r["fee_rate"]), is_taker=bool(r["is_taker"]), ts=parse_iso(r["ts"]),  # type: ignore[arg-type]
        strategy=r["strategy"], realized_pnl=_dz(r["realized_pnl"]), trade_id=r["trade_id"],
    )


def position_to_row(p: SpotPosition) -> dict[str, Any]:
    return {
        "strategy": p.strategy, "product_id": p.product_id, "base_currency": p.base_currency,
        "quantity": _s(p.quantity), "cost_basis": _s(p.cost_basis), "realized_pnl": _s(p.realized_pnl),
        "fees_paid": _s(p.fees_paid), "opened_at": iso(p.opened_at), "updated_at": iso(p.updated_at),
    }


def position_from_row(r: Mapping[str, Any] | sqlite3.Row) -> SpotPosition:
    return SpotPosition(
        product_id=r["product_id"], strategy=r["strategy"], base_currency=r["base_currency"],
        quantity=D(r["quantity"]), cost_basis=D(r["cost_basis"]), realized_pnl=_dz(r["realized_pnl"]),
        fees_paid=_dz(r["fees_paid"]), opened_at=parse_iso(r["opened_at"]), updated_at=parse_iso(r["updated_at"]),
    )


def _upsert_sql(table: str, row: Mapping[str, Any], keys: Sequence[str]) -> str:
    cols = list(row)
    placeholders = ",".join(f":{c}" for c in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in keys)
    return (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT({','.join(keys)}) DO UPDATE SET {updates}")


def _insert_sql(table: str, row: Mapping[str, Any]) -> str:
    cols = list(row)
    return f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join(':' + c for c in cols)})"


# --------------------------------------------------------------------------- SpotStore


class SpotStore:
    """Coinbase paper store. ``SpotStore(":memory:")`` for tests; the app uses
    ``settings.coinbase.storage_path`` with ``exclusive=True`` (one writer process)."""

    def __init__(self, path: str | os.PathLike[str] = DEFAULT_PATH, *, exclusive: bool = False,
                 busy_timeout_s: float = 2.0) -> None:
        self.path = str(path)
        memory = self.path == ":memory:" or self.path.startswith("file::memory:")
        self.memory = memory
        self._plock: ProcessLock | None = None
        if not memory:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            if exclusive:
                self._plock = ProcessLock(self.path).acquire()
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._closed = False
        try:
            self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False,
                                         timeout=busy_timeout_s)
            self._conn.row_factory = sqlite3.Row
            with self._lock:
                if not memory:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_s * 1000)}")
                self._migrate()
        except BaseException:
            if self._plock is not None:
                self._plock.release()
            raise

    # -- lifecycle -------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            finally:
                if self._plock is not None:
                    self._plock.release()

    @contextmanager
    def reader(self) -> Iterator[SpotStore]:
        """A separate read-only connection for heavy scans from a worker thread (in-memory
        stores yield themselves)."""
        if self.memory:
            yield self
            return
        r = SpotStore.__new__(SpotStore)
        r.path = self.path
        r.memory = False
        r._plock = None
        r._lock = threading.RLock()
        r._tx_depth = 0
        r._closed = False
        uri = Path(self.path).resolve().as_uri() + "?mode=ro"
        r._conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False, timeout=5)
        r._conn.row_factory = sqlite3.Row
        try:
            r._conn.execute("PRAGMA query_only=1")
            yield r
        finally:
            r._conn.close()

    def __enter__(self) -> SpotStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[SpotStore]:
        """Atomic block (re-entrant; nested blocks join the outer transaction)."""
        with self._lock:
            outer = self._tx_depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._tx_depth += 1
            try:
                yield self
            except BaseException:
                self._tx_depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._tx_depth -= 1
                if outer:
                    self._conn.execute("COMMIT")

    def _all(self, sql: str, params: Mapping[str, Any] | Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: Mapping[str, Any] | Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # -- schema ----------------------------------------------------------------------

    @property
    def schema_version(self) -> int:
        has = self._one("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'")
        if not has:
            return 0
        row = self._one("SELECT version FROM schema_version WHERE id=1")
        return int(row["version"]) if row else 0

    def _migrate(self) -> None:
        current = self.schema_version
        if current > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"{self.path}: schema version {current} is newer than this code ({SCHEMA_VERSION})")
        if current == SCHEMA_VERSION:
            return
        with self.transaction():
            for v in range(current + 1, SCHEMA_VERSION + 1):
                for stmt in MIGRATIONS[v]:
                    self._conn.execute(stmt)
                self._conn.execute(
                    "INSERT INTO schema_version(id, version, applied_at) VALUES(1, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET version=excluded.version, applied_at=excluded.applied_at",
                    (v, iso(_now())))

    def max_id(self, table: str) -> int:
        if table not in ("orders", "fills", "signals", "logs", "backtests", "equity_snapshots"):
            raise ValueError(f"unknown table {table}")
        row = self._one(f"SELECT MAX(id) AS m FROM {table}")
        return int(row["m"] or 0) if row else 0

    def count(self, table: str, where: str = "", params: Sequence[Any] = ()) -> int:
        if not table.isidentifier():
            raise ValueError(table)
        row = self._one(f"SELECT COUNT(*) AS n FROM {table} {('WHERE ' + where) if where else ''}", params)
        return int(row["n"]) if row else 0

    # -- account ---------------------------------------------------------------------

    def get_account(self) -> dict[str, Any] | None:
        r = self._one("SELECT * FROM account WHERE id=1")
        if r is None:
            return None
        return {
            "starting_balance": D(r["starting_balance"]), "cash": D(r["cash"]),
            "realized_pnl": _dz(r["realized_pnl"]), "fees_paid": _dz(r["fees_paid"]),
            "peak_equity": _d(r["peak_equity"]), "max_drawdown_pct": _dz(r["max_drawdown_pct"]),
            "created_at": parse_iso(r["created_at"]), "updated_at": parse_iso(r["updated_at"]),
        }

    def save_account(self, *, starting_balance: Decimal, cash: Decimal, realized_pnl: Decimal = ZERO,
                     fees_paid: Decimal = ZERO, peak_equity: Decimal | None = None,
                     max_drawdown_pct: Decimal = ZERO, ts: datetime | None = None) -> None:
        now = iso(ts or _now())
        row = {"id": 1, "starting_balance": _s(starting_balance), "cash": _s(cash),
               "realized_pnl": _s(realized_pnl), "fees_paid": _s(fees_paid),
               "peak_equity": _s(peak_equity), "max_drawdown_pct": _s(max_drawdown_pct),
               "created_at": now, "updated_at": now}
        sql = _upsert_sql("account", row, ["id"]).replace(",created_at=excluded.created_at", "")
        with self.transaction():
            self._conn.execute(sql, row)

    # -- orders ----------------------------------------------------------------------

    def upsert_order(self, order: SpotOrder) -> None:
        row = order_to_row(order)
        with self.transaction():
            self._conn.execute(_upsert_sql("orders", row, ["id"]), row)

    def get_order(self, order_id: int) -> SpotOrder | None:
        r = self._one("SELECT * FROM orders WHERE id=?", (order_id,))
        return order_from_row(r) if r else None

    def open_orders(self) -> list[SpotOrder]:
        rows = self._all("SELECT * FROM orders WHERE status IN ('open','partially_filled') ORDER BY id")
        return [order_from_row(r) for r in rows]

    def list_orders(self, status: str | None = "all", *, limit: int | None = 200, strategy: str | None = None,
                    product_id: str | None = None, offset: int = 0) -> list[SpotOrder]:
        """Newest first. ``status``: ``"all"``/None, ``"open"`` (open + partially_filled), or an exact status."""
        where: list[str] = []
        params: list[Any] = []
        if status == "open":
            where.append("status IN ('open','partially_filled')")
        elif status not in (None, "all"):
            where.append("status = ?")
            params.append(status)
        for col, val in (("strategy", strategy), ("product_id", product_id)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(val)
        sql = "SELECT * FROM orders" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        return [order_from_row(r) for r in self._all(sql, params)]

    def trade_counts(self) -> tuple[int, int]:
        """Closed trades: (finished sell orders with fills, of which realized P&L > 0)."""
        rows = self._all("SELECT realized_pnl FROM orders WHERE side='sell' AND status IN "
                         "('filled','cancelled','expired') AND CAST(filled_base AS REAL) > 0")
        return len(rows), sum(1 for r in rows if D(r["realized_pnl"]) > 0)

    # -- fills -----------------------------------------------------------------------

    def insert_fill(self, fill: SpotFill) -> None:
        row = fill_to_row(fill)
        with self.transaction():
            self._conn.execute(_insert_sql("fills", row), row)

    def list_fills(self, *, limit: int | None = 200, order_id: int | None = None, strategy: str | None = None,
                   product_id: str | None = None, since: datetime | None = None) -> list[SpotFill]:
        """Newest first."""
        where: list[str] = []
        params: list[Any] = []
        for col, val in (("order_id", order_id), ("strategy", strategy), ("product_id", product_id)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(val)
        if since is not None:
            where.append("ts >= ?")
            params.append(iso(since))
        sql = "SELECT * FROM fills" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [fill_from_row(r) for r in self._all(sql, params)]

    # -- positions -------------------------------------------------------------------

    def upsert_position(self, pos: SpotPosition) -> None:
        row = position_to_row(pos)
        with self.transaction():
            self._conn.execute(_upsert_sql("positions", row, ["strategy", "product_id"]), row)

    def get_position(self, strategy: str, product_id: str) -> SpotPosition | None:
        r = self._one("SELECT * FROM positions WHERE strategy=? AND product_id=?", (strategy, product_id))
        return position_from_row(r) if r else None

    def list_positions(self, *, open_only: bool = True, strategy: str | None = None) -> list[SpotPosition]:
        rows = self._all("SELECT * FROM positions" + (" WHERE strategy = ?" if strategy is not None else "")
                         + " ORDER BY opened_at, strategy, product_id",
                         (strategy,) if strategy is not None else ())
        out = [position_from_row(r) for r in rows]
        return [p for p in out if p.quantity > 0] if open_only else out

    def strategy_summary(self) -> dict[str, dict[str, Any]]:
        """Per strategy: orders (not rejected), fills, fees, realized_pnl, trades, wins (Decimal-exact)."""
        out: dict[str, dict[str, Any]] = {}

        def row(name: str) -> dict[str, Any]:
            return out.setdefault(name, {"orders": 0, "fills": 0, "fees": ZERO, "realized_pnl": ZERO,
                                         "trades": 0, "wins": 0})

        for r in self._all("SELECT strategy, COUNT(*) AS n FROM orders WHERE status != 'rejected' GROUP BY strategy"):
            row(r["strategy"])["orders"] = r["n"]
        for r in self._all("SELECT strategy, fee, realized_pnl, side FROM fills"):
            x = row(r["strategy"])
            x["fills"] += 1
            x["fees"] += D(r["fee"])
            if r["side"] == "sell":
                x["realized_pnl"] += D(r["realized_pnl"])
        for r in self._all("SELECT strategy, realized_pnl FROM orders WHERE side='sell' AND status IN "
                           "('filled','cancelled','expired') AND CAST(filled_base AS REAL) > 0"):
            x = row(r["strategy"])
            x["trades"] += 1
            x["wins"] += D(r["realized_pnl"]) > 0
        return out

    # -- equity snapshots --------------------------------------------------------------

    def insert_equity_snapshot(self, *, ts: datetime, equity: Decimal, equity_mid: Decimal, cash: Decimal,
                               reserved_cash: Decimal = ZERO, positions_value: Decimal = ZERO,
                               positions_mid_value: Decimal = ZERO, realized_pnl: Decimal = ZERO,
                               unrealized_pnl: Decimal = ZERO) -> None:
        row = {"ts": iso(ts), "equity": _s(equity), "equity_mid": _s(equity_mid), "cash": _s(cash),
               "reserved_cash": _s(reserved_cash), "positions_value": _s(positions_value),
               "positions_mid_value": _s(positions_mid_value), "realized_pnl": _s(realized_pnl),
               "unrealized_pnl": _s(unrealized_pnl)}
        with self.transaction():
            self._conn.execute(_insert_sql("equity_snapshots", row), row)

    def list_equity(self, *, since: datetime | None = None, until: datetime | None = None,
                    max_points: int | None = None) -> list[dict[str, Any]]:
        """Oldest first (Decimal values). ``max_points`` thins evenly (always keeps the latest point)."""
        where: list[str] = []
        params: list[Any] = []
        if since is not None:
            where.append("ts >= ?")
            params.append(iso(since))
        if until is not None:
            where.append("ts <= ?")
            params.append(iso(until))
        cond = (" WHERE " + " AND ".join(where)) if where else ""
        rows: list[sqlite3.Row] | None = None
        if max_points and max_points > 0:
            row = self._one("SELECT COUNT(*) AS n FROM equity_snapshots" + cond, params)
            if row is not None and int(row["n"]) > max_points:
                ids = [r["id"] for r in self._all("SELECT id FROM equity_snapshots" + cond + " ORDER BY ts, id",
                                                  params)]
                keep = ids[:: -(-len(ids) // max_points)]
                if keep[-1] != ids[-1]:
                    keep.append(ids[-1])
                rows = []
                for i in range(0, len(keep), 500):
                    chunk = keep[i: i + 500]
                    rows += self._all(f"SELECT * FROM equity_snapshots WHERE id IN ({','.join('?' * len(chunk))}) "
                                      "ORDER BY ts, id", chunk)
        if rows is None:
            rows = self._all("SELECT * FROM equity_snapshots" + cond + " ORDER BY ts, id", params)
        return [{
            "ts": parse_iso(r["ts"]), "equity": D(r["equity"]), "equity_mid": D(r["equity_mid"]),
            "cash": D(r["cash"]), "reserved_cash": D(r["reserved_cash"]),
            "positions_value": D(r["positions_value"]), "positions_mid_value": D(r["positions_mid_value"]),
            "realized_pnl": D(r["realized_pnl"]), "unrealized_pnl": D(r["unrealized_pnl"]),
        } for r in rows]

    def equity_drawdown(self) -> tuple[float, float]:
        """(max drawdown in $, in % of the running peak) over every stored snapshot."""
        with self._lock:
            vals = [float(r[0]) for r in self._conn.execute(
                "SELECT CAST(equity AS REAL) FROM equity_snapshots ORDER BY ts, id")]
        peak = dd = pct = 0.0
        for i, e in enumerate(vals):
            peak = e if i == 0 else max(peak, e)
            dd = max(dd, peak - e)
            if peak > 0:
                pct = max(pct, (peak - e) / peak * 100.0)
        return dd, pct

    def downsample_equity(self, older_than: datetime, bucket_s: int = 3600) -> int:
        """Thin snapshots older than ``older_than`` to the first, lowest, highest and last row
        of each ``bucket_s`` bucket (idempotent). Returns the number of rows deleted."""
        with self.transaction():
            cur = self._conn.execute(
                "DELETE FROM equity_snapshots WHERE ts < :cut AND id NOT IN ("
                " SELECT id FROM ("
                "  SELECT id,"
                "   ROW_NUMBER() OVER (PARTITION BY b ORDER BY e, id) AS r_lo,"
                "   ROW_NUMBER() OVER (PARTITION BY b ORDER BY e DESC, id) AS r_hi,"
                "   ROW_NUMBER() OVER (PARTITION BY b ORDER BY ts, id) AS r_first,"
                "   ROW_NUMBER() OVER (PARTITION BY b ORDER BY ts DESC, id DESC) AS r_last"
                "  FROM (SELECT id, ts, CAST(equity AS REAL) AS e,"
                "        CAST(strftime('%s', substr(ts, 1, 19)) AS INTEGER) / :b AS b"
                "        FROM equity_snapshots WHERE ts < :cut))"
                " WHERE r_lo = 1 OR r_hi = 1 OR r_first = 1 OR r_last = 1)",
                {"cut": iso(older_than), "b": int(bucket_s)})
            return int(cur.rowcount or 0)

    # -- signals / logs --------------------------------------------------------------

    _SIGNAL_COLS = ("ts", "strategy", "product_id", "side", "target_weight", "quote_size", "base_size",
                    "limit_price", "expected_edge_bps", "reason", "decision", "decision_reason", "order_id", "data")

    def insert_signal(self, **fields: Any) -> int:
        """Record a strategy intent and what happened to it (signals feed). Unknown keyword
        arguments go into ``data``."""
        unknown = set(fields) - set(self._SIGNAL_COLS)
        data = dict(fields.pop("data", None) or {})
        for k in unknown:
            data[k] = fields.pop(k)
        tw, eb = fields.get("target_weight"), fields.get("expected_edge_bps")
        row: dict[str, Any] = {
            "ts": _ts(fields.get("ts")) or iso(_now()),
            "strategy": fields.get("strategy") or "", "product_id": fields.get("product_id") or "",
            "side": fields.get("side"), "target_weight": float(tw) if tw is not None else None,
            "quote_size": _s(fields.get("quote_size")), "base_size": _s(fields.get("base_size")),
            "limit_price": _s(fields.get("limit_price")),
            "expected_edge_bps": float(eb) if eb is not None else None,
            "reason": fields.get("reason") or "", "decision": fields.get("decision") or "",
            "decision_reason": fields.get("decision_reason") or "", "order_id": fields.get("order_id"),
            "data": _dumps(data) if data else None,
        }
        with self.transaction():
            return int(self._conn.execute(_insert_sql("signals", row), row).lastrowid or 0)

    def list_signals(self, *, limit: int | None = 200, strategy: str | None = None,
                     decision: str | None = None) -> list[dict[str, Any]]:
        """Newest first; sizes/prices as Decimal, ``ts`` as datetime."""
        where: list[str] = []
        params: list[Any] = []
        for col, val in (("strategy", strategy), ("decision", decision)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(val)
        sql = "SELECT * FROM signals" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        out = []
        for r in self._all(sql, params):
            d = dict(r)
            d["ts"] = parse_iso(d["ts"])
            for k in ("quote_size", "base_size", "limit_price"):
                d[k] = _d(d[k])
            d["data"] = _loads(d["data"])
            out.append(d)
        return out

    def insert_log(self, level: str, kind: str, message: str, data: Mapping[str, Any] | None = None,
                   ts: datetime | None = None) -> int:
        row = {"ts": iso(ts or _now()), "level": level, "kind": kind, "message": message,
               "data": _dumps(dict(data)) if data else None}
        with self.transaction():
            return int(self._conn.execute(_insert_sql("logs", row), row).lastrowid or 0)

    def list_logs(self, *, limit: int | None = 200, level: str | None = None,
                  kind: str | None = None) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        for col, val in (("level", level), ("kind", kind)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(val)
        sql = "SELECT * FROM logs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [{"id": r["id"], "ts": parse_iso(r["ts"]), "level": r["level"], "kind": r["kind"],
                 "message": r["message"], "data": _loads(r["data"])} for r in self._all(sql, params)]

    def prune(self, table: str, keep_last: int) -> int:
        """Delete all but the newest ``keep_last`` rows of logs/signals/equity_snapshots."""
        if table not in ("logs", "signals", "equity_snapshots"):
            raise ValueError(f"prune not allowed for {table}")
        with self.transaction():
            cur = self._conn.execute(
                f"DELETE FROM {table} WHERE id <= (SELECT COALESCE(MAX(id), 0) FROM {table}) - ?", (keep_last,))
            return int(cur.rowcount or 0)

    # -- strategy state --------------------------------------------------------------

    def get_strategy_state(self, name: str) -> dict[str, Any] | None:
        r = self._one("SELECT * FROM strategy_state WHERE name=?", (name,))
        if r is None:
            return None
        return {"name": r["name"], "enabled": None if r["enabled"] is None else bool(r["enabled"]),
                "params": _loads(r["params"]), "state": _loads(r["state"]),
                "updated_at": parse_iso(r["updated_at"])}

    def list_strategy_states(self) -> dict[str, dict[str, Any]]:
        names = [r["name"] for r in self._all("SELECT name FROM strategy_state ORDER BY name")]
        return {n: self.get_strategy_state(n) for n in names}  # type: ignore[misc]

    def save_strategy_state(self, name: str, *, enabled: bool | None = None, params: Mapping[str, Any] | None = None,
                            state: Any = None) -> None:
        """Upsert; arguments left as None keep their stored value."""
        with self.transaction():
            cur = self.get_strategy_state(name) or {}
            row = {
                "name": name,
                "enabled": int(enabled) if enabled is not None else (
                    None if cur.get("enabled") is None else int(cur["enabled"])),
                "params": _dumps(dict(params)) if params is not None else _dumps(cur.get("params")),
                "state": _dumps(state) if state is not None else _dumps(cur.get("state")),
                "updated_at": iso(_now()),
            }
            self._conn.execute(_upsert_sql("strategy_state", row, ["name"]), row)

    # -- risk limits -----------------------------------------------------------------

    def get_risk_limits(self) -> dict[str, Any]:
        """Runtime risk-limit overrides (``PATCH /api/coinbase/risk``), JSON-decoded."""
        return {r["key"]: _loads(r["value"]) for r in self._all("SELECT key, value FROM risk_limits")}

    def save_risk_limits(self, limits: Mapping[str, Any], *, replace: bool = False) -> None:
        with self.transaction():
            if replace:
                self._conn.execute("DELETE FROM risk_limits")
            for k, v in limits.items():
                self._conn.execute(
                    "INSERT INTO risk_limits(key, value, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (k, _dumps(v), iso(_now())))

    # -- backtests -------------------------------------------------------------------

    _BT_JSON = ("params", "metrics", "equity_curve", "benchmarks", "trades", "by_year", "by_month")

    def create_backtest(self, strategy: str, params: Mapping[str, Any] | None = None, *, start: str | None = None,
                        end: str | None = None, starting_balance: Decimal | float | None = None,
                        fee_tier: str | None = None, status: str = "running") -> int:
        row = {"strategy": strategy, "params": _dumps(dict(params or {})), "start_at": start, "end_at": end,
               "starting_balance": _s(starting_balance), "fee_tier": fee_tier, "status": status,
               "created_at": iso(_now())}
        with self.transaction():
            return int(self._conn.execute(_insert_sql("backtests", row), row).lastrowid or 0)

    def update_backtest(self, backtest_id: int, **fields: Any) -> None:
        allowed = {"status", "error", "finished_at", "metrics", "equity_curve", "benchmarks", "trades", "by_year",
                   "by_month", "params", "start", "end", "fee_tier"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown backtest fields: {sorted(bad)}")
        if not fields:
            return
        rename = {"start": "start_at", "end": "end_at"}
        row = {rename.get(k, k): (_dumps(v) if k in self._BT_JSON else (_ts(v) if k == "finished_at" else v))
               for k, v in fields.items()}
        sets = ",".join(f"{k}=:{k}" for k in row)
        row["id"] = backtest_id
        with self.transaction():
            self._conn.execute(f"UPDATE backtests SET {sets} WHERE id=:id", row)

    def _bt(self, r: sqlite3.Row, full: bool) -> dict[str, Any]:
        d = {"id": r["id"], "strategy": r["strategy"], "params": _loads(r["params"]) or {}, "start": r["start_at"],
             "end": r["end_at"], "starting_balance": _d(r["starting_balance"]), "fee_tier": r["fee_tier"],
             "status": r["status"], "error": r["error"], "created_at": parse_iso(r["created_at"]),
             "finished_at": parse_iso(r["finished_at"]), "metrics": _loads(r["metrics"])}
        if full:
            d.update(equity_curve=_loads(r["equity_curve"]) or [], benchmarks=_loads(r["benchmarks"]) or {},
                     trades=_loads(r["trades"]) or [], by_year=_loads(r["by_year"]) or [],
                     by_month=_loads(r["by_month"]) or [])
        return d

    def get_backtest(self, backtest_id: int) -> dict[str, Any] | None:
        r = self._one("SELECT * FROM backtests WHERE id=?", (backtest_id,))
        return self._bt(r, True) if r else None

    def list_backtests(self, *, limit: int | None = 100) -> list[dict[str, Any]]:
        sql = ("SELECT id, strategy, params, start_at, end_at, starting_balance, fee_tier, status, error, "
               "created_at, finished_at, metrics FROM backtests ORDER BY id DESC")
        params: list[Any] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [self._bt(r, False) for r in self._all(sql, params)]

    # -- kv ----------------------------------------------------------------------------

    def get_kv(self, key: str, default: Any = None) -> Any:
        r = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return default if r is None else _loads(r["value"])

    def set_kv(self, key: str, value: Any) -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, _dumps(value), iso(_now())))

    def delete_kv(self, key: str) -> None:
        with self.transaction():
            self._conn.execute("DELETE FROM kv WHERE key=?", (key,))

    # -- reset -------------------------------------------------------------------------

    def reset_paper_state(self) -> None:
        """Wipe the paper account (orders, fills, positions, equity, signals, account and the
        broker's ``broker.*`` kv). Logs, strategy state, risk limits, backtests and other kv
        keys (e.g. the kill switch) are kept."""
        with self.transaction():
            for t in PAPER_TABLES:
                self._conn.execute(f"DELETE FROM {t}")
            self._conn.execute("DELETE FROM kv WHERE key LIKE 'broker.%'")
