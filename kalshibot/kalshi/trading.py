"""Authenticated Kalshi client for LIVE trading (real money).

Only :class:`~kalshibot.live.broker.LiveBroker` uses this. Market data keeps using the public
:class:`~kalshibot.kalshi.client.KalshiClient`.

Auth (docs.kalshi.com, OpenAPI ``securitySchemes``): every request carries

* ``KALSHI-ACCESS-KEY``: the API key id,
* ``KALSHI-ACCESS-TIMESTAMP``: the request time in milliseconds,
* ``KALSHI-ACCESS-SIGNATURE``: base64 of a signature over ``timestamp + METHOD + path``.
  ``path`` includes the ``/trade-api/v2`` prefix and excludes the query string. RSA keys sign
  with RSA-PSS/SHA-256 (salt length = digest length). Ed25519 keys sign the raw message.

Orders use the V2 shape (``POST /portfolio/events/orders``). ``side`` is a YES-book side
(``bid`` = buy YES, ``ask`` = sell YES = buy NO) and ``price`` is always the YES price in
fixed-point dollars. :func:`v2_side_price` converts the bot's (side, action, limit) into it.

Retries: reads retry on 429/5xx/transport errors like the public client. **Order creation
never retries automatically.** A timeout there is ambiguous (the order may be live), so it
raises :class:`OrderSubmitUnknown` and the broker settles it by ``client_order_id`` later.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx

from kalshibot.kalshi.client import (
    RETRY_STATUSES,
    KalshiAPIError,
    KalshiNotFound,
    KalshiRateLimited,
    SleepFn,
    TokenBucket,
    _clean_params,
    _error_message,
)
from kalshibot.money import ONE, D

__all__ = [
    "DEMO_BASE_URL",
    "PROD_BASE_URL",
    "KalshiAuthError",
    "KalshiSigner",
    "KalshiTradingClient",
    "OrderSubmitUnknown",
    "base_url_for",
    "load_private_key",
    "v2_side_price",
]

log = logging.getLogger(__name__)

PROD_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"

#: Kalshi's tif names for the bot's ``ioc``/``gtc`` (+ ``fok`` used for live taker orders).
TIF_NAMES = {"ioc": "immediate_or_cancel", "gtc": "good_till_canceled", "fok": "fill_or_kill"}


def base_url_for(environment: str) -> str:
    env = (environment or "prod").lower()
    if env in ("prod", "production", "live"):
        return PROD_BASE_URL
    if env == "demo":
        return DEMO_BASE_URL
    raise ValueError(f"unknown Kalshi environment {environment!r} (use prod or demo)")


class KalshiAuthError(KalshiAPIError):
    """HTTP 401/403: bad key id, bad signature, clock skew, or missing permission."""


class OrderSubmitUnknown(KalshiAPIError):
    """The order request failed in a way that leaves its fate unknown (timeout, 5xx,
    connection drop after sending). Resolve it by ``client_order_id``; never resend blindly."""


# --------------------------------------------------------------------------- signing


def load_private_key(*, path: str | None = None, pem: str | None = None) -> Any:
    """Load an RSA or Ed25519 private key (PEM, unencrypted) from ``pem`` text or ``path``."""
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError as e:  # pragma: no cover - a declared dependency
        raise RuntimeError("live trading needs the 'cryptography' package") from e
    if pem:
        data = pem.replace("\\n", "\n").encode()
    elif path:
        with open(os.path.expanduser(path), "rb") as fh:
            data = fh.read()
    else:
        raise ValueError("no private key: set live.private_key_path or KALSHIBOT_LIVE__PRIVATE_KEY_PEM")
    return serialization.load_pem_private_key(data, password=None)


class KalshiSigner:
    """Builds the ``KALSHI-ACCESS-*`` headers for one request."""

    def __init__(self, key_id: str, private_key: Any, *, clock_ms: Callable[[], int] | None = None):
        if not key_id:
            raise ValueError("no API key id: set live.api_key_id or KALSHIBOT_LIVE__API_KEY_ID")
        self.key_id = key_id
        self._key = private_key
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def sign(self, message: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

        if isinstance(self._key, rsa.RSAPrivateKey):
            return self._key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                       salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        if isinstance(self._key, ed25519.Ed25519PrivateKey):
            return self._key.sign(message)
        raise TypeError(f"unsupported key type {type(self._key).__name__} (need RSA or Ed25519)")

    def headers(self, method: str, path: str) -> dict[str, str]:
        ts = str(self._clock_ms())
        sig = self.sign(f"{ts}{method.upper()}{path}".encode())
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}


# --------------------------------------------------------------------------- order shape


def _fp(x: Decimal, places: int) -> str:
    return f"{x:.{places}f}"


def v2_side_price(side: str, action: str, limit_price: Decimal) -> tuple[str, Decimal]:
    """(book side, YES price) for an order to ``action`` ``side`` at ``limit_price``.

    buy YES @ p -> bid @ p; sell NO @ q -> bid @ 1-q (both end long YES);
    buy NO @ q -> ask @ 1-q; sell YES @ p -> ask @ p (both end long NO)."""
    buy_yes = (side == "yes") == (action == "buy")
    yes_price = limit_price if side == "yes" else ONE - limit_price
    return ("bid" if buy_yes else "ask"), yes_price


# --------------------------------------------------------------------------- client


class KalshiTradingClient:
    """Signed REST client for the portfolio endpoints. Use ``async with`` or ``aclose()``.

    ``signer`` may be None (no key yet): every request then raises :class:`KalshiAuthError`
    until :attr:`signer` is set (keys entered in the dashboard are swapped in at runtime)."""

    def __init__(
        self,
        signer: KalshiSigner | None,
        base_url: str = PROD_BASE_URL,
        *,
        read_rps: float = 10.0,
        write_rps: float = 5.0,
        timeout: float = 10.0,
        max_tries: int = 4,
        backoff_base: float = 0.5,
        backoff_max: float = 10.0,
        subaccount: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: SleepFn = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.signer = signer
        self.base_url = base_url.rstrip("/")
        self._prefix = urlsplit(self.base_url).path.rstrip("/")  # "/trade-api/v2": part of what is signed
        self.max_tries = max(1, int(max_tries))
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.subaccount = subaccount
        self._sleep = sleep
        self.read_limiter = TokenBucket(read_rps, 2.0, clock=clock, sleep=sleep)
        self.write_limiter = TokenBucket(write_rps, 2.0, clock=clock, sleep=sleep)
        self._http = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, transport=transport,
            headers={"User-Agent": "kalshibot-live/0.1", "Accept": "application/json"},
        )
        self.request_count = 0
        self.error_count = 0

    async def __aenter__(self) -> KalshiTradingClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- transport -------------------------------------------------------------------

    def _backoff(self, attempt: int) -> float:
        base = min(self.backoff_max, self.backoff_base * (2**attempt))
        return base * (0.5 + random.random() / 2)

    async def _send(self, method: str, path: str, *, params: Mapping[str, Any] | None = None,
                    json: Any = None) -> httpx.Response:
        if self.signer is None:
            raise KalshiAuthError(None, "no Kalshi API key configured", path)
        limiter = self.read_limiter if method == "GET" else self.write_limiter
        await limiter.acquire()
        self.request_count += 1
        headers = self.signer.headers(method, self._prefix + path)
        return await self._http.request(method, path, params=_clean_params(params), json=json, headers=headers)

    def _raise_for(self, resp: httpx.Response, path: str) -> None:
        msg = _error_message(resp)
        if resp.status_code in (401, 403):
            raise KalshiAuthError(resp.status_code, msg, path)
        if resp.status_code == 404:
            raise KalshiNotFound(404, msg, path)
        if resp.status_code == 429:
            raise KalshiRateLimited(429, msg, path)
        raise KalshiAPIError(resp.status_code, msg, path)

    @staticmethod
    def _json(resp: httpx.Response, path: str) -> dict[str, Any]:
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            data = resp.json()
        except ValueError as e:
            raise KalshiAPIError(resp.status_code, f"invalid JSON: {e}", path) from e
        if not isinstance(data, dict):
            raise KalshiAPIError(resp.status_code, "expected a JSON object", path)
        return data

    async def request(self, method: str, path: str, *, params: Mapping[str, Any] | None = None,
                      json: Any = None, retry: bool = True) -> dict[str, Any]:
        """Signed request. ``retry`` (idempotent calls only) retries 429/5xx/transport errors."""
        tries = self.max_tries if retry else 1
        last: Exception | None = None
        for attempt in range(tries):
            try:
                resp = await self._send(method, path, params=params, json=json)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                self.error_count += 1
                last = KalshiAPIError(None, f"{type(e).__name__}: {e}", path)
                if attempt + 1 < tries:
                    await self._sleep(self._backoff(attempt))
                continue
            if resp.status_code in RETRY_STATUSES:
                self.error_count += 1
                last = KalshiAPIError(resp.status_code, _error_message(resp), path)
                if resp.status_code == 429:
                    last = KalshiRateLimited(429, _error_message(resp), path)
                if attempt + 1 < tries:
                    await self._sleep(self._backoff(attempt))
                continue
            if resp.status_code >= 400:
                self.error_count += 1
                self._raise_for(resp, path)
            return self._json(resp, path)
        assert last is not None
        raise last

    def _sub(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.subaccount is not None:
            params["subaccount"] = self.subaccount
        return params

    # -- account -------------------------------------------------------------------------

    async def get_balance(self) -> dict[str, Any]:
        """``{balance (cents), balance_dollars, portfolio_value (cents), updated_ts}``."""
        return await self.request("GET", "/portfolio/balance", params=self._sub({}))

    async def balance_dollars(self) -> Decimal:
        b = await self.get_balance()
        if b.get("balance_dollars") not in (None, ""):
            return D(b["balance_dollars"])
        return D(b.get("balance", 0)) / 100

    async def get_positions(self, *, ticker: str | None = None) -> list[dict[str, Any]]:
        """All unsettled market positions with a non-zero position (paginated)."""
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(50):
            d = await self.request("GET", "/portfolio/positions", params=self._sub(
                {"limit": 1000, "cursor": cursor, "ticker": ticker, "count_filter": "position"}))
            out.extend(d.get("market_positions") or [])
            cursor = d.get("cursor") or None
            if not cursor:
                break
        return out

    # -- orders ----------------------------------------------------------------------------

    async def create_order(self, *, ticker: str, side: str, action: str, count: int, limit_price: Decimal,
                           tif: str, client_order_id: str, expiration_ts: int | None = None,
                           post_only: bool = False, reduce_only: bool = False,
                           cancel_order_on_pause: bool = True) -> dict[str, Any]:
        """Place one order (V2). ``side``/``action``/``limit_price`` are in the bot's terms
        (price for ``side``). Returns ``{order_id, client_order_id, fill_count, remaining_count,
        average_fill_price?, average_fee_paid?, ts_ms}``.

        Raises :class:`KalshiAPIError` (definitive rejection: nothing was placed) or
        :class:`OrderSubmitUnknown` (timeout/5xx/429: the order may or may not exist)."""
        book_side, yes_price = v2_side_price(side, action, limit_price)
        body: dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": book_side,
            "count": _fp(D(count), 2),
            "price": _fp(yes_price, 4),
            "time_in_force": TIF_NAMES[tif],
            "self_trade_prevention_type": "taker_at_cross",
            "cancel_order_on_pause": cancel_order_on_pause,
        }
        if post_only:
            body["post_only"] = True
        if reduce_only:
            body["reduce_only"] = True
        if expiration_ts is not None and tif == "gtc":
            body["expiration_time"] = int(expiration_ts)
        if self.subaccount is not None:
            body["subaccount"] = self.subaccount
        path = "/portfolio/events/orders"
        try:
            resp = await self._send("POST", path, json=body)
        except httpx.TimeoutException as e:
            self.error_count += 1
            raise OrderSubmitUnknown(None, f"timeout: {e}", path) from e
        except httpx.TransportError as e:
            self.error_count += 1
            # a connect error means the request never left; anything later is ambiguous
            if isinstance(e, httpx.ConnectError):
                raise KalshiAPIError(None, f"connect failed: {e}", path) from e
            raise OrderSubmitUnknown(None, f"{type(e).__name__}: {e}", path) from e
        if resp.status_code >= 500 or resp.status_code == 429:
            self.error_count += 1
            raise OrderSubmitUnknown(resp.status_code, _error_message(resp), path)
        if resp.status_code >= 400:
            self.error_count += 1
            self._raise_for(resp, path)
        return self._json(resp, path)

    async def cancel_order(self, order_id: str, ticker: str) -> dict[str, Any]:
        """Cancel a resting order. 404 (already gone) raises :class:`KalshiNotFound`."""
        return await self.request("DELETE", f"/portfolio/events/orders/{order_id}",
                                  params=self._sub({"market_ticker": ticker}))

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return (await self.request("GET", f"/portfolio/orders/{order_id}")).get("order") or {}

    async def get_orders(self, *, ticker: str | None = None, status: str | None = None,
                         min_ts: int | None = None, max_pages: int = 10) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            d = await self.request("GET", "/portfolio/orders", params=self._sub(
                {"ticker": ticker, "status": status, "min_ts": min_ts, "limit": 1000, "cursor": cursor}))
            out.extend(d.get("orders") or [])
            cursor = d.get("cursor") or None
            if not cursor:
                break
        return out

    async def get_fills(self, *, order_id: str | None = None, ticker: str | None = None,
                        min_ts: int | None = None, max_pages: int = 10) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            d = await self.request("GET", "/portfolio/fills", params=self._sub(
                {"order_id": order_id, "ticker": ticker, "min_ts": min_ts, "limit": 1000, "cursor": cursor}))
            out.extend(d.get("fills") or [])
            cursor = d.get("cursor") or None
            if not cursor:
                break
        return out
