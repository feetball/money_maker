"""Families A (trend), B (vol-target), C (x-sec momentum), E (baselines) on daily bars.

uv run --with pandas --with numpy --with pyarrow python run_daily.py
Writes: results_daily_all.csv.gz (every config, train+test at default cost), selected.csv,
        selected_sensitivity.csv, per_year.csv, rolling_origin.csv, bootstrap.csv
"""
from __future__ import annotations

import json
import warnings

import numpy as np
import pandas as pd

from common import (DEFAULT_TAKER, TEST, TRAIN, Data, block_bootstrap_excess, metrics, per_year,
                    simulate, trend_state)

warnings.filterwarnings("ignore")
D = Data()
IDX = D.idx
month_start = pd.Series(IDX.day == 1, index=IDX)
monday = pd.Series(IDX.dayofweek == 0, index=IDX)
PRE = json.load(open("prereg.json"))


def basket_cols(n):
    top = D.in_top(n)
    return top, list(top.columns[top.any()])


# ------------------------------------------------------------ config builders -> target frames
def tgt_single(prod, rule, p):
    st = trend_state(D, [prod], rule, p)
    return st.shift(1).fillna(0.0), None, 0.0          # decided at close t-1, filled open t


def tgt_basket_trend(n, rule, p):
    top, cols = basket_cols(n)
    st = trend_state(D, cols, rule, p).shift(1).fillna(0.0)
    tg = st * top[cols].astype(float) / n
    return tg, month_start, 0.5 / n


def vol30(cols):
    return D.ret_cc[cols].rolling(30, min_periods=20).std() * np.sqrt(365)


def tgt_voltarget(asset, sig, tv, band):
    if asset.startswith("EW"):
        n = int(asset.replace("EW-top", ""))
        top, cols = basket_cols(n)
        mem = top[cols].astype(float)
        scale = 1.0 / n
    else:
        cols = [asset]
        mem = pd.DataFrame(1.0, index=IDX, columns=cols)
        scale = 1.0
    st = trend_state(D, cols, "always" if sig == "always" else "sma", None if sig == "always" else (int(sig[3:]), 0.0))
    w = (tv / vol30(cols)).clip(upper=1.0)
    tg = (st * w).shift(1).fillna(0.0) * mem * scale
    return tg, None, band * scale


def tgt_xsec(U, L, k, filt):
    top, cols = basket_cols(U)
    c = D.close[cols]
    mom = c / c.shift(7 * L) - 1.0                      # known at close t
    elig = top[cols].shift(-1, fill_value=False) & D.valid[cols] & mom.notna()   # universe of trade day t+1
    m = mom.where(elig)
    rk = m.rank(axis=1, ascending=False, method="first")
    pick = (rk <= k).astype(float) / k
    if filt != "none":
        b = D.close["BTC-USD"]
        n = int(filt[3:])
        on = (b > b.rolling(n, min_periods=n).mean()).astype(float)
        pick = pick.mul(on, axis=0)
    tg = pick.shift(1)                                   # Sunday close signal -> Monday open
    tg = tg.where(monday, np.nan)                        # only act on Mondays (NaN = hold)
    return tg, monday, 0.5 / k


def configs():
    A = PRE["families"]["A_trend"]["rules"]
    rules = [("sma", (n, b)) for n in A["sma"]["N"] for b in A["sma"]["buffer"]]
    rules += [("ema", (n, b)) for n in A["ema"]["N"] for b in A["ema"]["buffer"]]
    rules += [("donchian", tuple(x)) for x in A["donchian"]["entry_exit"]]
    rules += [("dualma", tuple(x)) for x in A["dualma"]["fast_slow"]]
    for asset in ["BTC-USD", "ETH-USD", "EW-top5", "EW-top10"]:
        for rule, p in rules:
            name = f"A|{asset}|{rule}{p}"
            if asset.startswith("EW"):
                yield "A", asset, name, (lambda n=int(asset[6:]), r=rule, p=p: tgt_basket_trend(n, r, p))
            else:
                yield "A", asset, name, (lambda a=asset, r=rule, p=p: tgt_single(a, r, p))
    B = PRE["families"]["B_voltarget"]
    for asset in ["BTC-USD", "ETH-USD", "EW-top10"]:
        for sig in B["signal"]:
            for tv in B["target_vol"]:
                for band in B["band_abs_weight"]:
                    yield "B", asset, f"B|{asset}|{sig}|tv{tv}|band{band}", \
                        (lambda a=asset, s=sig, tv=tv, bd=band: tgt_voltarget(a, s, tv, bd))
    C = PRE["families"]["C_xsec_momentum"]
    for U in C["universe_topU_by_trailing30d_usd_volume"]:
        for L in C["lookback_weeks"]:
            for k in C["k"]:
                for f in C["btc_filter"]:
                    yield "C", f"top{U}", f"C|top{U}|L{L}w|k{k}|{f}", (lambda U=U, L=L, k=k, f=f: tgt_xsec(U, L, k, f))


def baselines():
    yield "E", "BTC-USD", "E|buyhold BTC", lambda: (pd.DataFrame(1.0, index=IDX, columns=["BTC-USD"]), None, 0.0)
    yield "E", "ETH-USD", "E|buyhold ETH", lambda: (pd.DataFrame(1.0, index=IDX, columns=["ETH-USD"]), None, 0.0)
    for n in (5, 10):
        def f(n=n):
            top, cols = basket_cols(n)
            return top[cols].astype(float) / n, month_start, 0.0
        yield "E", f"EW-top{n}", f"E|EW top{n} monthly", f


def dca(start, end, taker=DEFAULT_TAKER):
    """Invest initial capital in equal monthly slices into BTC at each month's first open."""
    idx = IDX[(IDX >= pd.Timestamp(start, tz="UTC")) & (IDX <= pd.Timestamp(end, tz="UTC"))]
    firsts = idx[idx.day == 1]
    slice_ = 1.0 / len(firsts)
    cr = taker + D.slip_bps["BTC-USD"] / 1e4
    px = D.exec_px["BTC-USD"].reindex(idx)
    cash, units, eq, ex = 1.0, 0.0, [], []
    for t in idx:
        if t.day == 1:
            spend = min(slice_, cash)
            units += spend * (1 - cr) / px[t]
            cash -= spend
        eq.append(cash + units * px[t])
        ex.append(units * px[t] / eq[-1])
    eq = pd.Series(eq, index=idx)
    ret = eq.pct_change().fillna(0.0)
    res = pd.DataFrame({"ret": ret, "ret_gross": ret, "cost": 0.0, "turnover": 0.0,
                        "exposure": pd.Series(ex, index=idx)})
    return res


# ------------------------------------------------------------ run everything at default cost
def run_all():
    rows, rets, builders = [], {}, {}
    for fam, asset, name, fn in list(configs()) + list(baselines()):
        tg, rb, band = fn()
        res = simulate(tg, D, rebal=rb, band=band)
        rets[name] = res
        builders[name] = fn
        mtr, mte = metrics(res, *TRAIN), metrics(res, *TEST)
        rows.append({"family": fam, "asset": asset, "config": name,
                     **{f"train_{k}": v for k, v in mtr.items()}, **{f"test_{k}": v for k, v in mte.items()}})
    return pd.DataFrame(rows), rets, builders


if __name__ == "__main__":
    df, rets, builders = run_all()
    bh = rets["E|buyhold BTC"]["ret"]
    ew = rets["E|EW top10 monthly"]["ret"]
    for nm, key in (("btc", "E|buyhold BTC"), ("ew10", "E|EW top10 monthly")):
        df[f"test_excess_cagr_vs_{nm}"] = df["test_cagr"] - metrics(rets[key], *TEST)["cagr"]
    df.to_csv("results_daily_all.csv.gz", index=False, float_format="%.4f")

    # ---- selection per (family, asset) on TRAIN Sharpe
    sel = (df[df.family != "E"].sort_values(["train_sharpe", "train_turnover_yr"], ascending=[False, True])
           .groupby(["family", "asset"]).head(1))
    chosen = list(sel.config) + [c for c in df.config if c.startswith("E|")]
    # robustness: median test over all configs in the group
    grp = df[df.family != "E"].groupby(["family", "asset"]).agg(
        n=("config", "size"), test_sharpe_median=("test_sharpe", "median"), test_cagr_median=("test_cagr", "median"),
        frac_beat_btc_test=("test_excess_cagr_vs_btc", lambda x: (x > 0).mean()),
        train_sharpe_best=("train_sharpe", "max"))
    grp.to_csv("group_robustness.csv", float_format="%.3f")

    # ---- sensitivity + per-year + bootstrap for chosen
    sens_rows, py_rows, bs_rows = [], [], []
    for c in chosen:
        tg, rb, band = builders[c]()
        for taker in [0.012, DEFAULT_TAKER, 0.006, 0.0025, 0.0]:
            res = rets[c] if taker == DEFAULT_TAKER else simulate(tg, D, taker=taker, rebal=rb, band=band)
            m = metrics(res, *TEST)
            sens_rows.append({"config": c, "taker": taker, **m,
                              "excess_cagr_vs_btc": m["cagr"] - metrics(rets["E|buyhold BTC"], *TEST)["cagr"]})
        py_rows.append({"config": c, **per_year(rets[c])})
        for bn, b in (("btc", bh), ("ew10", ew)):
            bs_rows.append({"config": c, "vs": bn, **block_bootstrap_excess(rets[c]["ret"], b, *TEST)})
    d_test = dca(*TEST)
    m = metrics(d_test, *TEST)
    sens_rows.append({"config": "E|DCA monthly BTC (test window)", "taker": DEFAULT_TAKER, **m})
    py_rows.append({"config": "E|DCA monthly BTC (test window)", **per_year(d_test)})
    pd.DataFrame(sens_rows).to_csv("selected_sensitivity.csv", index=False, float_format="%.3f")
    pd.DataFrame(py_rows).to_csv("per_year.csv", index=False, float_format="%.2f")
    pd.DataFrame(bs_rows).to_csv("bootstrap.csv", index=False, float_format="%.3f")
    sel.to_csv("selected.csv", index=False, float_format="%.3f")

    # ---- rolling origin: expanding window from 2018, reselect at each year end, trade next year
    ro_rows, ro_series = [], {}
    for (fam, asset), g in df[df.family != "E"].groupby(["family", "asset"]):
        pieces = []
        for Y in range(2020, 2026):
            best = max(g.config, key=lambda c: metrics(rets[c], "2018-01-01", f"{Y}-12-31")["sharpe"])
            s = rets[best].loc[f"{Y+1}-01-01":f"{Y+1}-12-31"]
            pieces.append(s)
            ro_rows.append({"family": fam, "asset": asset, "origin": f"{Y}-12-31", "trade_year": Y + 1, "picked": best,
                            "ret_year_pct": (np.prod(1 + s["ret"]) - 1) * 100,
                            "btc_year_pct": (np.prod(1 + bh.loc[s.index]) - 1) * 100})
        oos = pd.concat(pieces)
        ro_series[f"{fam}|{asset}"] = oos
        m = metrics(oos, oos.index[0], oos.index[-1])
        mb = metrics(rets["E|buyhold BTC"], oos.index[0], oos.index[-1])
        ro_rows.append({"family": fam, "asset": asset, "origin": "ALL", "trade_year": "2021-2026",
                        "picked": "", "ret_year_pct": m["cagr"], "btc_year_pct": mb["cagr"],
                        "sharpe": m["sharpe"], "mdd": m["mdd"], "btc_sharpe": mb["sharpe"], "btc_mdd": mb["mdd"]})
    pd.DataFrame(ro_rows).to_csv("rolling_origin.csv", index=False, float_format="%.3f")
    print("done")
