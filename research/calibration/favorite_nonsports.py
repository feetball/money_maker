"""Deep-dive on the one rule that survived train->test: buy YES at the ask when YES bid >= 97c, non-Sports.
    uv run --with pandas --with pyarrow --with numpy python favorite_nonsports.py   -> favorite_nonsports_trades.csv.gz"""
import json

import numpy as np
import pandas as pd

from calib_lib import HERE
from strategies import first_trigger, load_panel, portfolio_sim, rule_mask, summarize, trades_for

pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40); pd.set_option("display.max_rows", 200)
T = int(json.loads((HERE / "split.json").read_text())["split_ts"])
p = load_panel()
m = rule_mask(p, "yes", 0.97, 1.0, basis="bid", extra=(p["category"] != "Sports").to_numpy())
idx = first_trigger(p, m)
tt, cl = p["t"].to_numpy()[idx], p["close_ts"].to_numpy()[idx]
tr = trades_for(p, "yes", idx[(tt < T) & (cl <= T)]).assign(period="train")
te = trades_for(p, "yes", idx[tt >= T]).assign(period="test")
allt = pd.concat([tr, te])
allt["hold_h"] = (allt["close_ts"] - allt["t"]) / 3600
allt.to_csv(HERE / "favorite_nonsports_trades.csv.gz", index=False, compression="gzip")
S = lambda g, c="pnl": pd.Series(summarize(g, c))
print("== overall"); print(allt.groupby("period").apply(S).round(4))
print("\n== test halves"); te2 = te.assign(half=np.where(te["t"] < T + 11 * 86400, "Sep04-14", "Sep15-25"))
print(te2.groupby("half").apply(S).round(4))
print("\n== fee variants (test): C=100", round(te.pnl.mean(), 5), " C=10", round(te.pnl_c10.mean(), 5), " exact", round(te.pnl_exact.mean(), 5), " prefee", round(te.pnl_prefee.mean(), 5))
print("\n== maker variant (rest at YES bid; filled only if later trades < bid) per period")
print(allt.groupby("period").agg(fill=("maker_filled", "mean"), pnl_filled=("maker_pnl", "mean")).round(4))
print("\n== series concentration (top 15 by trades)")
sc = allt.groupby(["series_ticker"], observed=True).agg(n=("pnl", "size"), events=("event_ticker", "nunique"),
      pnl=("pnl", "mean"), losses=("win", lambda w: int((w == 0).sum())), cat=("category", "first")).sort_values("n", ascending=False)
print(sc.head(15).round(4)); print("n series:", len(sc), " share of trades in top 5 series:", round(sc.n.head(5).sum() / sc.n.sum(), 3))
print("\n== leave-one-series-out (test): min / max mean over dropping each of the top 10 series")
vals = [te[te.series_ticker != s].pnl.mean() for s in sc.index[:10]]
print(round(min(vals), 5), round(max(vals), 5))
print("\n== entry price distribution"); print(allt.groupby("period")["entry"].describe().round(4))
print("\n== fee multiplier / fee type"); print(allt.groupby(["fee_multiplier", "fee_type"], observed=True).size())
print("\n== hold time hours (close - entry)"); print(allt.hold_h.describe().round(2))
print("\n== portfolio (fixed fraction of equity per trade, one position per market, <=10% equity per event)")
rows = []
for per, g in allt.groupby("period"):
    for f in (0.005, 0.01, 0.02, 0.05):
        rows.append(dict(period=per, frac=f, **portfolio_sim(g, frac=f)))
pr = pd.DataFrame(rows); print(pr.round(4)); pr.to_csv(HERE / "favorite_nonsports_portfolio.csv", index=False)
# loss-rate CI (exact Poisson on number of losing EVENTS in test) vs breakeven
L = int((te.win == 0).sum()); Le = te[te.win == 0].event_ticker.nunique()
be = (1 - te.entry - (te.entry - te.pnl_prefee + te.pnl - te.pnl_prefee).abs() * 0).mean()
print(f"\n test losses {L} trades in {Le} events of {len(te)} trades / {te.event_ticker.nunique()} events; "
      f"mean entry {te.entry.mean():.4f}; break-even loss rate ~ {(1 - te.entry.mean() - (te.pnl_prefee - te.pnl).mean()):.4f}; "
      f"observed loss rate {1 - te.win.mean():.4f}")
