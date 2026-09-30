"""Fetch Coinbase Exchange public candles for the literature sanity-check backtests.

Public, unauthenticated endpoint only (no keys, no orders). Rate-limited to <= 2 req/s
(shared public-IP budget is 3 req/s across agents) with backoff on 429.

Output: research/coinbase/literature/data/{PRODUCT}_{1d|1h}.parquet (zstd-compressed).
Candle row format from Coinbase: [time, low, high, open, close, volume]; time = bucket start (UTC).
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pandas as pd

EXCH = "https://api.exchange.coinbase.com"
OUT = Path(__file__).resolve().parent / "data"
OUT.mkdir(exist_ok=True)
GAP = 0.5
_last = [0.0]
_client = httpx.Client(timeout=30.0, headers={"User-Agent": "kalshibot-paper-research/0.1"})


def get(url, params):
    for attempt in range(8):
        dt = time.monotonic() - _last[0]
        if dt < GAP:
            time.sleep(GAP - dt)
        _last[0] = time.monotonic()
        try:
            r = _client.get(url, params=params)
        except httpx.HTTPError:
            time.sleep(2.0 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2.0 * (attempt + 1))
            continue
        return r
    raise RuntimeError(f"failed {url} {params}")


def fetch(product: str, gran: int, start: datetime, end: datetime) -> pd.DataFrame:
    step = timedelta(seconds=gran * 300)
    rows = []
    t = start
    empty_run = 0
    while t < end:
        t2 = min(t + step, end)
        r = get(f"{EXCH}/products/{product}/candles",
                {"granularity": gran, "start": t.isoformat(), "end": t2.isoformat()})
        if r.status_code != 200:
            print(product, r.status_code, r.text[:200], file=sys.stderr)
            break
        data = r.json()
        rows.extend(data)
        empty_run = 0 if data else empty_run + 1
        t = t2
    df = pd.DataFrame(rows, columns=["time", "low", "high", "open", "close", "volume"])
    if df.empty:
        return df
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)
    return df


if __name__ == "__main__":
    end = datetime(2026, 9, 27, tzinfo=timezone.utc)
    daily = ["BTC-USD", "ETH-USD", "LTC-USD", "BCH-USD", "SOL-USD", "LINK-USD", "XRP-USD",
             "DOGE-USD", "ADA-USD", "AVAX-USD", "DOT-USD", "XLM-USD"]
    for p in daily:
        f = OUT / f"{p}_1d.parquet"
        if f.exists():
            continue
        df = fetch(p, 86400, datetime(2015, 1, 1, tzinfo=timezone.utc), end)
        df.to_parquet(f, compression="zstd", index=False)
        print(p, "1d", len(df), df["time"].min() if len(df) else None, flush=True)
    for p in ["BTC-USD", "ETH-USD"]:
        f = OUT / f"{p}_1h.parquet"
        if f.exists():
            continue
        df = fetch(p, 3600, datetime(2017, 1, 1, tzinfo=timezone.utc), end)
        df.to_parquet(f, compression="zstd", index=False)
        print(p, "1h", len(df), df["time"].min() if len(df) else None, flush=True)
