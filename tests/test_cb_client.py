"""CoinbaseClient tests with respx-mocked HTTP (no network); fixtures are trimmed live samples."""

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from kalshibot.coinbase.client import (
    DEFAULT_BASE_URL,
    MAX_CANDLES_PER_REQUEST,
    CoinbaseAPIError,
    CoinbaseClient,
    CoinbaseNotFound,
    CoinbaseRateLimited,
)
from kalshibot.coinbase.models import Candle, OrderBook, Product, Stats, Ticker, Trade

FIX = Path(__file__).parent / "fixtures" / "coinbase"
B = DEFAULT_BASE_URL
D = Decimal


def load(name: str):
    return json.loads((FIX / name).read_text())


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
    c = CoinbaseClient(max_rps=1000, sleep=ft.sleep, clock=ft.clock)
    yield c
    await c.aclose()


def test_defaults():
    c = CoinbaseClient()
    assert c.base_url == "https://api.exchange.coinbase.com"
    assert c.limiter.rate == 3 and c.max_tries == 5
    assert c._http.timeout.read == 10


def test_from_settings():
    from kalshibot.coinbase.config import CoinbaseSettings

    c = CoinbaseClient.from_settings(CoinbaseSettings(max_rps=1.5, timeout=4, base_url="http://127.0.0.1:8779/"))
    assert c.base_url == "http://127.0.0.1:8779" and c.limiter.rate == 1.5 and c._http.timeout.read == 4


# --------------------------------------------------------------------------- endpoints


@respx.mock
async def test_get_products_parses_and_skips_bad_rows(client):
    rows = load("products.json") + [{"no_id": True}, "junk", {"id": "BAD-USD", "base_increment": object()}]
    route = respx.get(f"{B}/products").mock(return_value=httpx.Response(200, json=rows[:-1]))
    ps = await client.get_products()
    assert route.call_count == 1
    assert all(isinstance(p, Product) for p in ps)
    assert [p.product_id for p in ps] == [r["id"] for r in load("products.json")]
    req = route.calls.last.request
    assert req.headers["User-Agent"].startswith("kalshibot-paper")  # Coinbase rejects requests without one
    assert "authorization" not in {k.lower() for k in req.headers} and not any(k.lower().startswith("cb-access")
                                                                               for k in req.headers)


@respx.mock
async def test_get_product_book_ticker_stats_time(client):
    respx.get(f"{B}/products/BTC-USD").mock(return_value=httpx.Response(200, json=load("product_btc_usd.json")))
    book = respx.get(f"{B}/products/BTC-USD/book").mock(
        return_value=httpx.Response(200, json=load("book_btc_usd_l2.json")))
    respx.get(f"{B}/products/BTC-USD/ticker").mock(return_value=httpx.Response(200, json=load("ticker_btc_usd.json")))
    respx.get(f"{B}/products/BTC-USD/stats").mock(return_value=httpx.Response(200, json=load("stats_btc_usd.json")))
    respx.get(f"{B}/time").mock(return_value=httpx.Response(200, json=load("time.json")))

    p = await client.get_product("BTC-USD")
    assert p.product_id == "BTC-USD" and p.tradable
    ob = await client.get_book("BTC-USD")
    assert isinstance(ob, OrderBook) and ob.best_bid == D("84429.83") and ob.product_id == "BTC-USD"
    assert book.calls.last.request.url.params["level"] == "2"
    await client.get_book("BTC-USD", level=1)
    assert book.calls.last.request.url.params["level"] == "1"
    tk = await client.get_ticker("BTC-USD")
    assert isinstance(tk, Ticker) and tk.ask == D("84429.84")
    st = await client.get_stats("BTC-USD")
    assert isinstance(st, Stats) and st.volume_30d == D("177144.23704693")
    assert await client.get_time() == datetime(2026, 9, 27, 15, 57, 32, 268000, tzinfo=UTC)
    assert client.success_count == 6 and client.last_success_at is not None


@respx.mock
async def test_unexpected_json_shape_is_an_error(client):
    respx.get(f"{B}/products").mock(return_value=httpx.Response(200, json={"message": "?"}))
    respx.get(f"{B}/products/BTC-USD/ticker").mock(return_value=httpx.Response(200, json=[1, 2]))
    respx.get(f"{B}/products/BTC-USD/stats").mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(CoinbaseAPIError, match="array"):
        await client.get_products()
    with pytest.raises(CoinbaseAPIError, match="object"):
        await client.get_ticker("BTC-USD")
    with pytest.raises(CoinbaseAPIError, match="invalid JSON"):
        await client.get_stats("BTC-USD")
    assert "invalid JSON" in (client.last_error or "")


# --------------------------------------------------------------------------- trades + pagination


def trade_pages():
    return load("trades_btc_usd.json")["pages"]


def tape_handler(tape: list[dict]):
    """Simulate GET /trades over ``tape`` (newest first): ``after`` = older than that id;
    cb-after = the oldest id on the page, cb-before = the newest."""

    def handler(request: httpx.Request) -> httpx.Response:
        limit = int(request.url.params.get("limit", 100))
        after = request.url.params.get("after")
        rows = [r for r in tape if after is None or r["trade_id"] < int(after)][:limit]
        headers = {}
        if rows:
            headers = {"cb-after": str(rows[-1]["trade_id"]), "cb-before": str(rows[0]["trade_id"])}
        return httpx.Response(200, json=rows, headers=headers)

    return handler


@respx.mock
async def test_get_trades_page_and_cursor(client):
    p1, p2 = trade_pages()
    route = respx.get(f"{B}/products/BTC-USD/trades").mock(side_effect=[
        httpx.Response(200, json=p1["body"], headers=p1["headers"]),
        httpx.Response(200, json=p2["body"], headers=p2["headers"]),
        httpx.Response(200, json=[]),
    ])
    trades, cur = await client.get_trades("BTC-USD", limit=5)
    assert [t.trade_id for t in trades] == [1099147579, 1099147578, 1099147577, 1099147576, 1099147575]
    assert all(isinstance(t, Trade) for t in trades)
    assert cur == "1099147575" == p1["headers"]["cb-after"]
    assert route.calls[0].request.url.params["limit"] == "5"
    assert "after" not in route.calls[0].request.url.params

    trades2, cur2 = await client.get_trades("BTC-USD", after=cur, limit=5)
    assert route.calls[1].request.url.params["after"] == "1099147575"
    assert [t.trade_id for t in trades2] == list(range(1099147574, 1099147569, -1))
    assert cur2 == "1099147570"

    empty, cur3 = await client.get_trades("BTC-USD", after="1")
    assert empty == [] and cur3 is None


@respx.mock
async def test_get_trades_clamps_limit(client):
    route = respx.get(f"{B}/products/BTC-USD/trades").mock(return_value=httpx.Response(200, json=[]))
    await client.get_trades("BTC-USD", limit=5000)
    assert route.calls.last.request.url.params["limit"] == "1000"
    await client.get_trades("BTC-USD", limit=0)
    assert route.calls.last.request.url.params["limit"] == "1"


@respx.mock
async def test_trades_since_paginates_via_cb_after(client):
    tape = trade_pages()[0]["body"] + trade_pages()[1]["body"]  # ids 579..570, newest first
    route = respx.get(f"{B}/products/BTC-USD/trades").mock(side_effect=tape_handler(tape))
    out = await client.trades_since("BTC-USD", 1099147571, limit=5)
    assert [t.trade_id for t in out] == list(range(1099147572, 1099147580))  # oldest first, > since
    assert route.call_count == 2
    assert route.calls[1].request.url.params["after"] == "1099147575"


@respx.mock
async def test_trades_since_stops_on_first_page_when_caught_up(client):
    tape = trade_pages()[0]["body"] + trade_pages()[1]["body"]
    route = respx.get(f"{B}/products/BTC-USD/trades").mock(side_effect=tape_handler(tape))
    assert await client.trades_since("BTC-USD", 1099147579, limit=5) == []
    assert [t.trade_id for t in await client.trades_since("BTC-USD", 1099147577, limit=5)] == [1099147578,
                                                                                                1099147579]
    assert route.call_count == 2  # one page each


@respx.mock
async def test_trades_since_contiguous_ids_need_no_extra_page(client):
    tape = trade_pages()[0]["body"] + trade_pages()[1]["body"]  # ids 579..570
    route = respx.get(f"{B}/products/BTC-USD/trades").mock(side_effect=tape_handler(tape))
    out = await client.trades_since("BTC-USD", 1099147574, limit=5)  # page 1 = 579..575 = since+1..
    assert [t.trade_id for t in out] == list(range(1099147575, 1099147580))
    assert route.call_count == 1


@respx.mock
async def test_trades_since_none_returns_newest_page(client):
    tape = trade_pages()[0]["body"] + trade_pages()[1]["body"]
    route = respx.get(f"{B}/products/BTC-USD/trades").mock(side_effect=tape_handler(tape))
    out = await client.trades_since("BTC-USD", None, limit=5)
    assert [t.trade_id for t in out] == list(range(1099147575, 1099147580))
    assert route.call_count == 1


@respx.mock
async def test_trades_since_respects_max_pages(client, caplog):
    tape = [{"trade_id": i, "side": "buy", "size": "0.1", "price": "100", "time": "2026-09-27T00:00:00Z"}
            for i in range(100, 0, -1)]
    route = respx.get(f"{B}/products/BTC-USD/trades").mock(side_effect=tape_handler(tape))
    with caplog.at_level(logging.WARNING, logger="kalshibot.coinbase.client"):
        out = await client.trades_since("BTC-USD", 10, max_pages=3, limit=10)
    assert route.call_count == 3
    assert [t.trade_id for t in out] == list(range(71, 101))
    assert any("stopped after 3 pages" in r.getMessage() for r in caplog.records)


@respx.mock
async def test_trades_since_dedupes_overlap_and_stops_on_repeated_cursor(client):
    page = trade_pages()[0]
    # a misbehaving API that returns the same page + cursor forever
    route = respx.get(f"{B}/products/BTC-USD/trades").mock(
        return_value=httpx.Response(200, json=page["body"], headers=page["headers"]))
    out = await client.trades_since("BTC-USD", 1000, max_pages=10, limit=5)
    assert [t.trade_id for t in out] == list(range(1099147575, 1099147580))
    assert route.call_count == 2  # second page repeats the cursor -> stop


@respx.mock
async def test_trades_since_stops_when_tape_ends(client):
    tape = trade_pages()[0]["body"]
    route = respx.get(f"{B}/products/BTC-USD/trades").mock(side_effect=tape_handler(tape))
    out = await client.trades_since("BTC-USD", 5, limit=5)
    assert len(out) == 5 and route.call_count == 2  # 2nd page empty


# --------------------------------------------------------------------------- errors, retries, rate limit


@respx.mock
async def test_429_backoff_then_success(client, ft):
    route = respx.get(f"{B}/products/BTC-USD/ticker").mock(side_effect=[
        httpx.Response(429, json={"message": "Public rate limit exceeded"}),
        httpx.Response(429, json={"message": "Public rate limit exceeded"}),
        httpx.Response(200, json=load("ticker_btc_usd.json")),
    ])
    tk = await client.get_ticker("BTC-USD")
    assert tk.bid == D("84429.83")
    assert route.call_count == 3 and client.retry_count == 2 and client.request_count == 3
    backoffs = [s for s in ft.sleeps if s > 0.01]  # ignore the rate limiter's tiny waits
    assert len(backoffs) == 2
    assert 0.25 <= backoffs[0] < 0.5 and 0.5 <= backoffs[1] < 1.0  # exponential with jitter


@respx.mock
async def test_retry_after_is_honoured(client, ft):
    respx.get(f"{B}/time").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "3"}, json={"message": "slow down"}),
        httpx.Response(200, json=load("time.json")),
    ])
    await client.get_time()
    assert 3.0 in ft.sleeps


@respx.mock
async def test_429_exhausted_raises_rate_limited(client, ft):
    route = respx.get(f"{B}/time").mock(return_value=httpx.Response(429, json={"message": "Public rate limit exceeded"}))
    with pytest.raises(CoinbaseRateLimited, match="Public rate limit exceeded"):
        await client.get_time()
    assert route.call_count == 5 and client.retry_count == 4
    assert "429" in (client.last_error or "") and client.last_error_at is not None


@respx.mock
async def test_5xx_and_timeouts_retry(client):
    route = respx.get(f"{B}/time").mock(side_effect=[
        httpx.Response(503, text="unavailable"),
        httpx.ConnectTimeout("boom"),
        httpx.ReadError("reset"),
        httpx.Response(200, json=load("time.json")),
    ])
    assert await client.get_time() is not None
    assert route.call_count == 4 and client.retry_count == 3


@respx.mock
async def test_transport_errors_exhausted(client):
    respx.get(f"{B}/time").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(CoinbaseAPIError, match="failed after 5 tries") as ei:
        await client.get_time()
    assert ei.value.status is None and not isinstance(ei.value, CoinbaseRateLimited)
    assert client.success_count == 0


@respx.mock
async def test_404_not_found_is_not_retried(client):
    route = respx.get(f"{B}/products/NOPE-USD").mock(return_value=httpx.Response(404, json=load("error_not_found.json")))
    with pytest.raises(CoinbaseNotFound) as ei:
        await client.get_product("NOPE-USD")
    assert ei.value.status == 404 and ei.value.message == "NotFound"
    assert route.call_count == 1 and client.success_count == 1  # Coinbase answered: reachable


@respx.mock
async def test_400_is_not_retried(client):
    route = respx.get(f"{B}/products/BTC-USD/book").mock(
        return_value=httpx.Response(400, json=load("error_bad_granularity.json")))
    with pytest.raises(CoinbaseAPIError, match="Unsupported granularity") as ei:
        await client.get_book("BTC-USD")
    assert ei.value.status == 400 and route.call_count == 1


@respx.mock
async def test_rate_limiter_spaces_requests(ft):
    c = CoinbaseClient(max_rps=2, sleep=ft.sleep, clock=ft.clock)
    respx.get(f"{B}/time").mock(return_value=httpx.Response(200, json=load("time.json")))
    try:
        for _ in range(5):
            await c.get_time()
    finally:
        await c.aclose()
    # burst of 1, then one request every 0.5 s
    assert sum(ft.sleeps) == pytest.approx(2.0)
    assert ft.sleeps == [pytest.approx(0.5)] * 4


# --------------------------------------------------------------------------- candles


DAILY = load("candles_btc_usd_1d_301.json")  # newest first, 2025-11-01 .. 2026-08-28


def candle_api(rows: list[list], *, extra: list[list] | None = None):
    """Simulate GET /candles: bars whose start is in [start, end] (inclusive), newest first,
    400 when more than 300 bars are requested. ``extra`` rows are appended to every response."""
    calls: list[tuple[datetime, datetime, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params
        g = int(q["granularity"])
        s = datetime.fromisoformat(q["start"].replace("Z", "+00:00"))
        e = datetime.fromisoformat(q["end"].replace("Z", "+00:00"))
        calls.append((s, e, g))
        if (e - s).total_seconds() / g + 1 > MAX_CANDLES_PER_REQUEST:
            return httpx.Response(400, json={"message": "granularity too small for the requested time range"})
        out = [r for r in rows if s.timestamp() <= r[0] <= e.timestamp()]
        out.sort(key=lambda r: r[0], reverse=True)
        return httpx.Response(200, json=out + (extra or []))

    return handler, calls


@respx.mock
async def test_candles_chunked_oldest_first(client):
    handler, calls = candle_api(DAILY)
    respx.get(f"{B}/products/BTC-USD/candles").mock(side_effect=handler)
    start, end = datetime(2025, 11, 1, tzinfo=UTC), datetime(2026, 8, 28, tzinfo=UTC)
    cs = await client.get_candles("BTC-USD", 86400, start, end)
    assert len(cs) == 301 and all(isinstance(c, Candle) for c in cs)
    assert [c.start for c in cs] == sorted(c.start for c in cs)  # oldest first
    assert cs[0].start == start and cs[-1].start == end
    assert len({c.start for c in cs}) == 301
    # two windows of <= 300 bars, contiguous, no overlap
    assert len(calls) == 2
    (s1, e1, g1), (s2, e2, _) = calls
    assert g1 == 86400
    assert s1 == start and e1 == start + timedelta(days=299)
    assert s2 == start + timedelta(days=300) and e2 == end
    # exact Decimal values from float JSON
    newest = DAILY[0]
    assert cs[-1].close == D(str(newest[4])) and cs[-1].volume == D(str(newest[5]))


@respx.mock
async def test_candles_request_params_are_iso_utc(client):
    handler, calls = candle_api(DAILY)
    route = respx.get(f"{B}/products/BTC-USD/candles").mock(side_effect=handler)
    await client.get_candles("BTC-USD", 86400, datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 3, tzinfo=UTC))
    q = route.calls.last.request.url.params
    assert q["start"] == "2026-08-01T00:00:00Z" and q["end"] == "2026-08-03T00:00:00Z" and q["granularity"] == "86400"


@respx.mock
async def test_candles_dedupe_and_filter_out_of_range_rows(client):
    stray_old = [int(datetime(2020, 1, 1, tzinfo=UTC).timestamp()), 1, 2, 1, 2, 3]
    dup = next(r for r in DAILY if r[0] == int(datetime(2026, 8, 2, tzinfo=UTC).timestamp()))
    handler, _ = candle_api(DAILY, extra=[dup, stray_old, dup])
    respx.get(f"{B}/products/BTC-USD/candles").mock(side_effect=handler)
    cs = await client.get_candles("BTC-USD", 86400, datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 5, tzinfo=UTC))
    assert [c.start.day for c in cs] == [1, 2, 3, 4, 5]


@respx.mock
async def test_candles_accept_epoch_seconds_and_align_start(client):
    handler, calls = candle_api(DAILY)
    respx.get(f"{B}/products/BTC-USD/candles").mock(side_effect=handler)
    start = datetime(2026, 8, 1, 5, 30, tzinfo=UTC).timestamp()  # mid-bar
    end = datetime(2026, 8, 3, 12, tzinfo=UTC).timestamp()
    cs = await client.get_candles("BTC-USD", 86400, start, end)
    assert calls[0][0] == datetime(2026, 8, 1, tzinfo=UTC)  # aligned down to the bar grid
    assert [c.start.day for c in cs] == [1, 2, 3]


@respx.mock
async def test_candles_hourly_window(client):
    rows = load("candles_btc_usd_1h.json")
    handler, calls = candle_api(rows)
    respx.get(f"{B}/products/BTC-USD/candles").mock(side_effect=handler)
    cs = await client.get_candles("BTC-USD", 3600, datetime(2026, 9, 20, tzinfo=UTC), datetime(2026, 9, 21, tzinfo=UTC))
    assert len(cs) == 25 and len(calls) == 1
    assert all(b.start - a.start == timedelta(hours=1) for a, b in zip(cs, cs[1:], strict=False))


@respx.mock
async def test_candles_long_hourly_range_needs_ceil_n_over_300_requests(client):
    handler, calls = candle_api([])
    respx.get(f"{B}/products/BTC-USD/candles").mock(side_effect=handler)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=1000)  # 1001 bar starts
    assert await client.get_candles("BTC-USD", 3600, start, end) == []
    assert len(calls) == 4
    for s, e, g in calls:
        assert (e - s).total_seconds() / g + 1 <= 300
    covered = sum(int((e - s).total_seconds() // 3600) + 1 for s, e, _ in calls)
    assert covered == 1001


@respx.mock
async def test_candles_closed_only_drops_in_progress_bar(client):
    now = datetime.now(UTC)
    cur = int(now.timestamp()) // 3600 * 3600
    rows = [[cur, 1, 2, 1, 2, 5], [cur - 3600, 1, 2, 1, 2, 5]]
    handler, _ = candle_api(rows)
    respx.get(f"{B}/products/BTC-USD/candles").mock(side_effect=handler)
    all_bars = await client.get_candles("BTC-USD", 3600, now - timedelta(hours=2), now)
    closed = await client.get_candles("BTC-USD", 3600, now - timedelta(hours=2), now, closed_only=True)
    assert len(all_bars) == 2 and len(closed) == 1
    assert closed[0].end <= now


async def test_candles_bad_args_make_no_request(client):
    with pytest.raises(ValueError, match="granularity"):
        await client.get_candles("BTC-USD", 1234, 0, 10)
    t = datetime(2026, 1, 2, tzinfo=UTC)
    assert await client.get_candles("BTC-USD", 3600, t, t - timedelta(days=1)) == []
    assert client.request_count == 0
