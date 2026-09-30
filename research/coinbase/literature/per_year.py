"""Per-calendar-year net returns of the top candidate rules vs BTC buy-and-hold (Coinbase BTC-USD)."""
import numpy as np, pandas as pd
from sanity_backtest import load, strategies, run, OUT
px = load("BTC-USD"); px = px[px.index >= "2015-08-01"]
S = strategies(px)
keep = ["buy_hold", "SMA50x200", "px>SMA200", "TSMOM 12w weekly", "SMA20x100 (Grayscale)",
        "Donchian ensemble + VT25% band0.10", "VT-only BH target50% band0.10", "px>SMA50", "MAX10 (PV22)"]
rows = {}
for k in keep:
    for fee_name, fee in (("gross", 0.0), ("maker0.40%", 0.004), ("taker1.20%", 0.012)):
        net, dw, held = run(px, S[k], fee, 0.0002 if fee else 0.0)
        y = (1 + net[net.index >= "2016-01-01"]).groupby(net[net.index >= "2016-01-01"].index.year).prod() - 1
        rows[(k, fee_name)] = y
        # annual fee drag = sum over year of cost
        cost = (dw * (fee + (0.0002 if fee else 0)))
        rows[(k, fee_name + "_cost")] = cost[cost.index >= "2016-01-01"].groupby(cost[cost.index >= "2016-01-01"].index.year).sum()
df = pd.DataFrame(rows).T
df.to_csv(OUT / "btc_per_year.csv", float_format="%.4f")
pd.set_option("display.width", 250, "display.max_columns", 20)
print((df.xs("taker1.20%", level=1) * 100).round(1))
print("\nannual cost drag at 1.2% taker (%):")
print((df.xs("taker1.20%_cost", level=1) * 100).round(1))
print("\nmaker0.40% returns (%):")
print((df.xs("maker0.40%", level=1) * 100).round(1))
