"""KalshiClient tests with respx-mocked HTTP (no network)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from kalshibot.kalshi.client import (
    DEFAULT_BASE_URL,
    KalshiAPIError,
    KalshiClient,
    KalshiNotFound,
    KalshiRateLimited,
    TokenBucket,
)
from kalshibot.kalshi.models import Event, Market, Orderbook, Series
from kalshibot.money import D

FIXTURES = Path(__file__).parent / "fixtures"
B = DEFAULT_BASE_URL
RATE_LIMITED = {"error": {"code": "too_many_requests", "message": "too many requests"}}  # live 429 body


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


class FakeTime:
    """Deterministic clock + sleep: sleeping advances the clock."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


@pytest.fixture
def ft():
    return FakeTime()


@pytest.fixture
async def client(ft):
    c = KalshiClient(max_rps=1000, sleep=ft.sleep, clock=ft.clock)
    yield c
    await c.aclose()


def market_dict(ticker: str) -> dict:
    m = dict(load("markets_page.json")["markets"][0])
    m["ticker"] = ticker
    return m


# --------------------------------------------------------------------------- endpoints


@respx.mock
async def test_get_markets_page_and_params(client):
    route = respx.get(f"{B}/markets").mock(return_value=httpx.Response(200, json=load("markets_page.json")))
    ms, cursor = await client.get_markets(status="open", tickers=["A", "B"], mve_filter="exclude",
                                          min_close_ts=datetime(2026, 9, 27, tzinfo=UTC), limit=3,
                                          event_ticker=None)
    assert [m.ticker for m in ms][0] == "KXUSLTOTAL-26SEP26IELMIA-7"
    assert all(isinstance(m, Market) for m in ms)
    assert cursor and cursor.startswith("Cgs")
    params = route.calls.last.request.url.params
    assert params["tickers"] == "A,B"  # comma list for /markets
    assert params["min_close_ts"] == str(int(datetime(2026, 9, 27, tzinfo=UTC).timestamp()))
    assert params["status"] == "open" and params["limit"] == "3"
    assert "event_ticker" not in params  # None dropped


@respx.mock
async def test_iter_markets_paginates_until_empty_cursor(client):
    pages = {
        None: {"markets": [market_dict("A-1"), market_dict("A-2")], "cursor": "c1"},
        "c1": {"markets": [market_dict("A-3")], "cursor": "c2"},
        "c2": {"markets": [market_dict("A-4")], "cursor": ""},
    }

    def handler(request: httpx.Request):
        return httpx.Response(200, json=pages[request.url.params.get("cursor")])

    route = respx.get(f"{B}/markets").mock(side_effect=handler)
    tickers = [m.ticker async for m in client.iter_markets(status="open")]
    assert tickers == ["A-1", "A-2", "A-3", "A-4"]
    assert route.call_count == 3
    first = route.calls[0].request.url.params
    assert first["limit"] == "1000" and first["mve_filter"] == "exclude" and "cursor" not in first
    assert route.calls[2].request.url.params["cursor"] == "c2"


@respx.mock
async def test_iter_markets_null_cursor_and_override(client):
    route = respx.get(f"{B}/markets").mock(
        return_value=httpx.Response(200, json={"markets": [market_dict("Z-1")], "cursor": None}))
    out = [m async for m in client.iter_markets(mve_filter=None, limit=10)]
    assert len(out) == 1 and route.call_count == 1
    assert "mve_filter" not in route.calls.last.request.url.params


@respx.mock
async def test_pagination_stops_on_repeated_cursor(client):
    route = respx.get(f"{B}/events").mock(
        return_value=httpx.Response(200, json={"events": [], "cursor": "same"}))
    assert [e async for e in client.iter_events()] == []
    assert route.call_count == 2  # second page repeats the cursor -> stop


@respx.mock
async def test_iter_events_with_nested_markets(client):
    route = respx.get(f"{B}/events").mock(return_value=httpx.Response(
        200, json=dict(load("events_nested.json"), cursor="")))
    evs = [e async for e in client.iter_events(status="open", with_nested_markets=True)]
    assert len(evs) == 1 and isinstance(evs[0], Event) and len(evs[0].markets) == 3
    p = route.calls.last.request.url.params
    assert p["with_nested_markets"] == "true" and p["limit"] == "200"


@respx.mock
async def test_get_market_event_series(client):
    respx.get(f"{B}/markets/KXBTCD-26SEP2620-T84499.99").mock(
        return_value=httpx.Response(200, json=load("market_single.json")))
    respx.get(f"{B}/events/KXUSLTOTAL-26SEP26IELMIA").mock(
        return_value=httpx.Response(200, json=load("event_single.json")))
    respx.get(f"{B}/series/KXMLBGAME").mock(
        return_value=httpx.Response(200, json=load("series_half_multiplier.json")))
    m = await client.get_market("KXBTCD-26SEP2620-T84499.99")
    assert m.ticker == "KXBTCD-26SEP2620-T84499.99"
    e = await client.get_event("KXUSLTOTAL-26SEP26IELMIA")
    assert e.series_ticker == "KXUSLTOTAL" and len(e.markets) == 3
    s = await client.get_series("KXMLBGAME")
    assert isinstance(s, Series) and s.fee_multiplier == D("0.5")


@respx.mock
async def test_get_orderbook_depth(client):
    route = respx.get(f"{B}/markets/T/orderbook").mock(
        return_value=httpx.Response(200, json=load("orderbook.json")))
    ob = await client.get_orderbook("T")
    assert isinstance(ob, Orderbook) and ob.ticker == "T" and ob.best_yes_bid == D("0.12")
    assert "depth" not in route.calls.last.request.url.params
    await client.get_orderbook("T", depth=5)
    assert route.calls.last.request.url.params["depth"] == "5"


@respx.mock
async def test_get_orderbooks_batch_repeats_param_and_chunks(client):
    batch = load("orderbooks_batch.json")

    def handler(request: httpx.Request):
        tickers = request.url.params.get_list("tickers")
        books = [e for e in batch["orderbooks"] if e["ticker"] in tickers]
        books += [{"ticker": t, "orderbook_fp": {"yes_dollars": [], "no_dollars": []}}
                  for t in tickers if t.startswith("FILL-")]
        return httpx.Response(200, json={"orderbooks": books})

    route = respx.get(f"{B}/markets/orderbooks").mock(side_effect=handler)
    real = ["KXBTCD-26SEP2620-T84499.99", "KXNFLCAREERRECYDS-MEVANS-22896"]
    books = await client.get_orderbooks(real)
    assert set(books) == set(real)
    assert books[real[0]].best_no_bid == D("0.88") and books[real[1]].is_empty
    url = str(route.calls.last.request.url)
    assert "tickers=KXBTCD-26SEP2620-T84499.99&tickers=KXNFLCAREERRECYDS-MEVANS-22896" in url

    many = [f"FILL-{i}" for i in range(250)] + ["FILL-0"]  # duplicates removed
    books = await client.get_orderbooks(many)
    assert len(books) == 250
    sizes = [len(c.request.url.params.get_list("tickers")) for c in route.calls[1:]]
    assert sizes == [100, 100, 50]


@respx.mock
async def test_get_trades_and_iter(client):
    page1 = load("trades.json")
    page2 = {"trades": page1["trades"][:2], "cursor": ""}

    def handler(request: httpx.Request):
        return httpx.Response(200, json=page2 if request.url.params.get("cursor") else page1)

    route = respx.get(f"{B}/markets/trades").mock(side_effect=handler)
    since = datetime(2026, 9, 26, 23, tzinfo=UTC)
    trades, cursor = await client.get_trades("KXBTCD-26SEP2620-T84499.99", min_ts=since, limit=12)
    assert len(trades) == 12 and cursor == page1["cursor"]
    p = route.calls.last.request.url.params
    assert p["ticker"] == "KXBTCD-26SEP2620-T84499.99" and p["min_ts"] == str(int(since.timestamp()))
    assert p["limit"] == "12" and "cursor" not in p
    all_trades = [t async for t in client.iter_trades("KXBTCD-26SEP2620-T84499.99", min_ts=since)]
    assert len(all_trades) == 14


@respx.mock
async def test_get_candlesticks(client):
    route = respx.get(f"{B}/series/KXBTC15M/markets/KXBTC15M-26SEP261845-45/candlesticks").mock(
        return_value=httpx.Response(200, json=load("candlesticks.json")))
    cs = await client.get_candlesticks("KXBTC15M", "KXBTC15M-26SEP261845-45", 1790461800,
                                       datetime.fromtimestamp(1790463000, tz=UTC), period=1)
    assert len(cs) == 16 and cs[0].price.close == D("0.45")
    p = route.calls.last.request.url.params
    assert (p["start_ts"], p["end_ts"], p["period_interval"]) == ("1790461800", "1790463000", "1")


@respx.mock
async def test_get_candlesticks_batch(client):
    candles = load("candlesticks.json")["candlesticks"]

    def handler(request: httpx.Request):
        tickers = request.url.params["market_tickers"].split(",")
        return httpx.Response(200, json={"markets": [
            {"market_ticker": t, "candlesticks": candles[:2]} for t in tickers]})

    route = respx.get(f"{B}/markets/candlesticks").mock(side_effect=handler)
    out = await client.get_candlesticks_batch(["A", "B"], 1790461800, 1790463000, period=1)
    assert set(out) == {"A", "B"} and len(out["A"]) == 2 and out["B"][0].price.close == D("0.45")
    p = route.calls.last.request.url.params
    assert p["market_tickers"] == "A,B" and p["period_interval"] == "1"
    await client.get_candlesticks_batch([f"T{i}" for i in range(150)], 0, 1)
    assert [len(c.request.url.params["market_tickers"].split(",")) for c in route.calls[1:]] == [100, 50]


@respx.mock
async def test_exchange_status(client):
    respx.get(f"{B}/exchange/status").mock(return_value=httpx.Response(200, json=load("exchange_status.json")))
    st = await client.get_exchange_status()
    assert st["trading_active"] is True and len(st["exchange_index_statuses"]) == 4


# --------------------------------------------------------------------------- errors / retries


@respx.mock
async def test_429_backoff_then_success(client, ft):
    route = respx.get(f"{B}/exchange/status").mock(side_effect=[
        httpx.Response(429, json=RATE_LIMITED),
        httpx.Response(429, json=RATE_LIMITED),
        httpx.Response(200, json={"trading_active": True}),
    ])
    assert (await client.get_exchange_status()) == {"trading_active": True}
    assert route.call_count == 3 and client.retry_count == 2
    backoffs = [s for s in ft.sleeps if s >= 0.2]
    assert len(backoffs) == 2
    # exponential with jitter in [base/2, base): base 0.5 then 1.0
    assert 0.25 <= backoffs[0] < 0.5 and 0.5 <= backoffs[1] < 1.0


@respx.mock
async def test_429_exhausted_raises(client):
    route = respx.get(f"{B}/exchange/status").mock(return_value=httpx.Response(429, json=RATE_LIMITED))
    with pytest.raises(KalshiRateLimited) as ei:
        await client.get_exchange_status()
    assert route.call_count == 5  # max 5 tries
    assert ei.value.status == 429 and "too many requests" in ei.value.message


@respx.mock
async def test_retry_after_header_honoured(client, ft):
    respx.get(f"{B}/exchange/status").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "3"}, json=RATE_LIMITED),
        httpx.Response(200, json={}),
    ])
    await client.get_exchange_status()
    assert 3.0 in ft.sleeps


@respx.mock
async def test_5xx_and_timeouts_retry(client):
    route = respx.get(f"{B}/exchange/status").mock(side_effect=[
        httpx.Response(503, text="unavailable"),
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("reset"),
        httpx.Response(200, json={"ok": 1}),
    ])
    assert await client.get_exchange_status() == {"ok": 1}
    assert route.call_count == 4


@respx.mock
async def test_404_and_400_not_retried(client):
    r404 = respx.get(f"{B}/markets/NOPE").mock(return_value=httpx.Response(
        404, json={"error": {"code": "not_found", "message": "market not found"}}))
    with pytest.raises(KalshiNotFound) as ei:
        await client.get_market("NOPE")
    assert r404.call_count == 1 and ei.value.status == 404 and "not found" in ei.value.message
    r400 = respx.get(f"{B}/markets/trades").mock(return_value=httpx.Response(400, json={"error": "bad cursor"}))
    with pytest.raises(KalshiAPIError) as ei:
        await client.get_trades(cursor="garbage")
    assert r400.call_count == 1 and ei.value.status == 400 and not isinstance(ei.value, KalshiNotFound)


@respx.mock
async def test_timeouts_exhausted(client):
    respx.get(f"{B}/exchange/status").mock(side_effect=httpx.ConnectTimeout("nope"))
    with pytest.raises(KalshiAPIError) as ei:
        await client.get_exchange_status()
    assert ei.value.status is None and "ConnectTimeout" in ei.value.message


# --------------------------------------------------------------------------- rate limiting


async def test_token_bucket_spacing_deterministic(ft):
    tb = TokenBucket(rate=3, capacity=1, clock=ft.clock, sleep=ft.sleep)
    t0 = ft.now
    for _ in range(10):
        await tb.acquire()
    # first request is free, the next 9 are spaced 1/3 s apart
    assert ft.now - t0 == pytest.approx(9 / 3)


async def test_token_bucket_burst_then_refill(ft):
    tb = TokenBucket(rate=2, capacity=3, clock=ft.clock, sleep=ft.sleep)
    waits = [await tb.acquire() for _ in range(5)]
    assert waits[:3] == [0, 0, 0] and waits[3] == pytest.approx(0.5) and waits[4] == pytest.approx(0.5)
    ft.now += 10  # idle: refills only up to capacity
    assert [await tb.acquire() for _ in range(3)] == [0, 0, 0]
    assert await tb.acquire() == pytest.approx(0.5)


async def test_token_bucket_concurrent_callers():
    import asyncio

    slept: list[float] = []

    async def record(s: float) -> None:  # callers arrive at the same instant: clock frozen
        slept.append(s)
        await asyncio.sleep(0)

    tb = TokenBucket(rate=4, capacity=1, clock=lambda: 50.0, sleep=record)
    waits = await asyncio.gather(*(tb.acquire() for _ in range(5)))
    # each caller reserves its own slot: staggered 1/rate apart, no thundering herd
    assert sorted(waits) == pytest.approx([0, 0.25, 0.5, 0.75, 1.0])
    assert sorted(slept) == pytest.approx([0.25, 0.5, 0.75, 1.0])


def test_token_bucket_validation():
    with pytest.raises(ValueError):
        TokenBucket(rate=0)


@respx.mock
async def test_client_rate_limit_applies_to_requests(ft):
    c = KalshiClient(max_rps=2, sleep=ft.sleep, clock=ft.clock)
    try:
        respx.get(f"{B}/exchange/status").mock(return_value=httpx.Response(200, json={}))
        t0 = ft.now
        for _ in range(7):
            await c.get_exchange_status()
        assert ft.now - t0 == pytest.approx(6 / 2)
        assert c.request_count == 7
    finally:
        await c.aclose()


@respx.mock
async def test_client_rate_limit_real_clock():
    c = KalshiClient(max_rps=20)
    try:
        respx.get(f"{B}/exchange/status").mock(return_value=httpx.Response(200, json={}))
        loop_t = __import__("time").monotonic
        t0 = loop_t()
        for _ in range(6):
            await c.get_exchange_status()
        assert loop_t() - t0 >= 5 / 20 * 0.9
    finally:
        await c.aclose()


async def test_context_manager():
    async with KalshiClient() as c:
        assert c.base_url == DEFAULT_BASE_URL
