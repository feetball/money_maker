"""Fetch Coinbase Exchange 1-min candles (public, no auth) -> data/spot_<PRODUCT>.csv
columns: ts (candle START, unix s), low, high, open, close, volume
Coinbase returns max 300 candles/request, newest first.
"""
import csv
import datetime as dt
import os
import sys
import time

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
RAW = os.path.join(HERE, "cache", "spot")
os.makedirs(DATA, exist_ok=True)
os.makedirs(RAW, exist_ok=True)

cl = httpx.Client(timeout=30, headers={"User-Agent": "research-bot/0.1"})


def fetch_chunk(product, start, end):
    path = os.path.join(RAW, f"{product}_{start}_{end}.csv")
    if os.path.exists(path):
        with open(path) as fh:
            return [list(map(float, r)) for r in csv.reader(fh)]
    delay = 1
    for _ in range(10):
        time.sleep(0.2)
        r = cl.get(f"https://api.exchange.coinbase.com/products/{product}/candles", params={
            "granularity": 60,
            "start": dt.datetime.fromtimestamp(start, dt.timezone.utc).isoformat(),
            "end": dt.datetime.fromtimestamp(end, dt.timezone.utc).isoformat()})
        if r.status_code == 200:
            rows = r.json()
            if end < time.time() - 3600:  # only cache completed history
                with open(path, "w", newline="") as fh:
                    csv.writer(fh).writerows(rows)
            return rows
        time.sleep(delay)
        delay = min(delay * 2, 30)
    raise RuntimeError(f"coinbase fail {product} {start}")


def main(product, start_date, end_date):
    s = int(dt.datetime.fromisoformat(start_date).replace(tzinfo=dt.timezone.utc).timestamp())
    e = int(dt.datetime.fromisoformat(end_date).replace(tzinfo=dt.timezone.utc).timestamp())
    allrows = {}
    t = s
    step = 300 * 60
    n = 0
    while t < e:
        rows = fetch_chunk(product, t, min(t + step - 60, e))
        for r in rows:
            allrows[int(r[0])] = r
        t += step
        n += 1
        if n % 50 == 0:
            print(product, dt.datetime.fromtimestamp(t, dt.timezone.utc), len(allrows), file=sys.stderr)
    out = os.path.join(DATA, f"spot_{product}.csv")
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "low", "high", "open", "close", "volume"])
        for k in sorted(allrows):
            r = allrows[k]
            w.writerow([int(r[0]), r[1], r[2], r[3], r[4], r[5]])
    print(product, "rows", len(allrows), file=sys.stderr)


if __name__ == "__main__":
    start, end = sys.argv[1], sys.argv[2]
    for p in sys.argv[3:]:
        main(p, start, end)
