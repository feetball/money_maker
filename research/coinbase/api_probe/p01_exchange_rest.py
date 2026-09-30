"""Probe Coinbase Exchange public REST shapes: products, book, ticker, trades, candles, stats."""
import json
from collections import Counter

from cbprobe import EXCH, get, hdrs


def show(label, r, n=600):
    print(f"\n=== {label}  HTTP {r.status_code}  {hdrs(r)}")
    print(r.text[:n])


r = get(f"{EXCH}/products")
prods = r.json()
print("products:", len(prods), hdrs(r))
keys = Counter()
for p in prods:
    keys.update(p.keys())
print("field presence:", dict(keys))
print("status:", Counter(p["status"] for p in prods))
print("quote ccy:", Counter(p["quote_currency"] for p in prods).most_common(12))
for flag in ("post_only", "limit_only", "cancel_only", "trading_disabled", "auction_mode", "fx_stablecoin", "margin_enabled"):
    print(flag, Counter(p.get(flag) for p in prods))
print("quote_increment (USD):", Counter(p["quote_increment"] for p in prods if p["quote_currency"] == "USD").most_common(12))
print("base_increment (USD):", Counter(p["base_increment"] for p in prods if p["quote_currency"] == "USD").most_common(12))
print("min_market_funds (USD):", Counter(p["min_market_funds"] for p in prods if p["quote_currency"] == "USD").most_common(8))
print("max_slippage_percentage:", Counter(p.get("max_slippage_percentage") for p in prods).most_common(8))
print("high_bid_limit_percentage:", Counter(p.get("high_bid_limit_percentage") for p in prods).most_common(8))
print("status_message nonempty:", [(p["id"], p["status"], p["status_message"]) for p in prods if p.get("status_message")][:10])
print("non-online / flagged:", [(p["id"], p["status"], p["post_only"], p["limit_only"], p["cancel_only"], p["trading_disabled"], p.get("auction_mode")) for p in prods if p["status"] != "online" or p["post_only"] or p["limit_only"] or p["cancel_only"] or p["trading_disabled"] or p.get("auction_mode")][:40])
usdc = [p["id"] for p in prods if p["quote_currency"] == "USDC"]
print("USDC-quoted:", len(usdc), usdc[:30])
print("examples:", [p for p in prods if p["id"] in ("ETH-USD", "SHIB-USD", "USDT-USD", "USDC-EUR", "BTC-USDC", "DOGE-USD", "XRP-USD")])

show("book level1", get(f"{EXCH}/products/BTC-USD/book", {"level": 1}))
r = get(f"{EXCH}/products/BTC-USD/book", {"level": 2})
b = r.json()
print(f"\n=== book level2 HTTP {r.status_code} {hdrs(r)} keys={list(b)} nbids={len(b['bids'])} nasks={len(b['asks'])}")
print("bids[:3]", b["bids"][:3], "asks[:3]", b["asks"][:3], "seq", b.get("sequence"), "time", b.get("time"),
      "auction_mode", b.get("auction_mode"), "auction", b.get("auction"))
print("worst bid", b["bids"][-1], "worst ask", b["asks"][-1])
show("book level3 (unauth)", get(f"{EXCH}/products/BTC-USD/book", {"level": 3}), 300)
show("book no level", get(f"{EXCH}/products/BTC-USD/book"), 300)
show("ticker", get(f"{EXCH}/products/BTC-USD/ticker"))
show("stats", get(f"{EXCH}/products/BTC-USD/stats"))
r = get(f"{EXCH}/products/stats")
print(f"\n=== /products/stats HTTP {r.status_code} {hdrs(r)} len={len(r.text)}")
try:
    js = r.json()
    k = list(js)[:2]
    print({kk: js[kk] for kk in k})
except Exception as e:
    print("not json", e, r.text[:200])
show("volume-summary", get(f"{EXCH}/products/volume-summary"), 400)
show("time", get(f"{EXCH}/time"))
show("currencies/BTC", get(f"{EXCH}/currencies/BTC"), 800)

r = get(f"{EXCH}/products/BTC-USD/trades")
t = r.json()
print(f"\n=== trades HTTP {r.status_code} {hdrs(r)} n={len(t)}")
print(t[:3])
print("first trade_id", t[0]["trade_id"], "last trade_id", t[-1]["trade_id"])
after = r.headers["cb-after"]
r2 = get(f"{EXCH}/products/BTC-USD/trades", {"after": after, "limit": 5})
t2 = r2.json()
print("page after=", after, "->", [x["trade_id"] for x in t2], hdrs(r2))
r3 = get(f"{EXCH}/products/BTC-USD/trades", {"before": t[0]["trade_id"] - 10, "limit": 5})
print("before=", t[0]["trade_id"] - 10, "->", [x["trade_id"] for x in r3.json()], hdrs(r3))
r4 = get(f"{EXCH}/products/BTC-USD/trades", {"limit": 1000})
print("limit=1000 ->", r4.status_code, len(r4.json()) if r4.status_code == 200 else r4.text[:200])
r4 = get(f"{EXCH}/products/BTC-USD/trades", {"limit": 1001})
print("limit=1001 ->", r4.status_code, len(r4.json()) if r4.status_code == 200 else r4.text[:200])
print("sides in last 1000:", Counter(x["side"] for x in r4.json()) if r4.status_code == 200 else None)

r = get(f"{EXCH}/products/BTC-USD/candles", {"granularity": 60})
c = r.json()
print(f"\n=== candles default HTTP {r.status_code} {hdrs(r)} n={len(c)} first={c[0]} last={c[-1]}")
for g in (60, 300, 900, 3600, 21600, 86400, 120, 1800, 14400, 604800):
    rr = get(f"{EXCH}/products/BTC-USD/candles", {"granularity": g})
    print("granularity", g, rr.status_code, (len(rr.json()) if rr.status_code == 200 else rr.text[:150]))
import datetime as dt
end = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
for n in (300, 301, 350):
    start = end - dt.timedelta(minutes=n)
    rr = get(f"{EXCH}/products/BTC-USD/candles", {"granularity": 60, "start": start.isoformat(), "end": end.isoformat()})
    js = rr.json() if rr.status_code == 200 else rr.text[:200]
    print(f"range {n} min ->", rr.status_code, len(js) if isinstance(js, list) else js,
          (js[0][0], js[-1][0]) if isinstance(js, list) and js else "")
