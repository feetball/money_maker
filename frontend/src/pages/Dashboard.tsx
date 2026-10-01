import { memo, useMemo } from "react";
import { Link } from "react-router";
import { api } from "../api/client";
import { completeAccount } from "../api/normalize";
import type { Account, EquityPoint, EquityRange, Position, Strategy } from "../api/types";
import { SignedBarChart, type SignedBarRow } from "../charts/BarCharts";
import { TimeSeriesChart, type SeriesSpec, type TimeRow } from "../charts/TimeSeriesChart";
import { ActivityFeed } from "../components/ActivityFeed";
import { DataTable } from "../components/DataTable";
import { EngineControls, engineErrorIsCurrent, engineState, engineStateLabel } from "../components/Engine";
import { Card, EmptyState, Freshness, KpiTile, PageHeader, PollView, Segmented } from "../components/ui";
import { Cents, MarketCell, Pnl, SideTag, StrategyTag, Time, Usd } from "../components/values";
import { serverClockOffset, useServerNow, usePolling, useStoredState } from "../lib/hooks";
import { fmtCompact, fmtDrawdownPct, fmtFrac, fmtInt, fmtPct, fmtPnl, fmtRelative, fmtUsd, parseTs, pnlTone } from "../lib/format";
import { useStatus } from "../lib/status";
import { useStreamAccount } from "../lib/stream";

const RANGES: { value: EquityRange; label: string }[] = [
  { value: "1d", label: "1D" },
  { value: "7d", label: "7D" },
  { value: "30d", label: "30D" },
  { value: "all", label: "All" },
];

type LiveAccount = { data: Partial<Account>; receivedAt: number };

/**
 * The polled account with a newer SSE `account` event merged over it FIELD BY FIELD.
 * §12 does not pin the SSE payload, so an event may carry only some fields; missing
 * ones keep their polled value instead of reading as $0.00. Before the first poll an
 * event is used only if it carries the full §12 key set.
 */
export function useLiveAccount() {
  const poll = usePolling((signal) => api.account({ signal }), {
    intervalMs: 5000,
    label: "account",
    refreshOn: ["fill", "settlement"],
  });
  const stream = useStreamAccount();
  // Compare with when the poll was SENT: a response that lands after an SSE event can
  // still describe the server state from before it, and must not revert the tiles.
  const live: LiveAccount | null = stream && (poll.requestedAt === null || stream.receivedAt > poll.requestedAt) ? stream : null;
  const data: Account | undefined = useMemo(() => {
    if (!live) return poll.data;
    if (poll.data) return { ...poll.data, ...live.data };
    return completeAccount(live.data) ?? undefined;
  }, [live, poll.data]);
  return { poll, data, live };
}

function Kpis({ a }: { a: Account }) {
  return (
    <div className="kpi-grid">
      <KpiTile
        hero
        label="Equity · liquidation value"
        value={fmtUsd(a.equity)}
        title="Cash + reserved cash + what selling every open position into the bid ladder would bring now (before exit fees)."
        sub={
          <>
            Mid-marked equity <span className="num">{fmtUsd(a.equity_mid)}</span> · started <span className="num">{fmtUsd(a.starting_balance)}</span>
          </>
        }
      />
      <KpiTile
        label="Total P&L · liq."
        value={fmtPnl(a.total_pnl)}
        tone={pnlTone(a.total_pnl)}
        title="Liquidation equity minus the starting balance (open positions valued by selling into the bids)."
        sub={<>{fmtPct(a.total_return_pct, { sign: true, dp: 2 })} return</>}
      />
      <KpiTile
        label="Today's P&L · liq."
        value={fmtPnl(a.todays_pnl)}
        tone={pnlTone(a.todays_pnl)}
        title="Liquidation equity now minus liquidation equity at 00:00 UTC."
        sub="since 00:00 UTC"
      />
      <KpiTile label="Realized P&L" value={fmtPnl(a.realized_pnl)} tone={pnlTone(a.realized_pnl)} sub={`${fmtInt(a.settled_trades)} settled`} />
      <KpiTile
        label="Unrealized · liquidation"
        value={fmtPnl(a.unrealized_pnl)}
        tone={pnlTone(a.unrealized_pnl)}
        title="Open positions valued by selling into the bid ladder, minus their cost basis and entry fees."
        sub={
          <>
            positions <span className="num">{fmtUsd(a.positions_liquidation_value)}</span> (mid {fmtUsd(a.positions_mid_value)})
          </>
        }
      />
      <KpiTile label="Cash" value={fmtUsd(a.cash)} sub={<>reserved for orders {fmtUsd(a.reserved_cash)}</>} />
      <KpiTile
        label="Reserved profit"
        value={fmtUsd(a.reserved_profit)}
        title="Profit from closed/settled trades kept out of the tradeable pool (not used for new orders or position sizing)."
        sub={<>net worth {fmtUsd(a.net_worth)}</>}
      />
      <KpiTile label="Fees paid" value={fmtUsd(a.fees_paid)} sub="Kalshi fee model" />
      <KpiTile
        label="Max drawdown"
        value={fmtDrawdownPct(a.max_drawdown_pct, 2)}
        sub="peak-to-trough, equity"
      />
      <KpiTile label="Open positions" value={fmtInt(a.open_positions)} sub={`${fmtInt(a.open_orders)} resting orders`} />
      <KpiTile label="Win rate" value={fmtFrac(a.win_rate)} sub={`of ${fmtInt(a.settled_trades)} settled trades`} />
    </div>
  );
}

/** Module constants: stable identities, so TimeSeriesChart's memo is actually reused. */
const EQUITY_SERIES: SeriesSpec[] = [
  { key: "equity", label: "Equity · liquidation", role: "primary" },
  { key: "equity_mid", label: "Equity · mid", role: "context" },
];

/**
 * Re-renders only when its own data changes (memo; the starting balance is its only
 * prop), not on every account poll of the Dashboard.
 */
const EquityCard = memo(function EquityCard({ startingBalance }: { startingBalance: number | undefined }) {
  // Latest SSE account event (NOT the poll-gated `live`, which vanishes whenever an
  // account poll lands and made the extra point flicker).
  const stream = useStreamAccount();
  const [range, setRange] = useStoredState<EquityRange>("kalshibot.equityRange", "7d");
  const equity = usePolling((signal) => api.equity(range, { signal }).then((points) => ({ range, points })), {
    intervalMs: 10_000,
    deps: [range],
    label: "equity history",
  });

  const rows: TimeRow[] = useMemo(() => {
    const pts: EquityPoint[] = equity.data?.points ?? [];
    const out: TimeRow[] = pts.map((p) => ({
      t: Date.parse(p.ts),
      equity: p.equity,
      equity_mid: p.equity_mid,
      realized: p.realized_pnl,
      unrealized: p.unrealized_pnl,
      cash: p.cash,
    }));
    // Extend the curve with the latest SSE account event, but only when that event
    // actually carried `equity` (a partial event must never plot a drop to $0). It is
    // placed at the SERVER time of the snapshot (its `ts`), or at the receive time on
    // the server's clock, never on the raw browser clock.
    const last = out[out.length - 1];
    const lp = stream?.data;
    const t = stream ? (parseTs(lp?.ts) ?? stream.receivedAt + serverClockOffset()) : null;
    if (stream && lp?.equity !== undefined && last && t !== null && t - last.t > 1000) {
      out.push({
        t,
        equity: lp.equity,
        equity_mid: lp.equity_mid ?? null,
        realized: lp.realized_pnl ?? null,
        unrealized: lp.unrealized_pnl ?? null,
        cash: lp.cash ?? null,
      });
    }
    return out;
  }, [equity.data, stream]);

  const start = startingBalance;
  const baseline = useMemo(() => (start ? { value: start, label: `start ${fmtUsd(start)}` } : undefined), [start]);
  const newestFirst = useMemo(() => [...rows].reverse(), [rows]);
  const table = (
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
        { key: "cash", header: "Cash", align: "right", render: (r) => <Usd value={r.cash} /> },
      ]}
    />
  );

  return (
    <Card
      title="Kalshi equity curve"
      subtitle="Liquidation value (positions sold into the bids) vs mid-marked equity"
      actions={<Segmented label="Equity range" options={RANGES} value={range} onChange={setRange} />}
    >
      <PollView poll={equity} loadingLabel="Loading equity history…">
        {() =>
          rows.length < 2 ? (
            <EmptyState title="Not enough history yet" hint="The engine records an equity snapshot every minute; the curve appears after the second one." />
          ) : (
            <TimeSeriesChart
              rows={rows}
              label="Equity over time"
              dim={equity.data?.range !== range}
              series={EQUITY_SERIES}
              baseline={baseline}
              extraTooltip={(r) => [
                { label: "realized P&L", value: fmtPnl(r.realized ?? null) },
                { label: "unrealized P&L", value: fmtPnl(r.unrealized ?? null) },
              ]}
              table={table}
            />
          )
        }
      </PollView>
    </Card>
  );
});

const EngineCard = memo(function EngineCard() {
  const { status, error } = useStatus();
  const now = useServerNow();
  const e = status?.engine;
  const st = engineState(status, error, now);
  const failing = st === "unreachable" || st === "backend-error";
  const errorCurrent = engineErrorIsCurrent(e, now);
  return (
    <Card title="Kalshi engine" subtitle={failing && e ? "Last known state — /api/status is failing" : "Kalshi paper trading loop"}>
      <dl className={failing ? "kv stale" : "kv"}>
        <dt>State</dt>
        <dd>
          {failing ? (
            <>
              <strong className="tone-neg">{engineStateLabel(st)}</strong>
              {e && <span className="muted"> · last known: {e.running ? "running" : "stopped"}</span>}
            </>
          ) : e ? (
            e.running ? "Running" : "Stopped"
          ) : (
            "—"
          )}
          {e?.kill_switch && <strong className="tone-neg"> · kill switch ON{failing ? " (last known)" : ""}</strong>}
        </dd>
        <dt>Started</dt>
        <dd>
          <Time value={e?.started_at} />
        </dd>
        <dt>Last tick</dt>
        <dd>
          <Time value={e?.last_tick_at} seconds />
        </dd>
        <dt>Ticks</dt>
        <dd className="num">{fmtInt(e?.tick_count)}</dd>
        <dt>Universe</dt>
        <dd className="num">{e ? `${fmtCompact(e.universe_size)} markets` : "—"}</dd>
        <dt>Exchange</dt>
        <dd>{status ? (status.exchange.trading_active === null ? "unknown" : status.exchange.trading_active ? "trading active" : "trading paused") : "—"}</dd>
        {e?.last_error && (
          <>
            <dt>Last error</dt>
            <dd className={errorCurrent ? "mono tone-neg wrap" : "mono muted wrap"}>
              {e.last_error_at && <span className="nowrap">{fmtRelative(e.last_error_at, now)}: </span>}
              {e.last_error}
              {!errorCurrent && <span className="sr-only"> (history; the engine has run normally since)</span>}
            </dd>
          </>
        )}
      </dl>
      <EngineControls />
    </Card>
  );
});

const StrategyPnlCard = memo(function StrategyPnlCard() {
  const strategies = usePolling((signal) => api.strategies({ signal }), { intervalMs: 10_000, label: "strategies", refreshOn: ["settlement"] });
  return (
    <Card title="P&L by strategy" subtitle="Realized + unrealized (liquidation), after fees">
      <PollView<Strategy[]>
        poll={strategies}
        isEmpty={(d) => d.length === 0}
        empty={<EmptyState title="No strategies registered" />}
      >
        {(list) => {
          const rows: SignedBarRow[] = list.map((s) => ({
            name: s.name,
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
              label="Total P&L by strategy"
              extraTooltip={(r) => [
                { label: "realized", value: fmtPnl(Number(r.realized)) },
                { label: "unrealized", value: fmtPnl(Number(r.unrealized)) },
                { label: "fees paid", value: fmtUsd(Number(r.fees)) },
                { label: "exposure", value: fmtUsd(Number(r.exposure)) },
              ]}
              table={
                <DataTable
                  caption="P&L by strategy"
                  rows={rows}
                  rowKey={(r) => r.name}
                  maxHeight="none"
                  columns={[
                    { key: "name", header: "Strategy", render: (r) => <StrategyTag name={r.name} /> },
                    { key: "realized", header: "Realized", align: "right", render: (r) => <Pnl value={Number(r.realized)} /> },
                    { key: "unrealized", header: "Unrealized", align: "right", render: (r) => <Pnl value={Number(r.unrealized)} /> },
                    { key: "total", header: "Total", align: "right", render: (r) => <Pnl value={Number(r.total)} /> },
                    { key: "fees", header: "Fees", align: "right", render: (r) => <Usd value={Number(r.fees)} /> },
                    { key: "exposure", header: "Exposure", align: "right", title: "Capital currently at risk", render: (r) => <Usd value={Number(r.exposure)} /> },
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
  const positions = usePolling((signal) => api.positions({ signal }), { intervalMs: 7500, label: "positions", refreshOn: ["fill", "settlement"] });
  return (
    <Card
      title="Open positions"
      subtitle="Largest unrealized moves first"
      actions={
        <Link to="/kalshi/positions" className="btn btn-sm btn-ghost">
          All positions →
        </Link>
      }
      flush
    >
      <PollView<Position[]>
        poll={positions}
        isEmpty={(d) => d.length === 0}
        empty={<EmptyState title="No open positions" hint="Positions appear here when a strategy's order fills." />}
      >
        {(list) => (
          <DataTable
            caption="Top open positions"
            rows={[...list].sort((a, b) => Math.abs(b.unrealized_pnl) - Math.abs(a.unrealized_pnl)).slice(0, 8)}
            rowKey={(p) => `${p.ticker}-${p.side}-${p.strategy}`}
            maxHeight="none"
            columns={[
              { key: "m", header: "Market", render: (p) => <MarketCell ticker={p.ticker} title={p.title} url={p.url} eventTicker={p.event_ticker} />, minWidth: 180 },
              { key: "side", header: "Side", render: (p) => <SideTag side={p.side} /> },
              { key: "count", header: "Qty", align: "right", render: (p) => <span className="num">{fmtInt(p.count)}</span> },
              { key: "avg", header: "Avg", align: "right", title: "Average entry price", render: (p) => <Cents value={p.avg_price} /> },
              { key: "mark", header: "Exit", align: "right", title: "Average exit price when selling the whole position into the bids (best bid when the top level covers it)", render: (p) => <Cents value={p.mark_price} /> },
              { key: "u", header: "Unreal. P&L", align: "right", render: (p) => <Pnl value={p.unrealized_pnl} /> },
              { key: "s", header: "Strategy", render: (p) => <StrategyTag name={p.strategy} /> },
            ]}
          />
        )}
      </PollView>
    </Card>
  );
});

const LiveActivityCard = memo(function LiveActivityCard() {
  return (
    <Card title="Live activity" subtitle="Kalshi signals, orders, fills, settlements and engine logs (SSE + recent log history)">
      <ActivityFeed />
    </Card>
  );
});

/**
 * The account poll (every 5 s + every SSE account event) re-renders this root and the
 * KPI tiles only: every other card is memoized and has no props that change with it.
 */
export function Dashboard() {
  const { poll, data } = useLiveAccount();
  return (
    <div className="page">
      <PageHeader title="Dashboard" subtitle="Kalshi paper account — simulated fills against live Kalshi books" actions={<Freshness poll={poll} />} />
      <PollView poll={{ ...poll, data }} loadingLabel="Loading account…">
        {(a) => <Kpis a={a} />}
      </PollView>
      <div className="grid grid-main">
        <EquityCard startingBalance={data?.starting_balance} />
        <EngineCard />
      </div>
      <div className="grid grid-2">
        <StrategyPnlCard />
        <PositionsCard />
      </div>
      <LiveActivityCard />
    </div>
  );
}
