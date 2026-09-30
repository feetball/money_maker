"""Cross-check AT WS market_trades `side` against Exchange WS matches `side` (maker side) for the same trade_id."""
import asyncio, json, time
import websockets

async def grab(url, sub, secs, out):
    async with websockets.connect(url, max_size=2**24) as ws:
        await ws.send(json.dumps(sub))
        t0 = time.time()
        while time.time() - t0 < secs:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), secs))
            except asyncio.TimeoutError:
                break
            out.append(m)

async def main():
    a, b = [], []
    await asyncio.gather(
        grab("wss://advanced-trade-ws.coinbase.com", {"type": "subscribe", "product_ids": ["BTC-USD"], "channel": "market_trades"}, 15, a),
        grab("wss://ws-feed.exchange.coinbase.com", {"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["matches"]}, 15, b))
    at = {}
    for m in a:
        for ev in m.get("events", []):
            if ev.get("type") == "update":
                for t in ev.get("trades", []):
                    at[int(t["trade_id"])] = t["side"]
    ex = {m["trade_id"]: m["side"] for m in b if m.get("type") in ("match", "last_match")}
    both = set(at) & set(ex)
    same = sum(1 for i in both if at[i].lower() == ex[i].lower())
    print(f"AT market_trades vs EX matches: overlap {len(both)}, same side {same}, opposite {len(both)-same}")

asyncio.run(main())
