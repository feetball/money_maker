"""60-second unauthenticated WS capture (matches + level2_batch) to size maker-fill assumptions.

Per product: taker orders/min, USD volume/min by maker side, share of taker orders that sweep >1 level,
median L1 queue (USD) at the touch, and a naive 'time to trade through the L1 queue' estimate.
"""
import asyncio
import json
import statistics as st
import time
from collections import defaultdict

import websockets

PIDS = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD"]
SECS = 60


async def main():
    books = {p: {"buy": {}, "sell": {}} for p in PIDS}
    matches = defaultdict(list)
    l1 = defaultdict(list)
    async with websockets.connect("wss://ws-feed.exchange.coinbase.com", max_size=2**26) as ws:
        await ws.send(json.dumps({"type": "subscribe", "product_ids": PIDS, "channels": ["matches", "level2_batch"]}))
        t0 = time.time(); last_sample = 0
        while time.time() - t0 < SECS:
            m = json.loads(await asyncio.wait_for(ws.recv(), 10))
            typ = m.get("type"); pid = m.get("product_id")
            if typ == "snapshot":
                books[pid]["buy"] = {float(p): float(s) for p, s in m["bids"]}
                books[pid]["sell"] = {float(p): float(s) for p, s in m["asks"]}
            elif typ == "l2update":
                for side, p, s in m["changes"]:
                    p = float(p); s = float(s)
                    if s == 0:
                        books[pid][side].pop(p, None)
                    else:
                        books[pid][side][p] = s
            elif typ == "match":
                matches[pid].append(m)
            if time.time() - last_sample > 1.0:
                last_sample = time.time()
                for p in PIDS:
                    b, a = books[p]["buy"], books[p]["sell"]
                    if b and a:
                        bb = max(b); ba = min(a)
                        l1[p].append((bb * b[bb], ba * a[ba], (ba - bb) / ((ba + bb) / 2) * 1e4))
    mins = SECS / 60
    print(f"{'product':<9} {'takers/min':>10} {'$vol/min hit-bid':>16} {'$vol/min lift-ask':>17} {'%sweep>1lvl':>11} {'med L1 bid $':>12} {'med L1 ask $':>12} {'med spr bps':>11} {'L1bid/(hit$/s) s':>16}")
    for p in PIDS:
        ms = matches[p]
        by_taker = defaultdict(set)
        for m in ms:
            by_taker[m["taker_order_id"]].add(m["price"])
        hit = sum(float(m["price"]) * float(m["size"]) for m in ms if m["side"] == "buy") / mins   # maker buy = bid hit
        lift = sum(float(m["price"]) * float(m["size"]) for m in ms if m["side"] == "sell") / mins
        sweep = 100 * sum(1 for v in by_taker.values() if len(v) > 1) / max(1, len(by_taker))
        mb = st.median(x[0] for x in l1[p]) if l1[p] else float("nan")
        ma = st.median(x[1] for x in l1[p]) if l1[p] else float("nan")
        sp = st.median(x[2] for x in l1[p]) if l1[p] else float("nan")
        ttq = mb / (hit / 60) if hit else float("inf")
        print(f"{p:<9} {len(by_taker)/mins:10.0f} {hit:16,.0f} {lift:17,.0f} {sweep:11.1f} {mb:12,.0f} {ma:12,.0f} {sp:11.2f} {ttq:16.1f}")


asyncio.run(main())
