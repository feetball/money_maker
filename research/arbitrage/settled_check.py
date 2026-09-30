"""Settlement sanity check: do 'mutually exclusive' events ever settle with >1 YES, do 'exhaustive' ones settle with
0 YES, and how often do markets settle at a non-binary 'fair price' (cancellations, ties)?  Uses recently settled
markets (GET /markets?status=settled) grouped by event_ticker, joined with event metadata."""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scanner as S  # noqa: E402
from kalshi_client import Kalshi  # noqa: E402

OUT = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=14)
    ap.add_argument("--rate", type=float, default=0.3)
    ap.add_argument("--max-pages", type=int, default=25)
    args = ap.parse_args()
    k = Kalshi(rate=args.rate)
    now = int(time.time())
    ms = k.paginate("/markets", {"status": "settled", "limit": 1000, "mve_filter": "exclude",
                                  "min_close_ts": now - int(args.days * 86400)}, "markets", max_pages=args.max_pages)
    print("settled markets", len(ms), "requests", k.n_requests, flush=True)
    by_ev = defaultdict(list)
    for m in ms:
        by_ev[m["event_ticker"]].append(m)
    # event metadata only where it matters: multi-market events with >1 YES, or with non-binary settlement values
    need = [et for et, lst in by_ev.items() if len(lst) >= 2 and (
        sum(1 for m in lst if m.get("result") == "yes") > 1 or
        any(S.f(m.get("settlement_value_dollars"), 0) not in (0.0, 1.0) for m in lst))]
    print("events needing metadata:", len(need), flush=True)
    meta = {}
    for et in need[:400]:
        try:
            d = k.get(f"/events/{et}")
            meta[et] = d.get("event", d)
        except Exception as ex:  # noqa: BLE001
            meta[et] = {"err": str(ex)}
    closes = sorted(m.get("close_time") or "" for m in ms)
    print("close_time range:", closes[0] if closes else None, closes[-1] if closes else None)
    sv = Counter()
    nonbinary = []
    for m in ms:
        v = S.f(m.get("settlement_value_dollars"), None)
        key = "1" if v == 1 else "0" if v == 0 else "other"
        sv[(m.get("result"), key)] += 1
        if key == "other":
            nonbinary.append((m["ticker"], m.get("result"), m.get("settlement_value_dollars"), m.get("yes_sub_title")))
    print("result/settlement_value:", dict(sv))
    print("non-binary settlements (sample):", nonbinary[:15])
    stats = Counter()
    bad = []
    for et, lst in by_ev.items():
        if et not in meta:
            n_yes = sum(1 for m in lst if m.get("result") == "yes")
            stats[("multi" if len(lst) > 1 else "single", "n_yes=" + ("0" if n_yes == 0 else "1" if n_yes == 1 else ">1"))] += 1
            continue
        e = meta[et]
        me = e.get("mutually_exclusive")
        n_yes = sum(1 for m in lst if m.get("result") == "yes")
        vals = [S.f(m.get("settlement_value_dollars"), 0) for m in lst]
        tot = sum(vals)
        ek = S.exhaustive_kind(lst) if me else None
        stats[(f"ME={me}", ek, "n_yes=" + ("0" if n_yes == 0 else "1" if n_yes == 1 else ">1"))] += 1
        if me and (n_yes > 1 or (tot > 1.0001)):
            bad.append((et, n_yes, round(tot, 4), [(m["ticker"], m.get("result"), m.get("settlement_value_dollars")) for m in lst][:6]))
        if me and ek in ("numeric_partition", "game_tie") and abs(tot - 1) > 1e-4:
            bad.append((et, "EXHAUSTIVE_BUT_SUM", round(tot, 4), [(m["ticker"], m.get("result"), m.get("settlement_value_dollars")) for m in lst][:8]))
    for kk, v in sorted(stats.items(), key=lambda x: -x[1]):
        print(v, kk)
    print("violations / anomalies:", len(bad))
    for b in bad[:30]:
        print("  ", b)
    with gzip.open(OUT / "settled_check.json.gz", "wt") as fh:
        json.dump({"n_markets": len(ms), "sv": {str(k): v for k, v in sv.items()}, "nonbinary": nonbinary,
                   "stats": {str(k): v for k, v in stats.items()}, "anomalies": bad}, fh)


if __name__ == "__main__":
    main()
