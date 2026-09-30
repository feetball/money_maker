import {
  CartesianGrid,
  ErrorBar,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipProps,
} from "recharts";
import type { CalibrationBucket } from "../api/types";
import { DataTable } from "../components/DataTable";
import { fmtFrac, fmtInt, fmtPct } from "../lib/format";
import { ChartFrame, TooltipCard } from "./ChartFrame";
import { useChartColors } from "./palette";

interface CalPoint {
  x: number;
  y: number;
  n: number;
  lo: number;
  hi: number;
  bucket: string;
  err: [number, number];
}

/** Wilson score interval (95 %) for a proportion — shows how much each bucket can be trusted. */
export function wilson(p: number, n: number, z = 1.96): [number, number] {
  if (n <= 0) return [0, 1];
  const denom = 1 + (z * z) / n;
  const centre = (p + (z * z) / (2 * n)) / denom;
  const half = (z * Math.sqrt((p * (1 - p)) / n + (z * z) / (4 * n * n))) / denom;
  return [Math.max(0, centre - half), Math.min(1, centre + half)];
}

const pctTicks = [0, 0.2, 0.4, 0.6, 0.8, 1];

/**
 * Label drawn ALONG the y = x diagonal, just above it near the lower-left end. For a
 * segment ReferenceLine recharts hands the label the segment's bounding box (the whole
 * plot), so the standard positions land in a corner away from the line; this computes
 * the line's on-screen angle from that box instead.
 */
function DiagonalLabel({ viewBox, fill }: { viewBox?: { x?: number; y?: number; width?: number; height?: number }; fill: string }) {
  const x0 = viewBox?.x ?? 0;
  const y0 = viewBox?.y ?? 0;
  const w = viewBox?.width ?? 0;
  const h = viewBox?.height ?? 0;
  if (w <= 0 || h <= 0) return null;
  const t = 0.05;
  const x = x0 + t * w;
  const y = y0 + h - t * h;
  const angle = (-Math.atan2(h, w) * 180) / Math.PI;
  return (
    <text x={x} y={y} dy={-6} transform={`rotate(${angle.toFixed(2)} ${x.toFixed(1)} ${y.toFixed(1)})`} fill={fill} fontSize={11} textAnchor="start">
      perfect calibration
    </text>
  );
}

/**
 * Calibration: mean model fair value per bucket (x) vs realized win rate (y), with
 * 95 % Wilson intervals. Points on the diagonal = well calibrated.
 */
export function CalibrationChart({ buckets }: { buckets: CalibrationBucket[] }) {
  const c = useChartColors();
  const points: CalPoint[] = buckets.map((b) => {
    const [lo, hi] = wilson(b.realized_rate, b.n);
    return {
      x: b.mean_fair_value,
      y: b.realized_rate,
      n: b.n,
      lo,
      hi,
      bucket: String(b.bucket),
      err: [Math.max(0, b.realized_rate - lo), Math.max(0, hi - b.realized_rate)],
    };
  });

  const renderTooltip = (p: TooltipProps<number, string>) => {
    if (!p.active || !p.payload?.length) return null;
    const d = p.payload[0]?.payload as CalPoint | undefined;
    if (!d) return null;
    return (
      <TooltipCard
        title={`Bucket ${d.bucket}`}
        rows={[
          { label: "realized win rate", value: fmtFrac(d.y), color: c.series1, shape: "dot" },
          { label: "mean model fair value", value: fmtFrac(d.x) },
          { label: "95% interval", value: `${fmtFrac(d.lo, 0)}–${fmtFrac(d.hi, 0)}` },
          { label: "settled trades", value: fmtInt(d.n) },
        ]}
      />
    );
  };

  const table = (
    <DataTable
      caption="Calibration buckets"
      rows={points}
      rowKey={(r) => r.bucket}
      maxHeight="none"
      columns={[
        { key: "bucket", header: "Bucket", render: (r) => <span className="mono">{r.bucket}</span> },
        { key: "n", header: "Trades", align: "right", render: (r) => fmtInt(r.n) },
        { key: "x", header: "Mean fair value", align: "right", render: (r) => fmtFrac(r.x) },
        { key: "y", header: "Realized rate", align: "right", render: (r) => fmtFrac(r.y) },
        { key: "gap", header: "Gap (pp)", align: "right", title: "Realized − predicted, percentage points", render: (r) => fmtPct((r.y - r.x) * 100, { sign: true }).replace("%", "") },
        { key: "ci", header: "95% interval", align: "right", render: (r) => `${fmtFrac(r.lo, 0)}–${fmtFrac(r.hi, 0)}` },
      ]}
    />
  );

  return (
    <ChartFrame table={table} height={300} label="Calibration: model fair value versus realized win rate">
      <ResponsiveContainer width="100%" height="100%">
        <ScatterChart margin={{ top: 10, right: 16, bottom: 22, left: 4 }}>
          <CartesianGrid stroke={c.grid} strokeWidth={1} />
          <XAxis
            type="number"
            dataKey="x"
            domain={[0, 1]}
            ticks={pctTicks}
            tickFormatter={(v: number) => fmtFrac(v, 0)}
            tick={{ fill: c.tick, fontSize: 11 }}
            tickLine={false}
            axisLine={{ stroke: c.axis }}
            label={{ value: "Model fair value (bucket mean)", position: "insideBottom", offset: -14, fill: c.tick, fontSize: 11 }}
          />
          <YAxis
            type="number"
            dataKey="y"
            domain={[0, 1]}
            ticks={pctTicks}
            tickFormatter={(v: number) => fmtFrac(v, 0)}
            tick={{ fill: c.tick, fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            width={44}
            label={{ value: "Realized", angle: -90, position: "insideLeft", offset: 10, fill: c.tick, fontSize: 11 }}
          />
          <ReferenceLine
            segment={[
              { x: 0, y: 0 },
              { x: 1, y: 1 },
            ]}
            stroke={c.axis}
            strokeWidth={1}
            ifOverflow="hidden"
            label={(props: { viewBox?: { x?: number; y?: number; width?: number; height?: number } }) => (
              <DiagonalLabel viewBox={props.viewBox} fill={c.tick} />
            )}
          />
          <Tooltip content={renderTooltip} cursor={false} isAnimationActive={false} />
          <Scatter
            data={points}
            fill={c.series1}
            isAnimationActive={false}
            shape={(raw: unknown) => {
              const p = raw as { cx?: number; cy?: number };
              const cx = p.cx ?? 0;
              const cy = p.cy ?? 0;
              return (
                <g>
                  <circle cx={cx} cy={cy} r={12} fill="transparent" />
                  <circle cx={cx} cy={cy} r={5} fill={c.series1} stroke={c.surface} strokeWidth={2} />
                </g>
              );
            }}
          >
            <ErrorBar dataKey="err" direction="y" width={6} stroke={c.series1} strokeWidth={1.5} />
          </Scatter>
        </ScatterChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}
