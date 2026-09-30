"""Consolidate headline numbers per series into results/summary_table.csv."""
import re, os, numpy as np, pandas as pd
ll = {}
for line in open("results/stacked_output.txt"):
    m = re.match(r"##### (\S+): walk-forward OOS rows (\d+) events (\d+)", line)
    if m: cur = m.group(1); ll[cur] = dict(oos_rows=int(m.group(2)), oos_events=int(m.group(3)))
    m = re.match(r"OOS logloss\s+mid ([\d.]+)\s+model ([\d.]+)\s+shrink-only ([\d.]+)\s+stacked ([\d.]+)", line)
    if m: ll[cur].update(ll_mid=float(m.group(1)), ll_model=float(m.group(2)), ll_shrink=float(m.group(3)), ll_stacked=float(m.group(4)))
rows = []
for s in ["KXBTCD", "KXETHD", "KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXNASDAQ100U", "KXINXU", "KXBTC"]:
    r = dict(series=s, **ll.get(s, {}))
    f = f"results/{s}_robustness.csv"
    if os.path.exists(f):
        R = pd.read_csv(f)
        x = R[(R.vol == "hl10") & (R.thr == 0.02) & (~R.exec_next) & (~R.rounded_fee)]
        for sp in ["train", "test"]:
            y = x[x.split == sp]
            if len(y): r.update({f"raw_taker_{sp}_n": int(y.n.iloc[0]), f"raw_taker_{sp}_c": round(y.mean_pnl_c.iloc[0], 2),
                                 f"raw_taker_{sp}_ci": f"[{y.ci_lo.iloc[0]:.2f},{y.ci_hi.iloc[0]:.2f}]"})
    f = f"results/{s}_range_trading.csv"
    if os.path.exists(f):
        R = pd.read_csv(f)
        x = R[(R.lags == "(5, 10, 15, 20, 30)") & (R.thr == 0.02) & (R["exec"] == "same_min")]
        for sp in ["train", "test"]:
            y = x[x.split == sp]
            r.update({f"raw_taker_{sp}_n": int(y.n.iloc[0]), f"raw_taker_{sp}_c": round(y.mean_pnl_c.iloc[0], 2),
                      f"raw_taker_{sp}_ci": f"[{y.ci_lo.iloc[0]:.2f},{y.ci_hi.iloc[0]:.2f}]"})
    f = f"results/{s}_stacked_walkforward.csv"
    if os.path.exists(f):
        R = pd.read_csv(f)
        for ex in ["same_min", "market_next"]:
            y = R[(R.model == "q_st") & (R.thr == 0.02) & (R["exec"] == ex) & (R.part == "all")]
            if len(y) and y.n.iloc[0] > 0:
                r.update({f"stacked_{ex}_n": int(y.n.iloc[0]), f"stacked_{ex}_c": round(y.mean_pnl_c.iloc[0], 2),
                          f"stacked_{ex}_ci": f"[{y.ci_lo.iloc[0]:.2f},{y.ci_hi.iloc[0]:.2f}]"})
    rows.append(r)
T = pd.DataFrame(rows)
# KXBTC range log-loss (train-chosen hl10 t3.5 x1.1, test half)
A = pd.read_csv("results/KXBTC_range_accuracy.csv")
a = A[(A.vol == "hl10") & (A.nu == 3.5) & (A.vm == 1.1) & (A.split == "test")].iloc[0]
T.loc[T.series == "KXBTC", ["ll_mid", "ll_model"]] = [a.ll_mid, a.ll_model]
T.to_csv("results/summary_table.csv", index=False)
pd.set_option("display.width", 300); pd.set_option("display.max_columns", 40)
print(T.to_string(index=False))
