"""Final walk-forward, out-of-sample strategy evaluation (supersedes the headline numbers of strategies.py/followup.py).

Differences vs strategies.py:
  * TEST is a FRESH START at the split T: the entry of a market in TEST is its first trigger at a grid time t >= T
    (strategies.py dropped markets that had already triggered before T, even if they were still open at T).
  * TRAIN entries: first trigger at t < T, market closed <= T (outcome known at T).
  * Every rule is also run on coarser calendar cadences (every 6h at 00/06/12/18 UTC; once a day at 14:00 UTC).
  * Event-clustered Poisson bootstrap CIs (B=2000), fees for orders of 1 / 10 / 100 contracts, weekly breakdown,
    fixed-fraction portfolio sims with max drawdown, per-series concentration for survivors.

Selection protocol (thresholds are chosen on TRAIN only):
  S1 pre-registered rules (fixed before any strategy result was seen; list in PRE below)
  S2 threshold families (fade-longshot X, back-favourite Y) x scope {ALL, non-Sports, Sports}: the member with the
     highest TRAIN mean among members with >= 100 train events is selected (only if > 0) -> its TEST result.
  S3 full grid (side x mid-band x horizon-to-EET x category x max-spread = ~4k configs): every config with TRAIN
     n_events >= 40, n >= 100, mean > 0, t >= 2 is "selected" -> distribution of their TEST means.

    uv run --with pandas --with pyarrow --with numpy python oos_eval.py
Outputs: oos_headline.csv, oos_families.csv, oos_grid.csv, oos_grid_selected.csv, oos_weekly.csv, oos_monthly.csv,
         oos_cadence.csv, oos_portfolio.csv, oos_survivor_series.csv, trades_oos_<id>.csv.gz
"""
from __future__ import annotations

import itertools
import json
import time

import numpy as np
import pandas as pd

from calib_lib import HERE, cluster_boot, fee_per_contract
from strategies import (BANDS, HZ, SPREADS, THRESH_HI, THRESH_LO, first_trigger, load_panel, portfolio_sim,
                        precompute, rule_mask, summarize, trades_for)

pd.set_option("display.width", 260); pd.set_option("display.max_columns", 50); pd.set_option("display.max_rows", 500)
TENNIS = ["KXATPMATCH", "KXATPCHALLENGERMATCH", "KXITFWMATCH", "KXITFMATCH", "KXWTAMATCH"]
B_BOOT = 2000


def split_idx(p: pd.DataFrame, m: np.ndarray, T: int):
    tt, cl = p["t"].to_numpy(), p["close_ts"].to_numpy()
    tr = first_trigger(p, m & (tt < T))
    tr = tr[cl[tr] <= T]
    te = first_trigger(p, m & (tt >= T))
    return tr, te


def make_trades(p, side, idx):
    t = trades_for(p, side, idx)
    if len(t):
        q, mult = t["entry"].to_numpy(), t["fee_multiplier"].astype("float64").to_numpy()
        t["pnl_c1"] = t["win"] - q - fee_per_contract(q, 1, mult)
    else:
        t["pnl_c1"] = []
    return t


def stats(d: pd.DataFrame, boot: bool = True) -> dict:
    s = summarize(d)
    if len(d) == 0:
        return s
    for c in ("pnl_prefee", "pnl_c1", "pnl_c10", "pnl_exact"):
        s[f"{c}_mean"] = d[c].mean()
    if boot and d["event_ticker"].nunique() >= 5:
        b = cluster_boot(d.assign(event_ticker=d["event_ticker"].astype(str)), ["pnl"], B=B_BOOT, seed=7)["pnl"]
        s["boot_lo95"], s["boot_hi95"] = b[1], b[2]
    fm = d[d["maker_filled"]]
    s["maker_fill_rate"] = d["maker_filled"].mean()
    s["maker_pnl_per_filled"] = fm["maker_pnl"].mean() if len(fm) else np.nan
    s["n_series"] = d["series_ticker"].nunique()
    s["max_loss_event_share"] = (d.groupby("event_ticker", observed=True)["pnl"].sum().min() / d["pnl"].sum()
                                 if d["pnl"].sum() > 0 else np.nan)
    return s


# ------------------------------------------------------------------------------------------------ rules
def pre_rules(p):
    nonsport = (p["category"] != "Sports").to_numpy()
    E, BID, MID = "entry", "bid", "mid"
    return {
        "A1": ("fade longshot: YES ask<=3c -> buy NO", dict(side="no", lo=0.97, hi=1.0, basis=BID)),
        "A2": ("fade longshot: YES ask<=5c -> buy NO", dict(side="no", lo=0.95, hi=1.0, basis=BID)),
        "A3": ("fade longshot: YES ask<=10c -> buy NO", dict(side="no", lo=0.90, hi=1.0, basis=BID)),
        "A4": ("fade longshot: YES ask<=15c -> buy NO", dict(side="no", lo=0.85, hi=1.0, basis=BID)),
        "B0": ("back favourite (literal): YES ask>=90c -> buy YES", dict(side="yes", lo=0.90, hi=1.0, basis=E)),
        "B1": ("back favourite: YES bid>=85c -> buy YES", dict(side="yes", lo=0.85, hi=1.0, basis=BID)),
        "B2": ("back favourite: YES bid>=90c -> buy YES", dict(side="yes", lo=0.90, hi=1.0, basis=BID)),
        "B3": ("back favourite: YES bid>=95c -> buy YES", dict(side="yes", lo=0.95, hi=1.0, basis=BID)),
        "B4": ("back favourite: YES bid>=97c -> buy YES", dict(side="yes", lo=0.97, hi=1.0, basis=BID)),
        "B4ns": ("back favourite: YES bid>=97c, non-Sports -> buy YES", dict(side="yes", lo=0.97, hi=1.0, basis=BID, extra=nonsport)),
        "C1": ("back longshot: YES ask<=10c -> buy YES", dict(side="yes", lo=0.001, hi=0.10 + 1e-6, basis=E)),
        "C2": ("fade favourite: YES bid>=80c -> buy NO", dict(side="no", lo=0.001, hi=0.20 + 1e-6, basis=E)),
        "C3": ("fade favourite: YES mid .70-.90, spread<=4c -> buy NO", dict(side="no", lo=0.10, hi=0.30 + 1e-6, basis=MID, max_spread=0.04)),
        "D1": ("tennis fade fav: YES mid .70-.90, spread<=4c, >=12h to EET -> buy NO",
               dict(side="no", lo=0.10, hi=0.30 + 1e-6, basis=MID, hz=(12, 1e9), max_spread=0.04, series=TENNIS)),
    }


def run_rule(p, T, kw):
    side = kw["side"]
    m = rule_mask(p, **kw)
    tr, te = split_idx(p, m, T)
    return make_trades(p, side, tr).assign(period="train"), make_trades(p, side, te).assign(period="test")


# ------------------------------------------------------------------------------------------------ grid (S3)
def grid(p: pd.DataFrame, T: int) -> pd.DataFrame:
    A = precompute(p)
    tt, cl = A["t"], A["close_ts"]
    h = p["h_to_eet"].to_numpy()
    cats = [None] + sorted(p["category"].astype(str).unique())
    cat_masks = {c: (p["category"] == c).to_numpy() for c in cats if c is not None}
    bands = [(lo, hi, f"[{lo:.3g},{hi:.3g})") for lo, hi in BANDS] + \
            [(x, 1.0, f">={x:.2f}") for x in THRESH_HI] + [(0.001, x, f"<{x:.2f}") for x in THRESH_LO]
    rows = []
    for side, (lo, hi, bname), (spn, sp) in itertools.product(["yes", "no"], bands, SPREADS.items()):
        base = rule_mask(p, side, lo, hi, None, None, sp, basis="mid")
        for (hzn, hz), cat in itertools.product(HZ.items(), cats):
            m = base
            if hz is not None:
                m = m & (h >= hz[0]) & (h < hz[1])
            if cat is not None:
                m = m & cat_masks[cat]
            if m.sum() < 30:
                continue
            tr = first_trigger(p, m & (tt < T))
            tr = tr[cl[tr] <= T]
            if len(tr) < 30:
                continue
            te = first_trigger(p, m & (tt >= T))
            r = dict(side=side, band=bname, lo=lo, hi=hi, horizon=hzn, category=cat or "ALL", spread=spn)
            for tag, ii in (("train", tr), ("test", te)):
                r.update({f"{tag}_{k}": v for k, v in _fast(A, side, ii).items()})
            rows.append(r)
    return pd.DataFrame(rows)


def _fast(A, side, idx):
    if len(idx) == 0:
        return dict(n=0)
    S = A[side]
    v, w, ev, dt = S["pnl"][idx], S["win"][idx], A["ev"][idx], A["date"][idx]
    n, mu = len(v), v.mean()

    def cse(codes):
        u, inv = np.unique(codes, return_inverse=True)
        G = len(u)
        sm = np.bincount(inv, weights=v - mu, minlength=G)
        return float(np.sqrt((sm ** 2).sum() / n ** 2 * (G / max(G - 1, 1)))), G
    se, G = cse(ev)
    se_d, _ = cse(dt)
    lr = max(1 - w.mean(), 1.0 / G)
    se_t = max(se, se_d, np.sqrt(lr * (1 - lr) / G))
    f = S["filled"][idx]
    return dict(n=n, n_events=G, mean=mu, se=se, se_date=se_d, t_stat=mu / se_t, hit=w.mean(),
                avg_entry=S["q"][idx].mean(), maker_fill=f.mean(),
                maker_mean=np.nanmean(S["mpnl"][idx]) if f.any() else np.nan)


# ------------------------------------------------------------------------------------------------ main
def cadence(p, name):
    if name == "1h":
        return p
    if name == "6h":
        k = p["t"] % (6 * 3600) == 0
    elif name == "daily14":
        k = p["t"] % 86400 == 14 * 3600
    else:
        raise ValueError(name)
    q = p[k].reset_index(drop=True)
    q["tcode"] = q["ticker"].cat.codes.astype("int64")
    return q


def main():
    t0 = time.time()
    split = json.loads((HERE / "split.json").read_text())
    T = int(split["split_ts"])
    p = load_panel()
    print(f"panel rows {len(p)}, markets {p.ticker.nunique()}, events {p.event_ticker.nunique()}, split {split['split']} "
          f"({time.time()-t0:.0f}s)")

    # ---------------- S1 pre-registered rules, three cadences
    PRE = pre_rules(p)
    head, cad_rows, all_trades, port = [], [], [], []
    for cad in ("1h", "6h", "daily14"):
        q = cadence(p, cad)
        PREq = pre_rules(q)
        for rid, (name, kw) in PREq.items():
            tr, te = run_rule(q, T, kw)
            for d in (tr, te):
                s = stats(d, boot=(cad == "1h"))
                s.update(rule_id=rid, rule=name, period=d["period"].iloc[0] if len(d) else "?", cadence=cad)
                (head if cad == "1h" else cad_rows).append(s)
            if cad == "1h":
                both = pd.concat([tr, te]).assign(rule_id=rid, rule=name)
                all_trades.append(both)
                for per, d in (("train", tr), ("test", te)):
                    for f in (0.01, 0.02, 0.05):
                        port.append(dict(rule_id=rid, period=per, frac=f, **portfolio_sim(d, frac=f)))
        print(f"cadence {cad} done ({time.time()-t0:.0f}s)")
    cols = ["rule_id", "rule", "cadence", "period", "n", "n_events", "n_series", "avg_entry", "hit", "pnl_prefee_mean",
            "mean", "se", "se_date", "t_stat", "boot_lo95", "boot_hi95", "pnl_c10_mean", "pnl_c1_mean", "pnl_exact_mean",
            "roi", "maker_fill_rate", "maker_pnl_per_filled"]
    hd = pd.DataFrame(head)
    hd = hd[[c for c in cols if c in hd.columns]]
    hd.to_csv(HERE / "oos_headline.csv", index=False)
    cd = pd.DataFrame(cad_rows)
    cd = pd.concat([hd, cd[[c for c in cols if c in cd.columns]]])
    cd.to_csv(HERE / "oos_cadence.csv", index=False)
    pr = pd.DataFrame(port)
    pr.to_csv(HERE / "oos_portfolio.csv", index=False)
    trades = pd.concat(all_trades, ignore_index=True)
    trades["week"] = np.where(trades["t"] >= T, "test wk" + ((trades["t"] - T) // (7 * 86400) + 1).astype(str),
                              "train")
    agg = dict(n=("pnl", "size"), n_events=("event_ticker", "nunique"), hit=("win", "mean"), entry=("entry", "mean"),
               pnl=("pnl", "mean"), pnl_c10=("pnl_c10", "mean"))
    mon = trades.groupby(["rule_id", "period", "month"], observed=True).agg(**agg).reset_index()
    mon.to_csv(HERE / "oos_monthly.csv", index=False)
    wk = trades[trades["period"] == "test"].groupby(["rule_id", "week"], observed=True).agg(**agg).reset_index()
    wk.to_csv(HERE / "oos_weekly.csv", index=False)
    print("\n== S1 pre-registered rules (hourly cadence)")
    print(hd.round(4).to_string(index=False))
    print("\n== cadence robustness (test only)")
    print(cd[cd.period == "test"][["rule_id", "cadence", "n", "n_events", "avg_entry", "hit", "mean", "se", "t_stat"]]
          .sort_values(["rule_id", "cadence"]).round(4).to_string(index=False))
    print("\n== portfolio sims")
    print(pr.round(4).to_string(index=False))
    print("\n== test weekly")
    print(wk.round(4).to_string(index=False))

    # ---------------- S2 families
    nonsport = (p["category"] != "Sports").to_numpy()
    fams = {
        "A fade longshot (YES ask<=X -> buy NO)": [(f"X={x}c", dict(side="no", lo=1 - x / 100, hi=1.0, basis="bid"))
                                                   for x in (1, 2, 3, 5, 7, 10, 15, 20)],
        "B back favourite (YES bid>=Y -> buy YES)": [(f"Y={y}c", dict(side="yes", lo=y / 100, hi=1.0, basis="bid"))
                                                     for y in (80, 85, 90, 93, 95, 96, 97, 98)],
    }
    rows = []
    for fam, members in fams.items():
        for scope, extra in (("ALL", None), ("non-Sports", nonsport), ("Sports", ~nonsport)):
            cand = []
            for mname, kw in members:
                tr, te = run_rule(p, T, dict(kw, extra=extra))
                s_tr, s_te = stats(tr, boot=False), stats(te, boot=True)
                cand.append(dict(family=fam, scope=scope, member=mname, **{f"train_{k}": v for k, v in s_tr.items()},
                                 **{f"test_{k}": v for k, v in s_te.items()}))
            c = pd.DataFrame(cand)
            ok = c[c["train_n_events"] >= 100]
            c["selected_on_train"] = False
            if len(ok) and ok["train_mean"].max() > 0:
                c.loc[ok["train_mean"].idxmax(), "selected_on_train"] = True
            rows.append(c)
    fs = pd.concat(rows, ignore_index=True)
    fs.to_csv(HERE / "oos_families.csv", index=False)
    show = ["family", "scope", "member", "train_n", "train_n_events", "train_mean", "train_t_stat", "test_n",
            "test_n_events", "test_mean", "test_boot_lo95", "test_boot_hi95", "test_t_stat", "test_hit",
            "test_pnl_c10_mean", "selected_on_train"]
    print("\n== S2 families (selection on TRAIN mean)")
    print(fs[show].round(4).to_string(index=False))
    print(f"({time.time()-t0:.0f}s)")

    # ---------------- survivor deep-dive: B family, YES bid >= 97c
    tr, te = run_rule(p, T, PRE["B4"][1])
    d = pd.concat([tr, te])
    ser = d.groupby(["period", "category", "series_ticker"], observed=True).agg(
        n=("pnl", "size"), events=("event_ticker", "nunique"), losses=("win", lambda w: int((w == 0).sum())),
        entry=("entry", "mean"), pnl=("pnl", "mean"), pnl_sum=("pnl", "sum")).reset_index()
    ser = ser[ser.n > 0].sort_values(["period", "n"], ascending=[True, False])
    ser.to_csv(HERE / "oos_survivor_series.csv", index=False)
    print("\n== B4 (YES bid>=97c) top series by trades")
    print(ser.groupby("period").head(20).round(4).to_string(index=False))
    for rid in ("B3", "B4", "B4ns", "A1", "D1"):
        trades[trades.rule_id == rid].to_csv(HERE / f"trades_oos_{rid}.csv.gz", index=False, compression="gzip")

    # ---------------- S3 grid
    g = grid(p, T)
    g.to_csv(HERE / "oos_grid.csv", index=False)
    sel = g[(g.train_n_events >= 40) & (g.train_n >= 100) & (g.train_mean > 0) & (g.train_t_stat >= 2.0)]
    sel = sel.sort_values("train_t_stat", ascending=False)
    sel.to_csv(HERE / "oos_grid_selected.csv", index=False)
    print(f"\n== S3 grid: {len(g)} configs; {len(sel)} selected on TRAIN (n_events>=40, n>=100, mean>0, t>=2)")
    if len(sel):
        tm = sel["test_mean"]
        print(f"TEST: share>0 {np.mean(tm > 0):.2f}; median {tm.median():.4f}; mean {tm.mean():.4f}; "
              f"share test t>=2: {np.mean(sel['test_t_stat'] >= 2):.2f}")
        cols = ["side", "band", "horizon", "category", "spread", "train_n", "train_n_events", "train_mean", "train_t_stat",
                "test_n", "test_n_events", "test_mean", "test_se", "test_t_stat", "test_hit", "test_avg_entry",
                "test_maker_fill", "test_maker_mean"]
        print(sel[cols].round(4).to_string(index=False))
    # all-config sanity: how many configs are positive in both periods with t>=2 in test?
    both = g[(g.train_mean > 0) & (g.test_mean > 0) & (g.test_t_stat >= 2) & (g.test_n_events >= 40)]
    print(f"configs with train mean>0 AND test mean>0 & test t>=2 & test events>=40: {len(both)} of {len(g)}")
    print(both.sort_values("test_t_stat", ascending=False).head(30)[
        ["side", "band", "horizon", "category", "spread", "train_n_events", "train_mean", "train_t_stat",
         "test_n_events", "test_mean", "test_t_stat", "test_avg_entry"]].round(4).to_string(index=False))
    print(f"done ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
