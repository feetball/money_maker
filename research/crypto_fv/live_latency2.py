"""Live latency study for thin 15-min crypto markets: poll Coinbase spot + Kalshi top-of-book
every ~2s and log, so we can measure how long spot-vs-quote mispricings persist (seconds).
Output: data/live_latency.jsonl
Usage: live_latency.py DURATION_S
"""
import json
import os
import sys
import time

import httpx

B = "https://api.elections.kalshi.com/trade-api/v2"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "live_latency2.jsonl")
ASSETS = {"KXBTC15M": "BTC-USD", "KXDOGE15M": "DOGE-USD", "KXSOL15M": "SOL-USD"}
cl = httpx.Client(timeout=10)


def get(url, params=None):
    for i in range(4):
        try:
            r = cl.get(url, params=params)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(1 + 2 * i)
        except httpx.HTTPError:
            time.sleep(0.5)
    return None


def current_market(series):
    d = get(B + "/markets", {"series_ticker": series, "status": "open", "limit": 10})
    if not d or not d.get("markets"):
        return None
    m = sorted(d["markets"], key=lambda x: x["close_time"])[0]
    return m


def main(duration):
    t_end = time.time() + duration
    mk = {}
    with open(OUT, "a") as fh:
        while time.time() < t_end:
            loop0 = time.time()
            for series, prod in ASSETS.items():
                m = mk.get(series)
                if m is None or m["_close"] <= time.time():
                    m = current_market(series)
                    if m is None:
                        continue
                    import datetime as dt
                    m["_close"] = dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp()
                    mk[series] = m
                tk = get(f"https://api.exchange.coinbase.com/products/{prod}/ticker")
                t_spot = time.time()
                ob = get(B + f"/markets/{m['ticker']}/orderbook", {"depth": 3})
                t_ob = time.time()
                if not tk or not ob:
                    continue
                book = ob.get("orderbook_fp") or {}
                yes = book.get("yes_dollars") or []
                no = book.get("no_dollars") or []
                rec = {"t_spot": t_spot, "t_ob": t_ob, "series": series, "ticker": m["ticker"],
                       "close_ts": m["_close"], "strike": m.get("floor_strike"),
                       "spot": float(tk["price"]), "spot_bid": float(tk.get("bid") or 0), "spot_ask": float(tk.get("ask") or 0),
                       "yes_bid": float(yes[-1][0]) if yes else 0.0, "yes_bid_sz": float(yes[-1][1]) if yes else 0.0,
                       "yes_ask": 1 - float(no[-1][0]) if no else 1.0, "yes_ask_sz": float(no[-1][1]) if no else 0.0}
                fh.write(json.dumps(rec) + "\n")
            fh.flush()
            time.sleep(max(0.0, 1.5 - (time.time() - loop0)))


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 1200)
