import { cbApi } from "../../api/coinbase/client";
import type { CbAnalytics, CbAnalyticsStats } from "../../api/coinbase/types";
import { SignedBarChart, type SignedBarRow } from "../../charts/BarCharts";
import { DataTable } from "../../components/DataTable";
import { Icon } from "../../components/Icon";
import { Card, EmptyState, Freshness, KpiTile, PageHeader, PollView } from "../../components/ui";
import { Pnl, StrategyTag, Usd } from "../../components/values";
import { fmtDate, fmtDrawdownPct, fmtInt, fmtNum, fmtPct, fmtPnl, fmtUsd, humanize, pnlTone } from "../../lib/format";
import { barLabel, fmtPp } from "./format";
import { useCbStatus } from "../../api/coinbase/status";
import { CbPage, useCbPolling } from "./shared";

interface Row extends CbAnalyticsStats {
  name: string;
  overall?: boolean;
}

function ReadinessCard({ a }: { a: CbAnalytics }) {
  const r = a.readiness;
  const o = a.overall;
  const btc = a.benchmark.btc_buy_hold_return_pct;
  const diff = o.return_pct !== null && btc !== null ? o.return_pct - btc : null;
  const tier = useCbStatus().status?.fee_tier;
  const summary =
    o.trades === 0
      ? "Not yet — the Coinbase paper account has no trades, so there is no track record to judge. Leave the Coinbase engine running."
      : r.ready
        ? `Yes, by this project's criteria. ${fmtInt(o.trades)} Coinbase paper trades returned ${fmtPct(o.return_pct, { sign: true, dp: 2 })} after fees${
            diff !== null ? ` (${fmtPp(diff)} vs holding BTC)` : ""
          }.`
        : `Not yet. ${fmtInt(o.trades)} Coinbase paper trades returned ${fmtPct(o.return_pct, { sign: true, dp: 2 })} after fees${
            diff !== null ? `, ${fmtPp(diff)} vs simply holding BTC over the same period` : ""
          }. The specific blockers are listed below.`;
  return (
    <section className={`card readiness ${r.ready ? "is-ready" : "not-ready"}`} aria-labelledby="cb-readiness-title">
      <div className="readiness-head">
        <span className="readiness-icon" aria-hidden="true">
          <Icon name={r.ready ? "check" : "alert"} />
        </span>
        <div>
          <div className="readiness-kicker">Coinbase go-live readiness</div>
          <h2 id="cb-readiness-title" className="readiness-verdict">
            {r.ready ? "Meets the go-live criteria" : "Not ready for real money"}
          </h2>
        </div>
      </div>
      <p className="readiness-summary">{summary}</p>
      {r.reasons.length > 0 && (
        <ul className="readiness-reasons">
          {r.reasons.map((x) => (
            <li key={x}>{x}</li>
          ))}
        </ul>
      )}
      <p className="readiness-foot">
        Judged on the Coinbase paper account alone (the Kalshi account has its own verdict).{" "}
        {tier
          ? `At the ${tier.label} fee tier (${fmtPct(tier.taker_rate * 100, { dp: 2 })} per taker trade, ${fmtPct(tier.taker_rate * 200, { dp: 2 })} for a taker round trip), `
          : "With retail spot fees near 1 % per taker trade, "}
        an honest outcome is a strategy that lowers drawdowns rather than one that beats holding BTC. This app is paper-only — a "ready" verdict never enables
        real orders.
      </p>
    </section>
  );
}

function BenchmarkKpis({ a }: { a: CbAnalytics }) {
  const o = a.overall;
  const btc = a.benchmark.btc_buy_hold_return_pct;
  const diff = o.return_pct !== null && btc !== null ? o.return_pct - btc : null;
  const since = a.benchmark.since ? `since ${fmtDate(a.benchmark.since)}` : "same period";
  return (
    <div className="kpi-grid">
      <KpiTile hero label="Coinbase paper return" value={fmtPct(o.return_pct, { sign: true, dp: 2 })} tone={pnlTone(o.return_pct, 0.005)} sub={`after fees · ${since}`} />
      <KpiTile
        label="BTC buy-and-hold"
        value={fmtPct(btc, { sign: true, dp: 2 })}
        tone={pnlTone(btc, 0.005)}
        sub={since}
        title="Return of simply buying BTC at the start of the Coinbase paper account and holding it"
      />
      <KpiTile
        label="vs BTC buy-and-hold"
        value={fmtPp(diff, 2)}
        tone={pnlTone(diff, 0.005)}
        sub={diff === null ? "not enough data" : diff >= 0 ? "ahead of holding BTC" : "behind holding BTC"}
        title="Difference in percentage points (pp) between the paper return and BTC buy-and-hold"
      />
      <KpiTile label="Total P&L" value={fmtPnl(o.total_pnl)} tone={pnlTone(o.total_pnl)} sub={`${fmtInt(o.trades)} trades`} />
      <KpiTile label="Sharpe (ann.)" value={fmtNum(o.sharpe, 2)} sub="from the equity curve" />
      <KpiTile label="Max drawdown" value={fmtDrawdownPct(o.max_drawdown_pct, 2)} sub="peak-to-trough" />
      <KpiTile label="Fees paid" value={fmtUsd(o.fees)} sub={o.total_pnl + o.fees > 0 ? `${fmtPct((o.fees / (o.total_pnl + o.fees)) * 100, { dp: 0 })} of gross P&L` : "USD, every fill"} />
      <KpiTile label="Turnover" value={o.turnover === null ? "—" : `${fmtNum(o.turnover, 1)}×`} sub="traded notional ÷ equity (per year)" />
    </div>
  );
}

function StrategyTable({ a }: { a: CbAnalytics }) {
  const rows: Row[] = [...Object.entries(a.by_strategy).map(([name, s]) => ({ ...s, name })), { ...a.overall, name: "All Coinbase strategies", overall: true }];
  const extraKeys = [...new Set(rows.flatMap((r) => Object.keys(r.extra)))];
  return (
    <DataTable
      caption="Coinbase performance by strategy"
      rows={rows}
      rowKey={(r) => r.name}
      maxHeight="none"
      rowClassName={(r) => (r.overall ? "totals" : undefined)}
      columns={[
        { key: "n", header: "Strategy", render: (r) => (r.overall ? <strong>{r.name}</strong> : <StrategyTag name={r.name} />) },
        { key: "t", header: "Trades", align: "right", sortValue: (r) => r.trades, render: (r) => <span className="num">{fmtInt(r.trades)}</span> },
        { key: "p", header: "P&L", align: "right", sortValue: (r) => r.total_pnl, render: (r) => <Pnl value={r.total_pnl} /> },
        {
          key: "r",
          header: "Return",
          align: "right",
          title: "On the strategy's allocation (overall: on the account)",
          sortValue: (r) => r.return_pct,
          render: (r) => <span className={`num tone-${pnlTone(r.return_pct, 0.005)}`}>{fmtPct(r.return_pct, { sign: true, dp: 2 })}</span>,
        },
        { key: "s", header: "Sharpe", align: "right", sortValue: (r) => r.sharpe, render: (r) => <span className="num">{fmtNum(r.sharpe, 2)}</span> },
        { key: "dd", header: "Max DD", align: "right", sortValue: (r) => r.max_drawdown_pct, render: (r) => <span className="num">{fmtDrawdownPct(r.max_drawdown_pct)}</span> },
        { key: "f", header: "Fees", align: "right", sortValue: (r) => r.fees, render: (r) => <Usd value={r.fees} /> },
        { key: "to", header: "Turnover", align: "right", sortValue: (r) => r.turnover, render: (r) => <span className="num">{r.turnover === null ? "—" : `${fmtNum(r.turnover, 1)}×`}</span> },
        ...extraKeys.map((k) => ({
          key: `x-${k}`,
          header: humanize(k),
          align: "right" as const,
          sortValue: (r: Row) => r.extra[k] ?? null,
          render: (r: Row) => <span className="num">{fmtNum(r.extra[k] ?? null, 2)}</span>,
        })),
      ]}
    />
  );
}

function AnalyticsBody() {
  const poll = useCbPolling((signal) => cbApi.analytics({ signal }), { intervalMs: 30_000, label: "Coinbase analytics", refreshOn: ["fill"] });
  return (
    <>
      <PageHeader
        title="Coinbase analytics"
        subtitle="Is the Coinbase paper account doing better than simply holding BTC, after fees? Per-strategy results and the go-live verdict."
        actions={<Freshness poll={poll} />}
      />
      <PollView<CbAnalytics> poll={poll} loadingLabel="Loading Coinbase analytics…">
        {(a) => (
          <>
            <ReadinessCard a={a} />
            <BenchmarkKpis a={a} />
            <div className="grid grid-2">
              <Card title="P&L by Coinbase strategy" subtitle="After fees">
                {Object.keys(a.by_strategy).length === 0 ? (
                  <EmptyState title="No per-strategy results yet" />
                ) : (
                  <SignedBarChart
                    rows={Object.entries(a.by_strategy).map(([name, s]): SignedBarRow => ({ name: barLabel(name), total: s.total_pnl, fees: s.fees, trades: s.trades }))}
                    series={[{ key: "total", label: "Total P&L" }]}
                    label="Coinbase P&L by strategy"
                    extraTooltip={(r) => [
                      { label: "fees paid", value: fmtUsd(Number(r.fees)) },
                      { label: "trades", value: fmtInt(Number(r.trades)) },
                    ]}
                    table={<StrategyTable a={a} />}
                  />
                )}
              </Card>
              <Card title="Fees vs P&L" subtitle="How much of the gross result the Coinbase fee schedule consumed">
                <FeesTable a={a} />
              </Card>
            </div>
            <Card title="Per-strategy statistics" flush>
              <StrategyTable a={a} />
            </Card>
          </>
        )}
      </PollView>
    </>
  );
}

function FeesTable({ a }: { a: CbAnalytics }) {
  const rows = [...Object.entries(a.by_strategy).map(([name, s]) => ({ name, s })), { name: "All Coinbase strategies", s: a.overall }];
  return (
    <DataTable
      caption="Coinbase fees vs P&L"
      rows={rows}
      rowKey={(r) => r.name}
      maxHeight="none"
      columns={[
        { key: "n", header: "Strategy", render: (r) => <StrategyTag name={r.name} /> },
        { key: "g", header: "Gross (before fees)", align: "right", title: "Net P&L + fees paid", render: (r) => <Pnl value={r.s.total_pnl + r.s.fees} /> },
        { key: "f", header: "Fees", align: "right", render: (r) => <Usd value={r.s.fees} /> },
        { key: "p", header: "Net P&L", align: "right", render: (r) => <Pnl value={r.s.total_pnl} /> },
        {
          key: "share",
          header: "Fees / gross",
          align: "right",
          render: (r) => {
            const g = r.s.total_pnl + r.s.fees;
            return <span className="num">{g > 0 ? fmtPct((r.s.fees / g) * 100, { dp: 0 }) : "—"}</span>;
          },
        },
      ]}
    />
  );
}

export function CoinbaseAnalytics() {
  return (
    <CbPage>
      <AnalyticsBody />
    </CbPage>
  );
}
