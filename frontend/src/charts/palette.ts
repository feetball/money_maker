/**
 * Chart colour roles (dataviz reference palette; validated with
 * validate_palette.js — slots 1–2 and the blue↔red diverging poles pass every check
 * in both modes against these surfaces). Chart text always uses ink tokens, never
 * series colours.
 */
import { useTheme } from "../lib/theme";

export interface ChartColors {
  surface: string;
  grid: string;
  axis: string;
  tick: string;
  ink: string;
  ink2: string;
  /** Categorical slot 1 (blue) / slot 2 (orange). */
  series1: string;
  series2: string;
  /** De-emphasis gray for context series. */
  deemph: string;
  /**
   * Diverging poles for signed values (P&L): blue = above zero, red = below. P&L text
   * uses the same polarity (--pos / --neg in tokens.css), so a gain is blue in every
   * widget; green stays reserved for status (good / running / filled).
   */
  divPos: string;
  divNeg: string;
}

export const CHART_COLORS: Record<"dark" | "light", ChartColors> = {
  dark: {
    surface: "#1a1a19",
    grid: "#2c2c2a",
    axis: "#383835",
    tick: "#a3a29b",
    ink: "#ededea",
    ink2: "#c3c2b7",
    series1: "#3987e5",
    series2: "#d95926",
    deemph: "#898781",
    divPos: "#3987e5",
    divNeg: "#e66767",
  },
  light: {
    surface: "#fcfcfb",
    grid: "#e1e0d9",
    axis: "#c3c2b7",
    tick: "#6b6a65",
    ink: "#0b0b0b",
    ink2: "#52514e",
    series1: "#2a78d6",
    series2: "#eb6834",
    deemph: "#898781",
    divPos: "#2a78d6",
    divNeg: "#e34948",
  },
};

export function useChartColors(): ChartColors {
  return CHART_COLORS[useTheme().resolved];
}

// ---------------------------------------------------------------------------
// Value-axis ticks
// ---------------------------------------------------------------------------

/**
 * Round step (1, 2 or 5 × 10^k) giving about `count` intervals over `span`. With
 * `integer`, never below 1 (whole-dollar axes).
 */
export function niceStep(span: number, count = 4, integer = false): number {
  if (!(span > 0) || !Number.isFinite(span)) return 1;
  const raw = span / Math.max(1, count);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const f = raw / mag;
  // Nearest nice factor on a log scale (d3's thresholds √2, √10, √50), so the tick
  // count stays close to `count` instead of always rounding the step up.
  const step = (f >= 7.071 ? 10 : f >= 3.162 ? 5 : f >= 1.414 ? 2 : 1) * mag;
  return integer ? Math.max(1, step) : step;
}

/**
 * Evenly spaced, round ticks covering [lo, hi]. Every tick is a multiple of one nice
 * step, so 0 is a tick whenever lo ≤ 0 ≤ hi and the first/last intervals are never
 * uneven. Charts pass these as `ticks` AND use [first, last] as the axis domain:
 * recharts' own fixed-domain ticks start at the raw minimum ($957, $972, …).
 */
export function niceTicks(lo: number, hi: number, count = 4, integer = false): number[] {
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return [0, 1];
  let a = Math.min(lo, hi);
  let b = Math.max(lo, hi);
  if (b - a < 1e-12) {
    const d = Math.abs(a) * 0.1 || 1;
    a -= d;
    b += d;
  }
  const step = niceStep(b - a, count, integer);
  const start = Math.floor(a / step + 1e-9);
  const end = Math.ceil(b / step - 1e-9);
  const out: number[] = [];
  for (let i = start; i <= end && out.length < 60; i++) out.push(Number((i * step).toPrecision(12)));
  return out.length >= 2 ? out : [a, b];
}

/** Axis domain matching niceTicks (first and last tick). */
export const tickDomain = (ticks: number[]): [number, number] => [ticks[0] ?? 0, ticks[ticks.length - 1] ?? 1];

// ---------------------------------------------------------------------------
// Time-axis ticks
// ---------------------------------------------------------------------------

type TickUnit = "minute" | "hour" | "day" | "month";

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;
const TICK_STEPS: [TickUnit, number][] = [
  ["minute", 5],
  ["minute", 15],
  ["minute", 30],
  ["hour", 1],
  ["hour", 2],
  ["hour", 3],
  ["hour", 6],
  ["hour", 12],
  ["day", 1],
  ["day", 2],
  ["day", 7],
  ["day", 14],
  ["month", 1],
  ["month", 2],
  ["month", 3],
  ["month", 6],
  ["month", 12],
];
const approxMs = ([u, n]: [TickUnit, number]) =>
  n * (u === "minute" ? MINUTE_MS : u === "hour" ? HOUR_MS : u === "day" ? DAY_MS : 30.44 * DAY_MS);

function advance(d: Date, unit: TickUnit, n: number) {
  if (unit === "minute") d.setMinutes(d.getMinutes() + n);
  else if (unit === "hour") d.setHours(d.getHours() + n);
  else if (unit === "day") d.setDate(d.getDate() + n);
  else d.setMonth(d.getMonth() + n);
}

/**
 * ~5–7 "nice" time ticks between min and max (ms), on round LOCAL boundaries built
 * with calendar arithmetic (setHours / setDate / setMonth), so day ticks stay on
 * midnight across DST changes and hour ticks land on :00 in half-hour time zones.
 */
export function timeTicks(min: number, max: number, target = 6): number[] {
  const span = max - min;
  if (!Number.isFinite(span)) return [];
  if (!(span > 0)) return [min];
  const step = TICK_STEPS.find((s) => span / approxMs(s) <= target) ?? (["month", 24] as [TickUnit, number]);
  const [unit, n] = step;
  const d = new Date(min);
  if (unit === "minute") {
    d.setSeconds(0, 0);
    while (d.getTime() < min || d.getMinutes() % n !== 0) d.setMinutes(d.getMinutes() + 1);
  } else if (unit === "hour") {
    d.setMinutes(0, 0, 0);
    while (d.getTime() < min || d.getHours() % n !== 0) d.setHours(d.getHours() + 1);
  } else if (unit === "day") {
    d.setHours(0, 0, 0, 0);
    if (d.getTime() < min) d.setDate(d.getDate() + 1);
  } else {
    d.setDate(1);
    d.setHours(0, 0, 0, 0);
    while (d.getTime() < min || d.getMonth() % Math.min(n, 12) !== 0) d.setMonth(d.getMonth() + 1);
  }
  const out: number[] = [];
  for (let i = 0; i < 400 && d.getTime() <= max; i++) {
    out.push(d.getTime());
    advance(d, unit, n);
  }
  return out.length ? out : [min, max];
}

export function fmtTimeTick(t: number, span: number): string {
  const d = new Date(t);
  if (span <= 2 * 24 * 3600_000) return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
  if (span <= 400 * 24 * 3600_000) return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return d.toLocaleDateString(undefined, { month: "short", year: "2-digit" });
}
