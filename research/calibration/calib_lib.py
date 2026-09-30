"""Shared helpers for the walk-forward calibration / strategy study (research/calibration).

Design rules (see REPORT.md):
  * Decisions happen on a fixed calendar grid (every hour on the hour, UTC). A market is in the decision set at
    time t iff open_time <= t < close_time (both observable at t: you can see a market is still open).
  * The quote used at t is the close of the last hourly candle whose period ENDED at or before t (candles are
    omitted when nothing changed, so forward-filling is exact). Nothing after t is used for the decision.
  * Time-to-resolution features use expected_expiration_time (EET), which is set ex ante (round values, ~0% equal to
    the actual close; see REPORT.md), never the actual close_time.
  * Series whose close timing depends on the outcome ("will X happen before D" style, most Mentions/Elections)
    are excluded: in those, sampling by close day and the 14-day candle window anchored on the actual close would
    leak the outcome.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
sys.path.insert(0, str(DATA))
from loader import load_markets  # noqa: E402

DATA_END = pd.Timestamp("2026-09-26 00:00", tz="UTC")
PANEL_FILE = HERE / "panel_hourly.parquet"


# --------------------------------------------------------------------------------------------- fees
def fee_per_contract(p, contracts=100, mult=1.0, coef=0.07, rounding="order"):
    """Kalshi quadratic fee per contract (dollars). rounding='order': ceil to the cent of the whole order of
    `contracts` (upper bound of the documented per-order accumulator scheme for non-direct members);
    rounding='none': exact 0.07*P*(1-P) (lower bound, ~what a direct member pays)."""
    p = np.asarray(p, dtype="float64")
    mult = np.asarray(mult, dtype="float64")
    raw = coef * mult * contracts * p * (1 - p)
    if rounding == "none":
        return raw / contracts
    return np.ceil(np.round(raw * 100, 9)) / 100 / contracts


# --------------------------------------------------------------------------------------------- universe
def series_flags(mk: pd.DataFrame) -> pd.DataFrame:
    """Per-series classification of close-timing mechanics (uses all sampled markets of the series).
    outcome_dep = close time relative to the ex-ante EET differs by >12h between YES and NO markets, or markets of
    the same event close >12h apart (90th pct) -> close timing reveals/depends on the outcome."""
    d = (mk["expected_expiration_time"] - mk["close_time"]).dt.total_seconds() / 3600
    x = mk.assign(d=d)
    g = x.groupby("series_ticker")
    s = pd.DataFrame({"category": g["category"].first(), "n": g.size(), "d_med": g["d"].median(),
                      "far_early": g["d"].apply(lambda v: (v > 24).mean())})
    s["d_yes_minus_no"] = x[x.y == 1].groupby("series_ticker")["d"].mean() - x[x.y == 0].groupby("series_ticker")["d"].mean()
    ev = x.groupby("event_ticker").agg(ser=("series_ticker", "first"), n=("ticker", "size"),
                                       spread=("close_time", lambda v: (v.max() - v.min()).total_seconds() / 3600))
    s["ev_close_spread_p90"] = ev[ev.n > 1].groupby("ser")["spread"].quantile(0.9)
    s["outcome_dep"] = (s["d_yes_minus_no"].abs() > 12) | (s["ev_close_spread_p90"] > 12)
    return s


def universe() -> pd.DataFrame:
    mk = load_markets()
    fl = series_flags(mk)
    mk = mk.merge(fl[["outcome_dep"]], left_on="series_ticker", right_index=True, how="left")
    mk["outcome_dep"] = mk["outcome_dep"].fillna(False).astype(bool)
    mk["in_scope"] = (~mk["outcome_dep"]) & (mk["expected_expiration_time"] < DATA_END)
    return mk


def load_candles_all() -> pd.DataFrame:
    a = pd.read_parquet(DATA / "candles_hourly.parquet")
    parts = [a]
    f = HERE / "candles_fill_hourly.parquet"
    if f.exists():
        parts.append(pd.read_parquet(f))
    c = pd.concat(parts, ignore_index=True)
    c = c.drop_duplicates(["ticker", "end_period_ts"]).sort_values(["ticker", "end_period_ts"]).reset_index(drop=True)
    return c


# --------------------------------------------------------------------------------------------- panel
def build_panel(mk: pd.DataFrame, c: pd.DataFrame, step: int = 3600) -> pd.DataFrame:
    """Hourly decision panel: one row per (market, t) with t on the UTC hour grid, open_time <= t < close_time and
    at least one candle ended <= t. Quote = last candle close with end_period_ts <= t.
    fut_low / fut_high = min / max traded YES price in candles that END AFTER t (i.e. trades after t, until the
    end of the candle window ~ close) -> used only for the optimistic maker-fill model, never for decisions."""
    c = c[c["ticker"].isin(mk["ticker"])].copy()
    c = c.sort_values(["ticker", "end_period_ts"]).reset_index(drop=True)
    # suffix min/max of trade prices over LATER candles (exclusive of the current one)
    c["fut_low"] = _suffix(c, "price_low", "min")
    c["fut_high"] = _suffix(c, "price_high", "max")
    c["cumvol"] = c.groupby("ticker")["volume"].cumsum()
    first = c.groupby("ticker")["end_period_ts"].min()
    m = mk.set_index("ticker").loc[first.index]
    open_ts = m["open_time"].dt.as_unit("s").astype("int64").to_numpy()
    close_ts = m["close_time"].dt.as_unit("s").astype("int64").to_numpy()
    start = np.maximum(first.to_numpy(), -(-open_ts // step) * step)          # first grid point >= open with data
    start = -(-start // step) * step
    n = np.maximum(0, (close_ts - 1 - start) // step + 1)                     # t < close_time
    tick = np.repeat(first.index.to_numpy(), n)
    off = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)
    t = np.repeat(start, n) + off * step
    grid = pd.DataFrame({"ticker": tick, "t": t.astype("int64")})
    cols = ["ticker", "end_period_ts", "yes_bid_close", "yes_ask_close", "price_close", "price_previous",
            "open_interest", "cumvol", "fut_low", "fut_high"]
    cc = c[cols].rename(columns={"end_period_ts": "t"})
    grid = grid.sort_values(["t"]).reset_index(drop=True)
    cc = cc.sort_values(["t"]).reset_index(drop=True)
    p = pd.merge_asof(grid, cc, on="t", by="ticker", direction="backward", allow_exact_matches=True)
    p = p.rename(columns={"yes_bid_close": "bid", "yes_ask_close": "ask"})
    p = p.dropna(subset=["bid", "ask"])
    return p


def _suffix(c: pd.DataFrame, col: str, how: str) -> np.ndarray:
    """For each candle row: min/max of `col` over the LATER rows of the same ticker (NaN if none). c is sorted by
    (ticker, end_period_ts)."""
    rev = c[["ticker", col]].iloc[::-1]
    g = rev.groupby("ticker", sort=False)[col]
    inc = g.cummin() if how == "min" else g.cummax()          # inclusive suffix, NaN at NaN rows
    inc = inc.groupby(rev["ticker"], sort=False).ffill()       # carry running extreme over NaN rows
    exc = inc.groupby(rev["ticker"], sort=False).shift(1)      # exclusive: only strictly later candles
    return exc.iloc[::-1].to_numpy()


def attach_market_cols(p: pd.DataFrame, mk: pd.DataFrame) -> pd.DataFrame:
    cols = ["ticker", "event_ticker", "series_ticker", "category", "y", "expected_expiration_time", "close_time",
            "settlement_ts", "fee_multiplier", "fee_type", "can_close_early", "source", "in_day_sample",
            "event_fully_covered", "open_time", "min_tick"]
    cols += [c for c in ("event_all_candles",) if c in mk.columns]
    p = p.merge(mk[cols], on="ticker", how="left")
    tt = pd.to_datetime(p["t"], unit="s", utc=True)
    p["h_to_eet"] = (p["expected_expiration_time"] - tt).dt.total_seconds() / 3600
    p["h_since_open"] = (tt - p["open_time"]).dt.total_seconds() / 3600
    p["month"] = tt.dt.strftime("%Y-%m")
    return p


# --------------------------------------------------------------------------------------------- stats
def cluster_boot(df: pd.DataFrame, value_cols: list[str], cluster: str = "event_ticker", B: int = 1000,
                 seed: int = 0, weight_col: str | None = None) -> dict:
    """Poisson (multiplier) bootstrap of means of value_cols with resampling by cluster.
    Returns {col: (mean, lo95, hi95, se)}."""
    rng = np.random.default_rng(seed)
    codes, uniq = pd.factorize(df[cluster])
    k = len(uniq)
    w0 = df[weight_col].to_numpy(dtype="float64") if weight_col else np.ones(len(df))
    cnt = np.bincount(codes, weights=w0, minlength=k)
    out = {}
    W = rng.poisson(1.0, size=(B, k)).astype("float64")
    den = W @ cnt
    for col in value_cols:
        s = np.bincount(codes, weights=w0 * df[col].to_numpy(dtype="float64"), minlength=k)
        est = s.sum() / cnt.sum()
        bs = (W @ s) / np.where(den > 0, den, np.nan)
        lo, hi = np.nanpercentile(bs, [2.5, 97.5])
        out[col] = (est, lo, hi, np.nanstd(bs))
    return out


def cluster_se(values: np.ndarray, clusters: np.ndarray) -> tuple[float, float, int]:
    """Mean and cluster-robust SE (sum within cluster, CR0)."""
    v = np.asarray(values, dtype="float64")
    codes, uniq = pd.factorize(clusters)
    n, G = len(v), len(uniq)
    mu = v.mean()
    s = np.bincount(codes, weights=v - mu, minlength=G)
    var = (s ** 2).sum() / n ** 2 * (G / max(G - 1, 1))
    return mu, float(np.sqrt(var)), G


PRICE_BUCKETS = [0.0, 0.01, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70,
                 0.75, 0.80, 0.85, 0.90, 0.95, 0.99, 1.0001]
H_BUCKETS = [-1e9, 0, 1, 3, 6, 12, 24, 48, 96, 168, 1e9]
H_LABELS = ["<0 (past EET)", "0-1h", "1-3h", "3-6h", "6-12h", "12-24h", "1-2d", "2-4d", "4-7d", ">7d"]
