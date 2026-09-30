"""AT ticker limit behaviour; error shapes for bad product / bad params."""
from cbprobe import AT, EXCH, get
for lim in (100, 101, 250, 500, 1000):
    r = get(f"{AT}/market/products/BTC-USD/ticker", {"limit": lim})
    print("AT ticker limit", lim, r.status_code, len(r.json().get("trades", [])) if r.status_code == 200 else r.text[:120])
for url, params in ((f"{EXCH}/products/NOPE-USD", None), (f"{EXCH}/products/NOPE-USD/book", {"level": 2}),
                    (f"{EXCH}/products/BTC-USD/book", {"level": 4}), (f"{EXCH}/products/BTC-USD/trades", {"limit": 0}),
                    (f"{AT}/market/products/NOPE-USD", None), (f"{AT}/market/product_book", {"product_id": "NOPE-USD"})):
    r = get(url, params)
    print(url.split('.com')[1], params, r.status_code, r.text[:160])
