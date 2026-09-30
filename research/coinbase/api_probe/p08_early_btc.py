"""Spot-check early BTC-USD history: are 1m/1h candles present between 2015-01 and 2015-07?"""
import datetime as dt
from cbprobe import EXCH, get
UTC = dt.timezone.utc
for day in ("2015-01-21", "2015-02-15", "2015-04-01", "2015-06-01", "2015-07-19", "2015-07-21"):
    s = dt.datetime.fromisoformat(day + "T00:00:00+00:00")
    for g, span in ((60, 299), (3600, 299), (86400, 10)):
        e = s + dt.timedelta(seconds=g * span)
        r = get(f"{EXCH}/products/BTC-USD/candles", {"granularity": g, "start": s.isoformat(), "end": e.isoformat()})
        c = r.json() if r.status_code == 200 else []
        print(day, f"g={g}", r.status_code, len(c), (dt.datetime.fromtimestamp(c[-1][0], UTC).isoformat(), c[-1]) if c else "")
