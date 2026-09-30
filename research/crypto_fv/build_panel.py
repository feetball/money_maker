"""Build a decision panel: one row per (market, lag) with quote at decision time,
spot at decision time, vol estimates (only past data), and realized outcome.

Usage: build_panel.py SERIES SPOT_PRODUCT
Output: data/panel_<SERIES>.parquet
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

LAGS = [int(x) for x in os.environ.get("LAGS", "55,45,30,20,15,10,5,3,2,1").split(",")]
HALF_LIVES = [10, 30, 60, 240, 1440]  # minutes


def load_spot(product):
    s = pd.read_csv(os.path.join(DATA, f"spot_{product}.csv"))
    # price "at" time t := close of candle that STARTED at t-60  (i.e., ended at t)
    s["t"] = s["ts"] + 60
    s = s.set_index("t").sort_index()
    grid = np.arange(s.index.min(), s.index.max() + 60, 60)
    s = s.reindex(grid)
    s["close"] = s["close"].ffill()
    s["mid_bar"] = ((s["open"] + s["close"]) / 2).fillna(s["close"])
    lp = np.log(s["close"])
    r = lp.diff()
    s["r"] = r
    for hl in HALF_LIVES:
        s[f"var_hl{hl}"] = (r ** 2).ewm(halflife=hl, min_periods=hl).mean()
    # seasonal: mean r^2 at same minute-of-day over trailing 14 days (strictly past days)
    mod = ((s.index.values // 60) % 1440)
    r2 = (r ** 2).values
    df = pd.DataFrame({"mod": mod, "r2": r2, "day": s.index.values // 86400}, index=s.index)
    # smooth r^2 within each day over +-15 min window to reduce noise, then average across past 14 days
    r2s = pd.Series(r2, index=s.index).rolling(31, center=True, min_periods=5).mean()
    df["r2s"] = r2s.values
    piv = df.pivot_table(index="day", columns="mod", values="r2s")
    # trailing 14-day mean excluding current day
    prof = piv.rolling(14, min_periods=5).mean().shift(1)
    s["day"] = df["day"].values
    s["mod"] = mod
    s.attrs["profile"] = prof
    return s


def seasonal_forward_var(s, t_arr, lag_arr):
    """Expected variance over (t, t+lag] minutes using the trailing seasonal profile
    scaled by (current 1d-EWMA / profile-average-over-last-day)."""
    prof = s.attrs["profile"]
    out = np.full(len(t_arr), np.nan)
    for i, (t, L) in enumerate(zip(t_arr, lag_arr)):
        day = t // 86400
        if day not in prof.index:
            continue
        row = prof.loc[day]
        mods = ((t // 60) + np.arange(1, L + 1)) % 1440
        v = row.values[mods]
        if np.isnan(v).any():
            continue
        out[i] = v.sum()
    return out


def main(series, product):
    s = load_spot(product)
    m = pd.read_csv(os.path.join(DATA, f"markets_{series}.csv.gz"))
    m = m[m["result"].isin(["yes", "no"])].drop_duplicates("ticker").copy()
    c = pd.read_csv(os.path.join(DATA, f"candles_{series}.csv.gz"))
    c = c.sort_values(["ticker", "ts"])
    c = c[c["ticker"].isin(m["ticker"])]
    m = m[m["ticker"].isin(c["ticker"].unique())]
    mi = m.set_index("ticker")

    rows = []
    for L in LAGS:
        t_dec = mi["close_ts"] - 60 * L
        d = pd.DataFrame({"ticker": mi.index, "t": t_dec.values})
        d = d[d["t"] >= mi.loc[d["ticker"], "open_ts"].values]
        d = d.sort_values("t")
        cc = c.sort_values("ts")
        # quote at t: last candle with ts <= t for same ticker (as-of join; no look-ahead)
        j = pd.merge_asof(d, cc[["ticker", "ts", "yes_bid", "yes_ask"]], left_on="t", right_on="ts",
                          by="ticker", direction="backward")
        # quote one minute later (latency robustness)
        d2 = d.copy()
        d2["t"] = d2["t"] + 60
        j2 = pd.merge_asof(d2.sort_values("t"), cc[["ticker", "ts", "yes_bid", "yes_ask"]], left_on="t",
                           right_on="ts", by="ticker", direction="backward")
        j2 = j2.rename(columns={"yes_bid": "yes_bid_next", "yes_ask": "yes_ask_next", "ts": "ts_next"})
        j2["t"] = j2["t"] - 60
        j = j.merge(j2[["ticker", "t", "yes_bid_next", "yes_ask_next"]], on=["ticker", "t"], how="left")
        j["lag"] = L
        j["quote_age"] = j["t"] - j["ts"]
        rows.append(j)
    p = pd.concat(rows, ignore_index=True)
    p = p.dropna(subset=["yes_bid", "yes_ask"])
    p = p.join(mi[["event_ticker", "cadence", "floor_strike", "cap_strike", "strike_type", "close_ts",
                   "open_ts", "result", "expiration_value", "volume"]], on="ticker")
    p["y"] = (p["result"] == "yes").astype(int)
    # spot at t
    p["S"] = s["close"].reindex(p["t"].values).values
    for hl in HALF_LIVES:
        p[f"var_hl{hl}"] = s[f"var_hl{hl}"].reindex(p["t"].values).values
    p["seas_var"] = seasonal_forward_var(s, p["t"].values.astype(int), p["lag"].values.astype(int))
    # 1d-EWMA vs profile daily mean ratio for scaling seasonal
    prof = s.attrs["profile"]
    daymean = prof.mean(axis=1)
    p["prof_mean"] = daymean.reindex((p["t"] // 86400).values).values
    # settlement proxy from Coinbase: avg of last-minute bar (open+close)/2 ending at close_ts
    p["S_T_cb"] = s["mid_bar"].reindex(p["close_ts"].values).values
    p["S_T_cb_close"] = s["close"].reindex(p["close_ts"].values).values
    p.to_parquet(os.path.join(DATA, f"panel_{series}.parquet"))
    print(series, p.shape, p.groupby("lag").size().to_dict())


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
