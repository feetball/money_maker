"""Prompt pickup of newly listed short-lived markets (``UniverseSpec.refresh_s``) and the extra
pre-close mark of held markets (ARCHITECTURE.md §4, §7, §9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from conftest import DummyStrategy, FakeKalshiClient, ScriptedStrategy, standard_market

from kalshibot.api.server import AppServices, build_services
from kalshibot.config import Settings
from kalshibot.engine import MIN_SERIES_REFRESH_S
from kalshibot.feeds import FeedRegistry
from kalshibot.strategies.base import UniverseSpec

SERIES = "KXBTC15M"


class Windows(ScriptedStrategy):
    name = "windows"
    refresh_s: float | None = 20

    def universe(self) -> UniverseSpec:
        return UniverseSpec(series_tickers=[SERIES], refresh_s=self.refresh_s)


def window(fc: FakeKalshiClient, minutes: int, status: str = "active") -> str:
    close = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(minutes=minutes)
    t = f"{SERIES}-26SEP27{close:%H%M}-15"
    fc.add_market(t, close_time=close, status=status, yes_bid="0.90", yes_ask="0.92", series_ticker=SERIES)
    return t


def stack(settings: Settings, fc: FakeKalshiClient, strategies: dict[str, Any]) -> AppServices:
    svc = build_services(settings, client=fc, strategies=strategies, feeds=FeedRegistry())
    svc.md.scanner_days_to_close = 0
    return svc


def market_gets(fc: FakeKalshiClient) -> int:
    return sum(1 for c in fc.calls if c[0] == "get" and c[1] == "/markets")


async def test_series_refresh_picks_up_new_windows_between_full_refreshes(settings, fake_client) -> None:
    first = window(fake_client, 10)
    nxt = window(fake_client, 25, status="initialized")  # pre-created, not trading yet
    svc = stack(settings, fake_client, {"windows": Windows})
    eng = svc.engine
    eng.update_strategy("windows", enabled=True)
    assert eng.jobs["series"].interval == 20
    await svc.md.refresh_universe(force=True)
    assert set(svc.md.markets) == {first}

    fake_client.update_market(nxt, status="active")  # the next window opens
    fake_client.update_market(first, status="closed")  # and the previous one closed
    assert not svc.md.refresh_due()  # a full refresh would wait (>= 60 s apart)
    before = market_gets(fake_client)
    await eng._job_series()
    assert market_gets(fake_client) - before == 1  # one request for the series
    assert set(svc.md.markets) == {nxt}
    rt = eng.runtimes["windows"]
    assert set(svc.md.markets_for(rt.spec())) == {nxt}
    await svc.aclose()


async def test_series_job_is_opt_in_and_clamped(settings, fake_client) -> None:
    window(fake_client, 10)
    svc = stack(settings, fake_client, {"windows": Windows})
    eng = svc.engine
    inst = eng.runtimes["windows"].instance
    inst.refresh_s = None
    eng.update_strategy("windows", enabled=True)
    assert eng.jobs["series"].interval == eng.intervals["universe"]
    await svc.md.refresh_universe(force=True)
    before = len(fake_client.calls)
    await eng._job_series()
    assert len(fake_client.calls) == before  # no refresh_s: no extra requests
    inst.refresh_s = 1
    eng._sync_specs()
    assert eng.jobs["series"].interval == MIN_SERIES_REFRESH_S
    await svc.aclose()


async def test_series_job_waits_for_the_first_full_refresh(settings, fake_client) -> None:
    window(fake_client, 10)
    svc = stack(settings, fake_client, {"windows": Windows})
    svc.engine.update_strategy("windows", enabled=True)
    await svc.engine._job_series()
    assert fake_client.calls == [] and svc.md.markets == {}
    await svc.aclose()


async def test_full_refresh_keeps_newer_series_data(settings, fake_client) -> None:
    """A full refresh that started before a series refresh does not resurrect / drop its markets."""
    first = window(fake_client, 10)
    svc = stack(settings, fake_client, {"windows": Windows})
    svc.engine.update_strategy("windows", enabled=True)
    md = svc.md
    await md.refresh_universe(force=True)
    t0 = md.mono()
    nxt = window(fake_client, 25)
    fake_client.update_market(first, status="closed")
    await md.refresh_series([SERIES])
    stale = {first: md.known_market(first)}  # what a full refresh started at t0 had fetched
    md._merge_fresh_series(stale, since=t0 - 1, series=[SERIES])
    assert set(stale) == {nxt}
    untouched = {first: md.known_market(first)}
    md._merge_fresh_series(untouched, since=md.mono() + 1, series=[SERIES])  # older series data: ignored
    assert set(untouched) == {first}
    await svc.aclose()


async def test_preclose_job_marks_held_markets_once_just_before_close(settings, fake_client) -> None:
    t = "KXTEST-26SEP27-A"
    standard_market(fake_client, t)
    svc = stack(settings, fake_client, {"dummy": DummyStrategy})
    eng = svc.engine
    eng.update_strategy("dummy", enabled=True)
    await svc.md.refresh_universe(force=True)
    await eng.tick()  # holds 2 YES
    assert svc.broker.position(t, "dummy") is not None

    def books() -> int:
        return sum(1 for c in fake_client.calls if c[0] in ("get_orderbooks", "get_orderbook"))

    n = books()
    await eng._job_preclose()
    assert books() == n  # nothing closing soon: no request
    close = datetime.now(UTC) + timedelta(seconds=3)
    fake_client.update_market(t, close_time=close.isoformat().replace("+00:00", "Z"))
    await svc.md.refresh_markets([t])
    fake_client.set_book(t, yes=[("0.47", 100)], no=[("0.50", 100)])
    svc.md._books.clear()  # the tick's books are older than a second by now
    await eng._job_preclose()
    assert books() == n + 1
    assert svc.broker.mark(t).yes_bid.to_eng_string() == "0.47"
    await eng._job_preclose()
    assert books() == n + 1  # once per close time
    await svc.aclose()


async def test_postclose_job_settles_just_closed_markets_quickly(settings, fake_client) -> None:
    t = "KXTEST-26SEP27-A"
    standard_market(fake_client, t)
    svc = stack(settings, fake_client, {"dummy": DummyStrategy})
    eng = svc.engine
    eng.update_strategy("dummy", enabled=True)
    await svc.md.refresh_universe(force=True)
    await eng.tick()  # holds 2 YES
    n = len(fake_client.calls)
    await eng._job_postclose()
    assert len(fake_client.calls) == n  # still open: no request

    def close_at(when: datetime, **kw: Any) -> None:
        fake_client.update_market(t, close_time=when.isoformat().replace("+00:00", "Z"), **kw)

    close_at(datetime.now(UTC) - timedelta(minutes=20), status="closed")  # long closed: normal cadence only
    await svc.md.refresh_markets([t])
    n = len(fake_client.calls)
    await eng._job_postclose()
    assert len(fake_client.calls) == n
    close_at(datetime.now(UTC) - timedelta(seconds=6), status="finalized", result="yes")
    await svc.md.refresh_markets([t])
    await eng._job_postclose()
    assert svc.broker.positions() == [] and svc.store.list_settlements()[0].payout == 2
    await svc.aclose()


async def test_ctx_markets_leave_out_markets_past_their_close(settings, fake_client) -> None:
    first = window(fake_client, 10)
    svc = stack(settings, fake_client, {"windows": Windows})
    svc.engine.update_strategy("windows", enabled=True)
    await svc.md.refresh_universe(force=True)
    spec = svc.engine.runtimes["windows"].spec()
    close = svc.md.markets[first].close_time
    assert set(svc.md.markets_for(spec, close - timedelta(seconds=1))) == {first}
    assert svc.md.markets_for(spec, close) == {}  # closed since the refresh: not offered as open
    await svc.aclose()
