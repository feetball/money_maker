import os; os.environ["POINT_SETTLE"]="1"
import numpy as np, pandas as pd
import backtest as bt
from backtest import load, add_model, trades, summarize
for series, vm, nu in [("KXNASDAQ100U",0.9,5.0),("KXINXU",0.9,5.0)]:
    p = load(series); p = p.dropna(subset=["S","var_hl10","var_hl1440"])
    et = pd.to_datetime(p["t"], unit="s", utc=True).dt.tz_convert("America/New_York"); mins = et.dt.hour*60+et.dt.minute
    p = p[(mins>=9*60+45)&(mins<=16*60)]
    ev = p.groupby("event_ticker").agg(xv=("expiration_value","first"), c=("S_T_cb_close","first")).dropna()
    print(series, "settle - yahoo close at T: median", round((ev.xv-ev.c).median(),4), "p90 abs", round((ev.xv-ev.c).abs().quantile(.9),4))
    p = add_model(p,"hl10",nu,vm,col="q")
    cut = p.close_ts.quantile(0.5); p["split"]=np.where(p.close_ts<=cut,"train","test")
    for lags in [(1,),(2,),(3,),(1,2,3),(5,10)]:
        for thr in [0.0,0.02,0.05]:
            for ex in [False,True]:
                r = {}
                for sp in ["train","test"]:
                    t = trades(p[(p.lag.isin(lags))&(p.split==sp)],thr,col="q",exec_next=ex,min_price=0.02,max_price=0.98)
                    s = summarize(t); r[sp]=(s.get("n",0), round(float(s.get("mean_pnl_c",np.nan)),2), round(float(s.get("ci_lo",np.nan)),2), round(float(s.get("ci_hi",np.nan)),2), s.get("events",0))
                print(series, lags, thr, "exec_next" if ex else "same_min", "train", r["train"], "test", r["test"])
