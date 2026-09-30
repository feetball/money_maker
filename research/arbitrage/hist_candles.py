"""Historical (top-of-book) frequency check using 1-minute candlesticks.

For every structure the live scanner would evaluate (ME NO-basket, ME YES-basket, strike-pair ladders, ...)
whose CURRENT top-of-book net edge is > SELECT_EDGE, pull 1-minute candles for all legs for the last
LOOKBACK_H hours and reconstruct the top-of-book at every minute boundary (candle close = last quote in that
minute; minutes without a candle carry the previous close forward).  Then count minutes / episodes with a
positive net edge after fees.  No depth information: this measures how OFTEN, not how BIG.

Runs at a low request rate (default 0.6 req/s) so it can coexist with the live scanner.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scanner as S  # noqa: E402
from kalshi_client import Kalshi  # noqa: E402

OUT = Path(__file__).resolve().parent


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback-h", type=float, default=24)
    ap.add_argument("--select-edge", type=float, default=-0.03)
    ap.add_argument("--rate", type=float, default=0.6)
    ap.add_argument("--batch", type=int, default=0, help="tickers per request (default: 10000 candles / minutes)")
    args = ap.parse_args()
    k = Kalshi(rate=args.rate, cache_dir="/tmp/claude-0/-root-money-maker/630d9deb-3b12-491b-aff2-7c62fc9cbc74/scratchpad/candle_cache")
    fees = S.Fees(k)
    events = k.paginate("/events", {"status": "open", "limit": 200, "with_nested_markets": "true", "mve_filter": "exclude"}, "events")
    fees.set_markets(events)
    mk = {m["ticker"]: m for e in events for m in e.get("markets", [])}
    lb = {t: S.book_from_listing(m) for t, m in mk.items()}
    structs = [s for s in S.discover(events, fees) + S.discover_cross(events) if s["kind"] != "SAME_MARKET" and S.risk_free(s)]
    sel = []
    for s in structs:
        te = S.top_edge(s, lb, fees)
        if te is not None and te > args.select_edge:
            sel.append(s)
    tickers = sorted({t for s in sel for t, _, _ in s["legs"]})
    print(f"selected {len(sel)} structures, {len(tickers)} tickers", flush=True)
    now = int(time.time())
    end_ts = now - now % 60
    start_ts = end_ts - int(args.lookback_h * 3600)
    candles = {}
    if not args.batch:
        args.batch = max(1, 10000 // int(args.lookback_h * 60 + 1))
    for i in range(0, len(tickers), args.batch):
        chunk = tickers[i:i + args.batch]
        d = k.get("/markets/candlesticks", {"market_tickers": ",".join(chunk), "start_ts": start_ts, "end_ts": end_ts,
                                            "period_interval": 1}, use_cache=True)
        for m in d.get("markets", []):
            t = m.get("market_ticker") or m.get("ticker")
            candles[t] = [(c["end_period_ts"], f(c["yes_bid"].get("close_dollars")), f(c["yes_ask"].get("close_dollars")))
                          for c in m.get("candlesticks", [])]
        if (i // args.batch) % 25 == 0:
            print(f"  candles {i + len(chunk)}/{len(tickers)} req={k.n_requests}", flush=True)
    # minute grid state
    grid = list(range(start_ts + 60, end_ts + 1, 60))
    state = {}
    for t in tickers:
        cs = sorted(candles.get(t, []))
        arr_b, arr_a = [], []
        j = 0
        cb = ca = None
        for g in grid:
            while j < len(cs) and cs[j][0] <= g:
                cb, ca = cs[j][1], cs[j][2]
                j += 1
            arr_b.append(cb)
            arr_a.append(ca)
        state[t] = (arr_b, arr_a)

    def book_at(t, idx):
        b, a = state[t][0][idx], state[t][1][idx]
        yes = [(b, 1.0)] if b is not None and b > 0 else []
        no = [(round(1 - a, 6), 1.0)] if a is not None and 0 < a < 1 else []
        return {"yes": yes, "no": no}

    res = []
    for s in sel:
        legs = [t for t, _, _ in s["legs"]]
        if any(t not in state for t in legs):
            continue
        pos_minutes = 0
        episodes = []
        cur = None
        best = -9
        for idx in range(len(grid)):
            books = {t: book_at(t, idx) for t in legs}
            te = S.top_edge(s, books, fees)
            if te is None:
                te = -9
            best = max(best, te)
            if te > 0:
                pos_minutes += 1
                if cur is None:
                    cur = [idx, idx, te]
                else:
                    cur[1] = idx
                    cur[2] = max(cur[2], te)
            elif cur is not None:
                episodes.append(cur)
                cur = None
        if cur is not None:
            episodes.append(cur)
        res.append({"kind": s["kind"], "event": s["event"], "series": s["series"], "risk_free": S.risk_free(s),
                    "meta": s["meta"], "legs": s["legs"], "minutes": len(grid), "pos_minutes": pos_minutes,
                    "episodes": [{"start": grid[a], "end": grid[b], "dur_min": b - a + 1, "max_edge": round(m, 4)} for a, b, m in episodes],
                    "best_edge": round(best, 4)})
    with gzip.open(OUT / "hist_candles_result.json.gz", "wt") as fh:
        json.dump({"start_ts": start_ts, "end_ts": end_ts, "n_selected": len(sel), "results": res}, fh)
    # summary
    by = defaultdict(lambda: {"structs": 0, "with_pos": 0, "episodes": 0, "pos_minutes": 0})
    for r in res:
        key = (r["kind"], r["risk_free"])
        by[key]["structs"] += 1
        by[key]["with_pos"] += 1 if r["pos_minutes"] else 0
        by[key]["episodes"] += len(r["episodes"])
        by[key]["pos_minutes"] += r["pos_minutes"]
    print(json.dumps({f"{k[0]}|rf={k[1]}": v for k, v in by.items()}, indent=1))
    for r in sorted(res, key=lambda r: -r["pos_minutes"])[:25]:
        if r["pos_minutes"]:
            print(r["kind"], r["risk_free"], r["event"], r["meta"].get("exhaustive"), "pos_min", r["pos_minutes"], "episodes", len(r["episodes"]),
                  "max", max(e["max_edge"] for e in r["episodes"]))


if __name__ == "__main__":
    main()
