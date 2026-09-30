"""Wiring of the Coinbase spot PAPER venue (docs/COINBASE_CONTRACT.md §10, §13).

``await build_coinbase_services(settings)`` builds the venue's whole stack - public client,
market data, SQLite store (its own file and single-writer lock), paper broker, risk manager,
event bus and engine - **without any network I/O** (the engine loads products once it
runs), so the server's startup never waits on Coinbase. It raises
:class:`CoinbaseUnavailable` when the venue is disabled (``coinbase.enabled: false``) or its
config was rejected (``coinbase.load_error``); the server then keeps ``app.state.cb = None``
and answers every ``/api/coinbase/*`` route with 503 while Kalshi runs normally.

PAPER TRADING ONLY: nothing here can place a real order or use credentials.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kalshibot.engine import EventBus

__all__ = ["CoinbaseServices", "CoinbaseUnavailable", "build_coinbase_services", "coinbase_settings"]

log = logging.getLogger(__name__)


class CoinbaseUnavailable(RuntimeError):
    """The Coinbase venue is not running (disabled, bad config, store locked, ...)."""


def coinbase_settings(settings: Any) -> Any:
    """``settings.coinbase`` (a full ``Settings``) or ``settings`` itself (a ``CoinbaseSettings``)."""
    return getattr(settings, "coinbase", None) or settings


@dataclass
class CoinbaseServices:
    """Everything the ``/api/coinbase/*`` routes need (built here, or injected by tests)."""

    settings: Any  # the full Settings (or a CoinbaseSettings)
    cb: Any  # CoinbaseSettings
    store: Any
    client: Any
    md: Any
    broker: Any
    risk: Any
    engine: Any
    bus: EventBus
    owns_client: bool = True
    owns_store: bool = True
    backtests: dict[int, asyncio.Future[Any]] = field(default_factory=dict)
    cache: dict[str, Any] = field(default_factory=dict)
    closed: bool = False

    @property
    def autostart(self) -> bool:
        return bool(getattr(getattr(self.cb, "engine", None), "autostart", True))

    def bind(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.bus.bind(loop or asyncio.get_running_loop())

    async def aclose(self, timeout: float = 5.0) -> None:
        """Stop the engine and release the client and the store (idempotent, never raises).

        Bounded: the engine loop gets ``timeout`` seconds and background jobs at most as long
        again before they are cancelled (every Coinbase job is cancellable; a job stuck in
        client retries during an outage must not eat the Kalshi venue's shutdown budget)."""
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.engine.close(timeout=timeout), 2 * timeout + 2)
        for t in list(self.backtests.values()):
            with contextlib.suppress(Exception):
                t.cancel()
        task = getattr(self.md, "_quote_task", None)
        if task is not None:
            with contextlib.suppress(Exception):
                task.cancel()
        if self.owns_client:
            with contextlib.suppress(Exception):
                await self.client.aclose()
        if self.owns_store:
            with contextlib.suppress(Exception):
                self.store.close()


def _fail_interrupted_backtests(store: Any) -> None:
    """Backtests left ``running`` by a previous process can never finish: mark them failed."""
    from datetime import UTC

    try:
        for r in store.list_backtests(limit=None):
            if r.get("status") in ("running", "queued"):
                store.update_backtest(r["id"], status="failed", error="interrupted (server restarted)",
                                      finished_at=datetime.now(UTC))
    except Exception:
        log.exception("coinbase: marking interrupted backtests failed")


async def build_coinbase_services(
    settings: Any,
    *,
    client: Any = None,
    store: Any = None,
    strategies: Any = None,
    clock: Callable[[], datetime] | None = None,
    md: Any = None,
) -> CoinbaseServices:
    """Build the Coinbase venue (see the module docstring). Raises :class:`CoinbaseUnavailable`
    when disabled or misconfigured; any other exception (store locked, import error) is also
    the caller's signal that the venue is unavailable."""
    cb = coinbase_settings(settings)
    if cb is None:
        raise CoinbaseUnavailable("no coinbase settings")
    err = getattr(cb, "load_error", None)
    if err:
        raise CoinbaseUnavailable(str(err))
    if not bool(getattr(cb, "enabled", True)):
        raise CoinbaseUnavailable("disabled in config (coinbase.enabled: false)")

    from kalshibot.coinbase.broker import SpotPaperBroker
    from kalshibot.coinbase.client import CoinbaseClient
    from kalshibot.coinbase.engine import CoinbaseEngine
    from kalshibot.coinbase.marketdata import SpotMarketData
    from kalshibot.coinbase.risk import SpotRiskManager
    from kalshibot.coinbase.store import SpotStore

    own_store = store is None
    own_client = client is None
    if own_store:
        # separate file + single-writer lock; a second server on the same file fails here
        store = SpotStore(cb.storage_path, exclusive=True)
    try:
        _fail_interrupted_backtests(store)
        if own_client:
            client = CoinbaseClient.from_settings(cb)
        md = md or SpotMarketData(client, cb, clock=clock)
        broker = SpotPaperBroker(md, store, settings=cb, clock=clock)
        risk = SpotRiskManager(cb, store=store, clock=clock, fee_tier=broker.tier)
        bus = EventBus()
        engine = CoinbaseEngine(settings, md, broker, risk, store, strategies, bus=bus, clock=clock)
    except BaseException:
        if own_client and client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()
        if own_store:
            with contextlib.suppress(Exception):
                store.close()
        raise
    return CoinbaseServices(settings=settings, cb=cb, store=store, client=client, md=md, broker=broker, risk=risk,
                            engine=engine, bus=bus, owns_client=own_client, owns_store=own_store)
