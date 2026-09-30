/**
 * Coinbase display units (contract §14): quantities with the base symbol
 * ("0.01234567 BTC"), USD prices at the precision the price level needs
 * ("$84,475.95", "$0.1234", "$0.00001812"), fees as "$0.61 (0.60%)".
 * Signs use a real minus (U+2212) like the rest of the UI.
 */
import { DASH, fmtUsd, MINUS } from "../../lib/format";

const finite = (v: number | null | undefined): v is number => typeof v === "number" && Number.isFinite(v);

const qtyFmt = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 8 });
const qtyBig = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** "0.01234567 BTC", "0.50 ETH", "1,234.50 DOGE" (8 dp max, trailing zeros trimmed to 2). */
export function fmtQty(q: number | null | undefined, base?: string | null, opts: { sign?: boolean } = {}): string {
  if (!finite(q)) return DASH;
  const a = Math.abs(q);
  const body = a >= 1_000_000 ? qtyBig.format(a) : qtyFmt.format(a);
  const zero = Number(body.replace(/,/g, "")) === 0;
  const sign = zero ? "" : q < 0 ? MINUS : opts.sign ? "+" : "";
  return `${sign}${body}${base ? ` ${base}` : ""}`;
}

const px2 = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const px4 = new Intl.NumberFormat("en-US", { minimumFractionDigits: 4, maximumFractionDigits: 4 });

/** USD price per unit: 2 dp from $1, 4 dp from 1¢, else 4 significant digits. */
export function fmtPrice(p: number | null | undefined): string {
  if (!finite(p)) return DASH;
  const a = Math.abs(p);
  let body: string;
  if (a >= 1 || a === 0) body = px2.format(a);
  else if (a >= 0.01) body = px4.format(a);
  else {
    const s = a.toPrecision(4);
    body = /e/.test(s) ? a.toFixed(12).replace(/0+$/, "") : s;
  }
  return `${p < 0 ? MINUS : ""}$${body}`;
}

/** Fee rate as a percentage: 0.006 → "0.60%", 0.00125 → "0.125%". */
export function fmtRate(rate: number | null | undefined): string {
  if (!finite(rate)) return DASH;
  const pct = rate * 100;
  const dp = Math.abs(pct * 100 - Math.round(pct * 100)) > 1e-6 ? 3 : 2;
  return `${pct.toFixed(dp)}%`;
}

/** "$0.61 (0.60%)"; the rate is derived from the notional when not given. */
export function fmtFee(fee: number | null | undefined, rate?: number | null, notional?: number | null): string {
  if (!finite(fee)) return DASH;
  const r = finite(rate) ? rate : finite(notional) && notional > 0 ? fee / notional : null;
  return r === null ? fmtUsd(fee) : `${fmtUsd(fee)} (${fmtRate(r)})`;
}

/** Basis points: 12.3 → "12.3 bps" (signed with `sign`). */
export function fmtBps(v: number | null | undefined, opts: { sign?: boolean; dp?: number } = {}): string {
  if (!finite(v)) return DASH;
  const dp = opts.dp ?? (Math.abs(v) >= 100 ? 0 : 1);
  const body = Math.abs(v).toFixed(dp);
  if (Number(body) === 0) return `${(0).toFixed(dp)} bps`;
  return `${v < 0 ? MINUS : opts.sign ? "+" : ""}${body} bps`;
}

/** Fraction → percent with 1 dp: 0.3333 → "33.3%". */
export function fmtWeight(w: number | null | undefined): string {
  if (!finite(w)) return DASH;
  return `${(w * 100).toFixed(1)}%`;
}

/** Bar size in words: 3600 → "1h", 86400 → "1d", 900 → "15m". */
export function fmtGranularity(s: number | null | undefined): string {
  if (!finite(s) || s <= 0) return DASH;
  if (s % 86400 === 0) return `${s / 86400}d`;
  if (s % 3600 === 0) return `${s / 3600}h`;
  if (s % 60 === 0) return `${s / 60}m`;
  return `${s}s`;
}

export function granularityLabel(s: number): string {
  return s === 86400 ? "Daily bars" : s === 3600 ? "Hourly bars" : `${fmtGranularity(s)} bars`;
}

const compactUsd = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

/** "$1.9B", "$412K", "$950". */
export function fmtUsdCompact(v: number | null | undefined): string {
  if (!finite(v)) return DASH;
  if (Math.abs(v) < 1000) return fmtUsd(v, { dp: 0 });
  return `${v < 0 ? MINUS : ""}$${compactUsd.format(Math.abs(v))}`;
}

/** Percentage points with sign and 2 dp: "+3.21 pp" (differences of returns). */
export function fmtPp(v: number | null | undefined, dp = 1): string {
  if (!finite(v)) return DASH;
  const body = Math.abs(v).toFixed(dp);
  if (Number(body) === 0) return `${(0).toFixed(dp)} pp`;
  return `${v < 0 ? MINUS : "+"}${body} pp`;
}

export const baseOf = (pid: string) => pid.split("-")[0] ?? pid;

/**
 * Category label for the shared horizontal bar chart, whose axis is a fixed ~118 px:
 * long strategy names get a middle ellipsis instead of being clipped at the left edge
 * (the table twin and the tooltip keep the full name).
 */
export function barLabel(name: string, max = 14): string {
  if (name.length <= max) return name;
  const head = Math.ceil((max - 1) * 0.6);
  return `${name.slice(0, head)}…${name.slice(name.length - (max - 1 - head))}`;
}
