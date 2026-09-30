"""Family D: short-horizon mean reversion on hourly bars (CONTROL; expected to die from fees).

Rule: z = h-hour return / rolling 720h stdev of that return, at CLOSE of hour t.  If flat and
z <= z_entry: buy at OPEN of hour t+1, sell at OPEN of hour t+1+hold.  Equal capital sleeve per
product (the 21 hourly products; survivorship-selected -> biased in favour of the strategy).
Costs per side = taker + per-product slippage (same model as the daily study).
Writes results_hourly.csv
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))
from loader import load_book_snapshot, load_hourly  # noqa: E402

PRE = json.load(open("prereg.json"))["families"]["D_hourly_meanrev_control"]
H = load_hourly(fill_gaps=2)
H["product"] = H["product"].astype(str)
C = H.pivot(index="ts", columns="product", values="close").sort_index()
O = H.pivot(index="ts", columns="product", values="open").reindex(C.index)
idx = pd.date_range(C.index[0], C.index[-1], freq="h")
C, O = C.reindex(idx), O.reindex(idx)
R = (O.shift(-1) / O - 1).fillna(0.0).to_numpy()
b = load_book_snapshot()
slip = b.groupby("product")[["buy_cost_bps_10000", "sell_cost_bps_10000"]].median().mean(axis=1)
SLIP = np.array([max(2.0, float(slip.get(p, 20.0))) for p in C.columns]) / 1e4
AVAIL = C.notna().to_numpy() & O.notna().to_numpy()


def positions(h, z_entry, hold):
    r = C / C.shift(h) - 1
    z = (r / r.rolling(720, min_periods=500).std()).to_numpy()
    T, P = z.shape
    pos = np.zeros((T, P))
    trades = np.zeros((T, P))
    for j in range(P):
        t = 0
        while t < T - 1:
            if z[t, j] <= z_entry and AVAIL[t + 1, j]:
                end = min(T, t + 1 + hold)
                pos[t + 1:end, j] = 1.0
                trades[t + 1, j] += 1
                if end < T:
                    trades[end, j] += 1
                t = end - 1
            t += 1
    return pos, trades


def run(pos, trades, taker):
    cost = trades * (taker + SLIP)
    sleeve = pos * R - cost
    k = AVAIL.sum(1)
    port = np.where(k > 0, sleeve.sum(1) / np.maximum(k, 1), 0.0)
    gross = np.where(k > 0, (pos * R).sum(1) / np.maximum(k, 1), 0.0)
    return pd.DataFrame({"ret": port, "gross": gross, "ntr": trades.sum(1),
                         "expo": np.where(k > 0, pos.sum(1) / np.maximum(k, 1), 0)}, index=idx)


def m(df, a, b_):
    s = df.loc[a:b_]
    r = s.ret.to_numpy()
    yrs = len(r) / 8760
    ann = 8760
    return dict(cagr=(np.prod(1 + r) ** (1 / yrs) - 1) * 100, cagr_gross=(np.prod(1 + s.gross) ** (1 / yrs) - 1) * 100,
                sharpe=r.mean() / r.std() * np.sqrt(ann), mdd=((np.cumprod(1 + r) / np.maximum.accumulate(np.cumprod(1 + r))) - 1).min() * 100,
                trades_per_yr=s.ntr.sum() / 2 / yrs, avg_expo=s.expo.mean() * 100)


rows = []
for h in PRE["h_hours"]:
    for ze in PRE["z_entry"]:
        for hold in PRE["hold_hours"]:
            pos, tr = positions(h, ze, hold)
            # gross per-trade edge (bps): sum of sleeve gross return / round trips
            gross_tot = (pos * R).sum()
            n_rt = tr.sum() / 2
            for taker in (0.009, 0.006, 0.0025, 0.0):
                df = run(pos, tr, taker)
                rows.append({"h": h, "z": ze, "hold": hold, "taker": taker,
                             "gross_bps_per_roundtrip": gross_tot / max(n_rt, 1) * 1e4,
                             **{f"train_{k}": v for k, v in m(df, "2022-01-01", "2023-12-31").items()},
                             **{f"test_{k}": v for k, v in m(df, "2024-01-01", "2026-09-27").items()}})
out = pd.DataFrame(rows)
out.to_csv("results_hourly.csv", index=False, float_format="%.3f")
pd.set_option("display.width", 250)
print(out.round(2).to_string())
