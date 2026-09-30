import sys, os, numpy as np, pandas as pd
from fv_model import fee_per_contract
from backtest import cluster_ci
series = sys.argv[1]
t = pd.read_csv(f"data/trades_{series}.csv.gz")
m = pd.read_csv(f"data/markets_{series}.csv.gz", usecols=["ticker","result","close_ts"])
t = t.merge(m, on="ticker")
t["y"] = (t.result=="yes").astype(int)
t["ts"] = (pd.to_datetime(t.created_time, format="ISO8601", utc=True) - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds()
t["mins_left"] = (t.close_ts - t.ts)/60
t["px"] = np.where(t.taker_side=="yes", t.yes_price, 1-t.yes_price)
t["win"] = np.where(t.taker_side=="yes", t.y, 1-t.y)
t["gross"] = t.win - t.px            # taker pnl before fee (= -maker pnl)
t["fee"] = fee_per_contract(t.px)
t["net"] = t.gross - t.fee
t["pnl"] = t.gross
print("trades", len(t), "contracts", round(t["count"].sum()), "events", t.event_ticker.nunique())
w = t["count"]
def agg(d):
    ww = d["count"]
    return pd.Series(dict(trades=len(d), contracts=ww.sum(), taker_gross_c=100*np.average(d.gross, weights=ww), taker_net_c=100*np.average(d.net, weights=ww), avg_px=np.average(d.px, weights=ww)))
print("== all (contract-weighted) =="); print(agg(t).round(3))
print("== by minutes to close =="); print(t.groupby(pd.cut(t.mins_left,[0,1,2,5,10,20,40,62]), observed=True).apply(agg).round(3))
print("== by taker price =="); print(t.groupby(pd.cut(t.px,[0,.05,.2,.4,.6,.8,.95,1]), observed=True).apply(agg).round(3))
print("== by trade size =="); print(t.groupby(pd.cut(t["count"],[0,10,100,1000,1e9]), observed=True).apply(agg).round(3))
# event-cluster CI of contract-weighted maker gross edge
g = t.assign(wg=t.gross*t["count"]).groupby("event_ticker").agg(s=("wg","sum"), c=("count","sum"))
rng = np.random.default_rng(0); idx = rng.integers(0,len(g),(1000,len(g)))
mm = g.s.values[idx].sum(1)/g.c.values[idx].sum(1)
print("taker gross edge c/contract 95% CI (event bootstrap):", np.round(100*np.percentile(mm,[2.5,50,97.5]),3))
