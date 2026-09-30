"""Blind (no model) early-window favorite rule on 15-min up/down series.
Buy the favorite side (mid>=0.5 -> YES else NO) at lags in LAGS if its ask in [LO,HI]."""
import sys, numpy as np, pandas as pd
from backtest import load, trades, summarize
LO, HI = 0.70, 0.95
for s in sys.argv[1:]:
    p = load(s); p = p[(p.yes_bid>0)&(p.yes_ask<1)].copy()
    p["qq"] = np.where(p.mid>=0.5, 0.9999, 0.0001)   # pseudo-prob that selects the favorite side
    cut = p.close_ts.quantile(0.5); p["split"]=np.where(p.close_ts<=cut,"H1","H2")
    for lags in [(12,),(10,),(7,),(7,10,12),(5,),(3,),(1,2)]:
        for ex in [False, True]:
            out=[]
            for sp in ["H1","H2","all"]:
                d = p[p.lag.isin(lags)] if sp=="all" else p[p.lag.isin(lags)&(p.split==sp)]
                t = trades(d, -1, col="qq", min_price=LO, max_price=HI, exec_next=ex)
                sm = summarize(t); out.append(f"{sp}: n={sm.get('n',0)} pnl={sm.get('mean_pnl_c',np.nan):.2f}c CI[{sm.get('ci_lo',np.nan):.2f},{sm.get('ci_hi',np.nan):.2f}]")
            print(s, lags, "market_next" if ex else "same_min", " | ".join(out))
