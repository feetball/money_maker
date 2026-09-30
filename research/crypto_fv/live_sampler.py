"""Poll live orderbooks for near-the-money strikes of the next-expiring KXBTCD/KXETHD hourly
events, plus Coinbase spot, every ~30s. Records depth at best and within 2c for capacity.
Output: data/live_books.jsonl
"""
import datetime as dt
import json
import os
import sys
import time

import httpx

B = "https://api.elections.kalshi.com/trade-api/v2"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "live_books.jsonl")
cl = httpx.Client(timeout=20)


def get(url, params=None):
    d = 1
    for _ in range(6):
        r = cl.get(url, params=params)
        if r.status_code == 200:
            return r.json()
        time.sleep(d)
        d *= 2
    return None


def spot(prod):
    d = get(f"https://api.exchange.coinbase.com/products/{prod}/ticker")
    return float(d["price"]) if d else None


def run(duration_s, series_list=("KXBTCD", "KXETHD"), n_strikes=6, period=30):
    t_end = time.time() + duration_s
    with open(OUT, "a") as fh:
        while time.time() < t_end:
            now = time.time()
            for series, prod in zip(series_list, ["BTC-USD", "ETH-USD"]):
                S = spot(prod)
                ms = get(B + "/markets", {"series_ticker": series, "status": "open", "limit": 1000})
                if not ms or S is None:
                    continue
                ms = ms["markets"]
                closes = sorted(set(m["close_time"] for m in ms))
                nxt = closes[0]
                cand = [m for m in ms if m["close_time"] == nxt]
                cand.sort(key=lambda m: abs(m["floor_strike"] - S))
                for m in cand[:n_strikes]:
                    ob = get(B + f"/markets/{m['ticker']}/orderbook", {"depth": 10})
                    time.sleep(0.25)
                    rec = {"ts": now, "series": series, "ticker": m["ticker"], "close_time": nxt,
                           "strike": m["floor_strike"], "spot": S,
                           "book": (ob or {}).get("orderbook_fp")}
                    fh.write(json.dumps(rec) + "\n")
                fh.flush()
            time.sleep(max(0, period - (time.time() - now)))


if __name__ == "__main__":
    run(float(sys.argv[1]) if len(sys.argv) > 1 else 3600)
