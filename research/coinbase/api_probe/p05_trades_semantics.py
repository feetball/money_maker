"""Trade tape semantics: side meaning across REST APIs, /trades cursor behaviour, auth-only endpoints."""
from collections import Counter

from cbprobe import AT, EXCH, get, hdrs

# 1) Exchange /trades vs Advanced Trade market ticker for the same trade_ids
ex = get(f"{EXCH}/products/BTC-USD/trades", {"limit": 300}).json()
at = get(f"{AT}/market/products/BTC-USD/ticker", {"limit": 100}).json()["trades"]
exm = {int(t["trade_id"]): t for t in ex}
atm = {int(t["trade_id"]): t for t in at}
both = sorted(set(exm) & set(atm))
same = sum(1 for i in both if exm[i]["side"].lower() == atm[i]["side"].lower())
print(f"REST side check: overlap {len(both)}; equal {same}; opposite {len(both) - same}")
print("sample:", [(i, exm[i]["side"], atm[i]["side"], exm[i]["price"], atm[i]["price"]) for i in both[:3]])

# 2) Exchange /trades cursor semantics
newest = max(exm)
print("\nnewest trade_id", newest)
for cur in (newest - 50, newest - 500, newest - 5000):
    r = get(f"{EXCH}/products/BTC-USD/trades", {"before": cur, "limit": 10})
    ids = [t["trade_id"] for t in r.json()]
    print(f"before={cur}: got {ids[0]}..{ids[-1]} (n={len(ids)}) cb-before={r.headers.get('cb-before')} cb-after={r.headers.get('cb-after')}")
for cur in (newest - 50,):
    r = get(f"{EXCH}/products/BTC-USD/trades", {"after": cur, "limit": 10})
    ids = [t["trade_id"] for t in r.json()]
    print(f"after={cur}: got {ids[0]}..{ids[-1]} (n={len(ids)}) cb-before={r.headers.get('cb-before')} cb-after={r.headers.get('cb-after')}")
# walk the tape backwards: how far back can /trades go? try an old cursor
for cur in (500_000_000, 100_000_000, 10_000_000, 1000):
    r = get(f"{EXCH}/products/BTC-USD/trades", {"after": cur, "limit": 3})
    js = r.json() if r.status_code == 200 else r.text[:200]
    print(f"after={cur}: HTTP {r.status_code}", js if isinstance(js, str) else [(t['trade_id'], t['time']) for t in js])

# 3) Advanced Trade ticker time-window params
import time
now = int(time.time())
r = get(f"{AT}/market/products/BTC-USD/ticker", {"limit": 100, "start": now - 3600 * 24 * 3, "end": now - 3600 * 24 * 3 + 60})
js = r.json()
tr = js.get("trades", [])
print(f"\nAT ticker start/end 3 days ago window: HTTP {r.status_code} n={len(tr)}", (tr[0]["time"], tr[-1]["time"]) if tr else r.text[:200])

# 4) Endpoints that need auth
for path in ("/fees", "/accounts", "/orders", "/users/self/trailing-volume"):
    r = get(f"{EXCH}{path}")
    print(f"EXCH {path}: HTTP {r.status_code} {r.text[:120]}")
r = get(f"{AT}/transaction_summary")
print("AT transaction_summary:", r.status_code, r.text[:120])
