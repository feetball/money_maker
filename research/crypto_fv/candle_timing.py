"""Which moment does a Kalshi 1-min candle's yes_bid/yes_ask 'close' represent?
Compare candle closes (end_period_ts = T) for the tickers recorded live with our ~1.5s book snapshots
taken at T+offset, for offsets in [-30s, +10s]; report match rate by offset."""
import json, numpy as np, pandas as pd
import kclient as k
d = pd.DataFrame([json.loads(l) for l in open("data/live_latency2.jsonl")])
rows = []
for tk, g in d.groupby("ticker"):
    ser = g.series.iloc[0]
    st, en = int(g.t_ob.min()) - 60, int(g.t_ob.max()) + 60
    r = k.get(f"/series/{ser}/markets/{tk}/candlesticks", {"start_ts": st, "end_ts": en, "period_interval": 1}, cache=False)
    for c in r.get("candlesticks") or []:
        yb = (c.get("yes_bid") or {}).get("close_dollars"); ya = (c.get("yes_ask") or {}).get("close_dollars")
        if yb is None or ya is None: continue
        rows.append(dict(ticker=tk, T=c["end_period_ts"], cb=float(yb), ca=float(ya)))
C = pd.DataFrame(rows)
print("candles", len(C), "tickers", C.ticker.nunique())
g = d.sort_values("t_ob")
res = {}
for off in [-30, -20, -15, -10, -6, -4, -3, -2, -1, 0, 1, 2, 4, 6, 10]:
    m = []
    for _, c in C.iterrows():
        s = g[(g.ticker == c.ticker)]
        tt = c["T"] + off
        j = s.t_ob.searchsorted(tt) - 1  # last snapshot at or before tt
        if j < 0 or j >= len(s) or tt - s.t_ob.iloc[j] > 3: continue
        m.append((abs(s.yes_bid.iloc[j] - c.cb) < 1e-6) and (abs(s.yes_ask.iloc[j] - c.ca) < 1e-6))
    res[off] = (round(float(np.mean(m)), 3), len(m))
print("match rate of candle (bid,ask) close vs our snapshot at T+offset (offset s: (rate, n)):")
for kk, v in res.items(): print(f"  {kk:+4d}s  {v}")
