"""Shared data prep, simulator and metrics for the Coinbase spot strategy study (PAPER/RESEARCH ONLY).

Conventions
- Daily bars are UTC days; ts = bar open.  A signal uses data up to the CLOSE of day t-1 and the
  trade fills at the OPEN of day t (the next bar).  Position returns are open-to-open.
- A product whose series ends (delisting / suspension > 3 days) gets a synthetic open on the day
  after its last bar equal to its last close; it is not eligible for new targets, so any holding
  is sold at that price.
- Costs per unit of one-way turnover = taker fee + per-product slippage (bps, from the book snapshot).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "data"))
from loader import load_book_snapshot, load_daily, load_products, load_universe  # noqa: E402

ANN = 365.0
TRAIN = ("2018-01-01", "2023-12-31")
TEST = ("2024-01-01", "2026-09-25")
DEFAULT_TAKER = 0.009
SLIP_FLOOR_BPS = 2.0
SLIP_FALLBACK_BPS = 20.0


class Data:
    def __init__(self):
        meta = load_products()
        excl = meta.loc[meta["is_stablecoin"].fillna(False).astype(bool)
                        | meta["is_pegged_derivative"].fillna(False).astype(bool), "product"]
        d = load_daily(fill_gaps=3)
        d = d[~d["product"].isin(set(excl))]
        d["product"] = d["product"].astype(str)
        self.close = d.pivot(index="ts", columns="product", values="close").sort_index()
        idx = pd.date_range(self.close.index[0], self.close.index[-1], freq="D")
        self.close = self.close.reindex(idx)
        self.open = d.pivot(index="ts", columns="product", values="open").reindex(idx)
        self.high = d.pivot(index="ts", columns="product", values="high").reindex(idx)
        self.low = d.pivot(index="ts", columns="product", values="low").reindex(idx)
        self.products = list(self.close.columns)
        self.idx = idx
        self.valid = self.close.notna()                      # real (or <=3d filled) bar exists
        # execution price: open, plus synthetic open = last close on the day after a run ends
        ex = self.open.copy()
        after_end = self.valid.shift(1, fill_value=False) & ~self.valid
        ex[after_end] = self.close.shift(1)[after_end]
        self.exec_px = ex
        nxt = ex.shift(-1)
        self.ret_oo = (nxt / ex - 1.0)                        # return of a position opened at open t
        self.ret_oo = self.ret_oo.where(self.ret_oo.notna(), 0.0)
        self.ret_cc = self.close.pct_change(fill_method=None)
        # point-in-time universe ranks (monthly, from trailing-30d volume before month start)
        u = load_universe()
        u = u[u["rank"].notna()]
        u["month"] = pd.to_datetime(u["month"], utc=True)
        rk = u.pivot(index="month", columns="product", values="rank").astype(float)
        rk = rk.reindex(columns=self.products)
        month_of = idx.to_period("M").to_timestamp().tz_localize("UTC")
        self.rank = rk.reindex(month_of).set_axis(idx)       # rank valid for trading on day t
        # slippage per product (bps)
        b = load_book_snapshot()
        cost = b.groupby("product")[["buy_cost_bps_10000", "sell_cost_bps_10000"]].median().mean(axis=1)
        slip = pd.Series(SLIP_FALLBACK_BPS, index=self.products)
        for p, v in cost.items():
            if p in slip.index and np.isfinite(v):
                slip[p] = max(SLIP_FLOOR_BPS, float(v))
        self.slip_bps = slip

    def in_top(self, n: int) -> pd.DataFrame:
        return (self.rank <= n) & self.valid


def simulate(target: pd.DataFrame, data: Data, taker: float = DEFAULT_TAKER, rebal: pd.Series | None = None,
             band: float | pd.DataFrame = 0.0, min_trade: float = 1e-3, dca: pd.Series | None = None):
    """Walk days; `target` row t = desired weights at the open of day t (decided at close t-1).

    Trades happen for an asset when: target 0 & held (exit), held 0 & target>0 (entry), or on a
    `rebal` day (or every day if rebal is None) when |w - target| > band.  NaN target = hold.
    Returns DataFrame(ret, ret_gross, cost, turnover, exposure).
    """
    cols = target.columns
    tgt = target.to_numpy(float)
    R = data.ret_oo[cols].to_numpy(float)
    ok = data.valid[cols].to_numpy(bool)
    cr = (taker + data.slip_bps[cols].to_numpy(float) / 1e4)
    T, P = tgt.shape
    rb = np.ones(T, bool) if rebal is None else rebal.reindex(target.index).fillna(False).to_numpy(bool)
    bandv = np.broadcast_to(np.asarray(band.to_numpy(float) if isinstance(band, pd.DataFrame) else band, float), (T, P))
    w = np.zeros(P)
    out = np.zeros((T, 5))
    for t in range(T):
        tg = tgt[t].copy()
        tg[~ok[t]] = 0.0                          # product without a bar today: cannot hold/enter
        hold = np.isnan(tg)
        tg[hold] = w[hold]
        d = tg - w
        trade = ((tg == 0) & (w > 0)) | ((w == 0) & (tg > 0)) | (rb[t] & (np.abs(d) > bandv[t]))
        trade &= (np.abs(d) >= min_trade) | (tg == 0)
        dw = np.where(trade, d, 0.0)
        # buys are paid from cash incl. fees: scale buys down so cash never goes negative
        buy = dw > 0
        if buy.any():
            s0 = float(np.sum(w + np.where(buy, 0.0, dw)))
            cs = float(np.sum(np.abs(np.where(buy, 0.0, dw)) * cr))
            B = float(dw[buy].sum()); cb = float(np.sum(dw[buy] * cr[buy]))
            f = min(1.0, max(0.0, (1.0 - s0 - cs) / (B + cb)))
            if f < 1.0:
                dw = np.where(buy, dw * f, dw)
        w = w + dw
        cost = float(np.sum(np.abs(dw) * cr))
        g = float(np.sum(w * R[t]))
        r = g - cost
        out[t] = (r, g, cost, np.abs(dw).sum(), w.sum())
        # drift
        cash = 1.0 - w.sum() - cost
        hv = w * (1.0 + R[t])
        eq = hv.sum() + cash
        w = hv / eq if eq > 0 else hv * 0
        w[w < 1e-12] = 0.0
    return pd.DataFrame(out, index=target.index, columns=["ret", "ret_gross", "cost", "turnover", "exposure"])


# ---------------------------------------------------------------- metrics
def _ts(x):
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def metrics(res: pd.DataFrame, start, end) -> dict:
    s = res.loc[_ts(start):_ts(end)]
    r = s["ret"].to_numpy()
    n = len(r)
    if n < 5:
        return {}
    yrs = n / ANN
    eq = np.cumprod(1 + r)
    cagr = eq[-1] ** (1 / yrs) - 1
    geq = np.prod(1 + s["ret_gross"].to_numpy())
    cagr_g = geq ** (1 / yrs) - 1
    vol = r.std(ddof=1) * np.sqrt(ANN)
    sharpe = r.mean() / r.std(ddof=1) * np.sqrt(ANN) if r.std() > 0 else np.nan
    dn = np.sqrt(np.mean(np.minimum(r, 0) ** 2))
    sortino = r.mean() / dn * np.sqrt(ANN) if dn > 0 else np.nan
    peak = np.maximum.accumulate(np.concatenate([[1.0], eq]))[1:]
    mdd = (eq / peak - 1).min()
    return dict(cagr=cagr * 100, cagr_gross=cagr_g * 100, fee_drag=(cagr_g - cagr) * 100,
                cost_pct_yr=s["cost"].sum() / yrs * 100, vol=vol * 100, sharpe=sharpe, sortino=sortino,
                mdd=mdd * 100, turnover_yr=s["turnover"].sum() / yrs, pct_invested=(s["exposure"] > 0.01).mean() * 100,
                avg_exposure=s["exposure"].mean() * 100, total_ret=(eq[-1] - 1) * 100, years=yrs)


def per_year(res: pd.DataFrame, years=range(2018, 2027)) -> dict:
    out = {}
    for y in years:
        s = res["ret"].loc[f"{y}-01-01":f"{y}-12-31"]
        if len(s):
            out[str(y)] = (np.prod(1 + s.to_numpy()) - 1) * 100
    return out


def block_bootstrap_excess(r_s: pd.Series, r_b: pd.Series, start, end, n_boot=4000, seed=7):
    """Monthly block bootstrap of annualised log-return difference (strategy - benchmark), %/yr."""
    a = r_s.loc[_ts(start):_ts(end)]
    b = r_b.reindex(a.index)
    x = np.log1p(a) - np.log1p(b)
    months = x.groupby(x.index.to_period("M"))
    blocks = [g.to_numpy() for _, g in months]
    sums = np.array([bl.sum() for bl in blocks])
    lens = np.array([len(bl) for bl in blocks])
    rng = np.random.default_rng(seed)
    k = len(blocks)
    pick = rng.integers(0, k, size=(n_boot, k))
    ann = sums[pick].sum(1) / lens[pick].sum(1) * ANN
    point = sums.sum() / lens.sum() * ANN
    lo, hi = np.percentile(ann, [2.5, 97.5])
    # convert log excess to "CAGR ratio" percent: exp(x)-1
    f = lambda v: (np.exp(v) - 1) * 100
    return dict(excess=f(point), ci_lo=f(lo), ci_hi=f(hi), p_gt0=float((ann > 0).mean()))


# ---------------------------------------------------------------- signals
def sma(c, n):
    return c.rolling(n, min_periods=n).mean()


def ema(c, n):
    return c.ewm(span=n, adjust=False, min_periods=n).mean()


def hysteresis(enter: pd.DataFrame, exit_: pd.DataFrame, avail: pd.DataFrame) -> pd.DataFrame:
    """State machine per column: 1 after enter, 0 after exit, hold otherwise; 0 when unavailable."""
    E = enter.to_numpy(bool); X = exit_.to_numpy(bool); A = avail.to_numpy(bool)
    st = np.zeros(E.shape)
    cur = np.zeros(E.shape[1])
    for t in range(E.shape[0]):
        cur = np.where(E[t], 1.0, np.where(X[t], 0.0, cur))
        cur = np.where(A[t], cur, 0.0)
        st[t] = cur
    return pd.DataFrame(st, index=enter.index, columns=enter.columns)


def trend_state(data: Data, cols, rule: str, p) -> pd.DataFrame:
    """1/0 state known at CLOSE of day t (index t).  Caller shifts by 1 to trade at open t+1."""
    c = data.close[cols]
    avail = c.notna()
    if rule in ("sma", "ema"):
        n, b = p
        m = sma(c, n) if rule == "sma" else ema(c, n)
        return hysteresis(c > m * (1 + b), c < m * (1 - b), avail & m.notna())
    if rule == "donchian":
        ne, nx = p
        hi = data.high[cols].rolling(ne, min_periods=ne).max().shift(1)
        lo = data.low[cols].rolling(nx, min_periods=nx).min().shift(1)
        return hysteresis(c > hi, c < lo, avail & hi.notna())
    if rule == "dualma":
        f, s = p
        a, b_ = sma(c, f), sma(c, s)
        return ((a > b_) & b_.notna() & avail).astype(float)
    if rule == "always":
        return avail.astype(float)
    raise ValueError(rule)
