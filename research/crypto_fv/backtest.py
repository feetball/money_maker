"""Fair-value backtest on the decision panel.

Usage: backtest.py SERIES  (reads data/panel_<SERIES>.parquet)
Prints: calibration, log-loss/brier model vs market, trading results by lag/threshold/vol,
        OOS split, cluster-bootstrap CIs. Writes results/<SERIES>_*.csv
"""
import os
import sys

import numpy as np
import pandas as pd

from fv_model import fee_per_contract, prob_above, horizon_minutes_eff

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
RES = os.path.join(HERE, "results")
os.makedirs(RES, exist_ok=True)
pd.set_option("display.width", 250)
POINT = os.environ.get("POINT_SETTLE") == "1"  # index markets settle on a point value, not a 60s average
pd.set_option("display.max_columns", 40)
pd.set_option("display.max_rows", 400)


def load(series):
    p = pd.read_parquet(os.path.join(DATA, f"panel_{series}.parquet"))
    p = p[p["strike_type"].isin(["greater", "greater_or_equal"])].copy()
    p["K"] = pd.to_numeric(p["floor_strike"], errors="coerce")
    p["expiration_value"] = pd.to_numeric(p["expiration_value"], errors="coerce")
    p["yes_bid"] = p["yes_bid"].astype(float)
    p["yes_ask"] = p["yes_ask"].astype(float)
    p["mid"] = (p["yes_bid"] + p["yes_ask"]) / 2
    # basis: BRTI settlement - Coinbase final-minute mid, using only PAST settled events
    ev = p.groupby("event_ticker").agg(close_ts=("close_ts", "first"), xv=("expiration_value", "first"),
                                       cb=("S_T_cb_close" if POINT else "S_T_cb", "first")).dropna().sort_values("close_ts")
    ev["b"] = ev["xv"] - ev["cb"]
    ev["b_roll"] = ev["b"].rolling(48, min_periods=10).median().shift(1)
    # as-of join: basis known at decision time t = rolling median of events closed before t
    bt = ev[["close_ts", "b_roll"]].dropna().sort_values("close_ts")
    p = p.sort_values("t")
    p = pd.merge_asof(p, bt.rename(columns={"close_ts": "bts"}), left_on="t", right_on="bts",
                      direction="backward")
    p["basis"] = p["b_roll"].fillna(0.0)
    p["date"] = pd.to_datetime(p["close_ts"], unit="s").dt.floor("D")
    return p


def add_model(p, vol="hl60", nu=None, vol_mult=1.0, col="pm"):
    tau = p["lag"].values.astype(float) if POINT else horizon_minutes_eff(p["lag"].values)
    if vol.startswith("hl"):
        var_min = p[f"var_{vol}"].values
        sig = np.sqrt(var_min * tau)
    elif vol == "seas":
        # seasonal forward variance scaled by current 1d EWMA / profile mean
        scale = (p["var_hl1440"] / p["prof_mean"]).clip(0.3, 3.0).values
        sig = np.sqrt(p["seas_var"].values * scale * tau / np.maximum(p["lag"].values, 1))
    elif vol == "blend":
        scale = (p["var_hl1440"] / p["prof_mean"]).clip(0.3, 3.0).values
        v_seas = p["seas_var"].values * scale / np.maximum(p["lag"].values, 1)
        v = 0.5 * v_seas + 0.25 * p["var_hl60"].values + 0.25 * p["var_hl240"].values
        sig = np.sqrt(v * tau)
    else:
        raise ValueError(vol)
    p[col] = prob_above(p["S"].values, p["K"].values, sig * vol_mult, nu=nu, basis=p["basis"].values)
    return p


def logloss(y, q):
    q = np.clip(q, 1e-4, 1 - 1e-4)
    return -(y * np.log(q) + (1 - y) * np.log(1 - q)).mean()


def trades(p, thr, col="pm", rounded_fee=False, exec_next=False, min_price=0.0, max_price=1.0):
    """Take liquidity: buy YES at yes_ask if pm - ask - fee > thr; buy NO at (1-yes_bid) if
    (1-pm) - no_ask - fee > thr. Returns a trade DataFrame with pnl per contract."""
    ya = p["yes_ask_next" if exec_next else "yes_ask"].astype(float)
    yb = p["yes_bid_next" if exec_next else "yes_bid"].astype(float)
    # decision uses quote at t (what we saw); execution optionally at t+1min quote
    ya_dec, yb_dec = p["yes_ask"].astype(float), p["yes_bid"].astype(float)
    q = p[col]
    na_dec = 1 - yb_dec
    e_yes = q - ya_dec - fee_per_contract(ya_dec, rounded_fee)
    e_no = (1 - q) - na_dec - fee_per_contract(na_dec, rounded_fee)
    ok_yes = (ya_dec < 1) & (ya_dec > 0) & (ya_dec >= min_price) & (ya_dec <= max_price)
    ok_no = (yb_dec > 0) & (yb_dec < 1) & (na_dec >= min_price) & (na_dec <= max_price)
    buy_yes = ok_yes & (e_yes > thr) & (e_yes >= e_no)
    buy_no = ok_no & (e_no > thr) & ~buy_yes
    out = []
    for side, mask in (("yes", buy_yes), ("no", buy_no)):
        d = p.loc[mask, ["ticker", "event_ticker", "lag", "t", "date", "K", "S", "y", col, "yes_bid",
                         "yes_ask", "volume", "close_ts"]].copy()
        if side == "yes":
            px = ya[mask]
            win = d["y"]
            d["edge"] = e_yes[mask]
        else:
            px = 1 - yb[mask]
            win = 1 - d["y"]
            d["edge"] = e_no[mask]
        d["side"] = side
        d["px"] = px.values
        if exec_next == "limit":
            # limit order at the decision price, sent one minute late: fill only if the next-minute ask
            # is still <= decision ask (fill at the better of the two); otherwise no trade
            dec = (ya_dec if side == "yes" else 1 - yb_dec)[mask].values
            d["px"] = np.where(d["px"].values <= dec + 1e-9, d["px"].values, np.nan)
        d["fee"] = fee_per_contract(d["px"].values, rounded_fee)
        d["pnl"] = win.values - d["px"].values - d["fee"].values
        d = d[(d["px"] > 0) & (d["px"] < 1)]  # NaN px (unfilled limit) dropped here
        out.append(d)
    return pd.concat(out, ignore_index=True)


def cluster_ci(tr, n=500, seed=0):
    """Bootstrap mean pnl/contract resampling EVENTS (outcomes within an event are correlated)."""
    if len(tr) == 0:
        return (np.nan, np.nan)
    g = tr.groupby("event_ticker")["pnl"].agg(["sum", "count"])
    s, c = g["sum"].values, g["count"].values
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(g), size=(n, len(g)))
    means = s[idx].sum(1) / c[idx].sum(1)
    return tuple(np.percentile(means, [2.5, 97.5]))


def summarize(tr):
    if len(tr) == 0:
        return dict(n=0)
    lo, hi = cluster_ci(tr)
    return dict(n=len(tr), events=tr["event_ticker"].nunique(), mean_pnl_c=100 * tr["pnl"].mean(),
                ci_lo=100 * lo, ci_hi=100 * hi, hit=tr["pnl"].gt(0).mean(), avg_px=tr["px"].mean(),
                avg_edge_c=100 * tr["edge"].mean(), avg_fee_c=100 * tr["fee"].mean())


def calib_table(p, col="pm", bins=(0, .02, .05, .1, .2, .3, .4, .5, .6, .7, .8, .9, .95, .98, 1)):
    b = pd.cut(p[col], bins, include_lowest=True)
    return p.groupby(b, observed=True).agg(n=("y", "size"), pred=(col, "mean"), mid=("mid", "mean"),
                                           actual=("y", "mean"))


def main(series):
    p = load(series)
    # keep two-sided, sane quotes for the efficiency comparison
    p = p.dropna(subset=["S", "var_hl60", "var_hl240", "var_hl1440"]).copy()
    if POINT:  # index: only decide during regular trading hours (spot is stale pre-open; futures not available)
        et = pd.to_datetime(p["t"], unit="s", utc=True).dt.tz_convert("America/New_York")
        mins = et.dt.hour * 60 + et.dt.minute
        p = p[(mins >= 9 * 60 + 45) & (mins <= 16 * 60)].copy()
    print(series, "panel rows", len(p), "events", p["event_ticker"].nunique(), "dates",
          p["date"].min().date(), p["date"].max().date())
    cut = p["close_ts"].quantile(0.5)
    p["split"] = np.where(p["close_ts"] <= cut, "train", "test")
    print("train/test cut", pd.to_datetime(cut, unit="s"))

    # ---------- 1. model vs market accuracy ----------
    two = p[(p["yes_bid"] > 0) & (p["yes_ask"] < 1)].copy()
    rows = []
    for vol in ["hl10", "hl30", "hl60", "hl240", "hl1440", "seas", "blend"]:
        for nu in [None, 5, 3.5]:
            for vm in [0.8, 0.9, 1.0, 1.1, 1.2]:
                add_model(two, vol, nu, vm, col="q")
                for sp in ["train", "test"]:
                    d = two[(two["split"] == sp) & two["q"].notna()]
                    rows.append(dict(vol=vol, nu=nu or 0, vm=vm, split=sp, n=len(d),
                                     ll_model=logloss(d["y"], d["q"]), ll_mid=logloss(d["y"], d["mid"]),
                                     brier_model=((d["q"] - d["y"]) ** 2).mean(),
                                     brier_mid=((d["mid"] - d["y"]) ** 2).mean()))
    acc = pd.DataFrame(rows)
    acc.to_csv(os.path.join(RES, f"{series}_accuracy.csv"), index=False)
    tr_acc = acc[acc["split"] == "train"].sort_values("ll_model")
    print("\n== accuracy (two-sided quotes), best 10 on TRAIN by log-loss ==")
    print(tr_acc.head(10).to_string(index=False))
    best = tr_acc.iloc[0]
    bvol, bnu, bvm = best["vol"], (None if best["nu"] == 0 else best["nu"]), best["vm"]
    print("chosen on train:", bvol, bnu, bvm)
    print(acc[(acc.vol == bvol) & (acc.nu == best["nu"]) & (acc.vm == bvm)].to_string(index=False))

    # per-lag accuracy: model vs mid
    add_model(two, bvol, bnu, bvm, col="q")
    per_lag = two.groupby(["lag", "split"]).apply(
        lambda d: pd.Series(dict(n=len(d), ll_model=logloss(d["y"], d["q"]), ll_mid=logloss(d["y"], d["mid"]),
                                 mean_spread_c=100 * (d["yes_ask"] - d["yes_bid"]).mean())))
    print("\n== per-lag log-loss model vs market mid ==")
    print(per_lag)
    print("\n== calibration (test) of model; columns: model pred, market mid, actual ==")
    print(calib_table(two[two.split == "test"], "q"))

    # ---------- 2. trading ----------
    add_model(p, bvol, bnu, bvm, col="pm")
    res = []
    for lag in sorted(p["lag"].unique()):
        for thr in [0.0, 0.01, 0.02, 0.03, 0.05, 0.08]:
            for sp in ["train", "test"]:
                d = p[(p["lag"] == lag) & (p["split"] == sp)]
                t = trades(d, thr)
                res.append(dict(lag=lag, thr=thr, split=sp, **summarize(t)))
    R = pd.DataFrame(res)
    R.to_csv(os.path.join(RES, f"{series}_trading_by_lag.csv"), index=False)
    print("\n== trading by lag/threshold (taker, amortized fee), pnl in cents/contract ==")
    print(R.round(3).to_string(index=False))

    # ---------- 3. does the model add information beyond the market mid? (logistic stacking) ----------
    from scipy.optimize import minimize

    def lg(x):
        x = np.clip(x, 1e-3, 1 - 1e-3)
        return np.log(x / (1 - x))
    add_model(two, bvol, bnu, bvm, col="q")
    X = np.c_[np.ones(len(two)), lg(two["mid"].values), lg(two["q"].values)]
    yv = two["y"].values
    trm = (two["split"] == "train").values

    def nll(b, X, y):
        z = X @ b
        return np.mean(np.logaddexp(0, z) - y * z)
    b = minimize(nll, np.array([0, 1, 0.0]), args=(X[trm], yv[trm]), method="BFGS").x
    q_st = 1 / (1 + np.exp(-(X @ b)))
    print("\n== stacking: logit(y) ~ a + b*logit(mid) + c*logit(model) fitted on train ==", np.round(b, 3))
    print("test logloss mid", round(logloss(yv[~trm], two["mid"].values[~trm]), 5),
          "model", round(logloss(yv[~trm], two["q"].values[~trm]), 5),
          "stacked", round(logloss(yv[~trm], q_st[~trm]), 5))
    two["q_st"] = q_st
    rs = []
    for thr in [0.0, 0.01, 0.02, 0.03]:
        for sp in ["train", "test"]:
            t = trades(two[two["split"] == sp], thr, col="q_st")
            rs.append(dict(model="stacked", thr=thr, split=sp, **summarize(t)))
    print(pd.DataFrame(rs).round(3).to_string(index=False))

    # ---------- 4. market-only calibration: EV of blindly taking each side by price bucket ----------
    q = p[(p["yes_ask"] < 1) & (p["yes_ask"] > 0)]
    evy = q["y"] - q["yes_ask"] - fee_per_contract(q["yes_ask"])
    bins = [0, .03, .06, .1, .2, .35, .5, .65, .8, .9, .94, .97, 1]
    tab_y = q.assign(ev=evy).groupby(pd.cut(q["yes_ask"], bins), observed=True).agg(
        n=("ev", "size"), events=("event_ticker", "nunique"), px=("yes_ask", "mean"), win=("y", "mean"),
        ev_c=("ev", lambda s: 100 * s.mean()))
    qn = p[(p["yes_bid"] > 0) & (p["yes_bid"] < 1)]
    na = 1 - qn["yes_bid"]
    evn = (1 - qn["y"]) - na - fee_per_contract(na)
    tab_n = qn.assign(ev=evn, na=na).groupby(pd.cut(na, bins), observed=True).agg(
        n=("ev", "size"), events=("event_ticker", "nunique"), px=("na", "mean"),
        win=("y", lambda s: 1 - s.mean()), ev_c=("ev", lambda s: 100 * s.mean()))
    print("\n== blind BUY YES at ask, by ask bucket (all lags) ==")
    print(tab_y.round(4))
    print("\n== blind BUY NO at (1-yes_bid), by NO-ask bucket (all lags) ==")
    print(tab_n.round(4))
    tab_y.to_csv(os.path.join(RES, f"{series}_blind_yes.csv"))
    tab_n.to_csv(os.path.join(RES, f"{series}_blind_no.csv"))

    # ---------- 5. robustness of the model-taker rule ----------
    rob = []
    for vol in ["hl10", "hl60", "hl240", "blend", "seas"]:
        add_model(p, vol, bnu, bvm, col="pv")
        for thr in [0.02, 0.05]:
            for exec_next in [False, True]:
                for rf in [False, True]:
                    for sp in ["train", "test"]:
                        d = p[(p["split"] == sp) & (p["lag"].isin([5, 10, 15, 20, 30]))]
                        t = trades(d, thr, col="pv", rounded_fee=rf, exec_next=exec_next,
                                   min_price=0.03, max_price=0.97)
                        rob.append(dict(vol=vol, thr=thr, exec_next=exec_next, rounded_fee=rf, split=sp,
                                        **summarize(t)))
    RB = pd.DataFrame(rob)
    RB.to_csv(os.path.join(RES, f"{series}_robustness.csv"), index=False)
    print("\n== robustness (lags 5-30 pooled, px in [0.03,0.97]) ==")
    print(RB.round(3).to_string(index=False))
    return p, R


if __name__ == "__main__":
    main(sys.argv[1])
