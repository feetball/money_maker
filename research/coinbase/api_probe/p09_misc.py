"""Misc checks: stable pairs, USDC aliasing, dust prints on the tape, L2 num-orders, AT alias book identity."""
from collections import Counter
from decimal import Decimal
from cbprobe import AT, EXCH, get

prods = get(f"{EXCH}/products").json()
fx = sorted(p["id"] for p in prods if p.get("fx_stablecoin") and p["status"] == "online")
print("Exchange fx_stablecoin (online):", len(fx), fx)
print("USDC-USD on Exchange:", [p["id"] for p in prods if p["id"] in ("USDC-USD", "USD-USDC")])
print("limit_only online:", sorted(p["id"] for p in prods if p["limit_only"] and p["status"] == "online"))
print("high_bid_limit_percentage set:", sorted(p["id"] for p in prods if p.get("high_bid_limit_percentage")))

at = get(f"{AT}/market/products", {"product_type": "SPOT"}).json()["products"]
atm = {p["product_id"]: p for p in at}
print("AT USDC-USD:", atm.get("USDC-USD", {}).get("status"), "| AT products w/o Exchange twin:",
      len([p for p in atm if p not in {q['id'] for q in prods} and not atm[p].get('alias')]))
alias = [p for p in at if p.get("alias")]
print("AT alias products (USDC->USD):", len(alias))
# does BTC-USDC ticker return BTC-USD trades?
t = get(f"{AT}/market/products/BTC-USDC/ticker", {"limit": 3}).json()
print("AT BTC-USDC ticker trades product_id:", [x["product_id"] for x in t["trades"]], [x["trade_id"] for x in t["trades"]])
# products only on AT (not on Exchange) - e.g. USDC-quoted non-alias
only_at = sorted(p for p in atm if p not in {q['id'] for q in prods})
print("AT-only product ids (sample):", len(only_at), only_at[:15])
# min sizes on AT for a few
for pid in ("BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "XRP-USD", "SHIB-USD", "USDT-USD"):
    p = atm.get(pid, {})
    print(pid, {k: p.get(k) for k in ("base_increment", "quote_increment", "price_increment", "base_min_size", "base_max_size", "quote_min_size", "quote_max_size")})

# dust prints on the tape
tr = get(f"{EXCH}/products/BTC-USD/trades", {"limit": 1000}).json()
notional = [Decimal(x["price"]) * Decimal(x["size"]) for x in tr]
tot = sum(notional)
small = [n for n in notional if n < 1]
print(f"BTC-USD last 1000 prints: {len(small)} (<$1 each) = {100*len(small)/len(tr):.0f}% of prints, {100*sum(small)/tot:.3f}% of notional; span {tr[-1]['time']} .. {tr[0]['time']}; total ${tot:,.0f}")
# L2 num-orders
b = get(f"{EXCH}/products/BTC-USD/book", {"level": 2}).json()
print("L2 top 5 bids [price,size,num_orders]:", b["bids"][:5])
