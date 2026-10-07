"""Live trading: the signed client (request shape, signatures) and LiveBroker (fills booked
from exchange reports, fractional fills, unknown submissions, cancels, reconcile)."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshibot.kalshi.client import KalshiAPIError, KalshiNotFound
from kalshibot.kalshi.trading import (
    KalshiSigner,
    KalshiTradingClient,
    OrderSubmitUnknown,
    v2_side_price,
)
from kalshibot.live import LiveBroker
from kalshibot.money import D
from kalshibot.paper import ManualClock, StaticMarketData, make_market
from kalshibot.store import Store

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
A = "KXTEST-26SEP-A"


@dataclass
class Intent:
    ticker: str
    side: str
    limit_price: Decimal
    count: int = 1
    action: str = "buy"
    tif: str = "ioc"
    expires_in_s: int | None = None
    strategy: str = "s1"
    reason: str = "test"
    fair_value: float | None = None
    expected_edge: Decimal | None = None
    group_id: str | None = None


def buy(side: str, price: str, count: int, **kw: Any) -> Intent:
    return Intent(ticker=A, side=side, limit_price=D(price), count=count, **kw)


# --------------------------------------------------------------------------- signed client


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_v2_side_price() -> None:
    assert v2_side_price("yes", "buy", D("0.56")) == ("bid", D("0.56"))
    assert v2_side_price("no", "buy", D("0.30")) == ("ask", D("0.70"))
    assert v2_side_price("yes", "sell", D("0.56")) == ("ask", D("0.56"))
    assert v2_side_price("no", "sell", D("0.30")) == ("bid", D("0.70"))


async def test_signed_create_order_request(rsa_key: rsa.RSAPrivateKey) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(201, json={"order_id": "x1", "fill_count": "5.00", "remaining_count": "0.00",
                                         "ts_ms": 1})

    signer = KalshiSigner("key-1", rsa_key, clock_ms=lambda: 1700000000000)
    async with KalshiTradingClient(signer, "https://demo-api.kalshi.co/trade-api/v2",
                                   transport=httpx.MockTransport(handler)) as c:
        resp = await c.create_order(ticker=A, side="no", action="buy", count=5, limit_price=D("0.3"), tif="fok",
                                    client_order_id="kb-1-abc")
    assert resp["order_id"] == "x1"
    req = seen[0]
    assert req.url.path == "/trade-api/v2/portfolio/events/orders"
    body = json.loads(req.content)
    assert body == {"ticker": A, "client_order_id": "kb-1-abc", "side": "ask", "count": "5.00", "price": "0.7000",
                    "time_in_force": "fill_or_kill", "self_trade_prevention_type": "taker_at_cross",
                    "cancel_order_on_pause": True}
    assert req.headers["KALSHI-ACCESS-KEY"] == "key-1"
    assert req.headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    # the signature covers timestamp + method + full path (prefix included, no query)
    rsa_key.public_key().verify(base64.b64decode(req.headers["KALSHI-ACCESS-SIGNATURE"]),
                                b"1700000000000POST/trade-api/v2/portfolio/events/orders",
                                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                                hashes.SHA256())


@pytest.mark.parametrize(("status", "exc"), [(500, OrderSubmitUnknown), (429, OrderSubmitUnknown),
                                              (400, KalshiAPIError)])
async def test_create_order_never_retries(rsa_key: rsa.RSAPrivateKey, status: int, exc: type) -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(status, json={"error": {"code": "x", "message": "nope"}})

    async with KalshiTradingClient(KalshiSigner("k", rsa_key), transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(exc) as ei:
            await c.create_order(ticker=A, side="yes", action="buy", count=1, limit_price=D("0.5"), tif="ioc",
                                 client_order_id="c")
    assert len(calls) == 1
    assert (ei.type is OrderSubmitUnknown) == (status != 400)


async def test_create_order_timeout_is_unknown(rsa_key: rsa.RSAPrivateKey) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    async with KalshiTradingClient(KalshiSigner("k", rsa_key), transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(OrderSubmitUnknown):
            await c.create_order(ticker=A, side="yes", action="buy", count=1, limit_price=D("0.5"), tif="ioc",
                                 client_order_id="c")


async def test_reads_retry_and_query_is_not_signed(rsa_key: rsa.RSAPrivateKey) -> None:
    n = {"i": 0}
    paths = []

    async def no_sleep(_: float) -> None:
        return None

    def handler(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        paths.append(req.url.params.get("order_id"))
        if n["i"] == 1:
            return httpx.Response(503, json={"error": "busy"})
        return httpx.Response(200, json={"fills": [{"fill_id": "f1"}], "cursor": ""})

    signer = KalshiSigner("k", rsa_key, clock_ms=lambda: 1)
    async with KalshiTradingClient(signer, transport=httpx.MockTransport(handler), sleep=no_sleep) as c:
        fills = await c.get_fills(order_id="o1")
    assert fills == [{"fill_id": "f1"}] and n["i"] == 2 and paths == ["o1", "o1"]


# --------------------------------------------------------------------------- fake exchange


class FakeTrader:
    """In-memory exchange: ``script`` decides what each new order does."""

    def __init__(self, balance: str = "500") -> None:
        self.balance = D(balance)
        self.orders: dict[str, dict[str, Any]] = {}
        self.fills: dict[str, list[dict[str, Any]]] = {}
        self.positions: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.next_fills: list[tuple[str, str, bool]] = []  # (count, buy-side price, is_taker) for the next order
        self.next_error: Exception | None = None
        self.hide_fills_once = False
        self.request_count = 0
        self.error_count = 0
        self.signer: Any = object()  # "a key is configured"
        self._n = 0

    async def balance_dollars(self) -> Decimal:
        return self.balance

    async def get_balance(self) -> dict[str, Any]:
        return {"balance": int(self.balance * 100), "balance_dollars": str(self.balance), "portfolio_value": 0}

    async def get_positions(self, *, ticker: str | None = None) -> list[dict[str, Any]]:
        return list(self.positions)

    async def create_order(self, *, ticker: str, side: str, action: str, count: int, limit_price: Decimal, tif: str,
                           client_order_id: str, expiration_ts: int | None = None, **kw: Any) -> dict[str, Any]:
        self.created.append({"ticker": ticker, "side": side, "action": action, "count": count,
                             "limit_price": limit_price, "tif": tif, "client_order_id": client_order_id,
                             "expiration_ts": expiration_ts})
        if self.next_error is not None:
            e, self.next_error = self.next_error, None
            if isinstance(e, OrderSubmitUnknown):  # it did reach the exchange
                self._make(ticker, side, action, count, tif, client_order_id)
            raise e
        return self._make(ticker, side, action, count, tif, client_order_id)

    def _make(self, ticker: str, side: str, action: str, count: int, tif: str, coid: str) -> dict[str, Any]:
        self._n += 1
        xid = f"x{self._n}"
        buy_side = side if action == "buy" else ("no" if side == "yes" else "yes")
        fl = []
        for i, (n, p, taker) in enumerate(self.next_fills):
            p = D(p)
            yes = p if buy_side == "yes" else 1 - p
            fl.append({"fill_id": f"{xid}-f{i}", "order_id": xid, "ticker": ticker, "outcome_side": buy_side,
                       "count_fp": n, "yes_price_dollars": str(yes), "no_price_dollars": str(1 - yes),
                       "is_taker": taker, "fee_cost": "0.0100",
                       "created_time": (T0 + timedelta(seconds=i)).isoformat().replace("+00:00", "Z")})
        self.next_fills = []
        filled = sum((D(f["count_fp"]) for f in fl), D(0))
        remaining = D(count) - filled if tif == "gtc" else D(0)
        self.fills[xid] = fl
        self.orders[xid] = {"order_id": xid, "client_order_id": coid, "ticker": ticker,
                            "status": "resting" if remaining > 0 else "executed",
                            "fill_count_fp": f"{filled:.2f}", "remaining_count_fp": f"{remaining:.2f}"}
        return {"order_id": xid, "client_order_id": coid, "fill_count": f"{filled:.2f}",
                "remaining_count": f"{remaining:.2f}", "ts_ms": 1}

    def fill_resting(self, xid: str, n: str, price: str) -> None:
        o = self.orders[xid]
        fl = self.fills[xid]
        p = D(price)
        fl.append({"fill_id": f"{xid}-f{len(fl)}", "order_id": xid, "ticker": o["ticker"], "outcome_side": "yes",
                   "count_fp": n, "yes_price_dollars": str(p), "no_price_dollars": str(1 - p), "is_taker": False,
                   "fee_cost": "0.0000", "created_time": T0.isoformat().replace("+00:00", "Z")})
        filled = D(o["fill_count_fp"]) + D(n)
        rem = D(o["remaining_count_fp"]) - D(n)
        o.update(fill_count_fp=f"{filled:.2f}", remaining_count_fp=f"{rem:.2f}",
                 status="executed" if rem <= 0 else "resting")

    async def get_fills(self, *, order_id: str | None = None, **kw: Any) -> list[dict[str, Any]]:
        if self.hide_fills_once:
            self.hide_fills_once = False
            return []
        return list(self.fills.get(order_id or "", []))

    async def get_order(self, order_id: str) -> dict[str, Any]:
        if order_id not in self.orders:
            raise KalshiNotFound(404, "nf", order_id)
        return dict(self.orders[order_id])

    async def get_orders(self, *, ticker: str | None = None, **kw: Any) -> list[dict[str, Any]]:
        return [dict(o) for o in self.orders.values() if ticker is None or o["ticker"] == ticker]

    async def cancel_order(self, order_id: str, ticker: str) -> dict[str, Any]:
        self.cancelled.append(order_id)
        o = self.orders[order_id]
        if o["status"] != "resting":
            raise KalshiNotFound(404, "not resting", order_id)
        o.update(status="canceled", remaining_count_fp="0.00")
        return {"order_id": order_id, "reduced_by": "1.00", "ts_ms": 1}


@dataclass
class LiveCfg:
    environment: str = "demo"
    max_order_contracts: int = 100
    max_order_cost: Decimal = D("100")
    taker_time_in_force: str = "fill_or_kill"
    client_order_prefix: str = "kb"


@dataclass
class Cfg:
    live: LiveCfg


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(T0)


@pytest.fixture
def md(clock: ManualClock) -> StaticMarketData:
    m = StaticMarketData([make_market(A)], clock=clock)
    m.set_book(A, yes_bids=[("0.40", 50)], no_bids=[("0.58", 10)])
    return m


@pytest.fixture
def trader() -> FakeTrader:
    return FakeTrader()


@pytest.fixture
async def broker(md: StaticMarketData, clock: ManualClock, trader: FakeTrader) -> LiveBroker:
    async def no_sleep(_: float) -> None:
        return None

    b = LiveBroker(md, Store(":memory:"), trader, settings=Cfg(LiveCfg()), clock=clock, sleep=no_sleep)
    await b.start()
    return b


def ledger_fills(b: LiveBroker) -> list[Any]:
    return sorted(b.store.list_fills(limit=None), key=lambda f: f.id)


# --------------------------------------------------------------------------- LiveBroker


async def test_start_adopts_exchange_balance(broker: LiveBroker) -> None:
    assert broker.starting_balance == D("500") and broker.cash == D("500")
    assert broker.exchange["cash_drift"] == 0 and broker.exchange["position_mismatches"] == []


async def test_taker_fill_booked_from_exchange(broker: LiveBroker, trader: FakeTrader) -> None:
    trader.next_fills = [("4.00", "0.42", True), ("6.00", "0.43", True)]
    o = await broker.place_order(buy("yes", "0.45", 10))
    sent = trader.created[0]
    assert sent["tif"] == "fok" and sent["count"] == 10 and sent["client_order_id"].startswith(f"kb-{o.id}-")
    assert o.status == "filled" and o.filled_count == 10
    assert broker.exchange_order_id(o) == "x1"
    got = [(f.price, f.count, f.fee) for f in ledger_fills(broker)]
    assert got == [(D("0.42"), 4, D("0.01")), (D("0.43"), 6, D("0.01"))]
    # cash: 500 - 4*.42 - 6*.43 - .02 = 495.72, nothing left reserved
    assert broker.cash == D("495.72") and broker.reserved_cash == 0
    p = broker.position(A, "s1")
    assert p is not None and p.count == 10 and p.side == "yes"


async def test_no_side_and_unfilled_fok(broker: LiveBroker, trader: FakeTrader) -> None:
    o = await broker.place_order(buy("no", "0.30", 5))
    assert o.status == "cancelled" and o.filled_count == 0 and "no fill" in o.status_reason
    assert broker.cash == D("500")


async def test_fractional_fills_book_whole_contracts(broker: LiveBroker, trader: FakeTrader) -> None:
    trader.next_fills = [("2.50", "0.40", True), ("7.50", "0.42", True)]
    o = await broker.place_order(buy("yes", "0.45", 10))
    assert o.filled_count == 10
    got = [(f.count, f.price) for f in ledger_fills(broker)]
    # 2.5 -> book 2 @ .40; then 10 total -> book 8 @ (.2 + 3.15) / 8 = .41875
    assert got == [(2, D("0.40")), (8, D("0.41875"))]
    assert broker.cash == D("500") - D("4.15") - D("0.02")


async def test_ioc_fractional_total_leaves_residue(md: StaticMarketData, clock: ManualClock,
                                                   trader: FakeTrader) -> None:
    b = LiveBroker(md, Store(":memory:"), trader, settings=Cfg(LiveCfg(taker_time_in_force="immediate_or_cancel")),
                   clock=clock)
    await b.start()
    trader.next_fills = [("3.50", "0.40", True)]
    o = await b.place_order(buy("yes", "0.45", 10))
    assert trader.created[0]["tif"] == "ioc"
    assert o.status == "cancelled" and o.filled_count == 3
    assert b._residue == {A: {"yes": D("0.5")}}
    # the whole 3.5 contracts were paid for: 1.40 + .01 fee
    assert b.cash == D("500") - D("1.41")
    trader.positions = [{"ticker": A, "position_fp": "3.50"}]
    st = await b.reconcile()
    assert st["position_mismatches"] == []


async def test_fill_reports_lagging_are_picked_up_later(broker: LiveBroker, trader: FakeTrader) -> None:
    trader.next_fills = [("5.00", "0.42", True)]
    trader.hide_fills_once = True
    broker._sleep = _no_sleep  # FILL_RETRY: hidden once, then visible on the first retry
    o = await broker.place_order(buy("yes", "0.45", 5))
    assert o.status == "filled" and o.filled_count == 5


async def _no_sleep(_: float) -> None:
    return None


async def test_kalshi_rejection_releases_cash(broker: LiveBroker, trader: FakeTrader) -> None:
    trader.next_error = KalshiAPIError(400, "insufficient balance", "/portfolio/events/orders")
    o = await broker.place_order(buy("yes", "0.45", 10))
    assert o.status == "rejected" and "insufficient balance" in o.status_reason
    assert broker.cash == D("500") and not broker.open_orders()


async def test_unknown_submission_resolved_by_client_id(broker: LiveBroker, trader: FakeTrader) -> None:
    trader.next_fills = [("10.00", "0.42", True)]
    trader.next_error = OrderSubmitUnknown(None, "timeout", "/portfolio/events/orders")
    o = await broker.place_order(buy("yes", "0.45", 10))
    assert o.status == "open" and "unknown" in o.status_reason
    assert broker.reserved_cash > 0
    await broker.process_resting_orders()
    o2 = broker.get_order(o.id)
    assert o2 is not None and o2.status == "filled" and o2.filled_count == 10
    assert broker.reserved_cash == 0


async def test_unknown_submission_that_never_arrived(broker: LiveBroker, trader: FakeTrader,
                                                     clock: ManualClock) -> None:
    trader.next_error = OrderSubmitUnknown(None, "timeout", "/x")
    trader.orders.clear()
    o = await broker.place_order(buy("yes", "0.45", 10))
    trader.orders.clear()  # nothing reached the exchange
    await broker.process_resting_orders()
    assert broker.get_order(o.id).status == "open"  # still within the grace period
    clock.advance(120)
    await broker.process_resting_orders()
    o2 = broker.get_order(o.id)
    assert o2.status == "rejected" and "never reached" in o2.status_reason and broker.cash == D("500")


async def test_resting_order_fills_then_cancel(broker: LiveBroker, trader: FakeTrader) -> None:
    o = await broker.place_order(buy("yes", "0.40", 10, tif="gtc", expires_in_s=600))
    assert trader.created[0]["tif"] == "gtc" and trader.created[0]["expiration_ts"] is not None
    assert o.status == "open" and broker.reserved_cash > 0
    trader.fill_resting("x1", "4.00", "0.40")
    await broker.process_resting_orders()
    o = broker.get_order(o.id)
    assert o.status == "partially_filled" and o.filled_count == 4 and ledger_fills(broker)[0].is_taker is False
    out = await broker.cancel_order(o.id, reason="cancelled by user")
    assert trader.cancelled == ["x1"]
    assert out.status == "cancelled" and out.filled_count == 4 and broker.reserved_cash == 0
    assert broker.cash == D("500") - D("1.60")


async def test_live_caps_and_baskets(broker: LiveBroker, trader: FakeTrader) -> None:
    o = await broker.place_order(buy("yes", "0.45", 101))
    assert o.status == "rejected" and "max_order_contracts" in o.status_reason
    broker.max_order_cost = D("50")
    o = await broker.place_order(buy("yes", "0.99", 51))
    assert o.status == "rejected" and "max_order_cost" in o.status_reason
    legs = await broker.place_basket([buy("yes", "0.45", 1), buy("no", "0.45", 1)])
    assert all(x.status == "rejected" and "basket" in x.status_reason for x in legs)
    assert trader.created == []


async def test_restart_keeps_open_order_and_bookkeeping(md: StaticMarketData, clock: ManualClock,
                                                        trader: FakeTrader, tmp_path: Any) -> None:
    path = str(tmp_path / "live.sqlite3")
    b = LiveBroker(md, Store(path), trader, settings=Cfg(LiveCfg()), clock=clock)
    await b.start()
    o = await b.place_order(buy("yes", "0.40", 10, tif="gtc"))
    b.store.close()
    trader.fill_resting("x1", "10.00", "0.40")
    b2 = LiveBroker(md, Store(path), trader, settings=Cfg(LiveCfg()), clock=clock)
    assert b2.starting_balance == D("500")
    await b2.start()  # resolves the order left open
    o2 = b2.get_order(o.id)
    assert o2.status == "filled" and o2.filled_count == 10 and b2.exchange_order_id(o2) == "x1"


async def test_reconcile_reports_drift_and_mismatch(broker: LiveBroker, trader: FakeTrader) -> None:
    trader.balance = D("480")
    trader.positions = [{"ticker": "OTHER-1", "position_fp": "-3.00"}]
    st = await broker.reconcile()
    assert st["cash_drift"] == -20.0
    assert st["position_mismatches"] == [{"ticker": "OTHER-1", "ledger": 0.0, "exchange": -3.0}]
    delta = await broker.sync_cash_to_exchange()
    assert delta == D("-20") and broker.cash == D("480") and broker.starting_balance == D("480")


async def test_reset_refused_with_positions(broker: LiveBroker, trader: FakeTrader) -> None:
    trader.next_fills = [("1.00", "0.42", True)]
    await broker.place_order(buy("yes", "0.45", 1))
    with pytest.raises(ValueError):
        await broker.reset()


# --------------------------------------------------------------------------- config + API wiring


def test_live_settings_switch_storage_and_environment(tmp_path: Any) -> None:
    from kalshibot.config import load_settings

    cfg = tmp_path / "config.yaml"
    cfg.write_text("live:\n  enabled: true\n  environment: demo\n  private_key_path: keys/k.pem\n")
    s = load_settings(cfg, env={"KALSHIBOT_LIVE__PRIVATE_KEY_PEM": "", "KALSHIBOT_LIVE__API_KEY_ID": "abc"})
    assert s.live.enabled and s.live.api_key_id == "abc"
    assert s.storage.path == str((tmp_path / "data" / "kalshibot-live.sqlite3").resolve())
    assert s.kalshi.base_url == "https://demo-api.kalshi.co/trade-api/v2"
    assert s.live.private_key_path == str((tmp_path / "keys" / "k.pem").resolve())
    # the private key never ends up in a saved config
    s.live.private_key_pem = "SECRET"
    assert "private_key_pem" not in s.model_dump()["live"]
    paper = load_settings(None, env={})
    assert not paper.live.enabled and paper.storage.path.endswith("kalshibot.sqlite3")


def test_api_in_live_mode(settings: Any, tmp_path: Any) -> None:
    from conftest import FakeKalshiClient
    from fastapi.testclient import TestClient

    from kalshibot.api.server import build_services, create_app
    from kalshibot.feeds import FeedRegistry

    settings.live.enabled = True
    trader = FakeTrader(balance="250")
    svc = build_services(settings, client=FakeKalshiClient(), strategies={}, feeds=FeedRegistry(), trader=trader)
    assert isinstance(svc.broker, LiveBroker)
    app = create_app(settings, services=svc, frontend_dist=tmp_path / "nodist")
    with TestClient(app) as client:
        st = client.get("/api/status").json()
        assert st["mode"] == "live" and st["live"]["environment"] == "demo"
        assert st["engine"]["running"] is False  # live.autostart defaults to off
        assert client.get("/api/account").json()["starting_balance"] == 250
        assert client.get("/api/live").json()["exchange"]["balance"] == 250
        trader.balance = D("260")
        assert client.post("/api/live/reconcile").json()["exchange"]["cash_drift"] == 10
        assert client.post("/api/live/sync-cash").json()["booked"] == 10
        assert client.get("/api/account").json()["cash"] == 260


def test_live_endpoints_404_in_paper_mode(settings: Any, tmp_path: Any) -> None:
    from conftest import FakeKalshiClient
    from fastapi.testclient import TestClient

    from kalshibot.api.server import build_services, create_app
    from kalshibot.feeds import FeedRegistry

    svc = build_services(settings, client=FakeKalshiClient(), strategies={}, feeds=FeedRegistry())
    with TestClient(create_app(settings, services=svc, autostart=False, frontend_dist=tmp_path / "x")) as client:
        assert client.get("/api/status").json()["mode"] == "paper"
        assert client.get("/api/live").status_code == 404


# --------------------------------------------------------------------------- keys entered in the dashboard


def _pem(key: Any) -> str:
    from cryptography.hazmat.primitives import serialization

    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def test_credential_store_is_owner_only_and_write_only(tmp_path: Any, rsa_key: rsa.RSAPrivateKey) -> None:
    import os
    import stat

    from kalshibot.live.secrets import CredentialStore

    store = CredentialStore(tmp_path / "secrets" / "keys.json")
    pem = _pem(rsa_key)
    store.put("demo", "a1b2c3d4-0000-1111-2222-333344445555", pem)
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.path.parent).st_mode) == 0o700
    got = store.get("demo")
    assert got is not None and got.private_key_pem.strip() == pem.strip()
    summ = store.summary("demo")
    assert summ is not None and summ["api_key_id"] == "a1b2…5555" and len(summ["fingerprint"]) == 19
    assert "PRIVATE" not in json.dumps(summ)
    os.chmod(store.path, 0o644)
    store.get("demo")  # tightened on read
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert store.delete("demo") and not store.path.exists() and store.get("demo") is None


@pytest.mark.parametrize(("key_id", "pem", "msg"), [
    ("a1b2c3d4-0000", "not a key", "not a PEM private key"),
    ("a1b2c3d4-0000", "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----", "could not be read"),
    ("bad id!", None, "API key id"),
])
def test_credential_store_rejects_bad_input(tmp_path: Any, rsa_key: rsa.RSAPrivateKey, key_id: str,
                                            pem: str | None, msg: str) -> None:
    from kalshibot.live.secrets import CredentialStore

    store = CredentialStore(tmp_path / "k.json")
    with pytest.raises(ValueError, match=msg) as ei:
        store.put("demo", key_id, pem if pem is not None else _pem(rsa_key))
    assert "AAAA" not in str(ei.value) and not store.path.exists()


class FakeProbe:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok

    async def balance_dollars(self) -> Decimal:
        if not self.ok:
            from kalshibot.kalshi.trading import KalshiAuthError

            raise KalshiAuthError(401, "INCORRECT_API_KEY_SIGNATURE", "/portfolio/balance")
        return D("123.45")

    async def aclose(self) -> None:
        return None


H = {"X-Kalshibot-Request": "1", "Host": "localhost:8765"}


@pytest.fixture
def locked_live_app(settings: Any, tmp_path: Any, monkeypatch: Any) -> Any:
    """Live mode enabled, no key anywhere: the server starts locked."""
    from conftest import FakeKalshiClient
    from fastapi.testclient import TestClient

    from kalshibot.api import server
    from kalshibot.feeds import FeedRegistry

    settings.live.enabled = True
    settings.live.secrets_path = str(tmp_path / "secrets" / "keys.json")
    svc = server.build_services(settings, client=FakeKalshiClient(), strategies={}, feeds=FeedRegistry())
    assert svc.broker.trader.signer is None
    fake = FakeTrader(balance="123.45")
    real_trader = svc.broker.trader

    async def fake_balance() -> Decimal:  # the exchange calls go to the fake once a key is set
        return await fake.balance_dollars()

    for name in ("balance_dollars", "get_balance", "get_positions", "get_orders", "get_fills"):
        monkeypatch.setattr(real_trader, name, getattr(fake, name))
    probe = {"ok": True}
    monkeypatch.setattr(server, "make_probe", lambda signer, env, st: FakeProbe(probe["ok"]))
    app = server.create_app(settings, services=svc, frontend_dist=tmp_path / "nodist")
    with TestClient(app, base_url="http://localhost:8765") as client:
        yield client, svc, probe


def test_locked_live_mode_then_key_from_dashboard(locked_live_app: Any, rsa_key: rsa.RSAPrivateKey) -> None:
    client, svc, probe = locked_live_app
    st = client.get("/api/status").json()
    assert st["mode"] == "live" and st["live"]["ready"] is False and "API key" in st["live"]["blocked_reason"]
    assert client.post("/api/engine/start", json={}).status_code == 409
    o = asyncio_run(svc.broker.place_order(buy("yes", "0.45", 1)))
    assert o.status == "rejected" and "not ready" in o.status_reason

    pem = _pem(rsa_key)
    body = {"api_key_id": "a1b2c3d4-0000-1111-2222-333344445555", "private_key_pem": pem}
    probe["ok"] = False
    r = client.put("/api/live/credentials/demo", json=body, headers=H)
    assert r.status_code == 422 and "rejected this key" in r.json()["detail"]
    assert svc.credentials.get("demo") is None  # a key Kalshi refuses is never stored
    probe["ok"] = True
    r = client.put("/api/live/credentials/demo", json=body, headers=H)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["activated"] is True and out["ready"] is True and out["verified_balance"] == 123.45
    assert out["keys"]["demo"]["source"] == "dashboard" and out["keys"]["demo"]["api_key_id"] == "a1b2…5555"
    assert out["keys"]["prod"] == {"source": None}
    # the key never comes back
    for resp in (r, client.get("/api/live/credentials", headers=H), client.get("/api/status"),
                 client.get("/api/live"), client.get("/api/logs")):
        assert "PRIVATE KEY" not in resp.text and pem.splitlines()[1] not in resp.text
    assert svc.broker.ready and svc.broker.trader.signer is not None
    assert client.get("/api/account").json()["starting_balance"] == 123.45

    # removing it while the engine runs is refused; stopped, it locks live trading again
    assert client.post("/api/engine/start", json={}).status_code == 200
    assert client.delete("/api/live/credentials/demo", headers=H).status_code == 409
    client.post("/api/engine/stop", json={})
    r = client.delete("/api/live/credentials/demo", headers=H)
    assert r.status_code == 200 and r.json()["keys"]["demo"] == {"source": None}
    assert svc.broker.ready is False and svc.broker.trader.signer is None


def test_key_endpoints_refuse_cross_site_requests(locked_live_app: Any, rsa_key: rsa.RSAPrivateKey) -> None:
    client, svc, _ = locked_live_app
    body = {"api_key_id": "a1b2c3d4-0000-1111-2222", "private_key_pem": _pem(rsa_key)}
    url = "/api/live/credentials/demo"
    assert client.put(url, json=body, headers={"Host": "localhost:8765"}).status_code == 403  # no header
    assert client.put(url, json=body, headers={**H, "Host": "evil.example:8765"}).status_code == 403  # rebinding
    assert client.put(url, json=body, headers={**H, "Origin": "http://evil.example"}).status_code == 403
    assert client.get("/api/live/credentials", headers={"Host": "localhost:8765"}).status_code == 403
    assert svc.credentials.get("demo") is None
    # an unknown environment / extra fields are refused too
    assert client.put("/api/live/credentials/live", json=body, headers=H).status_code == 404
    assert client.put(url, json={**body, "x": 1}, headers=H).status_code == 422


def test_config_key_wins_over_dashboard(locked_live_app: Any, rsa_key: rsa.RSAPrivateKey) -> None:
    client, svc, _ = locked_live_app
    svc.settings.live.api_key_id = "cfgkey-123456"
    svc.settings.live.private_key_pem = _pem(rsa_key)
    r = client.put("/api/live/credentials/demo",
                   json={"api_key_id": "a1b2c3d4-0000", "private_key_pem": _pem(rsa_key)}, headers=H)
    assert r.status_code == 409 and "config.yaml" in r.json()["detail"]
    assert client.get("/api/live/credentials", headers=H).json()["keys"]["demo"]["source"] == "config"


def asyncio_run(coro: Any) -> Any:
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)
