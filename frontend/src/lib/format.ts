/**
 * Display formatting. Conventions used everywhere in the UI:
 *  - dollars: always 2 dp ("$1,234.56"); P&L always signed ("+$3.20", "−$1.05")
 *    using a real minus sign (U+2212), so sign never depends on colour alone.
 *  - prices / fair values: cents ("93¢", "92.5¢"), since 1¢ = 1 % implied probability.
 *  - `_pct` fields are percentage points; fractions (win rate) are converted.
 */
export const MINUS = "−";
export const DASH = "—";

const usd2 = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const usd0 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const int = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

const finite = (v: number | null | undefined): v is number => typeof v === "number" && Number.isFinite(v);

/** "$1,234.56" / "−$12.30"; with sign: "+$12.30". Zero never gets a sign. */
export function fmtUsd(v: number | null | undefined, opts: { sign?: boolean; dp?: 0 | 2 } = {}): string {
  if (!finite(v)) return DASH;
  const f = opts.dp === 0 ? usd0 : usd2;
  const body = f.format(Math.abs(v));
  const isZero = Number(body.replace(/,/g, "")) === 0;
  if (isZero) return `$${body}`;
  if (v < 0) return `${MINUS}$${body}`;
  return opts.sign ? `+$${body}` : `$${body}`;
}

export const fmtPnl = (v: number | null | undefined) => fmtUsd(v, { sign: true });

/**
 * Signed dollars with 4 dp below $1 ("+$0.0123", "−$0.0040") and 2 dp above: for
 * per-trade means / CI bounds, where 2 dp would print a real +$0.004 as "$0.00" and
 * contradict "above zero". Matches the precision of the backend's readiness reasons.
 */
export function fmtPnlFine(v: number | null | undefined): string {
  if (!finite(v)) return DASH;
  const a = Math.abs(v);
  if (a >= 1) return fmtPnl(v);
  const body = a.toFixed(4);
  if (Number(body) === 0) return "$0.0000";
  return `${v < 0 ? MINUS : "+"}$${body}`;
}

/**
 * Axis tick: "$1,020"; cents when the span is under $10 or the tick itself is not a
 * whole dollar, so neighbouring ticks never round to the same or a wrong label.
 */
export function fmtUsdTick(v: number, span = 100): string {
  if (!finite(v)) return "";
  const whole = Math.abs(v - Math.round(v)) < 1e-9;
  return span < 10 || !whole ? fmtUsd(v) : fmtUsd(v, { dp: 0 });
}

/** Price in cents: 0.93 → "93¢", 0.925 → "92.5¢"; sign option for edges. */
export function fmtCents(p: number | null | undefined, opts: { sign?: boolean; dp?: number } = {}): string {
  if (!finite(p)) return DASH;
  const c = p * 100;
  const dp = opts.dp ?? (Math.abs(c - Math.round(c)) < 0.05 ? 0 : 1);
  const body = Math.abs(c).toFixed(dp);
  if (Number(body) === 0) return `${(0).toFixed(dp)}¢`;
  const sign = c < 0 ? MINUS : opts.sign ? "+" : "";
  return `${sign}${body}¢`;
}

/** Percentage points: 12.345 → "12.3%"; sign option → "+12.3%". */
export function fmtPct(v: number | null | undefined, opts: { sign?: boolean; dp?: number } = {}): string {
  if (!finite(v)) return DASH;
  const dp = opts.dp ?? 1;
  const body = Math.abs(v).toFixed(dp);
  if (Number(body) === 0) return `${(0).toFixed(dp)}%`;
  const sign = v < 0 ? MINUS : opts.sign ? "+" : "";
  return `${sign}${body}%`;
}

/** Fraction → percent: 0.574 → "57.4%". */
export function fmtFrac(v: number | null | undefined, dp = 1): string {
  return finite(v) ? fmtPct(v * 100, { dp }) : DASH;
}

export function fmtInt(v: number | null | undefined): string {
  if (!finite(v)) return DASH;
  const s = int.format(Math.abs(v));
  return v < 0 && s !== "0" ? `${MINUS}${s}` : s;
}

export function fmtCompact(v: number | null | undefined): string {
  if (!finite(v)) return DASH;
  return Math.abs(v) < 10_000 ? fmtInt(v) : compact.format(v).replace("-", MINUS);
}

export function fmtNum(v: number | null | undefined, dp = 2): string {
  if (!finite(v)) return DASH;
  const s = Math.abs(v).toFixed(dp);
  return v < 0 && Number(s) !== 0 ? `${MINUS}${s}` : s;
}

/**
 * Drawdowns are positive magnitudes shown as losses: "−$12.30" / "−4.2%". A drawdown
 * that rounds to zero gets no sign ("$0.00", "0.0%"), like every other zero.
 */
export function fmtDrawdownUsd(v: number | null | undefined): string {
  if (!finite(v)) return DASH;
  const s = fmtUsd(Math.abs(v));
  return s === fmtUsd(0) ? s : `${MINUS}${s}`;
}

export function fmtDrawdownPct(v: number | null | undefined, dp = 1): string {
  if (!finite(v)) return DASH;
  const s = fmtPct(Math.abs(v), { dp });
  return s === fmtPct(0, { dp }) ? s : `${MINUS}${s}`;
}

export type PnlTone = "pos" | "neg" | "zero";

/**
 * Tone of a signed value. `eps` is the dead zone treated as zero: half a cent (the
 * rounding step of a $ amount shown to 2 dp) by default. Values shown in ¢ must use a
 * matching, much smaller dead zone (see centsTone), or a real +0.4¢ edge reads neutral.
 */
export function pnlTone(v: number | null | undefined, eps = 0.005): PnlTone {
  if (!finite(v) || Math.abs(v) < eps) return "zero";
  return v > 0 ? "pos" : "neg";
}

/**
 * Tone of a per-contract value exactly as fmtCents displays it: "zero" only when the
 * rendered number is 0 (so the colour always agrees with the printed sign).
 */
export function centsTone(p: number | null | undefined, dp?: number): PnlTone {
  if (!finite(p)) return "zero";
  const c = p * 100;
  const d = dp ?? (Math.abs(c - Math.round(c)) < 0.05 ? 0 : 1);
  if (Number(Math.abs(c).toFixed(d)) === 0) return "zero";
  return c > 0 ? "pos" : "neg";
}

// ---------------------------------------------------------------------------
// Time
// ---------------------------------------------------------------------------

export function parseTs(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isFinite(t) ? t : null;
}

/** "just now", "12s ago", "5m ago", "3h ago", "2d ago", "in 4h". */
export function fmtRelative(iso: string | null | undefined, now = Date.now()): string {
  const t = parseTs(iso);
  if (t === null) return DASH;
  const d = t - now;
  const a = Math.abs(d);
  const s = Math.round(a / 1000);
  let body: string;
  if (s < 5) return "just now";
  if (s < 60) body = `${s}s`;
  else if (s < 3600) body = `${Math.floor(s / 60)}m`;
  else if (s < 86400) {
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    body = h < 10 && m ? `${h}h ${m}m` : `${h}h`;
  } else if (s < 86400 * 60) body = `${Math.floor(s / 86400)}d`;
  else body = `${Math.floor(s / (86400 * 30))}mo`;
  return d > 0 ? `in ${body}` : `${body} ago`;
}

const absFmtSameYear = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
const absFmtSeconds = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});
const absFmtFull = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
const dateOnly = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric" });
const dateOnlyUtc = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
/** Built once: constructing an Intl formatter per <Time> render was the hot path. */
const tooltipFmt = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});
const tzName = (() => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone ?? "local";
  } catch {
    return "local";
  }
})();

/** Local absolute time, compact: "Sep 26, 14:03" (with year if not this year). */
export function fmtAbsolute(iso: string | null | undefined, opts: { seconds?: boolean } = {}): string {
  const t = parseTs(iso);
  if (t === null) return DASH;
  const d = new Date(t);
  if (d.getFullYear() !== new Date().getFullYear()) return absFmtFull.format(d);
  return (opts.seconds ? absFmtSeconds : absFmtSameYear).format(d);
}

/** Tooltip text: local + UTC. */
export function fmtTooltipTime(iso: string | null | undefined): string {
  const t = parseTs(iso);
  if (t === null) return "";
  const d = new Date(t);
  return `${tooltipFmt.format(d)} (${tzName}) · ${d.toISOString().replace(".000Z", "Z")}`;
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return DASH;
  // Bare dates (YYYY-MM-DD) are calendar dates, not instants — don't shift by timezone.
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (m) return dateOnly.format(new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
  const t = parseTs(iso);
  return t === null ? iso : dateOnly.format(new Date(t));
}

/**
 * A calendar date the user picked (backtest start/end). Bare "YYYY-MM-DD" is shown
 * as-is; a datetime echo such as "2025-09-26T00:00:00Z" is shown in UTC, so it never
 * slides back a day for viewers west of UTC.
 */
export function fmtCalendarDate(v: string | null | undefined): string {
  if (!v) return DASH;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(v);
  if (m && v.length === 10) return dateOnly.format(new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
  const t = parseTs(v);
  return t === null ? v : dateOnlyUtc.format(new Date(t));
}

/** Local calendar day as "YYYY-MM-DD" (what <input type="date"> expects). */
export function isoLocalDay(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// ---------------------------------------------------------------------------
// Kalshi
// ---------------------------------------------------------------------------

/** Series ticker = prefix of the event/market ticker before the first "-". */
export function seriesOf(ticker: string | null | undefined): string {
  return (ticker ?? "").split("-")[0] ?? "";
}

/** Backend `url` when present, else best-effort https://kalshi.com/markets/{series_lower}. */
export function kalshiUrl(url: string | null | undefined, ticker?: string | null, eventTicker?: string | null): string | null {
  if (url && /^https?:\/\//i.test(url)) return url;
  const series = seriesOf(eventTicker || ticker);
  return series ? `https://kalshi.com/markets/${series.toLowerCase()}` : null;
}

/** "YES" / "NO"; anything else (an unknown side) is "?". */
export const sideLabel = (s: string) => {
  const v = s.toLowerCase();
  return v === "yes" || v === "no" ? v.toUpperCase() : "?";
};

export function humanize(key: string): string {
  const s = key.replace(/_/g, " ").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}
