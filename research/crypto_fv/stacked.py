"""Stacked fair value: q = sigmoid(a + b*logit(mid) + c*logit(p_model)), refit WALK-FORWARD
(expanding window, weekly refit; each week is traded with coefficients fit only on prior weeks).
Also reports a 'shrink-only' variant (c=0) to see whether the spot model adds anything beyond
recalibrating the market mid.

Usage: stacked.py SERIES [SERIES...]
"""
import os
import sys

import numpy as np
import pandas as pd
from scipy.optimize import minimize

import backtest as bt
from backtest import load, add_model, trades, summarize

HERE = os.path.dirname(os.path.abspath(__file__))
PARAMS = {  # vol estimator per series (chosen on first-half train in backtest.py runs)
    "KXBTCD": ("hl10", 3.5, 1.1), "KXETHD": ("hl10", 3.5, 1.0), "KXBTC15M": ("hl10", 3.5, 1.1),
    "KXETH15M": ("hl10", 3.5, 1.1), "KXSOL15M": ("hl30", 5.0, 1.0), "KXXRP15M": ("hl10", 3.5, 1.1),
    "KXDOGE15M": ("hl10", 3.5, 1.1), "KXNASDAQ100U": ("hl10", 5.0, 0.9), "KXINXU": ("hl10", 5.0, 0.9),
}


def lg(x):
    x = np.clip(x, 1e-3, 1 - 1e-3)
    return np.log(x / (1 - x))


def fit(X, y):
    def nll(b):
        z = X @ b
        return np.mean(np.logaddexp(0, z) - y * z)
    return minimize(nll, np.r_[0, 1, np.zeros(X.shape[1] - 2)], method="BFGS").x


def run(series, min_train_weeks=2):
    bt.POINT = series in ("KXNASDAQ100U", "KXINXU")
    vol, nu, vm = PARAMS.get(series, ("hl10", 3.5, 1.0))
    p = load(series)
    p = p.dropna(subset=["S", "var_hl10", "var_hl30", "var_hl1440"])
    if bt.POINT:
        et = pd.to_datetime(p["t"], unit="s", utc=True).dt.tz_convert("America/New_York")
        mm = et.dt.hour * 60 + et.dt.minute
        p = p[(mm >= 9 * 60 + 45) & (mm <= 16 * 60)]
    p = add_model(p, vol, nu, vm, col="pm")
    p = p[(p.yes_bid > 0) & (p.yes_ask < 1)].copy()
    p["week"] = (p.close_ts // (7 * 86400)).astype(int)
    weeks = sorted(p.week.unique())
    p["q_st"] = np.nan
    p["q_sh"] = np.nan
    coefs = []
    for i, w in enumerate(weeks):
        if i < min_train_weeks:
            continue
        tr = p[p.week < w]
        te = p.week == w
        y = tr.y.values
        X2 = np.c_[np.ones(len(tr)), lg(tr.mid.values), lg(tr.pm.values)]
        X1 = X2[:, :2]
        b2 = fit(X2, y)
        b1 = fit(X1, y)
        Xt = p.loc[te]
        p.loc[te, "q_st"] = 1 / (1 + np.exp(-(np.c_[np.ones(te.sum()), lg(Xt.mid.values), lg(Xt.pm.values)] @ b2)))
        p.loc[te, "q_sh"] = 1 / (1 + np.exp(-(np.c_[np.ones(te.sum()), lg(Xt.mid.values)] @ b1)))
        coefs.append((w, *np.round(b2, 3)))
    oos = p.dropna(subset=["q_st"]).copy()
    oos[["ticker", "event_ticker", "t", "lag", "close_ts", "y", "yes_bid", "yes_ask", "yes_bid_next", "yes_ask_next",
         "mid", "pm", "q_st", "q_sh", "volume", "date"]].to_parquet(os.path.join(HERE, "data", f"oos_stacked_{series}.parquet"))
    print(f"\n##### {series}: walk-forward OOS rows {len(oos)} events {oos.event_ticker.nunique()} "
          f"weeks {len(weeks) - min_train_weeks}; last coefs (a,b_mid,c_model) {coefs[-1][1:] if coefs else None}")
    ll = lambda y, q: -(y * np.log(np.clip(q, 1e-4, 1 - 1e-4)) + (1 - y) * np.log(np.clip(1 - q, 1e-4, 1 - 1e-4))).mean()
    print("OOS logloss  mid %.5f  model %.5f  shrink-only %.5f  stacked %.5f" % (
        ll(oos.y, oos.mid), ll(oos.y, oos.pm), ll(oos.y, oos.q_sh), ll(oos.y, oos.q_st)))
    cut = oos.close_ts.quantile(0.5)
    oos["half"] = np.where(oos.close_ts <= cut, "H1", "H2")
    rows = []
    for col in ["q_st", "q_sh"]:
        for thr in [0.0, 0.01, 0.02, 0.03, 0.05]:
            for ex in [False, "limit", True]:
                for part in ["all", "H1", "H2"]:
                    d = oos if part == "all" else oos[oos.half == part]
                    t = trades(d, thr, col=col, exec_next=ex, min_price=0.02, max_price=0.98)
                    s = summarize(t)
                    rows.append(dict(series=series, model=col, thr=thr,
                                     exec={False: "same_min", True: "market_next", "limit": "limit_next"}[ex],
                                     part=part, **{k: (float(v) if isinstance(v, (float, np.floating)) else v) for k, v in s.items()}))
    R = pd.DataFrame(rows)
    cols = ["model", "thr", "exec", "part", "n", "events", "mean_pnl_c", "ci_lo", "ci_hi", "hit", "avg_px", "avg_fee_c"]
    print(R[R.part == "all"][cols].round(3).to_string(index=False))
    print("-- halves (market_next) --")
    print(R[(R["exec"] == "market_next") & (R.part != "all")][cols].round(3).to_string(index=False))
    # per-lag for the stacked rule at thr 0.02 market_next
    per = []
    for lag, d in oos.groupby("lag"):
        t = trades(d, 0.02, col="q_st", exec_next=True, min_price=0.02, max_price=0.98)
        per.append(dict(lag=lag, **summarize(t)))
    print("-- per lag (stacked, thr 0.02, market_next) --")
    print(pd.DataFrame(per).round(3).to_string(index=False))
    R.to_csv(os.path.join(HERE, "results", f"{series}_stacked_walkforward.csv"), index=False)
    pd.DataFrame(coefs, columns=["week", "a", "b_mid", "c_model"]).assign(
        week_start=lambda d: pd.to_datetime(d.week * 7 * 86400, unit="s").dt.date).to_csv(
        os.path.join(HERE, "results", f"{series}_stacked_coefs.csv"), index=False)
    return R


if __name__ == "__main__":
    for s in sys.argv[1:]:
        run(s)
