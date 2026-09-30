"""Kalshi structural-arbitrage scanner (read-only, public market data).

Checks every open, non-MVE event for RISK-FREE structures, after taker fees, with full order-book depth:

  A. ME_NO_BASKET   mutually_exclusive events: buy NO on a subset S (|S|>=2) of markets.
                    At most one YES resolves -> payout >= |S|-1 per basket.  Needs no exhaustiveness.
  B. ME_YES_BASKET  mutually_exclusive events: buy YES on every active market; pays 1 iff exactly one
                    resolves YES -> only risk-free when the listed outcomes are exhaustive.
                    Exhaustiveness is classified (numeric_partition / catchall / two_way / unknown).
  C. PAIR_*         numeric-strike markets in the same event are mapped to interval sets on the real line.
                    For every pair (A,B):
                      A subset-of B  (ladder / monotonicity)  -> buy YES(B)+NO(A), payout >= 1
                      A,B disjoint                            -> buy NO(A)+NO(B),  payout >= 1
                      A union B = R  (complementary tails)    -> buy YES(A)+YES(B), payout >= 1
                    Plus "time ladders" ("Before <date>" markets in one event): YES(later)+NO(earlier).
  D. SAME_MARKET    YES_ask + NO_ask < 1 on one market (crossed book; should never happen).

Pipeline per scan: 1 paginated /events listing (nested markets carry an accurate top-of-book) ->
top-of-book pre-screen (depth can only make an arb worse) -> batch /markets/orderbooks (100 tickers/call)
for every structure whose top-of-book net edge > PRESCREEN -> depth walk with fees -> JSONL output.
Between full scans, tickers of live opportunities are re-polled every TRACK_EVERY seconds to measure
persistence.

Fees (taker, per Kalshi fee schedule + docs.kalshi.com/getting_started/fee_rounding):
  fee = 0.07 * fee_multiplier(series) * C * P * (1-P); per ORDER the account debit (cost+fee) is
  rounded UP to the cent for non-direct members (accumulator per order).  We charge that rounding
  per leg (one order per leg).
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kalshi_client import Kalshi  # noqa: E402

OUT = Path(__file__).resolve().parent
RAW = OUT / "raw"
SNAP = RAW / "snapshots"
TAKER = 0.07
EPS = 1e-9
CROSS = True  # also scan cross-event (same underlying, same settlement instant) structures
PRESCREEN = -0.01  # fetch books for structures whose top-of-book net edge per unit > this

CATCHALL = re.compile(
    r"\b(other|others|none|no one|nobody|tie|ties|draw|field|not listed|any other|no winner|neither|"
    r"someone else|anyone else|no contest|no other)\b",
    re.I,
)
BEFORE = re.compile(r"^\s*(before|by)\b", re.I)


def now_utc():
    return datetime.now(timezone.utc)


def iso(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def ceil_cent(x):
    return math.ceil(round(x * 100, 6)) / 100.0


# ----------------------------------------------------------------------------------------------
# Fees
# ----------------------------------------------------------------------------------------------
class Fees:
    def __init__(self, k: Kalshi, refresh_s=6 * 3600):
        p = RAW / "series_all.json.gz"
        if not p.exists() or time.time() - p.stat().st_mtime > refresh_s:
            d = k.get("/series")
            with gzip.open(p, "wt") as fh:
                json.dump(d, fh)
        with gzip.open(p, "rt") as fh:
            d = json.load(fh)
        self.mult = {}
        self.ftype = {}
        for s in d.get("series", []):
            self.mult[s["ticker"]] = f(s.get("fee_multiplier"), 1.0)
            self.ftype[s["ticker"]] = s.get("fee_type")

    def set_markets(self, events):
        """Per-market multiplier: the event-level fee_multiplier_override (e.g. MLB markets go from the series'
        0.5 to 1.0 at first pitch) takes precedence over the series fee_multiplier."""
        self.tmult = {}
        self.n_override = 0
        for e in events:
            em = e.get("fee_multiplier_override")
            sm = self.mult.get(e.get("series_ticker"), 1.0)
            if em is not None:
                self.n_override += 1
            for m in e.get("markets", []):
                self.tmult[m["ticker"]] = f(em, sm) if em is not None else sm

    def mult_for(self, ticker, series=None):
        tm = getattr(self, "tmult", {})
        if ticker in tm:
            return tm[ticker]
        return self.mult.get(series, 1.0)

    def per_contract(self, ticker, price, series=None):
        return TAKER * self.mult_for(ticker, series) * price * (1 - price)


# ----------------------------------------------------------------------------------------------
# Books.  book[ticker] = {"yes": [(p,s) desc], "no": [(p,s) desc]}  (bids only)
# Buying YES consumes NO bids at price 1-q; buying NO consumes YES bids at price 1-p.
# ----------------------------------------------------------------------------------------------
def book_from_listing(m):
    yes, no = [], []
    if f(m.get("yes_bid_size_fp")) > 0 and f(m.get("yes_bid_dollars")) > 0:
        yes.append((f(m["yes_bid_dollars"]), f(m["yes_bid_size_fp"])))
    if f(m.get("yes_ask_size_fp")) > 0 and 0 < f(m.get("yes_ask_dollars")) < 1:
        no.append((round(1 - f(m["yes_ask_dollars"]), 6), f(m["yes_ask_size_fp"])))
    return {"yes": yes, "no": no}


def book_from_api(ob):
    yes = [(f(p), f(s)) for p, s in (ob.get("yes_dollars") or []) if f(s) > 0]
    no = [(f(p), f(s)) for p, s in (ob.get("no_dollars") or []) if f(s) > 0]
    yes.sort(key=lambda x: -x[0])
    no.sort(key=lambda x: -x[0])
    return {"yes": yes, "no": no}


def asks(book, side):
    """Ask ladder (price, size) ascending for buying `side`."""
    opp = book["no"] if side == "yes" else book["yes"]
    return [(round(1 - p, 6), s) for p, s in opp]


# ----------------------------------------------------------------------------------------------
# Depth walks
# ----------------------------------------------------------------------------------------------
def walk_fixed(legs, payout, books, fees, max_units=1e9):
    """legs: list of (ticker, side, series). Buy equal quantity of every leg.  Guaranteed payout per unit.
    Returns dict with units, gross cost, fees, per-leg fills, profit after per-order cent rounding."""
    ladders = [list(asks(books[t], side)) for t, side, _ in legs]
    idx = [0] * len(legs)
    rem = [lad[0][1] if lad else 0 for lad in ladders]
    fills = [[] for _ in legs]
    units = 0.0
    first_unit_edge = None
    while units < max_units:
        if any(idx[i] >= len(ladders[i]) for i in range(len(legs))):
            break
        prices = [ladders[i][idx[i]][0] for i in range(len(legs))]
        fee_u = sum(fees.per_contract(legs[i][0], prices[i], legs[i][2]) for i in range(len(legs)))
        edge = payout - sum(prices) - fee_u
        if first_unit_edge is None:
            first_unit_edge = edge
        if edge <= EPS:
            break
        q = min(min(rem), max_units - units)
        for i in range(len(legs)):
            fills[i].append((prices[i], q))
            rem[i] -= q
            if rem[i] <= EPS:
                idx[i] += 1
                rem[i] = ladders[i][idx[i]][1] if idx[i] < len(ladders[i]) else 0
        units += q
    return finalize(legs, fills, units, payout_fn=lambda u: payout * u, fees=fees, first_edge=first_unit_edge)


def walk_no_basket(legs, books, fees):
    """ME NO-basket with dynamic subset: each basket unit buys 1 NO on every market whose current NO ask
    has positive contribution (yes_bid - fee > 0); unit payout >= |S|-1."""
    n = len(legs)
    ladders = [list(asks(books[t], "no")) for t, _, _ in legs]
    idx = [0] * n
    rem = [lad[0][1] if lad else 0 for lad in ladders]
    fills = [[] for _ in legs]
    units = 0.0
    payout_total = 0.0
    first_edge = None
    while True:
        S = []
        for i in range(n):
            if idx[i] < len(ladders[i]):
                p = ladders[i][idx[i]][0]
                contrib = (1 - p) - fees.per_contract(legs[i][0], p, legs[i][2])
                if contrib > EPS:
                    S.append((i, p, contrib))
        if len(S) < 2:
            break
        edge = sum(c for _, _, c in S) - 1
        if first_edge is None:
            first_edge = edge
        if edge <= EPS:
            break
        q = min(rem[i] for i, _, _ in S)
        for i, p, _ in S:
            fills[i].append((p, q))
            rem[i] -= q
            if rem[i] <= EPS:
                idx[i] += 1
                rem[i] = ladders[i][idx[i]][1] if idx[i] < len(ladders[i]) else 0
        units += q
        payout_total += (len(S) - 1) * q
    if first_edge is None:
        # report top-of-book edge even if <2 contributors
        tops = []
        for i in range(n):
            if ladders[i]:
                p = ladders[i][0][0]
                tops.append((1 - p) - fees.per_contract(legs[i][0], p, legs[i][2]))
        first_edge = sum(c for c in tops if c > 0) - 1 if tops else -1
    return finalize(legs, fills, units, payout_fn=lambda u: payout_total, fees=fees, first_edge=first_edge)


def finalize(legs, fills, units, payout_fn, fees, first_edge):
    cost = 0.0
    fee_exact = 0.0
    debit_rounded = 0.0
    leg_out = []
    for (t, side, series), fl in zip(legs, fills):
        if not fl:
            continue
        c = sum(p * q for p, q in fl)
        fe = sum(fees.per_contract(t, p, series) * q for p, q in fl)
        cost += c
        fee_exact += fe
        debit_rounded += ceil_cent(c + fe)
        leg_out.append({"ticker": t, "side": side, "qty": round(sum(q for _, q in fl), 4),
                        "worst_px": max(p for p, _ in fl), "best_px": min(p for p, _ in fl), "cost": round(c, 4)})
    payout = payout_fn(units) if units > 0 else 0.0
    return {
        "units": round(units, 4),
        "payout": round(payout, 4),
        "cost": round(cost, 4),
        "fees": round(fee_exact, 4),
        "rounding": round(debit_rounded - cost - fee_exact, 4),
        "profit": round(payout - debit_rounded, 4) if units > 0 else 0.0,
        "profit_exact_fees": round(payout - cost - fee_exact, 4) if units > 0 else 0.0,
        "top_edge": round(first_edge, 5) if first_edge is not None else None,
        "legs": leg_out,
    }


# ----------------------------------------------------------------------------------------------
# Structure discovery
# ----------------------------------------------------------------------------------------------
def interval(m):
    """Return (lo, lo_closed, hi, hi_closed) for numeric-strike markets, else None."""
    st = m.get("strike_type")
    fl, cp = m.get("floor_strike"), m.get("cap_strike")
    inf = math.inf
    # Kalshi sometimes fills cap_strike on plain thresholds (cap == floor, or cap == 0), so trust strike_type -- except
    # that some "exactly N" markets are mislabelled strike_type=less/greater with floor == cap == N (rules say "exactly").
    if st in ("less", "greater", "less_or_equal", "greater_or_equal", "structured") and fl is not None and cp is not None \
            and re.search(r"\bexactly\b", m.get("rules_primary") or "", re.I):
        return (fl, True, cp, True)
    if st == "greater" and fl is not None:
        return (fl, False, inf, False)
    if st == "greater_or_equal" and fl is not None:
        return (fl, True, inf, False)
    if st == "less" and cp is not None:
        return (-inf, False, cp, False)
    if st == "less_or_equal" and cp is not None:
        return (-inf, False, cp, True)
    if st == "between" and fl is not None and cp is not None:
        return (fl, True, cp, True)
    if st == "structured" and fl is not None and cp is None:
        return (fl, False, inf, False)  # e.g. floor 29.5 -> "30+"
    if st == "structured" and cp is not None and fl is None:
        return (-inf, False, cp, False)
    return None


PATH = re.compile(r"\b(by|before)\s+(the end of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d|"
                  r"\b(at any (point|time)|any time|ever|reach(es|ed)?|hits?|touch(es)?)\b", re.I)


def path_dependent(m):
    """'X above 6.20 BY Dec 31' is a first-passage event, not a terminal value: same-direction ladders stay monotone
    but opposite-direction pairs (hit-high vs hit-low) are NOT disjoint/complementary."""
    return bool(PATH.search(m.get("rules_primary") or ""))


def subset(a, b):
    """interval a subset-of interval b."""
    alo, alc, ahi, ahc = a
    blo, blc, bhi, bhc = b
    lo_ok = alo > blo or (alo == blo and (blc or not alc))
    hi_ok = ahi < bhi or (ahi == bhi and (bhc or not ahc))
    return lo_ok and hi_ok


def disjoint(a, b):
    alo, alc, ahi, ahc = a
    blo, blc, bhi, bhc = b
    if ahi < blo or (ahi == blo and not (ahc and blc)):
        return True
    if bhi < alo or (bhi == alo and not (bhc and alc)):
        return True
    return False


def covers_all(a, b):
    """a union b == R (two tails that overlap/touch)."""
    for x, y in ((a, b), (b, a)):
        if x[0] == -math.inf and y[2] == math.inf:
            # x = (-inf, c], y = [f, inf)
            c, cc = x[2], x[3]
            fl, fc = y[0], y[1]
            if fl < c or (fl == c and (cc or fc)):
                return True
    return False


def exhaustive_kind(ms):
    """Classify whether the listed outcomes of a mutually-exclusive event are exhaustive.
    numeric_partition : strike intervals with both open tails and no material gaps -> exhaustive by construction
    game_tie          : sports game/period result with an explicit Tie/Draw market (cancel -> fair-price rule)
    game_two_way      : 2-outcome sports game (exhaustive only if the sport cannot tie; check rules)
    catchall_other    : has an Other/None/No-one market (can still fail: e.g. 'nobody by deadline')
    tie_nongame       : has a 'Tie' market but not a game (e.g. Grammys before nominations) -> NOT exhaustive
    text_partition / numeric_gappy / two_way / unknown -> NOT exhaustive"""
    ivs = [interval(m) for m in ms]
    if all(iv is not None for iv in ivs) and len(ivs) >= 2:
        ivs = sorted(ivs, key=lambda x: (x[0], x[2]))
        if ivs[0][0] == -math.inf and ivs[-1][2] == math.inf:
            gaps = [ivs[i + 1][0] - ivs[i][2] for i in range(len(ivs) - 1)]
            widths = [iv[2] - iv[0] for iv in ivs if math.isfinite(iv[0]) and math.isfinite(iv[2])]
            wmin = min(widths) if widths else 1.0
            if all(g <= max(0.011, 0.26 * wmin) + 1e-9 and g >= -1e-9 for g in gaps):
                return "numeric_partition"
        return "numeric_gappy"
    subs = [(m.get("yes_sub_title") or "") for m in ms]
    rules = " ".join((m.get("rules_primary") or "") + " " + (m.get("rules_secondary") or "") for m in ms[:2]).lower()
    is_game = bool(re.search(r"\b(game|match|innings?|half|quarter|period|fight|bout)\b", rules))
    has_tie = any(re.search(r"\b(tie|draw)\b", x, re.I) for x in subs)
    if has_tie and is_game:
        return "game_tie"
    if any(re.search(r"\b(other|others|none|no one|nobody|field|not listed|any other|no winner|neither|someone else|anyone else|no other)\b", x, re.I) for x in subs):
        return "catchall_other"
    if has_tie:
        return "tie_nongame"
    txt = [x.lower() for x in subs]
    if any(re.search(r"or (below|less|lower|under)|\bbelow\b|\bunder\b", x) for x in txt) and any(
        re.search(r"or (above|more|higher|over)|\babove\b|\+\s*$", x) for x in txt
    ):
        return "text_partition"
    if len(ms) == 2:
        return "game_two_way" if is_game else "two_way"
    return "unknown"


def discover(events, fees):
    """Yield structures: dict(kind, event, series, legs=[(ticker, side, series)], payout, meta)."""
    structs = []
    now = now_utc()
    for e in events:
        ser = e.get("series_ticker")
        ms = [m for m in e.get("markets", []) if m.get("status") == "active"]
        ms = [m for m in ms if (iso(m.get("close_time")) or now) > now]
        if not ms:
            continue
        nonactive = [m for m in e.get("markets", []) if m.get("status") not in ("active", "finalized", "settled", "determined")]
        finalized_yes = [m for m in e.get("markets", []) if m.get("result") == "yes"]
        closes = {m["ticker"]: m.get("close_time") for m in ms}
        base = {"event": e["event_ticker"], "series": ser, "category": e.get("category"), "title": e.get("title"),
                "me": e.get("mutually_exclusive"), "crt": e.get("collateral_return_type")}
        # D. same market
        for m in ms:
            structs.append({**base, "kind": "SAME_MARKET", "legs": [(m["ticker"], "yes", ser), (m["ticker"], "no", ser)],
                            "payout": 1.0, "meta": {}})
        if e.get("mutually_exclusive") and len(ms) >= 2 and not finalized_yes:
            legs = [(m["ticker"], "no", ser) for m in ms]
            structs.append({**base, "kind": "ME_NO_BASKET", "legs": legs, "payout": None,
                            "meta": {"n": len(ms), "multi_close": len(set(closes.values())) > 1}})
            ek = exhaustive_kind([m for m in e.get("markets", []) if m.get("result") != "no"])
            structs.append({**base, "kind": "ME_YES_BASKET", "legs": [(m["ticker"], "yes", ser) for m in ms], "payout": 1.0,
                            "meta": {"n": len(ms), "exhaustive": ek, "nonactive_markets": len(nonactive),
                                     "multi_close": len(set(closes.values())) > 1}})
        # C. numeric pairs: same event AND same underlying entity (custom_strike, e.g. team/player)
        #    AND same close_time.  Groups with duplicate intervals at the same close are ambiguous
        #    (several underlyings share custom_strike=None, e.g. rain-by-city) and are skipped.
        groups = defaultdict(list)
        for m in ms:
            iv = interval(m)
            if iv is not None:
                groups[(json.dumps(m.get("custom_strike"), sort_keys=True), m.get("close_time"))].append((m, iv))
        pair_sets = []
        for gk, gl in groups.items():
            ivs = [iv for _, iv in gl]
            if len(set(ivs)) < len(ivs):
                AMBIGUOUS[e["event_ticker"]] += 1
                continue
            if len(gl) >= 2:
                pair_sets.append((gl, "PAIR"))
        # spread events: two teams' "wins by over X" ladders live on one axis (team-B margin > b  <=>  A-margin < -b)
        if "spread" in (e.get("title") or "").lower():
            by_close = defaultdict(list)
            for (cs, ct), gl in groups.items():
                by_close[ct].append((cs, gl))
            for ct, lst in by_close.items():
                if len(lst) == 2 and all(iv[2] == math.inf and not iv[1] for _, gl in lst for _, iv in gl):
                    (csA, glA), (csB, glB) = lst
                    comb = [(m, iv) for m, iv in glA] + [(m, (-math.inf, False, -iv[0], False)) for m, iv in glB]
                    pair_sets.append((comb, "XSPREAD"))
        for gl, tag in pair_sets:
            for i in range(len(gl)):
                for j in range(len(gl)):
                    if i == j:
                        continue
                    (ma, a), (mb, b) = gl[i], gl[j]
                    if tag == "XSPREAD" and (a[0] == -math.inf) == (b[0] == -math.inf):
                        continue  # within-team pairs already covered by the per-group PAIR set
                    same_dir = (a[2] == math.inf and b[2] == math.inf) or (a[0] == -math.inf and b[0] == -math.inf)
                    if subset(a, b) and (same_dir or not (path_dependent(ma) or path_dependent(mb))):
                        structs.append({**base, "kind": "PAIR_LADDER", "legs": [(mb["ticker"], "yes", ser), (ma["ticker"], "no", ser)],
                                        "payout": 1.0, "meta": {"multi_close": False, "group": tag}})
                    if path_dependent(ma) or path_dependent(mb):
                        continue  # only same-direction subset (ladder) relations are valid for first-passage events
                    if i < j and disjoint(a, b) and not e.get("mutually_exclusive"):
                        structs.append({**base, "kind": "PAIR_DISJOINT_NO", "legs": [(ma["ticker"], "no", ser), (mb["ticker"], "no", ser)],
                                        "payout": 1.0, "meta": {"multi_close": False, "group": tag}})
                    if i < j and covers_all(a, b):
                        structs.append({**base, "kind": "PAIR_COVER_YES", "legs": [(ma["ticker"], "yes", ser), (mb["ticker"], "yes", ser)],
                                        "payout": 1.0, "meta": {"multi_close": False, "group": tag}})
        # time ladders: "Before/By <date>" markets on the same question (same numeric interval or non-numeric),
        # nested by date: event-by-earlier-date  subset-of  event-by-later-date  -> YES(later) + NO(earlier) >= 1
        tlg = defaultdict(list)
        for m in ms:
            if BEFORE.match(m.get("yes_sub_title") or ""):
                iv = interval(m)
                tlg[(json.dumps(m.get("custom_strike"), sort_keys=True), iv)].append(m)
        if not e.get("mutually_exclusive"):
            for _, tl in tlg.items():
                tl.sort(key=lambda m: m.get("close_time") or "")
                for i in range(len(tl)):
                    for j in range(i + 1, len(tl)):
                        if tl[i].get("close_time") == tl[j].get("close_time"):
                            continue
                        structs.append({**base, "kind": "TIME_LADDER",
                                        "legs": [(tl[j]["ticker"], "yes", ser), (tl[i]["ticker"], "no", ser)],
                                        "payout": 1.0, "meta": {"multi_close": True}})
    return structs


AMBIGUOUS = defaultdict(int)

_REL = re.compile(r"\b(above|below|between|at or above|at or below|at least|at most|or more|or less|greater than|less than|"
                  r"higher than|lower than|more than|fewer than|exactly|over|under|strictly|and|or|to)\b")
_NUMTOK = re.compile(r"\d[\d,]*(?:\.\d+)?")


def rules_template(m):
    """rules_primary with ONLY this market's strike values (and relation words) blanked, so different underlyings
    ('5Y' vs '30Y' Treasury, CA-01 vs CA-02) keep distinct templates."""
    t = (m.get("rules_primary") or "").lower()
    strikes = [x for x in (m.get("floor_strike"), m.get("cap_strike")) if x is not None]

    def rep(mo):
        try:
            v = float(mo.group(0).replace(",", ""))
        except ValueError:
            return mo.group(0)
        return "#" if any(abs(v - x) <= 0.011 + 1e-9 * abs(x) for x in strikes) else mo.group(0)

    t = _NUMTOK.sub(rep, t)
    t = _REL.sub(" ", t)
    t = re.sub(r"[$%]", "", t)
    t = re.sub(r"#\s*(?:-|–)?\s*#", "#", t)
    return re.sub(r"\s+", " ", t).strip()


def discover_cross(events):
    """Same underlying + same settlement instant listed in DIFFERENT events (e.g. KXBTCD above-strikes vs KXBTC ranges).
    Markets are matched by an identical rules template (numbers and relation words stripped) and identical close_time.
    Emits cross-event pair structures and threshold-vs-range-partition baskets."""
    structs = []
    now = now_utc()
    buckets = defaultdict(list)  # (template, close_time) -> [(event, market, interval)]
    for e in events:
        for m in e.get("markets", []):
            if m.get("status") != "active" or m.get("custom_strike"):
                continue
            if (iso(m.get("close_time")) or now) <= now:
                continue
            iv = interval(m)
            if iv is None or path_dependent(m):
                continue
            buckets[(rules_template(m), m.get("close_time"))].append((e, m, iv))
    for (tmpl, ct), lst in buckets.items():
        evs = {e["event_ticker"] for e, _, _ in lst}
        if len(evs) < 2:
            continue
        if len({iv for _, _, iv in lst}) < len(lst) - 0:  # identical intervals in two events: duplicate listing -> still fine
            pass
        base_e = lst[0][0]
        base = {"event": "+".join(sorted(evs)), "series": base_e.get("series_ticker"), "category": base_e.get("category"),
                "title": base_e.get("title"), "me": None, "crt": "CROSS"}
        # pairs across events only
        for i in range(len(lst)):
            ea, ma, a = lst[i]
            for j in range(len(lst)):
                if i == j:
                    continue
                eb, mb, b = lst[j]
                if ea["event_ticker"] == eb["event_ticker"]:
                    continue
                sa, sb = ea.get("series_ticker"), eb.get("series_ticker")
                if subset(a, b):
                    structs.append({**base, "kind": "XEV_LADDER", "legs": [(mb["ticker"], "yes", sb), (ma["ticker"], "no", sa)],
                                    "payout": 1.0, "meta": {"multi_close": False}})
                if i < j and disjoint(a, b):
                    structs.append({**base, "kind": "XEV_DISJOINT_NO", "legs": [(ma["ticker"], "no", sa), (mb["ticker"], "no", sb)],
                                    "payout": 1.0, "meta": {"multi_close": False}})
                if i < j and covers_all(a, b):
                    structs.append({**base, "kind": "XEV_COVER_YES", "legs": [(ma["ticker"], "yes", sa), (mb["ticker"], "yes", sb)],
                                    "payout": 1.0, "meta": {"multi_close": False}})
        # threshold vs contiguous range run that exactly tiles (x, inf) or (-inf, x]
        by_ev = defaultdict(list)
        for e, m, iv in lst:
            by_ev[e["event_ticker"]].append((e, m, iv))
        for ev_t, tl in by_ev.items():
            thr = [(e, m, iv) for e, m, iv in tl if (iv[2] == math.inf) != (iv[0] == -math.inf)]
            for ev_r, rl in by_ev.items():
                if ev_r == ev_t:
                    continue
                rng = sorted([(e, m, iv) for e, m, iv in rl], key=lambda x: (x[2][0], x[2][2]))
                if len(rng) < 2:
                    continue
                for et, mt, tv in thr:
                    st = et.get("series_ticker")
                    if tv[2] == math.inf:  # threshold (x, inf): need ranges tiling (x, inf)
                        x = tv[0]
                        run = [r for r in rng if r[2][0] >= x - 1e-9]
                        if not run or run[-1][2][2] != math.inf:
                            continue
                        lo0 = run[0][2][0]
                    else:  # threshold (-inf, x): need ranges tiling (-inf, x)
                        x = tv[2]
                        run = [r for r in rng if r[2][2] <= x + 1e-9]
                        if not run or run[0][2][0] != -math.inf:
                            continue
                        lo0 = None
                    ok = True
                    for q in range(len(run) - 1):
                        gap = run[q + 1][2][0] - run[q][2][2]
                        if not (-1e-9 <= gap <= 0.011):
                            ok = False
                            break
                    if tv[2] == math.inf:
                        ok = ok and (0 <= lo0 - x <= 0.011)
                    else:
                        ok = ok and (0 <= x - run[-1][2][2] <= 0.011)
                    if not ok or len(run) > 60:
                        continue
                    kk = len(run)
                    rlegs_yes = [(r[1]["ticker"], "yes", r[0].get("series_ticker")) for r in run]
                    rlegs_no = [(r[1]["ticker"], "no", r[0].get("series_ticker")) for r in run]
                    # YES on all tiles + NO(threshold) -> payout >= 1 ;  YES(threshold) + NO on all tiles -> payout = k
                    structs.append({**base, "kind": "XEV_TILE_BASKET", "legs": rlegs_yes + [(mt["ticker"], "no", st)],
                                    "payout": 1.0, "meta": {"multi_close": False, "k": kk, "form": "yes_tiles+no_thr"}})
                    structs.append({**base, "kind": "XEV_TILE_BASKET", "legs": [(mt["ticker"], "yes", st)] + rlegs_no,
                                    "payout": float(kk), "meta": {"multi_close": False, "k": kk, "form": "yes_thr+no_tiles"}})
    return structs


def evaluate(s, books, fees):
    if s["kind"] == "SAME_MARKET":
        return walk_fixed(s["legs"], 1.0, books, fees)
    if s["kind"] == "ME_NO_BASKET":
        return walk_no_basket(s["legs"], books, fees)
    return walk_fixed(s["legs"], s["payout"], books, fees)


def top_edge(s, books, fees):
    """Top-of-book net edge per unit (fees exact, no rounding)."""
    if s["kind"] == "ME_NO_BASKET":
        tot = 0.0
        for t, _, ser in s["legs"]:
            a = asks(books[t], "no")
            if a:
                p = a[0][0]
                c = (1 - p) - fees.per_contract(t, p, ser)
                if c > 0:
                    tot += c
        return tot - 1
    tot = 0.0
    for t, side, ser in s["legs"]:
        a = asks(books[t], side)
        if not a:
            return None
        p = a[0][0]
        tot += p + fees.per_contract(t, p, ser)
    return s["payout"] - tot


def risk_free(s):
    k = s["kind"]
    if k == "ME_YES_BASKET":
        return s["meta"].get("exhaustive") in ("numeric_partition", "game_tie") and not s["meta"].get("nonactive_markets")
    if k == "TIME_LADDER":
        return False  # needs manual rules review; settlement-timing + early-close semantics
    return True


# ----------------------------------------------------------------------------------------------
# Scan
# ----------------------------------------------------------------------------------------------
def slim_market(m):
    keys = ("ticker", "event_ticker", "status", "yes_bid_dollars", "yes_ask_dollars", "yes_bid_size_fp", "yes_ask_size_fp",
            "strike_type", "floor_strike", "cap_strike", "yes_sub_title", "close_time", "expected_expiration_time",
            "volume_24h_fp", "open_interest_fp", "price_level_structure", "result", "can_close_early")
    return {k: m.get(k) for k in keys}


def fetch_books(k, tickers):
    out = {}
    tickers = sorted(set(tickers))
    for i in range(0, len(tickers), 100):
        d = k.get("/markets/orderbooks", {"tickers": tickers[i:i + 100]})
        for ob in d.get("orderbooks", []):
            out[ob["ticker"]] = book_from_api(ob.get("orderbook_fp") or {})
    return out


def scan_once(k, fees, scan_id, save_snap=True):
    t0 = time.time()
    ts = now_utc()
    events = k.paginate("/events", {"status": "open", "limit": 200, "with_nested_markets": "true", "mve_filter": "exclude"}, "events")
    t_list = time.time()
    fees.set_markets(events)
    mk = {m["ticker"]: m for e in events for m in e.get("markets", [])}
    lbooks = {t: book_from_listing(m) for t, m in mk.items()}
    structs = discover(events, fees) + (discover_cross(events) if CROSS else [])
    counts = defaultdict(int)
    for s in structs:
        counts[s["kind"]] += 1
    # pre-screen on top of book (from listing)
    cands = []
    edges_hist = defaultdict(list)
    for s in structs:
        te = top_edge(s, lbooks, fees)
        if te is None:
            continue
        if s["kind"] != "SAME_MARKET":
            edges_hist[s["kind"]].append(te)
        if te > (0.0 if s["kind"] == "SAME_MARKET" else PRESCREEN):
            s["listing_edge"] = te
            cands.append(s)
    # fetch full books for candidate legs
    tick = [t for s in cands for t, _, _ in s["legs"]]
    books = fetch_books(k, tick) if tick else {}
    t_books = time.time()
    if save_snap:
        SNAP.mkdir(parents=True, exist_ok=True)
        cand_events = {s["event"] for s in cands}
        slim = [{"event_ticker": e["event_ticker"], "series_ticker": e.get("series_ticker"), "mutually_exclusive": e.get("mutually_exclusive"),
                 "collateral_return_type": e.get("collateral_return_type"), "category": e.get("category"), "title": e.get("title"),
                 "markets": [slim_market(m) for m in e.get("markets", [])]} for e in events if e["event_ticker"] in cand_events]
        try:
            with gzip.open(SNAP / f"{scan_id}.json.gz", "wt") as fh:
                json.dump({"ts": ts.isoformat(), "books_ts": now_utc().isoformat(), "events": slim, "books": books}, fh)
        except OSError as ex:
            print("snapshot write failed", ex, flush=True)
    opps, near = [], []
    for s in cands:
        if any(t not in books for t, _, _ in s["legs"]):
            continue
        r = evaluate(s, books, fees)
        te = top_edge(s, books, fees)
        legs_meta = [{"ticker": t, "close_time": mk[t].get("close_time"), "exp": mk[t].get("expected_expiration_time"),
                      "can_close_early": mk[t].get("can_close_early"), "sub": mk[t].get("yes_sub_title"),
                      "vol24h": f(mk[t].get("volume_24h_fp"))} for t, _, _ in s["legs"]]
        rec = {"scan_id": scan_id, "ts": ts.isoformat(), "kind": s["kind"], "event": s["event"], "series": s["series"],
               "category": s.get("category"), "title": s.get("title"), "crt": s.get("crt"), "me": s.get("me"),
               "risk_free": risk_free(s), "meta": s["meta"], "listing_edge": round(s["listing_edge"], 5),
               "book_top_edge": round(te, 5) if te is not None else None,
               "fee_mult": max(fees.mult_for(t, s["series"]) for t, _, _ in s["legs"]), "key": key_of(s), "payout_unit": s["payout"], **r, "legs_meta": legs_meta,
               "struct_legs": s["legs"]}
        if r["profit"] > 0:
            opps.append(rec)
        else:
            near.append(rec)
    summary = {"scan_id": scan_id, "ts": ts.isoformat(), "n_events": len(events), "n_markets": len(mk),
               "n_active": sum(1 for m in mk.values() if m.get("status") == "active"), "structures": dict(counts),
               "n_candidates": len(cands), "n_opps": len(opps), "n_opps_riskfree": sum(1 for o in opps if o["risk_free"]),
               "profit_riskfree": round(sum(o["profit"] for o in opps if o["risk_free"]), 2),
               "profit_all": round(sum(o["profit"] for o in opps), 2),
               "requests": k.n_requests, "sec_listing": round(t_list - t0, 1), "sec_books": round(t_books - t_list, 1),
               "edge_quantiles": {kind: quantiles(v) for kind, v in edges_hist.items()}}
    return summary, opps, near


def quantiles(v):
    if not v:
        return {}
    v = sorted(v)
    q = lambda p: round(v[min(len(v) - 1, int(p * (len(v) - 1)))], 4)
    return {"n": len(v), "p50": q(0.5), "p90": q(0.9), "p99": q(0.99), "max": round(v[-1], 4),
            "n_gt_-0.01": sum(1 for x in v if x > -0.01), "n_gt_0": sum(1 for x in v if x > 0)}


def key_of(s):
    return s["kind"] + "|" + ",".join(f"{t}:{side}" for t, side, _ in s["legs"])


def track(k, fees, opps, until, every, fh):
    """Re-poll books of live opportunities until `until` (epoch s) to measure persistence."""
    if not opps:
        time.sleep(max(0, until - time.time()))
        return
    live = {o["key"]: o for o in opps}
    while time.time() < until and live:
        t0 = time.time()
        tick = [t for o in live.values() for t, _, _ in o["struct_legs"]]
        books = fetch_books(k, tick)
        for key, o in list(live.items()):
            s = {"kind": o["kind"], "legs": [tuple(x) for x in o["struct_legs"]], "payout": o.get("payout_unit", 1.0),
                 "meta": o["meta"]}
            r = evaluate(s, books, fees)
            fh.write(json.dumps({"ts": now_utc().isoformat(), "key": key, "profit": r["profit"], "units": r["units"],
                                 "top_edge": r["top_edge"]}) + "\n")
            fh.flush()
            if r["profit"] <= 0:
                del live[key]
        dt = time.time() - t0
        time.sleep(max(0, min(every - dt, until - time.time())))


class Tee:
    """Append-only JSONL writer mirrored to $ARB_BACKUP_DIR (other processes on this box delete/gzip files)."""
    def __init__(self, name):
        import os
        self.paths = [OUT / name]
        b = os.environ.get("ARB_BACKUP_DIR")
        if b:
            Path(b).mkdir(parents=True, exist_ok=True)
            self.paths.append(Path(b) / name)

    def write(self, line):
        for p in self.paths:
            try:
                with open(p, "a") as fh:
                    fh.write(line)
            except OSError as e:
                print("write failed", p, e, flush=True)

    def flush(self):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=1)
    ap.add_argument("--interval", type=float, default=150.0, help="seconds between scan starts")
    ap.add_argument("--track-every", type=float, default=10.0)
    ap.add_argument("--rate", type=float, default=4.5)
    ap.add_argument("--tag", default="run")
    args = ap.parse_args()
    k = Kalshi(rate=args.rate)
    fees = Fees(k)
    RAW.mkdir(exist_ok=True)
    fs = Tee(f"scans_{args.tag}.jsonl")
    fo = Tee(f"opps_{args.tag}.jsonl")
    fn = Tee(f"near_{args.tag}.jsonl")
    ft = Tee(f"track_{args.tag}.jsonl")
    for it in range(args.iterations):
        start = time.time()
        scan_id = f"{args.tag}_{now_utc().strftime('%Y%m%dT%H%M%S')}"
        try:
            summary, opps, near = scan_once(k, fees, scan_id, save_snap=True)
        except Exception as ex:  # keep looping on transient failures
            print(f"[{scan_id}] scan failed: {ex!r}", flush=True)
            time.sleep(max(0, start + args.interval - time.time()))
            continue
        fs.write(json.dumps(summary) + "\n"); fs.flush()
        for o in opps:
            fo.write(json.dumps(o) + "\n")
        fo.flush()
        for o in near:
            fn.write(json.dumps(o) + "\n")
        fn.flush()
        print(f"[{scan_id}] events={summary['n_events']} cands={summary['n_candidates']} opps={summary['n_opps']} "
              f"rf_opps={summary['n_opps_riskfree']} rf_profit=${summary['profit_riskfree']} all_profit=${summary['profit_all']} "
              f"listing={summary['sec_listing']}s books={summary['sec_books']}s req={summary['requests']}", flush=True)
        for o in sorted(opps, key=lambda o: -o["profit"])[:8]:
            print(f"   {o['kind']:14s} rf={o['risk_free']!s:5s} {o['event']:40s} units={o['units']:<9} profit=${o['profit']:<8} "
                  f"top_edge={o['top_edge']} meta={o['meta']}", flush=True)
        if it < args.iterations - 1:
            trackable = [o for o in opps if o["risk_free"] or o["meta"].get("exhaustive") in ("catchall_other", "game_two_way")]
            track(k, fees, trackable, start + args.interval, args.track_every, ft)


if __name__ == "__main__":
    main()
