"""Model parsing against real (trimmed) API responses captured 2026-09-26 in tests/fixtures/."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from kalshibot.fees import resolve_fee_params
from kalshibot.kalshi.models import (
    Candle,
    Event,
    Level,
    Market,
    Orderbook,
    Series,
    Trade,
    parse_price_ranges,
    parse_ts,
    series_from_event_ticker,
)
from kalshibot.money import DEFAULT_PRICE_RANGES, ONE, ZERO, D, PriceRange

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


def test_fixtures_are_small():
    for p in FIXTURES.glob("*.json"):
        assert p.stat().st_size < 1_000_000, p


class TestHelpers:
    def test_parse_ts(self):
        assert parse_ts("2026-09-26T23:04:12.72291Z") == datetime(2026, 9, 26, 23, 4, 12, 722910, tzinfo=UTC)
        assert parse_ts("0001-01-01T00:00:00Z") is None
        assert parse_ts("") is None and parse_ts(None) is None
        assert parse_ts(1790461860) == datetime(2026, 9, 26, 22, 31, tzinfo=UTC)
        assert parse_ts("2026-09-26T19:00:00-04:00") == datetime(2026, 9, 26, 23, tzinfo=UTC)

    def test_price_ranges(self):
        raw = [{"end": "0.1000", "start": "0.0000", "step": "0.0010"},
               {"end": "1.0000", "start": "0.9000", "step": "0.0010"},
               {"end": "0.9000", "start": "0.1000", "step": "0.0100"}]
        rs = parse_price_ranges(raw)
        assert [r.start for r in rs] == [D("0"), D("0.1"), D("0.9")]
        assert rs[1] == PriceRange(D("0.1"), D("0.9"), D("0.01"))
        assert parse_price_ranges(None) == DEFAULT_PRICE_RANGES
        assert parse_price_ranges([{"start": "x"}]) == DEFAULT_PRICE_RANGES

    def test_series_from_event(self):
        assert series_from_event_ticker("KXBTCD-26SEP2620") == "KXBTCD"
        assert series_from_event_ticker("") == ""


class TestMarket:
    def test_list_page(self):
        d = load("markets_page.json")
        ms = [Market.from_api(m) for m in d["markets"]]
        assert len(ms) == 3
        usl, nfl, btc = ms

        assert usl.ticker == "KXUSLTOTAL-26SEP26IELMIA-7"
        assert usl.event_ticker == "KXUSLTOTAL-26SEP26IELMIA"
        assert usl.series_ticker == "KXUSLTOTAL"  # derived: not in the payload
        assert usl.status == "active" and usl.is_open
        assert usl.market_type == "binary"
        assert (usl.yes_bid, usl.yes_ask, usl.no_bid, usl.no_ask) == (D("0.05"), D("0.82"), D("0.18"), D("0.95"))
        assert usl.yes_bid_size == D("30.00") and usl.yes_ask_size == D("3.00")
        assert usl.spread == D("0.77")
        assert usl.mid == D("0.435")
        assert usl.last_price is None  # 0.0000 = never traded
        assert usl.floor_strike == D("6.5") and usl.strike_type == "greater"
        assert usl.can_close_early is True
        assert usl.close_time == datetime(2026, 9, 28, 23, tzinfo=UTC)
        assert usl.expected_expiration_time == datetime(2026, 9, 27, 5, tzinfo=UTC)
        assert usl.result == "" and usl.settlement_value is None
        assert usl.price_ranges == DEFAULT_PRICE_RANGES
        assert usl.exchange_index == 0
        assert usl.rules_primary.startswith("If over 6.5 goals")
        assert usl.raw is d["markets"][0]

    def test_empty_book_sentinels_normalized(self):
        # live payload: yes_bid 0, yes_ask 0, no_bid 1, no_ask 1 for an empty book
        nfl = Market.from_api(load("markets_page.json")["markets"][1])
        assert nfl.raw["yes_ask_dollars"] == "0.0000" and nfl.raw["no_bid_dollars"] == "1.0000"
        assert (nfl.yes_bid, nfl.yes_ask, nfl.no_bid, nfl.no_ask) == (None, None, None, None)
        assert nfl.mid is None and nfl.spread is None
        assert nfl.yes_bid_size == ZERO
        assert nfl.custom_strike == {"football_player": "9490b862-60b2-4b8a-8ddc-f2613b5e1fb6"}

    def test_tapered_ticks(self):
        btc = Market.from_api(load("markets_page.json")["markets"][2])
        assert btc.price_level_structure == "tapered_deci_cent"
        assert btc.tick_at(D("0.05")) == D("0.001")
        assert btc.tick_at(D("0.50")) == D("0.01")
        assert btc.tick_at(D("0.95")) == D("0.001")
        assert btc.round_price(0.0543) == D("0.054")
        assert btc.round_price(0.5555) == D("0.56")
        assert btc.round_price(1.3) == D("0.999")
        assert btc.is_valid_price(D("0.054")) and not btc.is_valid_price(D("0.555"))

    def test_single_market_response(self):
        m = Market.from_api(load("market_single.json"))  # {"market": {...}}
        assert m.ticker == "KXBTCD-26SEP2620-T84499.99"
        assert m.series_ticker == "KXBTCD"
        assert m.yes_bid is not None and m.yes_ask is not None
        assert m.yes_ask == ONE - m.no_bid

    def test_settled_market(self):
        m = Market.from_api(load("market_settled.json")["market"])
        assert m.status == "finalized" and m.is_final and m.is_determined and not m.is_open
        assert m.result == "no"
        assert m.settlement_value == ZERO
        assert m.settlement_ts == datetime(2026, 9, 26, 22, 45, 6, 498770, tzinfo=UTC)
        assert m.expiration_value == "84275.12"
        assert (m.yes_bid, m.yes_ask, m.no_bid, m.no_ask) == (None, None, None, None)
        assert m.payout_per_contract("yes") == ZERO
        assert m.payout_per_contract("no") == ONE
        assert m.volume == D("2611281.33")
        assert not m.is_tradable(datetime(2026, 9, 26, 22, 40, tzinfo=UTC))

    def test_is_tradable(self):
        usl = Market.from_api(load("markets_page.json")["markets"][0])
        assert usl.is_tradable(datetime(2026, 9, 27, tzinfo=UTC))
        assert not usl.is_tradable(datetime(2026, 9, 29, tzinfo=UTC))

    def test_scalar_payout(self):
        raw = dict(load("market_settled.json")["market"], result="scalar", settlement_value_dollars="0.3700")
        m = Market.from_api(raw)
        assert m.payout_per_contract("yes") == D("0.37")
        assert m.payout_per_contract("no") == D("0.63")

    def test_legacy_cent_fields(self):
        m = Market.from_api({"ticker": "X-1", "event_ticker": "X-1", "yes_bid": 42, "yes_ask": 45,
                             "no_bid": 55, "no_ask": 58, "volume": 10, "status": "active"})
        assert (m.yes_bid, m.yes_ask) == (D("0.42"), D("0.45"))
        assert m.volume == D(10)

    def test_immutable(self):
        m = Market.from_api(load("market_single.json"))
        with pytest.raises(AttributeError):
            m.ticker = "nope"  # type: ignore[misc]


class TestEvent:
    def test_events_list_with_nested_markets(self):
        d = load("events_nested.json")
        e = Event.from_api(d["events"][0])
        assert e.event_ticker == "KXBTCD-26SEP2620"
        assert e.series_ticker == "KXBTCD"
        assert e.category == "Crypto"
        assert e.mutually_exclusive is False
        assert e.collateral_return_type == "DIRECNET"
        assert e.exchange_index == 2
        assert e.fee_type_override is None and e.fee_multiplier_override is None
        assert len(e.markets) == 3
        assert all(m.event_ticker == e.event_ticker and m.series_ticker == "KXBTCD" for m in e.markets)

    def test_get_event_response_shape(self):
        d = load("event_single.json")  # {"event": {...}, "markets": [...]}
        e = Event.from_api(d)
        assert e.event_ticker == "KXUSLTOTAL-26SEP26IELMIA"
        assert e.series_ticker == "KXUSLTOTAL"
        assert e.title == "Indy Eleven vs Miami: Total Goals"
        assert e.sub_title == "IEL vs MIA (Sep 26)"
        assert e.collateral_return_type == ""
        assert len(e.markets) == 3
        assert {m.event_ticker for m in e.markets} == {e.event_ticker}

    def test_fee_override(self):
        s = Series.from_api(load("series_half_multiplier.json"))
        raw = dict(load("event_single.json")["event"], fee_type_override="quadratic_with_maker_fees",
                   fee_multiplier_override=1)
        e = Event.from_api(raw)
        assert e.fee_type_override == "quadratic_with_maker_fees"
        assert e.fee_multiplier_override == D(1)
        assert resolve_fee_params(s) == ("quadratic_with_maker_fees", D("0.5"))
        assert resolve_fee_params(s, e) == ("quadratic_with_maker_fees", D(1))


class TestSeries:
    @pytest.mark.parametrize(
        "name,ticker,fee_type,mult",
        [
            ("series_quadratic.json", "KXBTC15M", "quadratic", "1"),
            ("series_maker_fees.json", "KXNBAGAME", "quadratic_with_maker_fees", "1"),
            ("series_half_multiplier.json", "KXMLBGAME", "quadratic_with_maker_fees", "0.5"),
        ],
    )
    def test_fee_fields(self, name, ticker, fee_type, mult):
        s = Series.from_api(load(name))
        assert s.ticker == ticker
        assert s.fee_type == fee_type
        assert s.fee_multiplier == D(mult)
        assert isinstance(s.fee_multiplier, Decimal)

    def test_other_fields(self):
        s = Series.from_api(load("series_quadratic.json"))
        assert s.title == "Bitcoin price up down"
        assert s.category == "Crypto"
        assert s.frequency == "fifteen_min"
        assert s.tags == ("BTC", "15 min")
        assert s.exchange_index == 2


class TestOrderbook:
    def test_single_book_best_first(self):
        d = load("orderbook.json")
        ob = Orderbook.from_api(d, ticker="KXBTCD-26SEP2620-T84499.99")
        # API sends ascending; we store best-first (descending)
        assert d["orderbook_fp"]["yes_dollars"][-1] == ["0.1200", "1090.25"]
        assert ob.yes_bids[0] == Level(D("0.12"), D("1090.25"))
        assert ob.no_bids[0] == Level(D("0.87"), D("212.74"))
        assert [lv.price for lv in ob.yes_bids] == sorted((lv.price for lv in ob.yes_bids), reverse=True)
        assert len(ob.yes_bids) == 8 and len(ob.no_bids) == 8
        assert ob.best_yes_bid == D("0.12")
        assert ob.best_no_bid == D("0.87")
        assert ob.best_yes_ask == D("0.13")
        assert ob.best_no_ask == D("0.88")
        assert ob.spread == D("0.01")
        assert ob.mid == D("0.125")

    def test_derived_asks(self):
        ob = Orderbook.from_api(load("orderbook.json"), ticker="T")
        assert ob.yes_asks[0] == Level(D("0.13"), D("212.74"))
        assert [lv.price for lv in ob.yes_asks] == sorted(lv.price for lv in ob.yes_asks)  # ascending
        assert ob.no_asks[0] == Level(D("0.88"), D("1090.25"))
        assert ob.asks("yes") == ob.yes_asks and ob.bids("no") == ob.no_bids
        assert ob.best_ask("no") == Level(D("0.88"), D("1090.25"))
        assert ob.best_bid("yes") == Level(D("0.12"), D("1090.25"))
        assert ob.size_at("yes", D("0.13"), book="ask") == D("212.74")
        assert ob.size_at("yes", D("0.11")) == D("1564.00")
        assert ob.size_at("yes", D("0.50")) == ZERO

    def test_batch_books(self):
        d = load("orderbooks_batch.json")
        books = {b.ticker: b for b in (Orderbook.from_api(e) for e in d["orderbooks"])}
        full = books["KXBTCD-26SEP2620-T84499.99"]
        assert full.best_yes_bid == D("0.11") and full.best_no_bid == D("0.88")
        empty = books["KXNFLCAREERRECYDS-MEVANS-22896"]
        assert empty.is_empty
        assert empty.best_yes_bid is None and empty.best_yes_ask is None and empty.mid is None
        assert empty.yes_asks == () and empty.best_ask("yes") is None

    def test_empty_book(self):
        ob = Orderbook.from_api(load("orderbook_empty.json"), ticker="X")
        assert ob.is_empty and ob.ticker == "X"

    def test_null_sides_and_legacy(self):
        ob = Orderbook.from_api({"orderbook_fp": {"yes_dollars": None, "no_dollars": [["0.40", "5.00"]]}}, "T")
        assert ob.yes_bids == () and ob.best_yes_ask == D("0.60")
        legacy = Orderbook.from_api({"orderbook": {"yes": [[40, 10], [42, 3]], "no": None}}, "T")
        assert legacy.yes_bids == (Level(D("0.42"), D(3)), Level(D("0.40"), D(10)))

    def test_from_levels(self):
        ob = Orderbook.from_levels("T", yes_bids=[("0.40", 5), ("0.45", 1)], no_bids=[(D("0.5"), D(2))])
        assert ob.best_yes_bid == D("0.45") and ob.best_yes_ask == D("0.5")

    def test_ts(self):
        ts = datetime(2026, 9, 26, tzinfo=UTC)
        assert Orderbook.from_api(load("orderbook.json"), "T", ts=ts).ts == ts


class TestTrade:
    def test_trades(self):
        d = load("trades.json")
        trades = [Trade.from_api(t) for t in d["trades"]]
        assert len(trades) == 12
        t = trades[0]
        assert t.ticker == "KXBTCD-26SEP2620-T84499.99"
        assert t.count == D("113.90")
        assert (t.yes_price, t.no_price) == (D("0.13"), D("0.87"))
        assert t.yes_price + t.no_price == ONE
        assert t.taker_side == "no" and t.taker_book_side == "ask"
        assert t.is_block_trade is False
        assert t.ts == datetime(2026, 9, 26, 23, 4, 12, 722910, tzinfo=UTC)
        assert t.price("no") == D("0.87")
        assert all(a.ts >= b.ts for a, b in zip(trades, trades[1:], strict=False))  # newest first
        assert d["cursor"]

    def test_prefers_outcome_side(self):
        t = Trade.from_api({"trade_id": "x", "ticker": "T", "count_fp": "1.00", "yes_price_dollars": "0.3",
                            "taker_outcome_side": "yes", "taker_side": "no", "created_time": "2026-01-01T00:00:00Z"})
        assert t.taker_side == "yes" and t.no_price == D("0.7")


class TestCandle:
    def test_live_candles(self):
        d = load("candlesticks.json")
        cs = [Candle.from_api(c) for c in d["candlesticks"]]
        assert len(cs) == 16
        c0 = cs[0]
        assert c0.end_ts == datetime(2026, 9, 26, 22, 31, tzinfo=UTC)
        assert (c0.price.open, c0.price.high, c0.price.low, c0.price.close) == (
            D("0.47"), D("0.48"), D("0.32"), D("0.45"))
        assert c0.price_mean == D("0.3929")
        assert c0.volume == D("226577.15") and c0.open_interest == D("113716.49")
        # yes_ask open/high were 1.0000 (empty ask side) -> None
        assert c0.yes_ask.open is None and c0.yes_ask.high is None
        assert c0.yes_ask.low == D("0.34") and c0.yes_ask.close == D("0.46")
        assert c0.yes_bid.low == D("0.001") and c0.yes_bid.close == D("0.45")

    def test_no_trade_period(self):
        last = Candle.from_api(load("candlesticks.json")["candlesticks"][-1])
        assert last.price.open is None and last.price.close is None and last.price_mean is None
        assert last.price_previous == D("0.001")
        assert last.volume == ZERO
        assert last.yes_bid.close is None  # 0.0000 bid sentinel
        assert last.yes_ask.close is None  # 1.0000 ask sentinel

    def test_historical_shape(self):
        # /historical/markets/{t}/candlesticks uses unsuffixed keys (docs/kalshi_api_notes.md §4.2)
        c = Candle.from_api({
            "end_period_ts": 1785196800, "open_interest": "23905.92", "volume": "21506.82",
            "price": {"open": "0.8500", "high": "0.9900", "low": "0.5200", "close": "0.9900",
                      "mean": "0.8855", "previous": "0.8600"},
            "yes_ask": {"open": "0.8500", "high": "1.0000", "low": "0.5500", "close": "1.0000"},
            "yes_bid": {"open": "0.8100", "high": "0.9900", "low": "0.0000", "close": "0.0000"}})
        assert c.volume == D("21506.82") and c.open_interest == D("23905.92")
        assert c.price.close == D("0.99") and c.price_mean == D("0.8855") and c.price_previous == D("0.86")
        assert c.yes_ask.close is None and c.yes_bid.low is None and c.yes_bid.open == D("0.81")
