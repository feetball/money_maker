"""Fix the chronological train/test split BEFORE looking at any strategy result.
Split = the UTC midnight closest to the 60th percentile of decision-grid rows (clean events, hourly grid),
so ~60% of the decision opportunities fall in TRAIN and ~40% in TEST. Writes split.json."""
import json

import numpy as np
import pandas as pd

from calib_lib import HERE, PANEL_FILE

p = pd.read_parquet(PANEL_FILE, columns=["t", "event_ticker", "event_all_candles"])
p = p[p["event_all_candles"]]
q = np.quantile(p["t"].to_numpy(), 0.60)
T = int(round(q / 86400) * 86400)
ev_first = p.groupby("event_ticker", observed=True)["t"].min()
out = {"split_ts": T, "split": str(pd.Timestamp(T, unit="s", tz="UTC")),
       "share_rows_before": float((p["t"] < T).mean()), "share_events_first_before": float((ev_first < T).mean()),
       "first": str(pd.Timestamp(p["t"].min(), unit="s", tz="UTC")), "last": str(pd.Timestamp(p["t"].max(), unit="s", tz="UTC"))}
(HERE / "split.json").write_text(json.dumps(out, indent=1))
print(out)
