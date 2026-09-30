"""Fair-value backtest for RANGE markets (KXBTC hourly: 'between' buckets + 'less'/'greater' tails).

Model: P(K1 <= S_T < K2) = P(S_T>K1) - P(S_T>K2) with Student-t log-returns, EWMA vol from Coinbase
1-min returns (no look-ahead), horizon = lag - 2/3 min (60s-average settlement), rolling BRTI-Coinbase
basis from previously settled events.  Taker rule identical to backtest.py.
Usage: range_backtest.py SERIES  (needs data/panel_SERIES.parquet)
"""
import os, sys
import numpy as np, pandas as pd
from fv_model import prob_above, horizon_minutes_eff, fee_per_contract
from backtest import trades, summarize, logloss

HERE = os.path.dirname(os.path.abspath(__file__))
pd.set_option("display.width", 250); pd.set_option("display.max_rows", 500)


def load(series):
    p = pd.read_parquet(os.path.join(HERE, "data", f"panel_{series}.parquet"))
    p["K1"] = np.where(p.strike_type.isin(["between", "greater", "greater_or_equal"]), pd.to_numeric(p.floor_strike, errors="coerce"), np.nan)
    p["K2"] = np.where(p.strike_type.isin(["between", "less", "less_or_equal"]), pd.to_numeric(p.cap_strike, errors="coerce"), np.nan)
    p["K"] = p["K1"].fillna(p["K2"])
    p["expiration_value"] = pd.to_numeric(p.expiration_value, errors="coerce")
    p["yes_bid"] = p.yes_bid.astype(float); p["yes_ask"] = p.yes_ask.astype(float)
    p["mid"] = (p.yes_bid + p.yes_ask) / 2
    ev = p.groupby("event_ticker").agg(close_ts=("close_ts", "first"), xv=("expiration_value", "first"), cb=("S_T_cb", "first")).dropna().sort_values("close_ts")
    ev["b"] = ev.xv - ev.cb
    ev["b_roll"] = ev.b.rolling(48, min_periods=10).median().shift(1)
    bt = ev[["close_ts", "b_roll"]].dropna().rename(columns={"close_ts": "bts"})
    p = pd.merge_asof(p.sort_values("t"), bt, left_on="t", right_on="bts", direction="backward")
    p["basis"] = p.b_roll.fillna(0.0)
    p["date"] = pd.to_datetime(p.close_ts, unit="s").dt.floor("D")
    return p.dropna(subset=["S", "var_hl10", "var_hl1440"])


def add_model(p, vol="hl10", nu=3.5, vm=1.1, col="pm"):
    tau = horizon_minutes_eff(p.lag.values)
    sig = np.sqrt(p[f"var_{vol}"].values * tau) * vm
    a1 = np.where(np.isnan(p.K1.values), 1.0, prob_above(p.S.values, np.nan_to_num(p.K1.values - 0.01, nan=1.0), sig, nu, p.basis.values))
    a2 = np.where(np.isnan(p.K2.values), 0.0, prob_above(p.S.values, np.nan_to_num(p.K2.values, nan=1.0), sig, nu, p.basis.values))
    p[col] = np.clip(a1 - a2, 0, 1)
    return p


def main(series):
    p = load(series)
    cut = p.close_ts.quantile(0.5)
    p["split"] = np.where(p.close_ts <= cut, "train", "test")
    print(series, "rows", len(p), "events", p.event_ticker.nunique(), "markets", p.ticker.nunique(),
          "dates", p.date.min().date(), p.date.max().date(), "cut", pd.to_datetime(cut, unit="s"))
    two = p[(p.yes_bid > 0) & (p.yes_ask < 1)].copy()
    print("two-sided rows", len(two), " median spread c by lag:", (100 * (two.yes_ask - two.yes_bid)).groupby(two.lag).median().to_dict())
    acc = []
    for vol in ["hl10", "hl30", "hl60", "hl240"]:
        for nu in [None, 5, 3.5]:
            for vm in [0.9, 1.0, 1.1, 1.2]:
                add_model(two, vol, nu, vm, "q")
                for sp in ["train", "test"]:
                    d = two[two.split == sp]
                    acc.append(dict(vol=vol, nu=nu or 0, vm=vm, split=sp, n=len(d), ll_model=logloss(d.y, d.q), ll_mid=logloss(d.y, d["mid"]),
                                    brier_model=((d.q - d.y) ** 2).mean(), brier_mid=((d["mid"] - d.y) ** 2).mean()))
    A = pd.DataFrame(acc); A.to_csv(os.path.join(HERE, "results", f"{series}_range_accuracy.csv"), index=False)
    b = A[A.split == "train"].sort_values("ll_model").iloc[0]
    bvol, bnu, bvm = b.vol, (None if b.nu == 0 else b.nu), b.vm
    print("\nbest on train:", bvol, bnu, bvm)
    print(A[(A.vol == bvol) & (A.nu == b.nu) & (A.vm == bvm)].to_string(index=False))
    add_model(two, bvol, bnu, bvm, "q")
    print("\nper-lag log-loss (test):")
    print(two[two.split == "test"].groupby("lag").apply(lambda d: pd.Series(dict(n=len(d), ll_model=logloss(d.y, d.q), ll_mid=logloss(d.y, d["mid"])))).round(4))
    bins = (0, .02, .05, .1, .2, .3, .4, .5, .6, .8, 1)
    print("\ncalibration (test): model pred vs market mid vs actual")
    t2 = two[two.split == "test"]
    print(t2.groupby(pd.cut(t2.q, bins, include_lowest=True), observed=True).agg(n=("y", "size"), pred=("q", "mean"), mid=("mid", "mean"), actual=("y", "mean")).round(4))
    print("calibration (test) by MARKET mid:")
    print(t2.groupby(pd.cut(t2["mid"], bins, include_lowest=True), observed=True).agg(n=("y", "size"), mid=("mid", "mean"), pred=("q", "mean"), actual=("y", "mean")).round(4))

    add_model(p, bvol, bnu, bvm, "pm")
    res = []
    for lags in [(1, 2, 3), (5, 10), (15, 20, 30), (45, 55), (5, 10, 15, 20, 30)]:
        for thr in [0.0, 0.02, 0.05, 0.08]:
            for ex in [False, True]:
                for sp in ["train", "test"]:
                    d = p[p.lag.isin(lags) & (p.split == sp)]
                    t = trades(d, thr, col="pm", exec_next=ex, min_price=0.03, max_price=0.97)
                    res.append(dict(lags=str(lags), thr=thr, exec="market_next" if ex else "same_min", split=sp, **summarize(t)))
    R = pd.DataFrame(res); R.to_csv(os.path.join(HERE, "results", f"{series}_range_trading.csv"), index=False)
    print("\n== taker trading (pnl c/contract, event-cluster bootstrap CI) ==")
    print(R.round(3).to_string(index=False))
    # sides split for the pooled mid-lag rule
    t = trades(p[p.lag.isin((5, 10, 15, 20, 30))], 0.02, col="pm", min_price=0.03, max_price=0.97)
    print("\nby side (lags 5-30, thr .02, same_min):")
    print(t.groupby("side").agg(n=("pnl", "size"), pnl_c=("pnl", lambda s: 100 * s.mean()), px=("px", "mean")).round(3))
    # blind EV by price bucket (no model): buying YES on range buckets at ask
    q = p[(p.yes_ask > 0) & (p.yes_ask < 1)]
    ev = q.y - q.yes_ask - fee_per_contract(q.yes_ask)
    print("\nblind BUY YES by ask bucket (all lags):")
    print(q.assign(ev=ev).groupby(pd.cut(q.yes_ask, [0, .03, .06, .1, .2, .35, .5, .65, .8, .9, .97, 1]), observed=True).agg(
        n=("ev", "size"), px=("yes_ask", "mean"), win=("y", "mean"), ev_c=("ev", lambda s: 100 * s.mean())).round(4))
    qn = p[(p.yes_bid > 0) & (p.yes_bid < 1)]
    na = 1 - qn.yes_bid
    evn = (1 - qn.y) - na - fee_per_contract(na)
    print("\nblind BUY NO by NO-ask bucket (all lags):")
    print(qn.assign(ev=evn, na=na).groupby(pd.cut(na, [0, .03, .06, .1, .2, .35, .5, .65, .8, .9, .97, 1]), observed=True).agg(
        n=("ev", "size"), px=("na", "mean"), win=("y", lambda s: 1 - s.mean()), ev_c=("ev", lambda s: 100 * s.mean())).round(4))


if __name__ == "__main__":
    main(sys.argv[1])
