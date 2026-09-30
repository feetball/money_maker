"""Rank online USD pairs by 24h USD notional and measure spread / depth within 10 bps from L2 books.

Writes a tiny CSV next to this script (liquidity_snapshot.csv).
"""
import csv
import statistics as st
import time
from decimal import Decimal
from pathlib import Path

from cbprobe import EXCH, get

prods = {p["id"]: p for p in get(f"{EXCH}/products").json()}
stats = get(f"{EXCH}/products/stats").json()
rows = []
for pid, s in stats.items():
    p = prods.get(pid)
    if not p or p["quote_currency"] != "USD" or p["status"] != "online" or p["trading_disabled"]:
        continue
    d = s.get("stats_24hour") or {}
    try:
        last = float(d["last"]); vol = float(d["volume"])
    except Exception:
        continue
    rows.append((pid, last * vol, last, vol, float((s.get("stats_30day") or {}).get("volume") or 0) * last))
rows.sort(key=lambda r: -r[1])
tot = sum(r[1] for r in rows)
print(f"online USD pairs with stats: {len(rows)}; total 24h USD notional ${tot/1e6:,.0f}M")
top = rows[:25]

N_ROUNDS = 3
meas = {pid: [] for pid, *_ in top}
for rnd in range(N_ROUNDS):
    for pid, *_ in top:
        r = get(f"{EXCH}/products/{pid}/book", {"level": 2})
        if r.status_code != 200:
            print(pid, r.status_code); continue
        b = r.json()
        bb, ba = float(b["bids"][0][0]), float(b["asks"][0][0])
        mid = (bb + ba) / 2
        spread_bps = (ba - bb) / mid * 1e4
        lo, hi = mid * (1 - 0.001), mid * (1 + 0.001)
        bid10 = sum(float(px) * float(sz) for px, sz, _ in b["bids"] if float(px) >= lo)
        ask10 = sum(float(px) * float(sz) for px, sz, _ in b["asks"] if float(px) <= hi)
        lo5, hi5 = mid * (1 - 0.0005), mid * (1 + 0.0005)
        bid5 = sum(float(px) * float(sz) for px, sz, _ in b["bids"] if float(px) >= lo5)
        ask5 = sum(float(px) * float(sz) for px, sz, _ in b["asks"] if float(px) <= hi5)
        top_usd = min(float(b["bids"][0][0]) * float(b["bids"][0][1]), float(b["asks"][0][0]) * float(b["asks"][0][1]))
        meas[pid].append((spread_bps, bid10, ask10, bid5, ask5, top_usd, len(b["bids"]) + len(b["asks"])))
    if rnd < N_ROUNDS - 1:
        time.sleep(20)

out = Path(__file__).with_name("liquidity_snapshot.csv")
with out.open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["rank", "product", "usd_24h_m", "usd_30d_avg_daily_m", "last", "quote_increment", "tick_bps", "base_increment",
                "min_market_funds", "spread_bps_median", "depth10bps_bid_usd_k", "depth10bps_ask_usd_k", "depth5bps_bid_usd_k",
                "depth5bps_ask_usd_k", "top_level_usd_min_k", "levels", "limit_only", "max_slippage_pct"])
    print(f"\n{'#':>2} {'product':<11} {'24h $M':>8} {'30d avg $M':>10} {'tick bps':>8} {'spread bps':>10} {'±10bps bid $k':>13} {'±10bps ask $k':>13} {'L1 $k':>7}")
    for i, (pid, usd24, last, vol, usd30) in enumerate(top, 1):
        m = meas[pid]
        if not m:
            continue
        sp = st.median(x[0] for x in m)
        b10 = st.median(x[1] for x in m); a10 = st.median(x[2] for x in m)
        b5 = st.median(x[3] for x in m); a5 = st.median(x[4] for x in m)
        l1 = st.median(x[5] for x in m)
        p = prods[pid]
        tick_bps = float(p["quote_increment"]) / last * 1e4
        w.writerow([i, pid, round(usd24 / 1e6, 2), round(usd30 / 30 / 1e6, 2), last, p["quote_increment"], round(tick_bps, 3), p["base_increment"],
                    p["min_market_funds"], round(sp, 3), round(b10 / 1e3, 1), round(a10 / 1e3, 1), round(b5 / 1e3, 1), round(a5 / 1e3, 1),
                    round(l1 / 1e3, 2), m[-1][6], p["limit_only"], p["max_slippage_percentage"]])
        print(f"{i:>2} {pid:<11} {usd24/1e6:8.1f} {usd30/30/1e6:10.1f} {tick_bps:8.3f} {sp:10.2f} {b10/1e3:13.0f} {a10/1e3:13.0f} {l1/1e3:7.1f}  qi={p['quote_increment']} bi={p['base_increment']}")
print("wrote", out)
