"""Robustness checks for the only rule family that is positive in both TRAIN and TEST:
B4 = buy YES at the YES ask when YES bid >= 0.97 (first trigger per market, hold to settlement), and its
non-Sports variant B4ns. Reads trades_oos_B4*.csv.gz written by oos_eval.py.
    uv run --with pandas --with pyarrow --with numpy python survivor_checks.py  -> survivor_checks.csv, survivor_losses.csv"""
import numpy as np
import pandas as pd

from calib_lib import HERE, cluster_boot, cluster_se

pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30); pd.set_option("display.max_rows", 300)
rows = []


def add(rule, period, by, key, d):
    if len(d) == 0:
        return
    mu, se, G = cluster_se(d["pnl"].to_numpy(), d["event_ticker"].astype(str).to_numpy())
    rows.append(dict(rule=rule, period=period, by=by, key=str(key), n=len(d), n_events=G, losses=int((d.win == 0).sum()),
                     loss_events=d.loc[d.win == 0, "event_ticker"].nunique(), avg_entry=d.entry.mean(),
                     breakeven_loss_rate=(1 - d.entry - (d.pnl_prefee - d.pnl)).mean(), loss_rate=1 - d.win.mean(),
                     pnl=mu, se=se, t=mu / se if se > 0 else np.nan, pnl_c1=d.pnl_c1.mean(), pnl_c10=d.pnl_c10.mean()))


for rid in ("B4", "B4ns"):
    t = pd.read_csv(HERE / f"trades_oos_{rid}.csv.gz")
    t["hz"] = pd.cut(t["h_to_eet"], [-1e9, 0, 1, 6, 24, 72, 1e9], labels=["<0", "0-1h", "1-6h", "6-24h", "1-3d", ">3d"])
    t["entry_px"] = pd.cut(t["entry"], [0.97, 0.9799, 0.9849, 0.9899, 0.9949, 1.0],
                           labels=["0.97-0.98", "0.98-0.985", "0.985-0.99", "0.99-0.995", ">0.995"])
    t["ladder"] = np.where(t["category"].isin(["Commodities", "Financials", "Crypto", "Economics"]), "price-ladder cats",
                           "other")
    for per in ("train", "test"):
        d = t[t.period == per]
        add(rid, per, "ALL", "ALL", d)
        for by in ("category", "hz", "entry_px", "fee_multiplier", "ladder"):
            for k, g in d.groupby(by, observed=True):
                add(rid, per, by, k, g)
        # leave-one-series-out and leave-one-category-out
        top = d["series_ticker"].value_counts().index[:15]
        vals = [(s, d[d.series_ticker != s].pnl.mean()) for s in top]
        mn = min(vals, key=lambda x: x[1]); mx = max(vals, key=lambda x: x[1])
        rows.append(dict(rule=rid, period=per, by="leave-one-series-out (top15)", key=f"min drop {mn[0]} / max drop {mx[0]}",
                         pnl=mn[1], se=mx[1]))
        cats = d["category"].unique()
        vals = [(c, d[d.category != c].pnl.mean()) for c in cats]
        mn = min(vals, key=lambda x: x[1]); mx = max(vals, key=lambda x: x[1])
        rows.append(dict(rule=rid, period=per, by="leave-one-category-out", key=f"min drop {mn[0]} / max drop {mx[0]}",
                         pnl=mn[1], se=mx[1]))
    # pooled train+test (NOT an out-of-sample number; for the size of the effect only)
    add(rid, "train+test", "ALL", "ALL", t)
    b = cluster_boot(t.assign(event_ticker=t.event_ticker.astype(str)), ["pnl"], B=2000, seed=3)["pnl"]
    rows.append(dict(rule=rid, period="train+test", by="bootstrap95", key="event-clustered", pnl=b[0], se=b[3],
                     n=len(t), n_events=t.event_ticker.nunique()))
    # how many extra losses would erase the TEST edge?
    d = t[t.period == "test"]
    extra = d.pnl.sum() / (d.entry.mean() + 0.001)
    rows.append(dict(rule=rid, period="test", by="stress", key="extra losing trades to erase test P&L",
                     pnl=extra, n=len(d)))
    if rid == "B4ns":
        L = t[t.win == 0][["period", "ticker", "series_ticker", "category", "date", "h_to_eet", "bid", "ask", "entry", "pnl"]]
        L.to_csv(HERE / "survivor_losses.csv", index=False)
        print("B4ns losing trades:"); print(L.to_string(index=False))

out = pd.DataFrame(rows)
out.to_csv(HERE / "survivor_checks.csv", index=False)
print(out.round(5).to_string(index=False))
