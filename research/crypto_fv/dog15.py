"""Late-window underdog rule on 15-min series: at lag L, buy the UNDERDOG side (mid<0.5 side) if its ask in [LO,HI]."""
import sys, numpy as np, pandas as pd
from backtest import load, trades, summarize
for s in sys.argv[1:]:
    p = load(s); p = p[(p.yes_bid>0)&(p.yes_ask<1)].copy()
    p["qq"] = np.where(p.mid<0.5, 0.9999, 0.0001)
    cut = p.close_ts.quantile(0.5); p["split"]=np.where(p.close_ts<=cut,"H1","H2")
    for LO,HI in [(0.05,0.30),(0.10,0.30),(0.02,0.15)]:
        for lags in [(3,),(2,),(3,5),(5,)]:
            for ex in [False, True]:
                out=[]
                for sp in ["H1","H2","all"]:
                    d = p[p.lag.isin(lags)] if sp=="all" else p[p.lag.isin(lags)&(p.split==sp)]
                    t = trades(d, -1, col="qq", min_price=LO, max_price=HI, exec_next=ex)
                    sm = summarize(t); out.append(f"{sp}: n={sm.get('n',0)} pnl={sm.get('mean_pnl_c',np.nan):.2f}c CI[{sm.get('ci_lo',np.nan):.2f},{sm.get('ci_hi',np.nan):.2f}]")
                print(s, (LO,HI), lags, "mkt_next" if ex else "same_min", " | ".join(out))
