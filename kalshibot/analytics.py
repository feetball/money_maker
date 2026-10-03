"""Performance analytics (ARCHITECTURE.md §11): is a strategy's edge real?

Computed over **realized trades**, i.e. the broker's ``Settlement`` rows: ``kind="settlement"``
(the market resolved) and ``kind="close"`` (netted out before resolution). Both count for
P&L statistics; only ``settlement`` rows (the outcome is known) enter the Brier score and
the calibration buckets.

Per strategy and overall (``GET /api/analytics``)::

    count, settled, closed, contracts, wins, win_rate,
    total_pnl (= realized_pnl), mean_pnl_per_trade, mean_pnl_per_contract,
    ci_low, ci_high, ci_basis="trade"          95% bootstrap CI of the mean P&L per trade
    (= ci_trade_low, ci_trade_high),            (readiness is decided on ci_low),
    ci_contract_low, ci_contract_high           and of the P&L per contract (ratio estimator),
    expected_edge_total, expected_edge_per_contract, trades_with_edge,
    realized_pnl_with_edge (P&L of the trades that carried an expected edge), edge_capture,
    brier, brier_n, fees, max_drawdown, max_drawdown_pct, readiness

The bootstrap resamples **UTC days** (clusters) with replacement, not individual trades or
events. Trades on one event (e.g. several strikes of one BTC hour) are correlated, but so are
different events on one day: one market regime moves every KXBTC15M window of that day together,
and each of those windows is its own event, so resampling events is in effect a per-trade
bootstrap and overstates confidence. An event belongs to one day, the UTC day of its earliest
entry (``Trade.opened_at``, else the row time), so a partial close and the later settlement of
the same event never split; an event without any time is its own cluster (:func:`day_clusters`).
Deterministic (seeded) so the numbers do not jitter between polls.

Go-live readiness (per strategy): ``ready`` only if ``count >= min_settled_trades`` (default
200; ``min_settled_trades_by_strategy`` sets more for rare-loss strategies, e.g. 1,500 for the
ladder), the CI lower bound of the mean P&L per trade is > 0, the **tail check** passes and the
max drawdown is within ``max_drawdown_pct`` (default 20 %).

* Tail check: a percentile bootstrap cannot represent a loss it has not seen (200 wins of $0.17
  and no loss say nothing about a $15 wipeout), so readiness also asks whether the loss rate is
  low enough to break even. Trades are grouped by event (losses cluster: one ladder event can
  lose 10 strikes at once; this bound still treats events as independent draws, unlike the
  day-clustered CI); with ``k`` losing events of ``n``, the one-sided 95% Clopper-Pearson
  upper bound on the loss-event rate must be below break-even ``W / (W + L)`` (``W`` = mean P&L
  of winning events, ``L`` = mean loss of losing events, or - with no loss yet - the mean event
  cost, i.e. a full wipeout).
* Drawdown is measured on the realized-P&L equity curve (starting capital + cumulative P&L, in
  settlement order), as a percentage of the running peak. The starting capital is the account's
  starting balance, per strategy too: the gate asks how far the strategy alone would have drawn
  the account down. Overall, the mark-to-market equity snapshots are also considered (the worse
  of the two); the API folds in the drawdown of **every** stored equity snapshot
  (:func:`with_equity_drawdown`, from ``Store.equity_drawdown``). Per strategy the drawdown
  against the strategy's **allocation** (``strategy_capital``, e.g. 10% of the account) is also
  reported, as ``allocation`` / ``max_drawdown_pct_of_allocation``, but does not gate readiness:
  the allocation caps concurrent exposure, not cumulative losses (``daily_loss_limit`` does
  that), and a strategy that stakes half its allocation per trade (btc15m_favorite: $50 of $100)
  would fail it on any losing streak - its in-sample replay, +3.5c/contract with a CI above 0,
  shows a 54% drawdown of its $100 allocation but 14% of the account.
* The headline ``readiness`` is **per strategy**: ``ready`` only when at least one strategy is
  individually ready (and the account drawdown is within the limit); ``reasons`` lists every
  strategy's verdict and ``ready_strategies`` the ready ones. The pooled ``overall`` stats and
  their ``overall.readiness`` are informational only - go-live is a per-strategy decision.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import numpy as np

__all__ = [
    "Trade",
    "bootstrap_ratio_ci",
    "brier_score",
    "calibration_buckets",
    "clopper_pearson_upper",
    "compute_analytics",
    "day_clusters",
    "drawdown",
    "headline_readiness",
    "readiness",
    "tail_stats",
    "trade_stats",
    "with_equity_drawdown",
]

DEFAULT_MIN_TRADES = 200
DEFAULT_MAX_DRAWDOWN_PCT = 20.0


@dataclass(frozen=True, slots=True)
class Trade:
    """A realized trade (one settlement row) reduced to floats."""

    ts: datetime | None
    strategy: str
    ticker: str
    event: str
    kind: str
    side: str
    count: float
    payout: float
    pnl: float
    fees: float
    expected_edge: float | None
    fair_value: float | None
    opened_at: datetime | None = None  # when the position was opened (earliest entry of these contracts)

    @property
    def outcome(self) -> float | None:
        """Per-contract payout of the held side in [0, 1] (1 = won) for resolved markets."""
        if self.kind != "settlement" or self.count <= 0:
            return None
        return min(1.0, max(0.0, self.payout / self.count))


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _fl(x: Any) -> float | None:
    if x is None:
        return None
    try:
        v = float(x) if not isinstance(x, Decimal) else float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_time(x: Any) -> datetime | None:
    """A ``datetime`` or an ISO-8601 string (``Z`` suffix allowed) as an aware UTC datetime; naive
    values are taken as UTC; None for anything else (including an empty or unparseable string)."""
    if isinstance(x, str):
        txt = x.strip()
        if not txt:
            return None
        try:
            x = datetime.fromisoformat(txt[:-1] + "+00:00" if txt.endswith(("Z", "z")) else txt)
        except ValueError:
            return None
    if not isinstance(x, datetime):
        return None
    return x.replace(tzinfo=UTC) if x.tzinfo is None else x.astimezone(UTC)


def day_clusters(events: Sequence[Any], times: Sequence[Any]) -> list[Any]:
    """Bootstrap cluster per row: the UTC day (``"YYYY-MM-DD"``) of the row's event's earliest time.

    ``events[i]`` is row ``i``'s event and ``times[i]`` its entry time (a datetime or ISO string;
    None or unparseable = unknown). All rows of an event share one cluster, the day of the earliest
    time among them, so a partial close and its settlement never split. An event with no known
    time at all is its own cluster."""
    first: dict[Any, datetime] = {}
    for ev, raw in zip(events, times, strict=True):
        t = parse_time(raw)
        if t is not None and (ev not in first or t < first[ev]):
            first[ev] = t
    return [first[ev].strftime("%Y-%m-%d") if ev in first else ("event", ev) for ev in events]


def to_trade(s: Any) -> Trade:
    ticker = str(_get(s, "ticker", "") or "")
    event = str(_get(s, "event_ticker", "") or "") or ticker.rsplit("-", 1)[0]
    return Trade(
        ts=parse_time(_get(s, "ts")),
        strategy=str(_get(s, "strategy", "") or ""),
        ticker=ticker,
        event=event,
        kind=str(_get(s, "kind", "settlement") or "settlement"),
        side=str(_get(s, "side", "") or ""),
        count=_fl(_get(s, "count")) or 0.0,
        payout=_fl(_get(s, "payout")) or 0.0,
        pnl=_fl(_get(s, "pnl")) or 0.0,
        fees=_fl(_get(s, "fees")) or 0.0,
        expected_edge=_fl(_get(s, "expected_edge")),
        fair_value=_fl(_get(s, "fair_value")),
        opened_at=parse_time(_get(s, "opened_at")),
    )


# --------------------------------------------------------------------------- primitives


def bootstrap_ratio_ci(
    num: Sequence[float],
    den: Sequence[float] | None = None,
    clusters: Sequence[Any] | None = None,
    *,
    n_boot: int = 2000,
    conf: float = 0.95,
    seed: int = 0,
) -> tuple[float | None, float | None]:
    """Percentile bootstrap CI of ``sum(num) / sum(den)`` resampling clusters.

    ``den=None`` means one per row (the CI of the mean). ``clusters`` groups rows (e.g. by
    UTC day, see :func:`day_clusters`); ``None`` treats each row as its own cluster. Returns
    ``(None, None)`` when there are fewer than two clusters (no variance estimate).
    """
    x = np.asarray(num, dtype=float)
    d = np.ones_like(x) if den is None else np.asarray(den, dtype=float)
    if x.size == 0 or x.size != d.size:
        return None, None
    if clusters is None:
        cx, cd = x, d
    else:
        keys: dict[Any, int] = {}
        idx = np.fromiter((keys.setdefault(c, len(keys)) for c in clusters), dtype=np.int64, count=x.size)
        cx = np.bincount(idx, weights=x)
        cd = np.bincount(idx, weights=d)
    k = cx.size
    if k < 2:
        return None, None
    rng = np.random.default_rng(seed)
    chunk = max(1, min(n_boot, 2_000_000 // k))
    stats: list[np.ndarray] = []
    done = 0
    while done < n_boot:
        b = min(chunk, n_boot - done)
        sel = rng.integers(0, k, size=(b, k))
        sd = cd[sel].sum(axis=1)
        sx = cx[sel].sum(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            r = np.where(sd > 0, sx / np.where(sd > 0, sd, 1), np.nan)
        stats.append(r)
        done += b
    allr = np.concatenate(stats)
    allr = allr[np.isfinite(allr)]
    if allr.size == 0:
        return None, None
    alpha = (1 - conf) / 2
    lo, hi = np.quantile(allr, [alpha, 1 - alpha])
    return float(lo), float(hi)


def brier_score(probs: Sequence[float], outcomes: Sequence[float]) -> float | None:
    """Mean squared error of probabilities vs outcomes (0 = perfect, 0.25 = coin flip at 0.5)."""
    if not probs:
        return None
    p = np.asarray(probs, dtype=float)
    o = np.asarray(outcomes, dtype=float)
    return float(np.mean((p - o) ** 2))


def calibration_buckets(probs: Sequence[float], outcomes: Sequence[float], n_buckets: int = 10
                        ) -> list[dict[str, Any]]:
    """Equal-width buckets of predicted probability: ``{bucket, lo, hi, n, mean_fair_value,
    realized_rate}`` (empty buckets omitted)."""
    out = []
    if not probs:
        return out
    p = np.clip(np.asarray(probs, dtype=float), 0.0, 1.0)
    o = np.asarray(outcomes, dtype=float)
    idx = np.minimum((p * n_buckets).astype(int), n_buckets - 1)
    for b in range(n_buckets):
        m = idx == b
        n = int(m.sum())
        if n == 0:
            continue
        lo, hi = b / n_buckets, (b + 1) / n_buckets
        out.append({
            "bucket": f"{lo:.1f}–{hi:.1f}",
            "lo": round(lo, 4),
            "hi": round(hi, 4),
            "n": n,
            "mean_fair_value": round(float(p[m].mean()), 4),
            "realized_rate": round(float(o[m].mean()), 4),
        })
    return out


def drawdown(equity: Sequence[float]) -> tuple[float, float]:
    """(max drawdown in dollars, max drawdown in % of the running peak), both >= 0."""
    if len(equity) == 0:
        return 0.0, 0.0
    e = np.asarray(equity, dtype=float)
    peak = np.maximum.accumulate(e)
    dd = peak - e
    with np.errstate(divide="ignore", invalid="ignore"):
        pct = np.where(peak > 0, dd / peak * 100.0, 0.0)
    return float(dd.max()), float(pct.max())


def _binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p)."""
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 1.0 if k >= n else 0.0
    lp, lq = math.log(p), math.log1p(-p)
    lg = math.lgamma(n + 1)
    total = 0.0
    for i in range(0, min(k, n) + 1):
        total += math.exp(lg - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq)
    return min(total, 1.0)


def clopper_pearson_upper(k: int, n: int, conf: float = 0.95) -> float:
    """One-sided ``conf`` Clopper-Pearson upper bound on a rate with ``k`` events in ``n`` trials
    (``1 - (1 - conf) ** (1 / n)`` when ``k == 0``)."""
    if n <= 0:
        return 1.0
    if k >= n:
        return 1.0
    alpha = 1.0 - conf
    lo, hi = k / n, 1.0
    for _ in range(100):  # P(X <= k | p) falls as p rises: find P(X <= k | p) = alpha
        mid = (lo + hi) / 2
        if _binom_cdf(k, n, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return hi


def tail_stats(trades: Sequence[Trade], conf: float = 0.95) -> dict[str, Any]:
    """Loss-event rate vs break-even (see the module doc). Events = ``Trade.event`` clusters."""
    ev_pnl: dict[str, float] = {}
    ev_cost: dict[str, float] = {}
    for t in trades:
        ev_pnl[t.event] = ev_pnl.get(t.event, 0.0) + t.pnl
        ev_cost[t.event] = ev_cost.get(t.event, 0.0) + max(t.payout - t.pnl, 0.0)  # cost basis + fees
    n = len(ev_pnl)
    wins = [v for v in ev_pnl.values() if v > 0]
    losses = [-v for v in ev_pnl.values() if v < 0]
    k = len(losses)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    if losses:
        avg_loss, basis = sum(losses) / k, "observed losing events"
    else:
        costs = list(ev_cost.values())
        avg_loss, basis = (sum(costs) / len(costs) if costs else 0.0), "no loss yet: a full wipeout of the mean event cost"
    be = avg_win / (avg_win + avg_loss) if avg_win + avg_loss > 0 else 0.0
    upper = clopper_pearson_upper(k, n, conf) if n else None
    return {"events": n, "loss_events": k, "loss_event_rate": _r(k / n) if n else None,
            "loss_rate_upper": _r(upper, 6) if upper is not None else None, "break_even_loss_rate": _r(be, 6),
            "avg_win_event": _r(avg_win), "avg_loss_event": _r(avg_loss), "loss_basis": basis}


def readiness(stats: Mapping[str, Any], *, min_settled_trades: int = DEFAULT_MIN_TRADES,
              max_drawdown_pct: float = DEFAULT_MAX_DRAWDOWN_PCT) -> dict[str, Any]:
    """Go-live verdict ``{ready, reasons}``: failing checks when not ready, the evidence when ready."""
    n = int(stats.get("count") or 0)
    lo = stats.get("ci_low")
    dd = stats.get("max_drawdown_pct")
    tail = stats.get("tail")
    fails: list[str] = []
    passes: list[str] = []
    if n >= min_settled_trades:
        passes.append(f"{n} settled trades (>= {min_settled_trades})")
    else:
        fails.append(f"only {n} settled trades (need >= {min_settled_trades})")
    if lo is None:
        fails.append("no confidence interval yet for the mean P&L per trade (need trades on >= 2 UTC days)")
    elif lo > 0:
        passes.append(f"95% CI lower bound of mean P&L per trade is ${lo:+.4f} (> 0)")
    else:
        fails.append(f"95% CI lower bound of mean P&L per trade is ${lo:+.4f} (must be > 0)")
    if isinstance(tail, Mapping) and tail.get("events"):
        up, be = tail.get("loss_rate_upper"), tail.get("break_even_loss_rate") or 0.0
        text = (f"{tail['loss_events']} losing events of {tail['events']}: 95% upper bound on the loss-event rate "
                f"{100 * (up or 0):.2f}% vs break-even {100 * be:.2f}% (avg winning event ${tail['avg_win_event']:.2f}, "
                f"avg loss ${tail['avg_loss_event']:.2f}; {tail['loss_basis']})")
        if up is not None and up < be:
            passes.append("tail check: " + text)
        else:
            fails.append("tail check failed: " + text)
    if dd is None or dd <= max_drawdown_pct:
        passes.append(f"max drawdown {dd or 0:.2f}% (limit {max_drawdown_pct:g}%)")
    else:
        fails.append(f"max drawdown {dd:.2f}% exceeds the {max_drawdown_pct:g}% limit")
    return {"ready": not fails, "reasons": fails if fails else passes}


def headline_readiness(by_strategy: Mapping[str, Mapping[str, Any]], *, account_drawdown_pct: float | None = None,
                       max_drawdown_pct: float = DEFAULT_MAX_DRAWDOWN_PCT) -> dict[str, Any]:
    """The go-live verdict shown on the dashboard: per strategy, never pooled. Ready only when at
    least one strategy is individually ready and the account drawdown is within the limit."""
    ready = [n for n in sorted(by_strategy) if (by_strategy[n].get("readiness") or {}).get("ready")]
    reasons: list[str] = []
    for n in sorted(by_strategy):
        r = by_strategy[n].get("readiness") or {}
        if r.get("ready"):
            reasons.append(f"{n}: ready")
        else:
            reasons.append(f"{n}: not ready ({'; '.join(r.get('reasons') or ['no verdict'])})")
    if not by_strategy:
        reasons.append("no settled trades yet")
    ok_dd = account_drawdown_pct is None or account_drawdown_pct <= max_drawdown_pct
    if not ok_dd:
        reasons.append(f"account: max drawdown {account_drawdown_pct:.2f}% exceeds the {max_drawdown_pct:g}% limit")
    return {"ready": bool(ready) and ok_dd, "reasons": reasons, "ready_strategies": ready,
            "basis": "per_strategy"}


def _r(x: float | None, nd: int = 4) -> float | None:
    if x is None or not math.isfinite(x):
        return None
    return round(x, nd)


# --------------------------------------------------------------------------- stats


def trade_stats(
    trades: Sequence[Trade],
    *,
    starting_balance: float = 1000.0,
    min_settled_trades: int = DEFAULT_MIN_TRADES,
    max_drawdown_pct: float = DEFAULT_MAX_DRAWDOWN_PCT,
    n_boot: int = 2000,
    seed: int = 0,
    equity_curve: Sequence[float] | None = None,
    allocation: float | None = None,
) -> dict[str, Any]:
    """Statistics for one group of realized trades (see the module docstring). ``allocation``
    (dollars, optional) adds the informational drawdown against it."""
    ordered = sorted(trades, key=lambda t: (t.ts is None, t.ts or datetime.min))
    n = len(ordered)
    contracts = sum(t.count for t in ordered)
    pnl = [t.pnl for t in ordered]
    total = float(sum(pnl))
    wins = sum(1 for t in ordered if t.pnl > 0)
    clusters = day_clusters([t.event for t in ordered], [t.opened_at or t.ts for t in ordered])
    lo, hi = bootstrap_ratio_ci(pnl, None, clusters, n_boot=n_boot, seed=seed) if n >= 2 else (None, None)
    clo, chi = (bootstrap_ratio_ci(pnl, [t.count for t in ordered], clusters, n_boot=n_boot, seed=seed)
                if n >= 2 and contracts > 0 else (None, None))
    with_ee = [t for t in ordered if t.expected_edge is not None]
    ee_total = sum(t.expected_edge for t in with_ee) if with_ee else None  # type: ignore[misc]
    ee_contracts = sum(t.count for t in with_ee)
    realized_on_ee = sum(t.pnl for t in with_ee) if with_ee else None
    scored = [t for t in ordered if t.fair_value is not None and t.outcome is not None]
    probs = [t.fair_value for t in scored]
    outs = [t.outcome for t in scored]
    curve = [starting_balance] + list(np.cumsum(pnl) + starting_balance) if n else [starting_balance]
    dd_usd, dd_pct = drawdown(curve)
    if equity_curve:
        e_usd, e_pct = drawdown(equity_curve)
        dd_usd, dd_pct = max(dd_usd, e_usd), max(dd_pct, e_pct)
    stats: dict[str, Any] = {
        "count": n,
        "settled": sum(1 for t in ordered if t.kind == "settlement"),
        "closed": sum(1 for t in ordered if t.kind == "close"),
        "contracts": _r(contracts),
        "wins": wins,
        "win_rate": _r(wins / n) if n else None,
        "total_pnl": _r(total),
        "realized_pnl": _r(total),
        "mean_pnl_per_trade": _r(total / n) if n else None,
        "mean_pnl_per_contract": _r(total / contracts) if contracts else None,
        "ci_low": _r(lo),
        "ci_high": _r(hi),
        "ci_basis": "trade",
        "ci_level": 0.95,
        "ci_trade_low": _r(lo),
        "ci_trade_high": _r(hi),
        "ci_contract_low": _r(clo),
        "ci_contract_high": _r(chi),
        "expected_edge_total": _r(ee_total),
        "expected_edge_per_contract": _r(ee_total / ee_contracts) if ee_total is not None and ee_contracts else None,
        "trades_with_edge": len(with_ee),
        "realized_pnl_with_edge": _r(realized_on_ee),
        "realized_per_contract_with_edge": _r(realized_on_ee / ee_contracts)
        if realized_on_ee is not None and ee_contracts else None,
        "edge_capture": _r(realized_on_ee / ee_total)
        if realized_on_ee is not None and ee_total not in (None, 0) else None,
        "brier": _r(brier_score(probs, outs)),  # type: ignore[arg-type]
        "brier_n": len(scored),
        "fees": _r(sum(t.fees for t in ordered)),
        "max_drawdown": _r(dd_usd),
        "max_drawdown_pct": _r(dd_pct),
        "starting_capital": _r(starting_balance),
        "tail": tail_stats(ordered),
        "min_settled_trades": min_settled_trades,
    }
    if allocation is not None and allocation > 0:
        a_usd, a_pct = drawdown([allocation] + list(np.cumsum(pnl) + allocation) if n else [allocation])
        stats["allocation"] = _r(allocation)
        stats["max_drawdown_pct_of_allocation"] = _r(a_pct)  # informational: not a readiness gate
    stats["readiness"] = readiness(stats, min_settled_trades=min_settled_trades, max_drawdown_pct=max_drawdown_pct)
    return stats


def compute_analytics(
    settlements: Iterable[Any],
    *,
    starting_balance: float | Decimal = 1000,
    min_settled_trades: int = DEFAULT_MIN_TRADES,
    max_drawdown_pct: float = DEFAULT_MAX_DRAWDOWN_PCT,
    equity_curve: Sequence[float] | None = None,
    n_boot: int = 2000,
    seed: int = 0,
    n_buckets: int = 10,
    min_settled_trades_by_strategy: Mapping[str, int] | None = None,
    strategy_capital: Mapping[str, float | Decimal] | None = None,
) -> dict[str, Any]:
    """``GET /api/analytics`` payload from settlement rows (``Settlement`` objects or dicts).

    ``min_settled_trades_by_strategy`` overrides ``min_settled_trades`` per strategy;
    ``strategy_capital`` is each strategy's allocation in dollars (reported as ``allocation`` with
    the informational ``max_drawdown_pct_of_allocation``; the readiness drawdown is measured
    against the account's starting balance)."""
    trades = [to_trade(s) for s in settlements]
    sb = float(starting_balance)
    kw = {"starting_balance": sb, "min_settled_trades": min_settled_trades,
          "max_drawdown_pct": max_drawdown_pct, "n_boot": n_boot, "seed": seed}
    overall = trade_stats(trades, equity_curve=equity_curve, **kw)  # type: ignore[arg-type]
    by_strategy: dict[str, Any] = {}
    mins = dict(min_settled_trades_by_strategy or {})
    caps = {k: float(v) for k, v in (strategy_capital or {}).items() if v is not None and float(v) > 0}
    for name in sorted({t.strategy for t in trades}):
        group = [t for t in trades if t.strategy == name]
        skw = {**kw, "min_settled_trades": int(mins.get(name, min_settled_trades)),
               "allocation": caps.get(name)}
        st = trade_stats(group, **skw)  # type: ignore[arg-type]
        scored = [t for t in group if t.fair_value is not None and t.outcome is not None]
        st["calibration"] = calibration_buckets([t.fair_value for t in scored],  # type: ignore[misc]
                                                [t.outcome for t in scored], n_buckets)  # type: ignore[misc]
        by_strategy[name] = st
    scored = [t for t in trades if t.fair_value is not None and t.outcome is not None]
    calibration = calibration_buckets([t.fair_value for t in scored],  # type: ignore[misc]
                                      [t.outcome for t in scored], n_buckets)  # type: ignore[misc]
    return {
        "overall": overall,
        "by_strategy": by_strategy,
        "calibration": calibration,
        "readiness": headline_readiness(by_strategy, account_drawdown_pct=overall.get("max_drawdown_pct"),
                                        max_drawdown_pct=max_drawdown_pct),
        "params": {"min_settled_trades": min_settled_trades, "max_drawdown_pct": max_drawdown_pct,
                   "min_settled_trades_by_strategy": mins, "strategy_capital": caps,
                   "bootstrap_resamples": n_boot, "cluster": "utc_day"},
    }


def with_equity_drawdown(result: Mapping[str, Any], dd_usd: float, dd_pct: float, *,
                         min_settled_trades: int = DEFAULT_MIN_TRADES,
                         max_drawdown_pct: float = DEFAULT_MAX_DRAWDOWN_PCT) -> dict[str, Any]:
    """Fold the account's equity-curve drawdown (computed over **every** snapshot, e.g. by
    ``Store.equity_drawdown``) into ``overall`` of a :func:`compute_analytics` result and
    recompute the readiness verdict. Returns a new dict; ``result`` is not modified."""
    out = dict(result)
    overall = dict(result["overall"])
    cur_usd = overall.get("max_drawdown") or 0.0
    cur_pct = overall.get("max_drawdown_pct") or 0.0
    overall["max_drawdown"] = _r(max(float(cur_usd), float(dd_usd)))
    overall["max_drawdown_pct"] = _r(max(float(cur_pct), float(dd_pct)))
    overall["readiness"] = readiness(overall, min_settled_trades=min_settled_trades,
                                     max_drawdown_pct=max_drawdown_pct)
    out["overall"] = overall
    out["readiness"] = headline_readiness(result.get("by_strategy") or {},
                                          account_drawdown_pct=overall["max_drawdown_pct"],
                                          max_drawdown_pct=max_drawdown_pct)
    return out
