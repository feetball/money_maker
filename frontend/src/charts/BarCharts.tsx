import { useState, type ReactNode } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type LabelProps,
  type TooltipProps,
} from "recharts";
import { fmtPnl, fmtUsdTick } from "../lib/format";
import { ChartFrame, TooltipCard, type LegendItem, type TooltipRow } from "./ChartFrame";
import { niceTicks, tickDomain, useChartColors } from "./palette";

interface ShapeProps {
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  fill?: string;
}

/** Estimated rendered width of an 11px tabular label (px); errs on the wide side. */
const labelPx = (s: string) => Math.ceil(s.length * 6.6);
const LABEL_GAP = 6;

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
/** A row value, or null when it is missing ("not reported" is not $0). */
const valueOf = (row: SignedBarRow, key: string): number | null => {
  const v = row[key];
  return isNum(v) ? v : null;
};

/**
 * Bar with a 4px rounded data end and a square end at the baseline, for either
 * orientation and either sign (recharts may hand us negative width/height).
 */
function roundedEndPath(p: ShapeProps, orientation: "h" | "v", positive: boolean): string {
  const x0 = Math.min(p.x ?? 0, (p.x ?? 0) + (p.width ?? 0));
  const y0 = Math.min(p.y ?? 0, (p.y ?? 0) + (p.height ?? 0));
  const w = Math.abs(p.width ?? 0);
  const h = Math.abs(p.height ?? 0);
  if (w < 0.5 || h < 0.5) return "";
  const r = Math.min(4, orientation === "h" ? w : h, orientation === "h" ? h / 2 : w / 2);
  const x1 = x0 + w;
  const y1 = y0 + h;
  if (orientation === "h") {
    return positive
      ? `M${x0},${y0}H${x1 - r}A${r},${r} 0 0 1 ${x1},${y0 + r}V${y1 - r}A${r},${r} 0 0 1 ${x1 - r},${y1}H${x0}Z`
      : `M${x1},${y0}H${x0 + r}A${r},${r} 0 0 0 ${x0},${y0 + r}V${y1 - r}A${r},${r} 0 0 0 ${x0 + r},${y1}H${x1}Z`;
  }
  return positive
    ? `M${x0},${y1}V${y0 + r}A${r},${r} 0 0 1 ${x0 + r},${y0}H${x1 - r}A${r},${r} 0 0 1 ${x1},${y0 + r}V${y1}Z`
    : `M${x0},${y0}V${y1 - r}A${r},${r} 0 0 0 ${x0 + r},${y1}H${x1 - r}A${r},${r} 0 0 0 ${x1},${y1 - r}V${y0}Z`;
}

function makeShape(orientation: "h" | "v", dataKey: string) {
  return (raw: unknown) => {
    const p = raw as ShapeProps & { payload?: Record<string, unknown> };
    const v = p.payload?.[dataKey];
    const positive = typeof v === "number" ? v >= 0 : true;
    return <path d={roundedEndPath(p, orientation, positive)} fill={p.fill} className="bar-mark" />;
  };
}

const labelValue = (props: LabelProps): number | null => {
  const raw = props.value;
  if (raw === null || raw === undefined || raw === "") return null;
  const v = Number(raw);
  return Number.isFinite(v) ? v : null;
};

/**
 * Signed value label just beyond a horizontal bar's end (text ink, never the series
 * colour). When the label would leave the plot (a small negative bar next to the
 * category axis, or a long positive bar at the right edge) it is drawn on the other
 * side of the zero line instead, which is empty in that bar's band.
 */
function hLabel(fill: string, format: (v: number) => string, plot: { left: number; right: number } | null) {
  return (props: LabelProps) => {
    const v = labelValue(props);
    if (v === null) return null;
    const x = Number(props.x ?? 0);
    const y = Number(props.y ?? 0);
    const w = Number(props.width ?? 0);
    const h = Number(props.height ?? 0);
    const text = format(v);
    const lw = labelPx(text);
    const lo = Math.min(x, x + w);
    const hi = Math.max(x, x + w);
    let tx: number;
    let anchor: "start" | "end";
    if (v >= 0) {
      const fits = !plot || hi + LABEL_GAP + lw <= plot.right;
      tx = fits ? hi + LABEL_GAP : lo - LABEL_GAP;
      anchor = fits ? "start" : "end";
    } else {
      const fits = !plot || lo - LABEL_GAP - lw >= plot.left;
      tx = fits ? lo - LABEL_GAP : hi + LABEL_GAP;
      anchor = fits ? "end" : "start";
    }
    return (
      <text x={tx} y={y + h / 2} dy="0.35em" textAnchor={anchor} fill={fill} fontSize={11} className="num">
        {text}
      </text>
    );
  };
}

/** Cap label above a positive column / below a negative one, for the indices in `show`. */
function vLabel(fill: string, format: (v: number) => string, show: (index: number) => boolean) {
  return (props: LabelProps) => {
    const v = labelValue(props);
    if (v === null || !show(Number(props.index ?? -1))) return null;
    const x = Number(props.x ?? 0);
    const y = Number(props.y ?? 0);
    const w = Number(props.width ?? 0);
    const h = Number(props.height ?? 0);
    const top = Math.min(y, y + h);
    const bottom = Math.max(y, y + h);
    return (
      <text x={x + w / 2} y={v >= 0 ? top - 6 : bottom + 13} textAnchor="middle" fill={fill} fontSize={11} className="num">
        {format(v)}
      </text>
    );
  };
}

/** Proportional headroom (used until the plot has been measured). */
function signedDomain(values: number[], headroom = 1.45): [number, number] {
  let lo = 0;
  let hi = 0;
  for (const v of values) {
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  const span = Math.max(hi - lo, 1);
  return [lo < 0 ? lo * headroom - span * 0.02 : 0, hi > 0 ? hi * headroom + span * 0.02 : 0];
}

/**
 * Domain that reserves room IN PIXELS for the tip labels on each side of zero, so a
 * small negative bar's label never runs into the category axis however lopsided the
 * values are. Falls back to proportional headroom while unmeasured or when the plot
 * is too narrow (the labels then flip sides, see hLabel).
 */
function labelledDomain(values: number[], format: (v: number) => string, plotPx: number): [number, number] {
  if (plotPx <= 40 || values.length === 0) return signedDomain(values);
  let lo = 0;
  let hi = 0;
  let negW = 0;
  let posW = 0;
  for (const v of values) {
    if (v < lo) lo = v;
    if (v > hi) hi = v;
    const w = labelPx(format(v)) + LABEL_GAP + 4;
    if (v < 0) negW = Math.max(negW, w);
    else posW = Math.max(posW, w);
  }
  const fn = negW / plotPx;
  const fp = posW / plotPx;
  if (fn + fp >= 0.75) return signedDomain(values);
  const base = hi - lo > 0 ? hi - lo : 1;
  const span = base / (1 - fn - fp);
  return [lo < 0 ? lo - fn * span : 0, hi + fp * span];
}

export interface SignedBarRow {
  name: string;
  [k: string]: number | string | null;
}

export interface BarSeries {
  key: string;
  label: string;
}

const H_MARGIN = { top: 4, right: 12, bottom: 2, left: 4 };
const H_AXIS_W = 118;

/**
 * Horizontal bars per category. With one series the bars use the diverging poles by
 * sign (blue ≥ 0, red < 0); with two series they use categorical slots 1–2 plus a
 * legend. Values are always printed signed at the bar tip. A null value is "not
 * reported": no bar, no label, and the tooltip says so.
 */
export function SignedBarChart({
  rows,
  series,
  label,
  table,
  extraTooltip,
  format = fmtPnl,
}: {
  rows: SignedBarRow[];
  series: BarSeries[];
  label: string;
  table: ReactNode;
  extraTooltip?: (row: SignedBarRow) => TooltipRow[];
  format?: (v: number) => string;
}) {
  const c = useChartColors();
  const [width, setWidth] = useState(0);
  const two = series.length > 1;
  const colorFor = (i: number) => (i === 0 ? c.series1 : c.series2);
  const values = rows.flatMap((r) => series.map((s) => valueOf(r, s.key))).filter(isNum);
  const plot = width > 0 ? { left: H_MARGIN.left + H_AXIS_W, right: width - H_MARGIN.right } : null;
  // Label headroom first, then widened to round ticks (0 is always one of them).
  const ticks = niceTicks(...labelledDomain(values, format, plot ? plot.right - plot.left : 0), 4);
  const domain = tickDomain(ticks);
  const perRow = two ? 44 : 32;
  const height = Math.max(120, rows.length * perRow + 36);
  const legend: LegendItem[] | undefined = two ? series.map((s, i) => ({ label: s.label, color: colorFor(i), shape: "rect" })) : undefined;

  const renderTooltip = (p: TooltipProps<number, string>) => {
    if (!p.active || !p.payload?.length) return null;
    const row = p.payload[0]?.payload as SignedBarRow | undefined;
    if (!row) return null;
    const out: TooltipRow[] = series.map((s, i) => {
      const v = valueOf(row, s.key);
      return {
        label: s.label,
        value: v === null ? "not reported" : format(v),
        color: two ? colorFor(i) : (v ?? 0) >= 0 ? c.divPos : c.divNeg,
        shape: "rect",
      };
    });
    return <TooltipCard title={row.name} rows={[...out, ...(extraTooltip?.(row) ?? [])]} />;
  };

  return (
    <ChartFrame legend={legend} table={table} height={height} label={label}>
      <ResponsiveContainer width="100%" height="100%" onResize={(w) => setWidth(Math.round(w))}>
        <BarChart data={rows} layout="vertical" margin={H_MARGIN} barGap={2} barCategoryGap={two ? "24%" : "30%"}>
          <CartesianGrid horizontal={false} stroke={c.grid} strokeWidth={1} />
          <XAxis
            type="number"
            domain={domain}
            ticks={ticks}
            interval={0}
            tickFormatter={(v: number) => fmtUsdTick(v, domain[1] - domain[0])}
            tick={{ fill: c.tick, fontSize: 11 }}
            tickLine={false}
            axisLine={false}
          />
          <YAxis type="category" dataKey="name" width={H_AXIS_W} tick={{ fill: c.ink2, fontSize: 12 }} tickLine={false} axisLine={false} interval={0} />
          <Tooltip content={renderTooltip} cursor={{ fill: c.grid, fillOpacity: 0.5 }} isAnimationActive={false} />
          <ReferenceLine x={0} stroke={c.axis} strokeWidth={1} />
          {series.map((s, i) => (
            <Bar
              key={s.key}
              dataKey={s.key}
              name={s.label}
              barSize={two ? 12 : 16}
              fill={colorFor(i)}
              shape={makeShape("h", s.key)}
              isAnimationActive={false}
            >
              {!two && rows.map((r) => <Cell key={r.name} fill={(valueOf(r, s.key) ?? 0) >= 0 ? c.divPos : c.divNeg} />)}
              <LabelList dataKey={s.key} content={hLabel(c.ink2, format, plot)} />
            </Bar>
          ))}
        </BarChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}

const V_MARGIN = { top: 18, right: 8, bottom: 2, left: 2 };
const V_AXIS_W = 60;

/**
 * Which columns get a cap label, decided from the measured slot width: all of them
 * when the widest label fits its slot; otherwise only the best and worst period (or
 * just the larger of the two when even those would collide). Tooltip + table carry
 * every value.
 */
function capLabels(values: (number | null)[], format: (v: number) => string, plotPx: number): (i: number) => boolean {
  const n = values.length;
  if (plotPx <= 0 || n === 0) return () => false;
  const slot = plotPx / n;
  const widths = values.map((v) => (v === null ? 0 : labelPx(format(v))));
  if (Math.max(...widths) + 4 <= slot) return () => true;
  let iMax = -1;
  let iMin = -1;
  values.forEach((v, i) => {
    if (v === null) return;
    if (iMax < 0 || v > (values[iMax] ?? 0)) iMax = i;
    if (iMin < 0 || v < (values[iMin] ?? 0)) iMin = i;
  });
  if (iMax < 0) return () => false;
  if (iMax === iMin) return (i) => i === iMax;
  const apart = Math.abs(iMax - iMin) * slot >= ((widths[iMax] ?? 0) + (widths[iMin] ?? 0)) / 2 + 4;
  // Opposite signs sit on opposite sides of the zero line and can never collide.
  const opposite = (values[iMax] ?? 0) >= 0 !== (values[iMin] ?? 0) >= 0;
  if (apart || opposite) return (i) => i === iMax || i === iMin;
  const keep = Math.abs(values[iMax] ?? 0) >= Math.abs(values[iMin] ?? 0) ? iMax : iMin;
  return (i) => i === keep;
}

/** Vertical signed columns over ordered periods (e.g. P&L by month). */
export function SignedColumnChart({
  rows,
  valueKey,
  label,
  table,
  height = 220,
  extraTooltip,
  format = fmtPnl,
}: {
  rows: SignedBarRow[];
  valueKey: string;
  label: string;
  table: ReactNode;
  height?: number;
  extraTooltip?: (row: SignedBarRow) => TooltipRow[];
  format?: (v: number) => string;
}) {
  const c = useChartColors();
  const [width, setWidth] = useState(0);
  const vals = rows.map((r) => valueOf(r, valueKey));
  const ticks = niceTicks(...signedDomain(vals.filter(isNum), 1.25), 4);
  const domain = tickDomain(ticks);
  const plotPx = width > 0 ? width - V_MARGIN.left - V_MARGIN.right - V_AXIS_W : 0;
  const show = capLabels(vals, format, plotPx);

  const renderTooltip = (p: TooltipProps<number, string>) => {
    if (!p.active || !p.payload?.length) return null;
    const row = p.payload[0]?.payload as SignedBarRow | undefined;
    if (!row) return null;
    const v = valueOf(row, valueKey);
    return (
      <TooltipCard
        title={row.name}
        rows={[
          { label: "P&L", value: v === null ? "not reported" : format(v), color: (v ?? 0) >= 0 ? c.divPos : c.divNeg, shape: "rect" },
          ...(extraTooltip?.(row) ?? []),
        ]}
      />
    );
  };

  return (
    <ChartFrame table={table} height={height} label={label}>
      <ResponsiveContainer width="100%" height="100%" onResize={(w) => setWidth(Math.round(w))}>
        <BarChart data={rows} margin={V_MARGIN} barCategoryGap="22%">
          <CartesianGrid vertical={false} stroke={c.grid} strokeWidth={1} />
          <XAxis dataKey="name" tick={{ fill: c.tick, fontSize: 11 }} tickLine={false} axisLine={{ stroke: c.axis }} interval="preserveStartEnd" minTickGap={8} />
          <YAxis
            domain={domain}
            ticks={ticks}
            interval={0}
            tickFormatter={(v: number) => fmtUsdTick(v, domain[1] - domain[0])}
            tick={{ fill: c.tick, fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            width={V_AXIS_W}
          />
          <Tooltip content={renderTooltip} cursor={{ fill: c.grid, fillOpacity: 0.5 }} isAnimationActive={false} />
          <ReferenceLine y={0} stroke={c.axis} strokeWidth={1} />
          <Bar dataKey={valueKey} maxBarSize={24} shape={makeShape("v", valueKey)} isAnimationActive={false}>
            {rows.map((r) => (
              <Cell key={r.name} fill={(valueOf(r, valueKey) ?? 0) >= 0 ? c.divPos : c.divNeg} />
            ))}
            <LabelList dataKey={valueKey} content={vLabel(c.ink2, format, show)} />
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}
