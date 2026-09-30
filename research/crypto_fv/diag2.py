import sys, numpy as np, pandas as pd
from backtest import load, cluster_ci
from fv_model import fee_per_contract
series = sys.argv[1]
p = load(series)
cut = p.close_ts.quantile(0.5); p["split"] = np.where(p.close_ts<=cut,"train","test")
q = p[(p.yes_ask<1)&(p.yes_ask>0)].copy()
q["pnl"] = q.y - q.yes_ask - fee_per_contract(q.yes_ask); q["px"]=q.yes_ask
n = p[(p.yes_bid>0)&(p.yes_bid<1)].copy()
n["px"] = 1-n.yes_bid; n["pnl"] = (1-n.y) - n.px - fee_per_contract(n.px)
for name, d in [("YES", q), ("NO", n)]:
    for lo, hi in [(0.80,0.94),(0.90,0.97),(0.94,0.99)]:
        for sp in ["train","test"]:
            for lagset in [(1,2,3),(5,10,15,20),(30,45,55)]:
                x = d[(d.px>lo)&(d.px<=hi)&(d.split==sp)&(d.lag.isin(lagset))]
                lo_ci, hi_ci = cluster_ci(x)
                print(name, lo, hi, sp, lagset, len(x), round(100*x.pnl.mean(),3), (round(100*lo_ci,2), round(100*hi_ci,2)))
# drift over sample
ev = p.groupby("event_ticker").agg(close=("close_ts","first"), xv=("expiration_value","first")).sort_values("close")
print("underlying first/last settle:", ev.xv.iloc[0], ev.xv.iloc[-1], "split mid:", ev[ev.close<=cut].xv.iloc[-1])
