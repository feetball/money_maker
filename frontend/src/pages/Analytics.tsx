import { api } from "../api/client";
import type { AnalyticsParams, AnalyticsResponse, AnalyticsStats, CiBasis, Readiness } from "../api/types";
import { SignedBarChart, type SignedBarRow } from "../charts/BarCharts";
import { CalibrationChart } from "../charts/CalibrationChart";
import { CiBar } from "../charts/CiBar";
import { DataTable } from "../components/DataTable";
import { Icon } from "../components/Icon";
import { Badge, Card, EmptyState, Freshness, KpiTile, PageHeader, PollView } from "../components/ui";
import { Cents, Pnl, StrategyTag, Usd } from "../components/values";
import { usePolling } from "../lib/hooks";
import {
  centsTone,
  fmtCents,
  fmtDrawdownPct,
  fmtDrawdownUsd,
  fmtFrac,
  fmtInt,
  fmtNum,
  fmtPct,
  fmtPnl,
  fmtPnlFine,
  pnlTone,
  type PnlTone,
} from "../lib/format";

interface Row extends AnalyticsStats {
  name: string;
  overall?: boolean;
}

// ---------------------------------------------------------------------------
// Units. The backend's ci_low/ci_high are per CONTRACT (ci_basis "contract", shown in
// ¢); ci_trade_low/high are per TRADE (shown in $) and decide readiness. An older or
// different backend may send a per-trade CI as ci_low (basis "trade"), or a CI whose
// basis cannot be inferred ("unknown": plain dollars, no mean drawn against it).
// ---------------------------------------------------------------------------

/** Unit phrase for a CI on this basis. */
const unitOf = (b: CiBasis) => (b === "trade" ? "per trade" : b === "contract" ? "per contract" : "(basis not reported)");
/** The mean that belongs to the CI's basis (null for "unknown": none is consistent). */
const ciMean = (s: AnalyticsStats) =>
  s.ci_basis === "trade" ? s.mean_pnl_per_trade : s.ci_basis === "contract" ? s.mean_pnl_per_contract : null;
/** A CI bound / mean on the CI's basis: ¢ per contract, otherwise dollars. */
const fmtOnBasis = (b: CiBasis, v: number | null) => (b === "contract" ? fmtCents(v, { sign: true, dp: 2 }) : fmtPnlFine(v));
const toneOnBasis = (b: CiBasis, v: number | null): PnlTone => (b === "contract" ? centsTone(v, 2) : pnlTone(v, 0.00005));
/** Table "Mean" cell: per contract in ¢ unless the CI is per trade. */
const tableMean = (s: AnalyticsStats) => (s.ci_basis === "trade" ? s.mean_pnl_per_trade : s.mean_pnl_per_contract);
const tableMeanBasis = (s: AnalyticsStats): CiBasis => (s.ci_basis === "trade" ? "trade" : "contract");

/** Realized P&L comparable with expected_edge_total (same trades), with a fallback flag. */
function realizedComparable(s: AnalyticsStats): { value: number | null; allTrades: boolean } {
  if (s.realized_pnl_with_edge !== null) return { value: s.realized_pnl_with_edge, allTrades: false };
  if (s.expected_edge_total === null) return { value: null, allTrades: false };
  // Older backend without realized_pnl_with_edge: the only figure is over ALL trades.
  return { value: s.realized_pnl, allTrades: true };
}

/** Edge capture as a fraction: the backend's own when sent (like-for-like), else a fallback. */
function edgeCapture(s: AnalyticsStats): { value: number | null; allTrades: boolean } {
  if (s.edge_capture !== null) return { value: s.edge_capture, allTrades: false };
  const r = realizedComparable(s);
  if (s.expected_edge_total === null || r.value === null || Math.abs(s.expected_edge_total) < 1e-9) return { value: null, allTrades: false };
  return { value: r.value / s.expected_edge_total, allTrades: r.allTrades };
}

// ---------------------------------------------------------------------------
// Readiness
// ---------------------------------------------------------------------------

/**
 * Plain-language verdict. It reasons about the PER-TRADE interval (ci_trade_low/high),
 * the one the backend's verdict and its listed reasons use, so the sentence can never
 * contradict the blockers printed under it.
 */
function plainSummary(r: Readiness, o: AnalyticsStats): string {
  if (o.count === 0) {
    return "Not yet — no paper trades have settled, so there is no track record to judge. Leave the engine running; the verdict updates as markets resolve.";
  }
  const lo = o.ci_trade_low;
  const hi = o.ci_trade_high;
  const m = o.mean_pnl_per_trade;
  const avg = m !== null ? ` (${fmtPnlFine(m)} per trade on average)` : "";
  const ci =
    lo !== null && hi !== null
      ? `we can say with 95% confidence that the average profit per trade is between ${fmtPnlFine(lo)} and ${fmtPnlFine(hi)}`
      : "there is not yet a confidence interval for the average profit per trade (it needs settled trades in at least two events)";
  if (r.ready) {
    if (r.ready_strategies.length > 0) {
      // The verdict is per strategy: the pooled numbers below can still include a losing one.
      return `Yes for ${r.ready_strategies.join(", ")}, by this project's criteria (judged per strategy). Across all ${fmtInt(o.count)} settled paper trades the bot made ${fmtPnl(o.total_pnl)}${avg}. Only the strategies named here passed; the per-strategy table shows the others.`;
    }
    const whole = lo !== null && lo > 0 ? " — the whole range is above zero, so the edge is unlikely to be luck —" : "";
    return `Yes, by this project's criteria. Across ${fmtInt(o.count)} settled paper trades the bot made ${fmtPnl(o.total_pnl)}${avg}, ${ci}${whole} and drawdown stayed within limits.`;
  }
  const luck = lo !== null && lo <= 0 ? " That range still includes zero or a loss, so the result so far could be luck." : "";
  return `Not yet. Across ${fmtInt(o.count)} settled paper trades the bot has made ${fmtPnl(o.total_pnl)}${avg}, and ${ci}.${luck} The specific blockers are listed below.`;
}

function criteriaText(p: AnalyticsParams | null): string {
  const n = p?.min_settled_trades ?? null;
  const dd = p?.max_drawdown_pct ?? null;
  if (n === null && dd === null) {
    return "Criteria (backend defaults): at least 200 settled trades, the lower bound of the 95% bootstrap CI of mean P&L per trade above zero, and max drawdown within limits.";
  }
  const per = Object.entries(p?.min_settled_trades_by_strategy ?? {});
  const trades =
    n !== null
      ? `at least ${fmtInt(n)} settled trades${per.length ? ` (${per.map(([k, v]) => `${k} ${fmtInt(v)}`).join(", ")})` : ""}`
      : "enough settled trades (threshold not reported)";
  const draw = dd !== null ? `max drawdown at most ${fmtPct(dd, { dp: dd % 1 === 0 ? 0 : 1 })}` : "max drawdown within limits";
  return `Criteria (from the backend's settings), judged per strategy: ${trades}, the lower bound of the 95% bootstrap CI of mean P&L per trade above zero, a loss-event rate whose 95% upper bound is below break-even, and ${draw}. The headline is ready when at least one strategy is.`;
}

function ReadinessCard({ a }: { a: AnalyticsResponse }) {
  const r = a.readiness;
  const o = a.overall;
  return (
    <section className={`card readiness ${r.ready ? "is-ready" : "not-ready"}`} aria-labelledby="readiness-title">
      <div className="readiness-head">
        <span className="readiness-icon" aria-hidden="true">
          <Icon name={r.ready ? "check" : "alert"} />
        </span>
        <div>
          <div className="readiness-kicker">Go-live readiness</div>
          <h2 id="readiness-title" className="readiness-verdict">
            {r.ready ? "Meets the go-live criteria" : "Not ready for real money"}
          </h2>
        </div>
      </div>
      <p className="readiness-summary">{plainSummary(r, o)}</p>
      {r.reasons.length > 0 && (
        <ul className="readiness-reasons">
          {r.reasons.map((x) => (
            <li key={x}>{x}</li>
          ))}
        </ul>
      )}
      <p className="readiness-foot">
        {criteriaText(a.params)} This app is paper-only — a "ready" verdict is evidence, not a switch; it never enables real orders.
      </p>
    </section>
  );
}

// ---------------------------------------------------------------------------
// KPIs
// ---------------------------------------------------------------------------

function OverallKpis({ o }: { o: AnalyticsStats }) {
  const perCt = o.mean_pnl_per_contract;
  const perTrade = o.mean_pnl_per_trade;
  const ctCi =
    o.ci_low !== null && o.ci_high !== null
      ? o.ci_basis === "contract"
        ? `95% CI ${fmtOnBasis("contract", o.ci_low)} to ${fmtOnBasis("contract", o.ci_high)}`
        : o.ci_basis === "unknown"
          ? `95% CI ${fmtPnlFine(o.ci_low)} to ${fmtPnlFine(o.ci_high)} (basis not reported)`
          : "CI reported per trade only"
      : "CI needs trades in 2+ events";
  const trCi =
    o.ci_trade_low !== null && o.ci_trade_high !== null
      ? `95% CI ${fmtPnlFine(o.ci_trade_low)} to ${fmtPnlFine(o.ci_trade_high)} · decides readiness`
      : "CI needs trades in 2+ events";
  const cap = edgeCapture(o);
  const real = realizedComparable(o);
  return (
    <div className="kpi-grid">
      <KpiTile label="Settled trades" value={fmtInt(o.count)} sub={o.contracts !== null ? `${fmtInt(o.contracts)} contracts` : undefined} />
      <KpiTile label="Realized P&L" value={fmtPnl(o.total_pnl)} tone={pnlTone(o.total_pnl)} sub="settled trades only" />
      <KpiTile label="Mean P&L / trade" value={fmtPnlFine(perTrade)} tone={pnlTone(perTrade, 0.00005)} sub={trCi} />
      <KpiTile label="Mean P&L / contract" value={fmtCents(perCt, { sign: true, dp: 2 })} tone={centsTone(perCt, 2)} sub={ctCi} />
      <KpiTile label="Win rate" value={fmtFrac(o.win_rate)} />
      <KpiTile
        label="Brier score"
        value={fmtNum(o.brier, 3)}
        sub="lower is better · 0.25 = coin flip"
        title="Mean squared error of fair_value vs outcome, over trades with a model"
      />
      <KpiTile
        label="Edge captured"
        value={cap.value === null ? "—" : fmtPct(cap.value * 100, { dp: 0 })}
        sub={
          o.expected_edge_total === null ? (
            "no settled trade reported an expected edge"
          ) : (
            <>
              expected {fmtPnl(o.expected_edge_total)} · realized {fmtPnl(real.value)}
              {real.allTrades ? " (all trades)" : o.trades_with_edge !== null ? ` on the ${fmtInt(o.trades_with_edge)} trades with an edge` : " (same trades)"}
            </>
          )
        }
        title="Realized P&L of the trades that reported an expected edge, as a share of that expected edge"
      />
      <KpiTile
        label="Max drawdown"
        value={fmtDrawdownUsd(o.max_drawdown)}
        sub={o.max_drawdown_pct !== null ? `${fmtDrawdownPct(o.max_drawdown_pct)} from peak equity` : undefined}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Per-strategy table (per-contract CI)
// ---------------------------------------------------------------------------

/** One shared forest-plot domain per CI basis (¢/contract and $/trade never share an axis). */
function domainsByBasis(rows: Row[]): Map<CiBasis, [number, number]> {
  const out = new Map<CiBasis, [number, number]>();
  const groups = new Map<CiBasis, number[]>();
  for (const r of rows) {
    const vals = [r.ci_low, r.ci_high, ciMean(r)].filter((v): v is number => v !== null);
    groups.set(r.ci_basis, [...(groups.get(r.ci_basis) ?? []), ...vals]);
  }
  for (const [b, vals] of groups) {
    const lo = Math.min(0, ...vals);
    const hi = Math.max(0, ...vals);
    const pad = (hi - lo) * 0.08 || 0.01;
    out.set(b, [lo - pad, hi + pad]);
  }
  return out;
}

function CiTable({ a }: { a: AnalyticsResponse }) {
  const rows: Row[] = [...Object.entries(a.by_strategy).map(([name, s]) => ({ ...s, name })), { ...a.overall, name: "All strategies", overall: true }];
  const domains = domainsByBasis(rows);
  return (
    <DataTable
      caption="Per-strategy performance with 95% confidence intervals of mean P&L per contract"
      rows={rows}
      rowKey={(r) => r.name}
      maxHeight="none"
      rowClassName={(r) => (r.overall ? "totals" : undefined)}
      columns={[
        { key: "name", header: "Strategy", render: (r) => (r.overall ? <strong>{r.name}</strong> : <StrategyTag name={r.name} />) },
        { key: "n", header: "Trades", align: "right", sortValue: (r) => r.count, render: (r) => <span className="num">{fmtInt(r.count)}</span> },
        { key: "wr", header: "Win rate", align: "right", sortValue: (r) => r.win_rate, render: (r) => <span className="num">{fmtFrac(r.win_rate)}</span> },
        { key: "pnl", header: "P&L", align: "right", title: "Realized P&L of all settled / closed trades", sortValue: (r) => r.total_pnl, render: (r) => <Pnl value={r.total_pnl} /> },
        {
          key: "mean",
          header: "Mean / ct",
          align: "right",
          title: "Mean P&L per contract, in ¢ (per trade, in $, when the backend reports only a per-trade interval)",
          sortValue: (r) => tableMean(r),
          render: (r) => {
            const b = tableMeanBasis(r);
            const v = tableMean(r);
            return (
              <span className={`num tone-${toneOnBasis(b, v)}`}>
                {fmtOnBasis(b, v)}
                {b === "trade" && <span className="muted"> /trade</span>}
              </span>
            );
          },
        },
        {
          key: "ci",
          header: "95% CI",
          align: "right",
          title: "Bootstrap 95% confidence interval of the mean P&L per contract (clustered by UTC day)",
          sortValue: (r) => r.ci_low,
          render: (r) =>
            r.ci_low !== null && r.ci_high !== null ? (
              <span className="num nowrap">
                {fmtOnBasis(r.ci_basis, r.ci_low)} … {fmtOnBasis(r.ci_basis, r.ci_high)}
                {r.ci_basis !== "contract" && <span className="muted"> {r.ci_basis === "trade" ? "/trade" : "(basis n/r)"}</span>}
              </span>
            ) : (
              <span className="muted">n/a</span>
            ),
        },
        {
          key: "forest",
          header: "Interval vs 0",
          render: (r) => (
            <CiBar
              lo={r.ci_low}
              hi={r.ci_high}
              mean={ciMean(r)}
              domain={domains.get(r.ci_basis) ?? [-0.01, 0.01]}
              format={(v) => fmtOnBasis(r.ci_basis, v)}
              unit={unitOf(r.ci_basis)}
            />
          ),
        },
        {
          key: "exp",
          header: "Expected",
          align: "right",
          title: "Sum of expected edge at entry (after fees), over the trades that reported one",
          sortValue: (r) => r.expected_edge_total,
          render: (r) => (r.expected_edge_total === null ? <span className="muted">not reported</span> : <Pnl value={r.expected_edge_total} />),
        },
        {
          key: "real",
          header: "Realized (same)",
          align: "right",
          title: "Realized P&L of the same trades that reported an expected edge",
          sortValue: (r) => realizedComparable(r).value,
          render: (r) => {
            const x = realizedComparable(r);
            if (x.value === null) return <span className="muted">—</span>;
            return (
              <span className="nowrap">
                <Pnl value={x.value} />
                {x.allTrades && <span className="muted" title="The backend did not report realized P&L for just the trades with an edge"> (all)</span>}
              </span>
            );
          },
        },
        { key: "brier", header: "Brier", align: "right", sortValue: (r) => r.brier, render: (r) => <span className="num">{fmtNum(r.brier, 3)}</span> },
        {
          key: "dd",
          header: "Max DD",
          align: "right",
          sortValue: (r) => r.max_drawdown,
          render: (r) => (r.max_drawdown !== null ? <span className="num">{fmtDrawdownUsd(r.max_drawdown)}</span> : <span className="muted">—</span>),
        },
        {
          key: "ready",
          header: "Ready",
          render: (r) => {
            const rd = r.overall ? a.readiness : r.readiness;
            if (!rd) return <span className="muted">—</span>;
            return (
              <span title={rd.reasons.join("\n")}>
                {rd.ready ? (
                  <Badge tone="good" icon="check">
                    Ready
                  </Badge>
                ) : (
                  <Badge tone="warn" icon="alert">
                    Not yet
                  </Badge>
                )}
              </span>
            );
          },
        },
      ]}
    />
  );
}

// ---------------------------------------------------------------------------
// Expected vs realized (same trades)
// ---------------------------------------------------------------------------

function ExpectedVsRealized({ a }: { a: AnalyticsResponse }) {
  const rows: SignedBarRow[] = Object.entries(a.by_strategy).map(([name, s]) => {
    const real = realizedComparable(s);
    return {
      name,
      // null = not reported: no bar and "not reported" in the tooltip, never $0.00.
      expected: s.expected_edge_total,
      realized: real.value,
      realized_all: s.realized_pnl,
      all_trades: real.allTrades ? 1 : 0,
      count: s.count,
      with_edge: s.trades_with_edge,
    };
  });
  if (rows.length === 0) return <EmptyState title="No settled trades per strategy yet" />;
  const num = (v: number | string | null | undefined) => (typeof v === "number" && Number.isFinite(v) ? v : null);
  return (
    <SignedBarChart
      rows={rows}
      series={[
        { key: "expected", label: "Expected edge" },
        { key: "realized", label: "Realized (same trades)" },
      ]}
      label="Expected edge versus realized P&L of the same trades, by strategy"
      extraTooltip={(r) => [
        { label: "trades with an edge", value: r.with_edge === null ? "not reported" : fmtInt(num(r.with_edge)) },
        { label: "settled trades (all)", value: fmtInt(num(r.count)) },
        { label: "realized, all trades", value: fmtPnl(num(r.realized_all)) },
        ...(r.all_trades ? [{ label: "note", value: "realized is over all trades" }] : []),
      ]}
      table={
        <DataTable
          caption="Expected versus realized (trades that reported an expected edge)"
          rows={rows}
          rowKey={(r) => r.name}
          maxHeight="none"
          columns={[
            { key: "name", header: "Strategy", render: (r) => <StrategyTag name={r.name} /> },
            { key: "n", header: "With edge", align: "right", title: "Settled trades that reported an expected edge", render: (r) => fmtInt(num(r.with_edge)) },
            {
              key: "e",
              header: "Expected",
              align: "right",
              render: (r) => (num(r.expected) === null ? <span className="muted">not reported</span> : <Pnl value={num(r.expected)} />),
            },
            {
              key: "r",
              header: "Realized",
              align: "right",
              render: (r) => (
                <span className="nowrap">
                  <Pnl value={num(r.realized)} />
                  {r.all_trades ? <span className="muted"> (all trades)</span> : null}
                </span>
              ),
            },
            {
              key: "gap",
              header: "Realized − expected",
              align: "right",
              render: (r) => {
                const e = num(r.expected);
                const x = num(r.realized);
                return e === null || x === null ? <span className="muted">—</span> : <Pnl value={x - e} />;
              },
            },
            { key: "all", header: "Realized, all trades", align: "right", render: (r) => <Pnl value={num(r.realized_all)} /> },
          ]}
        />
      }
    />
  );
}

export function Analytics() {
  const poll = usePolling((signal) => api.analytics({ signal }), { intervalMs: 10_000, label: "analytics", refreshOn: ["settlement"] });
  return (
    <div className="page">
      <PageHeader
        title="Analytics"
        subtitle="Performance over settled trades only — confidence intervals, calibration of model fair values, and expected vs realized edge."
        actions={<Freshness poll={poll} />}
      />
      <PollView<AnalyticsResponse> poll={poll} loadingLabel="Computing analytics…">
        {(a) => (
          <>
            <ReadinessCard a={a} />
            <OverallKpis o={a.overall} />
            <Card
              title="Per-strategy results"
              subtitle="Mean P&L per contract with 95% bootstrap confidence intervals (clustered by UTC day); an interval entirely right of the zero line is a statistically positive edge"
              flush
            >
              {Object.keys(a.by_strategy).length === 0 && a.overall.count === 0 ? <EmptyState title="No settled trades yet" /> : <CiTable a={a} />}
            </Card>
            <div className="grid grid-2">
              <Card title="Calibration" subtitle="Do model fair values match how often those trades actually won? Points on the diagonal are well calibrated; bars are 95% intervals.">
                {a.calibration.length === 0 ? (
                  <EmptyState title="No calibrated trades yet" hint="Calibration needs settled trades whose strategy reported a fair_value." />
                ) : (
                  <CalibrationChart buckets={a.calibration} />
                )}
              </Card>
              <Card title="Expected vs realized" subtitle="Edge the strategies expected at entry (after fees) against what those same trades actually returned">
                <ExpectedVsRealized a={a} />
              </Card>
            </div>
            <p className="footnote">
              Per-contract figures are in cents (<Cents value={0.01} /> = <Usd value={0.01} />); per-trade figures are in dollars. Readiness uses the per-trade
              interval. Expected edge is the sum of each opening intent's expected $/contract × contracts, over the trades that reported one.
            </p>
          </>
        )}
      </PollView>
    </div>
  );
}
