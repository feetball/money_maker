"""Baskets (ARCHITECTURE.md §6 rule 9, §8): intents sharing a ``group_id`` are risk-checked with
``RiskManager.check_basket`` and placed with ``PaperBroker.place_basket(all_or_none=True)``;
any trimmed or unfillable leg rejects every leg; a basket is never split by the per-tick cap."""

from __future__ import annotations

from typing import Any

from conftest import FakeKalshiClient, ScriptedStrategy, standard_market

from kalshibot.api.server import AppServices, build_services
from kalshibot.config import Settings
from kalshibot.feeds import FeedRegistry
from kalshibot.money import D
from kalshibot.strategies.base import OrderIntent, UniverseSpec

A, B, C = "KXB-EV-A", "KXB-EV-B", "KXB-EV-C"


class Basketeer(ScriptedStrategy):
    name = "scripted"

    def universe(self) -> UniverseSpec:
        return UniverseSpec(max_days_to_close=3)


def leg(ticker: str, count: int = 5, gid: str | None = "g1", **kw: Any) -> OrderIntent:
    return OrderIntent(ticker=ticker, side="no", count=count, limit_price=D("0.56"), group_id=gid,
                       reason="arb leg", expected_edge=D("0.001"), **kw)


async def stack(settings: Settings, fc: FakeKalshiClient, depth_b: int = 100) -> AppServices:
    standard_market(fc, A, yes_bid="0.44", yes_ask="0.46")  # NO ask .56
    standard_market(fc, B, yes_bid="0.44", yes_ask="0.46", depth=depth_b)
    standard_market(fc, C, yes_bid="0.44", yes_ask="0.46")
    svc = build_services(settings, client=fc, strategies={"scripted": Basketeer}, feeds=FeedRegistry())
    svc.md.scanner_days_to_close = 0
    svc.engine.update_strategy("scripted", enabled=True)
    await svc.md.refresh_universe(force=True)
    return svc


def spy(svc: AppServices) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"place_basket": [], "check_basket": [], "place_order": []}
    broker, risk = svc.broker, svc.risk
    pb, po, cb = broker.place_basket, broker.place_order, risk.check_basket

    async def place_basket(intents: Any, **kw: Any) -> Any:
        calls["place_basket"].append(([i.ticker for i in intents], kw))
        return await pb(intents, **kw)

    async def place_order(intent: Any, **kw: Any) -> Any:
        calls["place_order"].append(intent.ticker)
        return await po(intent, **kw)

    def check_basket(intents: Any, markets: Any, pv: Any, **kw: Any) -> Any:
        calls["check_basket"].append([i.ticker for i in intents])
        return cb(intents, markets, pv, **kw)

    broker.place_basket, broker.place_order, risk.check_basket = place_basket, place_order, check_basket
    return calls


async def tick(svc: AppServices, intents: list[Any]) -> list[dict[str, Any]]:
    svc.engine.runtimes["scripted"].instance.queue = [intents]
    await svc.engine.tick()
    return svc.store.list_signals(limit=len(intents))[::-1] if intents else []


async def test_group_is_routed_all_or_none_through_check_basket(settings, fake_client) -> None:
    svc = await stack(settings, fake_client)
    calls = spy(svc)
    sig = await tick(svc, [leg(A), leg(B), leg(C), leg(A, count=1, gid=None)])
    assert calls["check_basket"] == [[A, B, C]]
    [(tickers, kw)] = calls["place_basket"]
    assert kw.pop("decided_at") is not None  # the legs meet only books received after the decision
    assert (tickers, kw) == ([A, B, C], {"all_or_none": True, "counts": [5, 5, 5]})
    assert calls["place_order"] == [A]  # the single goes on its own
    assert [s["decision"] for s in sig] == ["executed"] * 4
    assert {s["group_id"] for s in sig[:3]} == {"g1"}
    assert {(p.ticker, p.side, p.count) for p in svc.broker.positions()} == {(A, "no", 6), (B, "no", 5),
                                                                              (C, "no", 5)}
    await svc.aclose()


async def test_one_unfillable_leg_rejects_every_leg(settings, fake_client) -> None:
    svc = await stack(settings, fake_client, depth_b=3)  # B shows only 3 at the limit
    sig = await tick(svc, [leg(A), leg(B), leg(C)])
    assert all(s["decision"] == "rejected" and "basket" in s["decision_reason"] for s in sig)
    assert "only 3/5 fillable" in sig[1]["decision_reason"]
    assert svc.broker.positions() == [] and svc.broker.cash == D(1000)
    await svc.aclose()


async def test_risk_trimming_one_leg_rejects_the_basket_before_the_broker(settings, fake_client) -> None:
    settings.risk.max_exposure_per_event = 7  # the 3 x 5 x .56 basket (~$8.9) does not fit the event cap
    svc = await stack(settings, fake_client)
    calls = spy(svc)
    sig = await tick(svc, [leg(A), leg(B), leg(C)])
    assert calls["check_basket"] == [[A, B, C]] and calls["place_basket"] == []
    assert all(s["decision"] == "rejected" and "all-or-none" in s["decision_reason"] for s in sig)
    assert "max_exposure_per_event" in sig[2]["decision_reason"]
    assert svc.risk.orders_last_minute() == 0  # a rejected basket does not count as orders
    await svc.aclose()


async def test_single_leg_group_is_fill_or_kill(settings, fake_client) -> None:
    svc = await stack(settings, fake_client, depth_b=3)
    calls = spy(svc)
    [s] = await tick(svc, [leg(B, gid="solo")])
    assert calls["place_basket"] and calls["place_order"] == []
    assert s["decision"] == "rejected" and "only 3/5 fillable" in s["decision_reason"]
    [s] = await tick(svc, [leg(B, gid=None)])  # without a group_id: an ordinary IOC, partial fill
    assert s["decision"] == "partial" and svc.broker.position(B, "scripted").count == 3
    await svc.aclose()


async def test_intent_cap_never_splits_a_basket(settings, fake_client) -> None:
    svc = await stack(settings, fake_client)
    svc.engine.max_intents_per_tick = 3
    calls = spy(svc)
    await tick(svc, [leg(A, count=1, gid=None), leg(A, gid="g"), leg(B, gid="g"), leg(C, gid="g")])
    assert calls["place_order"] == [A] and calls["place_basket"] == []  # the 3-leg basket went whole
    assert any("were dropped" in r["message"] for r in svc.store.list_logs(kind="strategy"))
    await tick(svc, [leg(A, gid="h"), leg(B, gid="h"), leg(C, gid="h")])  # fits exactly
    assert calls["place_basket"] and calls["place_basket"][0][0] == [A, B, C]
    await svc.aclose()


async def test_basket_legs_cannot_replace_orders_and_bad_legs_all_get_signals(settings, fake_client) -> None:
    svc = await stack(settings, fake_client)
    sig = await tick(svc, [leg(A, replaces=5), {"ticker": B, "side": "maybe", "limit_price": "0.56",
                                                "group_id": "g1"}])
    assert len(sig) == 2 and all(s["decision"] == "rejected" for s in sig)
    assert "replaces is not supported for basket legs" in sig[0]["decision_reason"]
    assert "side" in sig[1]["decision_reason"]
    await svc.aclose()
