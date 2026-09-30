"""Conservative passive (maker) simulation on 1-min quote candles.

At decision time t (lag L min before close), if the model says the current best YES bid b_t is cheap
(p - b_t > thr) we rest a YES bid at b_t (joining the queue). We count a FILL only if some later minute
close (t, min(t+H, close-60)] shows yes_ask <= b_t, i.e. the market traded THROUGH our level (we
ignore at-touch fills where queue position is unknown -> pessimistic on fill count, and fills are
adversely selected, which is the realistic part). Symmetric for NO bids at 1 - a_t.
PnL per contract: YES: y - b_t - maker_fee ; NO: a_t - y - maker_fee.
"""
import os
import sys

import numpy as np
import pandas as pd

from backtest import load, add_model, cluster_ci
from fv_model import fee_per_contract

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


def main(series):
    p = load(series)
    p = p.dropna(subset=["S", "var_hl10", "var_hl1440"])
    p = add_model(p, "hl10", 3.5, 1.1, col="q")
    cut = p.close_ts.quantile(0.5)
    p["split"] = np.where(p.close_ts <= cut, "train", "test")
    c = pd.read_csv(os.path.join(DATA, f"candles_{series}.csv.gz"), usecols=["ticker", "ts", "yes_bid", "yes_ask"])
    c = c[c.ticker.isin(p.ticker.unique())].sort_values(["ticker", "ts"])
    groups = {t: (g.ts.values, g.yes_bid.values, g.yes_ask.values) for t, g in c.groupby("ticker")}
    H = 10 * 60
    rows = []
    p = p[p.lag.isin([5, 10, 15, 20, 30, 45])]
    p = p[(p.yes_bid > 0) & (p.yes_ask < 1)]
    for r in p.itertuples():
        ts, yb, ya = groups[r.ticker]
        end = min(r.t + H, r.close_ts - 60)
        m = (ts > r.t) & (ts <= end)
        fut_ask = ya[m]
        fut_bid = yb[m]
        # YES bid at b
        b, a = r.yes_bid, r.yes_ask
        rows.append((r.ticker, r.event_ticker, r.lag, r.split, "yes", r.q - b, b, bool((fut_ask <= b).any()), r.y - b))
        rows.append((r.ticker, r.event_ticker, r.lag, r.split, "no", (1 - r.q) - (1 - a), 1 - a,
                     bool((fut_bid >= a).any()), a - r.y))
    d = pd.DataFrame(rows, columns=["ticker", "event_ticker", "lag", "split", "side", "edge", "px", "filled", "pnl_raw"])
    d.to_parquet(os.path.join(DATA, f"maker_{series}.parquet"))
    out = []
    for thr in [-1.0, 0.0, 0.02, 0.05]:
        for mf in [0.0, 0.0175]:
            for sp in ["train", "test"]:
                x = d[(d.edge > thr) & d.filled & (d.split == sp) & (d.px > 0.03) & (d.px < 0.97)].copy()
                x["pnl"] = x.pnl_raw - fee_per_contract(x.px.values, coef=mf) if mf else x.pnl_raw
                lo, hi = cluster_ci(x) if len(x) else (np.nan, np.nan)
                posted = ((d.edge > thr) & (d.split == sp) & (d.px > 0.03) & (d.px < 0.97)).sum()
                out.append(dict(thr=thr, maker_fee_coef=mf, split=sp, posted=posted, fills=len(x),
                                fill_rate=len(x) / max(posted, 1), mean_pnl_c=100 * x.pnl.mean(),
                                ci_lo=100 * lo, ci_hi=100 * hi, events=x.event_ticker.nunique()))
    R = pd.DataFrame(out)
    print(R.round(3).to_string(index=False))
    R.to_csv(os.path.join(HERE, "results", f"{series}_maker.csv"), index=False)


if __name__ == "__main__":
    main(sys.argv[1])
