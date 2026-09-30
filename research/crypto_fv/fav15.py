import numpy as np, pandas as pd
from backtest import load, add_model, trades, summarize
p = load("KXBTC15M"); p = p.dropna(subset=["S","var_hl10","var_hl1440"])
p = add_model(p,"hl10",3.5,1.1,col="q")
cut = p.close_ts.quantile(0.5); p["split"]=np.where(p.close_ts<=cut,"train","test")
p["month"] = pd.to_datetime(p.close_ts,unit="s").dt.to_period("M").astype(str)
rows=[]
for lag in [14,12,10,7,5]:
    for lo,hi in [(0.80,0.97),(0.85,0.97),(0.85,0.95),(0.9,0.98),(0.5,0.97)]:
        for thr in [0.0,0.01,0.02]:
            for sp in ["train","test"]:
                d=p[(p.lag==lag)&(p.split==sp)]
                t=trades(d,thr,col="q",min_price=lo,max_price=hi,exec_next=True)
                rows.append(dict(lag=lag,lo=lo,hi=hi,thr=thr,split=sp,**summarize(t)))
R=pd.DataFrame(rows)
print(R.pivot_table(index=["lag","lo","hi","thr"],columns="split",values=["mean_pnl_c","ci_lo","n"]).round(2).to_string())
t=trades(p[p.lag==10],0.01,col="q",min_price=0.85,max_price=0.97,exec_next=True).merge(p[["ticker","t","month"]],on=["ticker","t"])
print(t.groupby(["month","side"]).agg(n=("pnl","size"),pnl_c=("pnl",lambda s:100*s.mean())).round(2))
# blind favorites (no model) at lag 10 for comparison
for lo,hi in [(0.85,0.97)]:
    d=p[p.lag==10]
    q=d.copy(); q["q_hi"]=np.where(q.mid>=0.5,1.0,0.0)  # always choose favorite side
    tt=trades(q.assign(qq=np.where(q.mid>=0.5,0.999,0.001)),-1,col="qq",min_price=lo,max_price=hi,exec_next=True)
    print("blind favorite lag10:",{k:round(v,3) if isinstance(v,float) else v for k,v in summarize(tt).items()})
