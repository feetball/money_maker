"""MarketDataService: universe queries, caches, batching, pagination, fallbacks (no network)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FakeKalshiClient, raw_market, standard_market

from kalshibot.kalshi.client import KalshiAPIError, KalshiNotFound
from kalshibot.marketdata import MarketDataService, display_title, market_url
from kalshibot.money import D
from kalshibot.strategies.base import UniverseSpec

NOW = datetime.now(UTC).replace(microsecond=0)


class Mono:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def svc(fc: FakeKalshiClient, **kw) -> tuple[MarketDataService, Mono]:
    mono = Mono()
    kw.setdefault("scanner_days_to_close", 0)
    return MarketDataService(fc, clock=lambda: NOW, mono=mono, **kw), mono


def markets_calls(fc: FakeKalshiClient) -> list[dict]:
    return [c[2] for c in fc.calls if c[0] == "get" and c[1] == "/markets"]


@pytest.fixture
def universe_client() -> FakeKalshiClient:
    fc = FakeKalshiClient(page_size=2)
    fc.add_market("KXA-1", close_time=NOW + timedelta(days=1), yes_bid="0.2", yes_ask="0.3", volume_24h=50)
    fc.add_market("KXA-2", close_time=NOW + timedelta(days=1), status="initialized")
    fc.add_market("KXA-3", close_time=NOW + timedelta(days=5), yes_bid="0.5", yes_ask="0.6")
    fc.add_market("KXA-4", close_time=NOW + timedelta(hours=30), yes_bid="0.1", yes_ask="0.5", volume_24h=900)
    fc.add_market("KXS-X", close_time=NOW + timedelta(days=20), yes_bid="0.4", yes_ask="0.41")
    fc.add_market("KXS-Y", close_time=NOW + timedelta(days=20), status="closed")
    return fc


async def test_refresh_universe_queries_and_filters(universe_client: FakeKalshiClient) -> None:
    md, _ = svc(universe_client)
    md.set_specs({"a": UniverseSpec(max_days_to_close=2), "b": UniverseSpec(series_tickers=["KXS"])})
    assert await md.refresh_universe() is True
    assert set(md.markets) == {"KXA-1", "KXA-4", "KXS-X"}
    assert md.universe_size == 3 and md.last_refresh == NOW and md.last_error is None
    calls = markets_calls(universe_client)
    window = [c for c in calls if "min_close_ts" in c]
    series = [c for c in calls if c.get("series_ticker") == "KXS"]
    assert window and series
    first = window[0]
    assert "status" not in first  # close-ts filters only combine with an empty status
    assert first["mve_filter"] == "exclude" and first["limit"] == 1000
    # nearest chunk first ([0, 0.5d], [0.5d, 1d], [1d, 2d]): the API lists a window latest close first
    ts = int(NOW.timestamp())
    spans = [(c["min_close_ts"], c["max_close_ts"]) for c in window if "cursor" not in c]
    assert spans == [(ts, ts + 43200), (ts + 43200, ts + 86400), (ts + 86400, ts + 2 * 86400)]
    # [0.5d, 1d] holds KXA-1/KXA-2 (at 1 d) -> one page; [1d, 2d] holds KXA-1, KXA-2, KXA-4 -> 2 pages
    assert len(window) == 4 and sum(1 for c in window if "cursor" in c) == 1
    assert series[0]["status"] == "open"
    # per-strategy views
    assert set(md.markets_for(UniverseSpec(series_tickers=["KXS"]))) == {"KXS-X"}
    assert set(md.markets_for(UniverseSpec(max_days_to_close=1.1))) == {"KXA-1"}
    assert md.markets_for(UniverseSpec()) == {}


async def test_refresh_interval_force_and_scanner(universe_client: FakeKalshiClient) -> None:
    md, mono = svc(universe_client, scanner_days_to_close=1.5)
    assert await md.refresh_universe()
    assert set(md.markets) == {"KXA-1", "KXA-4"}  # scanner baseline window, no strategy specs
    n = len(universe_client.calls)
    assert await md.refresh_universe() is False  # < 60 s
    assert await md.refresh_universe(force=True) is False  # forced but < 5 s
    assert len(universe_client.calls) == n
    mono.t += 61
    assert await md.refresh_universe() is True
    assert md.refresh_count == 2


async def test_universe_page_cap_is_reported(universe_client: FakeKalshiClient) -> None:
    universe_client.page_size = 1
    md, mono = svc(universe_client, universe_max_pages=2)
    md.set_specs({"a": UniverseSpec(max_days_to_close=0.5)})  # one chunk
    await md.refresh_universe()
    assert md.truncated == [] and len(markets_calls(universe_client)) == 1  # empty window: one page
    universe_client.add_market("KXN-1", close_time=NOW + timedelta(hours=2), yes_bid="0.2", yes_ask="0.3")
    universe_client.add_market("KXN-2", close_time=NOW + timedelta(hours=3), yes_bid="0.2", yes_ask="0.3")
    universe_client.add_market("KXN-3", close_time=NOW + timedelta(hours=4), yes_bid="0.2", yes_ask="0.3")
    mono.t += 1000
    assert await md.refresh_universe()
    assert md.truncated == ["close<0.5d"]
    assert len(markets_calls(universe_client)) == 1 + 2


async def test_page_cap_keeps_the_markets_closing_soonest(universe_client: FakeKalshiClient) -> None:
    """The API lists a window latest close first: the chunks are read nearest first, sharing one page
    budget, so a cap loses the far end of the window (was: the markets closing soonest)."""
    universe_client.page_size = 1
    for h in (1, 2, 3):
        universe_client.add_market(f"KXN-{h}", close_time=NOW + timedelta(hours=h), yes_bid="0.2", yes_ask="0.3")
    md, _ = svc(universe_client, universe_max_pages=5)
    md.set_specs({"a": UniverseSpec(max_days_to_close=30)})
    await md.refresh_universe()
    # [0, 0.5d]: 3 pages (KXN-1..3), [0.5d, 1d]: KXA-1 + KXA-2 (initialized) -> budget of 5 used up
    assert {"KXN-1", "KXN-2", "KXN-3", "KXA-1"} <= set(md.markets)
    assert "KXA-3" not in md.markets and "KXA-4" not in md.markets  # 30 h / 5 d out: not read
    assert md.truncated == ["close<30d[1-30d]"]
    assert len(markets_calls(universe_client)) == 5
    # a cap inside a chunk reports that chunk (its nearer end is the part left unread)
    universe_client.calls.clear()
    md.universe_max_pages = 4
    md._last_window_scan_mono = None
    md._last_refresh_mono = None
    await md.refresh_universe()
    assert md.truncated == ["close<30d[0.5-1d]", "close<30d[1-30d]"]


async def test_failed_refresh_keeps_previous_universe(universe_client: FakeKalshiClient) -> None:
    md, mono = svc(universe_client)
    md.set_specs({"a": UniverseSpec(max_days_to_close=2)})
    await md.refresh_universe()
    before = set(md.markets)
    universe_client.fail.add("get")
    mono.t += 61
    assert await md.refresh_universe() is True
    assert set(md.markets) == before
    assert md.last_error and "simulated outage" in md.last_error


async def test_orderbook_ttl_cache() -> None:
    fc = FakeKalshiClient()
    standard_market(fc, "KXT-1")
    md, mono = svc(fc)
    b1 = await md.orderbook("KXT-1", max_age_s=5)
    b2 = await md.orderbook("KXT-1", max_age_s=5)
    assert b1 is b2 and fc.count("get_orderbook") == 1
    assert b1.best_yes_ask == D("0.45")
    mono.t += 6
    await md.orderbook("KXT-1", max_age_s=5)
    assert fc.count("get_orderbook") == 2
    assert md.cached_orderbook("KXT-1") is not None


async def test_concurrent_orderbooks_are_batched() -> None:
    fc = FakeKalshiClient()
    for t in ("KXT-1", "KXT-2", "KXT-3"):
        standard_market(fc, t)
    fc.omit_from_batch.add("KXT-3")  # the batch answer "forgets" one -> single fallback
    md, _ = svc(fc)
    books = await asyncio.gather(*(md.orderbook(t) for t in ("KXT-1", "KXT-2", "KXT-3", "KXT-1")))
    assert [b.ticker for b in books] == ["KXT-1", "KXT-2", "KXT-3", "KXT-1"]
    assert fc.count("get_orderbooks") == 1
    assert [c for c in fc.calls if c[0] == "get_orderbooks"][0][1] == ("KXT-1", "KXT-2", "KXT-3")
    assert fc.count("get_orderbook") == 1  # only the omitted one


async def test_orderbooks_omits_failures() -> None:
    fc = FakeKalshiClient()
    standard_market(fc, "KXT-1")
    md, _ = svc(fc)
    out = await md.orderbooks(["KXT-1", "NOPE-1"])
    assert set(out) == {"KXT-1"}
    with pytest.raises(KalshiNotFound):
        await md.orderbook("NOPE-1")


async def test_orderbook_errors_propagate_to_all_waiters() -> None:
    fc = FakeKalshiClient()
    standard_market(fc, "KXT-1")
    standard_market(fc, "KXT-2")
    fc.fail.add("get_orderbooks")
    md, _ = svc(fc)
    res = await asyncio.gather(md.orderbook("KXT-1"), md.orderbook("KXT-2"), return_exceptions=True)
    assert all(isinstance(r, KalshiAPIError) for r in res)
    fc.fail.clear()
    assert (await md.orderbook("KXT-1")).ticker == "KXT-1"  # nothing poisoned


async def test_trades_since_follows_every_page() -> None:
    fc = FakeKalshiClient()
    fc.trade_page_size = 2
    base = NOW - timedelta(minutes=10)
    for i in range(5):
        fc.add_trade("KXT-1", "0.5", 1, base + timedelta(seconds=30 * i))
    fc.add_trade("KXT-1", "0.5", 1, base - timedelta(minutes=5))  # before `since`
    md, _ = svc(fc)
    since = base + timedelta(microseconds=500_000)
    trades = await md.trades_since("KXT-1", since)
    assert len(trades) == 5  # min_ts is whole seconds: the print at `base` is included
    assert fc.count("get_trades") == 3
    assert all(c[2] == int(since.timestamp()) for c in fc.calls if c[0] == "get_trades")


async def test_trades_since_page_cap_logs_error(caplog: pytest.LogCaptureFixture) -> None:
    fc = FakeKalshiClient()
    fc.trade_page_size = 1
    for i in range(4):
        fc.add_trade("KXT-1", "0.5", 1, NOW - timedelta(seconds=i))
    md, _ = svc(fc, max_trade_pages=2)
    trades = await md.trades_since("KXT-1", NOW - timedelta(minutes=1))
    assert len(trades) == 2
    assert "backlog exceeds 2 pages" in caplog.text


async def test_market_cache_fresh_and_batch_priming(universe_client: FakeKalshiClient) -> None:
    md, mono = svc(universe_client)
    md.set_specs({"a": UniverseSpec(max_days_to_close=2)})
    await md.refresh_universe()
    m = await md.market("KXA-1")  # from the universe snapshot
    assert m.ticker == "KXA-1" and universe_client.count("get_market") == 0
    mono.t += 10
    await md.market("KXA-1", fresh=True)  # older than 5 s -> single fetch
    assert universe_client.count("get_market") == 1
    mono.t += 10
    got = await md.refresh_markets(["KXA-1", "KXS-Y"])
    assert set(got) == {"KXA-1", "KXS-Y"}
    tickers_calls = [c for c in markets_calls(universe_client) if "tickers" in c]
    assert tickers_calls[-1]["tickers"] == ["KXA-1", "KXS-Y"]
    await md.market("KXA-1", fresh=True)
    await md.market("KXS-Y", fresh=True)
    assert universe_client.count("get_market") == 1  # primed by the batch call


async def test_market_leaving_active_is_dropped_from_universe(universe_client: FakeKalshiClient) -> None:
    md, mono = svc(universe_client)
    md.set_specs({"a": UniverseSpec(max_days_to_close=2)})
    await md.refresh_universe()
    universe_client.update_market("KXA-1", status="closed")
    mono.t += 10
    m = await md.market("KXA-1", fresh=True)
    assert m.status == "closed"
    assert "KXA-1" not in md.markets
    assert md.known_market("KXA-1") is m


async def test_market_404_falls_back_to_historical() -> None:
    fc = FakeKalshiClient()
    fc.historical["OLD-1"] = raw_market("OLD-1", close_time=NOW - timedelta(days=90), status="finalized",
                                        result="yes", settlement_value="1")
    md, _ = svc(fc)
    m = await md.market("OLD-1", fresh=True)
    assert m.status == "finalized" and m.result == "yes"
    with pytest.raises(KalshiNotFound):
        await md.market("NOPE-1")


async def test_series_cache_stale_on_failure_and_prefetch(universe_client: FakeKalshiClient) -> None:
    universe_client.set_series("KXA", "quadratic_with_maker_fees", 1, category="Crypto")
    universe_client.set_series("KXS", category="Sports")
    md, mono = svc(universe_client, series_ttl_s=100)
    md.set_specs({"a": UniverseSpec(max_days_to_close=2), "b": UniverseSpec(series_tickers=["KXS"])})
    await md.refresh_universe()
    assert await md.prefetch_series(limit=1) == 1
    assert md.cached_series("KXA") is not None  # most markets first
    assert md.cached_series("KXS") is None
    assert await md.prefetch_series(limit=5) == 1
    s = await md.series("KXA")
    assert s.fee_type == "quadratic_with_maker_fees"
    assert universe_client.count("get_series") == 2
    mono.t += 101
    universe_client.fail.add("get_series")
    assert (await md.series("KXA")) is s  # stale copy on failure
    with pytest.raises(KalshiAPIError):
        await md.series("KXZ")
    m = md.markets["KXA-1"]
    assert md.category(m) == "Crypto"


async def test_event_lazy_with_universe_markets(universe_client: FakeKalshiClient) -> None:
    universe_client.update_market("KXA-1", event_ticker="KXA-EV")
    universe_client.update_market("KXA-4", event_ticker="KXA-EV")
    universe_client.set_event("KXA-EV", mutually_exclusive=True, category="Crypto")
    md, mono = svc(universe_client, event_ttl_s=100)
    md.set_specs({"a": UniverseSpec(max_days_to_close=2)})
    await md.refresh_universe()
    ev = await md.event("KXA-EV")
    assert ev is not None and ev.mutually_exclusive
    assert {m.ticker for m in ev.markets} == {"KXA-1", "KXA-4"}
    assert ev.markets[0] is md.markets[ev.markets[0].ticker]  # universe objects
    await md.event("KXA-EV")
    assert universe_client.count("get_event") == 1
    assert md.events["KXA-EV"] is ev
    mono.t += 101
    universe_client.fail.add("get_event")
    assert (await md.event("KXA-EV")).event_ticker == "KXA-EV"  # stale on failure


async def test_exchange_status_cached_never_raises() -> None:
    fc = FakeKalshiClient()
    md, mono = svc(fc)
    assert md.trading_active is None
    st = await md.exchange_status()
    assert st["trading_active"] is True and md.trading_active is True
    await md.exchange_status()
    assert fc.count("get_exchange_status") == 1
    mono.t += 20
    fc.fail.add("get_exchange_status")
    st2 = await md.exchange_status()
    assert st2 == st and md.exchange_error  # last known value
    fc.fail.clear()
    mono.t += 20
    fc.exchange["trading_active"] = False
    await md.exchange_status()
    assert md.trading_active is False and md.exchange_error is None


async def test_search_sort_and_titles(universe_client: FakeKalshiClient) -> None:
    md, _ = svc(universe_client)
    md.set_specs({"a": UniverseSpec(max_days_to_close=2), "b": UniverseSpec(series_tickers=["KXS"])})
    await md.refresh_universe()
    assert [m.ticker for m in md.search()] == ["KXA-4", "KXA-1", "KXS-X"]  # volume_24h desc
    assert [m.ticker for m in md.search(sort="close_time")] == ["KXA-1", "KXA-4", "KXS-X"]
    assert [m.ticker for m in md.search(sort="spread")] == ["KXS-X", "KXA-1", "KXA-4"]
    assert [m.ticker for m in md.search(search="kxs")] == ["KXS-X"]
    assert md.search(limit=1)[0].ticker == "KXA-4"
    m = md.markets["KXA-1"]
    assert display_title(m) == "Title of KXA-1"
    assert market_url("KXBTCD") == "https://kalshi.com/markets/kxbtcd"
    st = md.status()
    assert st["universe_size"] == 3 and st["refresh_count"] == 1


async def test_window_rescan_vs_series_refresh(universe_client: FakeKalshiClient) -> None:
    universe_client.update_market("KXA-3", status="active")
    md, mono = svc(universe_client, window_rescan_s=900)
    md.set_specs({"a": UniverseSpec(max_days_to_close=2)})
    await md.refresh_universe()
    assert md.last_refresh_kind == "full" and set(md.markets) == {"KXA-1", "KXA-4"}
    universe_client.calls.clear()
    # a new active market in a known window series shows up without a full scan;
    # KXA-3 (same series, closes in 5 days) stays out because it is outside the window
    universe_client.add_market("KXA-5", close_time=NOW + timedelta(hours=3), yes_bid="0.1", yes_ask="0.2")
    mono.t += 61
    assert await md.refresh_universe()
    assert md.last_refresh_kind == "series"
    calls = markets_calls(universe_client)
    assert calls and all("min_close_ts" not in c for c in calls)
    assert {c["series_ticker"] for c in calls} == {"KXA"} and all(c["status"] == "open" for c in calls)
    assert set(md.markets) == {"KXA-1", "KXA-4", "KXA-5"}
    # after window_rescan_s the window is scanned in full again
    mono.t += 900
    await md.refresh_universe()
    assert md.last_refresh_kind == "full"
    # a wider window forces a full scan immediately
    mono.t += 61
    md.set_specs({"a": UniverseSpec(max_days_to_close=6)})
    await md.refresh_universe()
    assert md.last_refresh_kind == "full" and "KXA-3" in md.markets
    assert md.status()["window_series"] == 1  # only KXA has active markets within 6 days
