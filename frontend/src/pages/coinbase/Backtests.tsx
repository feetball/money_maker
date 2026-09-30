import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { ApiError } from "../../api/client";
import { cbApi, isCbUnavailable } from "../../api/coinbase/client";
import { useCbStatus } from "../../api/coinbase/status";
import type { CbBacktestDetail, CbBacktestMetrics, CbBacktestStatus, CbBacktestSummary, CbCurvePoint, CbFeeTier, CbStrategy, ParamValue } from "../../api/coinbase/types";
import { SignedColumnChart, type SignedBarRow } from "../../charts/BarCharts";
import { DataTable } from "../../components/DataTable";
import { hasErrors, ParamEditor, type ParamErrors } from "../../components/ParamEditor";
import { Badge, Card, EmptyState, ErrorBlock, Field, Freshness, KpiTile, LoadingBlock, PageHeader, PollView, Spinner } from "../../components/ui";
import { ClampText, Pnl, StrategyTag, Time, Usd } from "../../components/values";
import { fmtCalendarDate, fmtDate, fmtDrawdownPct, fmtFrac, fmtInt, fmtNum, fmtPct, fmtUsd, humanize, isoLocalDay, parseTs, pnlTone } from "../../lib/format";
import { useAction, usePolling } from "../../lib/hooks";
import { CbGroupedColumns, CbTimeSeriesChart, type CbRow, type CbSeries, type GroupedRow } from "./charts";
import { baseOf, fmtBps, fmtPp, fmtQty, fmtRate, granularityLabel } from "./format";
import { CB_BASE, CbDecisionBadge, CbPage, CbSideTag, Fee, Price, ProductCell, useCbPolling, Weight } from "./shared";

const isDone = (s: CbBacktestStatus) => ["done", "completed", "complete", "finished", "succeeded", "success"].includes(s);
const isFailed = (s: CbBacktestStatus) => ["failed", "error", "errored", "cancelled"].includes(s);

function StatusBadge({ status }: { status: CbBacktestStatus }) {
  if (isDone(status))
    return (
      <Badge tone="good" icon="check">
        Done
      </Badge>
    );
  if (isFailed(status))
    return (
      <Badge tone="bad" icon="x">
        {humanize(status)}
      </Badge>
    );
  return (
    <Badge tone="info" icon="clock">
      {humanize(status || "running")}
    </Badge>
  );
}

const mnum = (m: CbBacktestMetrics | null | undefined, k: string): number | null => {
  const v = m?.[k];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
};
const metric = (b: CbBacktestSummary, k: string) => (isFailed(b.status) ? null : mnum(b.metrics, k));

function periodText(b: { start: string | null; end: string | null; period_reported: boolean }): string {
  if (!b.period_reported) return "—";
  if (!b.start && !b.end) return "all available data";
  return `${b.start ? fmtCalendarDate(b.start) : "start of data"} → ${b.end ? fmtCalendarDate(b.end) : "end of data"}`;
}

const tierText = (t: CbFeeTier) => `${t.label !== t.name ? `${t.label} · ` : ""}maker ${fmtRate(t.maker_rate)} · taker ${fmtRate(t.taker_rate)}`;

// ---------------------------------------------------------------------------
// Launch form
// ---------------------------------------------------------------------------

function LaunchForm({ strategies }: { strategies: CbStrategy[] }) {
  const [sp] = useSearchParams();
  const navigate = useNavigate();
  const { busy, run } = useAction();
  const { status } = useCbStatus();
  const candidates = strategies.filter((s) => s.backtestable);
  const initial = candidates.find((s) => s.name === sp.get("strategy"))?.name ?? candidates[0]?.name ?? "";
  const [name, setName] = useState(initial);
  const strat = strategies.find((s) => s.name === name);
  const today = new Date();
  const [start, setStart] = useState(() => {
    const d = new Date(today);
    d.setFullYear(d.getFullYear() - 3);
    return isoLocalDay(d);
  });
  const [end, setEnd] = useState(isoLocalDay(today));
  const [balance, setBalance] = useState("1000");
  const [tier, setTier] = useState<string>("");
  const [params, setParams] = useState<Record<string, ParamValue>>(strat?.params ?? {});
  const [errors, setErrors] = useState<ParamErrors>({});
  const [editorKey, setEditorKey] = useState(0);

  const tiers: CbFeeTier[] = useMemo(() => {
    const list = status?.fee_tiers.length ? status.fee_tiers : status ? [status.fee_tier] : [];
    return list.filter((t) => t.name && t.name !== "—");
  }, [status]);
  const current = status?.fee_tier.name ?? "";
  const tierValue = tier || current;
  const selectedTier = tiers.find((t) => t.name === tierValue) ?? null;

  useEffect(() => {
    if (!name && initial) setName(initial);
  }, [initial, name]);
  useEffect(() => {
    setParams(strat?.params ?? {});
    setErrors({});
    setEditorKey((k) => k + 1);
  }, [name]);

  const bal = Number(balance);
  const dateError = start && end && start >= end ? "Start must be before end" : null;
  const balError = !Number.isFinite(bal) || bal <= 0 ? "Enter a positive amount" : null;
  const canSubmit = !!strat && strat.backtestable && !dateError && !balError && !hasErrors(errors) && busy === null;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!canSubmit || !strat) return;
    const r = await run(
      "launch",
      () =>
        cbApi.createBacktest({
          strategy: strat.name,
          params,
          start: start || undefined,
          end: end || undefined,
          starting_balance: bal,
          fee_tier: tierValue || undefined,
        }),
      { success: `Coinbase backtest started for ${strat.name}`, error: "Couldn't start the Coinbase backtest" },
    );
    if (r) navigate(`${CB_BASE}/backtests/${encodeURIComponent(String(r.id))}`);
  };

  if (candidates.length === 0) {
    return <EmptyState title="No backtestable Coinbase strategies" hint="A strategy must declare backtestable = True to replay research/coinbase/data." />;
  }

  return (
    <form className="launch-form" onSubmit={submit} noValidate>
      <div className="form-row">
        <Field label="Strategy" htmlFor="cbbt-strategy" hint={strat ? `${granularityLabel(strat.bar_granularity_s)} · ${strat.universe.length} products` : undefined}>
          <select id="cbbt-strategy" className="input" value={name} onChange={(e) => setName(e.target.value)}>
            {strategies.map((s) => (
              <option key={s.name} value={s.name} disabled={!s.backtestable}>
                {s.name}
                {s.experimental ? " (experimental)" : ""}
                {s.backtestable ? "" : " (not backtestable)"}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Start date" htmlFor="cbbt-start" error={dateError}>
          <input id="cbbt-start" className="input" type="date" value={start} max={end || undefined} onChange={(e) => setStart(e.target.value)} />
        </Field>
        <Field label="End date" htmlFor="cbbt-end">
          <input id="cbbt-end" className="input" type="date" value={end} min={start || undefined} onChange={(e) => setEnd(e.target.value)} />
        </Field>
        <Field label="Starting balance (USD)" htmlFor="cbbt-bal" error={balError}>
          <input id="cbbt-bal" className="input num-input" type="number" min={1} step={100} inputMode="decimal" value={balance} onChange={(e) => setBalance(e.target.value)} />
        </Field>
        <Field
          label="Fee tier"
          htmlFor="cbbt-tier"
          hint={selectedTier ? `${tierText(selectedTier)} · taker round trip ${fmtRate(selectedTier.taker_rate * 2)}` : "The engine's current tier"}
        >
          {tiers.length > 0 ? (
            <select id="cbbt-tier" className="input" value={tierValue} onChange={(e) => setTier(e.target.value)}>
              {tiers.map((t) => (
                <option key={t.name} value={t.name}>
                  {t.name}
                  {t.name === current ? " (current)" : ""}
                </option>
              ))}
            </select>
          ) : (
            <input id="cbbt-tier" className="input mono" value={tier} placeholder={current || "default tier"} onChange={(e) => setTier(e.target.value.trim())} spellCheck={false} />
          )}
        </Field>
      </div>
      {strat && (
        <details className="params" open>
          <summary>
            Parameters <span className="muted">(prefilled with the live Coinbase values of {strat.name})</span>
          </summary>
          <ParamEditor
            key={`${name}-${editorKey}`}
            idPrefix={`cbbt-${name}`}
            schema={strat.param_schema}
            values={params}
            onChange={(v, e) => {
              setParams(v);
              setErrors(e);
            }}
          />
        </details>
      )}
      <div className="form-actions">
        <button type="submit" className="btn btn-primary" disabled={!canSubmit} aria-busy={busy === "launch"}>
          {busy === "launch" ? <Spinner label="Starting" /> : null} Run Coinbase backtest
        </button>
        <span className="muted">
          Replays historical Coinbase candles through the same strategy and rebalance code: decides at each bar close, fills at the next bar's open ± half the spread,
          plus the taker fee. Benchmarks: BTC buy-and-hold and an equal-weight universe, same fees.
        </span>
      </div>
    </form>
  );
}

// ---------------------------------------------------------------------------
// List
// ---------------------------------------------------------------------------

function BacktestList({ list }: { list: CbBacktestSummary[] }) {
  return (
    <DataTable
      caption="Coinbase backtests"
      rows={list}
      rowKey={(b) => String(b.id)}
      defaultSort={{ key: "created", dir: "desc" }}
      columns={[
        {
          key: "id",
          header: "Run",
          sortValue: (b) => (typeof b.id === "number" ? b.id : String(b.id)),
          render: (b) => (
            <Link className="mono link" to={`${CB_BASE}/backtests/${encodeURIComponent(String(b.id))}`}>
              #{String(b.id)}
            </Link>
          ),
        },
        { key: "s", header: "Strategy", sortValue: (b) => b.strategy, render: (b) => <StrategyTag name={b.strategy} /> },
        { key: "period", header: "Period", sortValue: (b) => b.start, render: (b) => <span className="nowrap">{periodText(b)}</span> },
        { key: "tier", header: "Fee tier", sortValue: (b) => b.fee_tier, render: (b) => <span className="mono">{b.fee_tier ?? "—"}</span> },
        { key: "created", header: "Created", sortValue: (b) => parseTs(b.created_at), render: (b) => <Time value={b.created_at} stack /> },
        { key: "st", header: "Status", sortValue: (b) => b.status, render: (b) => <StatusBadge status={b.status} /> },
        {
          key: "ret",
          header: "Return",
          align: "right",
          sortValue: (b) => metric(b, "total_return_pct"),
          render: (b) => <span className={`num tone-${pnlTone(metric(b, "total_return_pct"), 0.005)}`}>{fmtPct(metric(b, "total_return_pct"), { sign: true })}</span>,
        },
        {
          key: "vsbtc",
          header: "vs BTC",
          align: "right",
          title: "Return minus BTC buy-and-hold over the same period, percentage points",
          sortValue: (b) => metric(b, "excess_return_vs_btc_pct"),
          render: (b) => <span className={`num tone-${pnlTone(metric(b, "excess_return_vs_btc_pct"), 0.005)}`}>{fmtPp(metric(b, "excess_return_vs_btc_pct"))}</span>,
        },
        { key: "cagr", header: "CAGR", align: "right", sortValue: (b) => metric(b, "cagr_pct"), render: (b) => <span className="num">{fmtPct(metric(b, "cagr_pct"), { sign: true })}</span> },
        { key: "dd", header: "Max DD", align: "right", sortValue: (b) => metric(b, "max_drawdown_pct"), render: (b) => <span className="num">{fmtDrawdownPct(metric(b, "max_drawdown_pct"))}</span> },
        { key: "sh", header: "Sharpe", align: "right", sortValue: (b) => metric(b, "sharpe"), render: (b) => <span className="num">{fmtNum(metric(b, "sharpe"), 2)}</span> },
        { key: "n", header: "Trades", align: "right", sortValue: (b) => metric(b, "trades"), render: (b) => <span className="num">{fmtInt(metric(b, "trades"))}</span> },
      ]}
    />
  );
}

function BacktestsBody() {
  const strategies = useCbPolling((signal) => cbApi.strategies({ signal }), { intervalMs: 30_000, label: "Coinbase strategies" });
  const list = useCbPolling((signal) => cbApi.backtests({ signal }), { intervalMs: 5000, label: "Coinbase backtests" });
  return (
    <>
      <PageHeader
        title="Coinbase backtests"
        subtitle="Replay historical Coinbase candles through the same spot strategies the Coinbase engine runs, against BTC buy-and-hold and an equal-weight basket."
        actions={<Freshness poll={list} />}
      />
      <Card title="New Coinbase backtest">
        <PollView<CbStrategy[]> poll={strategies} loadingLabel="Loading Coinbase strategies…">
          {(s) => <LaunchForm strategies={s} />}
        </PollView>
      </Card>
      <Card title="Runs" flush>
        <PollView<CbBacktestSummary[]> poll={list} isEmpty={(d) => d.length === 0} empty={<EmptyState title="No Coinbase backtests yet" hint="Launch one above." />}>
          {(d) => <BacktestList list={d} />}
        </PollView>
      </Card>
    </>
  );
}

export function CoinbaseBacktests() {
  return (
    <CbPage>
      <BacktestsBody />
    </CbPage>
  );
}

// ---------------------------------------------------------------------------
// Detail
// ---------------------------------------------------------------------------

interface CurveStats {
  total_return_pct: number | null;
  cagr_pct: number | null;
  volatility_pct: number | null;
  sharpe: number | null;
  sortino: number | null;
  max_drawdown_pct: number | null;
  final_equity: number | null;
}

/** Headline stats computed from an equity curve (used when the backend did not report them). */
function curveStats(pts: CbCurvePoint[]): CurveStats | null {
  const xs = pts.filter((p) => Number.isFinite(p.equity) && p.equity > 0);
  if (xs.length < 2) return null;
  const first = xs[0]!;
  const last = xs[xs.length - 1]!;
  const t0 = Date.parse(first.ts);
  const t1 = Date.parse(last.ts);
  const years = (t1 - t0) / (365.25 * 86400_000);
  const rets: number[] = [];
  const gaps: number[] = [];
  let peak = first.equity;
  let dd = 0;
  for (let i = 1; i < xs.length; i++) {
    const a = xs[i - 1]!;
    const b = xs[i]!;
    rets.push(b.equity / a.equity - 1);
    gaps.push(Date.parse(b.ts) - Date.parse(a.ts));
    peak = Math.max(peak, b.equity);
    dd = Math.max(dd, (peak - b.equity) / peak);
  }
  gaps.sort((a, b) => a - b);
  const step = gaps[Math.floor(gaps.length / 2)] ?? 86400_000;
  const perYear = step > 0 ? (365.25 * 86400_000) / step : 365;
  const mean = rets.reduce((s, r) => s + r, 0) / rets.length;
  const sd = Math.sqrt(rets.reduce((s, r) => s + (r - mean) ** 2, 0) / Math.max(1, rets.length - 1));
  const dsd = Math.sqrt(rets.reduce((s, r) => s + (r < 0 ? r * r : 0), 0) / rets.length);
  const growth = last.equity / first.equity;
  return {
    total_return_pct: (growth - 1) * 100,
    cagr_pct: years > 0.05 ? (growth ** (1 / years) - 1) * 100 : null,
    volatility_pct: sd * Math.sqrt(perYear) * 100,
    sharpe: sd > 0 ? (mean / sd) * Math.sqrt(perYear) : null,
    sortino: dsd > 0 ? (mean / dsd) * Math.sqrt(perYear) : null,
    max_drawdown_pct: dd * 100,
    final_equity: last.equity,
  };
}

type Col = "strategy" | "btc" | "equal_weight";

interface MetricDef {
  key: string;
  label: string;
  fmt: (v: number | null) => string;
  /** Which direction is better, for bolding the best column. */
  better?: "high" | "low";
  title?: string;
}

const METRICS: MetricDef[] = [
  { key: "total_return_pct", label: "Total return", fmt: (v) => fmtPct(v, { sign: true, dp: 1 }), better: "high" },
  { key: "cagr_pct", label: "CAGR", fmt: (v) => fmtPct(v, { sign: true, dp: 1 }), better: "high", title: "Compound annual growth rate" },
  { key: "volatility_pct", label: "Volatility (ann.)", fmt: (v) => fmtPct(v, { dp: 1 }), better: "low" },
  { key: "sharpe", label: "Sharpe", fmt: (v) => fmtNum(v, 2), better: "high", title: "Annualized mean return ÷ volatility (risk-free rate 0)" },
  { key: "sortino", label: "Sortino", fmt: (v) => fmtNum(v, 2), better: "high", title: "Like Sharpe, but only downside volatility counts" },
  { key: "max_drawdown_pct", label: "Max drawdown", fmt: (v) => fmtDrawdownPct(v, 1), better: "low" },
  { key: "calmar", label: "Calmar", fmt: (v) => fmtNum(v, 2), better: "high", title: "CAGR ÷ max drawdown" },
  { key: "fees", label: "Fees paid", fmt: (v) => fmtUsd(v), better: "low" },
  { key: "turnover_per_year", label: "Turnover / year", fmt: (v) => (v === null ? "—" : `${fmtNum(v, 1)}×`), title: "Traded notional ÷ average equity, per year" },
  { key: "pct_time_invested", label: "Time invested", fmt: (v) => fmtPct(v, { dp: 0 }), title: "Share of bars with any holdings" },
  { key: "avg_exposure_pct", label: "Avg exposure", fmt: (v) => fmtPct(v, { dp: 0 }), title: "Average share of equity held in coins" },
  { key: "trades", label: "Trades", fmt: (v) => fmtInt(v) },
  { key: "win_rate", label: "Win rate", fmt: (v) => fmtFrac(v), title: "Share of round trips with positive realized P&L" },
  { key: "final_equity", label: "Final equity", fmt: (v) => fmtUsd(v), better: "high" },
];

function valueFor(col: Col, key: string, reported: Record<Col, CbBacktestMetrics | null>, computed: Record<Col, CurveStats | null>): { v: number | null; computed: boolean } {
  const r = mnum(reported[col], key);
  if (r !== null) return { v: r, computed: false };
  const c = computed[col];
  const cv = c && key in c ? (c as unknown as Record<string, number | null>)[key] ?? null : null;
  return { v: cv !== null && Number.isFinite(cv) ? cv : null, computed: cv !== null };
}

function ComparisonTable({ d }: { d: CbBacktestDetail }) {
  const reported: Record<Col, CbBacktestMetrics | null> = { strategy: d.metrics, btc: d.benchmark_metrics.btc, equal_weight: d.benchmark_metrics.equal_weight };
  const computed: Record<Col, CurveStats | null> = useMemo(
    () => ({ strategy: curveStats(d.equity_curve), btc: curveStats(d.benchmarks.btc), equal_weight: curveStats(d.benchmarks.equal_weight) }),
    [d],
  );
  const cols: { key: Col; label: ReactNode }[] = [
    { key: "strategy", label: <StrategyTag name={d.strategy} /> },
    { key: "btc", label: "BTC buy-and-hold" },
    { key: "equal_weight", label: "Equal-weight" },
  ];
  const rows = METRICS.map((m) => ({ m, vals: cols.map((c) => valueFor(c.key, m.key, reported, computed)) })).filter((r) => r.vals.some((x) => x.v !== null));
  const anyComputed = rows.some((r) => r.vals.some((x) => x.computed));
  return (
    <>
      <DataTable
        caption="Strategy vs benchmarks"
        rows={rows}
        rowKey={(r) => r.m.key}
        maxHeight="none"
        // Rows are metrics of one Coinbase backtest (the card + page banner say so), not
        // account rows: no per-row venue column, so all three result columns fit.
        venue={null}
        columns={[
          { key: "m", header: "Metric", render: (r) => <span className="nowrap" title={r.m.title}>{r.m.label}</span> },
          ...cols.map((c, i) => ({
            key: c.key,
            header: c.label,
            align: "right" as const,
            className: "cb-compare",
            render: (r: (typeof rows)[number]) => {
              const x = r.vals[i]!;
              const nums = r.vals.map((y) => y.v).filter((y): y is number => y !== null);
              const best = r.m.better && nums.length > 1 && x.v !== null ? (r.m.better === "high" ? x.v === Math.max(...nums) : x.v === Math.min(...nums)) : false;
              return (
                <span className={`num${best ? " cb-best" : ""}`} title={x.computed ? "Computed in the browser from the equity curve (not reported by the backtester)" : undefined}>
                  {best ? <strong>{r.m.fmt(x.v)}</strong> : r.m.fmt(x.v)}
                  {x.computed ? "*" : ""}
                </span>
              );
            },
          })),
        ]}
      />
      <p className="cb-note">
        Bold = best of the three where "better" is clear-cut. Benchmarks pay the same fees.{" "}
        {anyComputed ? "* computed in the browser from the curve (not reported by the backtester)." : ""}
      </p>
    </>
  );
}

const CURVE_SERIES: CbSeries[] = [
  { key: "strategy", label: "Strategy", role: "venue", area: true },
  { key: "btc", label: "BTC buy-and-hold", role: "btc" },
  { key: "ew", label: "Equal-weight universe", role: "context", dashed: true },
];

function mergeCurves(d: CbBacktestDetail): CbRow[] {
  const m = new Map<number, CbRow>();
  const put = (pts: CbCurvePoint[], key: string) => {
    for (const p of pts) {
      const t = Date.parse(p.ts);
      if (!Number.isFinite(t)) continue;
      const row = m.get(t) ?? ({ t, strategy: null, btc: null, ew: null } as CbRow);
      row[key] = p.equity;
      m.set(t, row);
    }
  };
  put(d.equity_curve, "strategy");
  put(d.benchmarks.btc, "btc");
  put(d.benchmarks.equal_weight, "ew");
  return [...m.values()].sort((a, b) => a.t - b.t);
}

/** Underwater curves (drawdown from the running peak, as negative %). */
function drawdowns(rows: CbRow[], keys: string[]): CbRow[] {
  const peak: Record<string, number> = {};
  return rows.map((r) => {
    const out: CbRow = { t: r.t };
    for (const k of keys) {
      const v = r[k];
      if (typeof v !== "number" || !(v > 0)) {
        out[k] = null;
        continue;
      }
      peak[k] = Math.max(peak[k] ?? v, v);
      out[k] = -((peak[k]! - v) / peak[k]!) * 100;
    }
    return out;
  });
}

const pctTick = (v: number) => fmtPct(v, { dp: Math.abs(v) < 10 && v % 1 !== 0 ? 1 : 0 });

function DetailsCard({ d }: { d: CbBacktestDetail }) {
  const det = d.details;
  if (!det) return null;
  const biases = Array.isArray(det.known_biases) ? det.known_biases.map(String) : [];
  const errors = Array.isArray(det.errors) ? det.errors.map((e) => (typeof e === "string" ? e : JSON.stringify(e))) : [];
  const skip = det.skip_reasons && typeof det.skip_reasons === "object" ? Object.entries(det.skip_reasons as Record<string, unknown>) : [];
  const slip = det.slippage && typeof det.slippage === "object" ? (det.slippage as Record<string, unknown>) : null;
  const ds = det.dataset && typeof det.dataset === "object" ? (det.dataset as Record<string, unknown>) : null;
  return (
    <Card title="Method & known biases" subtitle="As reported by the backtester">
      <dl className="kv">
        {typeof det.look_ahead === "string" && (
          <>
            <dt>Look-ahead</dt>
            <dd>{det.look_ahead}</dd>
          </>
        )}
        {slip && (
          <>
            <dt>Slippage</dt>
            <dd>
              {String(slip.mode ?? "—")}
              {typeof slip.default_bps === "number" ? ` · default ${fmtBps(slip.default_bps)}` : ""}
            </dd>
          </>
        )}
        {ds && (
          <>
            <dt>Dataset</dt>
            <dd className="mono wrap">
              {Object.entries(ds)
                .map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
                .join(" · ")}
            </dd>
          </>
        )}
        {skip.length > 0 && (
          <>
            <dt>Skipped intents</dt>
            <dd>{skip.map(([k, v]) => `${k} (${String(v)})`).join(" · ")}</dd>
          </>
        )}
        {errors.length > 0 && (
          <>
            <dt>Errors</dt>
            <dd className="mono tone-neg wrap">{errors.join(" · ")}</dd>
          </>
        )}
      </dl>
      {biases.length > 0 && (
        <>
          <h3 className="sub-title">Known biases</h3>
          <ul>
            {biases.map((b) => (
              <li key={b}>{b}</li>
            ))}
          </ul>
        </>
      )}
    </Card>
  );
}

function BacktestDetailView({ id }: { id: string }) {
  const poll = usePolling((signal) => cbApi.backtest(id, { signal }), {
    intervalMs: 5000,
    label: `Coinbase backtest #${id}`,
    silentWhen: isCbUnavailable,
    until: ({ data, error }) => (data !== undefined && (isDone(data.status) || isFailed(data.status))) || (error instanceof ApiError && error.status === 404),
  });
  const d = poll.data;
  const rows = useMemo(() => (d ? mergeCurves(d) : []), [d]);
  const ddRows = useMemo(() => drawdowns(rows, ["strategy", "btc", "ew"]), [rows]);
  const years: GroupedRow[] = useMemo(
    () => (d?.by_year ?? []).map((y) => ({ name: y.period, strategy: y.return_pct, btc: y.btc_return_pct, ew: y.equal_weight_return_pct })),
    [d],
  );
  const months: SignedBarRow[] = useMemo(
    () => (d?.by_month ?? []).map((m) => ({ name: m.period, pnl: m.pnl, return_pct: m.return_pct, btc: m.btc_return_pct, trades: m.trades })),
    [d],
  );

  if (!d) return poll.loading ? <LoadingBlock label="Loading Coinbase backtest…" /> : <ErrorBlock error={poll.error} onRetry={poll.refresh} />;
  const done = isDone(d.status);
  const failed = isFailed(d.status);
  const running = !done && !failed;
  const m = d.metrics;
  const btcComputed = curveStats(d.benchmarks.btc);
  const btcRet = mnum(d.benchmark_metrics.btc, "total_return_pct") ?? btcComputed?.total_return_pct ?? null;
  const btcDd = mnum(d.benchmark_metrics.btc, "max_drawdown_pct") ?? btcComputed?.max_drawdown_pct ?? null;
  const ret = mnum(m, "total_return_pct") ?? curveStats(d.equity_curve)?.total_return_pct ?? null;
  const excess = mnum(m, "excess_return_vs_btc_pct") ?? (ret !== null && btcRet !== null ? ret - btcRet : null);
  const dd = mnum(m, "max_drawdown_pct");
  const startEq = rows.find((r) => typeof r.strategy === "number")?.strategy ?? d.starting_balance ?? undefined;
  const hasBench = d.benchmarks.btc.length > 0 || d.benchmarks.equal_weight.length > 0;
  const series = CURVE_SERIES.filter((s) => s.key === "strategy" || (s.key === "btc" ? d.benchmarks.btc.length > 0 : d.benchmarks.equal_weight.length > 0));

  return (
    <>
      <PageHeader
        title={`Coinbase backtest #${String(d.id)}`}
        subtitle={
          <>
            <StrategyTag name={d.strategy} /> · {d.period_reported ? periodText(d) : "period not reported"}
            {d.fee_tier && (
              <>
                {" "}
                · fee tier <span className="mono">{d.fee_tier}</span>
              </>
            )}
            {d.granularity_s !== null && <> · {granularityLabel(d.granularity_s).toLowerCase()}</>}
            {d.created_at && (
              <>
                {" "}
                · created <Time value={d.created_at} />
              </>
            )}
          </>
        }
        actions={
          <>
            <StatusBadge status={d.status} />
            <Link to={`${CB_BASE}/backtests`} className="btn btn-sm btn-ghost">
              ← All Coinbase backtests
            </Link>
          </>
        }
      />
      {running && (
        <div className="banner banner-info" role="status">
          <Spinner /> <div>Coinbase backtest is running — results load automatically when it finishes.</div>
        </div>
      )}
      {(d.error || failed) && (
        <div className="banner banner-bad" role="alert">
          <div>
            <strong>Coinbase backtest failed — no results.</strong> <span className="mono wrap">{d.error || `status "${d.status}" reported without an error message`}</span>
          </div>
        </div>
      )}
      {done && (
        <div className="kpi-grid">
          <KpiTile
            hero
            label="Strategy return (after fees)"
            value={fmtPct(ret, { sign: true, dp: 1 })}
            tone={pnlTone(ret, 0.005)}
            sub={
              <>
                BTC buy-and-hold {fmtPct(btcRet, { sign: true, dp: 1 })} · difference <strong>{fmtPp(excess)}</strong>
              </>
            }
          />
          <KpiTile
            label="Max drawdown"
            value={fmtDrawdownPct(dd, 1)}
            sub={btcDd !== null ? `BTC ${fmtDrawdownPct(btcDd, 1)}${dd !== null ? ` · ${dd < btcDd ? "shallower" : "deeper"} than holding` : ""}` : undefined}
          />
          <KpiTile label="CAGR" value={fmtPct(mnum(m, "cagr_pct"), { sign: true, dp: 1 })} />
          <KpiTile label="Sharpe" value={fmtNum(mnum(m, "sharpe"), 2)} sub={`Sortino ${fmtNum(mnum(m, "sortino"), 2)}`} />
          <KpiTile label="Fees paid" value={fmtUsd(mnum(m, "fees") ?? mnum(m, "fees_paid"))} sub={d.fee_tier ? `tier ${d.fee_tier}` : undefined} />
          <KpiTile label="Time invested" value={fmtPct(mnum(m, "pct_time_invested"), { dp: 0 })} sub={`${fmtInt(mnum(m, "trades"))} trades · win rate ${fmtFrac(mnum(m, "win_rate"))}`} />
        </div>
      )}
      {done && (
        <>
          <Card title="Equity: strategy vs benchmarks" subtitle={hasBench ? "Same starting balance and fees; switch to Log to compare growth rates" : "Benchmarks not reported"}>
            {rows.length < 2 ? (
              <EmptyState title="No equity curve" />
            ) : (
              <CbTimeSeriesChart
                rows={rows}
                label="Coinbase backtest equity vs BTC buy-and-hold and equal-weight"
                series={series}
                allowLog
                height={300}
                baseline={startEq !== undefined && startEq !== null ? { value: startEq, label: `start ${fmtUsd(startEq)}` } : undefined}
                table={
                  <DataTable
                    caption="Backtest equity vs benchmarks"
                    rows={rows}
                    rowKey={(r) => String(r.t)}
                    maxHeight={320}
                    columns={[
                      { key: "t", header: "Date", render: (r) => fmtDate(new Date(r.t).toISOString()) },
                      { key: "s", header: "Strategy", align: "right", render: (r) => <Usd value={r.strategy} /> },
                      { key: "b", header: "BTC buy-and-hold", align: "right", render: (r) => <Usd value={r.btc} /> },
                      { key: "e", header: "Equal-weight", align: "right", render: (r) => <Usd value={r.ew} /> },
                    ]}
                  />
                }
              />
            )}
          </Card>
          <div className="grid grid-2">
            <Card title="Drawdown from peak" subtitle="How far each curve sat below its own high">
              {ddRows.length < 2 ? (
                <EmptyState title="No equity curve" />
              ) : (
                <CbTimeSeriesChart
                  rows={ddRows}
                  label="Drawdown: strategy vs BTC buy-and-hold and equal-weight"
                  series={series.map((s) => ({ ...s, area: false }))}
                  format={(v) => fmtPct(v, { dp: 1 })}
                  yTickFormat={pctTick}
                  table={
                    <DataTable
                      caption="Drawdowns"
                      rows={ddRows}
                      rowKey={(r) => String(r.t)}
                      maxHeight={320}
                      columns={[
                        { key: "t", header: "Date", render: (r) => fmtDate(new Date(r.t).toISOString()) },
                        { key: "s", header: "Strategy", align: "right", render: (r) => <span className="num">{fmtPct(r.strategy, { dp: 1 })}</span> },
                        { key: "b", header: "BTC", align: "right", render: (r) => <span className="num">{fmtPct(r.btc, { dp: 1 })}</span> },
                        { key: "e", header: "Equal-weight", align: "right", render: (r) => <span className="num">{fmtPct(r.ew, { dp: 1 })}</span> },
                      ]}
                    />
                  }
                />
              )}
            </Card>
            <Card title="Strategy vs benchmarks" subtitle="Headline metrics over the whole replay" flush>
              <ComparisonTable d={d} />
            </Card>
          </div>
          <div className="grid grid-2">
            <Card title="Return by year">
              {years.length === 0 ? (
                <EmptyState title="No yearly breakdown" />
              ) : (
                <CbGroupedColumns
                  rows={years}
                  series={[
                    { key: "strategy", label: "Strategy", role: "venue" },
                    { key: "btc", label: "BTC buy-and-hold", role: "btc" },
                    { key: "ew", label: "Equal-weight", role: "context" },
                  ]}
                  label="Coinbase backtest return by year vs benchmarks"
                  table={
                    <DataTable
                      caption="Return by year"
                      rows={d.by_year}
                      rowKey={(y) => y.period}
                      maxHeight="none"
                      columns={[
                        { key: "y", header: "Year", render: (y) => <span className="mono">{y.period}</span> },
                        { key: "r", header: "Strategy", align: "right", render: (y) => <span className="num">{fmtPct(y.return_pct, { sign: true })}</span> },
                        { key: "b", header: "BTC", align: "right", render: (y) => <span className="num">{fmtPct(y.btc_return_pct, { sign: true })}</span> },
                        { key: "e", header: "Equal-weight", align: "right", render: (y) => <span className="num">{fmtPct(y.equal_weight_return_pct, { sign: true })}</span> },
                        { key: "p", header: "P&L", align: "right", render: (y) => <Pnl value={y.pnl} /> },
                        { key: "t", header: "Trades", align: "right", render: (y) => fmtInt(y.trades) },
                        { key: "f", header: "Fees", align: "right", render: (y) => <Usd value={y.fees} /> },
                      ]}
                    />
                  }
                />
              )}
            </Card>
            <Card title="P&L by month" subtitle="Strategy only; the table twin lists BTC and equal-weight returns per month">
              {months.length === 0 ? (
                <EmptyState title="No monthly breakdown" />
              ) : (
                <SignedColumnChart
                  rows={months}
                  valueKey="pnl"
                  label="Coinbase backtest P&L by month"
                  extraTooltip={(r) => [
                    { label: "strategy return", value: fmtPct(r.return_pct === null ? null : Number(r.return_pct), { sign: true }) },
                    { label: "BTC return", value: fmtPct(r.btc === null ? null : Number(r.btc), { sign: true }) },
                    { label: "trades", value: fmtInt(r.trades === null ? null : Number(r.trades)) },
                  ]}
                  table={
                    <DataTable
                      caption="Return by month"
                      rows={d.by_month}
                      rowKey={(x) => x.period}
                      maxHeight={320}
                      columns={[
                        { key: "m", header: "Month", render: (x) => <span className="mono">{x.period}</span> },
                        { key: "r", header: "Strategy", align: "right", render: (x) => <span className="num">{fmtPct(x.return_pct, { sign: true })}</span> },
                        { key: "b", header: "BTC", align: "right", render: (x) => <span className="num">{fmtPct(x.btc_return_pct, { sign: true })}</span> },
                        { key: "e", header: "Equal-weight", align: "right", render: (x) => <span className="num">{fmtPct(x.equal_weight_return_pct, { sign: true })}</span> },
                        { key: "p", header: "P&L", align: "right", render: (x) => <Pnl value={x.pnl} /> },
                        { key: "t", header: "Trades", align: "right", render: (x) => fmtInt(x.trades) },
                      ]}
                    />
                  }
                />
              )}
            </Card>
          </div>
          <Card title="Trades" subtitle={`${fmtInt(d.trades.length)} simulated fills (taker, next bar open)`} flush>
            <DataTable
              caption="Coinbase backtest trades"
              rows={d.trades}
              rowKey={(t, i) => `${t.ts}-${t.product_id}-${i}`}
              defaultSort={{ key: "ts", dir: "asc" }}
              empty={<EmptyState title="No trades" hint="The strategy stayed in cash for the whole period." />}
              columns={[
                { key: "ts", header: "Date", sortValue: (t) => parseTs(t.ts), render: (t) => <span className="nowrap">{fmtDate(t.ts)}</span> },
                { key: "p", header: "Product", sortValue: (t) => t.product_id, render: (t) => <ProductCell pid={t.product_id} /> },
                { key: "side", header: "Side", sortValue: (t) => t.side, render: (t) => <CbSideTag side={t.side} /> },
                { key: "q", header: "Quantity", align: "right", sortValue: (t) => t.base_size, render: (t) => <span className="num nowrap">{fmtQty(t.base_size, baseOf(t.product_id))}</span> },
                { key: "px", header: "Price", align: "right", sortValue: (t) => t.price, render: (t) => <Price value={t.price} /> },
                { key: "n", header: "Notional", align: "right", sortValue: (t) => t.notional, render: (t) => <Usd value={t.notional} /> },
                { key: "fee", header: "Fee", align: "right", sortValue: (t) => t.fee, render: (t) => <Fee fee={t.fee} rate={t.fee_rate} notional={t.notional} /> },
                { key: "slip", header: "Slippage", align: "right", title: "Half-spread applied vs the bar open", sortValue: (t) => t.slippage_bps, render: (t) => <span className="num">{fmtBps(t.slippage_bps)}</span> },
                { key: "pnl", header: "Realized", align: "right", sortValue: (t) => t.pnl, render: (t) => <Pnl value={t.pnl} /> },
                { key: "w", header: "Target", align: "right", sortValue: (t) => t.target_weight, render: (t) => <Weight value={t.target_weight} /> },
                { key: "why", header: "Reason", minWidth: 180, render: (t) => <ClampText text={t.reason ?? ""} className="muted" /> },
              ]}
            />
          </Card>
          {d.signals.length > 0 && (
            <Card title="Replay decisions" subtitle="Order intents the replay rejected or skipped (and why)" flush>
              <DataTable
                caption="Backtest decisions"
                rows={d.signals}
                rowKey={(s, i) => `${s.ts}-${s.product_id}-${i}`}
                maxHeight={360}
                columns={[
                  { key: "ts", header: "Date", render: (s) => <span className="nowrap">{fmtDate(s.ts)}</span> },
                  { key: "p", header: "Product", render: (s) => <ProductCell pid={s.product_id} /> },
                  { key: "side", header: "Side", render: (s) => <CbSideTag side={s.side} /> },
                  { key: "w", header: "Target", align: "right", render: (s) => <Weight value={s.target_weight} /> },
                  { key: "d", header: "Decision", render: (s) => <CbDecisionBadge decision={s.decision} raw={s.decision_raw} /> },
                  { key: "r", header: "Reason", minWidth: 220, render: (s) => <ClampText text={s.decision_reason || s.reason} /> },
                ]}
              />
            </Card>
          )}
        </>
      )}
      {Object.keys(d.params).length > 0 && (
        <Card title="Parameters">
          <dl className="kv kv-grid">
            {Object.entries(d.params).map(([k, v]) => (
              <div key={k}>
                <dt className="mono">{k}</dt>
                <dd className="mono">{typeof v === "string" ? v : JSON.stringify(v)}</dd>
              </div>
            ))}
          </dl>
          {d.universe.length > 0 && (
            <>
              <h3 className="sub-title">Universe ({d.universe.length})</h3>
              <ul className="cb-chips">
                {d.universe.map((p) => (
                  <li key={p}>{p}</li>
                ))}
              </ul>
            </>
          )}
        </Card>
      )}
      {done && <DetailsCard d={d} />}
    </>
  );
}

function BacktestPageBody() {
  const { id = "" } = useParams();
  return <BacktestDetailView key={id} id={id} />;
}

export function CoinbaseBacktestPage() {
  return (
    <CbPage>
      <BacktestPageBody />
    </CbPage>
  );
}
