"""Coinbase model parsing against trimmed live samples (tests/fixtures/coinbase, fetched 2026-09-27)."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from kalshibot.coinbase.models import Candle, OrderBook, Product, Stats, Ticker, Trade, dec, parse_time

FIX = Path(__file__).parent / "fixtures" / "coinbase"
D = Decimal


def load(name: str):
    return json.loads((FIX / name).read_text())


def test_fixtures_stay_small():
    assert sum(f.stat().st_size for f in FIX.iterdir()) < 200_000


# --------------------------------------------------------------------------- products


def products() -> dict[str, Product]:
    return {p.product_id: p for p in (Product.from_api(d) for d in load("products.json"))}


def test_product_btc_usd():
    p = Product.from_api(load("product_btc_usd.json"))
    assert p.product_id == "BTC-USD" and p.base_currency == "BTC" and p.quote_currency == "USD"
    assert p.base_increment == D("0.00000001") and p.quote_increment == D("0.01")
    assert p.min_market_funds == D("1")
    assert p.tradable and not p.limit_only and not p.post_only and not p.cancel_only
    assert p.raw["max_slippage_percentage"] == "0.02000000"  # original payload kept


def test_product_flags_from_live_list():
    ps = products()
    assert {"BTC-USD", "ETH-USD", "SOL-USD", "BADGER-USD", "MOVE-USD", "BAL-BTC"} <= set(ps)
    assert all(isinstance(p.base_increment, Decimal) for p in ps.values())
    badger = ps["BADGER-USD"]
    assert badger.limit_only and badger.tradable  # limit-only products still accept limit orders
    assert not ps["MOVE-USD"].tradable and ps["MOVE-USD"].status == "delisted"
    assert not ps["BAL-BTC"].tradable and ps["BAL-BTC"].trading_disabled
    assert ps["ETH-EUR"].quote_currency == "EUR"


def test_product_defaults_for_missing_fields():
    p = Product.from_api({"id": "X-USD", "status": "online"})
    assert p.base_increment == D("0.00000001") and p.quote_increment == D("0.01") and p.min_market_funds == 1
    assert p.tradable
    assert not Product.from_api({"id": "X-USD", "status": "online", "cancel_only": True}).tradable


# --------------------------------------------------------------------------- book


def test_order_book_levels_best_first():
    raw = load("book_btc_usd_l2.json")
    ob = OrderBook.from_api("BTC-USD", raw)
    assert len(ob.bids) == 15 and len(ob.asks) == 15
    assert [lv.price for lv in ob.bids] == sorted((lv.price for lv in ob.bids), reverse=True)
    assert [lv.price for lv in ob.asks] == sorted(lv.price for lv in ob.asks)
    assert ob.best_bid == D("84429.83") and ob.best_ask == D("84429.84")
    assert ob.bids[0].size == D("0.0718") and ob.bids[0].num_orders == 1
    assert ob.mid == (D("84429.83") + D("84429.84")) / 2
    assert D(0) < ob.spread_bps < D("0.01")
    assert ob.sequence == 136847894554
    assert ob.time == datetime(2026, 9, 27, 15, 57, 26, 895760, tzinfo=UTC)  # nanoseconds trimmed


def test_order_book_unsorted_and_junk_levels():
    ob = OrderBook.from_api("X", {"bids": [["1", "1", 1], ["3", "1", 2], ["2", "0", 1], ["bad", "1", 1]],
                                  "asks": [["5", "1", 1], ["4", "2", 3]]})
    assert [lv.price for lv in ob.bids] == [D(3), D(1)]  # zero size and garbage dropped, re-sorted
    assert [lv.price for lv in ob.asks] == [D(4), D(5)]
    empty = OrderBook.from_api("X", {"bids": [], "asks": []})
    assert empty.best_bid is None and empty.mid is None and empty.spread_bps is None


# --------------------------------------------------------------------------- trades


def test_trade_maker_side_semantics_against_live_quote():
    """API ``side`` is the MAKER's side: "buy" = a resting bid was hit (prints at the bid),
    "sell" = a resting ask was lifted (prints at the ask). Verified with the same-moment ticker."""
    page = load("trades_btc_usd.json")["pages"][0]["body"]
    tick = Ticker.from_api("BTC-USD", load("ticker_btc_usd.json"))
    trades = [Trade.from_api("BTC-USD", r) for r in page]
    buys = [t for t in trades if t.maker_side == "buy"]
    sells = [t for t in trades if t.maker_side == "sell"]
    assert buys and sells
    assert all(t.price == tick.bid for t in buys)
    assert all(t.price == tick.ask for t in sells)
    assert all(t.taker_side == "sell" for t in buys) and all(t.taker_side == "buy" for t in sells)


def test_trade_fields_and_order():
    page = load("trades_btc_usd.json")["pages"][0]["body"]
    trades = [Trade.from_api("BTC-USD", r) for r in page]
    t = trades[0]
    assert t.trade_id == 1099147579 and t.product_id == "BTC-USD"
    assert t.price == D("84429.83000000") and t.size == D("0.00000008")
    assert t.time == datetime(2026, 9, 27, 15, 57, 28, 610277, tzinfo=UTC)
    ids = [x.trade_id for x in trades]
    assert ids == sorted(ids, reverse=True)  # the API sends newest first
    times = [x.time for x in trades]
    assert times == sorted(times, reverse=True)


# --------------------------------------------------------------------------- candles


def test_candles_newest_first_and_exact_decimals():
    rows = load("candles_btc_usd_1h.json")
    cs = [Candle.from_api("BTC-USD", 3600, r) for r in rows]
    assert len(cs) == 25  # a 24 h window is inclusive at both ends
    assert [c.start for c in cs] == sorted((c.start for c in cs), reverse=True)
    first = cs[0]
    assert first.start == datetime(2026, 9, 21, tzinfo=UTC)  # time = bar OPEN
    assert first.end == first.start + timedelta(hours=1)
    assert (first.low, first.high, first.open, first.close) == (D("81147.06"), D("81837"), D("81160.33"),
                                                                D("81590.55"))
    assert first.volume == D("392.52259658")
    for c in cs:
        assert c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high
        assert c.granularity_s == 3600


def test_daily_candles_fixture_is_contiguous():
    cs = [Candle.from_api("BTC-USD", 86400, r) for r in load("candles_btc_usd_1d_301.json")]
    assert len(cs) == 301
    starts = sorted(c.start for c in cs)
    assert all(b - a == timedelta(days=1) for a, b in zip(starts, starts[1:], strict=False))


# --------------------------------------------------------------------------- ticker / stats / helpers


def test_ticker_and_stats():
    tk = Ticker.from_api("BTC-USD", load("ticker_btc_usd.json"))
    assert tk.bid == D("84429.83") and tk.ask == D("84429.84") and tk.price == D("84429.83")
    assert tk.volume_24h == D("2398.55863983")
    assert tk.time == datetime(2026, 9, 27, 15, 57, 28, 906401, tzinfo=UTC)
    st = Stats.from_api("BTC-USD", load("stats_btc_usd.json"))
    assert st.open == D("84146.86") and st.high == D("85158.5") and st.low == D("83811.99")
    assert st.last == D("84435.15") and st.volume_24h == D("2398.12214278")
    assert st.volume_30d == D("177144.23704693")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-27T15:57:28.906401099Z", datetime(2026, 9, 27, 15, 57, 28, 906401, tzinfo=UTC)),
        ("2026-09-27T15:57:32.268Z", datetime(2026, 9, 27, 15, 57, 32, 268000, tzinfo=UTC)),
        ("2026-09-27T15:57:32Z", datetime(2026, 9, 27, 15, 57, 32, tzinfo=UTC)),
        ("2026-09-27T15:57:32", datetime(2026, 9, 27, 15, 57, 32, tzinfo=UTC)),
        (1789948800, datetime(2026, 9, 21, tzinfo=UTC)),
        (None, None),
        ("", None),
    ],
)
def test_parse_time(value, expected):
    assert parse_time(value) == expected


def test_dec():
    assert dec("0.1") == D("0.1") and dec(81147.06) == D("81147.06")
    assert dec("") is None and dec(None, D(0)) == 0 and dec("abc") is None
