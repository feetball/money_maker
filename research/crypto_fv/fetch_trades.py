"""Sample the trade tape for a random subset of settled events to measure aggregate taker PnL
(= gross maker edge) by time-to-expiry and price. Rate-limited separately (2 req/s).
Usage: fetch_trades.py SERIES N_EVENTS TICKERS_PER_EVENT
Output: data/trades_<SERIES>.csv.gz
"""
import csv
import gzip
import os
import random
import sys
import time

import pandas as pd

import kclient as k

k.MIN_INTERVAL = 0.5
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


def main(series, n_ev, n_tk):
    m = pd.read_csv(os.path.join(DATA, f"markets_{series}.csv.gz"))
    m = m[m.result.isin(["yes", "no"]) & (m.cadence == "hourly") if "cadence" in m else m.result.isin(["yes", "no"])]
    evs = sorted(m.event_ticker.unique())
    random.seed(7)
    pick = random.sample(evs, min(n_ev, len(evs)))
    out = os.path.join(DATA, f"trades_{series}.csv.gz")
    with gzip.open(out, "wt", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ticker", "event_ticker", "created_time", "yes_price", "count", "taker_side"])
        for i, ev in enumerate(pick):
            g = m[m.event_ticker == ev].sort_values("volume", ascending=False).head(n_tk)
            for r in g.itertuples():
                cursor = None
                while True:
                    prm = {"ticker": r.ticker, "limit": 1000, "min_ts": int(r.close_ts - 3700), "max_ts": int(r.close_ts)}
                    if cursor:
                        prm["cursor"] = cursor
                    d = k.get("/markets/trades", prm, cache=False)
                    for t in d.get("trades") or []:
                        w.writerow([r.ticker, ev, t["created_time"], t.get("yes_price_dollars"), t.get("count_fp"),
                                    t.get("taker_side")])
                    cursor = d.get("cursor")
                    if not cursor:
                        break
            if i % 10 == 0:
                print(series, "trades events", i, len(pick), file=sys.stderr)
                fh.flush()


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
