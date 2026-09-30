"""Aggregate scanner output (scans_*.jsonl, opps_*.jsonl, track_*.jsonl) into frequency / size / persistence stats.

Episode = maximal run of observations (full scans every ~170 s + 10 s re-polls in between) in which the same
structure (same legs) shows positive profit after fees and per-order cent rounding.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

OUT = Path(__file__).resolve().parent


def ts(x):
    return datetime.fromisoformat(x).timestamp()


def _lines(name):
    import gzip
    p = OUT / name
    if p.exists():
        return [json.loads(l) for l in open(p)]
    pg = OUT / (name + ".gz")
    if pg.exists():
        return [json.loads(l) for l in gzip.open(pg, "rt")]
    return []


def load(tag):
    return _lines(f"scans_{tag}.jsonl"), _lines(f"opps_{tag}.jsonl"), _lines(f"track_{tag}.jsonl")


def cls(o):
    if o["risk_free"]:
        return "risk_free"
    if o["kind"] == "ME_YES_BASKET":
        return "not_rf:" + str(o["meta"].get("exhaustive"))
    return "not_rf:" + o["kind"]


def main(tag="main"):
    scans, opps, track = load(tag)
    scans = sorted(scans, key=lambda s: s["ts"])
    scan_times = [ts(s["ts"]) for s in scans]
    t0, t1 = scan_times[0], scan_times[-1]
    span_h = max((t1 - t0) / 3600, 1e-9)
    print(f"scans={len(scans)} span={span_h*60:.1f} min  events/scan~{scans[-1]['n_events']} active markets~{scans[-1]['n_active']}")
    # observations per key
    obs = defaultdict(list)  # key -> [(t, profit, units, src)]
    info = {}
    # identical leg sets reported under several kinds (e.g. a k=1 tile basket == a cross-event ladder) are one opportunity
    legkey = {}
    for o in opps:
        lk = tuple(sorted(f"{t}:{sd}" for t, sd, _ in o["struct_legs"]))
        canon = legkey.setdefault(lk, o["key"])
        if canon != o["key"]:
            continue
        obs[o["key"]].append((ts(o["ts"]), o["profit"], o["units"], "scan", o["scan_id"]))
        info.setdefault(o["key"], o)
    for r in track:
        if r["key"] not in info:
            continue
        obs[r["key"]].append((ts(r["ts"]), r["profit"], r["units"], "track", None))
    scan_ids = [s["scan_id"] for s in scans]
    in_scan = defaultdict(set)
    for o in opps:
        in_scan[o["key"]].add(o["scan_id"])
    episodes = []
    for key, lst in obs.items():
        lst.sort()
        cur = None
        for t, p, u, src, sid in lst:
            if p > 0:
                if cur is None:
                    cur = {"key": key, "start": t, "last_pos": t, "max_profit": p, "first_profit": p, "n_obs": 1, "end": None}
                else:
                    cur["last_pos"] = t
                    cur["max_profit"] = max(cur["max_profit"], p)
                    cur["n_obs"] += 1
            else:
                if cur is not None:
                    cur["end"] = t
                    episodes.append(cur)
                    cur = None
        if cur is not None:
            # still positive at last observation: censored unless a later scan did not contain it
            later = [st for st, sid in zip(scan_times, scan_ids) if st > cur["last_pos"] + 1]
            if later and scan_ids[scan_times.index(later[0])] not in in_scan[key]:
                cur["end"] = later[0]
            episodes.append(cur)
    for e in episodes:
        o = info[e["key"]]
        e.update({"class": cls(o), "kind": o["kind"], "event": o["event"], "title": o.get("title"), "exhaustive": o["meta"].get("exhaustive"),
                  "units": o["units"], "cost": o["cost"], "fees": o["fees"], "fee_mult": o.get("fee_mult"),
                  "legs": len(o["struct_legs"]), "exp": max((l.get("exp") or "") for l in o["legs_meta"]),
                  "censored": e["end"] is None,
                  "dur_s_lower": e["last_pos"] - e["start"],
                  "dur_s_upper": (e["end"] - e["start"]) if e["end"] else None,
                  "at_first_scan": abs(e["start"] - t0) < 120})
    # summaries by class
    by = defaultdict(list)
    for e in episodes:
        by[e["class"]].append(e)
    summary = {"tag": tag, "n_scans": len(scans), "span_min": round(span_h * 60, 1), "classes": {}}
    for c, es in sorted(by.items()):
        new = [e for e in es if not e["at_first_scan"]]
        prof = sorted(e["max_profit"] for e in es)
        med = prof[len(prof) // 2] if prof else 0
        durs = sorted(e["dur_s_lower"] for e in es if not e["censored"])
        summary["classes"][c] = {
            "episodes": len(es), "new_after_first_scan": len(new),
            "new_per_hour": round(len(new) / span_h, 2),
            "sum_max_profit_all": round(sum(prof), 2), "median_profit": med, "max_profit": prof[-1] if prof else 0,
            "sum_profit_new": round(sum(e["max_profit"] for e in new), 2),
            "est_profit_per_day_from_new": round(sum(e["max_profit"] for e in new) / span_h * 24, 2),
            "median_dur_s_closed": durs[len(durs) // 2] if durs else None, "n_closed": len(durs),
            "n_censored": sum(1 for e in es if e["censored"]),
        }
    print(json.dumps(summary, indent=1))
    rf = sorted([e for e in episodes if e["class"] == "risk_free"], key=lambda e: -e["max_profit"])
    print("\nRISK-FREE episodes:")
    for e in rf:
        print(f"  {e['kind']:14s} {e['event']:38s} maxP=${e['max_profit']:<7} firstP=${e['first_profit']:<7} units={e['units']:<9} cost=${e['cost']:<9} "
              f"fees=${e['fees']:<6} fm={e['fee_mult']} dur>={e['dur_s_lower']:.0f}s end={'open' if e['censored'] else round(e['dur_s_upper'] or 0)} exp={e['exp'][:10]} ex={e['exhaustive']}")
    # scan-level
    print("\nper-scan risk-free opps/profit:")
    for s in scans:
        print(f"  {s['scan_id']}  rf_opps={s['n_opps_riskfree']:3d}  rf_profit=${s['profit_riskfree']:<8} cands={s['n_candidates']} listing={s['sec_listing']}s")
    # near-miss quantiles from last scan
    print("\nedge quantiles (top-of-book, per unit, after exact fees) last scan:")
    for k, v in scans[-1]["edge_quantiles"].items():
        print("  ", k, v)
    json.dump({"summary": summary, "episodes": episodes}, open(OUT / f"analysis_{tag}.json", "w"), indent=1, default=str)


if __name__ == "__main__":
    main(*(sys.argv[1:] or ["main"]))
