/**
 * Overview (`/`): both paper venues side by side (docs/COINBASE_CONTRACT.md §14).
 *
 * Two SEPARATE accounts: each card is one venue's own numbers, labeled with its
 * VenueBadge; the combined figure is explicitly "Sum of two separate paper accounts";
 * the equity chart draws one line per venue (never stacked or summed) in the venue
 * colours, with a legend + end labels naming the venue. A Coinbase venue that is
 * unavailable (disabled, import error, older server) is shown as such and never
 * blocks the Kalshi side.
 */
import { memo, useMemo, type ReactElement, type ReactNode } from "react";
import { Link } from "react-router";
import {
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipProps,
} from "recharts";
import type { OverviewCombined, OverviewResponse, OverviewVenue } from "../api/types";
import { baselineAxisLabel, baselineLow, ticksClearOfBaseline } from "../charts/baseline";
import { ChartFrame, TooltipCard, type LegendItem, type TooltipRow } from "../charts/ChartFrame";
import { fmtTimeTick, niceTicks, tickDomain, timeTicks, useChartColors } from "../charts/palette";
import { DataTable } from "../components/DataTable";
import { OVERVIEW_STATE, overviewEngineState } from "../components/Engine";
import { Icon } from "../components/Icon";
import { Badge, Card, EmptyState, ErrorBlock, Freshness, LoadingBlock, PageHeader, Segmented } from "../components/ui";
import { Pnl, Time, Usd } from "../components/values";
import { VenueBadge, VENUES, type Venue } from "../components/Venue";
import { fmtAbsolute, fmtInt, fmtPct, fmtPnl, fmtRelative, fmtUsd, fmtUsdTick, pnlTone } from "../lib/format";
import { useServerNow, useStoredState } from "../lib/hooks";
import { useOverview } from "../lib/overview";
import { useVenueChartColors } from "../lib/venue";
import { OpenPositionsBoth, RecentActivityBoth, StrategyScoreboard } from "./OverviewTables";

const ORDER: Venue[] = ["kalshi", "coinbase"];

// ---------------------------------------------------------------------------
// Venue cards
// ---------------------------------------------------------------------------

function Stat({ label, children, title }: { label: string; children: ReactNode; title?: string }) {
  return (
    <div className="vc-stat" title={title}>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function EngineStateBadge({ v, error }: { v: OverviewVenue | undefined; error: unknown }) {
  const serverNow = useServerNow();
  const st = overviewEngineState(v, error, serverNow);
  const p = OVERVIEW_STATE[st];
  const name = v ? VENUES[v.venue].name : "";
  return (
    <Badge tone={p.tone} icon={p.icon} title={`${name} engine: ${p.label}`}>
      <span className="sr-only">{name} engine: </span>
      {p.label}
    </Badge>
  );
}

function VenueCard({ venue, v, error }: { venue: Venue; v: OverviewVenue | undefined; error: unknown }) {
  const info = VENUES[venue];
  const serverNow = useServerNow();
  const headingId = `venue-card-${venue}`;
  const errorCurrent = v ? overviewEngineState(v, null, serverNow) === "error" : false;
  return (
    <section className={`card venue-card venue-${venue}`} aria-labelledby={headingId}>
      <header className="vc-head">
        <h2 id={headingId} className="vc-title">
          <VenueBadge venue={venue} long size="md" />
          <span className="sr-only"> paper account</span>
        </h2>
        <EngineStateBadge v={v} error={error} />
      </header>
      {!v ? (
        error ? <ErrorBlock error={error} /> : <LoadingBlock label={`Loading ${info.name}…`} />
      ) : !v.available ? (
        <div className="vc-unavailable" role="status">
          <Icon name="alert" />
          <div>
            <div className="state-title">{info.name} venue unavailable</div>
            <div className="state-hint wrap">{v.unavailable_reason ?? "The server did not report a reason."}</div>
            <div className="state-hint">
              {venue === "coinbase"
                ? "The Kalshi paper account is separate and keeps running."
                : "The Coinbase paper account is separate and keeps running."}
            </div>
          </div>
        </div>
      ) : (
        <>
          <div className="vc-hero">
            <div className="vc-label">{info.name} equity</div>
            <div className="vc-equity num">{fmtUsd(v.equity)}</div>
            <div className="vc-sub">
              started <span className="num">{fmtUsd(v.starting_balance)}</span> · cash <span className="num">{fmtUsd(v.cash)}</span>
            </div>
          </div>
          <dl className="vc-stats">
            <Stat label="Total P&L" title={`${info.name} equity minus its starting balance`}>
              <span className={`num tone-${pnlTone(v.total_pnl)}`}>{fmtPnl(v.total_pnl)}</span>{" "}
              <span className="muted num">({fmtPct(v.total_return_pct, { sign: true, dp: 2 })})</span>
            </Stat>
            <Stat label="Today" title={`${info.name} equity now minus equity at 00:00 UTC`}>
              <Pnl value={v.todays_pnl} />
            </Stat>
            <Stat label="Open positions">
              <span className="num">{fmtInt(v.open_positions)}</span>
            </Stat>
            <Stat label="Fees paid">
              <Usd value={v.fees_paid} />
            </Stat>
          </dl>
          {v.last_error && (
            <p className={errorCurrent ? "vc-error tone-neg" : "vc-error muted"}>
              <Icon name="alert" /> {errorCurrent ? "Engine error" : "Last error"}
              {v.last_error_at ? ` (${fmtRelative(v.last_error_at, serverNow)})` : ""}: <span className="mono wrap">{v.last_error}</span>
            </p>
          )}
        </>
      )}
      <footer className="vc-foot">
        <Link to={info.basePath} className="btn btn-sm vc-open">
          Open {info.name} →
        </Link>
      </footer>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Combined total
// ---------------------------------------------------------------------------

function CombinedTotal({ combined, venues }: { combined: OverviewCombined; venues: Record<Venue, OverviewVenue> }) {
  const included = ORDER.filter((k) => venues[k].available);
  const partial = included.length < ORDER.length;
  return (
    <section className="card combined" aria-labelledby="combined-title">
      <div className="combined-main">
        <h2 id="combined-title" className="vc-label">
          {partial ? "Total (one venue)" : "Combined equity"}
        </h2>
        <div className="combined-value num">{fmtUsd(combined.equity)}</div>
        <div className="combined-note">
          <Icon name="info" /> {partial ? `${VENUES[included[0] ?? "kalshi"].name} only — the other venue is unavailable` : combined.note}
        </div>
      </div>
      <dl className="combined-stats">
        <Stat label="Total P&L">
          <span className={`num tone-${pnlTone(combined.total_pnl)}`}>{fmtPnl(combined.total_pnl)}</span>{" "}
          <span className="muted num">({fmtPct(combined.total_return_pct, { sign: true, dp: 2 })})</span>
        </Stat>
        <Stat label="Started with">
          <Usd value={combined.starting_balance} />
        </Stat>
      </dl>
      <ul className="combined-parts" aria-label="Accounts in this total">
        {included.map((k, i) => (
          <li key={k}>
            {i > 0 && (
              <span className="combined-plus" aria-hidden="true">
                +
              </span>
            )}
            <VenueBadge venue={k} /> <span className="num">{fmtUsd(venues[k].equity)}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Equity by venue (one line per venue; one y-axis)
// ---------------------------------------------------------------------------

type Mode = "usd" | "pct";
type Range = "7d" | "30d" | "all";
const RANGE_MS: Record<Range, number> = { "7d": 7 * 86400_000, "30d": 30 * 86400_000, all: Infinity };

interface Row {
  t: number;
  /** Plotted values (null where the venue has no snapshot at this time). */
  kalshi: number | null;
  coinbase: number | null;
  /** Last known value at this time (tooltip / table), in the plotted unit. */
  kalshi_ff: number | null;
  coinbase_ff: number | null;
}

function buildRows(data: OverviewResponse, mode: Mode, range: Range, now: number): Row[] {
  const toUnit = (venue: Venue, equity: number, first: number) => {
    if (mode === "usd") return equity;
    const base = data.venues[venue].starting_balance || first;
    return base ? (equity / base - 1) * 100 : null;
  };
  const cut = now - RANGE_MS[range];
  const byT = new Map<number, Row>();
  for (const venue of ORDER) {
    const pts = data.equity_series[venue];
    const first = pts[0]?.equity ?? 0;
    for (const p of pts) {
      const t = Date.parse(p.ts);
      if (!Number.isFinite(t) || t < cut) continue;
      let row = byT.get(t);
      if (!row) {
        row = { t, kalshi: null, coinbase: null, kalshi_ff: null, coinbase_ff: null };
        byT.set(t, row);
      }
      row[venue] = toUnit(venue, p.equity, first);
    }
  }
  const rows = [...byT.values()].sort((a, b) => a.t - b.t);
  const last: Record<Venue, number | null> = { kalshi: null, coinbase: null };
  for (const r of rows) {
    for (const venue of ORDER) {
      if (r[venue] !== null) last[venue] = r[venue];
      r[`${venue}_ff`] = last[venue];
    }
  }
  return rows;
}

const EquityByVenue = memo(function EquityByVenue({ data }: { data: OverviewResponse }) {
  const c = useChartColors();
  const vc = useVenueChartColors();
  const [mode, setMode] = useStoredState<Mode>("kalshibot.overview.mode", "usd");
  const [range, setRange] = useStoredState<Range>("kalshibot.overview.range", "30d");
  const serverNow = useServerNow();
  // Recompute on new data / controls only (not on every clock tick).
  const nowBucket = Math.floor(serverNow / 60_000) * 60_000;
  const rows = useMemo(() => buildRows(data, mode, range, nowBucket), [data, mode, range, nowBucket]);
  const present = useMemo(() => ORDER.filter((v) => rows.some((r) => r[v] !== null)), [rows]);
  const height = 280;
  const margin = { top: 12, right: 84, bottom: 2, left: 2 };
  const fmtVal = (v: number | null) => (mode === "usd" ? fmtUsd(v) : fmtPct(v, { sign: true, dp: 2 }));

  const scale = useMemo(() => {
    let tMin = Infinity;
    let tMax = -Infinity;
    let lo = Infinity;
    let hi = -Infinity;
    for (const r of rows) {
      tMin = Math.min(tMin, r.t);
      tMax = Math.max(tMax, r.t);
      for (const v of ORDER) {
        const x = r[v];
        if (x !== null && Number.isFinite(x)) {
          lo = Math.min(lo, x);
          hi = Math.max(hi, x);
        }
      }
    }
    // Include the common baseline (start balance / 0 %) so "above/below start" reads.
    const sb = ORDER.map((v) => data.venues[v].starting_balance).filter((x): x is number => x !== null);
    const baseline = mode === "pct" ? 0 : sb.length && sb.every((x) => x === sb[0]) ? sb[0]! : null;
    if (baseline !== null) {
      lo = Math.min(lo, baseline);
      hi = Math.max(hi, baseline);
    }
    if (!Number.isFinite(tMin)) tMin = tMax = Date.now();
    if (!Number.isFinite(lo)) lo = hi = 0;
    const span = Math.max(hi - lo, mode === "usd" ? 2 : 0.2);
    const pad = span * 0.08;
    const yTicks = niceTicks(lo - pad, hi + pad, 4, mode === "usd" && span >= 10);
    return { tMin, tMax, xTicks: timeTicks(tMin, tMax), yTicks, yDomain: tickDomain(yTicks), span, baseline };
  }, [rows, data.venues, mode]);

  // End labels: the last point of each venue, nudged apart when they would collide.
  const endLabels = useMemo(() => {
    const plotH = height - margin.top - margin.bottom - 30;
    const [d0, d1] = scale.yDomain;
    const out: Partial<Record<Venue, { index: number; dy: number }>> = {};
    const ys: { v: Venue; px: number }[] = [];
    for (const v of present) {
      let idx = -1;
      for (let i = rows.length - 1; i >= 0; i--) {
        if (rows[i]![v] !== null) {
          idx = i;
          break;
        }
      }
      if (idx < 0) continue;
      const val = rows[idx]![v] as number;
      ys.push({ v, px: d1 > d0 ? ((d1 - val) / (d1 - d0)) * plotH : 0 });
      out[v] = { index: idx, dy: 0 };
    }
    if (ys.length === 2) {
      const [a, b] = ys as [{ v: Venue; px: number }, { v: Venue; px: number }];
      const gap = Math.abs(a.px - b.px);
      if (gap < 14) {
        const push = (14 - gap) / 2;
        const upper = a.px <= b.px ? a : b;
        const lower = upper === a ? b : a;
        out[upper.v]!.dy = -push;
        out[lower.v]!.dy = push;
      }
    }
    return out;
  }, [present, rows, scale.yDomain, margin.top, margin.bottom]);

  const legend: LegendItem[] = present.map((v) => {
    const last = rows.length ? rows[rows.length - 1]![`${v}_ff`] : null;
    return { label: VENUES[v].name, color: vc[v], shape: "line", value: fmtVal(last) };
  });

  const renderTooltip = (p: TooltipProps<number, string>) => {
    if (!p.active || !p.payload?.length) return null;
    const row = p.payload[0]?.payload as Row | undefined;
    if (!row) return null;
    const out: TooltipRow[] = present.map((v) => ({
      label: `${VENUES[v].name}${row[v] === null && row[`${v}_ff`] !== null ? " (last snapshot)" : ""}`,
      value: fmtVal(row[`${v}_ff`]),
      color: vc[v],
      shape: "line",
    }));
    return (
      <TooltipCard
        title={fmtAbsolute(new Date(row.t).toISOString(), { seconds: scale.tMax - scale.tMin < 2 * 86400_000 })}
        rows={out}
        note="Separate accounts — not a stacked total"
      />
    );
  };

  const newestFirst = useMemo(() => [...rows].reverse(), [rows]);
  const table = (
    <DataTable
      caption={mode === "usd" ? "Equity by venue" : "Return since start by venue"}
      venue={null}
      rows={newestFirst}
      rowKey={(r) => String(r.t)}
      maxHeight={300}
      columns={[
        { key: "t", header: "Time", render: (r) => <Time value={new Date(r.t).toISOString()} /> },
        ...present.map((v) => ({
          key: v,
          header: (
            <>
              <VenueBadge venue={v} /> {mode === "usd" ? "equity" : "return"}
            </>
          ),
          align: "right" as const,
          render: (r: Row) => <span className="num">{fmtVal(r[`${v}_ff`])}</span>,
        })),
      ]}
    />
  );

  const endLabel =
    (v: Venue) =>
    (props: { index?: number; x?: number | string; y?: number | string }): ReactElement<SVGElement> => {
      const at = endLabels[v];
      const x = Number(props.x);
      const y = Number(props.y);
      // Recharts calls this for every point: only the venue's last point gets a label.
      if (!at || props.index !== at.index || !Number.isFinite(x) || !Number.isFinite(y)) return <g key={`end-${v}-${props.index}`} />;
      return (
        <g key={`end-${v}`}>
          <circle cx={x} cy={y} r={4} fill={vc[v]} stroke={c.surface} strokeWidth={2} />
          <text x={x + 9} y={y + at.dy} dy={4} fill={c.ink2} fontSize={11} fontWeight={600}>
            {VENUES[v].name}
          </text>
        </g>
      );
    };

  const toolbar = (
    <>
      <Segmented<Range>
        label="Equity range"
        value={range}
        onChange={setRange}
        options={[
          { value: "7d", label: "7D" },
          { value: "30d", label: "30D" },
          { value: "all", label: "All" },
        ]}
      />
      <Segmented<Mode>
        label="Show equity in dollars or return since start"
        value={mode}
        onChange={setMode}
        options={[
          { value: "usd", label: "$ equity", title: "Equity in dollars" },
          { value: "pct", label: "% return", title: "Return since each account's own start (compares accounts of different sizes)" },
        ]}
      />
    </>
  );

  return (
    <Card
      title={mode === "usd" ? "Equity by venue" : "Return since start, by venue"}
      subtitle={
        mode === "usd"
          ? "One line per paper account, each in its venue colour — not stacked, not summed"
          : "Each account's return since its own starting balance, so accounts of different sizes compare directly — not stacked, not summed"
      }
      actions={toolbar}
    >
      {rows.length < 2 ? (
        <EmptyState
          title={rows.length === 0 ? "No equity history yet" : "Not enough history yet"}
          hint={
            range !== "all"
              ? "Nothing to draw in this range yet — try All. Each venue's engine records an equity snapshot every minute while it runs."
              : "Each venue's engine records an equity snapshot every minute while it runs; the lines appear after the second one."
          }
        />
      ) : (
        <ChartFrame
          legend={legend}
          table={table}
          height={height}
          label={mode === "usd" ? "Equity over time, one line per venue" : "Return since start over time, one line per venue"}
        >
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={rows} margin={margin}>
              <CartesianGrid vertical={false} stroke={c.grid} strokeWidth={1} />
              <XAxis
                dataKey="t"
                type="number"
                scale="time"
                domain={[scale.tMin, scale.tMax]}
                ticks={scale.xTicks}
                tickFormatter={(t: number) => fmtTimeTick(t, scale.tMax - scale.tMin)}
                tick={{ fill: c.tick, fontSize: 11 }}
                tickLine={false}
                axisLine={{ stroke: c.axis }}
                minTickGap={28}
                allowDataOverflow
              />
              <YAxis
                domain={scale.yDomain}
                ticks={ticksClearOfBaseline(scale.yTicks, scale.baseline, scale.yDomain)}
                interval={0}
                tickFormatter={(v: number) => (mode === "usd" ? fmtUsdTick(v, scale.span) : fmtPct(v, { dp: scale.span < 2 ? 1 : 0 }))}
                tick={{ fill: c.tick, fontSize: 11 }}
                tickLine={false}
                axisLine={false}
                width={68}
              />
              <Tooltip content={renderTooltip} cursor={{ stroke: c.axis, strokeWidth: 1 }} isAnimationActive={false} />
              {scale.baseline !== null && (
                <ReferenceLine
                  y={scale.baseline}
                  stroke={c.axis}
                  strokeWidth={1}
                  ifOverflow="extendDomain"
                  // In the y-axis gutter, not inside the plot: both lines start at this value.
                  label={baselineAxisLabel(
                    mode === "usd" ? fmtUsdTick(scale.baseline, scale.span) : fmtPct(scale.baseline, { dp: scale.span < 2 ? 1 : 0 }),
                    "start",
                    c.tick,
                    {
                      title: mode === "usd" ? `start ${fmtUsd(scale.baseline, { dp: 0 })}` : "start (0%)",
                      captionAbove: baselineLow(scale.baseline, scale.yDomain),
                    },
                  )}
                />
              )}
              {present.map((v) => (
                <Line
                  key={v}
                  dataKey={v}
                  name={VENUES[v].name}
                  type="linear"
                  stroke={vc[v]}
                  strokeWidth={2}
                  strokeLinejoin="round"
                  strokeLinecap="round"
                  dot={false}
                  activeDot={{ r: 4, fill: vc[v], stroke: c.surface, strokeWidth: 2 }}
                  label={endLabel(v)}
                  isAnimationActive={false}
                  connectNulls
                />
              ))}
            </ComposedChart>
          </ResponsiveContainer>
        </ChartFrame>
      )}
    </Card>
  );
});

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function Overview() {
  const poll = useOverview();
  const { data, error } = poll;
  return (
    <div className="page overview">
      <PageHeader
        title="Overview"
        subtitle="Two separate paper accounts. Each venue has its own cash, engine, kill switch and risk limits; nothing moves between them."
        actions={<Freshness poll={poll} />}
      />
      {data?.synthesized && (
        <div className="banner banner-info" role="note">
          <Icon name="info" />
          <div>
            The running server does not serve <code>/api/overview</code>, so only the Kalshi account is shown (assembled from the
            Kalshi endpoints). Restarting or rebooting will not change this: the build it runs does not contain the Coinbase backend
            yet. Once that code is in place, rebuild and redeploy (Docker: <code>./deploy.sh update</code>; the image bakes the code
            in). This page checks again every minute and switches over automatically.
          </div>
        </div>
      )}
      {!!error && data !== undefined && (
        <div className="stale-note" role="status">
          <Icon name="alert" /> Refresh failed — showing the last loaded overview
          {poll.updatedAt ? ` (from ${fmtAbsolute(new Date(poll.updatedAt).toISOString(), { seconds: true })})` : ""}.
        </div>
      )}
      <div className="venue-cards">
        {ORDER.map((v) => (
          <VenueCard key={v} venue={v} v={data?.venues[v]} error={data ? null : error} />
        ))}
      </div>
      {data && <CombinedTotal combined={data.combined} venues={data.venues} />}
      {data ? (
        <EquityByVenue data={data} />
      ) : error ? (
        <Card title="Equity by venue">
          <ErrorBlock error={error} onRetry={poll.refresh} />
        </Card>
      ) : null}
      {/* Each table polls both venues itself (every 10 s, paused while the tab is hidden)
          and handles loading / error / "Coinbase unavailable" per venue. */}
      <StrategyScoreboard />
      <OpenPositionsBoth />
      <RecentActivityBoth />
    </div>
  );
}
