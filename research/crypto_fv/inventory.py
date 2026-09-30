"""Live inventory of crypto / index price series: cadence, strike structure, settlement text,
nearest-expiry liquidity (spread, top size, depth within 3c, 24h volume) from live orderbooks.
Writes results/inventory_live.csv. Uncached live snapshot (rate ~1.4 req/s)."""
import datetime as dt, json, os, sys, time
import numpy as np, pandas as pd
import kclient as k
k.MIN_INTERVAL = 0.7
SERIES = ["KXBTCD", "KXBTC", "KXBTC15M", "KXETHD", "KXETH", "KXETH15M", "KXSOLD", "KXSOL", "KXSOL15M",
          "KXXRPD", "KXXRP", "KXXRP15M", "KXDOGED", "KXDOGE", "KXDOGE15M", "KXBTCMAXD",
          "KXINXU", "KXINX", "KXINXI", "KXINX15M", "KXINXZ", "KXNASDAQ100U", "KXNASDAQ100", "KXNASDAQ100Z"]
now = time.time()
ts = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
fl = lambda x: float(x) if x not in (None, "") else np.nan
rows = []
for s in SERIES:
    try:
        ser = k.get(f"/series/{s}", cache=True)["series"]
    except Exception as e:
        print(s, "series err", e); continue
    ms = k.paginate("/markets", {"series_ticker": s, "status": "open", "limit": 1000, "mve_filter": "exclude"}, "markets", cache=False, max_pages=5)
    if not ms:
        rows.append(dict(series=s, frequency=ser.get("frequency"), n_open=0)); print(s, "no open markets"); continue
    df = pd.DataFrame(ms)
    df["close"] = df.close_time.map(ts)
    evs = df.groupby("event_ticker").close.min().sort_values()
    evs = evs[evs > now + 120]
    if evs.empty: continue
    ev = evs.index[0]
    e = df[df.event_ticker == ev].copy()
    for c in ["yes_bid_dollars", "yes_ask_dollars", "volume_24h_fp", "volume_fp", "open_interest_fp"]:
        e[c] = e[c].map(fl)
    e["mid"] = (e.yes_bid_dollars + e.yes_ask_dollars) / 2
    strikes = np.sort(pd.to_numeric(e.get("floor_strike", pd.Series(dtype=float)), errors="coerce").dropna().unique())
    spacing = np.median(np.diff(strikes)) if len(strikes) > 2 else np.nan
    # near-the-money: 3 markets with mid closest to 0.5 (for ranges: highest mid)
    e["ntm"] = (e.mid - 0.5).abs() if not (e.get("strike_type", pd.Series(dtype=str)) == "between").any() else -e.mid
    sel = e.sort_values("ntm").head(3)
    for _, m in sel.iterrows():
        ob = k.get(f"/markets/{m.ticker}/orderbook", {"depth": 30}, cache=False).get("orderbook_fp") or {}
        yb = [(float(p), float(q)) for p, q in (ob.get("yes_dollars") or [])]
        nb = [(float(p), float(q)) for p, q in (ob.get("no_dollars") or [])]
        byb = yb[-1][0] if yb else 0.0; bnb = nb[-1][0] if nb else 0.0
        yask = 1 - bnb
        rows.append(dict(series=s, frequency=ser.get("frequency"), fee_type=ser.get("fee_type"), fee_mult=ser.get("fee_multiplier"),
                         settle_src=";".join(x.get("name", "") for x in ser.get("settlement_sources") or []),
                         event=ev, mins_to_close=round((m.close - now) / 60, 1), n_mkts_event=len(e),
                         strike_types=",".join(sorted(e.get("strike_type", pd.Series(dtype=str)).dropna().unique())), strike_spacing=spacing,
                         pls=m.price_level_structure, ticker=m.ticker, floor=m.get("floor_strike"), cap=m.get("cap_strike"),
                         yes_bid=byb, yes_ask=yask, spread_c=round(100 * (yask - byb), 2),
                         bid_sz=yb[-1][1] if yb else 0, ask_sz=nb[-1][1] if nb else 0,
                         yes_depth3c=sum(q for p, q in yb if p >= byb - 0.03), no_depth3c=sum(q for p, q in nb if p >= bnb - 0.03),
                         vol24h_mkt=m.volume_24h_fp, vol24h_event=e.volume_24h_fp.sum(), oi_event=e.open_interest_fp.sum(),
                         rules=m.get("rules_primary", "")[:220]))
    print(s, ev, "mkts", len(e), "spacing", spacing, "vol24h_event", e.volume_24h_fp.sum())
R = pd.DataFrame(rows)
R.to_csv("results/inventory_live.csv", index=False)
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
print(R.drop(columns=["rules", "settle_src"]).to_string(index=False))
