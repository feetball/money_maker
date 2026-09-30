"""Market-implied center/vol per (event, minute) from strike ladder mids, and lead-lag vs Coinbase.

For each event & minute t in final 55 min: take strikes with 0.04<mid<0.96 and two-sided quotes,
regress probit(mid) on ln K:  probit(mid) = (ln F - ln K)/sig  ->  sig = -1/slope, lnF = -intercept/slope.
Then compare ln F(t) with ln S(t - j) for j in -3..5 minutes (j<0 = FUTURE spot).
Output: data/implied_<SERIES>.parquet
"""
import os
import sys

import numpy as np
import pandas as pd
from scipy import stats

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


def main(series, product):
    m = pd.read_csv(os.path.join(DATA, f"markets_{series}.csv.gz"))
    m = m[m.strike_type == "greater"]
    c = pd.read_csv(os.path.join(DATA, f"candles_{series}.csv.gz"))
    c = c[c.ticker.isin(m.ticker)]
    mi = m.set_index("ticker")
    c = c.join(mi[["event_ticker", "floor_strike", "close_ts", "open_ts", "expiration_value"]], on="ticker")
    c = c[(c.ts > c.close_ts - 56 * 60) & (c.ts <= c.close_ts)]
    # forward-fill quotes per ticker on a minute grid
    out = []
    for ev, g in c.groupby("event_ticker"):
        close = g.close_ts.iloc[0]
        grid = np.arange(close - 55 * 60, close + 1, 60)
        piv_b = g.pivot_table(index="ts", columns="floor_strike", values="yes_bid").reindex(grid, method=None)
        piv_a = g.pivot_table(index="ts", columns="floor_strike", values="yes_ask").reindex(grid, method=None)
        # ffill within market lifetime (candles only when something changed)
        allb = g.pivot_table(index="ts", columns="floor_strike", values="yes_bid")
        alla = g.pivot_table(index="ts", columns="floor_strike", values="yes_ask")
        idx = np.union1d(allb.index.values, grid)
        piv_b = allb.reindex(idx).ffill().reindex(grid)
        piv_a = alla.reindex(idx).ffill().reindex(grid)
        K = piv_b.columns.values.astype(float)
        lnK = np.log(K)
        for t, yb, ya in zip(grid, piv_b.values, piv_a.values):
            ok = (yb > 0) & (ya < 1) & ~np.isnan(yb) & ~np.isnan(ya)
            mid = (yb + ya) / 2
            ok &= (mid > 0.04) & (mid < 0.96)
            if ok.sum() < 3:
                continue
            z = stats.norm.ppf(mid[ok])
            x = lnK[ok]
            A = np.vstack([np.ones_like(x), x]).T
            coef, *_ = np.linalg.lstsq(A, z, rcond=None)
            b0, b1 = coef
            if b1 >= 0:
                continue
            sig = -1 / b1
            lnF = -b0 / b1
            out.append((ev, close, t, np.exp(lnF), sig, ok.sum(), np.nanmean((ya - yb)[ok])))
    d = pd.DataFrame(out, columns=["event_ticker", "close_ts", "t", "F", "sig", "nk", "spread"])
    s = pd.read_csv(os.path.join(DATA, f"spot_{product}.csv"))
    s["t"] = s["ts"] + 60
    sp = s.set_index("t")["close"]
    for j in range(-3, 6):
        d[f"S_m{j}"] = sp.reindex(d["t"].values - 60 * j).values
    d.to_parquet(os.path.join(DATA, f"implied_{series}.parquet"))
    d["lag"] = (d.close_ts - d.t) // 60
    print(series, "rows", len(d), "events", d.event_ticker.nunique())
    # lead-lag: which spot lag best explains F changes? use minute-to-minute changes within event
    d = d.sort_values(["event_ticker", "t"])
    d["dF"] = np.log(d.F).groupby(d.event_ticker).diff()
    res = {}
    for j in range(-3, 6):
        dS = np.log(d[f"S_m{j}"]).groupby(d.event_ticker).diff()
        ok = d["dF"].notna() & dS.notna()
        res[j] = np.corrcoef(d.loc[ok, "dF"], dS[ok])[0, 1]
    print("corr( dlnF(t), dlnS(t-j) ) by j (neg j = future spot):", {k: round(v, 3) for k, v in res.items()})
    # level: basis F - S(t)
    d["basis_bps"] = 1e4 * np.log(d.F / d.S_m0)
    print("F vs S(t) basis bps: median", d.basis_bps.median(), "MAD", (d.basis_bps - d.basis_bps.median()).abs().median())
    # regression of dF on current and lagged dS
    X = []
    names = []
    for j in range(0, 4):
        X.append(np.log(d[f"S_m{j}"]).groupby(d.event_ticker).diff())
        names.append(f"dS_t-{j}")
    X = pd.concat(X, axis=1)
    X.columns = names
    ok = X.notna().all(1) & d.dF.notna()
    beta, *_ = np.linalg.lstsq(np.c_[np.ones(ok.sum()), X[ok].values], d.dF[ok].values, rcond=None)
    print("dlnF on dlnS lags:", dict(zip(["const"] + names, np.round(beta, 3))))
    # implied vol vs realized: annualized per-minute
    d["sig_per_min"] = d.sig / np.sqrt(np.maximum(d.lag - 2 / 3, 1 / 3))
    print(d.groupby(pd.cut(d.lag, [0, 2, 5, 10, 20, 35, 56]))[["sig_per_min", "spread", "nk"]].median())


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
