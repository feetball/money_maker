"""Follow-up: (1) threshold selection within the two task families on TRAIN only, evaluated on TEST;
(2) diagnostics for the only rule family that is not clearly negative (buy YES on heavy favourites).

    uv run --with pandas --with pyarrow --with numpy python followup.py
Outputs: family_selection.csv, favorite_diagnostics.csv, favorite_losses.csv, favorite_portfolio.csv
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from calib_lib import HERE, cluster_se
from strategies import first_trigger, load_panel, portfolio_sim, rule_mask, summarize, trades_for

pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40); pd.set_option("display.max_rows", 400)


def split_trades(p, T, side, m):
    idx = first_trigger(p, m)
    tt, cl = p["t"].to_numpy()[idx], p["close_ts"].to_numpy()[idx]
    return trades_for(p, side, idx[(tt < T) & (cl <= T)]), trades_for(p, side, idx[tt >= T])


def main():
    T = int(json.loads((HERE / "split.json").read_text())["split_ts"])
    p = load_panel()
    nonsport = (p["category"] != "Sports").to_numpy()
    rows = []
    fams = {
        "A fade longshot (YES ask<=X -> buy NO at 1-YES bid)":
            [(f"X={x}c", dict(side="no", lo=1 - x / 100, hi=1.0, basis="bid")) for x in (1, 2, 3, 5, 7, 10, 15, 20)],
        "B back favourite (YES bid>=Y -> buy YES at ask)":
            [(f"Y={y}c", dict(side="yes", lo=y / 100, hi=1.0, basis="bid")) for y in (80, 85, 90, 93, 95, 96, 97, 98)],
    }
    for fam, members in fams.items():
        for scope, extra in (("ALL", None), ("non-Sports", nonsport), ("Sports", ~nonsport)):
            cand = []
            for name, kw in members:
                tr, te = split_trades(p, T, kw["side"], rule_mask(p, **kw, extra=extra))
                s_tr, s_te = summarize(tr), summarize(te)
                cand.append(dict(family=fam, scope=scope, member=name,
                                 **{f"train_{k}": v for k, v in s_tr.items()}, **{f"test_{k}": v for k, v in s_te.items()},
                                 test_c10=summarize(te, "pnl_c10")["mean"] if len(te) else np.nan,
                                 test_exact=summarize(te, "pnl_exact")["mean"] if len(te) else np.nan))
            c = pd.DataFrame(cand)
            ok = c[c.train_n_events >= 100]
            best = ok.loc[ok.train_mean.idxmax()] if len(ok) else None
            c["selected_on_train"] = False
            if best is not None and best.train_mean > 0:
                c.loc[best.name, "selected_on_train"] = True
            rows.append(c)
    fs = pd.concat(rows, ignore_index=True)
    fs.to_csv(HERE / "family_selection.csv", index=False)
    show = ["family", "scope", "member", "train_n", "train_n_events", "train_mean", "train_t_stat", "test_n",
            "test_n_events", "test_mean", "test_se", "test_se_date", "test_t_stat", "test_hit", "test_c10", "selected_on_train"]
    print(fs[show].round(4).to_string(index=False))

    # ---------------- diagnostics for B (YES bid >= 97c), all scopes, both periods
    diag = []
    for y in (95, 97):
        m = rule_mask(p, "yes", y / 100, 1.0, basis="bid")
        tr, te = split_trades(p, T, "yes", m)
        allt = pd.concat([tr.assign(period="train"), te.assign(period="test")])
        allt["hz"] = pd.cut(allt["h_to_eet"], [-1e9, 0, 1, 6, 24, 72, 1e9], labels=["<0", "0-1h", "1-6h", "6-24h", "1-3d", ">3d"])
        allt["h_to_close_actual"] = (allt["close_ts"] - allt["t"]) / 3600
        for by in ("category", "hz", "month", "period"):
            for (per, key), g in allt.groupby(["period", by], observed=True):
                if len(g) < 20:
                    continue
                s = summarize(g)
                diag.append(dict(rule=f"YES bid>={y}c", period=per, by=by, key=str(key), **s))
        for per, g in allt.groupby("period"):
            s = summarize(g)
            diag.append(dict(rule=f"YES bid>={y}c", period=per, by="ALL", key="ALL", **s))
        s = summarize(allt)
        diag.append(dict(rule=f"YES bid>={y}c", period="train+test", by="ALL", key="ALL", **s))
        if y == 97:
            loss = allt[allt["win"] == 0][["period", "ticker", "category", "date", "h_to_eet", "h_to_close_actual",
                                             "bid", "ask", "pnl"]]
            loss.to_csv(HERE / "favorite_losses.csv", index=False)
            print(f"\nYES bid>=97c: {len(allt)} trades, {len(loss)} losses; losses by category:")
            print(loss.groupby(["period", "category"]).size())
            print("entry h_to_close_actual quantiles (diagnostic only, not usable ex ante):",
                  allt["h_to_close_actual"].quantile([.1, .25, .5, .75, .9]).round(2).to_dict())
            port = []
            for per, g in allt.groupby("period"):
                for f in (0.005, 0.01, 0.02, 0.05):
                    port.append(dict(period=per, frac=f, **portfolio_sim(g, frac=f)))
            pr = pd.DataFrame(port)
            pr.to_csv(HERE / "favorite_portfolio.csv", index=False)
            print(pr.round(4).to_string(index=False))
    dg = pd.DataFrame(diag)
    dg.to_csv(HERE / "favorite_diagnostics.csv", index=False)
    print(dg[["rule", "period", "by", "key", "n", "n_events", "mean", "se", "se_date", "t_stat", "hit", "avg_entry"]]
          .round(4).to_string(index=False))


if __name__ == "__main__":
    main()
