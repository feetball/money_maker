"""Unauthenticated WebSocket subscribe test for Coinbase Exchange feed and Advanced Trade WS.

One connection per channel, ~6 s each, prints message types, counts and a truncated sample.
Also cross-checks trade `side` semantics: Exchange ticker vs matches for the same trade_id.
"""
import asyncio
import json
import time
from collections import Counter, defaultdict

import websockets

EX_WS = "wss://ws-feed.exchange.coinbase.com"
AT_WS = "wss://advanced-trade-ws.coinbase.com"
PID = "BTC-USD"


async def probe(url, sub, label, secs=6.0, keep=None):
    out = {"label": label, "types": Counter(), "samples": {}, "err": None}
    try:
        async with websockets.connect(url, max_size=2**24, open_timeout=10) as ws:
            await ws.send(json.dumps(sub))
            t0 = time.time()
            while time.time() - t0 < secs:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=secs)
                except asyncio.TimeoutError:
                    break
                m = json.loads(raw)
                typ = m.get("type") or m.get("channel")
                if "events" in m and m["events"]:
                    typ = f"{m.get('channel')}/{m['events'][0].get('type')}"
                out["types"][typ] += 1
                if typ not in out["samples"]:
                    out["samples"][typ] = (raw[:700], len(raw))
                if keep is not None:
                    keep.append(m)
    except Exception as e:  # noqa
        out["err"] = repr(e)[:300]
    print(f"\n### {label}  err={out['err']}  types={dict(out['types'])}")
    for k, (s, n) in out["samples"].items():
        print(f"  [{k}] ({n} bytes) {s}")
    return out


async def main():
    # --- Exchange feed ---
    for ch in ("heartbeat", "ticker", "ticker_batch", "matches", "level2", "level2_batch", "full", "level3", "status", "auctionfeed", "rfq_matches"):
        sub = {"type": "subscribe", "product_ids": [PID], "channels": [ch]}
        if ch == "status":
            sub = {"type": "subscribe", "channels": [{"name": "status"}]}
        await probe(EX_WS, sub, f"EXCHANGE {ch}", secs=5.0)
        await asyncio.sleep(0.5)

    # side semantics: ticker vs matches on same connection
    msgs = []
    await probe(EX_WS, {"type": "subscribe", "product_ids": [PID], "channels": ["ticker", "matches"]}, "EXCHANGE ticker+matches (side check)", secs=12.0, keep=msgs)
    tk = {m["trade_id"]: m for m in msgs if m.get("type") == "ticker"}
    mt = {m["trade_id"]: m for m in msgs if m.get("type") in ("match", "last_match")}
    both = set(tk) & set(mt)
    same = sum(1 for t in both if tk[t]["side"] == mt[t]["side"])
    print(f"ticker/match overlap {len(both)} trades; side equal on {same}, opposite on {len(both) - same}")
    for t in list(both)[:3]:
        print("  ticker:", {k: tk[t].get(k) for k in ("trade_id", "side", "price", "best_bid", "best_ask", "last_size")},
              " match:", {k: mt[t].get(k) for k in ("trade_id", "side", "price", "size", "maker_order_id", "taker_order_id")})

    # --- Advanced Trade WS ---
    for ch in ("heartbeats", "ticker", "ticker_batch", "market_trades", "level2", "candles", "status", "user"):
        sub = {"type": "subscribe", "product_ids": [PID], "channel": ch}
        await probe(AT_WS, sub, f"ADVANCED {ch}", secs=6.0)
        await asyncio.sleep(0.5)


asyncio.run(main())
