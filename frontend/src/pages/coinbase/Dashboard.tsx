import { memo, useMemo } from "react";
import { Link } from "react-router";
import { cbApi } from "../../api/coinbase/client";
import { useCbStatus } from "../../api/coinbase/status";
import { useCbStreamAccount } from "../../api/coinbase/stream";
import type { CbAccount, CbEquityPoint, CbEquityRange, CbPosition, CbStrategy } from "../../api/coinbase/types";
import { SignedBarChart, type SignedBarRow } from "../../charts/BarCharts";
import { DataTable } from "../../components/DataTable";
import { Card, EmptyState, Freshness, KpiTile, PageHeader, PollView, Segmented } from "../../components/ui";
import { Pnl, StrategyTag, Time, Usd } from "../../components/values";
import { fmtDrawdownPct, fmtFrac, fmtInt, fmtPct, fmtPnl, fmtRelative, fmtUsd, parseTs, pnlTone } from "../../lib/format";
import { serverClockOffset, useServerNow, useStoredState } from "../../lib/hooks";
import { CbTimeSeriesChart, type CbRow, type CbSeries } from "./charts";
import { barLabel, fmtRate } from "./format";
import {
  CB_BASE,
  CbActivityFeed,
  CbEngineControls,
  CbEnginePill,
  CbPage,
  cbErrorIsCurrent,
  ProductCell,
  Qty,
  useCbLiveAccount,
  useCbPolling,
} from "./shared";

const RANGES: { value: CbEquityRange; label: string }[] = [
  { value: "1d", label: "1D" },
  { value: "7d", label: "7D" },
  { value: "30d", label: "30D" },
  { value: "all", label: "All" },
];

function Kpis({ a, feeTier }: { a: CbAccount; feeTier: string | null }) {
  return (
    <div className="kpi-grid">
      <KpiTile
        hero
        label="Coinbase equity · liquidation"
        value={fmtUsd(a.equity)}
        title="USD cash + cash reserved by resting buys + what selling every holding into the Coinbase bid ladder would bring now (before exit fees)."
        sub={
          <>
            Mid-marked <span className="num">{fmtUsd(a.equity_mid)}</span> · started <span className="num">{fmtUsd(a.starting_balance)}</span>
          </>
        }
      />
      <KpiTile
        label="Total P&L · liq."
        value={fmtPnl(a.total_pnl)}
        tone={pnlTone(a.total_pnl)}
        title="Liquidation equity minus the starting balance, after all fees."
        sub={<>{fmtPct(a.total_return_pct, { sign: true, dp: 2 })} return</>}
      />
      <KpiTile label="Today's P&L · liq." value={fmtPnl(a.todays_pnl)} tone={pnlTone(a.todays_pnl)} sub="since 00:00 UTC" />
      <KpiTile label="Realized P&L" value={fmtPnl(a.realized_pnl)} tone={pnlTone(a.realized_pnl)} sub="sells: proceeds − fees − avg cost" />
      <KpiTile
        label="Unrealized · liquidation"
        value={fmtPnl(a.unrealized_pnl)}
        tone={pnlTone(a.unrealized_pnl)}
        title="Holdings valued by selling into the bid ladder, minus their cost basis (which includes buy fees)."
        sub={
          <>
            holdings <span className="num">{fmtUsd(a.positions_liquidation_value)}</span> (mid {fmtUsd(a.positions_mid_value)})
          </>
        }
      />
      <KpiTile label="USD cash" value={fmtUsd(a.cash)} sub={<>reserved by resting buys {fmtUsd(a.reserved_cash)}</>} />
      <KpiTile label="Fees paid" value={fmtUsd(a.fees_paid)} sub={feeTier ? `Coinbase tier ${feeTier}` : "Coinbase fee schedule"} />
      <KpiTile label="Max drawdown" value={fmtDrawdownPct(a.max_drawdown_pct, 2)} sub="peak-to-trough, liquidation equity" />
      <KpiTile label="Open positions" value={fmtInt(a.open_positions)} sub={`${fmtInt(a.open_orders)} resting orders`} />
      <KpiTile label="Win rate" value={fmtFrac(a.win_rate)} sub={`${fmtInt(a.trades)} fills`} title="Share of closed round trips (sells) with positive realized P&L" />
    </div>
  );
}

const EQUITY_SERIES: CbSeries[] = [
  { key: "equity", label: "Coinbase equity · liquidation", role: "venue", area: true },
  { key: "equity_mid", label: "Coinbase equity · mid", role: "context" },
];

const EquityCard = memo(function EquityCard({ startingBalance }: { startingBalance: number | undefined }) {
  const stream = useCbStreamAccount();
  const [range, setRange] = useStoredState<CbEquityRange>("kalshibot.cb.equityRange", "7d");
  const equity = useCbPolling((signal) => cbApi.equity(range, { signal }).then((points) => ({ range, points })), {
    intervalMs: 15_000,
    deps: [range],
    label: "Coinbase equity history",
  });

  const rows: CbRow[] = useMemo(() => {
    const pts: CbEquityPoint[] = equity.data?.points ?? [];
    const out: CbRow[] = pts.map((p) => ({
      t: Date.parse(p.ts),
      equity: p.equity,
      equity_mid: p.equity_mid,
      realized: p.realized_pnl,
      unrealized: p.unrealized_pnl,
      cash: p.cash,
    }));
    const last = out[out.length - 1];
    const lp = stream?.data;
    const t = stream ? (parseTs(lp?.ts) ?? stream.receivedAt + serverClockOffset()) : null;
    if (stream && lp?.equity !== undefined && last && t !== null && t - last.t > 1000) {
      out.push({ t, equity: lp.equity, equity_mid: lp.equity_mid ?? null, realized: lp.realized_pnl ?? null, unrealized: lp.unrealized_pnl ?? null, cash: lp.cash ?? null });
    }
    return out;
  }, [equity.data, stream]);

  const baseline = useMemo(() => (startingBalance ? { value: startingBalance, label: `start ${fmtUsd(startingBalance)}` } : undefined), [startingBalance]);
  const newestFirst = useMemo(() => [...rows].reverse(), [rows]);

  return (
    <Card
      title="Coinbase equity curve"
      subtitle="Liquidation value (holdings sold into the bids) vs mid-marked equity"
      actions={<Segmented label="Coinbase equity range" options={RANGES} value={range} onChange={setRange} />}
    >
      <PollView poll={equity} loadingLabel="Loading Coinbase equity history…">
        {() =>
          rows.length < 2 ? (
            <EmptyState title="Not enough history yet" hint="The Coinbase engine records an equity snapshot every minute; the curve appears after the second one." />
          ) : (
            <CbTimeSeriesChart
              rows={rows}
              label="Coinbase equity over time"
              dim={equity.data?.range !== range}
              series={EQUITY_SERIES}
              baseline={baseline}
              extraTooltip={(r) => [
                { label: "realized P&L", value: fmtPnl(r.realized ?? null) },
                { label: "unrealized P&L", value: fmtPnl(r.unrealized ?? null) },
                { label: "USD cash", value: fmtUsd(r.cash ?? null) },
              ]}
              table={
                <DataTable
                  caption="Equity history"
                  rows={newestFirst}
                  rowKey={(r) => String(r.t)}
                  maxHeight={300}
                  columns={[
                    { key: "t", header: "Time", render: (r) => <Time value={new Date(r.t).toISOString()} /> },
                    { key: "equity", header: "Equity (liq.)", align: "right", render: (r) => <Usd value={r.equity} /> },
                    { key: "equity_mid", header: "Equity (mid)", align: "right", render: (r) => <Usd value={r.equity_mid} /> },
                    { key: "realized", header: "Realized", align: "right", render: (r) => <Pnl value={r.realized} /> },
                    { key: "unrealized", header: "Unrealized", align: "right", render: (r) => <Pnl value={r.unrealized} /> },
                    { key: "cash", header: "USD cash", align: "right", render: (r) => <Usd value={r.cash} /> },
                  ]}
                />
              }
            />
          )
        }
      </PollView>
    </Card>
  );
});

const EngineCard = memo(function EngineCard() {
  const { status, error } = useCbStatus();
  const now = useServerNow();
  const e = status?.engine;
  const errorCurrent = !!e && cbErrorIsCurrent(e, now);
  return (
    <Card title="Coinbase engine" subtitle={error && e ? "Last known state — /api/coinbase/status is failing" : "Bar-based paper trading loop"} actions={<CbEnginePill />}>
      <dl className={error ? "kv stale" : "kv"}>
        <dt>State</dt>
        <dd>
          {e ? (e.running ? "Running" : "Stopped") : "—"}
          {e?.kill_switch && <strong className="tone-neg"> · kill switch ON</strong>}
        </dd>
        <dt>Started</dt>
        <dd>
          <Time value={e?.started_at} />
        </dd>
        <dt>Last tick</dt>
        <dd>
          <Time value={e?.last_tick_at} seconds />
        </dd>
        <dt>Last bar</dt>
        <dd title="Close time of the last bar a strategy was evaluated on">
          <Time value={e?.last_bar_at} />
        </dd>
        <dt>Ticks</dt>
        <dd className="num">{fmtInt(e?.tick_count)}</dd>
        <dt>Products</dt>
        <dd className="num">{e ? `${fmtInt(e.products_loaded)} loaded` : "—"}</dd>
        <dt>Coinbase API</dt>
        <dd>{!e ? "—" : e.coinbase_reachable === null ? "not probed yet" : e.coinbase_reachable ? "reachable" : <strong className="tone-neg">unreachable (backing off)</strong>}</dd>
        <dt>Fee tier</dt>
        <dd>
          {status ? (
            <>
              <span className="mono">{status.fee_tier.name}</span> · maker {fmtRate(status.fee_tier.maker_rate)} · taker {fmtRate(status.fee_tier.taker_rate)}
            </>
          ) : (
            "—"
          )}
        </dd>
        <dt>Strategies on</dt>
        <dd>{e ? (e.strategies_enabled.length ? e.strategies_enabled.join(", ") : "none enabled") : "—"}</dd>
        {e?.last_error && (
          <>
            <dt>Last error</dt>
            <dd className={errorCurrent ? "mono tone-neg wrap" : "mono muted wrap"}>
              {e.last_error_at && <span className="nowrap">{fmtRelative(e.last_error_at, now)}: </span>}
              {e.last_error}
            </dd>
          </>
        )}
      </dl>
      <CbEngineControls />
    </Card>
  );
});

const StrategyPnlCard = memo(function StrategyPnlCard() {
  const strategies = useCbPolling((signal) => cbApi.strategies({ signal }), { intervalMs: 15_000, label: "Coinbase strategies", refreshOn: ["fill"] });
  return (
    <Card title="P&L by Coinbase strategy" subtitle="Realized + unrealized (liquidation), after fees">
      <PollView<CbStrategy[]> poll={strategies} isEmpty={(d) => d.length === 0} empty={<EmptyState title="No Coinbase strategies registered" />}>
        {(list) => {
          const rows: SignedBarRow[] = list.map((s) => ({
            name: barLabel(s.name),
            full: s.name,
            total: s.stats.realized_pnl + s.stats.unrealized_pnl,
            realized: s.stats.realized_pnl,
            unrealized: s.stats.unrealized_pnl,
            fees: s.stats.fees,
            exposure: s.stats.exposure,
          }));
          return (
            <SignedBarChart
              rows={rows}
              series={[{ key: "total", label: "Total P&L" }]}
              label="Total P&L by Coinbase strategy"
              extraTooltip={(r) => [
                { label: "realized", value: fmtPnl(Number(r.realized)) },
                { label: "unrealized", value: fmtPnl(Number(r.unrealized)) },
                { label: "fees paid", value: fmtUsd(Number(r.fees)) },
                { label: "held (liq.)", value: fmtUsd(Number(r.exposure)) },
              ]}
              table={
                <DataTable
                  caption="P&L by strategy"
                  rows={rows}
                  rowKey={(r) => String(r.full)}
                  maxHeight="none"
                  columns={[
                    { key: "name", header: "Strategy", render: (r) => <StrategyTag name={String(r.full)} /> },
                    { key: "realized", header: "Realized", align: "right", render: (r) => <Pnl value={Number(r.realized)} /> },
                    { key: "unrealized", header: "Unrealized", align: "right", render: (r) => <Pnl value={Number(r.unrealized)} /> },
                    { key: "total", header: "Total", align: "right", render: (r) => <Pnl value={Number(r.total)} /> },
                    { key: "fees", header: "Fees", align: "right", render: (r) => <Usd value={Number(r.fees)} /> },
                    { key: "exposure", header: "Held (liq.)", align: "right", render: (r) => <Usd value={Number(r.exposure)} /> },
                  ]}
                />
              }
            />
          );
        }}
      </PollView>
    </Card>
  );
});

const PositionsCard = memo(function PositionsCard() {
  const positions = useCbPolling((signal) => cbApi.positions({ signal }), { intervalMs: 7500, label: "Coinbase positions", refreshOn: ["fill"] });
  return (
    <Card
      title="Coinbase holdings"
      subtitle="Largest unrealized moves first"
      actions={
        <Link to={`${CB_BASE}/positions`} className="btn btn-sm btn-ghost">
          All Coinbase positions →
        </Link>
      }
      flush
    >
      <PollView<CbPosition[]>
        poll={positions}
        isEmpty={(d) => d.length === 0}
        empty={<EmptyState title="No open Coinbase positions" hint="Holdings appear when a strategy's buy fills; the rest of the account is USD cash." />}
      >
        {(list) => (
          <DataTable
            caption="Top Coinbase holdings"
            rows={[...list].sort((a, b) => Math.abs(b.unrealized_pnl) - Math.abs(a.unrealized_pnl)).slice(0, 8)}
            rowKey={(p) => `${p.product_id}-${p.strategy}`}
            maxHeight="none"
            columns={[
              { key: "p", header: "Product", render: (p) => <ProductCell pid={p.product_id} url={p.url} /> },
              { key: "q", header: "Quantity", align: "right", render: (p) => <Qty value={p.quantity} base={p.base_currency} /> },
              { key: "liq", header: "Liq. value", align: "right", render: (p) => <Usd value={p.liquidation_value} /> },
              {
                key: "u",
                header: "Unreal. P&L",
                align: "right",
                render: (p) => (
                  <span className="cb-stack">
                    <Pnl value={p.unrealized_pnl} />
                    <span className={`cb-stack-sub tone-${pnlTone(p.unrealized_pnl_pct, 0.05)}`}>{fmtPct(p.unrealized_pnl_pct, { sign: true })}</span>
                  </span>
                ),
              },
              { key: "s", header: "Strategy", render: (p) => <StrategyTag name={p.strategy} /> },
            ]}
          />
        )}
      </PollView>
    </Card>
  );
});

function DashboardBody() {
  const { poll, data } = useCbLiveAccount();
  const { status } = useCbStatus();
  return (
    <>
      <PageHeader
        title="Coinbase dashboard"
        subtitle="Spot crypto paper account — simulated fills against live public Coinbase order books, fees charged in USD on every fill."
        actions={<Freshness poll={poll} />}
      />
      <PollView poll={{ ...poll, data }} loadingLabel="Loading the Coinbase account…">
        {(a) => <Kpis a={a} feeTier={status?.fee_tier.name ?? null} />}
      </PollView>
      <div className="grid grid-main">
        <EquityCard startingBalance={data?.starting_balance} />
        <EngineCard />
      </div>
      <div className="grid grid-2">
        <StrategyPnlCard />
        <PositionsCard />
      </div>
      <Card title="Coinbase live activity" subtitle="Bars, signals, orders, fills and engine logs (Coinbase SSE + recent log history)">
        <CbActivityFeed />
      </Card>
    </>
  );
}

export function CoinbaseDashboard() {
  return (
    <CbPage>
      <DashboardBody />
    </CbPage>
  );
}
