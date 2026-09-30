import json, time, numpy as np, pandas as pd, httpx
from fv_model import prob_above, fee_per_contract
COEF = {"KXSOL15M": (-0.013,0.642,0.412,"hl30",5.0,1.0), "KXXRP15M": (0.074,0.8,0.273,"hl10",3.5,1.1), "KXDOGE15M": (-0.005,0.619,0.458,"hl10",3.5,1.1)}
PROD = {"KXSOL15M":"SOL-USD","KXXRP15M":"XRP-USD","KXDOGE15M":"DOGE-USD"}
d = pd.DataFrame([json.loads(l) for l in open("data/live_latency.jsonl")])
t0, t1 = d.t_spot.min()-4*3600, d.t_spot.max()
out=[]
for s, g in d.groupby("series"):
    a,b,c,vol,nu,vm = COEF[s]; hl = int(vol[2:])
    r = httpx.get(f"https://api.exchange.coinbase.com/products/{PROD[s]}/candles", params={"granularity":60,"start":pd.to_datetime(t1-300*60,unit="s").isoformat(),"end":pd.to_datetime(t1,unit="s").isoformat()}).json()
    cd = pd.DataFrame(r, columns=["ts","low","high","open","close","vol"]).sort_values("ts"); cd["t"]=(cd.ts+60).astype(float)
    lr = np.log(cd.close).diff(); cd["var"]=(lr**2).ewm(halflife=hl, min_periods=hl).mean()
    vv = pd.merge_asof(g.sort_values("t_spot"), cd[["t","var"]].dropna(), left_on="t_spot", right_on="t", direction="backward")
    tau = np.maximum((vv.close_ts - vv.t_spot)/60 - 2/3, 1/3)
    pm = prob_above(vv.spot, vv.strike, np.sqrt(vv["var"]*tau)*vm, nu=nu)
    ok = (vv.yes_bid>0)&(vv.yes_ask<1)
    mid = (vv.yes_bid+vv.yes_ask)/2
    lg = lambda x: np.log(np.clip(x,1e-3,1-1e-3)/(1-np.clip(x,1e-3,1-1e-3)))
    q = 1/(1+np.exp(-(a+b*lg(mid)+c*lg(pm))))
    vv["pm"]=pm; vv["q"]=q; vv["mid"]=mid
    vv["e_yes"]=q-vv.yes_ask-fee_per_contract(vv.yes_ask); vv["e_no"]=(1-q)-(1-vv.yes_bid)-fee_per_contract(1-vv.yes_bid)
    vv.loc[~ok,["e_yes","e_no"]]=np.nan
    vv["mins_left"]=(vv.close_ts-vv.t_spot)/60
    out.append(vv)
v = pd.concat(out)
v = v[(v.mins_left>2)&(v.mins_left<14)]
print("samples", len(v), "per series", v.series.value_counts().to_dict())
print("median sampling interval s:", v.groupby("series").t_spot.diff().median())
for s, g in v.groupby("series"):
    g = g.sort_values("t_spot").reset_index(drop=True)
    for side in ["e_yes","e_no"]:
        sig = (g[side]>0.02).values
        # episodes
        eps=[]; i=0
        while i < len(sig):
            if sig[i]:
                j=i
                while j+1<len(sig) and sig[j+1] and g.ticker[j+1]==g.ticker[i]: j+=1
                sz = g.yes_ask_sz[i] if side=="e_yes" else g.yes_bid_sz[i]
                eps.append((g.t_spot[j]-g.t_spot[i], j-i+1, g[side][i], sz, g.ticker[i]))
                i=j+1
            else: i+=1
        if eps:
            e=pd.DataFrame(eps,columns=["dur_s","samples","edge","size","ticker"])
            print(s, side, "episodes", len(e), "frac samples w/ signal %.3f"%sig.mean(), "median dur_s", e.dur_s.median(), "p75", e.dur_s.quantile(.75), "single-sample eps", (e.samples==1).mean().round(2), "median edge", e.edge.median().round(3), "median size", e["size"].median())
        else:
            print(s, side, "no episodes; frac", sig.mean())
v.to_csv("results/live_latency_scored.csv", index=False)

# ---- persistence: for each signal sample, is the same-side edge still > 0 at <= the same ask after k seconds?
print("\n== persistence of signals (edge>0.02 at t) ==")
for s, g in v.groupby("series"):
    g = g.sort_values("t_spot").reset_index(drop=True)
    for side, askcol in [("e_yes", "yes_ask"), ("e_no", None)]:
        idx = np.where(g[side].values > 0.02)[0]
        if len(idx) == 0:
            continue
        res = {}
        for k in [2, 4, 6, 10, 20]:
            still = []
            for i in idx:
                j = np.searchsorted(g.t_spot.values, g.t_spot[i] + k)
                if j >= len(g) or g.ticker[j] != g.ticker[i]:
                    continue
                if side == "e_yes":
                    still.append(g.yes_ask[j] <= g.yes_ask[i] + 1e-9)
                else:
                    still.append(g.yes_bid[j] >= g.yes_bid[i] - 1e-9)
            res[k] = (round(float(np.mean(still)), 2), len(still)) if still else None
        print(s, side, "signals", len(idx), "P(price still available after k s):", res)
