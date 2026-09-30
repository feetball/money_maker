"""Deep-dive: late-expiry favorite rule. Buy side with ask in [LO,HI] when model p_side - ask - fee > THR
at lags 1-3 min before close."""
import sys, numpy as np, pandas as pd
from backtest import load, add_model, trades, summarize, cluster_ci
LO, HI, THR = 0.85, 0.97, 0.01
res = []
for series, nu, vm in [("KXETHD",3.5,1.0),("KXBTCD",3.5,1.1),("KXBTC15M",3.5,1.1)]:
    p = load(series); p = p.dropna(subset=["S","var_hl10","var_hl1440"])
    p = add_model(p,"hl10",nu,vm,col="q")
    p["week"] = pd.to_datetime(p.close_ts,unit="s").dt.to_period("W").astype(str)
    for lags in [(1,),(2,),(3,),(1,2,3),(5,),(10,)]:
        d = p[p.lag.isin(lags)]
        for ex in [False, True]:
            for rf in [False, True]:
                t = trades(d, THR, col="q", min_price=LO, max_price=HI, exec_next=ex, rounded_fee=rf)
                res.append(dict(series=series, lags=str(lags), exec_next=ex, rounded_fee=rf, **summarize(t)))
    t = trades(p[p.lag.isin((1,2,3))], THR, col="q", min_price=LO, max_price=HI)
    t = t.merge(p[["ticker","t","week","quote_age"]], on=["ticker","t"], how="left")
    print(f"\n==== {series} lags 1-3 ask in [{LO},{HI}] thr {THR}: by side")
    print(t.groupby("side").apply(lambda x: pd.Series(summarize(x))).round(3))
    print("by week:")
    print(t.groupby("week").agg(n=("pnl","size"), pnl_c=("pnl", lambda s: 100*s.mean()), hit=("pnl", lambda s: (s>0).mean())).round(3).to_string())
    print("quote_age (s) quantiles:", t.quote_age.quantile([.1,.5,.9]).to_dict())
    print("losses:", (t.pnl<0).sum(), "of", len(t), " mean px", round(t.px.mean(),4), "realized win", round((t.pnl>0).mean(),4))
R = pd.DataFrame(res)
print(R.round(3).to_string(index=False))
R.to_csv("results/late_favorite.csv", index=False)
