import numpy as np, pandas as pd
from backtest import trades, summarize
for s in ["KXSOL15M","KXXRP15M","KXDOGE15M"]:
    o = pd.read_parquet(f"data/oos_stacked_{s}.parquet"); o["K"]=np.nan; o["S"]=np.nan; o["week"]=pd.to_datetime(o.close_ts,unit="s").dt.to_period("W").astype(str)
    for ex in [False, True]:
        t = trades(o[o.lag.isin([5,7,10,12])], 0.02, col="q_st", exec_next=ex, min_price=0.10, max_price=0.90)
        t = t.merge(o[["ticker","t","week"]], on=["ticker","t"])
        w = t.groupby("week").agg(n=("pnl","size"), pnl_c=("pnl", lambda x: 100*x.mean()))
        sm = summarize(t)
        print(s, "lags5-12 px[.1,.9] thr.02", "market_next" if ex else "same_min", "ALL n=%d pnl=%.2fc CI[%.2f,%.2f]"%(sm["n"],sm["mean_pnl_c"],sm["ci_lo"],sm["ci_hi"]), "| weeks positive: %d/%d"%((w.pnl_c>0).sum(), len(w)))
        print("   weekly pnl c:", w.pnl_c.round(1).tolist())
