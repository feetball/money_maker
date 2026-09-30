"""Quick replication of the Buergi-Deng-Whelan favourite-longshot check on 2026 Kalshi data.

Reads the shared settled-market table built by the data agent (read-only):
    /root/money_maker/research/data/markets.parquet
Uses `previous_price` (Kalshi: last traded YES price ~one day before the final state), so it
mixes maker- and taker-initiated prints exactly like the paper's "daily last trade" approach.
Both the YES side (price p) and the NO side (price 1-p) are counted, as in the paper.

Run: uv run --with pandas --with pyarrow python flb_check.py
"""
import numpy as np
import pandas as pd

M = "/root/money_maker/research/data/markets.parquet"
OUT = "/root/money_maker/research/literature/flb_check_results.csv"


def taker_fee(p, mult=1.0):
    # per-contract model fee, no rounding (large-order approximation)
    return 0.07 * mult * p * (1 - p)


def main():
    m = pd.read_parquet(M)
    m = m[m.result.isin(["yes", "no"])]
    # need a genuine pre-close print: market alive >= 48h and a non-degenerate previous price
    m = m[(m.life_hours >= 48) & (m.previous_price > 0) & (m.previous_price < 1)]
    m = m[m.volume >= 100]  # >=100 contracts traded
    m = m[~m.series_ticker.str.startswith("KXMVE")]
    y = (m.result == "yes").astype(float)
    mult = m.fee_multiplier.fillna(1.0)
    rows = pd.concat([
        pd.DataFrame({"p": m.previous_price.values, "win": y.values, "cat": m.category.values,
                      "mult": mult.values, "event": m.event_ticker.values, "side": "yes"}),
        pd.DataFrame({"p": 1 - m.previous_price.values, "win": 1 - y.values, "cat": m.category.values,
                      "mult": mult.values, "event": m.event_ticker.values, "side": "no"}),
    ])
    rows["fee"] = taker_fee(rows.p, rows.mult)
    rows["ret_pre"] = (rows.win - rows.p) / rows.p
    rows["ret_taker"] = (rows.win - rows.p - rows.fee) / (rows.p + rows.fee)
    rows["pnl_c"] = 100 * (rows.win - rows.p)  # cents per contract, pre-fee
    bins = [0, .10, .20, .30, .40, .50, .60, .70, .80, .90, .95, 1.0]
    rows["bucket"] = pd.cut(rows.p, bins, right=False)
    out = table(rows)
    print(f"markets used: {len(m):,}  (both sides => {len(rows):,} contract-prices)")
    print(out.to_string())
    out.to_csv(OUT)
    ns = rows[rows.cat != "Sports"]
    print(f"\nNON-SPORTS only ({len(ns):,} contract-prices)")
    print(table(ns).to_string())
    # category split for the favourite side (p >= 0.80) and longshot side (p < 0.20)
    for name, sub in [("favourites p>=0.80", rows[rows.p >= .8]), ("longshots p<0.20", rows[rows.p < .2])]:
        c = sub.groupby("cat").agg(n=("p", "size"), avg_p=("p", "mean"), win=("win", "mean"),
                                   edge_c=("pnl_c", "mean"))
        c["se_c"] = sub.groupby("cat").pnl_c.std() / np.sqrt(c.n)
        print("\n", name); print(c[c.n >= 200].round(3).to_string())


def table(rows):
    g = rows.groupby("bucket", observed=True)
    return pd.DataFrame({
        "n": g.size(),
        "n_events": g.event.nunique(),
        "avg_price": g.p.mean(),
        "win_rate": g.win.mean(),
        "edge_cents_pre_fee": g.pnl_c.mean(),
        "se_cents": g.pnl_c.std() / np.sqrt(g.size()),
        "ret_pre_fee_pct": 100 * g.ret_pre.mean(),
        "ret_after_taker_fee_pct": 100 * g.ret_taker.mean(),
    }).round(3)


if __name__ == "__main__":
    main()
