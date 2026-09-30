"""Pre-registered replication of the BTC15M mid-window favorite rule on other 15-min series.
Rule (frozen from BTC15M analysis): lag=10 min before close, favorite side ask in [0.85,0.97],
model hl10 EWMA, Student-t nu=3.5, vol_mult=1.1, buy if p_side - ask - fee > 0.01, execute at NEXT-minute quote."""
import sys, numpy as np, pandas as pd
from backtest import load, add_model, trades, summarize
for series in sys.argv[1:]:
    p = load(series); p = p.dropna(subset=["S","var_hl10","var_hl1440"])
    p = add_model(p,"hl10",3.5,1.1,col="q")
    cut = p.close_ts.quantile(0.5); p["split"]=np.where(p.close_ts<=cut,"first_half","second_half")
    for lag in [10, 7, 5]:
        for ex in [True, False]:
            for sp in ["first_half","second_half","all"]:
                d = p[(p.lag==lag)&((p.split==sp)|(sp=="all"))]
                t = trades(d,0.01,col="q",min_price=0.85,max_price=0.97,exec_next=ex)
                s = summarize(t)
                print(series, "lag",lag,"exec_next",ex, sp, {k:(round(float(v),3) if not isinstance(v,(int,np.integer)) else int(v)) for k,v in s.items()})
