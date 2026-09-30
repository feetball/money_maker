"""Minute-level lead-lag between Kalshi 15-min market mid and spot-implied model probability."""
import sys, numpy as np, pandas as pd
from fv_model import prob_above
for series, prod in [("KXBTC15M","BTC-USD"),("KXETH15M","ETH-USD"),("KXSOL15M","SOL-USD"),("KXXRP15M","XRP-USD"),("KXDOGE15M","DOGE-USD")]:
    m = pd.read_csv(f"data/markets_{series}.csv.gz").drop_duplicates("ticker").set_index("ticker")
    c = pd.read_csv(f"data/candles_{series}.csv.gz").drop_duplicates(["ticker","ts"])
    s = pd.read_csv(f"data/spot_{prod}.csv"); s["t"]=s.ts+60; s=s.set_index("t").sort_index()
    s = s.reindex(np.arange(s.index.min(), s.index.max()+60, 60)); s["close"]=s.close.ffill()
    r = np.log(s.close).diff(); var = (r**2).ewm(halflife=10, min_periods=10).mean()
    rows=[]
    for tk, g in c.groupby("ticker"):
        if tk not in m.index: continue
        close = int(m.at[tk,"close_ts"]); K = float(m.at[tk,"floor_strike"])
        grid = np.arange(close-14*60, close-60+1, 60)
        g = g.set_index("ts")[["yes_bid","yes_ask"]]
        g = g.reindex(np.union1d(g.index.values, grid)).ffill().reindex(grid)
        ok = (g.yes_bid>0)&(g.yes_ask<1)
        mid = ((g.yes_bid+g.yes_ask)/2).where(ok)
        S = s.close.reindex(grid).values; v = var.reindex(grid).values
        tau = (close-grid)/60 - 2/3
        pm = prob_above(S, K, np.sqrt(v*tau), nu=3.5)
        rows.append(pd.DataFrame({"tk":tk,"t":grid,"mid":mid.values,"pm":pm}))
    d = pd.concat(rows)
    d = d.sort_values(["tk","t"])
    d["dmid"]=d.groupby("tk").mid.diff(); d["dpm"]=d.groupby("tk").pm.diff()
    d["dpm_l1"]=d.groupby("tk").dpm.shift(1); d["dpm_f1"]=d.groupby("tk").dpm.shift(-1)
    d["gap"]=d.pm-d.mid; d["gap_l1"]=d.groupby("tk").gap.shift(1)
    x=d.dropna(subset=["dmid","dpm","dpm_l1","gap_l1"])
    X=np.c_[np.ones(len(x)), x.dpm, x.dpm_l1]
    b,*_=np.linalg.lstsq(X, x.dmid.values, rcond=None)
    # does last minute's (model - mid) gap predict this minute's mid change?
    X2=np.c_[np.ones(len(x)), x.dpm, x.gap_l1]
    b2,*_=np.linalg.lstsq(X2, x.dmid.values, rcond=None)
    print(series, "n",len(x), "dmid ~ dpm_t, dpm_t-1:", np.round(b[1:],3), "| dmid ~ dpm_t + gap_t-1:", np.round(b2[1:],3),
          "| corr(dmid,dpm)=%.3f corr(dmid,dpm_l1)=%.3f"%(np.corrcoef(x.dmid,x.dpm)[0,1], np.corrcoef(x.dmid,x.dpm_l1)[0,1]))
