"""Probe Advanced Trade public market endpoints (no auth): shapes, caching, symbol differences."""
import json
from collections import Counter

from cbprobe import AT, EXCH, get, hdrs


def show(label, r, n=700):
    print(f"\n=== {label}  HTTP {r.status_code}  {hdrs(r)}")
    print(r.text[:n])


show("time", get(f"{AT}/time"))
r = get(f"{AT}/market/products")
js = r.json()
prods = js.get("products", [])
print(f"\n=== market/products HTTP {r.status_code} {hdrs(r)} top-keys={list(js)} n={len(prods)} num_products={js.get('num_products')}")
keys = Counter()
for p in prods:
    keys.update(p.keys())
print("fields:", dict(keys))
print("product_type:", Counter(p.get("product_type") for p in prods))
print("status:", Counter(p.get("status") for p in prods))
print("quote:", Counter(p.get("quote_currency_id") for p in prods).most_common(10))
print("trading_disabled:", Counter(p.get("trading_disabled") for p in prods))
print("is_disabled:", Counter(p.get("is_disabled") for p in prods))
print("view_only:", Counter(p.get("view_only") for p in prods))
print("new:", Counter(p.get("new") for p in prods))
print("fcm_trading_session_details present:", sum(1 for p in prods if p.get("fcm_trading_session_details")))
print("alias examples:", [(p["product_id"], p.get("alias"), p.get("alias_to")) for p in prods if p.get("alias") or p.get("alias_to")][:20])
for pid in ("BTC-USD", "BTC-USDC", "ETH-USDC", "SOL-USDC"):
    m = [p for p in prods if p["product_id"] == pid]
    print(pid, json.dumps(m[0])[:1600] if m else None)

# pagination / filters
for params in ({"limit": 5}, {"product_type": "SPOT", "limit": 3}, {"product_type": "FUTURE", "limit": 2},
               {"product_ids": ["BTC-USD", "ETH-USD"]}, {"get_all_products": "true", "limit": 2}):
    rr = get(f"{AT}/market/products", params)
    jj = rr.json()
    print("params", params, rr.status_code, "n=", len(jj.get("products", [])), "num_products", jj.get("num_products"),
          [p["product_id"] for p in jj.get("products", [])][:5])

show("market/products/BTC-USD", get(f"{AT}/market/products/BTC-USD"), 2000)
show("market/products/BTC-USDC", get(f"{AT}/market/products/BTC-USDC"), 400)
r = get(f"{AT}/market/product_book", {"product_id": "BTC-USD"})
jb = r.json()
pb = jb.get("pricebook", {})
print(f"\n=== product_book default HTTP {r.status_code} {hdrs(r)} keys={list(jb)} bids={len(pb.get('bids', []))} asks={len(pb.get('asks', []))}")
print(json.dumps(jb)[:600])
for lim in (1, 50, 100, 250, 500, 1000, 5000):
    rr = get(f"{AT}/market/product_book", {"product_id": "BTC-USD", "limit": lim})
    pbb = rr.json().get("pricebook", {})
    print("limit", lim, rr.status_code, len(pbb.get("bids", [])), len(pbb.get("asks", [])), hdrs(rr).get("cache-control"))
rr = get(f"{AT}/market/product_book", {"product_id": "BTC-USD", "limit": 10, "aggregation_price_increment": "10"})
print("aggregation_price_increment=10:", rr.status_code, rr.text[:500])
r = get(f"{AT}/market/products/BTC-USD/ticker", {"limit": 5})
show("market ticker (trades)", r, 1200)
rr = get(f"{AT}/market/products/BTC-USD/ticker", {"limit": 1000})
jt = rr.json()
print("ticker limit=1000:", rr.status_code, len(jt.get("trades", [])), "best_bid", jt.get("best_bid"), "best_ask", jt.get("best_ask"))
if jt.get("trades"):
    print("first", jt["trades"][0], "last", jt["trades"][-1])
    print("sides:", Counter(t["side"] for t in jt["trades"]))
rr = get(f"{AT}/market/products/BTC-USD/ticker", {"limit": 1001})
print("ticker limit=1001:", rr.status_code, rr.text[:200] if rr.status_code != 200 else len(rr.json().get("trades", [])))

# candles
import time
now = int(time.time())
r = get(f"{AT}/market/products/BTC-USD/candles", {"start": now - 3600, "end": now, "granularity": "ONE_MINUTE"})
jc = r.json()
print(f"\n=== AT candles HTTP {r.status_code} {hdrs(r)} n={len(jc.get('candles', []))}")
print(jc.get("candles", [])[:2], jc.get("candles", [])[-1:])
for g in ("ONE_MINUTE", "FIVE_MINUTE", "FIFTEEN_MINUTE", "THIRTY_MINUTE", "ONE_HOUR", "TWO_HOUR", "FOUR_HOUR", "SIX_HOUR", "ONE_DAY", "ONE_WEEK"):
    secs = {"ONE_MINUTE": 60, "FIVE_MINUTE": 300, "FIFTEEN_MINUTE": 900, "THIRTY_MINUTE": 1800, "ONE_HOUR": 3600, "TWO_HOUR": 7200,
            "FOUR_HOUR": 14400, "SIX_HOUR": 21600, "ONE_DAY": 86400, "ONE_WEEK": 604800}[g]
    rr = get(f"{AT}/market/products/BTC-USD/candles", {"start": now - secs * 350, "end": now, "granularity": g})
    print(g, rr.status_code, len(rr.json().get("candles", [])) if rr.status_code == 200 else rr.text[:200])
for n in (300, 350, 351, 400):
    rr = get(f"{AT}/market/products/BTC-USD/candles", {"start": now - 60 * n, "end": now, "granularity": "ONE_MINUTE"})
    print("AT 1m span", n, rr.status_code, len(rr.json().get("candles", [])) if rr.status_code == 200 else rr.text[:200])
rr = get(f"{AT}/market/products/BTC-USD/candles", {"start": now - 60 * 100, "end": now, "granularity": "ONE_MINUTE", "limit": 10})
print("AT limit=10", rr.status_code, len(rr.json().get("candles", [])) if rr.status_code == 200 else rr.text[:200])

# fields on exchange-only endpoints under AT?
show("AT best_bid_ask (auth?)", get(f"{AT}/best_bid_ask", {"product_ids": "BTC-USD"}), 300)
show("AT products (auth?)", get(f"{AT}/products/BTC-USD"), 300)
