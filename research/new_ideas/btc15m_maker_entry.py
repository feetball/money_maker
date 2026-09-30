import sys; sys.path.insert(0,'.')
import numpy as np, pandas as pd
from backtest import load, add_model, trades, summarize, cluster_ci
from fv_model import fee_per_contract
p = load("KXBTC15M"); p = p.dropna(subset=["S","var_hl10","var_hl1440"])
p = add_model(p,"hl10",3.5,1.1,col="q")
cut = p.close_ts.quantile(0.5); p["split"]=np.where(p.close_ts<=cut,"train","test")
c = pd.read_csv("data/candles_KXBTC15M.csv.gz", usecols=["ticker","ts","yes_bid","yes_ask"]).sort_values(["ticker","ts"])
g = {t:(x.ts.values,x.yes_bid.values.astype(float),x.yes_ask.values.astype(float)) for t,x in c.groupby("ticker")}
for lag in [10]:
  d = p[p.lag==lag]
  tk = trades(d,0.01,col="q",min_price=0.85,max_price=0.97,exec_next=True)   # research taker rule
  tk["pnl"]=tk["pnl"]
  print("taker(next-min)", {k:round(v,3) if isinstance(v,float) else v for k,v in summarize(tk).items()})
  # same signals, decided on quote at t (exec_next False for signal selection)
  sig = trades(d,0.01,col="q",min_price=0.85,max_price=0.97,exec_next=False)
  rows=[]
  for r in sig.itertuples():
    ts,yb,ya = g[r.ticker]
    if r.side=="yes": b=r.yes_bid; win=r.y
    else: b=1-r.yes_ask; win=1-r.y
    if not (0.03<b<0.98): continue
    for H in (3,5,9):
      end=min(r.t+H*60, r.close_ts-60); m=(ts>r.t)&(ts<=end)
      if r.side=="yes": fill=bool((ya[m]<=b+1e-9).any())          # market traded through our YES bid
      else: fill=bool(((1-yb[m])<=b+1e-9).any())
      rows.append((r.event_ticker,H,fill,win-b, win-r.px, r.px, b))
  R=pd.DataFrame(rows,columns=["event_ticker","H","fill","pnl_m","pnl_t","px","b"])
  sp = sig.merge(p[["ticker","t","split"]],on=["ticker","t"]).split.values
  R["split"]=np.tile(sp,3)[:0] if False else None
  print(R.groupby("H").agg(n=("fill","size"),fill=("fill","mean")).round(3))
  for H,x in R.groupby("H"):
    f=x[x.fill]
    for mf in (0,0.0175):
      pm=f.pnl_m-(fee_per_contract(f.b.values,coef=mf) if mf else 0)
      # per-signal expectation including unfilled=0 and taker comparison on same signals
      print(f"H={H} maker_fee_coef={mf}: filled n={len(f)} mean={100*pm.mean():.2f}c  per-signal(all)={100*pm.sum()/len(x):.2f}c  | taker same signals(mean per signal, decision-quote px)= {100*(x.pnl_t-fee_per_contract(x.px.values)).mean():.2f}c  | bid px {x.b.mean():.3f} vs ask px {x.px.mean():.3f}")
