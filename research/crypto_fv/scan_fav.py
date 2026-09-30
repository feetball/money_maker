import sys, numpy as np, pandas as pd
from backtest import load, add_model, trades, summarize
out=[]
for series in ["KXBTCD","KXETHD"]:
    p = load(series); p = p.dropna(subset=["S","var_hl10","var_hl1440"])
    cut = p.close_ts.quantile(0.5); p["split"]=np.where(p.close_ts<=cut,"train","test")
    p = add_model(p,"hl10",3.5,1.0,col="q")
    for lo,hi in [(0.03,0.97),(0.5,0.97),(0.8,0.97),(0.85,0.95),(0.9,0.98)]:
        for thr in [0.0,0.01,0.02]:
            for lags in [(5,10,15,20,30),(30,45,55),(1,2,3)]:
                for sp in ["train","test"]:
                    d = p[(p.split==sp)&p.lag.isin(lags)]
                    t = trades(d,thr,col="q",min_price=lo,max_price=hi)
                    out.append(dict(series=series,lo=lo,hi=hi,thr=thr,lags=str(lags),split=sp,**summarize(t)))
R=pd.DataFrame(out)
R.to_csv("results/scan_price_ranges.csv",index=False)
w = R.pivot_table(index=["series","lo","hi","thr","lags"],columns="split",values=["mean_pnl_c","n","ci_lo"]).round(2)
print(w.to_string())
