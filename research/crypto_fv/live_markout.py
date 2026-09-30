"""Markout / quote-response analysis of the live recording (data/live_latency2.jsonl).

For each sample: model prob pm (Student-t nu, EWMA hl vol from Coinbase 1-min candles known at that time,
horizon = mins_left - 2/3), stacked q = sigmoid(a + b logit(mid) + c logit(pm)) with coefficients fitted
on the historical walk-forward (no fitting on live data). Signals: q - ask - fee > thr (YES) or
(1-q) - (1-bid) - fee > thr (NO). Markout at +k s: mid(t+k) - entry - fee (same ticker), and settlement P&L.
Also: how fast Kalshi mid responds to model changes (regress dmid on dpm at sample lags 0..4).
Usage: live_markout.py [jsonl]
"""
import json, sys, time
import numpy as np, pandas as pd, httpx
from fv_model import prob_above, fee_per_contract
import kclient as k

F = sys.argv[1] if len(sys.argv) > 1 else "data/live_latency2.jsonl"
COEF = {"KXBTC15M": (-0.010, 0.930, 0.094, "hl10", 3.5, 1.1), "KXSOL15M": (-0.013, 0.642, 0.412, "hl30", 5.0, 1.0),
        "KXXRP15M": (0.074, 0.8, 0.273, "hl10", 3.5, 1.1), "KXDOGE15M": (-0.005, 0.619, 0.458, "hl10", 3.5, 1.1)}
PROD = {"KXBTC15M": "BTC-USD", "KXSOL15M": "SOL-USD", "KXXRP15M": "XRP-USD", "KXDOGE15M": "DOGE-USD"}
lg = lambda x: np.log(np.clip(x, 1e-3, 1 - 1e-3) / (1 - np.clip(x, 1e-3, 1 - 1e-3)))
d = pd.DataFrame([json.loads(l) for l in open(F)])
t1 = d.t_spot.max()
out = []
for s, g in d.groupby("series"):
    a, b, c, vol, nu, vm = COEF[s]; hl = int(vol[2:])
    rows = []
    for end in [t1, t1 - 300 * 60]:  # 600 minutes of candles
        r = httpx.get(f"https://api.exchange.coinbase.com/products/{PROD[s]}/candles", params={"granularity": 60,
            "start": pd.to_datetime(end - 300 * 60, unit="s").isoformat(), "end": pd.to_datetime(end, unit="s").isoformat()}).json()
        rows += r
    cd = pd.DataFrame(rows, columns=["ts", "low", "high", "open", "close", "vol"]).drop_duplicates("ts").sort_values("ts")
    cd["t"] = (cd.ts + 60).astype(float)
    cd["var"] = (np.log(cd.close).diff() ** 2).ewm(halflife=hl, min_periods=hl).mean()
    vv = pd.merge_asof(g.sort_values("t_spot"), cd[["t", "var"]].dropna(), left_on="t_spot", right_on="t", direction="backward")
    # basis: median(settlement index value - Coinbase final-minute bar mid) over events that closed BEFORE the recording
    t0 = g.t_spot.min()
    st = k.get("/markets", {"series_ticker": s, "status": "settled", "limit": 60}, cache=False).get("markets") or []
    cdi = cd.set_index("t")
    bs = []
    for m in st:
        ct = pd.Timestamp(m["close_time"]).timestamp()
        xv = pd.to_numeric(m.get("expiration_value"), errors="coerce")
        if ct < t0 and ct in cdi.index and not np.isnan(xv):
            bs.append(xv - (cdi.at[ct, "open"] + cdi.at[ct, "close"]) / 2)
    basis = float(np.median(bs)) if len(bs) >= 5 else 0.0
    print(s, "basis from", len(bs), "prior events:", basis, "(%.2f bps)" % (1e4 * basis / g.spot.mean()))
    vv["basis"] = basis
    tau = np.maximum((vv.close_ts - vv.t_spot) / 60 - 2 / 3, 1 / 3)
    vv["spot_mid"] = np.where((vv.spot_bid > 0) & (vv.spot_ask > 0), (vv.spot_bid + vv.spot_ask) / 2, vv.spot)
    vv["pm"] = prob_above(vv.spot_mid, vv.strike, np.sqrt(vv["var"] * tau) * vm, nu=nu, basis=basis)
    vv["mid"] = (vv.yes_bid + vv.yes_ask) / 2
    vv["q"] = 1 / (1 + np.exp(-(a + b * lg(vv["mid"]) + c * lg(vv.pm))))
    ok = (vv.yes_bid > 0) & (vv.yes_ask < 1)
    vv["e_yes"] = np.where(ok, vv.q - vv.yes_ask - fee_per_contract(vv.yes_ask), np.nan)
    vv["e_no"] = np.where(ok, (1 - vv.q) - (1 - vv.yes_bid) - fee_per_contract(1 - vv.yes_bid), np.nan)
    vv["e_yes_raw"] = np.where(ok, vv.pm - vv.yes_ask - fee_per_contract(vv.yes_ask), np.nan)
    vv["e_no_raw"] = np.where(ok, (1 - vv.pm) - (1 - vv.yes_bid) - fee_per_contract(1 - vv.yes_bid), np.nan)
    vv["mins_left"] = (vv.close_ts - vv.t_spot) / 60
    out.append(vv)
v = pd.concat(out).sort_values(["series", "t_spot"]).reset_index(drop=True)
# settlement results
res = {}
for tk in v.ticker.unique():
    m = k.get(f"/markets/{tk}", cache=False).get("market", {})
    res[tk] = m.get("result")
v["result"] = v.ticker.map(res)
v["y"] = v.result.map({"yes": 1, "no": 0})
v = v[(v.mins_left > 1) & (v.mins_left < 14)]
print("samples", len(v), v.series.value_counts().to_dict(), "tickers", v.ticker.nunique(), "settled", v.drop_duplicates("ticker").y.notna().sum())
print("median sample interval s", v.groupby("series").t_spot.diff().median().round(2))
print("median spread c", (100 * (v.yes_ask - v.yes_bid)).groupby(v.series).median().to_dict(),
      "median top size yes_ask", v.groupby("series").yes_ask_sz.median().to_dict())

# markouts
K = [2, 6, 15, 30, 60]
rows = []
for s, g in v.groupby("series"):
    g = g.reset_index(drop=True)
    tt = g.t_spot.values
    for kind, ey, en in [("stacked", "e_yes", "e_no"), ("raw", "e_yes_raw", "e_no_raw")]:
        for thr in [0.02, 0.05]:
            for side, ecol in [("yes", ey), ("no", en)]:
                idx = np.where(g[ecol].values > thr)[0]
                for i in idx:
                    entry = g.yes_ask[i] if side == "yes" else 1 - g.yes_bid[i]
                    fee = fee_per_contract(entry)
                    rec = dict(series=s, model=kind, thr=thr, side=side, t=tt[i], ticker=g.ticker[i], edge=g[ecol][i], entry=entry,
                               size=g.yes_ask_sz[i] if side == "yes" else g.yes_bid_sz[i])
                    for kk in K:
                        j = np.searchsorted(tt, tt[i] + kk)
                        if j < len(g) and g.ticker[j] == g.ticker[i]:
                            m2 = g["mid"][j]
                            rec[f"mo{kk}"] = (m2 - entry if side == "yes" else (1 - m2) - entry) - fee
                            px2 = g.yes_ask[j] if side == "yes" else 1 - g.yes_bid[j]
                            rec[f"avail{kk}"] = float(px2 <= entry + 1e-9)
                    yv = g.y[i]
                    rec["settle_pnl"] = (yv - entry - fee if side == "yes" else (1 - yv) - entry - fee) if not np.isnan(yv) else np.nan
                    rows.append(rec)
M = pd.DataFrame(rows)
M.to_csv("results/live_markout_signals.csv", index=False)
agg = M.groupby(["series", "model", "thr", "side"]).agg(
    n=("edge", "size"), tickers=("ticker", "nunique"), edge_c=("edge", lambda x: 100 * x.mean()),
    size_med=("size", "median"),
    **{f"mo{kk}_c": (f"mo{kk}", lambda x: 100 * x.mean()) for kk in K},
    **{f"avail{kk}": (f"avail{kk}", "mean") for kk in [2, 6, 15]},
    settle_c=("settle_pnl", lambda x: 100 * x.mean()))
pd.set_option("display.width", 250)
print("\n== live signal markouts (c/contract, net of fee; mo_k = mid(t+k s) - entry - fee) ==")
print(agg.round(2).to_string())

# response speed: dmid_t on dpm_{t-j}
print("\n== Kalshi mid response to model-prob changes (per-sample regression, same ticker) ==")
for s, g in v.groupby("series"):
    g = g.copy()
    g["dmid"] = g.groupby("ticker")["mid"].diff(); g["dpm"] = g.groupby("ticker").pm.diff()
    for j in range(1, 5):
        g[f"dpm_l{j}"] = g.groupby("ticker").dpm.shift(j)
    x = g.dropna(subset=["dmid", "dpm"] + [f"dpm_l{j}" for j in range(1, 5)])
    x = x[(x.yes_bid > 0) & (x.yes_ask < 1)]
    X = np.c_[np.ones(len(x)), x.dpm] if False else np.c_[np.ones(len(x)), x[["dpm"] + [f"dpm_l{j}" for j in range(1, 5)]].values]
    bb, *_ = np.linalg.lstsq(X, x.dmid.values, rcond=None)
    dt = g.t_spot.diff().median()
    print(s, "n", len(x), "sample dt %.1fs" % dt, "coef on dpm lags 0..4:", np.round(bb[1:], 3), "cum", np.round(np.cumsum(bb[1:]), 3))
v.to_csv("results/live_markout_scored.csv", index=False)
