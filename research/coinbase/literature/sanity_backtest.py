"""Sanity-check the literature's spot-crypto rules on Coinbase data at Coinbase retail fees.

Not a production backtest: daily close-to-close, signal at close t, position held over t->t+1
(i.e. trade at the close that generated the signal; no look-ahead because the signal only uses
closes <= t). Long/cash only (spot, no shorting, no leverage). Cash earns 0.
Fees: cost = fee_per_side * |delta weight| charged on the day of the trade.
Slippage: +2 bps per side for BTC/ETH, +10 bps for other coins (Coinbase BTC-USD spread ~1 tick).

Rules are taken from the papers with their published parameters (no tuning here), so for rules
published before 2022 the 2022-2026 window is post-publication.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

D = Path(__file__).resolve().parent / "data"
OUT = Path(__file__).resolve().parent / "results"
OUT.mkdir(exist_ok=True)

FEES = {"gross": 0.0, "0.10%": 0.001, "maker0.40%": 0.004, "taker0.60%": 0.006,
        "taker0.90%": 0.009, "taker1.20%": 0.012}
PERIODS = {
    "2016-2026": ("2016-01-01", "2026-09-26"),
    "2016-2019": ("2016-01-01", "2019-12-31"),
    "2020-2021": ("2020-01-01", "2021-12-31"),
    "2022": ("2022-01-01", "2022-12-31"),
    "2023": ("2023-01-01", "2023-12-31"),
    "postETF_2024-01-11+": ("2024-01-11", "2026-09-26"),
    "2025-01+": ("2025-01-01", "2026-09-26"),
}


def load(p, g="1d"):
    df = pd.read_parquet(D / f"{p}_{g}.parquet")
    s = df.set_index("time")["close"].astype(float)
    s.index = s.index.tz_convert(None)
    return s


def rvol(r, n):
    return r.rolling(n).std() * np.sqrt(365)


# ---------------- signals (weight in [0,1] decided at close t, held over t -> t+1) ---------------

def sig_bh(px):
    return pd.Series(1.0, index=px.index)


def sig_price_above_sma(px, n):
    return (px > px.rolling(n).mean()).astype(float)


def sig_sma_cross(px, fast, slow):
    return (px.rolling(fast).mean() > px.rolling(slow).mean()).astype(float)


def sig_tsmom(px, lookback_days, rebalance="W-SUN"):
    """Long if trailing return > 0, evaluated only at weekly closes (LT 2021 style)."""
    raw = (px / px.shift(lookback_days) - 1 > 0).astype(float)
    wk = raw.resample(rebalance).last()
    return wk.reindex(px.index, method="ffill").shift(0).fillna(0.0)


def sig_donchian_single(px, n):
    """Zarattini-Pagani-Barbon single model: enter when close >= max of prior n closes; exit on a
    trailing stop = max(prior stop, channel midpoint of the last n closes)."""
    hi = px.rolling(n).max()
    lo = px.rolling(n).min()
    mid = (hi + lo) / 2
    pos = np.zeros(len(px))
    stop = np.nan
    inpos = False
    v = px.values
    hv, mv = hi.values, mid.values
    for i in range(len(px)):
        if np.isnan(hv[i]):
            continue
        if not inpos:
            if v[i] >= hv[i]:
                inpos = True
                stop = mv[i]
        else:
            stop = max(stop, mv[i])
            if v[i] < stop:
                inpos = False
        pos[i] = 1.0 if inpos else 0.0
    return pd.Series(pos, index=px.index)


def sig_donchian_ensemble(px, lbs=(5, 10, 20, 30, 60, 90, 150, 250, 360)):
    return sum(sig_donchian_single(px, n) for n in lbs) / len(lbs)


def vol_target(w, px, target=0.25, n=90, cap=1.0, band=0.0):
    r = px.pct_change()
    sv = rvol(r, n)
    tw = (w * (target / sv)).clip(upper=cap).fillna(0.0)
    if band <= 0:
        return tw
    # only trade when the target moves more than `band` away from the held weight
    out = np.zeros(len(tw))
    cur = 0.0
    for i, x in enumerate(tw.values):
        if abs(x - cur) > band or (x == 0.0 and cur != 0.0):
            cur = x
        out[i] = cur
    return pd.Series(out, index=tw.index)


def sig_max_min(px, n, which):
    """Padysak & Vojtko 2022: long for the next day when today's close is the n-day max (MAX),
    the n-day min (MIN), or either (BOTH)."""
    is_max = px >= px.rolling(n).max()
    is_min = px <= px.rolling(n).min()
    if which == "MAX":
        s = is_max
    elif which == "MIN":
        s = is_min
    else:
        s = is_max | is_min
    return s.astype(float)


# ---------------- evaluation ----------------

def run(px, w, fee, slip):
    r = px.pct_change().fillna(0.0)
    w = w.reindex(px.index).fillna(0.0)
    held = w.shift(1).fillna(0.0)  # weight held over day t (decided at close t-1)
    dw = w.diff().abs().fillna(w.abs())
    cost = dw * (fee + slip)  # paid at close t when rebalancing to w_t
    net = held * r - cost
    return net, dw, held


def stats(net, dw, held, start, end):
    m = (net.index >= start) & (net.index <= end)
    n = net[m]
    if len(n) < 20:
        return None
    eq = (1 + n).cumprod()
    yrs = len(n) / 365.0
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    vol = n.std() * np.sqrt(365)
    sharpe = n.mean() * 365 / vol if vol > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    to = dw[m].sum() / yrs
    return dict(cagr=cagr, vol=vol, sharpe=sharpe, maxdd=dd, turnover_per_yr=to,
                round_trips_per_yr=to / 2, exposure=held[m].mean(), total=eq.iloc[-1] - 1)


def strategies(px):
    s = {}
    s["buy_hold"] = sig_bh(px)
    for n in (20, 50, 100, 200):
        s[f"px>SMA{n}"] = sig_price_above_sma(px, n)
    s["SMA20x100 (Grayscale)"] = sig_sma_cross(px, 20, 100)
    s["SMA50x200"] = sig_sma_cross(px, 50, 200)
    s["TSMOM 1w weekly (LT21)"] = sig_tsmom(px, 7)
    s["TSMOM 4w weekly"] = sig_tsmom(px, 28)
    s["TSMOM 12w weekly"] = sig_tsmom(px, 84)
    s["Donchian20 midstop"] = sig_donchian_single(px, 20)
    s["Donchian ensemble (ZPB25) unscaled"] = sig_donchian_ensemble(px)
    ens = sig_donchian_ensemble(px)
    s["Donchian ensemble + VT25% daily"] = vol_target(ens, px, 0.25, 90)
    s["Donchian ensemble + VT25% band0.10"] = vol_target(ens, px, 0.25, 90, band=0.10)
    s["VT-only BH target50% band0.10"] = vol_target(sig_bh(px), px, 0.50, 30, band=0.10)
    s["VT-only BH target50% daily"] = vol_target(sig_bh(px), px, 0.50, 30)
    s["MAX10 (PV22)"] = sig_max_min(px, 10, "MAX")
    s["MIN10 (PV22)"] = sig_max_min(px, 10, "MIN")
    s["MAX10|MIN10 (PV22)"] = sig_max_min(px, 10, "BOTH")
    return s


def main():
    rows = []
    for prod, slip in (("BTC-USD", 0.0002), ("ETH-USD", 0.0002)):
        px = load(prod)
        px = px[px.index >= "2015-08-01"]
        for name, w in strategies(px).items():
            for fname, fee in FEES.items():
                net, dw, held = run(px, w, fee, slip if fee > 0 else 0.0)
                for pname, (a, b) in PERIODS.items():
                    st = stats(net, dw, held, a, b)
                    if st is None:
                        continue
                    rows.append(dict(product=prod, strategy=name, fee=fname, period=pname, **st))
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "daily_rules_coinbase.csv.gz", index=False, float_format="%.5f")
    return df


if __name__ == "__main__":
    df = main()
    pd.set_option("display.width", 250, "display.max_rows", 500, "display.max_columns", 20)
    for prod in ("BTC-USD", "ETH-USD"):
        for per in ("2016-2026", "2022", "postETF_2024-01-11+"):
            t = df[(df["product"] == prod) & (df.period == per) & (df.fee.isin(["gross", "taker0.60%", "taker1.20%"]))]
            p = t.pivot_table(index="strategy", columns="fee", values=["cagr", "sharpe"]).round(3)
            p.columns = [f"{a}|{b}" for a, b in p.columns]
            extra = t[t.fee == "gross"].set_index("strategy")[["maxdd", "round_trips_per_yr", "exposure"]].round(2)
            print(f"\n=== {prod} {per}")
            print(p.join(extra))
