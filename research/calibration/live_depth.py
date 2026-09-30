"""Capacity check for the 'back heavy favourites' family: snapshot LIVE order books (2026-09-26) of currently open
markets in the series the rule trades most, and measure how many contracts are offered on the YES ask side at
<= 0.99 when the YES bid is >= 0.97. YES asks = 1 - NO bids (orderbook_fp.no_dollars, bids only, ascending).
~50 requests at 2 req/s, cached under raw/live_depth_<date>.
    uv run --with pandas --with httpx python live_depth.py  -> live_depth.csv"""
import logging
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "data"))
import kalshi_client  # noqa: E402

kalshi_client.RAW_DIR = HERE / "raw"
kalshi_client.MIN_FREE_BYTES = 700 * 1024 * 1024
from kalshi_client import KalshiClient  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
NS = "live_depth_2026-09-26"
SERIES = ["KXWTI", "KXGOLDD", "KXINXU", "KXAAAGASD", "KXDIESELD", "KXRT", "KXTRUEV", "KXNASDAQ100U", "KXBTCD", "KXRAIN",
          "KXHIGHNY"]
c = KalshiClient(rate=2.0)
rows = []
for s in SERIES:
    js = c.get("/markets", {"series_ticker": s, "status": "open", "limit": 500, "mve_filter": "exclude"}, namespace=NS)
    ms = js.get("markets", [])
    cand = []
    for m in ms:
        yb, ya = float(m.get("yes_bid_dollars") or 0), float(m.get("yes_ask_dollars") or 1)
        if yb >= 0.97 and ya < 1:
            cand.append((m["ticker"], yb, ya, m.get("close_time"), float(m.get("volume_fp") or 0)))
    logging.info("%s: %d open markets, %d with yes_bid>=0.97", s, len(ms), len(cand))
    for tk, yb, ya, ct, vol in sorted(cand, key=lambda x: -x[4])[:5]:
        ob = c.get(f"/markets/{tk}/orderbook", {"depth": 20}, namespace=NS).get("orderbook_fp", {})
        no_b = [(float(p), float(q)) for p, q in (ob.get("no_dollars") or [])]
        yes_b = [(float(p), float(q)) for p, q in (ob.get("yes_dollars") or [])]
        asks = sorted((round(1 - p, 4), q) for p, q in no_b)            # YES asks ascending
        best_ask = asks[0][0] if asks else None
        rows.append(dict(series=s, ticker=tk, close_time=ct, lifetime_volume=vol, yes_bid=yb, yes_ask=ya,
                         ob_best_yes_ask=best_ask, size_at_best_ask=asks[0][1] if asks else 0,
                         size_ask_le_099=sum(q for p, q in asks if p <= 0.99 + 1e-9),
                         notional_ask_le_099=sum(p * q for p, q in asks if p <= 0.99 + 1e-9),
                         size_at_best_bid=yes_b[-1][1] if yes_b else 0))
df = pd.DataFrame(rows)
df.to_csv(HERE / "live_depth.csv", index=False)
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 20)
print(df.round(4).to_string(index=False))
if len(df):
    print(df.groupby("series")[["size_at_best_ask", "size_ask_le_099", "notional_ask_le_099"]].median().round(1))
    print("overall median contracts offered at <=0.99:", df.size_ask_le_099.median(), " p25:", df.size_ask_le_099.quantile(.25))
print(c.stats())
