import { useMemo, type ReactNode } from "react";
import {
  Area,
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
import { fmtAbsolute, fmtUsd, fmtUsdTick } from "../lib/format";
import { ChartFrame, TooltipCard, type LegendItem, type TooltipRow } from "./ChartFrame";
import { useVenueChartColors, useVenueScope } from "../lib/venue";
import { baselineAxisLabel, baselineLow, ticksClearOfBaseline } from "./baseline";
import { fmtTimeTick, niceTicks, tickDomain, timeTicks, useChartColors } from "./palette";

export interface SeriesSpec {
  key: string;
  label: string;
  /** "primary" = slot-1 accent with a 10 % area wash; "context" = de-emphasis gray line. */
  role: "primary" | "context";
}

export type TimeRow = { t: number } & Record<string, number | null>;

/**
 * Time-series line/area chart with a crosshair tooltip listing every series at the
 * hovered time, optional zero/start baseline, legend with latest values, and a
 * table twin. One y-axis only.
 */
export function TimeSeriesChart({
  rows: rawRows,
  series,
  baseline,
  height = 260,
  label,
  table,
  dim,
  extraTooltip,
  toolbar,
}: {
  rows: TimeRow[];
  series: SeriesSpec[];
  baseline?: { value: number; label: string };
  height?: number;
  label: string;
  table: ReactNode;
  dim?: boolean;
  extraTooltip?: (row: TimeRow) => TooltipRow[];
  toolbar?: ReactNode;
}) {
  const c = useChartColors();
  // Inside a venue's pages the primary line is that venue's colour (as on the Overview),
  // never P&L blue (contract §14).
  const venue = useVenueScope();
  const vc = useVenueChartColors();
  const primary = venue ? vc[venue] : c.series1;
  const color = (s: SeriesSpec) => (s.role === "primary" ? primary : c.deemph);

  // Points without a usable time are skipped (never plotted at NaN).
  const rows = useMemo(() => rawRows.filter((r) => Number.isFinite(r.t)), [rawRows]);

  const { tMin, tMax, ticks, yTicks, yDomain, ySpan } = useMemo(() => {
    // Loops rather than Math.min(...xs): spreading thousands of points can overflow the stack.
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
    const span = Math.max(hi - lo, 2);
    const pad = span * 0.08;
    // Round, evenly spaced $ ticks (whole dollars unless the range is under $10); the
    // domain is exactly first..last tick, so the start balance / $0 sit on gridlines
    // whenever they are round numbers.
    const yTicks = niceTicks(lo - pad, hi + pad, 4, span >= 10);
    return {
      tMin,
      tMax,
      ticks: timeTicks(tMin, tMax),
      yTicks,
      yDomain: tickDomain(yTicks),
      ySpan: span,
    };
  }, [rows, series, baseline]);

  const last = rows[rows.length - 1];
  const legend: LegendItem[] | undefined =
    series.length > 1
      ? series.map((s) => ({
          label: s.label,
          color: color(s),
          shape: "line" as const,
          value: last ? fmtUsd(last[s.key] ?? null) : undefined,
        }))
      : undefined;

  const renderTooltip = (p: TooltipProps<number, string>) => {
    if (!p.active || !p.payload?.length) return null;
    const row = p.payload[0]?.payload as TimeRow | undefined;
    if (!row) return null;
    const out: TooltipRow[] = series.map((s) => ({ label: s.label, value: fmtUsd(row[s.key] ?? null), color: color(s), shape: "line" }));
    return <TooltipCard title={fmtAbsolute(new Date(row.t).toISOString(), { seconds: tMax - tMin < 2 * 86400_000 })} rows={[...out, ...(extraTooltip?.(row) ?? [])]} />;
  };

  return (
    <ChartFrame legend={legend} table={table} height={height} label={label} dim={dim} toolbar={toolbar}>
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
            domain={yDomain}
            ticks={baseline ? ticksClearOfBaseline(yTicks, baseline.value, yDomain) : yTicks}
            interval={0}
            tickFormatter={(v: number) => fmtUsdTick(v, ySpan)}
            tick={{ fill: c.tick, fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            width={68}
          />
          <Tooltip content={renderTooltip} cursor={{ stroke: c.axis, strokeWidth: 1 }} isAnimationActive={false} />
          {baseline && (
            <ReferenceLine
              y={baseline.value}
              stroke={c.axis}
              strokeWidth={1}
              ifOverflow="extendDomain"
              label={baselineAxisLabel(fmtUsdTick(baseline.value, ySpan), "start", c.tick, {
                title: baseline.label,
                captionAbove: baselineLow(baseline.value, yDomain),
              })}
            />
          )}
          {series
            .filter((s) => s.role === "context")
            .map((s) => (
              <Line
                key={s.key}
                dataKey={s.key}
                name={s.label}
                type="linear"
                stroke={c.deemph}
                strokeWidth={2}
                strokeLinejoin="round"
                strokeLinecap="round"
                dot={false}
                activeDot={{ r: 4, fill: c.deemph, stroke: c.surface, strokeWidth: 2 }}
                isAnimationActive={false}
                connectNulls
              />
            ))}
          {series
            .filter((s) => s.role === "primary")
            .map((s) => (
              <Area
                key={s.key}
                dataKey={s.key}
                name={s.label}
                type="linear"
                stroke={primary}
                strokeWidth={2}
                strokeLinejoin="round"
                strokeLinecap="round"
                fill={primary}
                fillOpacity={0.1}
                baseValue="dataMin"
                dot={false}
                activeDot={{ r: 4, fill: primary, stroke: c.surface, strokeWidth: 2 }}
                isAnimationActive={false}
                connectNulls
              />
            ))}
        </ComposedChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}
