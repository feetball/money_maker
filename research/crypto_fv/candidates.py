"""Evaluate frozen candidate rules under 3 execution assumptions and time splits."""
import os, sys, numpy as np, pandas as pd
import backtest as bt
from backtest import load, add_model, trades, summarize
C = [
  # name, series, point_settle, vol, nu, vm, lags, lo, hi, thr
  ("C1_fv_taker_btc_hourly", "KXBTCD", False, "hl10", 3.5, 1.1, (5,10,15,20,30), 0.03, 0.97, 0.02),
  ("C1b_fv_taker_eth_hourly", "KXETHD", False, "hl10", 3.5, 1.0, (5,10,15,20,30), 0.03, 0.97, 0.02),
  ("C2_eth_hourly_late_fav", "KXETHD", False, "hl10", 3.5, 1.0, (1,2,3), 0.85, 0.97, 0.01),
  ("C2b_btc_hourly_late_fav", "KXBTCD", False, "hl10", 3.5, 1.1, (1,2,3), 0.85, 0.97, 0.01),
  ("C3_btc15m_lag10_fav", "KXBTC15M", False, "hl10", 3.5, 1.1, (10,), 0.85, 0.97, 0.01),
  ("C3r_eth15m_lag10_fav", "KXETH15M", False, "hl10", 3.5, 1.1, (10,), 0.85, 0.97, 0.01),
  ("C3r_sol15m_lag10_fav", "KXSOL15M", False, "hl10", 3.5, 1.1, (10,), 0.85, 0.97, 0.01),
  ("C4_ndx_hourly_late_fv", "KXNASDAQ100U", True, "hl10", 5.0, 0.9, (1,2,3), 0.02, 0.98, 0.02),
  ("C5_ndx_hourly_mid_fv", "KXNASDAQ100U", True, "hl10", 5.0, 0.9, (5,10), 0.02, 0.98, 0.05),
]
only = sys.argv[1:]
rows = []
cache = {}
for name, series, point, vol, nu, vm, lags, lo, hi, thr in C:
    if only and not any(o in name for o in only): continue
    if not os.path.exists(f"data/panel_{series}.parquet"): print("skip", name); continue
    bt.POINT = point
    p = load(series); p = p.dropna(subset=["S","var_hl10","var_hl1440"])
    if point:
        et = pd.to_datetime(p["t"], unit="s", utc=True).dt.tz_convert("America/New_York"); m = et.dt.hour*60+et.dt.minute
        p = p[(m>=9*60+45)&(m<=16*60)]
    p = add_model(p, vol, nu, vm, col="q")
    cut = p.close_ts.quantile(0.5); p["split"] = np.where(p.close_ts<=cut,"H1","H2")
    d = p[p.lag.isin(lags)]
    for ex in [False, "limit", True]:
        for sp in ["H1","H2","all"]:
            x = d if sp=="all" else d[d.split==sp]
            t = trades(x, thr, col="q", min_price=lo, max_price=hi, exec_next=ex)
            s = summarize(t)
            rows.append(dict(name=name, exec={False:"same_min",True:"market_next","limit":"limit_next"}[ex], split=sp,
                             **{k:(float(v) if isinstance(v,(float,np.floating)) else v) for k,v in s.items()},
                             ev_first=pd.to_datetime(x.close_ts.min(),unit="s").date(), ev_last=pd.to_datetime(x.close_ts.max(),unit="s").date()))
R = pd.DataFrame(rows)
cols=["name","exec","split","n","events","mean_pnl_c","ci_lo","ci_hi","hit","avg_px","avg_fee_c","ev_first","ev_last"]
print(R[cols].round(3).to_string(index=False))
R[cols].to_csv("results/candidates_summary.csv" if not only else f"results/candidates_{'_'.join(only)}.csv", index=False)
