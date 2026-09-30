"""Independent spot-check of panel_hourly.parquet (no look-ahead, correct quote, correct outcome, open-market rule).
For 300 random markets: recompute, for every panel row, the last candle with end_period_ts <= t directly from the
raw candle files and compare bid/ask; check open_time <= t < close_time; check y against markets.parquet; check
fut_low/fut_high only use candles ending strictly after t.
    uv run --with pandas --with pyarrow --with numpy python verify_panel.py   -> logs/verify_panel.txt"""
import numpy as np
import pandas as pd

from calib_lib import HERE, PANEL_FILE, load_candles_all
from loader import load_markets

rng = np.random.default_rng(1)
p = pd.read_parquet(PANEL_FILE, columns=["ticker", "t", "bid", "ask", "y", "close_time", "fut_low", "fut_high", "h_to_eet",
                                          "expected_expiration_time"])
tick = rng.choice(p["ticker"].astype(str).unique(), 300, replace=False)
p = p[p["ticker"].astype(str).isin(tick)].copy()
p["ticker"] = p["ticker"].astype(str)
c = load_candles_all()
c = c[c["ticker"].isin(tick)]
mk = load_markets().set_index("ticker")
bad_q = bad_open = bad_y = bad_fut = n = 0
for t_, g in p.groupby("ticker"):
    cc = c[c["ticker"] == t_].sort_values("end_period_ts")
    ends = cc["end_period_ts"].to_numpy()
    m = mk.loc[t_]
    for r in g.itertuples():
        n += 1
        i = np.searchsorted(ends, r.t, side="right") - 1          # last candle ended <= t
        if i < 0 or abs(cc["yes_bid_close"].iloc[i] - r.bid) > 1e-6 or abs(cc["yes_ask_close"].iloc[i] - r.ask) > 1e-6:
            bad_q += 1
        if not (m["open_time"].timestamp() <= r.t < m["close_time"].timestamp()):
            bad_open += 1
        if int(m["y"]) != int(r.y):
            bad_y += 1
        later = cc[cc["end_period_ts"] > r.t]["price_low"].dropna()
        exp = later.min() if len(later) else np.nan
        if not ((np.isnan(exp) and np.isnan(r.fut_low)) or abs(exp - r.fut_low) < 1e-6):
            bad_fut += 1
eet_h = ((p["expected_expiration_time"] - pd.to_datetime(p["t"], unit="s", utc=True)).dt.total_seconds() / 3600 - p["h_to_eet"]).abs().max()
out = (f"checked {n} panel rows of {len(tick)} random markets\n  quote mismatches (vs last candle ended <= t): {bad_q}\n"
       f"  rows outside open_time <= t < close_time: {bad_open}\n  outcome mismatches: {bad_y}\n"
       f"  fut_low mismatches (min trade price over candles ending AFTER t): {bad_fut}\n  max |h_to_eet error| (h): {eet_h:.2e}\n")
print(out)
(HERE / "logs" / "verify_panel.txt").write_text(out)
