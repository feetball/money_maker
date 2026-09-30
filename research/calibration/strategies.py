"""Walk-forward strategy search with a chronological out-of-sample split.

Mechanics (identical for every rule):
  * Hourly decision grid (UTC, on the hour). At time t a market is eligible iff it is open (open_time <= t <
    close_time) and has a quote from a candle that ended <= t (see calib_lib.build_panel).
  * A rule = side (buy YES at the YES ask | buy NO at the NO ask = 1 - YES bid) + band on the ENTRY PRICE +
    optional filters (hours to expected expiration, category, max spread). The first grid time at which a market
    satisfies the rule is the entry (one position per market, ever). Hold to settlement. No exits.
  * P&L per contract = win - entry - taker fee. Fee = ceil_to_cent(0.07*mult*C*P*(1-P)) per order of C contracts
    (C=100 primary, C=10 and the unrounded fee also reported).
  * TRAIN = entries before the split time whose market also CLOSED before the split (no look-ahead into outcomes);
    TEST = entries at or after the split. Thresholds are chosen on TRAIN only.
  * Maker variant (OPTIMISTIC): at the same trigger, rest a limit order at the bid (YES bid for YES; NO bid =
    1 - YES ask for NO). Filled only if a LATER candle traded strictly through the price (YES trade price < our YES
    bid, or > YES ask for NO), no queue modelled, whole size filled; maker fee 0 on 'quadratic' series, 0.0175*P*(1-P)
    on 'quadratic_with_maker_fees'.

    uv run --with pandas --with pyarrow --with numpy python strategies.py
Outputs: grid_results.csv, oos_selected.csv, headline_rules.csv, headline_monthly.csv, portfolio_sims.csv,
         trades_<rule>.csv.gz for headline rules.
"""
from __future__ import annotations

import itertools
import json
import time

import numpy as np
import pandas as pd

from calib_lib import HERE, PANEL_FILE, cluster_se, fee_per_contract

MAKER_COEF = 0.0175


# ------------------------------------------------------------------------------------------------ data
def load_panel() -> pd.DataFrame:
    p = pd.read_parquet(PANEL_FILE)
    if "event_all_candles" in p:
        p = p[p["event_all_candles"]]
    p = p.sort_values(["ticker", "t"], kind="stable").reset_index(drop=True)
    p["tcode"] = p["ticker"].cat.codes.astype("int64")
    p["ask"] = p["ask"].astype("float64").round(4)
    p["bid"] = p["bid"].astype("float64").round(4)
    ask, bid = p["ask"], p["bid"]
    p["q_yes"] = np.where((ask > 0) & (ask < 1), ask, np.nan)          # pay the YES ask
    p["q_no"] = np.where((bid > 0) & (bid < 1), 1 - bid, np.nan)       # pay the NO ask = 1 - YES bid
    p["spread"] = np.where((ask < 1) & (bid > 0), ask - bid, np.nan)
    p["close_ts"] = p["close_time"].dt.as_unit("s").astype("int64")
    p["date"] = pd.to_datetime(p["t"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    return p


# ------------------------------------------------------------------------------------------------ rules
def rule_mask(p: pd.DataFrame, side: str, lo: float, hi: float, hz=None, cat=None, max_spread=None,
              series=None, extra=None, basis: str = "mid") -> np.ndarray:
    """basis = which price the [lo, hi) band applies to, always expressed for the side we BUY:
       'entry' = that side's ask (what we pay), 'bid' = that side's bid (YES bid, or NO bid = 1 - YES ask),
       'mid' = that side's mid. A valid entry price (ask < 1 on that side) is always required."""
    q = p["q_yes"].to_numpy() if side == "yes" else p["q_no"].to_numpy()
    bid, ask = p["bid"].to_numpy(dtype="float64"), p["ask"].to_numpy(dtype="float64")
    if basis == "entry":
        x = q
    elif basis == "bid":
        x = np.where(bid > 0, bid, np.nan) if side == "yes" else np.where(ask < 1, 1 - ask, np.nan)
    else:
        mid = (bid + ask) / 2
        x = mid if side == "yes" else 1 - mid
    m = np.isfinite(q) & (x >= lo - 1e-9) & (x < hi - 1e-9)
    if hz is not None:
        h = p["h_to_eet"].to_numpy()
        m &= (h >= hz[0]) & (h < hz[1])
    if cat is not None:
        m &= (p["category"] == cat).to_numpy()
    if max_spread is not None:
        s = p["spread"].to_numpy()
        m &= s <= max_spread + 1e-9
    if series is not None:
        m &= p["series_ticker"].isin(series).to_numpy()
    if extra is not None:
        m &= extra
    return m


def first_trigger(p: pd.DataFrame, mask: np.ndarray) -> np.ndarray:
    idx = np.flatnonzero(mask)
    if len(idx) == 0:
        return idx
    _, first = np.unique(p["tcode"].to_numpy()[idx], return_index=True)   # p sorted by (ticker, t)
    return idx[first]


def trades_for(p: pd.DataFrame, side: str, idx: np.ndarray) -> pd.DataFrame:
    t = p.iloc[idx][["ticker", "event_ticker", "series_ticker", "category", "t", "date", "month", "close_ts",
                     "settlement_ts", "h_to_eet", "bid", "ask", "spread", "y", "fee_multiplier", "fee_type",
                     "fut_low", "fut_high"]].copy()
    y = t["y"].astype("float64").to_numpy()
    mult = t["fee_multiplier"].astype("float64").to_numpy()
    bid, ask = t["bid"].astype("float64").to_numpy(), t["ask"].astype("float64").to_numpy()
    if side == "yes":
        q, win = ask, y
        mq = bid                                         # maker: rest at the YES bid
        filled = (bid > 0) & (t["fut_low"].to_numpy() < bid - 1e-9)
    else:
        q, win = 1 - bid, 1 - y
        mq = 1 - ask                                     # maker: rest at the NO bid = 1 - YES ask
        filled = (ask < 1) & (t["fut_high"].to_numpy() > ask + 1e-9)
    t["side"] = side
    t["entry"] = q
    t["win"] = win
    t["pnl_prefee"] = win - q
    t["pnl"] = win - q - fee_per_contract(q, 100, mult)
    t["pnl_c10"] = win - q - fee_per_contract(q, 10, mult)
    t["pnl_exact"] = win - q - fee_per_contract(q, 100, mult, rounding="none")
    mk_fee = np.where(t["fee_type"].astype(str).str.contains("maker").to_numpy(),
                      fee_per_contract(mq, 100, mult, coef=MAKER_COEF), 0.0)
    t["maker_price"] = mq
    t["maker_filled"] = filled
    t["maker_pnl"] = np.where(filled, win - mq - mk_fee, np.nan)
    return t


def summarize(t: pd.DataFrame, col: str = "pnl") -> dict:
    if len(t) == 0:
        return dict(n=0, n_events=0, mean=np.nan, se=np.nan, t_stat=np.nan, hit=np.nan, avg_entry=np.nan)
    mu, se, G = cluster_se(t[col].to_numpy(), t["event_ticker"].astype(str).to_numpy())
    _, se_d, _ = cluster_se(t[col].to_numpy(), t["date"].astype(str).to_numpy())
    # floor: a sample with no (or very few) losses still has binomial uncertainty -> rule-of-one floor on loss rate
    lr = max(1 - t["win"].mean(), 1.0 / G)
    se_floor = np.sqrt(lr * (1 - lr) / G)
    se_t = max(se, se_d, se_floor)
    return dict(n=len(t), n_events=G, mean=mu, se=se, se_date=se_d, t_stat=mu / se_t if se_t > 0 else np.nan,
                hit=t["win"].mean(), avg_entry=t["entry"].mean(), roi=t[col].sum() / t["entry"].sum())


# ------------------------------------------------------------------------------------------------ portfolio
def portfolio_sim(t: pd.DataFrame, frac: float = 0.02, bank0: float = 1000.0, col: str = "pnl",
                  max_event_frac: float = 0.10) -> dict:
    """Fixed-fraction sim: each trade stakes frac * current equity (cost basis) if cash allows, at most
    max_event_frac of equity per event; positions settle at settlement_ts. Equity = cash + cost of open
    positions (mark-to-cost), sampled at every settlement."""
    if len(t) == 0:
        return dict(trades=0)
    ev = []
    for r in t.itertuples():
        ev.append((int(r.t), 1, r))
        st = int(pd.Timestamp(r.settlement_ts).timestamp()) if pd.notna(r.settlement_ts) else int(r.close_ts)
        ev.append((max(st, int(r.t) + 1), 0, r))
    ev.sort(key=lambda e: (e[0], e[1]))   # settlements before entries at the same second
    cash, open_cost, taken, skipped = bank0, 0.0, 0, 0
    pos, ev_exp = {}, {}
    curve, peak, mdd = [], bank0, 0.0
    for ts, kind, r in ev:
        key = r.ticker
        if kind == 1:
            eq = cash + open_cost
            stake = frac * eq
            if ev_exp.get(r.event_ticker, 0.0) + stake > max_event_frac * eq or stake > cash or r.entry <= 0:
                skipped += 1
                continue
            n = stake / r.entry
            cost = n * r.entry
            cash -= cost; open_cost += cost
            pos[key] = (n, cost, r.event_ticker)
            ev_exp[r.event_ticker] = ev_exp.get(r.event_ticker, 0.0) + cost
            taken += 1
        elif key in pos:
            n, cost, e = pos.pop(key)
            open_cost -= cost
            ev_exp[e] -= cost
            cash += cost + n * getattr(r, col)       # pnl per contract already net of fee
            eq = cash + open_cost
            peak = max(peak, eq)
            mdd = max(mdd, 1 - eq / peak)
            curve.append((ts, eq))
    eq = cash + open_cost
    return dict(trades=taken, skipped=skipped, final_equity=eq, ret=eq / bank0 - 1, max_dd=min(mdd, 1.0),
                days=(ev[-1][0] - ev[0][0]) / 86400)


# ------------------------------------------------------------------------------------------------ grid
# bands on the MID of the side we buy (YES mid, or NO mid = 1 - YES mid); entry is always at that side's ask
BANDS = [(0.001, 0.03), (0.03, 0.07), (0.07, 0.15), (0.15, 0.30), (0.30, 0.45), (0.45, 0.55), (0.55, 0.70),
         (0.70, 0.85), (0.85, 0.93), (0.93, 0.97), (0.97, 1.0)]
THRESH_HI = [0.80, 0.85, 0.90, 0.95, 0.97]             # mid >= x : back the favourite side
THRESH_LO = [0.03, 0.05, 0.10, 0.20]                   # mid <  x : back the longshot side
HZ = {"all": None, "0-1h": (0, 1), "1-6h": (1, 6), "6-24h": (6, 24), "1-3d": (24, 72), ">3d": (72, 1e9)}
SPREADS = {"<=2c": 0.02, "<=5c": 0.05, "<=10c": 0.10}


def precompute(p: pd.DataFrame) -> dict:
    """Per-row arrays for both sides (fast grid evaluation; same formulas as trades_for)."""
    y = p["y"].to_numpy(dtype="float64")
    mult = p["fee_multiplier"].to_numpy(dtype="float64")
    bid, ask = p["bid"].to_numpy(), p["ask"].to_numpy()
    maker_series = p["fee_type"].astype(str).str.contains("maker").to_numpy()
    fl, fh = p["fut_low"].to_numpy(dtype="float64"), p["fut_high"].to_numpy(dtype="float64")
    A = {"ev": pd.factorize(p["event_ticker"])[0], "date": pd.factorize(p["date"])[0],
         "t": p["t"].to_numpy(), "close_ts": p["close_ts"].to_numpy()}
    for side in ("yes", "no"):
        q = p["q_yes"].to_numpy() if side == "yes" else p["q_no"].to_numpy()
        win = y if side == "yes" else 1 - y
        mq = bid if side == "yes" else 1 - ask
        filled = ((bid > 0) & (fl < bid - 1e-9)) if side == "yes" else ((ask < 1) & (fh > ask + 1e-9))
        mfee = np.where(maker_series, fee_per_contract(mq, 100, mult, coef=MAKER_COEF), 0.0)
        A[side] = {"q": q, "win": win, "pnl": win - q - fee_per_contract(np.nan_to_num(q), 100, mult),
                   "filled": filled, "mpnl": np.where(filled, win - mq - mfee, np.nan)}
    return A


def fast_stats(A: dict, side: str, idx: np.ndarray) -> dict:
    if len(idx) == 0:
        return dict(n=0)
    S = A[side]
    v, w, ev, dt = S["pnl"][idx], S["win"][idx], A["ev"][idx], A["date"][idx]
    n, mu = len(v), v.mean()
    def cse(codes):
        u, inv = np.unique(codes, return_inverse=True)
        G = len(u)
        sm = np.bincount(inv, weights=v - mu, minlength=G)
        return float(np.sqrt((sm ** 2).sum() / n ** 2 * (G / max(G - 1, 1)))), G
    se, G = cse(ev)
    se_d, _ = cse(dt)
    lr = max(1 - w.mean(), 1.0 / G)
    se_t = max(se, se_d, np.sqrt(lr * (1 - lr) / G))
    f = S["filled"][idx]
    return dict(n=n, n_events=G, mean=mu, se=se, se_date=se_d, t_stat=mu / se_t, hit=w.mean(),
                avg_entry=S["q"][idx].mean(), maker_fill=f.mean(),
                maker_mean=np.nanmean(S["mpnl"][idx]) if f.any() else np.nan)


def grid(p: pd.DataFrame, T: int) -> pd.DataFrame:
    A = precompute(p)
    cats = [None] + sorted(p["category"].astype(str).unique())
    bands = [(lo, hi, f"[{lo:.3g},{hi:.3g})") for lo, hi in BANDS] + \
            [(x, 1.0, f">={x:.2f}") for x in THRESH_HI] + [(0.001, x, f"<{x:.2f}") for x in THRESH_LO]
    tt, close_ts = A["t"], A["close_ts"]
    cat_masks = {c: (p["category"] == c).to_numpy() for c in cats if c is not None}
    rows = []
    t0 = time.time()
    for side, (lo, hi, bname), (spn, sp) in itertools.product(["yes", "no"], bands, SPREADS.items()):
        base = rule_mask(p, side, lo, hi, None, None, sp, basis="mid")
        for (hzn, hz), cat in itertools.product(HZ.items(), cats):
            m = base
            if hz is not None:
                h = p["h_to_eet"].to_numpy()
                m = m & (h >= hz[0]) & (h < hz[1])
            if cat is not None:
                m = m & cat_masks[cat]
            if m.sum() < 30:
                continue
            idx = first_trigger(p, m)
            tr_idx = idx[(tt[idx] < T) & (close_ts[idx] <= T)]
            te_idx = idx[tt[idx] >= T]
            if len(tr_idx) < 30:
                continue
            r = dict(side=side, band=bname, lo=lo, hi=hi, horizon=hzn, category=cat or "ALL", spread=spn)
            for tag, ii in (("train", tr_idx), ("test", te_idx)):
                r.update({f"{tag}_{k}": v for k, v in fast_stats(A, side, ii).items()})
            rows.append(r)
    print(f"grid: {len(rows)} configs evaluated in {time.time()-t0:.0f}s")
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------------------------ main
def rule_report(p, T, name, side, lo, hi, **kw):
    idx = first_trigger(p, rule_mask(p, side, lo, hi, **kw))
    tt, cl = p["t"].to_numpy()[idx], p["close_ts"].to_numpy()[idx]
    tr = trades_for(p, side, idx[(tt < T) & (cl <= T)])
    te = trades_for(p, side, idx[tt >= T])
    out = []
    for tag, d in (("train", tr), ("test", te)):
        s = summarize(d)
        s.update({f"c10_{k}": v for k, v in summarize(d, "pnl_c10").items() if k in ("mean", "se")})
        s.update({f"exact_{k}": v for k, v in summarize(d, "pnl_exact").items() if k in ("mean", "se")})
        s.update({f"prefee_{k}": v for k, v in summarize(d, "pnl_prefee").items() if k in ("mean", "se")})
        fm = d[d["maker_filled"]] if len(d) else d
        s["maker_fill_rate"] = d["maker_filled"].mean() if len(d) else np.nan
        s["maker_pnl_per_filled"] = fm["maker_pnl"].mean() if len(fm) else np.nan
        s["maker_pnl_se"] = cluster_se(fm["maker_pnl"].to_numpy(), fm["event_ticker"].astype(str).to_numpy())[1] \
            if len(fm) > 1 else np.nan
        for f in (0.01, 0.02, 0.05):
            ps = portfolio_sim(d, frac=f)
            s[f"sim{int(f*100)}pct_ret"] = ps.get("ret"); s[f"sim{int(f*100)}pct_maxdd"] = ps.get("max_dd")
        s.update(rule=name, period=tag)
        out.append(s)
    allt = pd.concat([tr.assign(period="train"), te.assign(period="test")])
    allt["rule"] = name
    return out, allt


def main():
    split = json.loads((HERE / "split.json").read_text())
    T = int(split["split_ts"])
    p = load_panel()
    print(f"panel rows {len(p)}, markets {p.ticker.nunique()}, events {p.event_ticker.nunique()}, split {split['split']}")

    # ---- 1. pre-registered simple rules (no category / horizon tuning)
    TENNIS = ["KXATPMATCH", "KXATPCHALLENGERMATCH", "KXITFWMATCH", "KXITFMATCH", "KXWTAMATCH"]
    E, BID, MID = "entry", "bid", "mid"
    pre = {
        # task-literal: buy NO when YES ask <= X  (== NO bid >= 1-X), pay NO ask = 1 - YES bid
        "A1 fade longshot: YES ask<=3c -> buy NO": dict(side="no", lo=0.97, hi=1.0, basis=BID),
        "A2 fade longshot: YES ask<=5c -> buy NO": dict(side="no", lo=0.95, hi=1.0, basis=BID),
        "A3 fade longshot: YES ask<=10c -> buy NO": dict(side="no", lo=0.90, hi=1.0, basis=BID),
        "A4 fade longshot: YES ask<=15c -> buy NO": dict(side="no", lo=0.85, hi=1.0, basis=BID),
        # task-literal: buy YES when YES ask >= Y  (fires on wide 0.02/0.98 books -> shown for completeness)
        "B0 back favorite (literal): YES ask>=90c -> buy YES": dict(side="yes", lo=0.90, hi=1.0, basis=E),
        # sensible mirror of A: buy YES when YES BID >= Y (both sides of the book say favourite), pay the ask
        "B1 back favorite: YES bid>=85c -> buy YES": dict(side="yes", lo=0.85, hi=1.0, basis=BID),
        "B2 back favorite: YES bid>=90c -> buy YES": dict(side="yes", lo=0.90, hi=1.0, basis=BID),
        "B3 back favorite: YES bid>=95c -> buy YES": dict(side="yes", lo=0.95, hi=1.0, basis=BID),
        "B4 back favorite: YES bid>=97c -> buy YES": dict(side="yes", lo=0.97, hi=1.0, basis=BID),
        # mirrors
        "C1 back longshot: YES ask<=10c -> buy YES": dict(side="yes", lo=0.001, hi=0.10 + 1e-6, basis=E),
        "C2 fade favorite: YES bid>=80c -> buy NO": dict(side="no", lo=0.001, hi=0.20 + 1e-6, basis=E),
        "C3 fade favorite: YES mid .70-.90, spread<=4c -> buy NO": dict(side="no", lo=0.10, hi=0.30 + 1e-6, basis=MID, max_spread=0.04),
        # dataset agent's exploratory hypothesis (NOT independent of the test period: found on data incl. Sep)
        "D1 tennis fade fav: YES mid .70-.90, spread<=4c, >=12h to EET -> buy NO": dict(
            side="no", lo=0.10, hi=0.30 + 1e-6, basis=MID, hz=(12, 1e9), max_spread=0.04, series=TENNIS),
    }
    rep, trades = [], []
    for name, kw in pre.items():
        o, t = rule_report(p, T, name, **kw)
        rep += o; trades.append(t)
    rep = pd.DataFrame(rep)
    cols = ["rule", "period", "n", "n_events", "avg_entry", "hit", "prefee_mean", "mean", "se", "se_date", "t_stat",
            "c10_mean", "exact_mean", "roi", "maker_fill_rate", "maker_pnl_per_filled", "maker_pnl_se",
            "sim1pct_ret", "sim1pct_maxdd", "sim2pct_ret", "sim2pct_maxdd", "sim5pct_ret", "sim5pct_maxdd"]
    rep[cols].to_csv(HERE / "headline_rules.csv", index=False)
    trades = pd.concat(trades)
    mon = trades.groupby(["rule", "period", "month"], observed=True).agg(
        n=("pnl", "size"), n_events=("event_ticker", "nunique"), hit=("win", "mean"), entry=("entry", "mean"),
        pnl=("pnl", "mean"), pnl_c10=("pnl_c10", "mean")).reset_index()
    mon.to_csv(HERE / "headline_monthly.csv", index=False)
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40); pd.set_option("display.max_rows", 500)
    print(rep[cols].round(4).to_string(index=False))
    print(mon.round(4).to_string(index=False))
    trades.to_csv(HERE / "trades_headline_rules.csv.gz", index=False, compression="gzip")

    # ---- 2. grid search on TRAIN, evaluate on TEST
    g = grid(p, T)
    g.to_csv(HERE / "grid_results.csv", index=False)
    sel = g[(g.train_n_events >= 40) & (g.train_n >= 100) & (g.train_mean > 0) & (g.train_t_stat >= 2.0)].copy()
    sel = sel.sort_values("train_t_stat", ascending=False)
    sel.to_csv(HERE / "oos_selected.csv", index=False)
    print(f"\n{len(g)} configs; {len(sel)} pass the TRAIN filter (n_events>=40, n>=100, mean>0, t>=2)")
    if len(sel):
        print("TEST outcome of train-selected configs: share with test_mean>0 = %.2f; median test mean = %.4f; "
              "n-weighted test mean = %.4f" % ((sel.test_mean > 0).mean(), sel.test_mean.median(),
                                                np.average(sel.test_mean.fillna(0), weights=sel.test_n.fillna(0) + 1e-9)))
        show = ["side", "band", "horizon", "category", "spread", "train_n", "train_n_events", "train_mean", "train_t_stat",
                "test_n", "test_n_events", "test_mean", "test_se", "test_t_stat", "test_hit", "test_avg_entry",
                "test_maker_fill", "test_maker_mean"]
        print(sel[show].head(60).round(4).to_string(index=False))


if __name__ == "__main__":
    main()
