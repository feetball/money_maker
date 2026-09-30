import sys, numpy as np, pandas as pd
from backtest import load, add_model
series = sys.argv[1]
p = load(series)
p = p.dropna(subset=["S","var_hl10","var_hl1440"])
p = add_model(p, "hl10", 3.5, 1.1, col="q")
two = p[(p.yes_bid>0)&(p.yes_ask<1)].copy()
two["dev"] = two.q - two.mid
two["res"] = two.y - two.mid
two["spr"] = two.yes_ask - two.yes_bid
b = pd.cut(two.dev, [-1,-.15,-.08,-.05,-.03,-.015,.015,.03,.05,.08,.15,1])
print("== realized (y - mid) vs model deviation (q - mid), all lags ==")
print(two.groupby(b, observed=True).agg(n=("res","size"), dev=("dev","mean"), realized=("res","mean"), spread=("spr","mean")).round(4))
two["hour_et"] = ((two.close_ts//3600) - 4) % 24
two["tod"] = pd.to_datetime(two.close_ts, unit="s").dt.tz_localize("UTC").dt.tz_convert("America/New_York").dt.hour
g = two.groupby("tod").apply(lambda d: pd.Series(dict(n=len(d), ll_model=-(d.y*np.log(d.q.clip(1e-4,1-1e-4))+(1-d.y)*np.log((1-d.q).clip(1e-4,1-1e-4))).mean(), ll_mid=-(d.y*np.log(d.mid.clip(1e-4,1-1e-4))+(1-d.y)*np.log((1-d.mid).clip(1e-4,1-1e-4))).mean(), spread=d.spr.mean())))
print("== by close hour ET ==")
print(g.round(4))
print("realized sqrt var_hl10 per-min median", np.sqrt(p.var_hl10).median(), "hl1440", np.sqrt(p.var_hl1440).median())
