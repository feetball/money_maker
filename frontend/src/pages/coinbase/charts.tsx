/**
 * Coinbase charts (dataviz method; see charts/palette.ts for the shared roles).
 *
 * - The Coinbase line always wears the venue colour (useVenueChartColors().coinbase,
 *   the hex twin of --venue-coinbase) — never P&L blue/red or a status colour.
 * - Benchmarks: BTC buy-and-hold = categorical slot 2 (orange); equal-weight universe =
 *   de-emphasis gray, dashed (secondary encoding). Violet/orange/gray was run through
 *   the dataviz validator in both modes: every adjacent pair passes CVD (≥ 9.8) and
 *   normal-vision (≥ 16.7) separation; the gray is a context role, not a slot.
 * - One y-axis, legend with latest values for ≥ 2 series, crosshair tooltip, and a
 *   table twin via ChartFrame.
 */
import { baselineAxisLabel, baselineLow, ticksClearOfBaseline } from "../../charts/baseline";
import { useMemo, useState, type ReactNode } from "react";
import {
  Area,
  Bar,
  BarChart,
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
import { ChartFrame, TooltipCard, type LegendItem, type TooltipRow } from "../../charts/ChartFrame";
import { fmtTimeTick, niceTicks, tickDomain, timeTicks, useChartColors } from "../../charts/palette";
import { Segmented } from "../../components/ui";
import { fmtAbsolute, fmtPct, fmtUsd, fmtUsdTick } from "../../lib/format";
import { useVenueChartColors } from "../../lib/venue";

export type CbRow = { t: number } & Record<string, number | null>;

export interface CbSeries {
  key: string;
  label: string;
  /** "venue" = Coinbase violet (with an area wash when `area`), "btc" = slot 2, "context" = gray. */
  role: "venue" | "btc" | "context";
  area?: boolean;
  dashed?: boolean;
}

export function useCbSeriesColor(): (role: CbSeries["role"]) => string {
  const c = useChartColors();
  const v = useVenueChartColors();
  return (role) => (role === "venue" ? v.coinbase : role === "btc" ? c.series2 : c.deemph);
}

type Scale = "linear" | "log";

/** 1-2-5 × 10^k ticks inside [lo, hi] (lo > 0) for a log axis. */
function logTicks(lo: number, hi: number): number[] {
  const out: number[] = [];
  const k0 = Math.floor(Math.log10(lo));
  const k1 = Math.ceil(Math.log10(hi));
  for (let k = k0; k <= k1; k++) {
    for (const m of [1, 2, 5]) {
      const v = m * 10 ** k;
      if (v >= lo && v <= hi) out.push(Number(v.toPrecision(6)));
    }
  }
  return out.length >= 2 ? out : [lo, hi];
}

/**
 * Multi-series time chart for Coinbase equity (and backtest benchmarks). `format`
 * formats values in the tooltip/legend (USD by default).
 */
export function CbTimeSeriesChart({
  rows: rawRows,
  series,
  baseline,
  height = 260,
  label,
  table,
  dim,
  extraTooltip,
  format = (v: number | null) => fmtUsd(v),
  allowLog = false,
  toolbar,
  yTickFormat,
}: {
  rows: CbRow[];
  series: CbSeries[];
  baseline?: { value: number; label: string };
  height?: number;
  label: string;
  table: ReactNode;
  dim?: boolean;
  extraTooltip?: (row: CbRow) => TooltipRow[];
  format?: (v: number | null) => string;
  /** Offer a Linear/Log toggle (multi-year crypto curves). */
  allowLog?: boolean;
  toolbar?: ReactNode;
  /** Value-axis tick labels (USD by default). */
  yTickFormat?: (v: number, span: number) => string;
}) {
  const c = useChartColors();
  const colorOf = useCbSeriesColor();
  const [scale, setScale] = useState<Scale>("linear");
  const rows = useMemo(() => rawRows.filter((r) => Number.isFinite(r.t)), [rawRows]);

  const { tMin, tMax, ticks, yTicks, yDomain, ySpan, canLog } = useMemo(() => {
    let tMin = Infinity;
    let tMax = -Infinity;
    let lo = Infinity;
    let hi = -Infinity;
    for (const r of rows) {
      if (r.t < tMin) tMin = r.t;
      if (r.t > tMax) tMax = r.t;
      for (const s of series) {
        const v = r[s.key];
        if (typeof v === "number" && Number.isFinite(v)) {
          if (v < lo) lo = v;
          if (v > hi) hi = v;
        }
      }
    }
    if (baseline && Number.isFinite(baseline.value)) {
      lo = Math.min(lo, baseline.value);
      hi = Math.max(hi, baseline.value);
    }
    if (!Number.isFinite(tMin)) tMin = tMax = Date.now();
    if (!Number.isFinite(lo)) lo = hi = 0;
    const canLog = lo > 0 && hi / lo > 1.5;
    if (allowLog && scale === "log" && canLog) {
      const t = logTicks(lo * 0.95, hi * 1.05);
      return { tMin, tMax, ticks: timeTicks(tMin, tMax), yTicks: t, yDomain: [lo * 0.95, hi * 1.05] as [number, number], ySpan: hi - lo, canLog };
    }
    const span = Math.max(hi - lo, yTickFormat ? 0.5 : 2);
    const pad = span * 0.08;
    // An all-negative series (drawdowns) keeps 0 as the top of the axis.
    // A never-negative series (equity) never gets a negative axis from the padding.
    const yTicks = niceTicks(lo >= 0 ? Math.max(0, lo - pad) : lo - pad, hi <= 0 ? 0 : hi + pad, 4, !yTickFormat && span >= 10);
    return { tMin, tMax, ticks: timeTicks(tMin, tMax), yTicks, yDomain: tickDomain(yTicks), ySpan: span, canLog };
  }, [rows, series, baseline, scale, allowLog, yTickFormat]);

  const logOn = allowLog && scale === "log" && canLog;
  const last = rows[rows.length - 1];
  const legend: LegendItem[] | undefined =
    series.length > 1
      ? series.map((s) => ({ label: s.label, color: colorOf(s.role), shape: "line" as const, value: last ? format(last[s.key] ?? null) : undefined }))
      : undefined;

  const renderTooltip = (p: TooltipProps<number, string>) => {
    if (!p.active || !p.payload?.length) return null;
    const row = p.payload[0]?.payload as CbRow | undefined;
    if (!row) return null;
    const out: TooltipRow[] = series.map((s) => ({ label: s.label, value: format(row[s.key] ?? null), color: colorOf(s.role), shape: "line" }));
    return <TooltipCard title={fmtAbsolute(new Date(row.t).toISOString(), { seconds: tMax - tMin < 2 * 86400_000 })} rows={[...out, ...(extraTooltip?.(row) ?? [])]} />;
  };

  const tools = (
    <>
      {toolbar}
      {allowLog && canLog && (
        <Segmented<Scale>
          label="Value axis scale"
          value={scale}
          onChange={setScale}
          options={[
            { value: "linear", label: "Linear", title: "Dollar changes to scale" },
            { value: "log", label: "Log", title: "Equal percentage moves look equal (compare growth rates)" },
          ]}
        />
      )}
    </>
  );

  return (
    <ChartFrame legend={legend} table={table} height={height} label={label} dim={dim} toolbar={tools}>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={rows} margin={{ top: 10, right: 14, bottom: 2, left: 2 }}>
          <CartesianGrid vertical={false} stroke={c.grid} strokeWidth={1} />
          <XAxis
            dataKey="t"
            type="number"
            scale="time"
            domain={[tMin, tMax]}
            ticks={ticks}
            tickFormatter={(t: number) => fmtTimeTick(t, tMax - tMin)}
            tick={{ fill: c.tick, fontSize: 11 }}
            tickLine={false}
            axisLine={{ stroke: c.axis }}
            minTickGap={28}
            allowDataOverflow
          />
          <YAxis
            scale={logOn ? "log" : "auto"}
            domain={yDomain}
            ticks={baseline ? ticksClearOfBaseline(yTicks, baseline.value, yDomain, logOn) : yTicks}
            interval={0}
            allowDataOverflow={logOn}
            tickFormatter={(v: number) => (yTickFormat ?? fmtUsdTick)(v, logOn ? 100 : ySpan)}
            tick={{ fill: c.tick, fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            width={72}
          />
          <Tooltip content={renderTooltip} cursor={{ stroke: c.axis, strokeWidth: 1 }} isAnimationActive={false} />
          {baseline && (
            <ReferenceLine
              y={baseline.value}
              stroke={c.axis}
              strokeWidth={1}
              ifOverflow="extendDomain"
              label={baselineAxisLabel((yTickFormat ?? fmtUsdTick)(baseline.value, logOn ? 100 : ySpan), "start", c.tick, {
                title: baseline.label,
                captionAbove: baselineLow(baseline.value, yDomain, logOn),
              })}
            />
          )}
          {/* Context and benchmark lines first, the Coinbase line on top. */}
          {[...series]
            .sort((a, b) => (a.role === "venue" ? 1 : 0) - (b.role === "venue" ? 1 : 0))
            .map((s) => {
              const color = colorOf(s.role);
              const common = {
                dataKey: s.key,
                name: s.label,
                type: "linear" as const,
                stroke: color,
                strokeWidth: 2,
                strokeLinejoin: "round" as const,
                strokeLinecap: "round" as const,
                strokeDasharray: s.dashed ? "5 4" : undefined,
                dot: false,
                activeDot: { r: 4, fill: color, stroke: c.surface, strokeWidth: 2 },
                isAnimationActive: false,
                connectNulls: true,
              };
              return s.area && !logOn ? (
                <Area key={s.key} {...common} fill={color} fillOpacity={0.1} baseValue="dataMin" />
              ) : (
                <Line key={s.key} {...common} />
              );
            })}
        </ComposedChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}

// ---------------------------------------------------------------------------
// Grouped signed columns (period returns: strategy vs BTC vs equal-weight)
// ---------------------------------------------------------------------------

export interface GroupedRow {
  name: string;
  [k: string]: number | string | null;
}

/** Column with a 4px rounded data end and a square end at zero, for either sign. */
function columnPath(x: number, y: number, w: number, h: number, positive: boolean): string {
  const x0 = x;
  const y0 = Math.min(y, y + h);
  const hh = Math.abs(h);
  if (w < 0.5 || hh < 0.5) return "";
  const r = Math.min(4, hh, w / 2);
  const x1 = x0 + w;
  const y1 = y0 + hh;
  return positive
    ? `M${x0},${y1}V${y0 + r}A${r},${r} 0 0 1 ${x0 + r},${y0}H${x1 - r}A${r},${r} 0 0 1 ${x1},${y0 + r}V${y1}Z`
    : `M${x0},${y0}V${y1 - r}A${r},${r} 0 0 0 ${x0 + r},${y1}H${x1 - r}A${r},${r} 0 0 0 ${x1},${y1 - r}V${y0}Z`;
}

function columnShape(dataKey: string) {
  return (raw: unknown) => {
    const p = raw as { x?: number; y?: number; width?: number; height?: number; fill?: string; payload?: Record<string, unknown> };
    const v = p.payload?.[dataKey];
    return <path d={columnPath(p.x ?? 0, p.y ?? 0, p.width ?? 0, p.height ?? 0, typeof v === "number" ? v >= 0 : true)} fill={p.fill} className="bar-mark" />;
  };
}

/**
 * Grouped vertical columns per period for up to three series (Coinbase strategy,
 * BTC, equal-weight). Values are percentage points by default. Null = not reported:
 * no column, and the tooltip says so.
 */
export function CbGroupedColumns({
  rows,
  series,
  label,
  table,
  height = 240,
  format = (v: number) => fmtPct(v, { sign: true }),
  tickFormat = (v: number) => fmtPct(v, { dp: Math.abs(v) < 10 && v % 1 !== 0 ? 1 : 0 }),
}: {
  rows: GroupedRow[];
  series: CbSeries[];
  label: string;
  table: ReactNode;
  height?: number;
  format?: (v: number) => string;
  tickFormat?: (v: number) => string;
}) {
  const c = useChartColors();
  const colorOf = useCbSeriesColor();
  const values: number[] = [];
  for (const r of rows) for (const s of series) if (typeof r[s.key] === "number" && Number.isFinite(r[s.key] as number)) values.push(r[s.key] as number);
  let lo = 0;
  let hi = 0;
  for (const v of values) {
    lo = Math.min(lo, v);
    hi = Math.max(hi, v);
  }
  const ticks = niceTicks(lo * 1.08, hi * 1.08 || 1, 4);
  const domain = tickDomain(ticks);
  const legend: LegendItem[] = series.map((s) => ({ label: s.label, color: colorOf(s.role), shape: "rect" }));

  const renderTooltip = (p: TooltipProps<number, string>) => {
    if (!p.active || !p.payload?.length) return null;
    const row = p.payload[0]?.payload as GroupedRow | undefined;
    if (!row) return null;
    return (
      <TooltipCard
        title={row.name}
        rows={series.map((s) => {
          const v = row[s.key];
          return { label: s.label, value: typeof v === "number" && Number.isFinite(v) ? format(v) : "not reported", color: colorOf(s.role), shape: "rect" };
        })}
      />
    );
  };

  return (
    <ChartFrame legend={legend} table={table} height={height} label={label}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} margin={{ top: 12, right: 8, bottom: 2, left: 2 }} barGap={2} barCategoryGap="22%">
          <CartesianGrid vertical={false} stroke={c.grid} strokeWidth={1} />
          <XAxis dataKey="name" tick={{ fill: c.tick, fontSize: 11 }} tickLine={false} axisLine={{ stroke: c.axis }} interval="preserveStartEnd" minTickGap={8} />
          <YAxis domain={domain} ticks={ticks} interval={0} tickFormatter={tickFormat} tick={{ fill: c.tick, fontSize: 11 }} tickLine={false} axisLine={false} width={56} />
          <Tooltip content={renderTooltip} cursor={{ fill: c.grid, fillOpacity: 0.5 }} isAnimationActive={false} />
          <ReferenceLine y={0} stroke={c.axis} strokeWidth={1} />
          {series.map((s) => (
            <Bar key={s.key} dataKey={s.key} name={s.label} fill={colorOf(s.role)} maxBarSize={22} shape={columnShape(s.key)} isAnimationActive={false} />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}
