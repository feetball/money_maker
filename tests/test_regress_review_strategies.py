"""Regressions from the strategy review (2026-09-27).

* ladder_favorite decides on the research's hourly grid (first tick at/after each UTC hour), not on
  intra-hour touches of 0.97; triggers over ``max_intents_per_tick`` are carried within the slot;
* ladder_favorite caps correlated ladders (one underlying) and its total cost;
* btc15m_favorite ticks every 5 s, re-reads its series every 20 s, reports the decision lag, skips a
  decision more than 15 s late and reads the order book only after the spot;
* maker_favorite: no optimistic prior, a cap on accumulated positions + resting bids, Crypto and
  Mentions excluded.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import test_strategy_btc15m as B
import test_strategy_ladder as L
import test_strategy_maker as M
from conftest import FakeKalshiClient

from kalshibot.api.server import build_services
from kalshibot.config import Settings
from kalshibot.money import D
from kalshibot.paper.models import Position
from kalshibot.strategies.btc15m_favorite import Btc15mFavorite
from kalshibot.strategies.ladder_favorite import LadderFavorite, underlying_group
from kalshibot.strategies import maker_favorite as maker_module
from kalshibot.strategies.maker_favorite import MakerFavoriteHarvest

H = timedelta(hours=1)


# --------------------------------------------------------------------------- ladder: decision grid (1)


async def test_ladder_decides_only_at_the_hour_boundary_not_on_intra_hour_touches() -> None:
    s = LadderFavorite()
    ctx = L.FakeCtx(now=L.NOW + timedelta(seconds=20))  # the first tick of the 14:00 slot
    m = ctx.add(L.market("KXWTI-26SEP28-T70", bid="0.96", ask="0.98"), bk=L.book("KXWTI-26SEP28-T70", "0.96", "0.98"))
    assert await L.run(s, ctx) == {}  # below 0.97 at the boundary
    # 14:20 the bid touches 0.97 inside the hour: the research's hourly close never saw it
    ctx.now = L.NOW + timedelta(minutes=20)
    ctx.markets[m.ticker] = L.market(m.ticker, bid="0.97", ask="0.98")
    ctx.books[m.ticker] = L.book(m.ticker, "0.97", "0.98")
    calls = len(ctx.book_calls)
    assert await L.run(s, ctx) == {}  # was: bought at the intra-hour touch
    assert len(ctx.book_calls) == calls  # no book requests between decision times
    # it falls back before the hour closes: the 15:00 decision does not trigger either
    ctx.now = L.NOW + H + timedelta(seconds=10)
    ctx.markets[m.ticker] = L.market(m.ticker, bid="0.96", ask="0.98")
    ctx.books[m.ticker] = L.book(m.ticker, "0.96", "0.98")
    assert await L.run(s, ctx) == {}
    # still >= 0.97 at the 16:00 boundary: enters on that book
    ctx.now = L.NOW + 2 * H + timedelta(seconds=5)
    ctx.markets[m.ticker] = L.market(m.ticker, bid="0.97", ask="0.98")
    ctx.books[m.ticker] = L.book(m.ticker, "0.97", "0.98")
    assert m.ticker in await L.run(s, ctx)


async def test_ladder_skips_a_slot_when_the_first_tick_is_late() -> None:
    ctx = L.FakeCtx(now=L.NOW + timedelta(minutes=37))  # engine started at 14:37
    ctx.add(L.market("KXWTI-26SEP28-T70"))
    s = LadderFavorite()
    assert await L.run(s, ctx) == {}
    assert any("slot skipped" in msg and "15:00" in msg for msg in ctx.logs)
    assert ctx.series_calls == ["KXWTI"]  # categories are resolved ahead of the next decision
    ctx.now = L.NOW + H + timedelta(seconds=30)
    assert len(await L.run(s, ctx)) == 1 and ctx.series_calls == ["KXWTI"]


async def test_ladder_carries_capped_triggers_within_the_slot_on_a_fresh_book() -> None:
    ctx = L.FakeCtx(now=L.NOW + timedelta(seconds=5))
    for k in range(4):
        ctx.add(L.market(f"KXGOLDD-26SEP2{k}17-T3700", event=f"KXGOLDD-26SEP2{k}17"), category="Commodities")
    s = LadderFavorite({"max_intents_per_tick": 2, "max_group_cost": 1000})
    assert len(await L.run(s, ctx)) == 2
    ctx.now += timedelta(seconds=30)
    ctx.books["KXGOLDD-26SEP2217-T3700"] = L.book("KXGOLDD-26SEP2217-T3700", "0.95", "0.98")  # fell back
    out = await L.run(s, ctx)
    assert list(out) == ["KXGOLDD-26SEP2317-T3700"]  # the carried trigger, re-checked on the new book
    ctx.now += timedelta(seconds=30)
    assert await L.run(s, ctx) == {}  # the slot is done; the fallen one waits for 15:00


async def test_ladder_continuous_mode_is_opt_in() -> None:
    ctx = L.FakeCtx(now=L.NOW + timedelta(minutes=20))
    ctx.add(L.market("KXWTI-26SEP28-T70"))
    assert await L.run(LadderFavorite(), ctx) == {}
    assert len(await L.run(LadderFavorite({"decision_grid_s": 0}), ctx)) == 1


# --------------------------------------------------------------------------- ladder: correlated caps (14)


def test_underlying_groups() -> None:
    assert underlying_group("KXAAAGASDFL-26SEP05") == underlying_group("KXAAAGASD-26SEP05-3.10") == "gas"
    assert underlying_group("KXWTI-26SEP28") == underlying_group("KXBRENTW-26OCT02") == "oil"
    assert underlying_group("KXINXU-26SEP30H1600") == underlying_group("KXNASDAQ100U-26SEP30") == "us_equity"
    assert underlying_group("KXDJI-26SEP30") == "us_equity" and underlying_group("KXGOLDD-26JUN1017") == "gold"
    assert underlying_group("KXFOO-26SEP30-T1") == "KXFOO"


async def test_ladder_caps_one_underlying_across_events() -> None:
    ctx = L.FakeCtx()
    gas = ["KXAAAGASD", "KXAAAGASDFL", "KXAAAGASDNJ", "KXAAAGASDNC", "KXAAAGASDMI"]
    for sr in gas:  # five gas events settling on the same AAA print
        ctx.add(L.market(f"{sr}-26SEP28-3.10", event=f"{sr}-26SEP28"), category="Economics")
    ctx.add(L.market("KXWTI-26SEP28-T70"))
    out = await L.run(LadderFavorite({"max_intents_per_tick": 50}), ctx)
    gas_out = [i for t, i in out.items() if t.startswith("KXAAAGAS")]
    assert len(gas_out) == 4 and sum(i.count * i.limit_price for i in gas_out) == D("39.20")  # $40 cap
    assert "KXWTI-26SEP28-T70" in out  # another underlying is unaffected
    assert any("underlying cap $40" in msg and "gas" in msg for msg in ctx.logs)
    assert any("gas $39.20 of $40" in i.reason for i in gas_out)


async def test_ladder_group_cap_counts_open_positions_and_the_total_cap_applies() -> None:
    held = (L.position("KXAAAGASDNJ-26SEP27-3.00", "35.00", count=36),)
    ctx = L.FakeCtx(portfolio=L.portfolio(held))
    ctx.add(L.market("KXAAAGASDFL-26SEP28-3.10", event="KXAAAGASDFL-26SEP28"), category="Economics")
    ctx.add(L.market("KXWTI-26SEP28-T70"))
    out = await L.run(LadderFavorite(), ctx)
    assert list(out) == ["KXWTI-26SEP28-T70"]  # gas has $5 left < 10 contracts
    assert "KXAAAGASDFL-26SEP28-3.10" not in ctx.book_calls  # decided before fetching its book
    many = tuple(L.position(f"KXFOO{k}-26SEP27-T1", "14.60", count=15) for k in range(10))  # $146 open
    ctx2 = L.FakeCtx(portfolio=L.portfolio(many))
    ctx2.add(L.market("KXWTI-26SEP28-T70"))
    assert await L.run(LadderFavorite(), ctx2) == {}
    assert any("strategy total cap $150" in msg for msg in ctx2.logs) and ctx2.book_calls == []


# --------------------------------------------------------------------------- btc15m timing (2, 9)


async def test_btc15m_reports_the_decision_lag_and_skips_late_decisions() -> None:
    now = B.C - timedelta(minutes=9, seconds=57)  # 3 s after the 10:00 mark
    ctx = B.make_ctx(now=now, strike=await B.strike_for(0.99, now))
    (it,) = await B.run(Btc15mFavorite(B.FIXED), ctx)
    assert "decision lag 3s after the 10-min mark" in it.reason
    band = B.make_ctx(now=now, yes_bid="0.60", yes_ask="0.61")
    await B.run(Btc15mFavorite(B.FIXED), band)
    assert band.logs[0][1]["lag_s"] == 3.0 and "lag 3s" in band.logs[0][0]
    late = B.C - timedelta(minutes=9, seconds=40)  # 20 s late: was traded (window (9, 10])
    ctx = B.make_ctx(now=late, strike=await B.strike_for(0.99, late))
    assert await B.run(Btc15mFavorite(B.FIXED), ctx) == [] and ctx.book_calls == []


async def test_btc15m_engine_cadence(settings: Settings, fake_client: FakeKalshiClient) -> None:
    svc = build_services(settings, client=fake_client, strategies={B.NAME: Btc15mFavorite}, feeds=B.replay_feeds(
        B.Clock(B.T0)))
    try:
        svc.engine.update_strategy(B.NAME, enabled=True)
        rt = svc.engine.runtimes[B.NAME]
        assert svc.engine.strategy_interval(rt) == 5.0  # was engine.tick_s (30): decisions anywhere in (9.5, 10]
        assert svc.engine.jobs["series"].interval == 20.0  # was the 120 s universe refresh
    finally:
        await svc.aclose()


class OrderedCtx(B.FakeCtx):
    """Records the order of model-input and book requests; supports ``max_age_s`` like the engine."""

    def __init__(self, base: B.FakeCtx) -> None:
        super().__init__(now=base.now, markets=base.markets, feeds=base.feeds, books=base.books)
        self.order: list[str] = []
        spot = self.feeds.crypto.spot

        async def spy(symbol: str = "BTC", **kw: Any) -> Any:
            self.order.append("spot")
            return await spot(symbol, **kw)

        self.feeds.crypto.spot = spy  # type: ignore[method-assign]
        self.ages: list[Any] = []

    async def orderbook(self, ticker: str, max_age_s: float | None = None) -> Any:  # type: ignore[override]
        self.order.append("book")
        self.ages.append(max_age_s)
        return await super().orderbook(ticker)


async def test_btc15m_reads_the_book_after_the_spot() -> None:
    ctx = OrderedCtx(B.make_ctx(strike=await B.strike_for(0.99)))
    (it,) = await B.run(Btc15mFavorite(B.FIXED), ctx)
    assert ctx.order == ["spot", "book"]  # was ["book", "spot"]: a book older than the spot it is compared with
    assert ctx.ages == [0]  # and never a cached one


# --------------------------------------------------------------------------- maker (4, 18)


async def test_maker_expected_edge_is_not_an_optimistic_prior() -> None:
    m = M.market("KXGAS-26OCT01-T3")
    [i] = await M.run(MakerFavoriteHarvest(), M.FakeCtx([m]))
    assert i.expected_edge == 0 and i.fair_value == float(i.limit_price)  # was +0.005 (literature prior)
    assert "negative in this band" in i.reason
    d = MakerFavoriteHarvest.description
    assert "NEGATIVE" in d and "prior edge is 0" in d
    doc = maker_module.__doc__ or ""  # the +0.2..+0.4c figure is the >= 0.97 (B4ns) maker, not this band
    assert "+0.2...+0.4c" in doc and ">= 0.97" in doc and "-2.36c" in doc


async def test_maker_open_cost_cap_counts_filled_positions() -> None:
    ms = [M.market(f"KXS{i:02d}-26OCT01-A") for i in range(6)]
    held = tuple(Position(ticker=f"KXH{i}-26OCT01-A", strategy=M.NAME, event_ticker=f"KXH{i}-26OCT01", side="yes",
                          count=11, cost_basis=D("9.90")) for i in range(9))  # $89.10 of fills held to settlement
    strat = MakerFavoriteHarvest()
    out = M.entries(await M.run(strat, M.FakeCtx(ms, pf=M.portfolio(positions=held))))
    assert len(out) == 1  # $10.90 left: one more $9.90 bid (was: 8 more, any number of fills)
    assert strat.last_scan["skipped"]["open cost cap"] == 1
    held10 = held + (Position(ticker="KXH9-26OCT01-A", strategy=M.NAME, event_ticker="KXH9-26OCT01", side="yes",
                              count=11, cost_basis=D("9.90")),)
    ctx = M.FakeCtx(ms, pf=M.portfolio(positions=held10 + (held10[0],)))  # $108.90
    assert M.entries(await M.run(MakerFavoriteHarvest(), ctx)) == [] and ctx.calls["orderbooks"] == 0


async def test_maker_skips_crypto_and_mentions_by_default() -> None:
    btc = M.market("KXBTCD-26OCT01-T100000")
    say = M.market("KXTRUMPSAYCOMPANY-26OCT01-X")
    ctx = M.FakeCtx([btc, say], series_map={"KXBTCD": M.series("KXBTCD", category="Crypto"),
                                            "KXTRUMPSAYCOMPANY": M.series("KXTRUMPSAYCOMPANY", category="Mentions")})
    strat = MakerFavoriteHarvest()
    assert await M.run(strat, ctx) == []
    assert strat.last_scan["skipped"]["excluded category"] == 2


def test_new_defaults_bound_the_bursts() -> None:
    assert MakerFavoriteHarvest().params["max_resting_orders"] == 8  # was 15 (also halves tape polling)
    assert LadderFavorite().params["max_intents_per_tick"] == 5  # was 20
    assert Decimal(str(LadderFavorite().params["max_position_cost"])) == 10  # was 15


async def test_btc15m_warms_the_fee_event_a_minute_before_the_decision() -> None:
    class EventCtx(B.FakeCtx):
        def __init__(self, base: B.FakeCtx) -> None:
            super().__init__(now=base.now, markets=base.markets, feeds=base.feeds, books=base.books)
            self.event_calls: list[str] = []

        async def event(self, event_ticker: str) -> Any:
            self.event_calls.append(event_ticker)
            return None

    s = Btc15mFavorite(B.FIXED)
    ctx = EventCtx(B.make_ctx(now=B.C - timedelta(minutes=10, seconds=30)))
    assert await B.run(s, ctx) == [] and ctx.book_calls == []
    assert ctx.event_calls == [B.TICKER.rsplit("-", 1)[0]]  # fetched once, ahead of the window
    ctx.now = B.C - timedelta(minutes=10, seconds=10)
    await B.run(s, ctx)
    assert len(ctx.event_calls) == 1
