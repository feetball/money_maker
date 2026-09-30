"""Minimal rate-limited, caching Kalshi public REST client (no auth; market data only).

- Global rate limit: <= 4.5 req/s (shared across threads in this process).
- Exponential backoff on HTTP 429 / 5xx.
- Optional on-disk cache of raw JSON responses (cache_dir) so re-runs are free.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import httpx

BASE = "https://api.elections.kalshi.com/trade-api/v2"


class RateLimiter:
    def __init__(self, rate: float):
        self.min_interval = 1.0 / rate
        self.lock = threading.Lock()
        self.next_t = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if now < self.next_t:
                time.sleep(self.next_t - now)
                now = time.monotonic()
            self.next_t = now + self.min_interval


class Kalshi:
    def __init__(self, rate: float = 4.5, cache_dir: str | None = None, timeout: float = 20.0):
        self.rl = RateLimiter(rate)
        self.client = httpx.Client(base_url=BASE, timeout=timeout, headers={"User-Agent": "research-arb-scanner/0.1"})
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.n_requests = 0

    def _cache_path(self, path: str, params: dict | None) -> Path | None:
        if not self.cache_dir:
            return None
        key = path + "?" + json.dumps(params or {}, sort_keys=True)
        h = hashlib.sha1(key.encode()).hexdigest()[:20]
        return self.cache_dir / f"{h}.json.gz"

    def get(self, path: str, params: dict | None = None, use_cache: bool = False) -> dict:
        cp = self._cache_path(path, params) if use_cache else None
        if cp and cp.exists():
            with gzip.open(cp, "rt") as f:
                return json.load(f)
        delay = 1.0
        for attempt in range(8):
            self.rl.wait()
            try:
                r = self.client.get(path, params=params)
                self.n_requests += 1
            except httpx.HTTPError as e:  # network hiccup
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            r.raise_for_status()
            data = r.json()
            if cp:
                with gzip.open(cp, "wt") as f:
                    json.dump(data, f)
            return data
        raise RuntimeError(f"GET {path} {params} failed after retries")

    def paginate(self, path: str, params: dict, key: str, use_cache: bool = False, max_pages: int = 10_000):
        params = dict(params)
        out = []
        for _ in range(max_pages):
            d = self.get(path, params, use_cache=use_cache)
            out.extend(d.get(key, []) or [])
            cur = d.get("cursor")
            if not cur:
                break
            params["cursor"] = cur
        return out
