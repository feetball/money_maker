"""Regression tests for the operations review of the Coinbase venue: shutdown budget, strategy
threads that outlive their timeout, ``serve --no-engine``, the overview's bar/tick fields,
no-trade candle buckets and provisional final bars. Fakes only - no network. PAPER ONLY.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

from test_cb_api import make_app
from test_cb_engine import (
    BAR_END,
    NOW,
    PID,
    PID2,
    HoldStrategy,
    candle,
    make_services,
    standard_client,
)

from kalshibot.coinbase.marketdata import SpotMarketData
from kalshibot.coinbase.strategies.base import TargetWeight

# --------------------------------------------------------------------------- candles


class TwoProducts(HoldStrategy):
    name = "t_two"
    default_params: ClassVar[dict[str, Any]] = {"weight": 0.5, "products": [PID, PID2]}

    def universe(self, products: Any) -> list[str]:
        return [PID, PID2]

    def on_bar(self, ctx: Any) -> list[TargetWeight] | None:
        return [TargetWeight(PID, 0.5, "x")]


class OnlyAlt(HoldStrategy):
    name = "t_alt"
    default_params: ClassVar[dict[str, Any]] = {"weight": 0.5, "products": [PID2]}

    def universe(self, products: Any) -> list[str]:
        return [PID2]

    def on_bar(self, ctx: Any) -> list[TargetWeight] | None:
        return None


async def test_a_no_trade_bucket_does_not_hold_up_the_bar(tmp_path: Path) -> None:
    fc = standard_client()
    fc.add_product(PID2)
    fc.add_hourly(PID2, until=BAR_END - timedelta(hours=1), n=10)  # no trades in the final hour
    cb, _ = await make_services(tmp_path, strategies=[TwoProducts], client=fc)
    rt = cb.engine.runtimes["t_two"]
    now = BAR_END + timedelta(seconds=cb.engine.bar_delay_s)
    # TST-USD has the final bar: the bar is published, ALT-USD simply did not trade
    assert await cb.engine.run_bar(rt, BAR_END, now=now) is True
    assert sum(1 for c in fc.calls if c[0] == "get_candles" and c[1] == PID2) == 1
    await cb.aclose()


async def test_a_reference_series_tells_unpublished_from_no_trades(tmp_path: Path) -> None:
    fc = standard_client()
    fc.add_product(PID2)
    fc.add_product("BTC-USD")
    fc.add_hourly(PID2, until=BAR_END - timedelta(hours=1), n=10)
    fc.add_hourly("BTC-USD", until=BAR_END - timedelta(hours=1), n=10)  # BTC not published either
    cb, _ = await make_services(tmp_path, strategies=[OnlyAlt], client=fc)
    rt = cb.engine.runtimes["t_alt"]
    now = BAR_END + timedelta(seconds=cb.engine.bar_delay_s)
    assert await cb.engine.run_bar(rt, BAR_END, now=now) is False  # not published yet: wait
    fc.add_hourly("BTC-USD", until=BAR_END, n=10)  # BTC's final bar is out: ALT had no trades
    assert await cb.engine.run_bar(rt, BAR_END, now=now + timedelta(seconds=20)) is True
    await cb.aclose()


async def test_a_provisional_final_bar_is_corrected_later() -> None:
    fc = standard_client()
    md = SpotMarketData(fc, clock=fc.clock, parse_in_thread=False)
    fc.clock.now = BAR_END + timedelta(seconds=30)
    await md.candles(PID, 3600, 3, bar_end=BAR_END)  # the 11:00 bucket may still be aggregating
    bars = fc.candles[(PID, 3600)]
    bars[-1] = candle(PID, bars[-1].start, 105)  # Coinbase finalizes it
    # another caller at the same bar, a little later, gets the corrected bar
    fc.clock.now = BAR_END + timedelta(seconds=60)
    out = await md.candles(PID, 3600, 3, bar_end=BAR_END)
    assert out[-1].close == Decimal(105)
    # and the next bar's call overlaps one bar, so a late correction is picked up too
    bars[-1] = candle(PID, bars[-1].start, 106)
    fc.candles[(PID, 3600)].append(candle(PID, BAR_END, 110))
    fc.clock.now = BAR_END + timedelta(hours=1, seconds=30)
    out = await md.candles(PID, 3600, 3, bar_end=BAR_END + timedelta(hours=1))
    assert [(c.start.hour, c.close) for c in out][-2:] == [(11, Decimal(106)), (12, Decimal(110))]
    # a bar fetched well after its end is final: no more requests for the same bar_end
    n = fc.count("get_candles")
    fc.clock.now = BAR_END + timedelta(hours=1, seconds=600)
    await md.candles(PID, 3600, 3, bar_end=BAR_END + timedelta(hours=1))
    await md.candles(PID, 3600, 3, bar_end=BAR_END + timedelta(hours=1))
    assert fc.count("get_candles") <= n + 1


# --------------------------------------------------------------------------- strategy threads


class Hang(HoldStrategy):
    name = "t_hang"
    release: ClassVar[threading.Event] = threading.Event()
    calls: ClassVar[list[str]] = []

    def on_bar(self, ctx: Any) -> list[TargetWeight] | None:
        Hang.calls.append(threading.current_thread().name)
        Hang.release.wait(10)
        return None


async def test_a_hung_on_bar_uses_no_shared_executor_and_is_not_restarted(tmp_path: Path) -> None:
    Hang.release.clear()
    Hang.calls.clear()
    cb, fc = await make_services(tmp_path, strategies=[Hang])
    eng = cb.engine
    eng.on_bar_timeout_s = 0.05
    rt = eng.runtimes["t_hang"]
    loop = asyncio.get_running_loop()
    used_default: list[Any] = []
    orig = loop.run_in_executor

    def spy(executor: Any, fn: Any, *args: Any) -> Any:
        if executor is None:
            used_default.append(fn)
        return orig(executor, fn, *args)

    loop.run_in_executor = spy  # type: ignore[method-assign]
    try:
        assert await eng.run_bar(rt, BAR_END, now=NOW) is True
        assert "timed out" in (rt.last_error or "")
        stuck = [t for t in threading.enumerate() if t.name == Hang.calls[0]]
        assert stuck and stuck[0].daemon  # never holds up interpreter exit
        # the next bar: the previous call is still running -> skipped, no second thread
        fc.add_hourly(PID, until=BAR_END + timedelta(hours=1))
        assert await eng.run_bar(rt, BAR_END + timedelta(hours=1), now=NOW + timedelta(hours=1)) is True
        assert len(Hang.calls) == 1 and "still running" in (rt.last_error or "")
        Hang.release.set()
        for _ in range(100):
            if not stuck[0].is_alive():
                break
            await asyncio.sleep(0.01)
        fc.add_hourly(PID, until=BAR_END + timedelta(hours=2))
        assert await eng.run_bar(rt, BAR_END + timedelta(hours=2), now=NOW + timedelta(hours=2)) is True
        assert len(Hang.calls) == 2
    finally:
        loop.run_in_executor = orig  # type: ignore[method-assign]
        Hang.release.set()
    assert not [f for f in used_default if "on_bar" in repr(f)]
    await cb.aclose()


# --------------------------------------------------------------------------- shutdown


async def test_services_close_is_bounded_when_a_job_hangs(tmp_path: Path) -> None:
    cb, fc = await make_services(tmp_path)

    async def hang(*a: Any, **k: Any) -> Any:
        await asyncio.sleep(3600)

    fc.get_products = hang  # type: ignore[method-assign]
    await cb.engine.start()
    await asyncio.sleep(0.05)
    t0 = time.monotonic()
    await cb.aclose(timeout=0.3)
    assert time.monotonic() - t0 < 2.0


def test_kalshi_shutdown_does_not_wait_for_coinbase(tmp_path: Path) -> None:
    events: dict[str, float] = {}

    class SlowCb:
        autostart = False
        engine = None

        def bind(self, loop: Any) -> None:
            pass

        async def aclose(self, **kw: Any) -> None:
            events["cb_start"] = time.monotonic()
            await asyncio.sleep(1.0)
            events["cb_end"] = time.monotonic()

    client, svc = make_app(tmp_path, SlowCb())  # type: ignore[arg-type]
    orig = svc.aclose

    async def kalshi_close() -> None:
        events["kalshi_start"] = time.monotonic()
        await orig()

    svc.aclose = kalshi_close  # type: ignore[method-assign]
    with client:
        pass
    assert events["kalshi_start"] < events["cb_end"]  # the venues close side by side


# --------------------------------------------------------------------------- serve --no-engine


def test_serve_no_engine_starts_neither_engine(monkeypatch: Any, tmp_path: Path) -> None:
    import uvicorn

    from kalshibot import cli
    from kalshibot.api import server

    seen: dict[str, Any] = {}

    def fake_create_app(settings: Any, **kw: Any) -> Any:
        seen.update(kw)
        return type("App", (), {"state": type("S", (), {})()})()

    class FakeServer:
        def __init__(self, config: Any) -> None:
            pass

        def run(self) -> None:
            pass

    monkeypatch.setattr(server, "create_app", fake_create_app)
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(uvicorn, "Config", lambda *a, **k: None)
    monkeypatch.setenv("KALSHIBOT_STORAGE__PATH", str(tmp_path / "k.sqlite3"))
    monkeypatch.setenv("KALSHIBOT_COINBASE__STORAGE_PATH", str(tmp_path / "cb.sqlite3"))
    cli.main(["serve", "--no-engine", "--port", "8779"])
    assert seen["autostart"] is False and seen["coinbase_autostart"] is False
    seen.clear()
    cli.main(["serve", "--port", "8779"])
    assert seen["autostart"] is None and seen["coinbase_autostart"] is None


# --------------------------------------------------------------------------- overview


async def test_overview_reports_bar_and_tick_separately(tmp_path: Path) -> None:
    from kalshibot.api.overview import coinbase_part

    cb, _ = await make_services(tmp_path)
    await cb.engine.run_due_bars(now=NOW)
    await cb.engine._job_snapshot()
    block, _ = coinbase_part(cb, None, None, 100)
    st = cb.engine.status()
    assert block["last_bar_at"] == st["last_bar_at"] and block["last_bar_at"] is not None
    assert block["last_tick_at"] == st["last_tick_at"]
    assert "coinbase_reachable" in block
    blank, _ = coinbase_part(None, "down", None, 100)
    assert blank["last_bar_at"] is None and blank["coinbase_reachable"] is None
    await cb.aclose()
