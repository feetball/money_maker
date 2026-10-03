"""BTC 15-minute favourite at 10 minutes to close (KXBTC15M). The PRIMARY research strategy.

The only rule in ``research/FINDINGS.md`` that replicated out of sample. Research:
``research/crypto_fv/fav15.py`` + ``backtest.py`` + ``fv_model.py`` (the frozen rule: lag 10,
band [0.85, 0.97], threshold 1c, ``add_model(p, "hl10", 3.5, 1.1)``, ``exec_next=True``), re-checked in
``verify_stats/VERDICT.md`` and ``verify_leakage/VERDICT.md``:

=====================================  ======  =========================================
sample                                 trades  c/contract after fees (95% CI)
=====================================  ======  =========================================
in-sample Jul 19 - Sep 26              555     +3.7 (+1.4 .. +5.8, day blocks)
untouched holdout Jun 21 - Jul 18      203     +4.9 (+1.7 .. +7.8), 5/5 weeks positive
blind favourite, no model (holdout)    485     +3.5 (+1.2 .. +5.7)
=====================================  ======  =========================================

Plan on about +2..+3c forward, 7-8 trades a day. Timing-sensitive: lags 7 and 11 min are about
zero or negative, so **do not re-tune the timing, band or model** (forward-test it frozen).

Rule (one decision per 15-minute window)
----------------------------------------
* **Market**: series ``KXBTC15M`` ("BTC price up in next 15 mins?"): YES iff the CF Benchmarks
  BRTI 60-second average at close is >= ``floor_strike`` (the previous window's settlement
  value; ``strike_type`` ``greater_or_equal``).
* **When**: the first tick with ``entry_minutes_min < minutes_to_close <= entry_minutes_max``
  (default 9.75 < m <= 10). The edge is a sharp peak at exactly 10:00 (research lag profile: lag
  10 +3.7 / +4.9c, lag 9 +1.8 / +2.1c, lag 11 +1.4 / -0.8c; a one-minute-old quote +1.4 / +0.7c),
  so the strategy ticks every ``tick_interval_s`` = 5 s (decisions land in (9.92, 10]) and a
  decision later than 15 s is skipped rather than traded. ``ctx.now`` is frozen at the tick start,
  so the deadline is checked again after the model and book reads against the engine's real clock
  (``ctx.clock()``, unrounded seconds): a decision whose network reads ran past the limit is
  skipped with ``skip="late"`` (backtest contexts have no clock, so replay results are unchanged).
  Every intent and skip carries the decision lag (``lag_s`` = real seconds after the 10:00 mark)
  so forward results can be split by it.
  New windows are picked up by a series refresh every 20 s (``UniverseSpec.refresh_s``), and a
  minute before the decision the window's event is fetched (``ctx.event``) so the order's fee
  lookup is a cache hit.
* **Inputs, in order**: the model inputs first (settled windows, Coinbase candles, spot last),
  then the order book, fetched **after** the spot (``ctx.orderbook(t, max_age_s=0)`` where the
  context supports it), so the book the decision uses is never older than the spot it is
  compared with (Kalshi absorbs spot moves in about 1.5 s). The spot is fetched fresh too
  (``spot("BTC", max_age_s=0)``, not the feed's 5 s cache): after a sharp move a 5 s old spot
  makes a favourite that just weakened look cheap, an adverse selection the research (same-second
  candles) did not have. A feed whose ``spot`` takes no ``max_age_s`` is called plainly.
* **Side**: a side is a candidate when its best ask is inside ``[price_min, price_max]``
  (default [0.85, 0.97], inclusive): YES at the YES ask, NO at the NO ask (= 1 - YES bid).
* **Model** (``use_model``, default on): P(YES) = 1 - F((ln(K - basis) - ln S) / sigma), F = the
  Student-t CDF with 3.5 d.f. rescaled to unit variance, sigma = 1.1 * sqrt(v * tau):

  - S: Coinbase BTC-USD spot (last trade);
  - v: EWMA (half-life 10 min, pandas ``adjust=True``) of squared 1-minute log returns of
    Coinbase candle closes on a forward-filled minute grid (complete bars only, last 120 min);
  - tau = minutes to close - 2/3 (settlement averages the final minute), floored at 1/3;
  - K: ``floor_strike`` of the market;
  - basis: median of (``expiration_value`` - Coinbase final-minute mid) over the last 48
    settled KXBTC15M windows that closed **before this window opened** (at least 10); the
    final-minute mid is (open + close) / 2 of the Coinbase bar ending at the window's close.

  A candidate side qualifies when ``P(side) - ask - fee/contract >= min_model_edge``
  (default 0.01), with the fee of the actual order (``ctx.fee`` at its count). YES wins when
  both qualify and its edge is at least NO's (research ``trades()``); in practice only one side
  can be in the band.
* **Blind mode** (``use_model`` off): buy the in-band side (YES if both and the mid >= 0.5).
* **Order**: buy IOC at the ask (+ ``slippage_ticks`` grid ticks), hold to settlement (no exits).
* **Size** (``sizing``): ``kelly`` = ``risk.kelly_count(P(side), ask, equity, kelly_fraction)``
  (at least ``min_contracts``); ``fixed`` = ``contracts``. Then at most ``max_contracts`` and at
  most ``max_cost_per_trade`` dollars including the fee. Fewer than ``min_contracts`` (10;
  1-lots lose to fee rounding) -> skip.

Every intent carries ``reason``, ``fair_value`` (model P(side), None in blind mode without spot
data) and ``expected_edge`` (P(side) - limit - fee/contract; in blind mode without a model the
research's blind estimate ``blind_prior_edge``). Every skipped window is logged with its reason
(``ctx.log``): out of band, model disagrees, spot data missing/stale, already held, size.

A window is **decided once**: after a trade or a definite skip (band, model, size, already held)
it is never revisited, even if the IOC fills nothing or risk rejects it. Only missing inputs
(order book, spot data, basis) leave it undecided so the next tick *inside the same window*
retries. It never enters a window it already holds or has an open order in
(``ctx.portfolio``), and the decided windows survive a restart (``dump_state``).

Ambiguities resolved (vs the research backtest)
-----------------------------------------------
* Timing: the research decided on the close-10:00 quote and spot and filled at the next
  minute's (close-9:00) ask. Live we decide on a fresh book within 15 s after the 10:00 mark,
  with the freshest spot and the actual minutes left in tau, and the IOC reaches the paper
  exchange ``paper.taker_latency_s`` later at whatever the book then shows (never the decision
  book). The live-equivalent backtest is ``--fill same`` (decide and fill on the 10:00 quote:
  +3.52c in-sample, +5.46c holdout); the default minute replay ``next_ask`` (the ask 60 s later,
  whatever it is) is the research's conservative convention.
* Spot is the Coinbase last trade (the research used the 1-minute candle close, i.e. the last
  trade of the minute); vol uses complete bars only.
* The research's ``rolling(48).median().shift(1)`` as-of join drops the window that closed at
  this window's open; so does this rule (``close_time < open_time``). With fewer than 10 basis
  windows the research used basis 0; this rule **skips** (conservative). Expiration values with
  thousands separators (``"79,604.96"``, 2 of 6,632 in the research data) are parsed rather
  than dropped.
* The research compared the edge with an amortized fee 0.07 P (1-P) and ``> 0.01``; here the
  order's cent-rounded fee and ``>= min_model_edge`` (identical in practice, slightly stricter).
* Model inputs must come from Coinbase (the research's source and the basis's reference). A
  Kraken fallback quote/candle, stale data, a missing feed or too little history -> skip.

Feed interface (``ctx.feeds``; live and backtest)
--------------------------------------------------
``ctx.feeds["crypto"]`` (live :class:`~kalshibot.feeds.crypto.CryptoSpotFeed`, backtest
:class:`~kalshibot.feeds.replay.ReplayCryptoFeed`):

* ``await spot("BTC") -> SpotQuote``: ``.price`` (last trade, float), ``.ts`` (time of that
  trade / end of the replayed bar), ``.fetched_at``, ``.source`` (must be ``"coinbase"``).
  Stale when ``now - ts`` or ``now - fetched_at`` > ``max_spot_age_s`` (60 s).
* ``await candles("BTC", minutes) -> list[SpotCandle]``, oldest first, 1-minute bars: ``.ts`` =
  bar **start**, ``.end`` = start + 60 s, ``.open``, ``.close``, ``.complete``, ``.source``. Only
  complete bars with ``end <= now`` are used; stale when the newest ended more than
  ``max_candle_age_s`` (180 s) ago. The strategy asks for 120 minutes, or 900 when its cache of
  final-minute mids for the basis lacks older windows (cold start, downtime).

``ctx.feeds["kalshi_settled"]`` (live :class:`~kalshibot.feeds.kalshi_settled.KalshiSettledFeed`,
backtest :class:`~kalshibot.feeds.replay.ReplaySettledFeed`):

* ``await settled_markets("KXBTC15M", since=datetime) -> list[Market]``: finalized markets
  (``close_time``, ``expiration_value``, ``settlement_ts``), settled at or after ``since``.

A backtest context registers the replay feeds with ``clock=lambda: ctx.now``, which serves
only bars that ended by ``now`` and markets settled by ``now`` (no look-ahead). To mirror the
research, snapshot each window at exactly close - 10:00 (spot = the bar ending then) and fill
at the next minute's ask. Requests live, per window: 2 Coinbase + 1 Kalshi (settled windows) +
1 book (fetched after the spot).
"""

from __future__ import annotations

import inspect
import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import TYPE_CHECKING, Any, ClassVar

from kalshibot.money import ONE, ZERO, D, next_price_up
from kalshibot.risk import kelly_count
from kalshibot.strategies.base import OrderIntent, Strategy, StrategyContext, UniverseSpec

if TYPE_CHECKING:
    from kalshibot.kalshi.models import Market

__all__ = [
    "Btc15mFavorite",
    "ModelUnavailable",
    "ModelView",
    "ewma_variance",
    "final_minute_mid",
    "horizon_minutes",
    "minute_grid",
    "parse_expiration_value",
    "prob_above",
    "rolling_basis",
    "student_t_cdf",
    "unit_t_cdf",
]

SERIES = "KXBTC15M"
SYMBOL = "BTC"
SPOT_FEED = "crypto"
SETTLED_FEED = "kalshi_settled"
WINDOW = timedelta(minutes=15)

# --- the frozen research model (research/crypto_fv/fav15.py: add_model(p, "hl10", 3.5, 1.1)) ---
T_DOF = 3.5
VOL_MULT = 1.1
VOL_HALFLIFE_MIN = 10.0
#: settlement is the 60 s average ending at close: its variance over the final minute is 1/3 of
#: a point price, so the effective horizon is minutes_left - 2/3 (fv_model.horizon_minutes_eff)
SETTLE_AVG_OFFSET_MIN = 2.0 / 3.0
MIN_HORIZON_MIN = 1.0 / 3.0
BASIS_EVENTS = 48
BASIS_MIN_EVENTS = 10  # pandas min_periods in the research
#: minutes of 1-minute bars for the EWMA. The research ran the EWMA over months of history;
#: the weight beyond 120 min is 0.5**12, so 120 bars reproduce it to ~1e-4.
VOL_LOOKBACK_MIN = 120
#: fewer returns than this (e.g. right after a feed outage) -> the truncated EWMA is too noisy
VOL_MIN_RETURNS = 60
#: bars fetched when the basis cache is missing final-minute mids (cold start): 3 Coinbase pages
BASIS_HISTORY_MIN = 900
#: settled windows requested from the settled-markets feed (48 + slack for gaps)
SETTLED_LOOKBACK = BASIS_EVENTS + 8
#: final-minute bar missing -> forward-fill the previous close only if it is at most this old
MAX_FFILL_S = 300
STATE_TTL = timedelta(days=2)


# --------------------------------------------------------------------------- model math


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 500):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_bt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    bt = math.exp(ln_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def student_t_cdf(t: float, nu: float) -> float:
    """CDF of Student's t with ``nu`` (> 0, may be fractional) degrees of freedom."""
    if math.isnan(t):
        return math.nan
    if math.isinf(t):
        return 1.0 if t > 0 else 0.0
    tail = 0.5 * _betai(nu / 2.0, 0.5, nu / (nu + t * t))
    return 1.0 - tail if t > 0 else tail


def unit_t_cdf(z: float, nu: float | None) -> float:
    """CDF of a Student-t rescaled to unit variance (normal when ``nu`` is None/inf); needs nu > 2."""
    if nu is None or math.isinf(nu):
        return 0.5 * math.erfc(-z / math.sqrt(2.0))
    return student_t_cdf(z / math.sqrt((nu - 2.0) / nu), nu)


def prob_above(spot: float, strike: float, sigma_tot: float, nu: float | None = T_DOF,
               basis: float = 0.0) -> float:
    """P(S_T + basis > K) with ln(S_T / S) ~ sigma_tot * Z, zero drift (research ``fv_model.prob_above``)."""
    sig = max(float(sigma_tot), 1e-9)
    z = (math.log(max(strike - basis, 1e-9)) - math.log(spot)) / sig
    return 1.0 - unit_t_cdf(z, nu)


def horizon_minutes(minutes_left: float) -> float:
    """Effective horizon for a 60 s-average settlement (research ``horizon_minutes_eff``)."""
    return max(float(minutes_left) - SETTLE_AVG_OFFSET_MIN, MIN_HORIZON_MIN)


def minute_grid(candles: Iterable[Any]) -> list[tuple[int, float]]:
    """``[(bar end epoch s, close)]`` on a regular 1-minute grid, gaps forward-filled (research
    ``load_spot``: ``reindex`` + ``close.ffill()``). ``candles``: objects with ``ts`` (bar start)
    and ``close``."""
    closes: dict[int, float] = {}
    for c in candles:
        end = int(c.ts.timestamp()) + 60
        closes[end - end % 60] = float(c.close)
    if not closes:
        return []
    ends = sorted(closes)
    out: list[tuple[int, float]] = []
    last = closes[ends[0]]
    for t in range(ends[0], ends[-1] + 60, 60):
        last = closes.get(t, last)
        out.append((t, last))
    return out


def ewma_variance(closes: Sequence[float], halflife: float = VOL_HALFLIFE_MIN,
                  min_periods: int = VOL_MIN_RETURNS) -> float | None:
    """EWMA of squared log returns of consecutive ``closes`` (pandas ``ewm(halflife).mean()``,
    ``adjust=True``) at the last point; None with fewer than ``min_periods`` returns."""
    rs = [math.log(b / a) for a, b in pairwise(closes) if a > 0 and b > 0]
    if len(rs) < max(1, min_periods):
        return None
    w = 0.5 ** (1.0 / halflife)  # 1 - alpha, alpha = 1 - exp(-ln 2 / halflife)
    num = den = 0.0
    wi = 1.0
    for r in reversed(rs):
        num += wi * r * r
        den += wi
        wi *= w
    return num / den


def final_minute_mid(candles_by_end: Mapping[int, Any], close_epoch: int) -> float | None:
    """Coinbase mid of the settlement minute: (open + close) / 2 of the bar ending at ``close_epoch``.

    If that bar is missing (no trades that minute) but later bars exist, the last close before it
    (at most ``MAX_FFILL_S`` old), like the research's ``mid_bar.fillna(close.ffill())``. None
    when the bars do not cover ``close_epoch``."""
    c = candles_by_end.get(close_epoch)
    if c is not None:
        return (float(c.open) + float(c.close)) / 2.0
    if not candles_by_end or max(candles_by_end) < close_epoch:
        return None
    prev = max((t for t in candles_by_end if t < close_epoch), default=None)
    if prev is None or close_epoch - prev > MAX_FFILL_S:
        return None
    return float(candles_by_end[prev].close)


def parse_expiration_value(v: Any) -> float | None:
    """``"84264.76"`` / ``"79,604.96"`` -> float; None for empty or non-numeric (e.g. "Cancelled")."""
    if v is None:
        return None
    try:
        x = float(str(v).replace(",", "").strip())
    except ValueError:
        return None
    return x if math.isfinite(x) and x > 0 else None


def rolling_basis(records: Iterable[tuple[float, float, float]], n: int = BASIS_EVENTS,
                  min_n: int = BASIS_MIN_EVENTS) -> tuple[float | None, int]:
    """Median of ``settlement - coinbase`` over the ``n`` most recent ``(close_epoch, settlement,
    coinbase_mid)`` records; ``(None, k)`` with fewer than ``min_n``."""
    rows = sorted(records, key=lambda r: r[0])[-n:]
    if len(rows) < min_n:
        return None, len(rows)
    return statistics.median(x - cb for _, x, cb in rows), len(rows)


@dataclass(frozen=True)
class ModelView:
    """Inputs and output of one model evaluation (for reasons/logs/tests)."""

    p_yes: float
    spot: float
    strike: float
    basis: float
    basis_n: int
    sigma_min: float  # per-minute vol (sqrt of the EWMA variance)
    tau_min: float
    minutes_left: float


class ModelUnavailable(Exception):
    """Model inputs missing or stale (the window stays undecided; the next tick retries)."""


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def _src_ok(source: Any) -> bool:
    return not source or str(source) == "coinbase"


def _fmt_money(x: float) -> str:
    return f"{x:,.2f}"


# --------------------------------------------------------------------------- strategy


class Btc15mFavorite(Strategy):
    #: 5 s ticks: decisions land within 5 s of the 10:00 mark; ticks outside the window make no requests
    tick_interval_s: ClassVar[float | None] = 5.0
    risk_defaults: ClassVar[dict[str, Any]] = {"max_allocation_pct": 10, "daily_loss_limit": 100}
    #: the primary rule runs unless the config / dashboard turns it off
    enabled_by_default: ClassVar[bool] = True
    name: ClassVar[str] = "btc15m_favorite"
    description: ClassVar[str] = (
        "PRIMARY research rule, the only one that replicated out of sample. KXBTC15M only: once per 15-minute "
        "window, at 10 minutes before close (decisions more than 15 s late are skipped), buy the favourite (the side whose ask is in "
        "0.85-0.97) as a taker at the ask when a Coinbase spot model (Student-t 3.5 d.f., vol x1.1, 10-min EWMA "
        "vol, settlement basis) says it wins at least 1c/contract more often than the ask plus fee; hold to "
        "settlement. Evidence (c/contract after fees): in-sample +3.7 (n=555), untouched holdout +4.9 "
        "(n=203, CI +1.7..+7.8); forward estimate +2..+3, about 7-8 trades a day. Timing-sensitive: keep "
        "the defaults."
    )
    backtestable: ClassVar[bool] = True
    default_params: ClassVar[dict[str, Any]] = {
        "use_model": True,
        "entry_minutes_max": 10.0,
        "entry_minutes_min": 9.75,
        "price_min": 0.85,
        "price_max": 0.97,
        "min_model_edge": 0.01,
        "slippage_ticks": 0,
        "sizing": "kelly",
        "kelly_fraction": 0.25,
        "contracts": 20,
        "min_contracts": 10,
        "max_contracts": 1000,
        "max_cost_per_trade": 50.0,
        "max_spot_age_s": 60.0,
        "max_candle_age_s": 180.0,
        "blind_prior_edge": 0.02,
    }
    param_schema: ClassVar[dict[str, dict[str, Any]]] = {
        "use_model": {"type": "bool",
                      "help": "Require the Coinbase spot model to confirm the favourite (the research rule). "
                              "Off = blind favourite (weaker: +2.1c in-sample, +3.5c holdout). With the model "
                              "on, a window without usable spot data is skipped"},
        "entry_minutes_max": {"type": "float", "min": 1, "max": 14,
                              "help": "Decide on the first tick with minutes-to-close <= this (research: 10; "
                                      "lags 7 and 11 lost money, so keep it)"},
        "entry_minutes_min": {"type": "float", "min": 0, "max": 14,
                              "help": "... and > this: later decisions are skipped, not traded (the edge peaks "
                                      "sharply at 10:00; research lag 9 earned half of lag 10)"},
        "price_min": {"type": "float", "min": 0.5, "max": 0.99,
                      "help": "Lowest favourite ask traded, inclusive (research: 0.85)"},
        "price_max": {"type": "float", "min": 0.5, "max": 0.999,
                      "help": "Highest favourite ask traded, inclusive (research: 0.97)"},
        "min_model_edge": {"type": "float", "min": -0.2, "max": 0.2,
                           "help": "Required model P(side) - ask - fee per contract, in dollars (research: 0.01)"},
        "slippage_ticks": {"type": "int", "min": 0, "max": 5,
                           "help": "Limit = ask + this many price-grid ticks (0 = exactly the ask)"},
        "sizing": {"type": "enum", "choices": ["kelly", "fixed"],
                   "help": "kelly: fractional Kelly on the model probability (blind mode without a model "
                           "uses `contracts`); fixed: always `contracts`"},
        "kelly_fraction": {"type": "float", "min": 0.01, "max": 1,
                           "help": "Fraction of full Kelly staked (config risk.kelly_fraction is 0.25)"},
        "contracts": {"type": "int", "min": 1, "max": 10000, "help": "Contracts per trade for fixed sizing"},
        "min_contracts": {"type": "int", "min": 1, "max": 1000,
                          "help": "Kelly floor, and the smallest order sent (1-lots lose to fee rounding)"},
        "max_contracts": {"type": "int", "min": 1, "max": 100000, "help": "Most contracts per trade"},
        "max_cost_per_trade": {"type": "float", "min": 1, "max": 100000,
                               "help": "Dollar cap per trade including the fee (the risk manager's "
                                       "per-market cap still applies)"},
        "max_spot_age_s": {"type": "float", "min": 1, "max": 600,
                           "help": "Skip when the Coinbase spot quote is older than this (seconds)"},
        "max_candle_age_s": {"type": "float", "min": 60, "max": 1800,
                             "help": "Skip when the newest complete Coinbase 1-minute bar ended longer ago "
                                     "than this (seconds)"},
        "blind_prior_edge": {"type": "float", "min": -0.2, "max": 0.2,
                             "help": "expected_edge reported by blind-mode trades without a model (research "
                                     "blind favourite: +0.021 in-sample, +0.035 holdout)"},
    }

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        super().__init__(params)
        self._decided: dict[str, int] = {}  # ticker -> close epoch (one decision per window)
        self._cb_mid: dict[int, float] = {}  # window close epoch -> Coinbase final-minute mid
        self._cb_failed: dict[int, int] = {}  # window close epoch -> when its bars had no usable price
        self._warmed: dict[str, int] = {}  # ticker -> close epoch: fee event fetched ahead of the decision

    def universe(self) -> UniverseSpec:
        # new 15-minute windows show up within ~20 s + CDN instead of the 120 s universe refresh
        return UniverseSpec(series_tickers=[SERIES], refresh_s=20)

    # ------------------------------------------------------------------ state

    def dump_state(self) -> Any:
        return {"decided": dict(sorted(self._decided.items())),
                "cb_mid": {str(k): v for k, v in sorted(self._cb_mid.items())}}

    def load_state(self, state: Any) -> None:
        if not isinstance(state, Mapping):
            return
        dec = state.get("decided")
        if isinstance(dec, Mapping):
            for t, ts in dec.items():
                try:
                    self._decided[str(t)] = int(ts)
                except (TypeError, ValueError):
                    continue
        cb = state.get("cb_mid")
        if isinstance(cb, Mapping):
            for k, v in cb.items():
                try:
                    x = float(v)
                    if math.isfinite(x) and x > 0:
                        self._cb_mid[int(k)] = x
                except (TypeError, ValueError):
                    continue

    def has_decided(self, ticker: str) -> bool:
        return ticker in self._decided

    def _prune(self, now: datetime) -> None:
        cut = _epoch(now - STATE_TTL)
        for t in [t for t, c in self._decided.items() if c < cut]:
            del self._decided[t]
        for k in [k for k in self._cb_mid if k < cut]:
            del self._cb_mid[k]
        for k in [k for k in self._cb_failed if k < cut]:
            del self._cb_failed[k]
        for t in [t for t, c in self._warmed.items() if c < cut]:
            del self._warmed[t]

    # ------------------------------------------------------------------ tick

    async def on_tick(self, ctx: StrategyContext) -> list[OrderIntent]:
        now = ctx.now
        self._prune(now)
        lo, hi = float(self.params["entry_minutes_min"]), float(self.params["entry_minutes_max"])
        out: list[OrderIntent] = []
        for ticker in sorted(ctx.markets):
            m = ctx.markets[ticker]
            if m.series_ticker != SERIES or m.close_time is None or ticker in self._decided:
                continue
            mtc = (m.close_time - now).total_seconds() / 60.0
            if hi < mtc <= hi + 1.0 and ticker not in self._warmed:
                await self._warm(ctx, m)
            if not lo < mtc <= hi or not m.is_tradable(now):
                continue
            pf = ctx.portfolio
            if pf is not None and (pf.holds(ticker, self.name) or pf.has_open_order(ticker, self.name)):
                self._decide(m)
                ctx.log(f"{ticker}: skip at {mtc:.2f} min to close: already holds a position or an open "
                        "order in this window (no second entry)", ticker=ticker, skip="held")
                continue
            intent = await self._evaluate(ctx, m, mtc)
            if intent is not None:
                out.append(intent)
        return out

    async def _warm(self, ctx: StrategyContext, m: Market) -> None:
        """A minute before the decision, fetch the window's event (fee overrides) so the order's
        fee lookup is a cache hit and does not stretch the simulated order latency."""
        self._warmed[m.ticker] = _epoch(m.close_time) if m.close_time is not None else 0
        fetch = getattr(ctx, "event", None)
        if callable(fetch):
            try:
                await fetch(m.event_ticker)
            except Exception:  # best effort: the broker fetches it at order time otherwise
                pass

    def _decide(self, m: Market) -> None:
        self._decided[m.ticker] = _epoch(m.close_time) if m.close_time is not None else 0

    async def _evaluate(self, ctx: StrategyContext, m: Market, mtc: float) -> OrderIntent | None:
        p = self.params
        t = m.ticker
        # model inputs first (spot last), then the book: the decision book is never older than the spot
        model: ModelView | None = None
        why = ""
        try:
            model = await self.model(ctx, m)
        except ModelUnavailable as e:
            why = str(e)
        book: Any = None
        book_error: Exception | None = None
        try:
            book = await _fresh_book(ctx, t)
        except Exception as e:  # the next tick in the window retries
            book_error = e
        # ctx.now is frozen at the tick start: after the network reads, the real clock decides
        real_mtc = min(mtc, _minutes_to_close(ctx, m))
        lag = round((float(p["entry_minutes_max"]) - real_mtc) * 60, 1)  # seconds after the 10:00 mark
        if real_mtc <= float(p["entry_minutes_min"]):
            self._decide(m)  # the window's deadline has passed: no later tick can trade it
            limit_s = (float(p["entry_minutes_max"]) - float(p["entry_minutes_min"])) * 60
            ctx.log(f"{t}: skip: the reads finished at {real_mtc:.2f} min to close (lag {lag:g}s), past the "
                    f"{limit_s:g}s decision limit", ticker=t, skip="late", lag_s=lag)
            return None
        mtc = real_mtc
        if book_error is not None:
            ctx.log(f"{t}: order book unavailable at {mtc:.2f} min to close (lag {lag:g}s; "
                    f"{type(book_error).__name__}: {book_error}); will retry within the window", ticker=t,
                    skip="no_book", lag_s=lag)
            return None
        lo_px, hi_px = D(p["price_min"]), D(p["price_max"])
        asks: dict[str, Decimal | None] = {"yes": book.best_yes_ask, "no": book.best_no_ask}
        cands = [s for s, a in asks.items() if a is not None and ZERO < a < ONE and lo_px <= a <= hi_px]
        if not cands:
            self._decide(m)
            ctx.log(f"{t}: skip at {mtc:.2f} min to close (lag {lag:g}s): price out of band (YES ask {asks['yes']}, "
                    f"NO ask {asks['no']}; band [{lo_px}, {hi_px}])", ticker=t, skip="band", lag_s=lag,
                    yes_ask=_f(asks["yes"]), no_ask=_f(asks["no"]))
            return None
        if model is None and p["use_model"]:
            ctx.log(f"{t}: skip at {mtc:.2f} min to close (lag {lag:g}s): model unavailable ({why}); will retry "
                    "within the window", ticker=t, skip="no_model", reason=why, lag_s=lag)
            return None

        equity = D(getattr(ctx.portfolio, "equity", ZERO) or ZERO)
        evals: dict[str, tuple[Decimal, int, float | None, float | None]] = {}  # side -> (ask, n, fv, edge)
        for side in cands:
            ask: Decimal = asks[side]  # type: ignore[assignment]
            fv = None if model is None else (model.p_yes if side == "yes" else 1.0 - model.p_yes)
            n = self._count(ctx, m, fv, ask, equity)
            edge = None
            if fv is not None and n > 0:
                edge = fv - float(ask) - float(D(ctx.fee(m, ask, n, is_taker=True)) / n)
            evals[side] = (ask, n, fv, edge)

        if p["use_model"]:
            thr = float(p["min_model_edge"])
            ok = {s for s, (_, n, _, e) in evals.items() if e is not None and e >= thr}
            if not ok:
                self._decide(m)
                detail = "; ".join(f"{s.upper()} ask {a}: P={fv:.4f}, edge {100 * e:+.2f}c" for s, (a, _, fv, e)
                                   in evals.items() if fv is not None and e is not None)
                small = [s for s, (_, n, _, _) in evals.items() if n <= 0]
                if small and not detail:
                    return self._skip_size(ctx, m, mtc, small[0], evals[small[0]][0])
                ctx.log(f"{t}: skip at {mtc:.2f} min to close (lag {lag:g}s): model disagrees ({detail}; need >= "
                        f"{100 * thr:.1f}c after fee; {self._model_text(model)})", ticker=t, skip="model",
                        p_yes=model.p_yes if model else None, lag_s=lag)
                return None
            e_yes = evals["yes"][3] if "yes" in ok else None
            e_no = evals["no"][3] if "no" in ok else None
            side = "yes" if e_yes is not None and (e_no is None or e_yes >= e_no) else "no"
        else:
            if len(cands) == 1:
                side = cands[0]
            else:
                mid = book.mid
                side = "yes" if mid is None or mid >= D("0.5") else "no"

        ask, n, fv, _ = evals[side]
        self._decide(m)
        if n < int(p["min_contracts"]) or n <= 0:
            return self._skip_size(ctx, m, mtc, side, ask)
        limit = self._limit(m, ask)
        if limit is None:
            ctx.log(f"{t}: skip at {mtc:.2f} min to close: no valid limit price above {ask}", ticker=t,
                    skip="price")
            return None
        fee = D(ctx.fee(m, limit, n, is_taker=True))
        if fv is not None:
            exp_edge = D(fv) - limit - fee / n
        else:
            exp_edge = D(p["blind_prior_edge"])
        # every window's decision is in the log (skips above; the trade's full reason is in its signal)
        ctx.log(f"{t}: trade at {mtc:.2f} min to close (lag {lag:g}s): buy {n} {side.upper()} @ {limit} IOC "
                f"({'model P=' + format(fv, '.4f') if fv is not None else 'blind'}, edge "
                f"{float(exp_edge) * 100:+.2f}c/contract after fee)", ticker=t, trade=side, lag_s=lag)
        return OrderIntent(
            ticker=t, side=side, action="buy", count=n, limit_price=limit, tif="ioc", strategy=self.name,
            reason=self._reason(m, mtc, side, ask, limit, n, fee, fv, model, lag),
            fair_value=round(fv, 6) if fv is not None else None,
            expected_edge=exp_edge.quantize(Decimal("0.000001")),
        )

    def _skip_size(self, ctx: StrategyContext, m: Market, mtc: float, side: str, ask: Decimal) -> None:
        """Log a size skip (returns None so callers can ``return self._skip_size(...)``)."""
        p = self.params
        ctx.log(f"{m.ticker}: skip at {mtc:.2f} min to close: size below min_contracts ({p['min_contracts']}) "
                f"for {side.upper()} @ {ask} (max_cost_per_trade ${p['max_cost_per_trade']}, max_contracts "
                f"{p['max_contracts']}, sizing {p['sizing']})", ticker=m.ticker, skip="size")

    # ------------------------------------------------------------------ sizing / pricing

    def _count(self, ctx: StrategyContext, m: Market, fv: float | None, price: Decimal, equity: Decimal) -> int:
        """Contracts to buy at ``price`` (0 when even ``min_contracts`` does not fit the dollar cap)."""
        p = self.params
        min_n = int(p["min_contracts"])
        fee_ref = D(ctx.fee(m, price, 100, is_taker=True)) / 100  # amortized per-contract fee
        if p["sizing"] == "kelly" and fv is not None:
            n = max(kelly_count(fv, price, equity, p["kelly_fraction"], fee=fee_ref), min_n)
        else:
            n = int(p["contracts"])
        n = min(n, int(p["max_contracts"]))
        cap = D(p["max_cost_per_trade"])
        n = min(n, int(cap / (price + fee_ref)))
        while n > 0 and n * price + D(ctx.fee(m, price, n, is_taker=True)) > cap:
            n -= 1
        return n if n >= min_n else 0

    def _limit(self, m: Market, ask: Decimal) -> Decimal | None:
        limit = ask
        for _ in range(int(self.params["slippage_ticks"])):
            nxt = next_price_up(limit, m.price_ranges)
            if nxt is None or nxt >= ONE:
                break
            limit = nxt
        return limit if m.is_valid_price(limit) else None

    # ------------------------------------------------------------------ model

    async def model(self, ctx: StrategyContext, m: Market) -> ModelView:
        """Evaluate the research model for market ``m`` at ``ctx.now``.

        Raises :class:`ModelUnavailable` (with the reason) when an input is missing or stale.
        Updates the cache of Coinbase final-minute mids as a side effect."""
        now = ctx.now
        if m.strike_type not in ("greater", "greater_or_equal") or m.floor_strike is None:
            raise ModelUnavailable(f"no usable strike (strike_type {m.strike_type!r}, "
                                   f"floor_strike {m.floor_strike})")
        strike = float(m.floor_strike)
        if m.close_time is None or strike <= 0:
            raise ModelUnavailable("no close time / strike")
        feeds = getattr(ctx, "feeds", None)
        spot_feed = feeds.get(SPOT_FEED) if isinstance(feeds, Mapping) else None
        settled_feed = feeds.get(SETTLED_FEED) if isinstance(feeds, Mapping) else None
        if spot_feed is None or settled_feed is None:
            raise ModelUnavailable(f"feeds {SPOT_FEED!r} and {SETTLED_FEED!r} are required")
        opened = m.open_time or (m.close_time - WINDOW)

        # 1. settled windows for the basis (those that closed before this window opened)
        try:
            settled = await settled_feed.settled_markets(SERIES, since=opened - WINDOW * SETTLED_LOOKBACK)
        except Exception as e:  # any feed failure: skip, the next tick in the window retries
            raise ModelUnavailable(f"settled markets: {type(e).__name__}: {e}") from None
        events: dict[int, float] = {}
        for s in settled:
            if s.series_ticker != SERIES or s.close_time is None or not s.close_time < opened:
                continue
            xv = parse_expiration_value(s.expiration_value)
            if xv is not None:
                events[_epoch(s.close_time)] = xv
        needed = sorted(events)[-(BASIS_EVENTS + 8):]

        # 2. Coinbase 1-minute bars (vol + final-minute mids for the basis cache). The long fetch
        #    happens only when one of the 48 windows the basis will use is older than the short
        #    fetch reaches and not cached yet (cold start, downtime) and was not tried before.
        missing = [c for c in needed if c not in self._cb_mid]
        oldest_ok = _epoch(now) - (VOL_LOOKBACK_MIN - 2) * 60
        want = [c for c in reversed(needed) if c in self._cb_mid or c not in self._cb_failed][:BASIS_EVENTS]
        long_fetch = any(c not in self._cb_mid and c < oldest_ok for c in want)
        minutes = BASIS_HISTORY_MIN if long_fetch else VOL_LOOKBACK_MIN
        try:
            raw = await spot_feed.candles(SYMBOL, minutes)
        except Exception as e:  # FeedError, HTTP errors, ...: skip (retried within the window)
            raise ModelUnavailable(f"Coinbase candles: {type(e).__name__}: {e}") from None
        bars = [c for c in raw if getattr(c, "complete", True) and c.end <= now]
        if not bars:
            raise ModelUnavailable("no complete 1-minute bars")
        if not all(_src_ok(getattr(c, "source", "")) for c in bars):
            raise ModelUnavailable(f"candles not from Coinbase (source {getattr(bars[-1], 'source', '')!r})")
        age = (now - bars[-1].end).total_seconds()
        if age > float(self.params["max_candle_age_s"]):
            raise ModelUnavailable(f"candles stale (newest bar ended {age:.0f}s ago)")
        grid = minute_grid(bars)
        by_end = {_epoch(c.end): c for c in bars}
        first_end = _epoch(bars[0].end)
        for close in missing:
            mid = final_minute_mid(by_end, close)
            if mid is not None:
                self._cb_mid[close] = mid
                self._cb_failed.pop(close, None)
            elif first_end <= close:  # the bars covered it, but there is no usable price
                self._cb_failed[close] = _epoch(now)
        cut = _epoch(now) - VOL_LOOKBACK_MIN * 60
        var = ewma_variance([v for t, v in grid if t >= cut])
        if var is None or not math.isfinite(var) or var <= 0:
            raise ModelUnavailable(f"too little bar history for the EWMA (need {VOL_MIN_RETURNS} returns)")

        # 3. basis
        recs = [(float(c), events[c], self._cb_mid[c]) for c in needed if c in self._cb_mid]
        basis, n_basis = rolling_basis(recs)
        if basis is None:
            raise ModelUnavailable(f"basis needs {BASIS_MIN_EVENTS} settled windows with Coinbase data, "
                                   f"have {n_basis}")

        # 4. spot (last: freshest, and fetched now rather than taken from the feed's cache)
        try:
            q = await _call_fresh(spot_feed.spot, SYMBOL)
        except Exception as e:  # FeedError, HTTP errors, ...: skip (retried within the window)
            raise ModelUnavailable(f"Coinbase spot: {type(e).__name__}: {e}") from None
        if not _src_ok(getattr(q, "source", "")):
            raise ModelUnavailable(f"spot not from Coinbase (source {q.source!r})")
        max_age = float(self.params["max_spot_age_s"])
        ages = [(now - x).total_seconds() for x in (q.ts, getattr(q, "fetched_at", None) or q.ts)]
        if max(ages) > max_age:
            raise ModelUnavailable(f"spot stale ({max(ages):.0f}s old > {max_age:g}s)")
        spot = float(q.price)
        if not math.isfinite(spot) or spot <= 0:
            raise ModelUnavailable(f"bad spot {q.price!r}")

        minutes_left = (m.close_time - now).total_seconds() / 60.0
        tau = horizon_minutes(minutes_left)
        sigma = VOL_MULT * math.sqrt(var * tau)
        p_yes = prob_above(spot, strike, sigma, T_DOF, basis)
        return ModelView(p_yes=p_yes, spot=spot, strike=strike, basis=basis, basis_n=n_basis,
                         sigma_min=math.sqrt(var), tau_min=tau, minutes_left=minutes_left)

    @staticmethod
    def _model_text(mv: ModelView | None) -> str:
        if mv is None:
            return "no model"
        return (f"P(YES)={mv.p_yes:.4f}: Coinbase {_fmt_money(mv.spot)} vs strike {_fmt_money(mv.strike)}, "
                f"basis {mv.basis:+.2f} ({mv.basis_n} windows), vol {100 * mv.sigma_min:.4f}%/min, "
                f"tau {mv.tau_min:.2f} min, t{T_DOF:g} x{VOL_MULT:g}")

    def _reason(self, m: Market, mtc: float, side: str, ask: Decimal, limit: Decimal, n: int, fee: Decimal,
                fv: float | None, mv: ModelView | None, lag: float = 0.0) -> str:
        p = self.params
        band = f"[{D(p['price_min'])}, {D(p['price_max'])}]"
        head = (f"BTC15M favourite: {side.upper()} ask {ask} in {band} at {mtc:.2f} min to close "
                f"(decision lag {lag:g}s after the {float(p['entry_minutes_max']):g}-min mark)")
        if fv is not None:
            edge = fv - float(limit) - float(fee) / n
            mode = "model-confirmed" if p["use_model"] else "blind (model shown for reference)"
            body = (f"; {mode}: P({side.upper()})={fv:.4f}, edge {100 * edge:+.2f}c/contract after fee "
                    f"(need >= {100 * float(p['min_model_edge']):.1f}c); {self._model_text(mv)}")
        else:
            body = f"; blind favourite (no model), prior edge {100 * float(p['blind_prior_edge']):+.1f}c"
        tail = f"; buy {n} {side.upper()} @ {limit} IOC (fee ${fee}), hold to settlement"
        return head + body + tail


def _minutes_to_close(ctx: StrategyContext, m: Market) -> float:
    """Minutes to ``m``'s close on the context's real clock (``ctx.clock()``; ``ctx.now`` if it has none)."""
    clock = getattr(ctx, "clock", None)
    real = clock() if callable(clock) else None
    if not isinstance(real, datetime):
        real = ctx.now
    return (m.close_time - real).total_seconds() / 60.0  # type: ignore[operator]


def _f(x: Decimal | None) -> float | None:
    return float(x) if x is not None else None


async def _call_fresh(fn: Any, *args: Any) -> Any:
    """``await fn(*args, max_age_s=0)`` (data fetched now, never cached) where ``fn`` takes
    ``max_age_s``, else ``await fn(*args)``."""
    try:
        takes_age = "max_age_s" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        takes_age = False
    return await (fn(*args, max_age_s=0) if takes_age else fn(*args))


async def _fresh_book(ctx: StrategyContext, ticker: str) -> Any:
    """A book fetched now where the context supports ``max_age_s`` (the engine's), else ``ctx.orderbook``."""
    return await _call_fresh(ctx.orderbook, ticker)

