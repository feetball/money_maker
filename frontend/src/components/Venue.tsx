/**
 * Venue identity - the one place that says what "Kalshi" and "Coinbase" look like.
 * (docs/COINBASE_CONTRACT.md §9). Every venue-scoped page header, KPI, table and
 * activity row uses these so the two paper accounts can never be confused:
 * a text label + a monogram + a colour token, never colour alone.
 *
 * Colour tokens (defined in styles/tokens.css for dark + light; the fallbacks below
 * keep this component usable before they exist):
 *   --venue-kalshi, --venue-kalshi-bg, --venue-kalshi-text      (teal)
 *   --venue-coinbase, --venue-coinbase-bg, --venue-coinbase-text (violet)
 *   --venue-kalshi-on, --venue-coinbase-on  (monogram glyph on the venue colour, ≥ 4.5:1)
 * Teal/violet are deliberately distinct from P&L blue/red and the status greens/ambers.
 */
import type { CSSProperties, ReactNode } from "react";

export type Venue = "kalshi" | "coinbase";

export interface VenueInfo {
  id: Venue;
  /** Short name used in badges and nav. */
  name: string;
  /** Full label for page headers: "KALSHI · prediction markets". */
  label: string;
  /** What is traded, for subtitles/tooltips. */
  description: string;
  /** Monogram shown in the badge square. */
  mono: string;
  /** URL prefix of this venue's pages. */
  basePath: string;
  /** API prefix. Kalshi keeps the legacy un-prefixed /api/* routes. */
  apiBase: string;
  color: string;
  bg: string;
  text: string;
  /** Text colour on `color` (the monogram glyph). */
  on: string;
}

export const VENUES: Record<Venue, VenueInfo> = {
  kalshi: {
    id: "kalshi",
    name: "Kalshi",
    label: "KALSHI · prediction markets",
    description: "Kalshi event contracts ($0–$1 per contract, settle yes/no)",
    mono: "K",
    basePath: "/kalshi",
    apiBase: "/api",
    color: "var(--venue-kalshi, #1fb5b0)",
    bg: "var(--venue-kalshi-bg, rgba(31, 181, 176, 0.14))",
    text: "var(--venue-kalshi-text, #3cc9c3)",
    on: "var(--venue-kalshi-on, #0d0d0d)",
  },
  coinbase: {
    id: "coinbase",
    name: "Coinbase",
    label: "COINBASE · crypto spot",
    description: "Coinbase spot crypto (coin quantities, USD prices)",
    mono: "C",
    basePath: "/coinbase",
    apiBase: "/api/coinbase",
    color: "var(--venue-coinbase, #9a7cf2)",
    bg: "var(--venue-coinbase-bg, rgba(154, 124, 242, 0.15))",
    text: "var(--venue-coinbase-text, #b39cf6)",
    on: "var(--venue-coinbase-on, #0d0d0d)",
  },
};

export const venueFromPath = (pathname: string): Venue | null =>
  pathname === "/coinbase" || pathname.startsWith("/coinbase/")
    ? "coinbase"
    : pathname === "/kalshi" || pathname.startsWith("/kalshi/")
      ? "kalshi"
      : null;

function Mono({ v, size }: { v: VenueInfo; size: number }) {
  const style: CSSProperties = {
    display: "inline-grid",
    placeItems: "center",
    width: size,
    height: size,
    borderRadius: 4,
    background: v.color,
    color: v.on,
    fontWeight: 800,
    fontSize: Math.round(size * 0.62),
    lineHeight: 1,
    flex: "none",
  };
  return (
    <span aria-hidden="true" style={style}>
      {v.mono}
    </span>
  );
}

/**
 * Compact venue tag for table rows, KPI tiles, feed items and nav:
 *   [K] Kalshi   /   [C] Coinbase
 * `long` swaps the name for the full label ("KALSHI · prediction markets").
 * `compact` draws only the monogram square (the name stays for screen readers and in
 * the tooltip), for places that repeat the venue on every row where the page already
 * names it; `compact="phone"` does that only at phone width (≤ 640px, see app.css).
 */
export function VenueBadge({ venue, long = false, size = "sm", title, compact = false }: {
  venue: Venue;
  long?: boolean;
  size?: "sm" | "md";
  title?: string;
  compact?: boolean | "phone";
}) {
  const v = VENUES[venue];
  const px = size === "md" ? 18 : 14;
  const mono = compact === true;
  const style: CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    gap: mono ? 0 : 6,
    padding: mono ? 1 : size === "md" ? "3px 8px 3px 4px" : "1px 6px 1px 2px",
    borderRadius: 999,
    background: v.bg,
    color: v.text,
    border: `1px solid ${v.color}`,
    fontSize: size === "md" ? 12 : 11,
    fontWeight: 700,
    letterSpacing: long ? "0.04em" : undefined,
    whiteSpace: "nowrap",
    verticalAlign: "middle",
  };
  return (
    <span
      className={`venue-badge venue-${venue}${compact === "phone" ? " venue-badge-phone-compact" : ""}`}
      style={style}
      title={title ?? (mono ? `${v.name} paper account — ${v.description}` : v.description)}
    >
      <Mono v={v} size={px} />
      <span className={mono ? "sr-only" : "venue-badge-name"}>{long ? v.label : v.name}</span>
    </span>
  );
}

/**
 * Coloured band placed at the very top of every venue-scoped page (above PageHeader),
 * so the active venue is visible even when scrolled into a table.
 */
export function VenueBanner({ venue, children }: { venue: Venue; children?: ReactNode }) {
  const v = VENUES[venue];
  const style: CSSProperties = {
    display: "flex",
    alignItems: "center",
    gap: 10,
    padding: "6px 12px",
    marginBottom: 12,
    borderRadius: 6,
    background: v.bg,
    borderLeft: `4px solid ${v.color}`,
    color: v.text,
    fontSize: 12,
    fontWeight: 700,
    letterSpacing: "0.04em",
  };
  return (
    <div className={`venue-banner venue-${venue}`} style={style} role="note" aria-label={`${v.label} — paper account`}>
      <Mono v={v} size={18} />
      <span>{v.label}</span>
      <span style={{ fontWeight: 500, letterSpacing: 0, opacity: 0.85 }}>· separate paper account</span>
      {children ? <span style={{ marginLeft: "auto", fontWeight: 500, letterSpacing: 0 }}>{children}</span> : null}
    </div>
  );
}
