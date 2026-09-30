"""How far back do candles go? Binary-search the earliest non-empty 300-candle window per product/granularity.

Also: gap density of 1-minute candles (minutes with no trades are omitted), AT candle history,
and the effect of a `Cache-Control: no-cache` request header on CDN caching.
"""
import datetime as dt
import time

from cbprobe import AT, EXCH, _client, get

UTC = dt.timezone.utc
LO = int(dt.datetime(2014, 12, 1, tzinfo=UTC).timestamp())
NOW = int(time.time())


def ex_window(pid, g, start):
    end = start + 299 * g
    r = get(f"{EXCH}/products/{pid}/candles", {"granularity": g, "start": dt.datetime.fromtimestamp(start, UTC).isoformat(),
                                               "end": dt.datetime.fromtimestamp(end, UTC).isoformat()})
    if r.status_code != 200:
        return None
    return r.json()


def earliest(pid, g):
    lo, hi = LO, NOW
    # find smallest start whose 300-candle window has data (assumes data continuous once listed)
    while hi - lo > 300 * g:
        mid = (lo + hi) // 2
        c = ex_window(pid, g, mid)
        if c:
            hi = mid
        else:
            lo = mid
    c = ex_window(pid, g, lo) or ex_window(pid, g, hi) or []
    return min(x[0] for x in c) if c else None


for pid in ("BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "HYPE-USD"):
    out = []
    for g in (86400, 3600, 60):
        e = earliest(pid, g)
        out.append(f"g={g}: {dt.datetime.fromtimestamp(e, UTC):%Y-%m-%d %H:%M}" if e else f"g={g}: none")
    print(pid, " | ".join(out))

# gap density: 1-minute candles present out of 300 in a few windows
for pid, when in (("BTC-USD", "2015-06-01"), ("BTC-USD", "2020-06-01"), ("BTC-USD", "2026-09-20"),
                  ("SOL-USD", "2026-09-20"), ("AERO-USD", "2026-09-20"), ("DASH-USD", "2026-09-20")):
    s = int(dt.datetime.fromisoformat(when + "T12:00:00+00:00").timestamp())
    c = ex_window(pid, 60, s) or []
    print(f"1m density {pid} {when}: {len(c)}/300 candles present")

# Advanced Trade candles: same history?
for pid in ("BTC-USD",):
    s = int(dt.datetime(2015, 7, 20, tzinfo=UTC).timestamp())
    r = get(f"{AT}/market/products/{pid}/candles", {"start": s, "end": s + 86400 * 30, "granularity": "ONE_DAY"})
    c = r.json().get("candles", []) if r.status_code == 200 else r.text[:200]
    print("AT ONE_DAY from 2015-07-20:", r.status_code, len(c) if isinstance(c, list) else c,
          (c[-1]["start"], c[0]["start"]) if isinstance(c, list) and c else "")
    s = int(dt.datetime(2016, 1, 5, tzinfo=UTC).timestamp())
    r = get(f"{AT}/market/products/{pid}/candles", {"start": s, "end": s + 60 * 300, "granularity": "ONE_MINUTE"})
    c = r.json().get("candles", []) if r.status_code == 200 else r.text[:200]
    print("AT ONE_MINUTE 2016-01-05:", r.status_code, len(c) if isinstance(c, list) else c)

# Cache-Control: no-cache request header
for url, params in ((f"{AT}/market/product_book", {"product_id": "BTC-USD", "limit": 1}),
                    (f"{EXCH}/products/BTC-USD/book", {"level": 1}),
                    (f"{EXCH}/products/BTC-USD/candles", {"granularity": 60})):
    for hdr in ({}, {"Cache-Control": "no-cache"}):
        time.sleep(0.5)
        r = _client.get(url, params=params, headers=hdr)
        print("no-cache test", url.split(".com")[1][:40], hdr or "(none)", r.status_code, "cf=", r.headers.get("cf-cache-status"),
              "age=", r.headers.get("age"), "cc=", r.headers.get("cache-control"))
