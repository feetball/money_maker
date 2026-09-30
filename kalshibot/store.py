"""SQLite persistence (ARCHITECTURE.md §1, §6): stdlib ``sqlite3``, WAL mode, no ORM.

Everything the paper account needs to resume after a restart lives here: the account row
(cash, counters), orders (incl. the per-order fee accumulator and queue position), fills,
positions, settlements, plus the broker's small working state (consumed liquidity, trade
cursors, daily equity baseline) in ``kv``. Also: equity snapshots, signals, logs,
strategy state, risk-limit overrides and backtests.

Conventions
-----------
* Money/prices/sizes are stored as TEXT (``str(Decimal)``) so they round-trip exactly.
* Timestamps are TEXT, UTC ISO-8601 with microseconds and ``Z`` (sortable).
* JSON blobs are TEXT.

Single writer
-------------
``Store(path, exclusive=True)`` (used by ``kalshibot serve`` and ``kalshibot reset``) takes an
exclusive ``fcntl.flock`` on ``<path>.lock``; a second writer fails fast with
:class:`StoreLockedError` instead of silently interleaving with the running server's
in-memory ledger. ``busy_timeout`` is short (2 s; it was 30 s) so an external lock holder
cannot freeze the event loop for long - a write that times out fails cleanly (the broker
restores its in-memory state) and is retried by the next poll. Heavy read-only scans (analytics) go through :meth:`Store.reader`,
a separate read-only connection usable from a worker thread (WAL readers never block the
writer).

Schema versioning
-----------------
``meta.schema_version`` holds the applied version; :data:`MIGRATIONS` maps each version to
its DDL. Opening an older database applies the missing migrations in order inside one
transaction; opening a *newer* one raises :class:`SchemaVersionError`.

Thread safety
-------------
The FastAPI server (worker threads) and the engine (event loop) share one :class:`Store`.
It holds **one connection** (``check_same_thread=False``) guarded by a re-entrant
``threading.RLock``: every public method takes the lock, and :meth:`Store.transaction`
holds it for the whole ``BEGIN IMMEDIATE ... COMMIT`` block, so multi-row broker updates
are atomic and never interleave with another thread's statements. Writes are small and
infrequent (a few per second at most), so a single serialized connection is simpler and
safer than a pool; WAL still lets *other processes* (e.g. ``sqlite3`` CLI) read while the
app writes.
"""

from __future__ import annotations

import contextlib
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

from kalshibot.money import ZERO, D
from kalshibot.paper.models import Fill, Order, Position, Settlement, iso, parse_iso

__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "ProcessLock", "SchemaVersionError", "Store", "StoreLockedError"]

SCHEMA_VERSION = 1

MIGRATIONS: dict[int, list[str]] = {
    1: [
        """CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)""",
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
            ticker TEXT NOT NULL,
            event_ticker TEXT NOT NULL DEFAULT '',
            side TEXT NOT NULL,
            action TEXT NOT NULL,
            count INTEGER NOT NULL,
            filled_count INTEGER NOT NULL DEFAULT 0,
            limit_price TEXT NOT NULL,
            avg_fill_price TEXT,
            tif TEXT NOT NULL,
            status TEXT NOT NULL,
            status_reason TEXT NOT NULL DEFAULT '',
            strategy TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            expected_edge TEXT,
            fair_value REAL,
            group_id TEXT,
            queue_ahead TEXT,
            created_at TEXT,
            updated_at TEXT,
            expires_at TEXT,
            fees TEXT NOT NULL DEFAULT '0',
            reserved TEXT NOT NULL DEFAULT '0',
            filled_notional TEXT NOT NULL DEFAULT '0',
            fill_credit TEXT NOT NULL DEFAULT '0',
            taker_filled_count INTEGER NOT NULL DEFAULT 0,
            fee_state TEXT NOT NULL DEFAULT '{}'
        )""",
        "CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status)",
        "CREATE INDEX IF NOT EXISTS ix_orders_strategy ON orders(strategy)",
        "CREATE INDEX IF NOT EXISTS ix_orders_ticker ON orders(ticker)",
        """CREATE TABLE IF NOT EXISTS fills (
            id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            event_ticker TEXT NOT NULL DEFAULT '',
            side TEXT NOT NULL,
            action TEXT NOT NULL,
            count INTEGER NOT NULL,
            price TEXT NOT NULL,
            fee TEXT NOT NULL,
            is_taker INTEGER NOT NULL,
            ts TEXT NOT NULL,
            strategy TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX IF NOT EXISTS ix_fills_order ON fills(order_id)",
        "CREATE INDEX IF NOT EXISTS ix_fills_strategy ON fills(strategy)",
        """CREATE TABLE IF NOT EXISTS positions (
            strategy TEXT NOT NULL,
            ticker TEXT NOT NULL,
            event_ticker TEXT NOT NULL DEFAULT '',
            side TEXT NOT NULL,
            count INTEGER NOT NULL,
            cost_basis TEXT NOT NULL,
            open_fees TEXT NOT NULL DEFAULT '0',
            realized_pnl TEXT NOT NULL DEFAULT '0',
            fees_paid TEXT NOT NULL DEFAULT '0',
            opened_at TEXT,
            updated_at TEXT,
            expected_edge_total TEXT,
            fv_sum REAL NOT NULL DEFAULT 0,
            fv_weight REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (strategy, ticker)
        )""",
        """CREATE TABLE IF NOT EXISTS settlements (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            event_ticker TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'settlement',
            result TEXT NOT NULL,
            side TEXT NOT NULL,
            count INTEGER NOT NULL,
            payout TEXT NOT NULL,
            cost_basis TEXT NOT NULL,
            fees TEXT NOT NULL DEFAULT '0',
            pnl TEXT NOT NULL,
            expected_edge TEXT,
            fair_value REAL,
            settlement_value TEXT,
            exit_price TEXT,
            opened_at TEXT,
            ts TEXT NOT NULL,
            strategy TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX IF NOT EXISTS ix_settlements_strategy ON settlements(strategy)",
        """CREATE TABLE IF NOT EXISTS equity_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            equity TEXT NOT NULL,
            equity_mid TEXT NOT NULL,
            cash TEXT NOT NULL,
            reserved_cash TEXT NOT NULL DEFAULT '0',
            positions_value TEXT NOT NULL DEFAULT '0',
            realized_pnl TEXT NOT NULL DEFAULT '0',
            unrealized_pnl TEXT NOT NULL DEFAULT '0'
        )""",
        "CREATE INDEX IF NOT EXISTS ix_equity_ts ON equity_snapshots(ts)",
        """CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            strategy TEXT NOT NULL DEFAULT '',
            ticker TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            side TEXT,
            action TEXT,
            count INTEGER,
            limit_price TEXT,
            fair_value REAL,
            expected_edge TEXT,
            reason TEXT NOT NULL DEFAULT '',
            decision TEXT NOT NULL DEFAULT '',
            decision_reason TEXT NOT NULL DEFAULT '',
            order_id INTEGER,
            group_id TEXT,
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
            status TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            metrics TEXT,
            equity_curve TEXT,
            trades TEXT,
            by_month TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)""",
    ],
}

#: Tables wiped by :meth:`Store.reset_paper_state` (the paper account itself).
PAPER_TABLES = ("orders", "fills", "positions", "settlements", "equity_snapshots", "signals", "account")


class SchemaVersionError(RuntimeError):
    pass


class StoreLockedError(RuntimeError):
    """Another process holds the database's single-writer lock (e.g. ``kalshibot serve`` is running)."""


class ProcessLock:
    """Exclusive advisory lock on ``<db>.lock`` so only one process writes a paper account.

    ``fcntl.flock`` locks belong to the open file description: they are released when the
    holder closes the file or dies (no stale-lock cleanup needed), and a second open of the
    same file - even in the same process - conflicts. Not available on Windows (no-op there).
    """

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.db_path = str(db_path)
        self.path = f"{self.db_path}.lock"
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> ProcessLock:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX
            return self
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                holder = os.pread(fd, 32, 0).decode("ascii", "replace").strip()
            except OSError:
                holder = ""
            os.close(fd)
            who = f" (pid {holder})" if holder.isdigit() else ""
            raise StoreLockedError(
                f"database {self.db_path} is in use by another kalshibot process{who}; stop it first "
                f"(a running server can reset the account via POST /api/account/reset)") from None
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)
            os.pwrite(fd, str(os.getpid()).encode("ascii"), 0)
        self._fd = fd
        return self

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        except (ImportError, OSError):  # pragma: no cover
            pass
        finally:
            os.close(fd)


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


def order_to_row(o: Order) -> dict[str, Any]:
    return {
        "id": o.id, "ticker": o.ticker, "event_ticker": o.event_ticker, "side": o.side,
        "action": o.action, "count": o.count, "filled_count": o.filled_count,
        "limit_price": _s(o.limit_price), "avg_fill_price": _s(o.avg_fill_price), "tif": o.tif,
        "status": o.status, "status_reason": o.status_reason, "strategy": o.strategy,
        "reason": o.reason, "expected_edge": _s(o.expected_edge), "fair_value": o.fair_value,
        "group_id": o.group_id, "queue_ahead": _s(o.queue_ahead), "created_at": iso(o.created_at),
        "updated_at": iso(o.updated_at), "expires_at": iso(o.expires_at), "fees": _s(o.fees),
        "reserved": _s(o.reserved), "filled_notional": _s(o.filled_notional),
        "fill_credit": _s(o.fill_credit), "taker_filled_count": o.taker_filled_count,
        "fee_state": _dumps(o.fee_state or {}),
    }


def order_from_row(r: Mapping[str, Any]) -> Order:
    return Order(
        id=r["id"], ticker=r["ticker"], side=r["side"], action=r["action"], count=r["count"],
        limit_price=D(r["limit_price"]), tif=r["tif"], status=r["status"],
        filled_count=r["filled_count"], avg_fill_price=_d(r["avg_fill_price"]),
        strategy=r["strategy"], reason=r["reason"], expected_edge=_d(r["expected_edge"]),
        fair_value=r["fair_value"], group_id=r["group_id"], queue_ahead=_d(r["queue_ahead"]),
        created_at=parse_iso(r["created_at"]), updated_at=parse_iso(r["updated_at"]),
        expires_at=parse_iso(r["expires_at"]), fees=_dz(r["fees"]), event_ticker=r["event_ticker"],
        status_reason=r["status_reason"], reserved=_dz(r["reserved"]),
        filled_notional=_dz(r["filled_notional"]), fill_credit=_dz(r["fill_credit"]),
        fee_state=_loads(r["fee_state"]) or {}, taker_filled_count=r["taker_filled_count"],
    )


def fill_to_row(f: Fill) -> dict[str, Any]:
    return {
        "id": f.id, "order_id": f.order_id, "ticker": f.ticker, "event_ticker": f.event_ticker,
        "side": f.side, "action": f.action, "count": f.count, "price": _s(f.price),
        "fee": _s(f.fee), "is_taker": int(f.is_taker), "ts": iso(f.ts), "strategy": f.strategy,
    }


def fill_from_row(r: Mapping[str, Any]) -> Fill:
    return Fill(
        id=r["id"], order_id=r["order_id"], ticker=r["ticker"], side=r["side"], action=r["action"],
        count=r["count"], price=D(r["price"]), fee=D(r["fee"]), is_taker=bool(r["is_taker"]),
        ts=parse_iso(r["ts"]), strategy=r["strategy"], event_ticker=r["event_ticker"],
    )


def position_to_row(p: Position) -> dict[str, Any]:
    return {
        "strategy": p.strategy, "ticker": p.ticker, "event_ticker": p.event_ticker, "side": p.side,
        "count": p.count, "cost_basis": _s(p.cost_basis), "open_fees": _s(p.open_fees),
        "realized_pnl": _s(p.realized_pnl), "fees_paid": _s(p.fees_paid),
        "opened_at": iso(p.opened_at), "updated_at": iso(p.updated_at),
        "expected_edge_total": _s(p.expected_edge_total), "fv_sum": p.fv_sum, "fv_weight": p.fv_weight,
    }


def position_from_row(r: Mapping[str, Any]) -> Position:
    return Position(
        ticker=r["ticker"], strategy=r["strategy"], event_ticker=r["event_ticker"], side=r["side"],
        count=r["count"], cost_basis=D(r["cost_basis"]), realized_pnl=_dz(r["realized_pnl"]),
        fees_paid=_dz(r["fees_paid"]), opened_at=parse_iso(r["opened_at"]),
        expected_edge_total=_d(r["expected_edge_total"]), open_fees=_dz(r["open_fees"]),
        fv_sum=r["fv_sum"] or 0.0, fv_weight=r["fv_weight"] or 0.0, updated_at=parse_iso(r["updated_at"]),
    )


def settlement_to_row(s: Settlement) -> dict[str, Any]:
    return {
        "id": s.id, "ticker": s.ticker, "event_ticker": s.event_ticker, "kind": s.kind,
        "result": s.result, "side": s.side, "count": s.count, "payout": _s(s.payout),
        "cost_basis": _s(s.cost_basis), "fees": _s(s.fees), "pnl": _s(s.pnl),
        "expected_edge": _s(s.expected_edge), "fair_value": s.fair_value,
        "settlement_value": _s(s.settlement_value), "exit_price": _s(s.exit_price),
        "opened_at": iso(s.opened_at), "ts": iso(s.ts), "strategy": s.strategy,
    }


def settlement_from_row(r: Mapping[str, Any]) -> Settlement:
    return Settlement(
        id=r["id"], ticker=r["ticker"], result=r["result"], side=r["side"], count=r["count"],
        payout=D(r["payout"]), cost_basis=D(r["cost_basis"]), pnl=D(r["pnl"]), ts=parse_iso(r["ts"]),
        strategy=r["strategy"], event_ticker=r["event_ticker"], kind=r["kind"], fees=_dz(r["fees"]),
        expected_edge=_d(r["expected_edge"]), fair_value=r["fair_value"],
        settlement_value=_d(r["settlement_value"]), exit_price=_d(r["exit_price"]),
        opened_at=parse_iso(r["opened_at"]),
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


# --------------------------------------------------------------------------- Store


class Store:
    """SQLite store. ``Store(":memory:")`` for tests; the app uses ``settings.storage.path``."""

    def __init__(self, path: str | os.PathLike[str] = "data/kalshibot.sqlite3", *, exclusive: bool = False,
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
            try:
                self._conn.close()
            finally:
                if self._plock is not None:
                    self._plock.release()

    @contextmanager
    def reader(self) -> Iterator[Store]:
        """A separate read-only connection for heavy scans from a worker thread.

        WAL readers see a consistent snapshot and never block the writer (or the event
        loop, which serializes on this store's lock). An in-memory store has no second
        connection, so it yields itself.
        """
        if self.memory:
            yield self
            return
        r = Store.__new__(Store)
        r.path = self.path
        r.memory = False
        r._plock = None
        r._lock = threading.RLock()
        r._tx_depth = 0
        uri = Path(self.path).resolve().as_uri() + "?mode=ro"
        r._conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False, timeout=5)
        r._conn.row_factory = sqlite3.Row
        try:
            r._conn.execute("PRAGMA query_only=1")
            yield r
        finally:
            r._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[Store]:
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

    def _exec(self, sql: str, params: Mapping[str, Any] | Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _all(self, sql: str, params: Mapping[str, Any] | Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: Mapping[str, Any] | Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # -- schema ----------------------------------------------------------------------

    @property
    def schema_version(self) -> int:
        row = self._one("SELECT value FROM meta WHERE key='schema_version'")
        return int(row["value"]) if row else 0

    def _migrate(self) -> None:
        has_meta = self._one("SELECT name FROM sqlite_master WHERE type='table' AND name='meta'")
        current = self.schema_version if has_meta else 0
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
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(v),))

    def max_id(self, table: str) -> int:
        if table not in ("orders", "fills", "settlements", "signals", "logs", "backtests", "equity_snapshots"):
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

    def upsert_order(self, order: Order) -> None:
        row = order_to_row(order)
        with self.transaction():
            self._conn.execute(_upsert_sql("orders", row, ["id"]), row)

    def get_order(self, order_id: int) -> Order | None:
        r = self._one("SELECT * FROM orders WHERE id=?", (order_id,))
        return order_from_row(r) if r else None

    def open_orders(self) -> list[Order]:
        rows = self._all("SELECT * FROM orders WHERE status IN ('open','partially_filled') ORDER BY id")
        return [order_from_row(r) for r in rows]

    def list_orders(self, status: str | None = "all", *, limit: int | None = 200, strategy: str | None = None,
                    ticker: str | None = None, group_id: str | None = None, offset: int = 0) -> list[Order]:
        """Newest first. ``status``: ``"all"``/None, ``"open"`` (open + partially_filled), or an exact status."""
        where, params = [], []
        if status == "open":
            where.append("status IN ('open','partially_filled')")
        elif status not in (None, "all"):
            where.append("status = ?")
            params.append(status)
        for col, val in (("strategy", strategy), ("ticker", ticker), ("group_id", group_id)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(val)
        sql = "SELECT * FROM orders"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        return [order_from_row(r) for r in self._all(sql, params)]

    # -- fills -----------------------------------------------------------------------

    def insert_fill(self, fill: Fill) -> None:
        row = fill_to_row(fill)
        with self.transaction():
            self._conn.execute(_insert_sql("fills", row), row)

    def list_fills(self, *, limit: int | None = 200, order_id: int | None = None, strategy: str | None = None,
                   ticker: str | None = None, since: datetime | None = None) -> list[Fill]:
        where, params = [], []
        for col, val in (("order_id", order_id), ("strategy", strategy), ("ticker", ticker)):
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

    def upsert_position(self, pos: Position) -> None:
        row = position_to_row(pos)
        with self.transaction():
            self._conn.execute(_upsert_sql("positions", row, ["strategy", "ticker"]), row)

    def get_position(self, strategy: str, ticker: str) -> Position | None:
        r = self._one("SELECT * FROM positions WHERE strategy=? AND ticker=?", (strategy, ticker))
        return position_from_row(r) if r else None

    def list_positions(self, *, open_only: bool = True, strategy: str | None = None) -> list[Position]:
        where, params = [], []
        if open_only:
            where.append("count > 0")
        if strategy is not None:
            where.append("strategy = ?")
            params.append(strategy)
        sql = "SELECT * FROM positions" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY opened_at, strategy, ticker"
        return [position_from_row(r) for r in self._all(sql, params)]

    # -- settlements -----------------------------------------------------------------

    def insert_settlement(self, s: Settlement) -> None:
        row = settlement_to_row(s)
        with self.transaction():
            self._conn.execute(_insert_sql("settlements", row), row)

    def list_settlements(self, *, limit: int | None = 200, strategy: str | None = None, kind: str | None = None,
                         since: datetime | None = None, ticker: str | None = None) -> list[Settlement]:
        where, params = [], []
        for col, val in (("strategy", strategy), ("kind", kind), ("ticker", ticker)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(val)
        if since is not None:
            where.append("ts >= ?")
            params.append(iso(since))
        sql = "SELECT * FROM settlements" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [settlement_from_row(r) for r in self._all(sql, params)]

    def settlement_counts(self) -> tuple[int, int]:
        """(number of settlement rows, rows with pnl > 0)."""
        rows = self._all("SELECT pnl FROM settlements")
        return len(rows), sum(1 for r in rows if D(r["pnl"]) > 0)

    def strategy_summary(self) -> dict[str, dict[str, Any]]:
        """Per strategy: orders, fills, settled, wins, realized_pnl, fees (Decimal-exact sums)."""
        out: dict[str, dict[str, Any]] = {}

        def row(name: str) -> dict[str, Any]:
            return out.setdefault(name, {"orders": 0, "fills": 0, "settled": 0, "wins": 0,
                                         "realized_pnl": ZERO, "fees": ZERO})

        for r in self._all("SELECT strategy, COUNT(*) AS n FROM orders WHERE status != 'rejected' GROUP BY strategy"):
            row(r["strategy"])["orders"] = r["n"]
        for r in self._all("SELECT strategy, fee FROM fills"):
            x = row(r["strategy"])
            x["fills"] += 1
            x["fees"] += D(r["fee"])
        for r in self._all("SELECT strategy, pnl FROM settlements"):
            x = row(r["strategy"])
            x["settled"] += 1
            p = D(r["pnl"])
            x["realized_pnl"] += p
            x["wins"] += p > 0
        return out

    # -- equity snapshots --------------------------------------------------------------

    def insert_equity_snapshot(self, *, ts: datetime, equity: Decimal, equity_mid: Decimal, cash: Decimal,
                               reserved_cash: Decimal = ZERO, positions_value: Decimal = ZERO,
                               realized_pnl: Decimal = ZERO, unrealized_pnl: Decimal = ZERO) -> None:
        row = {"ts": iso(ts), "equity": _s(equity), "equity_mid": _s(equity_mid), "cash": _s(cash),
               "reserved_cash": _s(reserved_cash), "positions_value": _s(positions_value),
               "realized_pnl": _s(realized_pnl), "unrealized_pnl": _s(unrealized_pnl)}
        with self.transaction():
            self._conn.execute(_insert_sql("equity_snapshots", row), row)

    def list_equity(self, *, since: datetime | None = None, until: datetime | None = None,
                    max_points: int | None = None) -> list[dict[str, Any]]:
        """Oldest first. ``max_points`` thins evenly (always keeps the latest point)."""
        where, params = [], []
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
                # thin (charts only): every k-th id plus the latest; only the kept rows are decoded
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
            "positions_value": D(r["positions_value"]), "realized_pnl": D(r["realized_pnl"]),
            "unrealized_pnl": D(r["unrealized_pnl"]),
        } for r in rows]

    def equity_drawdown(self) -> tuple[float, float]:
        """(max drawdown in $, max drawdown in % of the running peak) over **every** stored
        equity snapshot, computed in SQL (never on a thinned series)."""
        import numpy as np  # local: the store itself does not need numpy

        with self._lock:
            cur = self._conn.execute("SELECT CAST(equity AS REAL) FROM equity_snapshots ORDER BY ts, id")
            e = np.fromiter((r[0] for r in cur), dtype=float)
        if e.size == 0:
            return 0.0, 0.0
        peak = np.maximum.accumulate(e)
        dd = peak - e
        with np.errstate(divide="ignore", invalid="ignore"):
            pct = np.where(peak > 0, dd / peak * 100.0, 0.0)
        return max(0.0, float(dd.max())), max(0.0, float(pct.max()))

    def downsample_equity(self, older_than: datetime, bucket_s: int = 3600, *,
                          newer_than: datetime | None = None) -> int:
        """Thin snapshots older than ``older_than`` to the first, lowest, highest and last row
        of each ``bucket_s`` bucket (extremes survive for charts and drawdowns; idempotent).
        ``newer_than`` limits the pass to recent rows (a bucket cut by it keeps a few extra
        rows, never fewer). Returns the number of rows deleted."""
        lo = iso(newer_than) if newer_than is not None else ""
        with self.transaction():
            cur = self._conn.execute(
                "DELETE FROM equity_snapshots WHERE ts < :cut AND ts >= :lo AND id NOT IN ("
                " SELECT id FROM ("
                "  SELECT id,"
                "   ROW_NUMBER() OVER (PARTITION BY b ORDER BY e, id) AS r_lo,"
                "   ROW_NUMBER() OVER (PARTITION BY b ORDER BY e DESC, id) AS r_hi,"
                "   ROW_NUMBER() OVER (PARTITION BY b ORDER BY ts, id) AS r_first,"
                "   ROW_NUMBER() OVER (PARTITION BY b ORDER BY ts DESC, id DESC) AS r_last"
                "  FROM (SELECT id, ts, CAST(equity AS REAL) AS e,"
                "        CAST(strftime('%s', substr(ts, 1, 19)) AS INTEGER) / :b AS b"
                "        FROM equity_snapshots WHERE ts < :cut AND ts >= :lo))"
                " WHERE r_lo = 1 OR r_hi = 1 OR r_first = 1 OR r_last = 1)",
                {"cut": iso(older_than), "lo": lo, "b": int(bucket_s)})
            return int(cur.rowcount or 0)

    def ledger_version(self) -> tuple[int, int, int]:
        """Cheap change detector for cached reports: (settlement rows, max settlement id,
        max equity snapshot id)."""
        r = self._one("SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS m FROM settlements")
        return (int(r["n"]) if r else 0, int(r["m"]) if r else 0, self.max_id("equity_snapshots"))

    # -- signals / logs --------------------------------------------------------------

    _SIGNAL_COLS = ("ts", "strategy", "ticker", "title", "side", "action", "count", "limit_price", "fair_value",
                    "expected_edge", "reason", "decision", "decision_reason", "order_id", "group_id", "data")

    def insert_signal(self, **fields: Any) -> int:
        """Record a strategy intent and what happened to it (UI signals feed)."""
        unknown = set(fields) - set(self._SIGNAL_COLS)
        data = dict(fields.pop("data", None) or {})
        for k in unknown:
            data[k] = fields.pop(k)
        row: dict[str, Any] = {
            "ts": _ts(fields.get("ts")) or iso(_now()),
            "strategy": fields.get("strategy") or "", "ticker": fields.get("ticker") or "",
            "title": fields.get("title") or "", "side": fields.get("side"), "action": fields.get("action"),
            "count": fields.get("count"), "limit_price": _s(fields.get("limit_price")),
            "fair_value": fields.get("fair_value"), "expected_edge": _s(fields.get("expected_edge")),
            "reason": fields.get("reason") or "", "decision": fields.get("decision") or "",
            "decision_reason": fields.get("decision_reason") or "", "order_id": fields.get("order_id"),
            "group_id": fields.get("group_id"), "data": _dumps(data) if data else None,
        }
        with self.transaction():
            return int(self._conn.execute(_insert_sql("signals", row), row).lastrowid)

    def list_signals(self, *, limit: int | None = 200, strategy: str | None = None,
                     decision: str | None = None) -> list[dict[str, Any]]:
        where, params = [], []
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
            d["limit_price"] = _d(d["limit_price"])
            d["expected_edge"] = _d(d["expected_edge"])
            d["data"] = _loads(d["data"])
            out.append(d)
        return out

    def insert_log(self, level: str, kind: str, message: str, data: Mapping[str, Any] | None = None,
                   ts: datetime | None = None) -> int:
        row = {"ts": iso(ts or _now()), "level": level, "kind": kind, "message": message,
               "data": _dumps(dict(data)) if data else None}
        with self.transaction():
            return int(self._conn.execute(_insert_sql("logs", row), row).lastrowid)

    def list_logs(self, *, limit: int | None = 200, level: str | None = None,
                  kind: str | None = None) -> list[dict[str, Any]]:
        where, params = [], []
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
        """Delete all but the newest ``keep_last`` rows of logs/signals/equity_snapshots (disk hygiene)."""
        if table not in ("logs", "signals", "equity_snapshots"):
            raise ValueError(f"prune not allowed for {table}")
        with self.transaction():
            cur = self._conn.execute(
                f"DELETE FROM {table} WHERE id <= (SELECT COALESCE(MAX(id), 0) FROM {table}) - ?", (keep_last,))
            return cur.rowcount

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
        """Risk-limit overrides set at runtime (PATCH /api/risk); values as JSON-decoded."""
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

    _BT_JSON = ("params", "metrics", "equity_curve", "trades", "by_month")

    def create_backtest(self, strategy: str, params: Mapping[str, Any] | None = None, *, start: str | None = None,
                        end: str | None = None, starting_balance: Decimal | None = None,
                        status: str = "running") -> int:
        row = {"strategy": strategy, "params": _dumps(dict(params or {})), "start_at": start, "end_at": end,
               "starting_balance": _s(starting_balance), "status": status, "created_at": iso(_now())}
        with self.transaction():
            return int(self._conn.execute(_insert_sql("backtests", row), row).lastrowid)

    def update_backtest(self, backtest_id: int, **fields: Any) -> None:
        allowed = {"status", "error", "finished_at", "metrics", "equity_curve", "trades", "by_month", "params",
                   "start", "end"}
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
             "end": r["end_at"], "starting_balance": _d(r["starting_balance"]), "status": r["status"],
             "error": r["error"], "created_at": parse_iso(r["created_at"]),
             "finished_at": parse_iso(r["finished_at"]), "metrics": _loads(r["metrics"])}
        if full:
            d.update(equity_curve=_loads(r["equity_curve"]) or [], trades=_loads(r["trades"]) or [],
                     by_month=_loads(r["by_month"]) or [])
        return d

    def get_backtest(self, backtest_id: int) -> dict[str, Any] | None:
        r = self._one("SELECT * FROM backtests WHERE id=?", (backtest_id,))
        return self._bt(r, True) if r else None

    def list_backtests(self, *, limit: int | None = 100) -> list[dict[str, Any]]:
        sql = ("SELECT id, strategy, params, start_at, end_at, starting_balance, status, error, created_at, "
               "finished_at, metrics FROM backtests ORDER BY id DESC")
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
        """Wipe the paper account (orders, fills, positions, settlements, equity, signals, account,
        ``broker.*`` kv). Logs, strategy state, risk limits, backtests and other kv keys are kept."""
        with self.transaction():
            for t in PAPER_TABLES:
                self._conn.execute(f"DELETE FROM {t}")
            self._conn.execute("DELETE FROM kv WHERE key LIKE 'broker.%'")
