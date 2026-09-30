"""Tiny cached, rate-limited HTTP client for Kalshi public API + spot exchanges.

- Global rate limit (default 4 req/s) across the process.
- Exponential backoff on 429 / 5xx.
- Raw JSON cached to disk under cache/http/<sha1>.json so reruns are free.
"""
import hashlib
import json
import os
import threading
import time

import httpx

BASE = "https://api.elections.kalshi.com/trade-api/v2"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache", "http")
os.makedirs(CACHE_DIR, exist_ok=True)

_lock = threading.Lock()
_last = [0.0]
MIN_INTERVAL = 0.25  # 4 req/s
_client = httpx.Client(timeout=30.0, headers={"User-Agent": "research-bot/0.1"})


def _throttle():
    with _lock:
        now = time.time()
        wait = _last[0] + MIN_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()


def _key(url, params):
    s = url + "?" + json.dumps(params or {}, sort_keys=True)
    return hashlib.sha1(s.encode()).hexdigest()


def get(url, params=None, cache=True, max_tries=8):
    if not url.startswith("http"):
        url = BASE + url
    k = _key(url, params)
    path = os.path.join(CACHE_DIR, k + ".json")
    if cache and os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    delay = 1.0
    for attempt in range(max_tries):
        _throttle()
        try:
            r = _client.get(url, params=params)
        except httpx.HTTPError as e:
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} {url} {params}: {r.text[:300]}")
        data = r.json()
        if cache:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        return data
    raise RuntimeError(f"gave up {url} {params}")


def paginate(path, params, key, cache=True, max_pages=1000):
    out = []
    cursor = None
    p = dict(params)
    for _ in range(max_pages):
        if cursor:
            p["cursor"] = cursor
        d = get(path, p, cache=cache)
        out.extend(d.get(key) or [])
        cursor = d.get("cursor")
        if not cursor:
            break
    return out
