"""Calibration of Kalshi quotes on a walk-forward calendar grid (every 6h: 00/06/12/18 UTC).

For every in-scope market OPEN at a grid time t (open_time <= t < close_time) the quote is the last hourly candle
that ENDED at or before t. Outputs (CSV, in this directory):
  calib_ask.csv       realized YES rate vs YES ask bucket  (+ EV of buying YES at the ask after taker fees)
  calib_bid.csv       realized YES rate vs YES bid bucket  (+ EV of buying NO at 1-bid after taker fees)
  calib_mid.csv       realized YES rate vs mid bucket, spread <= 0.04 only
  calib_by_category.csv / calib_by_horizon.csv / calib_by_period.csv   (coarse buckets)
All CIs: 95% Poisson bootstrap, resampling whole EVENTS (B=500).

    uv run --with pandas --with pyarrow --with numpy python calibration.py
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from calib_lib import H_BUCKETS, H_LABELS, HERE, PANEL_FILE, PRICE_BUCKETS, cluster_boot, fee_per_contract

COARSE = [0.0, 0.05, 0.15, 0.35, 0.65, 0.85, 0.95, 1.0001]
B = 500


def load_grid(step_h: int = 6) -> pd.DataFrame:
    p = pd.read_parquet(PANEL_FILE)
    p = p[p["t"] % (step_h * 3600) == 0]
    p = p[p["event_all_candles"]] if "event_all_candles" in p else p
    return p.reset_index(drop=True)


def add_ev(p: pd.DataFrame) -> pd.DataFrame:
    p = p.copy()
    ask, bid, y, m = p["ask"].astype(float), p["bid"].astype(float), p["y"].astype(float), p["fee_multiplier"].astype(float)
    p["has_ask"] = (ask < 1) & (ask > 0)
    p["has_bid"] = (bid > 0) & (bid < 1)
    p["ev_yes_prefee"] = y - ask
    p["ev_yes"] = y - ask - fee_per_contract(ask, 100, m)
    p["ev_no_prefee"] = bid - y
    p["ev_no"] = bid - y - fee_per_contract(1 - bid, 100, m)
    p["mid"] = (ask + bid) / 2
    p["spread"] = ask - bid
    return p


def table(p: pd.DataFrame, price_col: str, edges, by: list[str], ev_cols: list[str], min_n: int = 30) -> pd.DataFrame:
    p = p.assign(bucket=pd.cut(p[price_col], edges, right=False))
    rows = []
    for key, g in p.groupby(by + ["bucket"], observed=True):
        if len(g) < min_n:
            continue
        bs = cluster_boot(g, ["y"] + ev_cols, B=B, seed=len(g))
        key = key if isinstance(key, tuple) else (key,)
        r = dict(zip(by + ["bucket"], [str(k) for k in key]))
        r.update(n_obs=len(g), n_markets=g["ticker"].nunique(), n_events=g["event_ticker"].nunique(),
                 mean_price=g[price_col].mean(), yes_rate=bs["y"][0], yes_lo=bs["y"][1], yes_hi=bs["y"][2],
                 bias=bs["y"][0] - g[price_col].mean())
        for c in ev_cols:
            r[c] = bs[c][0]; r[c + "_lo"] = bs[c][1]; r[c + "_hi"] = bs[c][2]
        rows.append(r)
    return pd.DataFrame(rows)


def main():
    split = json.loads((HERE / "split.json").read_text())
    T = split["split_ts"]
    p = add_ev(load_grid())
    p["period"] = np.where(p["t"] < T, "train(<split)", "test(>=split)")
    p["hz"] = pd.cut(p["h_to_eet"], H_BUCKETS, labels=H_LABELS, right=False)
    print(f"grid rows {len(p)}, markets {p.ticker.nunique()}, events {p.event_ticker.nunique()}")
    a = p[p["has_ask"]]
    b = p[p["has_bid"]]
    tm = p[p["has_ask"] & p["has_bid"] & (p["spread"] <= 0.04)]
    out = {}
    ev2 = ["ev_yes", "ev_no"]
    out["calib_ask"] = pd.concat([
        table(a.assign(all="all"), "ask", PRICE_BUCKETS, ["all"], ["ev_yes_prefee", "ev_yes"]).assign(filter="any spread"),
        table(a[a["spread"] <= 0.04].assign(all="all"), "ask", PRICE_BUCKETS, ["all"], ["ev_yes_prefee", "ev_yes"]).assign(filter="spread<=4c")])
    out["calib_bid"] = pd.concat([
        table(b.assign(all="all"), "bid", PRICE_BUCKETS, ["all"], ["ev_no_prefee", "ev_no"]).assign(filter="any spread"),
        table(b[b["spread"] <= 0.04].assign(all="all"), "bid", PRICE_BUCKETS, ["all"], ["ev_no_prefee", "ev_no"]).assign(filter="spread<=4c")])
    out["calib_mid"] = pd.concat([
        table(tm.assign(all="all"), "mid", PRICE_BUCKETS, ["all"], ev2).assign(filter="spread<=4c"),
        table(tm[tm["spread"] <= 0.02].assign(all="all"), "mid", PRICE_BUCKETS, ["all"], ev2).assign(filter="spread<=2c")])
    # all breakdowns below: tight two-sided books (spread <= 4c), bucketed by MID; EVs are for crossing the spread
    out["calib_by_category"] = table(tm, "mid", COARSE, ["category"], ev2)
    out["calib_by_horizon"] = table(tm, "mid", COARSE, ["hz"], ev2)
    out["calib_by_period"] = table(tm, "mid", COARSE, ["period", "month"], ev2)
    out["calib_by_category_horizon"] = table(tm, "mid", COARSE, ["category", "hz"], ev2, min_n=100)
    out["calib_sports_series"] = table(tm[tm["category"] == "Sports"], "mid", COARSE, ["series_ticker"], ev2, min_n=200)
    wide = p[p["has_ask"] & p["has_bid"]].assign(sp=pd.cut(p["spread"], [0, 0.02, 0.04, 0.10, 0.30, 1.0]))
    out["calib_by_spread"] = table(wide, "mid", COARSE, ["sp"], ev2)
    for k, v in out.items():
        v.round(4).to_csv(HERE / f"{k}.csv", index=False)
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30); pd.set_option("display.max_rows", 400)
    short = ["bucket", "n_obs", "n_events", "mean_price", "yes_rate", "yes_lo", "yes_hi", "bias"]
    for k in out:
        cols = [c for c in out[k].columns if c in short or c in ("category", "hz", "period", "month", "series_ticker", "sp", "filter")
                or c.startswith("ev_")]
        print("\n==", k); print(out[k][cols].round(3).to_string(index=False))


if __name__ == "__main__":
    main()
