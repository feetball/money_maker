"""Sanity checks 2: cross-sectional momentum/reversal among Coinbase majors, and intraday /
weekend seasonality on Coinbase BTC-USD hourly candles, at retail fee levels.

Caveat: the 12-coin universe is a hand-picked set of *survivors* listed on Coinbase today, so
cross-sectional results are biased upward; treat them as an upper bound.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

D = Path(__file__).resolve().parent / "data"
OUT = Path(__file__).resolve().parent / "results"
COINS = ["BTC-USD", "ETH-USD", "LTC-USD", "BCH-USD", "SOL-USD", "LINK-USD", "XRP-USD",
         "DOGE-USD", "ADA-USD", "AVAX-USD", "DOT-USD", "XLM-USD"]
FEES = {"gross": 0.0, "taker0.60%": 0.006, "taker1.20%": 0.012}
SLIP = 0.0010  # 10 bps per side for alts


def closes():
    cols = {}
    for c in COINS:
        df = pd.read_parquet(D / f"{c}_1d.parquet")
        s = df.set_index("time")["close"].astype(float)
        s.index = s.index.tz_convert(None)
        cols[c] = s
    px = pd.DataFrame(cols).sort_index()
    return px[px.index >= "2017-01-01"]


def weekly_xsec(px, lookback_w, top_k, mode, abs_filter):
    """Weekly Sunday-close rebalance. mode='mom' buys the top_k past winners, 'rev' the top_k
    losers. abs_filter: only hold coins whose own lookback return > 0 (else cash)."""
    wk = px.resample("W-SUN").last()
    # coin eligible if it has >= lookback_w+4 weeks of history (avoid fresh listings)
    hist = wk.notna().cumsum()
    ret_lb = wk / wk.shift(lookback_w) - 1
    fwd = wk.shift(-1) / wk - 1
    rows = []
    prev = pd.Series(0.0, index=wk.columns)
    for t in wk.index[:-1]:
        r = ret_lb.loc[t]
        elig = r[(hist.loc[t] >= lookback_w + 4) & r.notna() & fwd.loc[t].notna()]
        if len(elig) < top_k + 2:
            rows.append((t, 0.0, 0.0, 0.0))
            continue
        pick = elig.sort_values(ascending=(mode == "rev")).index[:top_k]
        w = pd.Series(0.0, index=wk.columns)
        for c in pick:
            if (not abs_filter) or elig[c] > 0:
                w[c] = 1.0 / top_k
        # drift previous weights to today before computing turnover
        gross = float((w * fwd.loc[t].fillna(0)).sum())
        to = float((w - prev).abs().sum())
        ew = float(fwd.loc[t][elig.index].mean())
        rows.append((t, gross, to, ew))
        # weights after the week's move
        grown = w * (1 + fwd.loc[t].fillna(0))
        tot = grown.sum() + (1 - w.sum())
        prev = grown / tot if tot > 0 else w
    return pd.DataFrame(rows, columns=["t", "gross", "turnover", "ew_universe"]).set_index("t")


def summarize_weekly(r, label):
    out = []
    for fname, fee in FEES.items():
        cost = r["turnover"] * (fee + (SLIP if fee > 0 else 0))
        net = r["gross"] - cost
        for pname, (a, b) in {"2018-2026": ("2018-01-01", "2026-12-31"),
                              "2018-2021": ("2018-01-01", "2021-12-31"),
                              "2022-2026": ("2022-01-01", "2026-12-31"),
                              "postETF": ("2024-01-11", "2026-12-31")}.items():
            n = net[(net.index >= a) & (net.index <= b)]
            ew = r["ew_universe"][(r.index >= a) & (r.index <= b)]
            if len(n) < 10:
                continue
            yrs = len(n) / 52.18
            cagr = (1 + n).prod() ** (1 / yrs) - 1
            sh = n.mean() / n.std() * np.sqrt(52.18) if n.std() > 0 else np.nan
            eq = (1 + n).cumprod()
            out.append(dict(strategy=label, fee=fname, period=pname, cagr=cagr, sharpe=sh,
                            maxdd=(eq / eq.cummax() - 1).min(),
                            turnover_per_yr=r["turnover"][n.index].sum() / yrs,
                            ew_universe_cagr=(1 + ew).prod() ** (1 / yrs) - 1))
    return out


def xsec():
    px = closes()
    res = []
    for lb in (1, 3, 4):
        for mode in ("mom", "rev"):
            for absf in (False, True):
                if mode == "rev" and absf:
                    continue
                r = weekly_xsec(px, lb, 3, mode, absf)
                res += summarize_weekly(r, f"{mode} lb{lb}w top3{' +abs' if absf else ''}")
    df = pd.DataFrame(res)
    df.to_csv(OUT / "xsec_weekly_coinbase12.csv.gz", index=False, float_format="%.5f")
    return df


def intraday():
    h = pd.read_parquet(D / "BTC-USD_1h.parquet").set_index("time")["close"].astype(float)
    h.index = h.index.tz_convert(None)
    h = h.asfreq("1h").ffill()
    r = h.pct_change().dropna()  # return of the hour ENDING at index; candle time = bucket start
    # candle 'time' is the bucket start; close of bucket starting at t is at t+1h.
    # r at index t = close(t)/close(t-1h) - 1 = return over bucket [t, t+1h).
    df = pd.DataFrame({"r": r})
    df["hour"] = df.index.hour
    df["dow"] = df.index.dayofweek
    per = {"2017-2021": ("2017-01-01", "2021-12-31"), "2022-2023": ("2022-01-01", "2024-01-10"),
           "postETF": ("2024-01-11", "2026-09-27")}
    rows = []
    for pn, (a, b) in per.items():
        x = df[(df.index >= a) & (df.index <= b)]
        g = x.groupby("hour")["r"].agg(["mean", "std", "count"])
        g["t"] = g["mean"] / (g["std"] / np.sqrt(g["count"]))
        g["mean_bps"] = g["mean"] * 1e4
        g["period"] = pn
        rows.append(g.reset_index())
    hours = pd.concat(rows)
    hours.to_csv(OUT / "btc_hour_of_day.csv.gz", index=False, float_format="%.6f")

    # 21:00-23:00 UTC hold (buckets starting 21 and 22) -- Padysak & Vojtko 2022
    strat = []
    for pn, (a, b) in per.items():
        x = df[(df.index >= a) & (df.index <= b)]
        dly = x[x.hour.isin([21, 22])]["r"].groupby(x[x.hour.isin([21, 22])].index.date).apply(lambda s: (1 + s).prod() - 1)
        for fname, fee in {"gross": 0, "0.10%": 0.001, "taker0.60%": 0.006, "taker1.20%": 0.012}.items():
            net = dly - 2 * fee
            strat.append(dict(period=pn, fee=fname, days=len(net), mean_bps_per_day=net.mean() * 1e4,
                              t=net.mean() / (net.std() / np.sqrt(len(net))),
                              ann_return_simple=net.mean() * 365))
    # US cash-session (14:00-21:00 UTC buckets ~ 9:30-16:00 ET incl. DST approx) vs rest, weekdays
    sess = []
    for pn, (a, b) in per.items():
        x = df[(df.index >= a) & (df.index <= b)]
        wd = x[x.dow < 5]
        us = wd[wd.hour.between(14, 20)]["r"]
        non = x.drop(us.index)["r"]
        ndays = x.index.normalize().nunique()
        sess.append(dict(period=pn, us_session_bps_per_day=us.groupby(us.index.date).sum().sum() / ndays * 1e4,
                         other_hours_bps_per_day=non.sum() / ndays * 1e4))
    # weekend vs weekday daily
    d = pd.read_parquet(D / "BTC-USD_1d.parquet").set_index("time")["close"].astype(float)
    d.index = d.index.tz_convert(None)
    dr = d.pct_change().dropna().to_frame("r")
    dr["wkend"] = dr.index.dayofweek >= 5
    wk = []
    for pn, (a, b) in {"2016-2021": ("2016-01-01", "2021-12-31"), "2022-2023": ("2022-01-01", "2024-01-10"),
                       "postETF": ("2024-01-11", "2026-09-27")}.items():
        x = dr[(dr.index >= a) & (dr.index <= b)]
        for k, g in x.groupby("wkend"):
            wk.append(dict(period=pn, weekend=k, mean_bps=g.r.mean() * 1e4, std_bps=g.r.std() * 1e4,
                           t=g.r.mean() / (g.r.std() / np.sqrt(len(g))), n=len(g)))
    return hours, pd.DataFrame(strat), pd.DataFrame(sess), pd.DataFrame(wk)


if __name__ == "__main__":
    pd.set_option("display.width", 250, "display.max_rows", 500, "display.max_columns", 20)
    x = xsec()
    for per in ("2018-2026", "2022-2026", "postETF"):
        t = x[x.period == per]
        p = t.pivot_table(index="strategy", columns="fee", values=["cagr", "sharpe"]).round(3)
        p.columns = [f"{a}|{b}" for a, b in p.columns]
        e = t[t.fee == "gross"].set_index("strategy")[["maxdd", "turnover_per_yr", "ew_universe_cagr"]].round(2)
        print(f"\n=== xsec {per}\n", p.join(e))
    import os
    if os.path.exists(D / "BTC-USD_1h.parquet"):
        hours, strat, sess, wk = intraday()
        print("\n=== BTC hour-of-day mean bps (t-stat)")
        hp = hours.pivot_table(index="hour", columns="period", values=["mean_bps", "t"]).round(2)
        print(hp)
        print("\n=== 21-23 UTC hold\n", strat.round(3))
        print("\n=== US session vs other\n", sess.round(2))
        print("\n=== weekend vs weekday\n", wk.round(2))
        strat.to_csv(OUT / "btc_21_23utc_hold.csv", index=False, float_format="%.4f")
        sess.to_csv(OUT / "btc_us_session_split.csv", index=False, float_format="%.4f")
        wk.to_csv(OUT / "btc_weekend.csv", index=False, float_format="%.4f")
