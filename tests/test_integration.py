"""Backend/frontend integration fixes (found by driving the built dashboard's API calls live).

* SSE events carry strictly increasing ``id:`` lines; ``?replay=N`` and ``Last-Event-ID``
  re-send buffered events before the id-less ``account`` greeting (frontend gap fill).
* Engaging the kill switch cancels every resting paper order (API, PATCH /api/risk, and the
  automatic daily-loss trip), as the dashboard tells the user.
* ``frontend/dist`` is looked up per request (a build made while the server runs is served);
  a missing ``/assets/*`` file is a 404, never index.html.
* Market titles are plain text (Kalshi's Markdown ``**bold**`` is stripped) and searchable.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from conftest import raw_market
from test_api import C, make

from kalshibot.config import Settings
from kalshibot.engine import EventBus
from kalshibot.kalshi.models import Market
from kalshibot.marketdata import display_title, plain_text


def _events(body: str) -> list[dict[str, Any]]:
    out = []
    for blk in body.split("\n\n"):
        fields: dict[str, Any] = {}
        for line in blk.split("\n"):
            if line.startswith(":") or ": " not in line:
                continue
            k, v = line.split(": ", 1)
            fields[k] = v
        if "event" in fields:
            fields["data"] = json.loads(fields["data"])
            out.append(fields)
    return out


def _wait_published(bus: EventBus, n: int) -> None:
    deadline = time.monotonic() + 3
    while bus.published < n and time.monotonic() < deadline:
        time.sleep(0.01)
    assert bus.published >= n


# --------------------------------------------------------------------------- event bus / SSE


def test_bus_ids_increase_and_replay_filters() -> None:
    bus = EventBus(history=3)
    q_plain = bus.subscribe()
    q_ids = bus.subscribe(with_ids=True)
    for i in range(5):
        bus.publish("tick", {"i": i})
    ids = [e[0] for e in bus.recent]
    assert len(ids) == 3 and ids == sorted(ids) and len(set(ids)) == 3 and ids[-1] == bus.last_id
    assert bus.last_id > 1_600_000_000_000  # epoch-ms based: ids keep increasing across restarts
    assert q_plain.get_nowait() == ("tick", {"i": 0})
    first = q_ids.get_nowait()
    assert len(first) == 3 and first[1:] == ("tick", {"i": 0})
    assert [e[2]["i"] for e in bus.replay(last=2)] == [3, 4]
    assert [e[2]["i"] for e in bus.replay(after=ids[0])] == [3, 4]
    assert [e[2]["i"] for e in bus.replay(after=ids[0], last=1)] == [4]
    assert bus.replay(last=0) == []
    older = EventBus()
    time.sleep(0.003)
    assert EventBus().last_id > older.last_id  # a restarted process starts above the previous ids


def test_stream_sends_ids_and_replays_backlog(settings: Settings, tmp_path: Path) -> None:
    client, svc, _ = make(settings, tmp_path)
    with client as c:
        base = svc.bus.published
        for i in range(4):
            svc.bus.publish("tick", {"tick_count": i})
        svc.bus.publish("bogus", {"x": 1})
        _wait_published(svc.bus, base + 5)
        ticks = [e for e in svc.bus.recent if e[1] == "tick"][-4:]

        # replay the last 5 buffered events: 4 ticks (the unknown type is skipped), then the greeting
        with c.stream("GET", "/api/stream?replay=5&max_events=5&duration=3") as r:
            assert r.status_code == 200
            evs = _events("".join(r.iter_text()))
        assert [e["event"] for e in evs] == ["tick"] * 4 + ["account"]
        assert [int(e["id"]) for e in evs[:4]] == [t[0] for t in ticks]
        assert [e["data"]["tick_count"] for e in evs[:4]] == [0, 1, 2, 3]
        assert "id" not in evs[4]  # after a replay the greeting has no id of its own

        # Last-Event-ID: only what came after it
        with c.stream("GET", "/api/stream?max_events=2&duration=3",
                      headers={"Last-Event-ID": str(ticks[2][0])}) as r:
            evs = _events("".join(r.iter_text()))
        assert [e["event"] for e in evs] == ["tick", "account"]
        assert int(evs[0]["id"]) == ticks[3][0]

        # no replay requested: greeting first, then live events with ids above everything buffered
        # (the test client buffers the whole response, so a thread publishes meanwhile)
        before = svc.bus.last_id
        stop = threading.Event()

        def publisher() -> None:
            while not stop.is_set():
                svc.bus.publish("log", {"id": None, "message": "live"})
                time.sleep(0.02)

        th = threading.Thread(target=publisher, daemon=True)
        th.start()
        try:
            with c.stream("GET", "/api/stream?max_events=2&duration=3") as r:
                evs = _events("".join(r.iter_text()))
        finally:
            stop.set()
            th.join()
        assert [e["event"] for e in evs] == ["account", "log"]
        assert int(evs[1]["id"]) > before and evs[1]["data"]["message"] == "live"
        # the plain greeting marks the stream position (latest event id when it was sent)
        assert before <= int(evs[0]["id"]) < int(evs[1]["id"])


# --------------------------------------------------------------------------- kill switch


def test_kill_switch_cancels_resting_orders(settings: Settings, tmp_path: Path) -> None:
    client, svc, _ = make(settings, tmp_path)
    with client as c:
        open_before = c.get("/api/orders?status=open").json()
        assert [o["ticker"] for o in open_before] == [C]
        r = c.post("/api/engine/kill-switch", json={"on": True})
        assert r.status_code == 200 and r.json()["engine"]["kill_switch"] is True
        assert c.get("/api/orders?status=open").json() == []
        o = next(x for x in c.get("/api/orders?status=all").json() if x["id"] == open_before[0]["id"])
        assert o["status"] == "cancelled"
        acct = c.get("/api/account").json()
        assert acct["open_orders"] == 0 and acct["reserved_cash"] == 0
        assert any("cancelled 1 resting order" in lg["message"] for lg in c.get("/api/logs").json())
        # releasing it cancels nothing and resting orders placed afterwards survive
        assert c.post("/api/engine/kill-switch", json={"on": False}).json()["engine"]["kill_switch"] is False


def test_patch_risk_kill_switch_cancels_resting_orders(settings: Settings, tmp_path: Path) -> None:
    client, svc, _ = make(settings, tmp_path)
    with client as c:
        assert len(c.get("/api/orders?status=open").json()) == 1
        r = c.patch("/api/risk", json={"kill_switch": True})
        assert r.status_code == 200 and r.json()["kill_switch"] is True
        assert c.get("/api/orders?status=open").json() == []


def test_daily_loss_trip_cancels_resting_orders(settings: Settings, tmp_path: Path) -> None:
    _, svc, _ = make(settings, tmp_path)
    assert len(svc.broker.open_orders()) == 1

    def tripped(portfolio: Any) -> bool:
        svc.risk.set_kill_switch(True, "daily loss limit: test")
        return True

    svc.risk.evaluate = tripped  # type: ignore[method-assign]
    asyncio.run(svc.engine._job_snapshot())
    assert svc.risk.kill_switch is True and svc.broker.open_orders() == []


def test_engine_set_kill_switch_only_cancels_on_engage(settings: Settings, tmp_path: Path) -> None:
    _, svc, _ = make(settings, tmp_path)

    async def go() -> tuple[int, int, int]:
        a = len(await svc.engine.set_kill_switch(False, "x"))  # already off: nothing
        b = len(await svc.engine.set_kill_switch(True, "manual"))  # engage: cancels the resting order
        c = len(await svc.engine.set_kill_switch(True, "again"))  # already on: nothing more
        return a, b, c

    assert asyncio.run(go()) == (0, 1, 0)


# --------------------------------------------------------------------------- SPA serving


def test_dist_built_after_start_is_served(settings: Settings, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    client, _, _ = make(settings, tmp_path, populate_state=False, dist=dist)
    with client as c:
        assert "PAPER TRADING" in c.get("/strategies").text  # placeholder until built
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<!doctype html><title>app</title>")
        (dist / "assets" / "app-1234.js").write_text("console.log(1)")
        r = c.get("/backtests/7")
        assert "<title>app</title>" in r.text and r.headers["cache-control"] == "no-cache"
        r = c.get("/assets/app-1234.js")
        assert r.text == "console.log(1)" and "immutable" in r.headers["cache-control"]
        assert "javascript" in r.headers["content-type"]
        r = c.get("/assets/gone-0000.js")
        assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
        assert c.get("/api/nope").status_code == 404
        assert "<title>app</title>" in c.get("/../../etc/passwd").text


def test_spa_does_not_shadow_api_or_docs(settings: Settings, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>app</title>")
    client, _, _ = make(settings, tmp_path, populate_state=False, dist=dist)
    with client as c:
        assert c.get("/api/status").json()["mode"] == "paper"
        assert c.get("/openapi.json").json()["info"]["title"].startswith("kalshibot")


# --------------------------------------------------------------------------- titles


def test_titles_are_plain_text_and_searchable(settings: Settings, tmp_path: Path) -> None:
    now = datetime.now(UTC)
    m = Market.from_api(raw_market("KXAAAGASD-26SEP27-4.4850", close_time=now + timedelta(hours=2),
                                   title="Will average **gas prices** be above $4.4850?",
                                   yes_sub_title="Above 4.4850"))
    assert display_title(m) == "Will average gas prices be above $4.4850? — Above 4.4850"
    assert plain_text("2**3 and a__b") == "2**3 and a__b"
    _, svc, _ = make(settings, tmp_path, populate_state=False)
    svc.md.markets[m.ticker] = m
    assert [x.ticker for x in svc.md.search(search="average gas prices")] == [m.ticker]
