/**
 * Start-balance baseline drawn as a y-axis annotation instead of text inside the plot.
 * Equity lines begin AT the baseline on the left edge (and often hover near it), so a
 * label inside the plot always sits under them. Here the label sits in the y-axis
 * gutter, left of the plot: the value in the tick style plus a small "start" caption,
 * and y-axis ticks too close to it are dropped so the two never overlap.
 */
import type { ReactElement } from "react";

/** Minimum gap between the baseline label and a regular tick, as a fraction of the axis. */
const MIN_GAP = 0.09;

/** `ticks` without the ones that would collide with the baseline's axis label. */
export function ticksClearOfBaseline(
  ticks: number[],
  baseline: number | null | undefined,
  domain: readonly [number, number],
  log = false,
): number[] {
  if (baseline === null || baseline === undefined || !Number.isFinite(baseline)) return ticks;
  const f = (v: number) => (log ? Math.log10(Math.max(v, Number.MIN_VALUE)) : v);
  const lo = f(domain[0]);
  const span = f(domain[1]) - lo;
  if (!(span > 0)) return ticks;
  const b = f(baseline);
  return ticks.filter((t) => Math.abs(f(t) - b) / span >= MIN_GAP);
}

interface LabelProps {
  viewBox?: { x?: number; y?: number };
}

/** True when the baseline sits in the lower part of the axis (put the caption above it). */
export function baselineLow(baseline: number, domain: readonly [number, number], log = false): boolean {
  const f = (v: number) => (log ? Math.log10(Math.max(v, Number.MIN_VALUE)) : v);
  const span = f(domain[1]) - f(domain[0]);
  return span > 0 && (f(baseline) - f(domain[0])) / span < 0.2;
}

/**
 * `label` for a horizontal <ReferenceLine>: `value` right-aligned in the y-axis gutter
 * at the line's height (like a tick label), with `caption` ("start") under it, or above
 * it when `captionAbove` (a baseline near the bottom would push it into the x-axis).
 */
export function baselineAxisLabel(value: string, caption: string, fill: string, opts: { title?: string; captionAbove?: boolean } = {}) {
  const { title, captionAbove = false } = opts;
  return function BaselineAxisLabel(p: LabelProps): ReactElement<SVGElement> {
    const x = p.viewBox?.x;
    const y = p.viewBox?.y;
    if (x === undefined || y === undefined || !Number.isFinite(x) || !Number.isFinite(y)) return <g />;
    const tx = x - 8;
    return (
      <text x={tx} y={y} textAnchor="end" fill={fill} fontSize={11}>
        {title && <title>{title}</title>}
        {captionAbove ? (
          <>
            <tspan x={tx} dy="-0.8em" fontSize={10}>
              {caption}
            </tspan>
            <tspan x={tx} dy="1.15em" fontWeight={600}>
              {value}
            </tspan>
          </>
        ) : (
          <>
            <tspan x={tx} dy="0.35em" fontWeight={600}>
              {value}
            </tspan>
            <tspan x={tx} dy="1.15em" fontSize={10}>
              {caption}
            </tspan>
          </>
        )}
      </text>
    );
  };
}
