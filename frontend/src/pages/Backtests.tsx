import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { api, ApiError } from "../api/client";
import type { BacktestMetrics, BacktestStatus, BacktestSummary, ParamValue, Strategy } from "../api/types";
import { SignedColumnChart, type SignedBarRow } from "../charts/BarCharts";
import { TimeSeriesChart, type TimeRow } from "../charts/TimeSeriesChart";
import { DataTable } from "../components/DataTable";
import { hasErrors, ParamEditor, type ParamErrors } from "../components/ParamEditor";
import { Badge, Card, EmptyState, ErrorBlock, Field, Freshness, KpiTile, LoadingBlock, PageHeader, PollView, Spinner } from "../components/ui";
import { Cents, ClampText, Pnl, ResultTag, SideTag, StrategyTag, Time, Usd } from "../components/values";
import { useAction, usePolling } from "../lib/hooks";
import {
  centsTone,
  fmtCalendarDate,
  fmtCents,
  fmtDate,
  fmtDrawdownPct,
  fmtDrawdownUsd,
  fmtFrac,
  fmtInt,
  fmtNum,
  fmtPct,
  fmtPnl,
  fmtUsd,
  humanize,
  isoLocalDay,
  parseTs,
  pnlTone,
  type PnlTone,
} from "../lib/format";

const isDone = (s: BacktestStatus) => ["done", "completed", "complete", "finished", "succeeded", "success"].includes(s);
const isFailed = (s: BacktestStatus) => ["failed", "error", "errored", "cancelled"].includes(s);

export function BacktestStatusBadge({ status }: { status: BacktestStatus }) {
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

const num = (m: BacktestMetrics | null, k: string): number | null => {
  const v = m?.[k];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
};

/** A list-row metric; a failed run shows "—" even if it carried partial metrics. */
const metric = (b: BacktestSummary, k: string): number | null => (isFailed(b.status) ? null : num(b.metrics, k));

// ---------------------------------------------------------------------------
// Launch form
// ---------------------------------------------------------------------------

/** "Jan 1, 2025 → Dec 31, 2025"; "full dataset" when both ends are open; "—" when not reported. */
function periodText(b: { start: string | null; end: string | null; period_reported: boolean }): string {
  if (!b.period_reported) return "—";
  if (!b.start && !b.end) return "full dataset";
  return `${b.start ? fmtCalendarDate(b.start) : "start of data"} → ${b.end ? fmtCalendarDate(b.end) : "end of data"}`;
}

function LaunchForm({ strategies }: { strategies: Strategy[] }) {
  const [sp] = useSearchParams();
  const navigate = useNavigate();
  const { busy, run } = useAction();
  const candidates = strategies.filter((s) => s.backtestable);
  const initial = candidates.find((s) => s.name === sp.get("strategy"))?.name ?? candidates[0]?.name ?? "";
  const [name, setName] = useState(initial);
  const strat = strategies.find((s) => s.name === name);
  const today = new Date();
  // Local calendar days (what <input type="date"> shows), not the UTC day.
  const [start, setStart] = useState(() => {
    const d = new Date(today);
    d.setFullYear(d.getFullYear() - 1);
    return isoLocalDay(d);
  });
  const [end, setEnd] = useState(isoLocalDay(today));
  const [balance, setBalance] = useState("1000");
  const [params, setParams] = useState<Record<string, ParamValue>>(strat?.params ?? {});
  const [errors, setErrors] = useState<ParamErrors>({});
  const [editorKey, setEditorKey] = useState(0);

  useEffect(() => {
    if (!name && initial) setName(initial);
  }, [initial, name]);
  useEffect(() => {
    setParams(strat?.params ?? {});
    setErrors({});
    setEditorKey((k) => k + 1);
    // Reload the live params only when the selected strategy changes.
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
      () => api.createBacktest({ strategy: strat.name, params, start: start || undefined, end: end || undefined, starting_balance: bal }),
      { success: `Backtest started for ${strat.name}`, error: "Couldn't start the backtest" },
    );
    if (r) navigate(`/kalshi/backtests/${encodeURIComponent(String(r.id))}`);
  };

  if (candidates.length === 0) {
    return <EmptyState title="No backtestable strategies" hint="Strategies must declare backtestable = True to replay historical candle data." />;
  }

  return (
    <form className="launch-form" onSubmit={submit} noValidate>
      <div className="form-row">
        <Field label="Strategy" htmlFor="bt-strategy">
          <select id="bt-strategy" className="input" value={name} onChange={(e) => setName(e.target.value)}>
            {strategies.map((s) => (
              <option key={s.name} value={s.name} disabled={!s.backtestable}>
                {s.name}
                {s.backtestable ? "" : " (not backtestable)"}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Start date" htmlFor="bt-start" error={dateError}>
          <input id="bt-start" className="input" type="date" value={start} max={end || undefined} onChange={(e) => setStart(e.target.value)} />
        </Field>
        <Field label="End date" htmlFor="bt-end">
          <input id="bt-end" className="input" type="date" value={end} min={start || undefined} onChange={(e) => setEnd(e.target.value)} />
        </Field>
        <Field label="Starting balance ($)" htmlFor="bt-bal" error={balError}>
          <input id="bt-bal" className="input num-input" type="number" min={1} step={100} inputMode="decimal" value={balance} onChange={(e) => setBalance(e.target.value)} />
        </Field>
      </div>
      {strat && (
        <details className="params" open>
          <summary>
            Parameters <span className="muted">(prefilled with the live values of {strat.name})</span>
          </summary>
          <ParamEditor
            key={`${name}-${editorKey}`}
            idPrefix={`bt-${name}`}
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
          {busy === "launch" ? <Spinner label="Starting" /> : null} Run backtest
        </button>
        <span className="muted">Replays the historical dataset through the same strategy code with walk-forward candles, synthetic books and Kalshi fees.</span>
      </div>
    </form>
  );
}

// ---------------------------------------------------------------------------
// List
// ---------------------------------------------------------------------------

function BacktestList({ list }: { list: BacktestSummary[] }) {
  return (
    <DataTable
      caption="Backtests"
      rows={list}
      rowKey={(b) => String(b.id)}
      defaultSort={{ key: "created", dir: "desc" }}
      columns={[
        {
          key: "id",
          header: "Run",
          sortValue: (b) => (typeof b.id === "number" ? b.id : String(b.id)),
          render: (b) => (
            <Link className="mono link" to={`/kalshi/backtests/${encodeURIComponent(String(b.id))}`}>
              #{String(b.id)}
            </Link>
          ),
        },
        { key: "s", header: "Strategy", sortValue: (b) => b.strategy, render: (b) => <StrategyTag name={b.strategy} /> },
        {
          key: "period",
          header: "Period",
          sortValue: (b) => b.start,
          render: (b) => <span className="nowrap">{periodText(b)}</span>,
        },
        { key: "created", header: "Created", sortValue: (b) => parseTs(b.created_at), render: (b) => <Time value={b.created_at} stack /> },
        { key: "st", header: "Status", sortValue: (b) => b.status, render: (b) => <BacktestStatusBadge status={b.status} /> },
        { key: "pnl", header: "P&L", align: "right", sortValue: (b) => metric(b, "total_pnl"), render: (b) => <Pnl value={metric(b, "total_pnl")} /> },
        { key: "n", header: "Trades", align: "right", sortValue: (b) => metric(b, "n_trades"), render: (b) => <span className="num">{fmtInt(metric(b, "n_trades"))}</span> },
        {
          key: "ev",
          header: "EV / ct",
          align: "right",
          title: "Mean P&L per contract (95% CI clustered by event)",
          sortValue: (b) => metric(b, "ev_per_contract"),
          render: (b) => {
            const lo = metric(b, "ev_ci_low");
            const hi = metric(b, "ev_ci_high");
            return (
              <span className="num nowrap" title={lo !== null && hi !== null ? `95% CI ${fmtCents(lo, { sign: true, dp: 2 })} to ${fmtCents(hi, { sign: true, dp: 2 })}` : undefined}>
                <Cents value={metric(b, "ev_per_contract")} sign tone dp={2} />
                {lo !== null && hi !== null && <span className="muted"> [{fmtCents(lo, { sign: true, dp: 1 })}, {fmtCents(hi, { sign: true, dp: 1 })}]</span>}
              </span>
            );
          },
        },
        { key: "hit", header: "Hit rate", align: "right", sortValue: (b) => metric(b, "hit_rate"), render: (b) => <span className="num">{fmtFrac(metric(b, "hit_rate"))}</span> },
        {
          key: "dd",
          header: "Max DD",
          align: "right",
          sortValue: (b) => metric(b, "max_drawdown_pct"),
          render: (b) => <span className="num">{fmtDrawdownPct(metric(b, "max_drawdown_pct"))}</span>,
        },
        { key: "sh", header: "Sharpe", align: "right", sortValue: (b) => metric(b, "sharpe"), render: (b) => <span className="num">{fmtNum(metric(b, "sharpe"), 2)}</span> },
      ]}
    />
  );
}

export function Backtests() {
  const strategies = usePolling((signal) => api.strategies({ signal }), { intervalMs: 30_000, label: "strategies" });
  const list = usePolling((signal) => api.backtests({ signal }), { intervalMs: 5000, label: "backtests" });
  return (
    <div className="page">
      <PageHeader title="Backtests" subtitle="Replay historical Kalshi data through the same strategy classes the engine runs." actions={<Freshness poll={list} />} />
      <Card title="New backtest">
        <PollView<Strategy[]> poll={strategies} loadingLabel="Loading strategies…">
          {(s) => <LaunchForm strategies={s} />}
        </PollView>
      </Card>
      <Card title="Runs" flush>
        <PollView<BacktestSummary[]> poll={list} isEmpty={(d) => d.length === 0} empty={<EmptyState title="No backtests yet" hint="Launch one above." />}>
          {(d) => <BacktestList list={d} />}
        </PollView>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Detail
// ---------------------------------------------------------------------------

const KNOWN_METRICS: {
  key: string;
  label: string;
  fmt: (v: number) => string;
  tone?: (v: number) => PnlTone;
  sub?: (m: BacktestMetrics) => string | undefined;
}[] = [
  { key: "total_pnl", label: "Total P&L", fmt: (v) => fmtPnl(v), tone: (v) => pnlTone(v) },
  { key: "total_return_pct", label: "Return", fmt: (v) => fmtPct(v, { sign: true, dp: 2 }), tone: (v) => pnlTone(v, 0.005) },
  { key: "final_equity", label: "Final equity", fmt: (v) => fmtUsd(v) },
  { key: "n_trades", label: "Trades", fmt: (v) => fmtInt(v) },
  { key: "contracts", label: "Contracts", fmt: (v) => fmtInt(v) },
  {
    key: "ev_per_contract",
    label: "EV / contract",
    // The value alone: the tile truncates long text, and the CI decides whether the
    // edge is real, so it goes on its own line (never ellipsized away).
    fmt: (v) => fmtCents(v, { sign: true, dp: 2 }),
    tone: (v) => centsTone(v, 2),
    sub: (m) => {
      const lo = num(m, "ev_ci_low");
      const hi = num(m, "ev_ci_high");
      return lo !== null && hi !== null
        ? `95% CI ${fmtCents(lo, { sign: true, dp: 2 })} to ${fmtCents(hi, { sign: true, dp: 2 })} · clustered by event`
        : "no confidence interval reported";
    },
  },
  { key: "hit_rate", label: "Hit rate", fmt: (v) => fmtFrac(v) },
  { key: "max_drawdown", label: "Max drawdown", fmt: (v) => fmtDrawdownUsd(v) },
  { key: "max_drawdown_pct", label: "Max drawdown %", fmt: (v) => fmtDrawdownPct(v) },
  { key: "sharpe", label: "Sharpe-like (ann.)", fmt: (v) => fmtNum(v, 2) },
  { key: "fees", label: "Fees", fmt: (v) => fmtUsd(v) },
];
const SKIP_METRICS = new Set([...KNOWN_METRICS.map((k) => k.key), "ev_ci_low", "ev_ci_high"]);

function MetricsGrid({ m }: { m: BacktestMetrics }) {
  const extra = Object.entries(m).filter(([k, v]) => !SKIP_METRICS.has(k) && (typeof v === "number" || typeof v === "string"));
  return (
    <div className="kpi-grid">
      {KNOWN_METRICS.map((k) => {
        const v = num(m, k.key);
        if (v === null) return null;
        return <KpiTile key={k.key} label={k.label} value={k.fmt(v)} tone={k.tone?.(v)} sub={k.sub?.(m)} />;
      })}
      {extra.map(([k, v]) => (
        <KpiTile key={k} label={humanize(k)} value={typeof v === "number" ? fmtNum(v, Number.isInteger(v) ? 0 : 4) : String(v)} />
      ))}
    </div>
  );
}

function BacktestDetailView({ id }: { id: string }) {
  // Poll while running; stop (without a second fetch) once the run is finished or the
  // id does not exist. Coming back to the tab does not refetch a finished run.
  const poll = usePolling((signal) => api.backtest(id, { signal }), {
    intervalMs: 5000,
    label: `backtest #${id}`,
    until: ({ data, error }) =>
      (data !== undefined && (isDone(data.status) || isFailed(data.status))) || (error instanceof ApiError && error.status === 404),
  });
  const d = poll.data;

  const curve: TimeRow[] = useMemo(() => (d?.equity_curve ?? []).map((p) => ({ t: Date.parse(p.ts), equity: p.equity })), [d]);
  const months: SignedBarRow[] = useMemo(
    () => (d?.by_month ?? []).map((m) => ({ name: m.month, pnl: m.pnl, trades: m.trades, win_rate: m.win_rate })),
    [d],
  );

  if (!d) return poll.loading ? <LoadingBlock label="Loading backtest…" /> : <ErrorBlock error={poll.error} onRetry={poll.refresh} />;
  const done = isDone(d.status);
  const failed = isFailed(d.status);
  const running = !done && !failed;
  const startEq = curve[0]?.equity ?? undefined;

  return (
    <>
      <PageHeader
        title={`Backtest #${String(d.id)}`}
        subtitle={
          <>
            <StrategyTag name={d.strategy} /> · {d.period_reported ? periodText(d) : "period not reported"}
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
            <BacktestStatusBadge status={d.status} />
            <Link to="/kalshi/backtests" className="btn btn-sm btn-ghost">
              ← All backtests
            </Link>
          </>
        }
      />
      {running && (
        <div className="banner banner-info" role="status">
          <Spinner /> <div>Backtest is running — results load automatically when it finishes.</div>
        </div>
      )}
      {(d.error || failed) && (
        <div className="banner banner-bad" role="alert">
          <div>
            <strong>Backtest failed — no results.</strong> <span className="mono wrap">{d.error || `status "${d.status}" reported without an error message`}</span>
          </div>
        </div>
      )}
      {done && d.metrics && <MetricsGrid m={d.metrics} />}
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
        </Card>
      )}
      {done && (
        <>
          <div className="grid grid-2">
            <Card title="Equity curve" subtitle="Simulated account equity over the replay">
              {curve.length < 2 ? (
                <EmptyState title="No equity curve" />
              ) : (
                <TimeSeriesChart
                  rows={curve}
                  label="Backtest equity"
                  series={[{ key: "equity", label: "Equity", role: "primary" }]}
                  baseline={startEq !== undefined ? { value: startEq, label: `start ${fmtUsd(startEq)}` } : undefined}
                  table={
                    <DataTable
                      caption="Backtest equity"
                      rows={curve}
                      rowKey={(r) => String(r.t)}
                      maxHeight={300}
                      columns={[
                        { key: "t", header: "Date", render: (r) => fmtDate(new Date(r.t).toISOString()) },
                        { key: "e", header: "Equity", align: "right", render: (r) => <Usd value={r.equity} /> },
                      ]}
                    />
                  }
                />
              )}
            </Card>
            <Card title="P&L by month">
              {months.length === 0 ? (
                <EmptyState title="No monthly breakdown" />
              ) : (
                <SignedColumnChart
                  rows={months}
                  valueKey="pnl"
                  label="Backtest P&L by month"
                  extraTooltip={(r) => [
                    { label: "trades", value: fmtInt(r.trades === null ? null : Number(r.trades)) },
                    { label: "win rate", value: fmtFrac(r.win_rate === null ? null : Number(r.win_rate)) },
                  ]}
                  table={
                    <DataTable
                      caption="P&L by month"
                      rows={d.by_month}
                      rowKey={(m) => m.month}
                      maxHeight="none"
                      columns={[
                        { key: "m", header: "Month", render: (m) => <span className="mono">{m.month}</span> },
                        { key: "p", header: "P&L", align: "right", render: (m) => <Pnl value={m.pnl} /> },
                        { key: "t", header: "Trades", align: "right", render: (m) => fmtInt(m.trades) },
                        { key: "c", header: "Contracts", align: "right", render: (m) => fmtInt(m.contracts) },
                        { key: "w", header: "Win rate", align: "right", render: (m) => fmtFrac(m.win_rate) },
                      ]}
                    />
                  }
                />
              )}
            </Card>
          </div>
          <Card title="Trades" subtitle={`${fmtInt(d.trades.length)} simulated trades`} flush>
            <DataTable
              caption="Backtest trades"
              rows={d.trades}
              rowKey={(t, i) => `${t.ts}-${t.ticker}-${i}`}
              defaultSort={{ key: "ts", dir: "asc" }}
              empty={<EmptyState title="No trades" hint="The strategy found no entries in this period." />}
              columns={[
                { key: "ts", header: "Entry", sortValue: (t) => parseTs(t.ts), render: (t) => <span className="nowrap">{fmtDate(t.ts)}</span> },
                { key: "tk", header: "Ticker", sortValue: (t) => t.ticker, render: (t) => <span className="ticker">{t.ticker}</span> },
                { key: "side", header: "Side", render: (t) => <SideTag side={String(t.side)} /> },
                { key: "n", header: "Qty", align: "right", sortValue: (t) => t.count, render: (t) => <span className="num">{fmtInt(t.count)}</span> },
                { key: "px", header: "Price", align: "right", sortValue: (t) => t.price, render: (t) => <Cents value={t.price} /> },
                { key: "fee", header: "Fee", align: "right", sortValue: (t) => t.fee, render: (t) => <Usd value={t.fee} /> },
                { key: "res", header: "Result", render: (t) => <ResultTag result={t.result} side={String(t.side)} /> },
                { key: "pnl", header: "P&L", align: "right", sortValue: (t) => t.pnl, render: (t) => <Pnl value={t.pnl} /> },
                { key: "why", header: "Reason", minWidth: 180, render: (t) => <ClampText text={t.reason ?? ""} className="muted" /> },
              ]}
            />
          </Card>
        </>
      )}
    </>
  );
}

export function BacktestPage() {
  const { id = "" } = useParams();
  return (
    <div className="page">
      <BacktestDetailView key={id} id={id} />
    </div>
  );
}
