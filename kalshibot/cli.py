"""Command line: ``kalshibot serve|reset|backtest|coinbase-reset|coinbase-backtest`` (PAPER TRADING ONLY).

* ``kalshibot serve`` runs the API + dashboard + engine in one process
  (http://127.0.0.1:8765 by default). The Kalshi engine starts when ``engine.autostart`` is
  true, the Coinbase engine when ``coinbase.engine.autostart`` is (``--no-engine`` starts
  neither). Ctrl-C shuts down cleanly (engine stopped, SSE streams
  closed within a few seconds, database closed).
* ``kalshibot reset`` wipes the paper account (orders, fills, positions, settlements,
  equity, signals) and starts over with ``--starting-balance`` (default from config). It
  refuses while a server owns the database (single-writer lock on ``<db>.lock``); use
  ``POST /api/account/reset`` on the running server instead.
* ``kalshibot backtest --strategy NAME [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--param k=v ...]``
  replays the research data through the strategy (``kalshibot.backtest.runner.run_backtest``;
  PAPER only) and prints the metrics as JSON. ``--end`` is inclusive. Runner options:
  ``--fill same|next|next_ask``, ``--data hourly|minute``, ``--book-size``, ``--latency``,
  ``--no-risk``, ``--opt k=v``. ``--save`` stores the result in the database (``storage.path`` or
  ``--storage``) so the dashboard's Backtests page lists it; ``--trades FILE.csv`` writes the
  trade list. The hourly research data needs pyarrow: ``uv run --with pyarrow kalshibot backtest ...``.

* ``kalshibot coinbase-reset [--starting-balance USD] [--clear-kill-switch] [-y]`` wipes the
  separate Coinbase paper account (``coinbase.storage_path``); refuses while a server owns it
  (use ``POST /api/coinbase/account/reset`` instead).
* ``kalshibot coinbase-backtest --strategy NAME [--start] [--end] [--param k=v ...] [--fee-tier]
  [--slippage spread|BPS] [--save]`` replays ``research/coinbase/data`` through a Coinbase spot
  strategy (``kalshibot.coinbase.backtest.run_spot_backtest``; PAPER only).

Config: ``--config PATH``, else ``$KALSHIBOT_CONFIG``, else ``./config.yaml`` (created from
``config.example.yaml`` on the first ``serve``), plus ``KALSHIBOT_<SECTION>__<KEY>`` env
overrides. A relative ``storage.path`` is relative to the config file's directory. Config
errors are reported in one line (exit code 2).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Sequence
from typing import Any

import pydantic
import yaml

from kalshibot.config import CONFIG_ENV, DEFAULT_CONFIG_PATH, Settings, ensure_config_file, load_settings
from kalshibot.store import ProcessLock, StoreLockedError

__all__ = ["ConfigError", "build_parser", "main"]

log = logging.getLogger("kalshibot")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kalshibot",
                                description="Kalshi paper-trading bot and dashboard (PAPER TRADING ONLY).")
    p.add_argument("-c", "--config", help="config file (default: $KALSHIBOT_CONFIG or ./config.yaml)")
    p.add_argument("--log-level", default=os.environ.get("KALSHIBOT_LOG_LEVEL", "INFO"),
                   help="logging level (default INFO)")
    sub = p.add_subparsers(dest="command", required=True,
                           metavar="{serve,reset,backtest,coinbase-reset,coinbase-backtest}")

    s = sub.add_parser("serve", help="run the API, dashboard and trading engine")
    s.add_argument("--host", help="bind address (default: server.host)")
    s.add_argument("--port", type=int, help="port (default: server.port)")
    s.add_argument("--storage", help="SQLite path (default: storage.path)")
    s.add_argument("--no-engine", action="store_true", help="start neither venue's engine (Kalshi nor Coinbase) automatically")

    r = sub.add_parser("reset", help="wipe the paper account and start over")
    r.add_argument("--starting-balance", type=float, help="new starting balance (default: account.starting_balance)")
    r.add_argument("--storage", help="SQLite path (default: storage.path)")
    r.add_argument("--clear-kill-switch", action="store_true", help="also turn the risk kill switch off")
    r.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    b = sub.add_parser("backtest", help="replay research data through a strategy (paper only)")
    b.add_argument("--strategy", required=True, help="strategy name (see GET /api/strategies)")
    b.add_argument("--start", help="first day YYYY-MM-DD, UTC (default: start of the data)")
    b.add_argument("--end", help="last day YYYY-MM-DD, UTC, inclusive (default: end of the data)")
    b.add_argument("--param", action="append", default=[], metavar="K=V",
                   help="strategy parameter (repeatable); JSON values (0.97, true, [..]) else text")
    b.add_argument("--params", default="{}", help="JSON object of strategy parameters (--param wins)")
    b.add_argument("--starting-balance", type=float, help="starting balance (default: account.starting_balance)")
    b.add_argument("--fill", choices=["auto", "same", "next", "next_ask"],
                   help="execution: at the decision quote, --latency later at the limit, or at the ask "
                        "--latency later (auto: hourly=same, minute=next_ask, the research conventions)")
    b.add_argument("--data", choices=["auto", "hourly", "minute"], help="historical data source (default auto)")
    b.add_argument("--book-size", type=int, dest="book_size", help="contracts per synthetic book level (250)")
    b.add_argument("--latency", type=int, dest="latency_s", help="seconds to the fill for --fill next/next_ask (60)")
    b.add_argument("--no-risk", action="store_true", help="skip the risk manager (default: settings.risk)")
    b.add_argument("--opt", action="append", default=[], metavar="K=V",
                   help="other runner option, e.g. snapshot_s=3600, universe=all (see kalshibot.backtest.runner)")
    b.add_argument("--trades", metavar="CSV", help="also write the trade list to this CSV file")
    b.add_argument("--save", action="store_true",
                   help="store the result in the database so the dashboard's Backtests page lists it")
    b.add_argument("--storage", help="SQLite path for --save (default: storage.path)")
    b.add_argument("--json", action="store_true", help="print the whole result (trades, equity curve) as JSON")

    # -- Coinbase spot PAPER venue (docs/COINBASE_CONTRACT.md) -----------------------------
    cr = sub.add_parser("coinbase-reset", help="wipe the Coinbase paper account and start over")
    cr.add_argument("--starting-balance", type=float,
                    help="new starting balance in USD (default: coinbase.starting_balance)")
    cr.add_argument("--storage", dest="cb_storage", help="Coinbase SQLite path (default: coinbase.storage_path)")
    cr.add_argument("--clear-kill-switch", action="store_true", help="also turn the Coinbase kill switch off")
    cr.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    cb = sub.add_parser("coinbase-backtest", help="replay research/coinbase/data through a Coinbase strategy")
    cb.add_argument("--strategy", required=True, help="Coinbase strategy name (see GET /api/coinbase/strategies)")
    cb.add_argument("--start", help="first day YYYY-MM-DD, UTC (default: after the strategy's warm-up)")
    cb.add_argument("--end", help="last day YYYY-MM-DD, UTC, inclusive (default: the last complete bar)")
    cb.add_argument("--param", action="append", default=[], metavar="K=V",
                    help="strategy parameter (repeatable); JSON values (0.97, true, [..]) else text")
    cb.add_argument("--params", default="{}", help="JSON object of strategy parameters (--param wins)")
    cb.add_argument("--starting-balance", type=float, help="starting balance in USD (default 1000)")
    cb.add_argument("--fee-tier", help="fee tier key, e.g. intro, intro_pre_2026_09 (default: coinbase.fee_tier)")
    cb.add_argument("--slippage", help="'spread' (half the product's spread, default) or a number of bps")
    cb.add_argument("--data-dir", help="research data directory (default: research/coinbase/data)")
    cb.add_argument("--opt", action="append", default=[], metavar="K=V",
                    help="other backtester option, e.g. min_trade_usd=10, allocation_pct=100, benchmarks=false")
    cb.add_argument("--trades", metavar="CSV", help="also write the trade list to this CSV file")
    cb.add_argument("--save", action="store_true",
                    help="store the result in the Coinbase database so the dashboard's Coinbase Backtests page lists it")
    cb.add_argument("--storage", dest="cb_storage",
                    help="Coinbase SQLite path for --save (default: coinbase.storage_path)")
    cb.add_argument("--json", action="store_true", help="print the whole result as JSON")
    return p


def _kv_pairs(items: Sequence[str], what: str) -> dict[str, Any]:
    """``["k=v", ...]`` -> dict; values parsed as JSON when they parse, else kept as text."""
    out: dict[str, Any] = {}
    for item in items:
        k, sep, v = item.partition("=")
        if not sep or not k.strip():
            raise ValueError(f"{what} must look like key=value, got {item!r}")
        try:
            out[k.strip()] = json.loads(v)
        except ValueError:
            out[k.strip()] = v
    return out


class ConfigError(Exception):
    """The config file / KALSHIBOT_* environment is invalid (reported in one line)."""


def _settings(args: argparse.Namespace, *, create: bool = False) -> Settings:
    path = args.config
    if path is None and create and not os.environ.get(CONFIG_ENV) and not DEFAULT_CONFIG_PATH.exists():
        created = ensure_config_file()
        if created.exists():
            log.info("created %s from config.example.yaml", created)
    where = path or os.environ.get(CONFIG_ENV) or (str(DEFAULT_CONFIG_PATH) if DEFAULT_CONFIG_PATH.exists() else "")
    where = f" ({where} + KALSHIBOT_* env)" if where else " (KALSHIBOT_* env)"
    try:
        settings = load_settings(path)
    except pydantic.ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg')}"
                             for err in e.errors())
        raise ConfigError(f"invalid config{where}: {problems}") from None
    except (ValueError, yaml.YAMLError) as e:
        msg = " ".join(str(e).split())
        raise ConfigError(f"invalid config{where}: {msg}") from None
    storage = getattr(args, "storage", None)
    if storage:
        settings.storage.path = storage
    return settings


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from kalshibot.api.server import create_app

    settings = _settings(args, create=True)
    if args.host:
        settings.server.host = args.host
    if args.port:
        settings.server.port = args.port
    # fail fast (before uvicorn starts) if another process owns this paper account; the
    # server's Store re-acquires the lock and holds it for its whole lifetime
    if settings.storage.path != ":memory:" and not settings.storage.path.startswith("file:"):
        ProcessLock(settings.storage.path).acquire().release()
    # --no-engine: neither venue's engine starts (each can be started from the UI)
    app = create_app(settings, autostart=False if args.no_engine else None,
                     coinbase_autostart=False if args.no_engine else None)
    log.info("kalshibot (PAPER TRADING) on http://%s:%d  storage=%s", settings.server.host, settings.server.port,
             os.path.abspath(settings.storage.path))

    class Server(uvicorn.Server):
        def handle_exit(self, sig: int, frame: Any) -> None:
            app.state.stopping = True  # lets open SSE streams finish so shutdown is quick
            super().handle_exit(sig, frame)

    config = uvicorn.Config(app, host=settings.server.host, port=settings.server.port,
                            log_level=args.log_level.lower(), timeout_graceful_shutdown=5)
    Server(config).run()
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    from kalshibot.paper.broker import PaperBroker
    from kalshibot.paper.sim import StaticMarketData
    from kalshibot.risk import RiskManager
    from kalshibot.store import Store

    settings = _settings(args)
    start = args.starting_balance if args.starting_balance is not None else settings.account.starting_balance
    if not args.yes:
        ans = input(f"Wipe the paper account in {os.path.abspath(settings.storage.path)} and restart at ${start}? "
                    "[y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    store = Store(settings.storage.path, exclusive=True)  # refuses while `serve` owns the database
    try:
        broker = PaperBroker(StaticMarketData(), store, settings=settings)  # no network needed for a reset
        acct = asyncio.run(broker.reset(start))
        if args.clear_kill_switch:
            RiskManager(settings, store=store).set_kill_switch(False, "reset via CLI")
    finally:
        store.close()
    print(json.dumps(acct.to_json(), indent=2))
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from kalshibot.api.server import _call_kwargs, normalize_backtest_result, resolve_backtest_runner
    from kalshibot.strategies import REGISTRY, ParamError

    fn, why = resolve_backtest_runner()
    if fn is None:
        print(f"backtests are unavailable: {why}", file=sys.stderr)
        return 2
    settings = _settings(args)
    cls = REGISTRY.get(args.strategy)
    if cls is None:
        print(f"unknown strategy {args.strategy!r}; known: {', '.join(sorted(REGISTRY)) or '(none)'}",
              file=sys.stderr)
        return 2
    try:
        raw: Any = json.loads(args.params)
        if not isinstance(raw, dict):
            raise ValueError("--params must be a JSON object")
        raw.update(_kv_pairs(args.param, "--param"))
        params = cls.resolve_params(raw, strict=True)
    except (ValueError, ParamError) as e:
        print(f"invalid --params/--param: {e}", file=sys.stderr)
        return 2
    try:
        opts = _kv_pairs(args.opt, "--opt")
    except ValueError as e:
        print(f"invalid --opt: {e}", file=sys.stderr)
        return 2
    for k in ("fill", "data", "book_size", "latency_s"):
        if getattr(args, k, None) is not None:
            opts[k] = getattr(args, k)
    if args.no_risk:
        opts["risk"] = False
    start_bal = args.starting_balance if args.starting_balance is not None else float(
        settings.account.starting_balance)
    kwargs = _call_kwargs(fn, {"strategy": args.strategy, "strategy_cls": cls, "params": params,
                               "start": args.start, "end": args.end, "starting_balance": start_bal,
                               "settings": settings, **opts})
    try:
        res = fn(**kwargs)
        if asyncio.iscoroutine(res):
            res = asyncio.run(res)
    except (ValueError, RuntimeError) as e:  # bad option/period, missing data (BacktestDataError)
        print(f"error: {e}", file=sys.stderr)
        return 2
    out = normalize_backtest_result(res)
    if args.trades:
        _write_trades_csv(args.trades, getattr(res, "trades", None) or out["trades"])
    saved = None
    if args.save:
        saved = _save_backtest(settings, args.strategy, params, args.start, args.end, start_bal, out)
    if args.json:
        print(json.dumps({**out, "id": saved}, indent=2))
    else:
        summary: dict[str, Any] = {"metrics": out["metrics"], "by_month": out["by_month"],
                                   "trades": len(out["trades"])}
        if saved is not None:
            summary["id"] = saved
        print(json.dumps(summary, indent=2))
    return 0


def _write_trades_csv(path: str, trades: Sequence[Any]) -> None:
    import csv

    rows = [dict(t) for t in trades]
    cols: list[str] = []
    for r in rows:
        cols.extend(k for k in r if k not in cols)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols or ["ticker"])
        w.writeheader()
        for r in rows:
            w.writerow({k: (json.dumps(v) if isinstance(v, list | dict) else v) for k, v in r.items()})


def _save_backtest(settings: Settings, strategy: str, params: Any, start: str | None, end: str | None,
                   starting_balance: float, out: dict[str, Any]) -> int:
    """Store a finished run in the backtests table (a plain, non-exclusive writer: the running
    server's lock guards its paper ledger, which this does not touch)."""
    from datetime import UTC, datetime

    from kalshibot.engine import jsonable
    from kalshibot.money import D
    from kalshibot.store import Store

    store = Store(settings.storage.path)
    try:
        bt_id = store.create_backtest(strategy, jsonable(params), start=start, end=end,
                                      starting_balance=D(str(starting_balance)), status="done")
        store.update_backtest(bt_id, finished_at=datetime.now(UTC), **out)
    finally:
        store.close()
    return bt_id


def cmd_coinbase_reset(args: argparse.Namespace) -> int:
    """Wipe the Coinbase paper account (refuses while a server owns its database)."""
    from decimal import Decimal

    from kalshibot.coinbase.broker import SpotPaperBroker
    from kalshibot.coinbase.risk import SpotRiskManager
    from kalshibot.coinbase.store import SpotStore

    settings = _settings(args)
    cb = settings.coinbase
    if getattr(cb, "load_error", None):
        raise ConfigError(str(cb.load_error))
    path = args.cb_storage or cb.storage_path
    start = args.starting_balance if args.starting_balance is not None else float(cb.starting_balance)
    if start <= 0:
        print("error: --starting-balance must be positive", file=sys.stderr)
        return 2
    if not args.yes:
        ans = input(f"Wipe the Coinbase paper account in {os.path.abspath(path)} and restart at ${start}? [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    store = SpotStore(path, exclusive=True)  # refuses while `serve` owns the database
    try:
        broker = SpotPaperBroker(None, store, settings=cb)  # no market data needed for a reset
        acct = broker.reset(Decimal(str(start)))
        if args.clear_kill_switch:
            SpotRiskManager(cb, store=store).set_kill_switch(False, "reset via CLI")
    finally:
        store.close()
    print(json.dumps(acct.to_json(), indent=2))
    return 0


def cmd_coinbase_backtest(args: argparse.Namespace) -> int:
    """Replay the research candles through a Coinbase strategy and print the metrics."""
    from kalshibot.coinbase.strategies import REGISTRY, ParamError

    settings = _settings(args)
    cls = REGISTRY.get(args.strategy)
    if cls is None:
        print(f"unknown coinbase strategy {args.strategy!r}; known: {', '.join(sorted(REGISTRY)) or '(none)'}",
              file=sys.stderr)
        return 2
    try:
        raw: Any = json.loads(args.params)
        if not isinstance(raw, dict):
            raise ValueError("--params must be a JSON object")
        raw.update(_kv_pairs(args.param, "--param"))
        params = cls.resolve_params(raw, strict=True)
        opts = _kv_pairs(args.opt, "--opt")
    except (ValueError, ParamError) as e:
        print(f"invalid --params/--param/--opt: {e}", file=sys.stderr)
        return 2
    slippage: Any = "spread"
    if args.slippage and args.slippage != "spread":
        try:
            slippage = float(args.slippage)
        except ValueError:
            print("--slippage must be 'spread' or a number of bps", file=sys.stderr)
            return 2
    from kalshibot.coinbase.backtest import run_spot_backtest

    start_bal = args.starting_balance if args.starting_balance is not None else 1000.0
    try:
        res = run_spot_backtest(cls, params, start=args.start, end=args.end, starting_balance=start_bal,
                                fee_tier=args.fee_tier, slippage=slippage, data_dir=args.data_dir,
                                settings=settings, **opts)
    except (ValueError, RuntimeError, KeyError) as e:  # bad request, missing data (SpotBacktestError)
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.trades:
        _write_trades_csv(args.trades, res.get("trades") or [])
    saved = None
    if args.save:
        saved = _save_coinbase_backtest(settings, args, args.strategy, params, start_bal, res)
    if args.json:
        print(json.dumps({**res, "id": saved}, indent=2, default=str))
    else:
        metrics = {k: v for k, v in (res.get("metrics") or {}).items() if k != "details"}
        summary: dict[str, Any] = {"venue": "coinbase", "strategy": args.strategy, "start": res.get("start"),
                                   "end": res.get("end"), "metrics": metrics, "by_year": res.get("by_year"),
                                   "trades": len(res.get("trades") or [])}
        if saved is not None:
            summary["id"] = saved
        print(json.dumps(summary, indent=2, default=str))
    return 0


def _save_coinbase_backtest(settings: Settings, args: argparse.Namespace, strategy: str, params: Any,
                            starting_balance: float, res: dict[str, Any]) -> int:
    """Store a finished run in the Coinbase backtests table (non-exclusive writer: the running
    server's lock guards its paper ledger, which this does not touch)."""
    from datetime import UTC, datetime
    from decimal import Decimal

    from kalshibot.coinbase.api import _store_result
    from kalshibot.coinbase.store import SpotStore
    from kalshibot.engine import jsonable

    tier = ((res.get("metrics") or {}).get("details") or {}).get("fee_tier")
    tier_name = tier.get("name") if isinstance(tier, dict) else (args.fee_tier or None)
    store = SpotStore(args.cb_storage or settings.coinbase.storage_path)
    try:
        bt_id = store.create_backtest(strategy, jsonable(params), start=args.start, end=args.end,
                                      starting_balance=Decimal(str(starting_balance)), fee_tier=tier_name,
                                      status="done")
        store.update_backtest(bt_id, finished_at=datetime.now(UTC), **_store_result(res))
    finally:
        store.close()
    return bt_id


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.log_level)
    try:
        if args.command == "serve":
            return cmd_serve(args)
        if args.command == "reset":
            return cmd_reset(args)
        if args.command == "backtest":
            return cmd_backtest(args)
        if args.command == "coinbase-reset":
            return cmd_coinbase_reset(args)
        if args.command == "coinbase-backtest":
            return cmd_coinbase_backtest(args)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except StoreLockedError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
