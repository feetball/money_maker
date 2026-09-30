"""Fetch settled crypto events (post historical cutoff) + 1-min bid/ask candles
for the final ~70 minutes of each market. Disk-lean + resumable.

Outputs (under data/):
  markets_<SERIES>.csv.gz   one row per market (strike, result, expiration_value, volume...)
  candles_<SERIES>.csv.gz   ticker, ts (end of minute, unix s), yes_bid, yes_ask, volume, price_close, oi
  done_<SERIES>.txt         events whose candles are complete (for resume)
Raw responses are NOT cached (they are huge; disk is tight).
"""
import csv
import datetime as dt
import gzip
import os
import sys

import kclient as k

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)

WINDOW_MIN = {"KXBTC15M": 16, "KXETH15M": 16, "KXSOL15M": 16, "KXXRP15M": 16, "KXDOGE15M": 16}
DEFAULT_WINDOW = 70
MAX_MKTS = 100
FIELDS = ["series", "event_ticker", "cadence", "ticker", "strike_type", "floor_strike", "cap_strike",
          "open_ts", "close_ts", "result", "expiration_value", "settlement_value", "volume",
          "open_interest", "pls", "settlement_ts"]


def ts(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fetch_series(series):
    """Load the compact markets file written by _fetch_all_events."""
    out = os.path.join(DATA, f"markets_{series}.csv.gz")
    with gzip.open(out, "rt") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        for c in ("open_ts", "close_ts"):
            r[c] = int(float(r[c]))
        r["volume"] = f(r["volume"]) or 0.0
    print(series, "markets loaded from disk", len(rows), file=sys.stderr)
    return rows


def _fetch_all_events(series, min_close_ts=None):
    out = os.path.join(DATA, f"markets_{series}.csv.gz")
    if os.path.exists(out):
        return fetch_series(series)
    mrows = []
    cursor = None
    nev = 0
    while True:
        p = {"series_ticker": series, "status": "settled", "limit": 200, "with_nested_markets": "true"}
        if cursor:
            p["cursor"] = cursor
        d = k.get("/events", p, cache=False)
        evs = d.get("events") or []
        nev += len(evs)
        oldest = None
        for e in evs:
            cad = (e.get("product_metadata") or {}).get("cadence", "")
            for m in e.get("markets") or []:
                c_ts = ts(m["close_time"])
                oldest = c_ts if oldest is None else min(oldest, c_ts)
                mrows.append({
                    "series": series, "event_ticker": e["event_ticker"], "cadence": cad,
                    "ticker": m["ticker"], "strike_type": m.get("strike_type"),
                    "floor_strike": m.get("floor_strike"), "cap_strike": m.get("cap_strike"),
                    "open_ts": ts(m["open_time"]), "close_ts": c_ts, "result": m.get("result"),
                    "expiration_value": m.get("expiration_value"),
                    "settlement_value": m.get("settlement_value_dollars"),
                    "volume": f(m.get("volume_fp")) or 0.0, "open_interest": f(m.get("open_interest_fp")),
                    "pls": m.get("price_level_structure"), "settlement_ts": m.get("settlement_ts")})
        cursor = d.get("cursor")
        print(series, "events so far", nev, file=sys.stderr)
        if not cursor or not evs or (min_close_ts and oldest is not None and oldest < min_close_ts):
            break
    with gzip.open(out, "wt", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(mrows)
    print(series, "events", nev, "markets", len(mrows), file=sys.stderr)
    return mrows


def fetch_candles(series, mrows, group=1):
    """group>1: batch several consecutive single-market events (e.g. 15-min series) per request."""
    win = WINDOW_MIN.get(series, DEFAULT_WINDOW) * 60
    by_ev = {}
    for m in mrows:
        by_ev.setdefault(m["event_ticker"], []).append(m)
    done_path = os.path.join(DATA, f"done_{series}.txt")
    done = set()
    if os.path.exists(done_path):
        done = set(x.strip() for x in open(done_path) if x.strip())
    out = os.path.join(DATA, f"candles_{series}.csv.gz")
    new_file = not os.path.exists(out)
    todo = sorted((ev for ev in by_ev if ev not in done), key=lambda e: max(m["close_ts"] for m in by_ev[e]))
    print(series, "events todo", len(todo), "done", len(done), file=sys.stderr)
    n = 0
    with gzip.open(out, "at", newline="") as fh, open(done_path, "a") as dfh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(["ticker", "ts", "yes_bid", "yes_ask", "volume", "price_close", "oi"])
        i = 0
        while i < len(todo):
            evs = todo[i:i + group]
            i += group
            ms = []
            for ev in evs:
                mm = [m for m in by_ev[ev] if (m["volume"] or 0) > 0]
                mm.sort(key=lambda m: -(m["volume"] or 0))
                ms.extend(mm[:MAX_MKTS])
            if not ms:
                for ev in evs:
                    dfh.write(ev + "\n")
                continue
            close = max(m["close_ts"] for m in ms)
            start = min(m["close_ts"] for m in ms) - win
            tickers = [m["ticker"] for m in ms]
            span_min = (close - start) // 60 + 1
            chunk = max(1, min(100, 9500 // span_min))
            rows = []
            ok = True
            for j in range(0, len(tickers), chunk):
                sub = tickers[j:j + chunk]
                try:
                    d = k.get("/markets/candlesticks", {
                        "market_tickers": ",".join(sub), "start_ts": start, "end_ts": close,
                        "period_interval": 1}, cache=False)
                except RuntimeError as e:
                    print("ERR", evs, e, file=sys.stderr)
                    ok = False
                    break
                for mk in d.get("markets") or []:
                    t = mk.get("market_ticker")
                    for c in mk.get("candlesticks") or []:
                        yb = (c.get("yes_bid") or {}).get("close_dollars")
                        ya = (c.get("yes_ask") or {}).get("close_dollars")
                        pc = (c.get("price") or {}).get("close_dollars")
                        rows.append([t, c["end_period_ts"], yb, ya, c.get("volume_fp"), pc, c.get("open_interest_fp")])
            if not ok:
                continue
            w.writerows(rows)
            n += len(rows)
            fh.flush()
            for ev in evs:
                dfh.write(ev + "\n")
            dfh.flush()
            if (i // group) % 50 == 0:
                print(series, "event", i, len(todo), "candles", n, file=sys.stderr)
    print(series, "done candles", n, file=sys.stderr)


if __name__ == "__main__":
    # args: SERIES[:group] ...  ; env MIN_CLOSE (iso date) optional
    mc = os.environ.get("MIN_CLOSE")
    mc_ts = ts(mc + "T00:00:00Z") if mc else None
    for arg in sys.argv[1:]:
        s, _, g = arg.partition(":")
        mr = _fetch_all_events(s, mc_ts)
        fetch_candles(s, mr, group=int(g or 1))
