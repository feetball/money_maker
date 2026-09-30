"""Tiny rate-limited HTTP helper for probing Coinbase public APIs.

Keeps this process under 3 req/s (shared public IP budget) and backs off on 429.
Paper-research only: no auth headers, no order endpoints.
"""
from __future__ import annotations

import time

import httpx

EXCH = "https://api.exchange.coinbase.com"
AT = "https://api.coinbase.com/api/v3/brokerage"

_MIN_GAP = 0.40  # seconds between requests -> <= 2.5 req/s
_last = [0.0]
_client = httpx.Client(timeout=20.0, headers={"User-Agent": "kalshibot-paper-research/0.1"})


def get(url: str, params: dict | None = None, retries: int = 5) -> httpx.Response:
    for attempt in range(retries):
        gap = time.monotonic() - _last[0]
        if gap < _MIN_GAP:
            time.sleep(_MIN_GAP - gap)
        _last[0] = time.monotonic()
        r = _client.get(url, params=params)
        if r.status_code == 429:
            time.sleep(2.0 * (attempt + 1))
            continue
        return r
    return r


def hdrs(r: httpx.Response, keys=("cache-control", "cb-before", "cb-after", "age", "cf-cache-status",
                                   "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
                                   "ratelimit-limit", "ratelimit-remaining", "etag", "last-modified")) -> dict:
    return {k: r.headers.get(k) for k in keys if r.headers.get(k) is not None}
