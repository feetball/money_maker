"""Reference live signal for the 'alt 15-min stacked fair value' candidate (paper trading only).

For each series in CONFIG: find the currently open 15-min market, pull Coinbase 1-min candles
(for EWMA vol) + Coinbase ticker (spot), Kalshi top of book, then compute
  p_model = P(avg_T >= K) under zero-drift Student-t with EWMA vol
  q       = sigmoid(a + b*logit(mid) + c*logit(p_model))      (stacked, coefs from walk-forward fit)
  edge_yes = q - yes_ask - fee(yes_ask) ; edge_no = (1-q) - no_ask - fee(no_ask)
and emit BUY_YES / BUY_NO when edge > THR and the rule filters pass.

Usage: live_signal.py            (prints one JSON line per series)
"""
import datetime as dt
import json
import math
import time

import httpx
import numpy as np

from fv_model import fee_per_contract, prob_above

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
# series -> (coinbase product, EWMA half-life min, student-t nu, vol multiplier, a, b_mid, c_model)
CONFIG = {
    "KXDOGE15M": ("DOGE-USD", 10, 3.5, 1.1, -0.005, 0.619, 0.458),
    "KXSOL15M": ("SOL-USD", 30, 5.0, 1.0, -0.013, 0.642, 0.412),
    "KXXRP15M": ("XRP-USD", 10, 3.5, 1.1, 0.074, 0.800, 0.273),
}
THR = 0.02            # min edge after fee, $/contract
LAG_MIN, LAG_MAX = 5.0, 12.5   # minutes to close allowed
PX_MIN, PX_MAX = 0.10, 0.90    # only buy contracts priced in this band
BASIS_BPS = 1.0       # BRTI-style settlement minus Coinbase, median ~+1 bp for alts (from settled data)

cl = httpx.Client(timeout=10, headers={"User-Agent": "paper-research/0.1"})


def _get(url, params=None):
    for i in range(4):
        r = cl.get(url, params=params)
        if r.status_code == 200:
            return r.json()
        time.sleep(0.5 * (2 ** i))
    raise RuntimeError(f"{url} -> {r.status_code}")


def ewma_var_per_min(product, half_life):
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(minutes=299)
    rows = _get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                {"granularity": 60, "start": start.isoformat(), "end": end.isoformat()})
    closes = np.array([r[4] for r in sorted(rows, key=lambda r: r[0])], float)
    r = np.diff(np.log(closes))
    lam = 0.5 ** (1.0 / half_life)
    w = lam ** np.arange(len(r))[::-1]
    return float(np.sum(w * r ** 2) / np.sum(w))


def signal(series):
    prod, hl, nu, vm, a, b, c = CONFIG[series]
    ms = _get(KALSHI + "/markets", {"series_ticker": series, "status": "open", "limit": 10})["markets"]
    m = sorted(ms, key=lambda x: x["close_time"])[0]
    close = dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp()
    now = time.time()
    lag = (close - now) / 60.0
    K = m.get("floor_strike")
    ob = _get(KALSHI + f"/markets/{m['ticker']}/orderbook", {"depth": 1}).get("orderbook_fp") or {}
    yes, no = ob.get("yes_dollars") or [], ob.get("no_dollars") or []
    out = {"series": series, "ticker": m["ticker"], "minutes_to_close": round(lag, 2), "strike": K, "action": "NONE"}
    if K is None or not yes or not no:
        out["reason"] = "strike not set or one-sided book"
        return out
    yes_bid, yes_bid_sz = float(yes[-1][0]), float(yes[-1][1])
    no_bid, no_bid_sz = float(no[-1][0]), float(no[-1][1])
    yes_ask, no_ask = 1 - no_bid, 1 - yes_bid
    spot = float(_get(f"https://api.exchange.coinbase.com/products/{prod}/ticker")["price"])
    var = ewma_var_per_min(prod, hl)
    tau = max(lag - 2.0 / 3.0, 1.0 / 3.0)
    p = float(prob_above(spot, K, math.sqrt(var * tau) * vm, nu=nu, basis=spot * BASIS_BPS / 1e4))
    mid = (yes_bid + yes_ask) / 2
    lg = lambda x: math.log(min(max(x, 1e-3), 1 - 1e-3) / (1 - min(max(x, 1e-3), 1 - 1e-3)))
    q = 1 / (1 + math.exp(-(a + b * lg(mid) + c * lg(p))))
    e_yes = q - yes_ask - float(fee_per_contract(yes_ask))
    e_no = (1 - q) - no_ask - float(fee_per_contract(no_ask))
    out.update(spot=spot, p_model=round(p, 4), q_stacked=round(q, 4), yes_bid=yes_bid, yes_ask=round(yes_ask, 4),
               edge_yes=round(e_yes, 4), edge_no=round(e_no, 4), ask_size_yes=no_bid_sz, ask_size_no=yes_bid_sz)
    if not (LAG_MIN <= lag <= LAG_MAX):
        out["reason"] = "outside decision window"
    elif e_yes > THR and e_yes >= e_no and PX_MIN <= yes_ask <= PX_MAX:
        out.update(action="BUY_YES", limit_price=round(yes_ask, 4), max_size=no_bid_sz)
    elif e_no > THR and PX_MIN <= no_ask <= PX_MAX:
        out.update(action="BUY_NO", limit_price=round(no_ask, 4), max_size=yes_bid_sz)
    return out


if __name__ == "__main__":
    for s in CONFIG:
        try:
            print(json.dumps(signal(s)))
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"series": s, "error": str(e)}))
