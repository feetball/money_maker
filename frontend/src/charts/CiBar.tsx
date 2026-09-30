import { useChartColors } from "./palette";

/**
 * Inline forest-plot cell: 95 % CI whisker + mean dot against a zero line, on a
 * domain shared by every row of the same basis so intervals are comparable. The
 * caller supplies the formatter and unit ("per contract" in ¢, "per trade" in $),
 * so the accessible label always matches the numbers shown next to it.
 */
export function CiBar({
  lo,
  hi,
  mean,
  domain,
  format,
  unit,
  width = 132,
}: {
  lo: number | null;
  hi: number | null;
  /** null when the mean is not on the CI's basis (then only the interval is drawn). */
  mean: number | null;
  domain: [number, number];
  format: (v: number) => string;
  /** e.g. "per contract", "per trade", "(basis not reported)". */
  unit: string;
  width?: number;
}) {
  const c = useChartColors();
  const h = 18;
  const pad = 5;
  const [d0, d1] = domain;
  const span = d1 - d0 || 1;
  const sx = (v: number) => pad + ((Math.min(d1, Math.max(d0, v)) - d0) / span) * (width - 2 * pad);
  const label =
    lo !== null && hi !== null
      ? `95% CI ${format(lo)} to ${format(hi)} ${unit}${mean !== null ? `, mean ${format(mean)}` : ""}`
      : "No confidence interval yet";
  return (
    <svg width={width} height={h} role="img" aria-label={label} className="ci-bar">
      <title>{label}</title>
      <line x1={sx(0)} x2={sx(0)} y1={1} y2={h - 1} stroke={c.axis} strokeWidth={1} />
      {lo !== null && hi !== null && (
        <>
          <line x1={sx(lo)} x2={sx(hi)} y1={h / 2} y2={h / 2} stroke={c.series1} strokeWidth={2} strokeLinecap="round" />
          <line x1={sx(lo)} x2={sx(lo)} y1={h / 2 - 4} y2={h / 2 + 4} stroke={c.series1} strokeWidth={2} strokeLinecap="round" />
          <line x1={sx(hi)} x2={sx(hi)} y1={h / 2 - 4} y2={h / 2 + 4} stroke={c.series1} strokeWidth={2} strokeLinecap="round" />
        </>
      )}
      {mean !== null && <circle cx={sx(mean)} cy={h / 2} r={4} fill={c.series1} stroke={c.surface} strokeWidth={2} />}
    </svg>
  );
}
