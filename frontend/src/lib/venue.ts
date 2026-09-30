/**
 * Venue plumbing shared by the shell and pages (docs/COINBASE_CONTRACT.md §14).
 *
 * - `VenueScope` / `useVenueScope` (re-exported from ./venueScope): marks a subtree as
 *   one paper account's, so tables, KPI tiles, toasts and dialogs inside it are labeled.
 * - `VENUE_CHART_COLORS` mirrors the --venue-* tokens as hex for recharts (SVG
 *   presentation attributes cannot read CSS variables).
 * - `venuePageTitle()` builds the browser tab title ("Kalshi · Positions — kalshibot").
 */
import { VENUES, type Venue } from "../components/Venue";
import { useTheme } from "./theme";

export { useResolvedVenue, useVenueScope, VenueScope } from "./venueScope";

// ---------------------------------------------------------------------------
// Chart colours (hex twins of tokens.css --venue-*; validated with the dataviz
// validator as a 2-series categorical palette in both modes)
// ---------------------------------------------------------------------------

export const VENUE_CHART_COLORS: Record<"dark" | "light", Record<Venue, string>> = {
  dark: { kalshi: "#19a6a1", coinbase: "#9a7cf2" },
  light: { kalshi: "#00918b", coinbase: "#6e4fd6" },
};

export function useVenueChartColors(): Record<Venue, string> {
  return VENUE_CHART_COLORS[useTheme().resolved];
}

// ---------------------------------------------------------------------------
// Document titles
// ---------------------------------------------------------------------------

export const APP_NAME = "kalshibot";

/** Page names per path segment under a venue (shared by both venues). */
const PAGE_TITLES: Record<string, string> = {
  "": "Dashboard",
  positions: "Positions",
  history: "History",
  strategies: "Strategies",
  signals: "Signals",
  markets: "Markets",
  analytics: "Analytics",
  backtests: "Backtests",
  settings: "Settings",
};

/**
 * "Overview — kalshibot", "Kalshi · Positions — kalshibot",
 * "Coinbase · Backtest #12 — kalshibot", "Page not found — kalshibot".
 */
export function venuePageTitle(pathname: string): string {
  const parts = pathname.replace(/\/+$/, "").split("/").filter(Boolean);
  if (parts.length === 0) return `Overview — ${APP_NAME}`;
  // Pre-venue Kalshi paths (/positions, …) redirect to /kalshi/…; title them as Kalshi.
  if (parts[0] && parts[0] in PAGE_TITLES) parts.unshift("kalshi");
  const [first, page = "", sub] = parts;
  const venue = first === "kalshi" || first === "coinbase" ? (first as Venue) : null;
  if (!venue) return `Page not found — ${APP_NAME}`;
  const name = VENUES[venue].name;
  if (page === "backtests" && sub !== undefined && parts.length === 3) {
    return `${name} · Backtest #${decodeURIComponentSafe(sub)} — ${APP_NAME}`;
  }
  const title = parts.length <= 2 ? PAGE_TITLES[page] : undefined;
  return title ? `${name} · ${title} — ${APP_NAME}` : `${name} · Page not found — ${APP_NAME}`;
}

function decodeURIComponentSafe(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}
