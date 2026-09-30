"""Build the hourly walk-forward decision panel -> panel_hourly.parquet (+ universe.parquet, series_flags.csv).

    uv run --with pandas --with pyarrow --with numpy python build_panel.py
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd

from calib_lib import HERE, PANEL_FILE, attach_market_cols, build_panel, load_candles_all, series_flags, universe

t0 = time.time()
mk = universe()
series_flags(mk).to_csv(HERE / "series_flags.csv")
c = load_candles_all()
mk["has_candles"] = mk["ticker"].isin(set(c["ticker"].unique()))
g = mk.groupby("event_ticker")["has_candles"]
mk["event_all_candles"] = g.transform("all")
mk.drop(columns=["tags", "price_ranges"], errors="ignore").to_parquet(HERE / "universe.parquet", index=False)
print(f"markets {len(mk)}, in_scope {mk.in_scope.sum()}, with candles {mk.has_candles.sum()}, "
      f"in_scope&candles {(mk.in_scope & mk.has_candles).sum()}  ({time.time()-t0:.0f}s)")
print("in-scope markets with candles, by source x event_all_candles:")
print(mk[mk.in_scope & mk.has_candles].groupby(["source", "event_all_candles"]).size())

use = mk[mk.in_scope & mk.has_candles]
p = build_panel(use, c)
print(f"panel rows {len(p)}  ({time.time()-t0:.0f}s)")
p = attach_market_cols(p, use)
for col in ["bid", "ask", "price_close", "price_previous", "fut_low", "fut_high", "fee_multiplier", "min_tick"]:
    p[col] = p[col].astype("float32")
for col in ["h_to_eet", "h_since_open", "open_interest", "cumvol"]:
    p[col] = p[col].astype("float32")
for col in ["ticker", "event_ticker", "series_ticker", "category", "fee_type", "source", "month"]:
    p[col] = p[col].astype("category")
p = p.drop(columns=["open_time"])
p.to_parquet(PANEL_FILE, index=False, compression="zstd")
print(f"wrote {PANEL_FILE} {len(p)} rows, {p.ticker.nunique()} markets, {p.event_ticker.nunique()} events "
      f"({time.time()-t0:.0f}s)")
