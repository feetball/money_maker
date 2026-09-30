"""Staleness of Exchange /trades (default page) vs /ticker vs AT ticker: newest trade_id seen by each."""
import datetime as dt, time
from cbprobe import AT, EXCH, get
for i in range(10):
    r1 = get(f"{EXCH}/products/BTC-USD/trades", {"limit": 100})
    r2 = get(f"{EXCH}/products/BTC-USD/ticker")
    r3 = get(f"{AT}/market/products/BTC-USD/ticker", {"limit": 1})
    t1 = r1.json()[0]; t2 = r2.json(); t3 = r3.json()["trades"][0]
    age = lambda s: time.time() - dt.datetime.fromisoformat(s[:26].rstrip('Z') + "+00:00").timestamp()
    print(f"/trades newest {t1['trade_id']} age {age(t1['time']):5.2f}s cf={r1.headers.get('cf-cache-status')} age_hdr={r1.headers.get('age')} | "
          f"/ticker {t2['trade_id']} age {age(t2['time']):5.2f}s | AT {t3['trade_id']} age {age(t3['time']):5.2f}s")
    time.sleep(1.5)
