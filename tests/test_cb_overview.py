"""``GET /api/overview`` (contract §13): both paper venues side by side, each block isolated.
Fakes only - no network. PAPER ONLY."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from test_cb_api import make_app, populated

from kalshibot.api.overview import COINBASE_LABEL, COMBINED_NOTE, KALSHI_LABEL

VENUE_KEYS = {"venue", "label", "available", "unavailable_reason", "engine_running", "kill_switch",
              "starting_balance", "equity", "cash", "total_pnl", "total_return_pct", "todays_pnl", "open_positions",
              "fees_paid", "last_error"}


def has(d: dict[str, Any], keys: set[str]) -> None:
    missing = keys - set(d)
    assert not missing, f"missing keys: {sorted(missing)}"


def test_overview_both_venues(tmp_path: Path) -> None:
    cb, _ = asyncio.run(populated(tmp_path))
    client, _ = make_app(tmp_path, cb)
    with client as c:
        d = c.get("/api/overview").json()
        assert d["generated_at"].endswith("Z")
        k, cbv = d["venues"]["kalshi"], d["venues"]["coinbase"]
        has(k, VENUE_KEYS)
        has(cbv, VENUE_KEYS)
        assert k["venue"] == "kalshi" and k["label"] == KALSHI_LABEL and k["available"] is True
        assert cbv["venue"] == "coinbase" and cbv["label"] == COINBASE_LABEL and cbv["available"] is True
        assert cbv["unavailable_reason"] is None and cbv["open_positions"] == 1 and cbv["fees_paid"] > 0
        acct = c.get("/api/coinbase/account").json()
        kacct = c.get("/api/account").json()
        assert cbv["equity"] == pytest.approx(acct["equity"]) and k["equity"] == pytest.approx(kacct["equity"])
        comb = d["combined"]
        assert comb["note"] == COMBINED_NOTE and comb["venues_included"] == ["kalshi", "coinbase"]
        assert comb["starting_balance"] == pytest.approx(k["starting_balance"] + cbv["starting_balance"])
        assert comb["equity"] == pytest.approx(k["equity"] + cbv["equity"], abs=1e-6)
        assert comb["total_return_pct"] == pytest.approx(comb["total_pnl"] / comb["starting_balance"] * 100)
        es = d["equity_series"]
        assert set(es) == {"kalshi", "coinbase"} and len(es["coinbase"]) >= 2 and len(es["kalshi"]) >= 1
        assert all(set(p) == {"ts", "equity"} for p in es["coinbase"] + es["kalshi"])
        assert c.get("/api/overview?range=7d&max_points=10").status_code == 200
        assert c.get("/api/overview?range=nope").status_code == 422
    asyncio.run(cb.aclose())


def test_overview_coinbase_unavailable(tmp_path: Path) -> None:
    client, _ = make_app(tmp_path, None, build_coinbase=False)
    with client as c:
        c.app.state.cb_error = "disabled in config (coinbase.enabled: false)"  # type: ignore[attr-defined]
        d = c.get("/api/overview").json()
        cbv = d["venues"]["coinbase"]
        assert cbv["available"] is False and "disabled" in cbv["unavailable_reason"]
        assert cbv["equity"] is None and cbv["total_pnl"] is None and cbv["open_positions"] is None
        assert d["venues"]["kalshi"]["available"] is True
        assert d["combined"]["venues_included"] == ["kalshi"]
        assert d["combined"]["equity"] == d["venues"]["kalshi"]["equity"]
        assert d["equity_series"]["coinbase"] == []


def test_overview_isolates_a_failing_coinbase_block(tmp_path: Path) -> None:
    cb, _ = asyncio.run(populated(tmp_path))
    client, _ = make_app(tmp_path, cb)

    def boom() -> Any:
        raise RuntimeError("broker exploded")

    with client as c:
        cb.broker.account = boom  # type: ignore[method-assign]
        r = c.get("/api/overview")
        assert r.status_code == 200
        d = r.json()
        assert d["venues"]["coinbase"]["available"] is False
        assert "broker exploded" in d["venues"]["coinbase"]["unavailable_reason"]
        assert d["venues"]["kalshi"]["available"] is True and d["venues"]["kalshi"]["equity"] is not None
    del cb.broker.account
    asyncio.run(cb.aclose())
