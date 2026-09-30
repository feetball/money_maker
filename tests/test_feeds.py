"""CryptoSpotFeed (Coinbase primary, Kraken fallback, TTL cache) and FeedRegistry; no network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from kalshibot.feeds import FeedRegistry, build_feeds
from kalshibot.feeds.crypto import CryptoSpotFeed, FeedError

NOW = datetime(2026, 9, 26, 12, 30, 20, tzinfo=UTC)


class Mono:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class Exchange:
    """Mock Coinbase + Kraken public endpoints."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.coinbase_down = False
        self.kraken_down = False

    def handler(self, req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        self.calls.append(f"{host}{path}")
        if "coinbase" in host:
            if self.coinbase_down:
                return httpx.Response(503, json={"message": "down"})
            if path.endswith("/ticker"):
                sym = path.split("/")[2].split("-")[0]
                price = {"BTC": "65000.50", "ETH": "2500.25"}[sym]
                return httpx.Response(200, json={"price": price, "bid": "64999.00", "ask": "65001.00",
                                                 "time": "2026-09-26T12:30:19.5Z", "volume": "1"})
            if path.endswith("/candles"):
                start = datetime.fromisoformat(req.url.params["start"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(req.url.params["end"].replace("Z", "+00:00"))
                rows = []
                t = end - timedelta(minutes=1)
                while t >= start:
                    ts = int(t.timestamp())
                    rows.append([ts, 99.0, 101.0, 100.0, 100.5, 3.0])  # newest first
                    t -= timedelta(minutes=1)
                return httpx.Response(200, json=rows)
        if "kraken" in host:
            if self.kraken_down:
                return httpx.Response(200, json={"error": ["EService:Unavailable"], "result": {}})
            if path.endswith("/Ticker"):
                assert req.url.params["pair"] in ("XBTUSD", "ETHUSD")
                return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": {
                    "a": ["65010.0", "1", "1.000"], "b": ["65000.0", "1", "1.000"], "c": ["65005.0", "0.1"]}}})
            if path.endswith("/OHLC"):
                base = int((NOW - timedelta(minutes=3)).replace(second=0).timestamp())
                rows = [[base + 60 * i, "1.0", "2.0", "0.5", "1.5", "1.2", "10.0", 5] for i in range(4)]
                return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": rows, "last": base}})
        return httpx.Response(404)


def make_feed(ex: Exchange, **kw: Any) -> tuple[CryptoSpotFeed, Mono]:
    mono = Mono()
    feed = CryptoSpotFeed(transport=httpx.MockTransport(ex.handler), clock=mono, wallclock=lambda: NOW,
                          max_rps=1000, **kw)
    return feed, mono


async def test_spot_from_coinbase_with_ttl_cache() -> None:
    ex = Exchange()
    feed, mono = make_feed(ex, ttl_s=5)
    q = await feed.spot("btc")
    assert q.source == "coinbase" and q.price == 65000.5 and q.bid == 64999.0 and q.ask == 65001.0
    assert q.mid == 65000.0
    assert q.ts == datetime(2026, 9, 26, 12, 30, 19, 500000, tzinfo=UTC)
    await feed.spot("BTC")
    assert len(ex.calls) == 1
    mono.t += 6
    assert await feed.price("BTC") == 65000.5
    assert len(ex.calls) == 2
    assert feed.last["BTC"] is not None
    await feed.aclose()


async def test_spot_falls_back_to_kraken() -> None:
    ex = Exchange()
    ex.coinbase_down = True
    feed, _ = make_feed(ex)
    q = await feed.spot("BTC")
    assert q.source == "kraken" and q.price == 65005.0 and q.bid == 65000.0 and q.ask == 65010.0
    assert any("kraken" in c for c in ex.calls)
    await feed.aclose()


async def test_spot_all_sources_down_raises_and_keeps_last() -> None:
    ex = Exchange()
    feed, mono = make_feed(ex, ttl_s=1)
    good = await feed.spot("BTC")
    ex.coinbase_down = ex.kraken_down = True
    mono.t += 2
    with pytest.raises(FeedError):
        await feed.spot("BTC")
    assert feed.last["BTC"] is good
    assert "BTC" in feed.errors
    st = feed.status()
    assert st["last"]["BTC"]["price"] == 65000.5 and "BTC" in st["errors"]
    await feed.aclose()


async def test_candles_coinbase_paged_oldest_first() -> None:
    ex = Exchange()
    feed, mono = make_feed(ex)
    cs = await feed.candles("ETH", 350)  # > 300 -> two requests
    assert len(cs) == 350
    assert sum(1 for c in ex.calls if c.endswith("/candles")) == 2
    assert all(a.ts < b.ts for a, b in zip(cs, cs[1:], strict=False))
    assert cs[-1].ts == datetime(2026, 9, 26, 12, 30, tzinfo=UTC) and not cs[-1].complete
    assert cs[-2].complete and cs[0].open == 100.0 and cs[0].close == 100.5
    n = len(ex.calls)
    await feed.candles("ETH", 350)
    assert len(ex.calls) == n  # cached
    mono.t += 31
    await feed.candles("ETH", 350)
    assert len(ex.calls) == n + 2
    await feed.aclose()


async def test_candles_kraken_fallback() -> None:
    ex = Exchange()
    ex.coinbase_down = True
    feed, _ = make_feed(ex)
    cs = await feed.candles("BTC", 3)
    assert len(cs) == 3 and cs[0].open == 1.0 and cs[0].high == 2.0 and cs[0].low == 0.5
    assert cs[0].close == 1.5 and cs[0].volume == 10.0
    await feed.aclose()


def test_pair_names() -> None:
    assert CryptoSpotFeed.coinbase_product("btc") == "BTC-USD"
    assert CryptoSpotFeed.kraken_pair("BTC") == "XBTUSD"
    assert CryptoSpotFeed.kraken_pair("ETH") == "ETHUSD"
    assert CryptoSpotFeed.kraken_pair("doge") == "XDGUSD"


async def test_registry_mapping_and_attribute_access() -> None:
    class Stub:
        closed = False

        def status(self) -> dict[str, Any]:
            return {"ok": True}

        async def aclose(self) -> None:
            self.closed = True

    stub = Stub()
    reg = FeedRegistry({"stub": stub})
    assert reg.stub is stub and reg["stub"] is stub and "stub" in reg and len(reg) == 1
    with pytest.raises(AttributeError):
        _ = reg.nope
    with pytest.raises(ValueError):
        reg.register("bad name", stub)
    assert reg.status() == {"stub": {"ok": True}}
    await reg.aclose()
    assert stub.closed
    default = build_feeds()
    assert isinstance(default.crypto, CryptoSpotFeed) and default.crypto.symbols == ["BTC", "ETH"]
    await default.aclose()
