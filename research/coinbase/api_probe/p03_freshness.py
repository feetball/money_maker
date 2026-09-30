"""Measure data freshness / CDN caching of Exchange vs Advanced Trade public market endpoints.

Each loop: Exchange L1 book, Exchange ticker, AT product_book(limit=1), AT ticker(limit=1).
Compares the newest trade_id seen by each API and the server-stamped book time vs wall clock.
"""
import datetime as dt
import time

from cbprobe import AT, EXCH, get


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")[:26] + "+00:00" if "." in s else s.replace("Z", "+00:00"))


rows = []
for i in range(15):
    t0 = time.time()
    r1 = get(f"{EXCH}/products/BTC-USD/book", {"level": 1})
    b1 = r1.json()
    book_age = time.time() - dt.datetime.fromisoformat(b1["time"][:26] + "+00:00").timestamp()
    r2 = get(f"{EXCH}/products/BTC-USD/ticker")
    tk = r2.json()
    r3 = get(f"{AT}/market/product_book", {"product_id": "BTC-USD", "limit": 1})
    pb = r3.json()
    r4 = get(f"{AT}/market/products/BTC-USD/ticker", {"limit": 1})
    at = r4.json()
    at_tid = int(at["trades"][0]["trade_id"])
    at_trade_age = time.time() - dt.datetime.fromisoformat(at["trades"][0]["time"][:26] + "+00:00").timestamp()
    ex_trade_age = time.time() - dt.datetime.fromisoformat(tk["time"][:26] + "+00:00").timestamp()
    pbt = pb["pricebook"].get("time")
    at_book_age = (time.time() - dt.datetime.fromisoformat(pbt[:26] + "+00:00").timestamp()) if pbt else None
    print(f"{i:2d} EX-L1 bid {b1['bids'][0][0]} ask {b1['asks'][0][0]} age {book_age:5.2f}s cf={r1.headers.get('cf-cache-status')}/{r1.headers.get('age')} | "
          f"EX-tk tid {tk['trade_id']} age {ex_trade_age:5.2f}s cf={r2.headers.get('cf-cache-status')} | "
          f"AT-book bid {pb['pricebook']['bids'][0]['price']} ask {pb['pricebook']['asks'][0]['price']} t={pbt} age={at_book_age if at_book_age is None else round(at_book_age,2)} cf={r3.headers.get('cf-cache-status')} | "
          f"AT-tk tid {at_tid} age {at_trade_age:5.2f}s cf={r4.headers.get('cf-cache-status')}")
    time.sleep(0.5)

# Exchange candles: default (no start/end) vs explicit range freshness
now = time.time()
r = get(f"{EXCH}/products/BTC-USD/candles", {"granularity": 60})
c = r.json()
print("\nEX candles default newest start", c[0][0], "lag(s) vs now", round(now - c[0][0], 1), "cf", r.headers.get("cf-cache-status"), "age", r.headers.get("age"))
end = dt.datetime.fromtimestamp(now, dt.timezone.utc).replace(microsecond=0)
r = get(f"{EXCH}/products/BTC-USD/candles", {"granularity": 60, "start": (end - dt.timedelta(minutes=10)).isoformat(), "end": end.isoformat()})
c = r.json()
print("EX candles explicit range newest start", c[0][0], "lag(s)", round(now - c[0][0], 1), "n", len(c), "cf", r.headers.get("cf-cache-status"), "age", r.headers.get("age"), r.headers.get("cache-control"))
r = get(f"{AT}/market/products/BTC-USD/candles", {"granularity": "ONE_MINUTE", "start": int(now) - 600, "end": int(now)})
c = r.json()["candles"]
print("AT candles newest start", c[0]["start"], "lag(s)", round(now - int(c[0]["start"]), 1), "n", len(c), "cf", r.headers.get("cf-cache-status"))
r = get(f"{AT}/market/products/BTC-USD/candles", {"granularity": "ONE_WEEK", "start": int(now) - 604800 * 10, "end": int(now)})
print("AT ONE_WEEK 10 weeks:", r.status_code, r.text[:200])
r = get(f"{AT}/market/products/BTC-USD/candles", {"granularity": "ONE_DAY", "start": int(now) - 86400 * 400, "end": int(now) - 86400 * 60})
print("AT ONE_DAY 340d:", r.status_code, len(r.json().get("candles", [])) if r.status_code == 200 else r.text[:200])
