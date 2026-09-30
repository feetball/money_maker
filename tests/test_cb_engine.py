"""Coinbase engine + market data (contract §10): bars, rebalancing, execution, kill switch,
maker-then-taker, outages, persistence. Fakes only - no network. PAPER ONLY.

``FakeCoinbaseClient`` / ``make_services`` / the test strategies are also used by
test_cb_api.py, test_cb_overview.py and test_cb_isolation.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest
from test_cb_broker_fakes import Clock, make_book, make_product, make_trade

from kalshibot.coinbase.client import CoinbaseAPIError
from kalshibot.coinbase.config import CoinbaseSettings
from kalshibot.coinbase.engine import CoinbaseEngine, LiveSpotContext
from kalshibot.coinbase.marketdata import SpotMarketData, parse_book, trim_book
from kalshibot.coinbase.models import Candle, OrderBook, Product, Stats, Trade
from kalshibot.coinbase.services import (
    CoinbaseServices,
    CoinbaseUnavailable,
    build_coinbase_services,
)
from kalshibot.coinbase.strategies.base import ParamError, SpotStrategy, TargetWeight
from kalshibot.config import Settings

PID = "TST-USD"
PID2 = "ALT-USD"
#: 12:01 UTC: the 11:00-12:00 hourly bar closed 60 s ago (>= bar_delay_s = 60)
NOW = datetime(2026, 9, 27, 12, 1, 0, tzinfo=UTC)
BAR_END = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- fakes


def candle(pid: str, start: datetime, close: Any, g: int = 3600) -> Candle:
    c = Decimal(str(close))
    return Candle(product_id=pid, start=start, granularity_s=g, open=c, high=c, low=c, close=c, volume=Decimal(5))


class FakeCoinbaseClient:
    """In-memory stand-in for ``CoinbaseClient`` (same method names and shapes)."""

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock = clock or Clock(NOW)
        self.products: dict[str, Product] = {}
        self.books: dict[str, OrderBook] = {}
        self.trades: dict[str, list[Trade]] = {}
        self.candles: dict[tuple[str, int], list[Candle]] = {}
        self.stats: dict[str, dict[str, Any]] = {}
        self.down = False  # every call raises a network error
        self.calls: list[tuple[Any, ...]] = []
        self.request_count = 0
        self.closed = False

    def _hit(self, *call: Any) -> None:
        self.calls.append(call)
        self.request_count += 1
        if self.down:
            raise CoinbaseAPIError(None, "ConnectError: network unreachable", str(call[0]))

    def count(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)

    # -- setup helpers
    def add_product(self, pid: str = PID, *, bid: Any = "99.99", ask: Any = "100.00", size: Any = 1000,
                    last: Any = "100", open_: Any = "95", volume: Any = 1000, **kw: Any) -> None:
        self.products[pid] = make_product(pid, **kw)
        self.books[pid] = make_book(pid, bids=[(bid, size), (Decimal(str(bid)) - 1, size)],
                                    asks=[(ask, size), (Decimal(str(ask)) + 1, size)])
        self.stats[pid] = {"stats_24hour": {"open": str(open_), "high": str(last), "low": str(open_),
                                            "last": str(last), "volume": str(volume)},
                           "stats_30day": {"volume": str(volume * 30)}}

    def add_hourly(self, pid: str = PID, *, until: datetime = BAR_END, n: int = 10, close: Any = 100) -> None:
        bars = [candle(pid, until - timedelta(hours=i + 1), close) for i in range(n)]
        self.candles[(pid, 3600)] = sorted(bars, key=lambda c: c.start)

    # -- client API
    async def get_products(self) -> list[Product]:
        self._hit("get_products")
        return list(self.products.values())

    async def get_product(self, pid: str) -> Product:
        self._hit("get_product", pid)
        return self.products[pid]

    async def get_book(self, pid: str, level: int = 2) -> OrderBook:
        self._hit("get_book", pid, level)
        b = self.books[pid]
        return OrderBook(product_id=pid, bids=b.bids, asks=b.asks, time=self.clock.now)

    async def get_stats(self, pid: str) -> Stats:
        self._hit("get_stats", pid)
        d = self.stats[pid]
        return Stats.from_api(pid, {**d["stats_24hour"], "volume_30day": d["stats_30day"]["volume"]})

    async def get(self, path: str, params: Any = None) -> Any:
        self._hit("get", path)
        if path == "/products/stats":
            return dict(self.stats)
        raise CoinbaseAPIError(404, "NotFound", path)

    async def trades_since(self, pid: str, since: int | None, max_pages: int = 5) -> list[Trade]:
        self._hit("trades_since", pid, since)
        tape = sorted(self.trades.get(pid, []), key=lambda t: t.trade_id)
        if since is None:
            return tape[-100:]
        return [t for t in tape if t.trade_id > since]

    async def get_candles(self, pid: str, g: int, start: datetime, end: datetime, *,
                          closed_only: bool = False) -> list[Candle]:
        self._hit("get_candles", pid, g, start, end)
        return [c for c in self.candles.get((pid, g), []) if start <= c.start <= end]

    async def aclose(self) -> None:
        self.closed = True


class HoldStrategy(SpotStrategy):
    """Hourly: hold ``weight`` of TST-USD (config param)."""

    name = "t_hold"
    description = "test: hold a fixed weight"
    bar_granularity_s = 3600
    history_bars = 3
    enabled_by_default = True
    default_params: ClassVar[dict[str, Any]] = {"weight": 1.0, "products": [PID]}
    param_schema: ClassVar[dict[str, dict[str, Any]]] = {
        "weight": {"type": "float", "min": 0, "max": 1}, "products": {"type": "list"}}
    seen: ClassVar[list[Any]] = []

    def on_bar(self, ctx: Any) -> list[TargetWeight] | None:
        HoldStrategy.seen.append((ctx.bar_end, [c.close for c in ctx.candles(PID, 10)], ctx.stats(PID)))
        ctx.log("deciding", weight=self.params["weight"])
        return [TargetWeight(PID, float(self.params["weight"]), "hold test", expected_edge_bps=12.0)]


class BoomStrategy(SpotStrategy):
    name = "t_boom"
    description = "test: always raises"
    bar_granularity_s = 3600
    history_bars = 2
    enabled_by_default = True
    default_params: ClassVar[dict[str, Any]] = {"products": [PID]}
    param_schema: ClassVar[dict[str, dict[str, Any]]] = {"products": {"type": "list"}}

    def on_bar(self, ctx: Any) -> list[TargetWeight] | None:
        raise RuntimeError("strategy bug")


class MakerStrategy(HoldStrategy):
    name = "t_maker"
    execution = "maker_then_taker"
    default_params: ClassVar[dict[str, Any]] = {"weight": 0.5, "products": [PID]}


def cb_settings(tmp_path: Path | None = None, **engine: Any) -> CoinbaseSettings:
    cb = CoinbaseSettings()
    if tmp_path is not None:
        cb.storage_path = str(tmp_path / "cb.sqlite3")
    cb.engine.autostart = False
    for k, v in engine.items():
        setattr(cb.engine, k, v)
    return cb


def full_settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.storage.path = str(tmp_path / "kalshi.sqlite3")
    s.engine.autostart = False
    s.coinbase.storage_path = str(tmp_path / "cb.sqlite3")
    s.coinbase.engine.autostart = False
    return s


def standard_client(clock: Clock | None = None) -> FakeCoinbaseClient:
    fc = FakeCoinbaseClient(clock)
    fc.add_product(PID)
    fc.add_hourly(PID)
    return fc


async def make_services(tmp_path: Path, *, strategies: Iterable[type[SpotStrategy]] | None = None,
                        client: FakeCoinbaseClient | None = None, settings: Any = None,
                        load_products: bool = True) -> tuple[CoinbaseServices, FakeCoinbaseClient]:
    fc = client or standard_client()
    settings = settings if settings is not None else cb_settings(tmp_path)
    strats = {c.name: c for c in (strategies if strategies is not None else [HoldStrategy])}
    md = SpotMarketData(fc, settings, clock=fc.clock, parse_in_thread=False)
    cb = await build_coinbase_services(settings, client=fc, strategies=strats, clock=fc.clock, md=md)
    cb.owns_client = False
    if load_products:
        await cb.md.refresh_products()
    return cb, fc


@pytest.fixture(autouse=True)
def _reset_seen() -> None:
    HoldStrategy.seen.clear()


# --------------------------------------------------------------------------- market data


async def test_book_cache_respects_max_age_and_trims() -> None:
    fc = standard_client()
    md = SpotMarketData(fc, clock=fc.clock, parse_in_thread=False)
    b1 = await md.book(PID, max_age_s=5)
    b2 = await md.book(PID, max_age_s=5)
    assert b1 is b2 and fc.count("get_book") == 1
    await md.book(PID, max_age_s=0)  # 0 = fetch now
    assert fc.count("get_book") == 2
    assert md.quote(PID)[1] == Decimal("99.99") and md.known_book(PID) is not None
    # trimming keeps min_levels and everything within the band
    wide = make_book(PID, bids=[(100 - i, 1) for i in range(60)], asks=[(101 + i, 1) for i in range(60)])
    t = trim_book(wide, band_pct=10, min_levels=5)
    assert t.bids[-1].price >= Decimal(90) and t.asks[-1].price <= Decimal("111.1") and len(t.bids) == 11
    raw = {"bids": [["100", "1", 2], ["99", "2", 3], ["50", "1", 1], ["0.01", "9", 1]], "asks": [["101", "1", 1]],
           "sequence": 7, "time": "2026-09-27T12:00:00.123456789Z"}
    pb = parse_book(PID, raw, band_pct=10, min_levels=1)
    assert [lv.price for lv in pb.bids] == [Decimal(100), Decimal(99)] and pb.sequence == 7 and pb.raw == {}


async def test_candles_incremental_and_closed_only() -> None:
    fc = standard_client()
    md = SpotMarketData(fc, clock=fc.clock, parse_in_thread=False)
    bars = await md.candles(PID, 3600, 3, bar_end=BAR_END)
    assert [c.start.hour for c in bars] == [9, 10, 11] and all(c.end <= BAR_END for c in bars)
    assert fc.count("get_candles") == 1
    await md.candles(PID, 3600, 3, bar_end=BAR_END)  # final bar cached: no request
    assert fc.count("get_candles") == 1
    # next hour: the new bar plus one bar of overlap (a bar cached while still aggregating is
    # replaced by its final version) in one request
    nxt = BAR_END + timedelta(hours=1)
    fc.candles[(PID, 3600)].append(candle(PID, BAR_END, 110))
    fc.candles[(PID, 3600)].append(candle(PID, nxt, 999))  # in progress at nxt: never returned
    bars = await md.candles(PID, 3600, 3, bar_end=nxt)
    assert [c.close for c in bars] == [Decimal(100), Decimal(100), Decimal(110)]
    call = [c for c in fc.calls if c[0] == "get_candles"][-1]
    assert call[3] == BAR_END - timedelta(hours=1) and call[4] == BAR_END
    assert len(md.cached_candles(PID, 3600)) <= 3 + md.candle_slack


async def test_stats_bulk_and_reachability() -> None:
    fc = standard_client()
    md = SpotMarketData(fc, clock=fc.clock, parse_in_thread=False)
    assert md.reachable is None
    await md.refresh_stats()
    assert md.stats(PID).last == Decimal(100) and md.reachable is True
    await md.refresh_stats()
    assert fc.count("get") == 1  # cached for stats_ttl_s
    fc.down = True
    with pytest.raises(CoinbaseAPIError):
        await md.refresh_products()
    assert md.reachable is False and md.last_error


# --------------------------------------------------------------------------- bars -> orders


async def test_bar_rebalances_into_target_and_records_signal(tmp_path: Path) -> None:
    cb, fc = await make_services(tmp_path)
    eng = cb.engine
    events: list[tuple[str, Any]] = []
    q = cb.bus.subscribe()
    ran = await eng.run_due_bars(now=NOW)
    assert ran == ["t_hold"]
    # the strategy saw closed bars only, oldest first, and the stats
    bar_end, closes, stats = HoldStrategy.seen[0]
    assert bar_end == BAR_END and closes == [Decimal(100)] * 10 and stats is None or stats.last == Decimal(100)
    pos = cb.broker.positions("t_hold")
    assert len(pos) == 1 and pos[0].product_id == PID and pos[0].quantity > 0
    # allocation 50 % of $1000 -> ~$500 incl. fee
    assert Decimal(480) < pos[0].cost_basis <= Decimal(500)
    sig = cb.store.list_signals(limit=10)
    assert len(sig) == 1 and sig[0]["decision"] == "executed" and sig[0]["side"] == "buy"
    assert sig[0]["order_id"] and "filled" in sig[0]["decision_reason"]
    while not q.empty():
        events.append(q.get_nowait())
    kinds = [k for k, _ in events]
    assert {"signal", "order", "fill", "bar", "log"} <= set(kinds)
    assert all(d.get("venue") == "coinbase" for _, d in events)
    bar = next(d for k, d in events if k == "bar")
    assert bar["strategy"] == "t_hold" and bar["intents"] == 1 and bar["bar_end"].startswith("2026-09-27T12:00:00")
    # the same bar is never evaluated twice, also after a restart (persisted)
    assert await eng.run_due_bars(now=NOW) == []
    eng2 = CoinbaseEngine(cb.settings, cb.md, cb.broker, cb.risk, cb.store, {"t_hold": HoldStrategy}, clock=fc.clock)
    assert eng2.runtimes["t_hold"].last_bar_at == BAR_END and await eng2.run_due_bars(now=NOW) == []
    st = eng.status()
    assert st["last_bar_at"].startswith("2026-09-27T12:00:00") and st["strategies_enabled"] == ["t_hold"]
    await cb.aclose()


async def test_rebalance_sells_to_zero_when_target_drops(tmp_path: Path) -> None:
    cb, fc = await make_services(tmp_path)
    await cb.engine.run_due_bars(now=NOW)
    held = cb.broker.positions("t_hold")[0].quantity
    cb.engine.update_strategy("t_hold", params={"weight": 0.0})
    later = NOW + timedelta(hours=1)
    fc.clock.now = later
    fc.add_hourly(PID, until=BAR_END + timedelta(hours=1), n=12)
    assert await cb.engine.run_due_bars(now=later) == ["t_hold"]
    assert cb.broker.positions("t_hold") == []
    sells = [s for s in cb.store.list_signals(limit=10) if s["side"] == "sell"]
    assert sells and sells[0]["decision"] == "executed" and Decimal(str(sells[0]["base_size"])) == held
    await cb.aclose()


async def test_strategy_exception_is_isolated(tmp_path: Path) -> None:
    cb, _ = await make_services(tmp_path, strategies=[BoomStrategy, HoldStrategy])
    ran = await cb.engine.run_due_bars(now=NOW)
    assert ran == ["t_boom", "t_hold"]  # the broken one is recorded and skipped, the other trades
    boom = cb.engine.runtimes["t_boom"]
    assert "strategy bug" in (boom.last_error or "") and boom.last_bar_at == BAR_END
    assert cb.broker.positions("t_hold")
    js = cb.engine.strategy_json("t_boom")
    assert js["stats"]["last_error"] and js["venue"] == "coinbase"
    logs = cb.store.list_logs(limit=50, kind="strategy")
    assert any("strategy bug" in r["message"] for r in logs)
    await cb.aclose()


async def test_waits_for_late_final_bar_then_runs(tmp_path: Path) -> None:
    fc = standard_client()
    fc.add_hourly(PID, until=BAR_END - timedelta(hours=1))  # the 11:00 bar is not published yet
    cb, _ = await make_services(tmp_path, client=fc)
    rt = cb.engine.runtimes["t_hold"]
    assert await cb.engine.run_bar(rt, BAR_END, now=NOW) is False
    assert rt.last_bar_at is None and not cb.broker.positions()
    # published now -> runs
    fc.add_hourly(PID)
    assert await cb.engine.run_bar(rt, BAR_END, now=NOW) is True
    assert rt.last_bar_at == BAR_END and HoldStrategy.seen[-1][1][-1] == Decimal(100)
    # a product that never prints the final bar only delays until bar_delay + the bar's wait
    fc.add_hourly(PID, until=BAR_END, n=10)
    wait = cb.engine.bar_wait_for(3600)
    still_waiting = BAR_END + timedelta(hours=1, seconds=cb.engine.bar_delay_s + wait - 1)
    assert await cb.engine.run_bar(rt, BAR_END + timedelta(hours=1), now=still_waiting) is False
    rt.next_try = 0.0
    late = BAR_END + timedelta(hours=1, seconds=cb.engine.bar_delay_s + wait + 1)
    assert await cb.engine.run_bar(rt, BAR_END + timedelta(hours=1), now=late) is True
    await cb.aclose()


async def test_daily_bars_wait_up_to_an_hour_for_the_final_candle(tmp_path: Path) -> None:
    # a day-late decision cost the BTC trend rule ~7 pts/yr, so a late daily candle is
    # waited for (up to an hour) instead of running the day without it
    cb, _ = await make_services(tmp_path, client=standard_client())
    assert cb.engine.bar_wait_for(86400) == 3600.0
    assert cb.engine.bar_wait_for(3600) == 180.0
    assert cb.engine.bar_wait_for(60) == cb.engine.bar_wait_s
    await cb.aclose()


async def test_outage_backs_off_and_reports_unreachable(tmp_path: Path) -> None:
    cb, fc = await make_services(tmp_path)
    fc.down = True
    rt = cb.engine.runtimes["t_hold"]
    assert await cb.engine.run_bar(rt, BAR_END, now=NOW) is False
    assert rt.failures == 1 and rt.next_try > 0 and rt.last_bar_at is None
    assert cb.engine.status()["coinbase_reachable"] is False
    assert await cb.engine.run_due_bars(now=NOW) == []  # still backing off
    fc.down = False
    rt.next_try = 0
    assert await cb.engine.run_due_bars(now=NOW) == ["t_hold"]
    assert cb.engine.status()["coinbase_reachable"] is True and rt.last_error is None
    await cb.aclose()


async def test_kill_switch_cancels_resting_buys_and_blocks_new_buys(tmp_path: Path) -> None:
    from kalshibot.coinbase.paper import SpotOrderIntent

    cb, _fc = await make_services(tmp_path)
    o = await cb.broker.place_order(SpotOrderIntent(PID, "buy", quote_size=Decimal(50), order_type="limit",
                                                    limit_price=Decimal(90), tif="gtc", strategy="t_hold"))
    assert o.is_open
    cancelled = await cb.engine.set_kill_switch(True, "test")
    assert [c.id for c in cancelled] == [o.id] and cb.broker.open_orders() == []
    await cb.engine.run_due_bars(now=NOW)
    sig = cb.store.list_signals(limit=5)
    assert sig[0]["decision"] == "rejected" and "kill switch" in sig[0]["decision_reason"]
    assert cb.engine.status()["kill_switch"] is True
    await cb.engine.set_kill_switch(False, "")
    assert cb.engine.status()["kill_switch"] is False
    await cb.aclose()


async def test_maker_then_taker_rests_then_takes_remainder(tmp_path: Path) -> None:
    cb, fc = await make_services(tmp_path, strategies=[MakerStrategy])
    t = [0.0]
    cb.engine.mono = lambda: t[0]
    await cb.engine.run_due_bars(now=NOW)
    opens = cb.broker.open_orders("t_maker")
    assert len(opens) == 1 and opens[0].post_only and opens[0].limit_price == Decimal("99.99")
    assert cb.store.list_signals(limit=1)[0]["decision"] == "resting"
    # a public print hits part of our bid (after the queue ahead is gone? queue ahead = 1000 at 99.99:
    # a trade-through below our price fills us regardless)
    fc.clock.now = NOW + timedelta(seconds=30)
    fc.trades[PID] = [make_trade(1, "99.00", "1", "buy", NOW + timedelta(seconds=20))]
    await cb.engine._job_maintenance()
    fc.trades[PID].append(make_trade(2, "98.50", "1", "buy", NOW + timedelta(seconds=25)))
    await cb.engine._job_maintenance()
    o = cb.broker.get_order(opens[0].id)
    assert o is not None and o.filled_base > 0 and o.is_open
    # timeout: cancel and take the rest as a taker
    t[0] = cb.engine.maker_timeout_s + 1
    await cb.engine._job_maintenance()
    assert cb.broker.open_orders("t_maker") == []
    sig = cb.store.list_signals(limit=5)
    assert sig[0]["decision"] == "executed" and "timed out" in sig[0]["decision_reason"]
    pos = cb.broker.positions("t_maker")[0]
    assert Decimal(240) < pos.cost_basis <= Decimal(250)  # 50 % of the 50 % allocation, fee included
    await cb.aclose()


async def test_update_strategy_validates_and_persists(tmp_path: Path) -> None:
    cb, fc = await make_services(tmp_path)
    assert fc.count("get_products") == 1
    with pytest.raises(ParamError):
        cb.engine.update_strategy("t_hold", params={"weight": 3})
    with pytest.raises(ParamError):
        cb.engine.update_strategy("t_hold", params={"nope": 1})
    js = cb.engine.update_strategy("t_hold", enabled=False, params={"weight": 0.25})
    assert js["enabled"] is False and js["enabled_source"] == "dashboard" and js["params"]["weight"] == 0.25
    assert await cb.engine.run_due_bars(now=NOW) == []  # disabled
    eng2 = CoinbaseEngine(cb.settings, cb.md, cb.broker, cb.risk, cb.store, {"t_hold": HoldStrategy}, clock=fc.clock)
    rt = eng2.runtimes["t_hold"]
    assert rt.enabled is False and rt.instance.params["weight"] == 0.25
    with pytest.raises(KeyError):
        cb.engine.update_strategy("missing", enabled=True)
    await cb.aclose()


async def test_reset_strategies_forgets_bars(tmp_path: Path) -> None:
    cb, _ = await make_services(tmp_path)
    await cb.engine.run_due_bars(now=NOW)
    cb.broker.reset()
    cb.engine.reset_strategies()
    assert cb.engine.runtimes["t_hold"].last_bar_at is None
    assert await cb.engine.run_due_bars(now=NOW) == ["t_hold"]  # re-enters after the reset
    await cb.aclose()


async def test_engine_loop_start_stop_snapshot(tmp_path: Path) -> None:
    fc = standard_client()
    cb, _ = await make_services(tmp_path, client=fc, load_products=False, strategies=[])
    cb.bind()
    q = cb.bus.subscribe()
    await cb.engine.start()
    for _ in range(100):
        if cb.engine.tick_count >= 1 and cb.md.products_loaded:
            break
        await asyncio.sleep(0.02)
    st = cb.engine.status()
    assert st["running"] is True and st["products_loaded"] == 1 and st["tick_count"] >= 1
    assert st["last_tick_at"] and st["coinbase_reachable"] is True
    await cb.engine.stop()
    assert cb.engine.status()["running"] is False
    kinds = set()
    while not q.empty():
        k, d = q.get_nowait()
        kinds.add(k)
        assert d["venue"] == "coinbase"
    assert {"tick", "account", "log"} <= kinds
    assert cb.store.list_equity()  # a snapshot row was written
    await cb.aclose()


async def test_engine_loop_survives_outage(tmp_path: Path) -> None:
    fc = standard_client()
    fc.down = True
    cb, _ = await make_services(tmp_path, client=fc, load_products=False)
    await cb.engine.start()
    for _ in range(100):
        if cb.engine.jobs["products"].failures:
            break
        await asyncio.sleep(0.02)
    st = cb.engine.status()
    assert st["running"] is True and st["coinbase_reachable"] is False and "unreachable" in (st["last_error"] or "")
    assert 1 < st["jobs"]["products"]["next_in_s"] <= 300  # backed off, but retried long before the hourly refresh
    await cb.engine.stop()
    await cb.aclose()


async def test_live_context_is_read_only() -> None:
    c = [candle(PID, BAR_END - timedelta(hours=2), 1), candle(PID, BAR_END - timedelta(hours=1), 2)]
    got: list[Any] = []
    ctx = LiveSpotContext(now=NOW, bar_end=BAR_END, products={PID: make_product(PID)}, params={"a": 1},
                          candles={PID: c}, stats=lambda p: None, portfolio=None,  # type: ignore[arg-type]
                          log=lambda m, d: got.append((m, d)))
    assert [x.close for x in ctx.candles(PID, 5)] == [1, 2] and ctx.candles(PID, 1)[0].close == 2
    assert ctx.candles("NOPE-USD", 3) == [] and ctx.candles(PID, 0) == []
    ctx.candles(PID, 5).clear()
    assert len(ctx.candles(PID, 5)) == 2
    with pytest.raises(TypeError):
        ctx.products["X"] = None  # type: ignore[index]
    ctx.log("hi", x=1)
    assert got == [("hi", {"x": 1})]


async def test_build_refuses_disabled_or_bad_config(tmp_path: Path) -> None:
    s = cb_settings(tmp_path)
    s.enabled = False
    with pytest.raises(CoinbaseUnavailable, match="disabled"):
        await build_coinbase_services(s)
    s2 = CoinbaseSettings(enabled=False, load_error="invalid coinbase config: max_rps: bad")
    with pytest.raises(CoinbaseUnavailable, match="invalid coinbase config"):
        await build_coinbase_services(s2)
    assert not (tmp_path / "cb.sqlite3").exists()
