"""Command line: ``kalshibot serve|reset|backtest|live-check``.

Paper trading unless ``live.enabled`` is true in the config (then ``serve`` sends real orders
to Kalshi; see :mod:`kalshibot.live.broker`).

* ``kalshibot serve`` runs the API + dashboard + engine in one process
  (http://127.0.0.1:8765 by default). The Kalshi engine starts when ``engine.autostart`` is
  true (``--no-engine`` leaves it stopped). Ctrl-C shuts down cleanly (engine stopped, SSE streams
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
* ``kalshibot live-check`` (read-only) checks the live credentials and prints the Kalshi balance,
  open positions and resting orders. It places no orders.

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
                                description="Kalshi trading bot and dashboard (paper unless live.enabled).")
    p.add_argument("-c", "--config", help="config file (default: $KALSHIBOT_CONFIG or ./config.yaml)")
    p.add_argument("--log-level", default=os.environ.get("KALSHIBOT_LOG_LEVEL", "INFO"),
                   help="logging level (default INFO)")
    sub = p.add_subparsers(dest="command", required=True,
                           metavar="{serve,reset,backtest,live-check}")

    s = sub.add_parser("serve", help="run the API, dashboard and trading engine")
    s.add_argument("--host", help="bind address (default: server.host)")
    s.add_argument("--port", type=int, help="port (default: server.port)")
    s.add_argument("--storage", help="SQLite path (default: storage.path)")
    s.add_argument("--no-engine", action="store_true", help="do not start the trading engine automatically")

    sub.add_parser("live-check", help="check the live API key and show the Kalshi balance (read-only)")

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
    # --no-engine: the engine does not start (it can be started from the UI)
    app = create_app(settings, autostart=False if args.no_engine else None)
    mode = f"LIVE TRADING on Kalshi {settings.live.environment}" if settings.live.enabled else "PAPER TRADING"
    log.info("kalshibot (%s) on http://%s:%d  storage=%s", mode, settings.server.host, settings.server.port,
             os.path.abspath(settings.storage.path))
    if settings.live.enabled:
        log.warning("LIVE MODE: strategies place real orders on %s (max %d contracts / $%s per order). "
                    "The engine %s.", settings.kalshi.base_url, settings.live.max_order_contracts,
                    settings.live.max_order_cost,
                    "starts now" if settings.live.autostart and not args.no_engine
                    else "stays stopped until you start it from the dashboard")

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
    if settings.live.enabled:
        print("error: live.enabled is true; the live ledger is reset from the running server "
              "(POST /api/account/reset), which re-reads the Kalshi balance", file=sys.stderr)
        return 2
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


def cmd_live_check(args: argparse.Namespace) -> int:
    """Read-only: sign a few GETs with the live key and print what Kalshi reports."""
    from kalshibot.api.server import build_trader
    from kalshibot.live.secrets import CredentialStore

    settings = _settings(args)
    try:
        trader, source = build_trader(settings, CredentialStore(settings.live.secrets_path))
    except (OSError, ValueError, TypeError) as e:
        print(f"error: cannot load the live credentials: {e}", file=sys.stderr)
        return 2
    if trader.signer is None:
        print(f"error: no Kalshi {settings.live.environment} API key: set live.api_key_id + "
              "live.private_key_path, or add one in the dashboard (Settings -> API keys)", file=sys.stderr)
        return 2

    async def run() -> dict[str, Any]:
        async with trader:
            bal = await trader.get_balance()
            positions = await trader.get_positions()
            resting = await trader.get_orders(status="resting", max_pages=1)
        return {"environment": settings.live.environment, "base_url": trader.base_url, "key_source": source,
                "enabled": settings.live.enabled, "balance": bal, "positions": positions,
                "resting_orders": len(resting)}

    try:
        out = asyncio.run(run())
    except Exception as e:
        print(f"error: Kalshi refused or failed the request: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2, default=str))
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
        if args.command == "live-check":
            return cmd_live_check(args)
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
