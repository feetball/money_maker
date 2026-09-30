import { useEffect } from "react";
import { Link, Navigate, NavLink, Outlet, useLocation } from "react-router";
import { errorMessage, IS_MOCK, isUnreachable } from "../api/client";
import { fmtAbsolute } from "../lib/format";
import { useServerNow } from "../lib/hooks";
import { useStatus } from "../lib/status";
import { venuePageTitle } from "../lib/venue";
import { VenueScope } from "../lib/venueScope";
import { CoinbaseVenueStatus, EngineControls, engineErrorIsCurrent, KalshiVenueStatus, statusErrorSummary } from "./Engine";
import { ErrorBoundary } from "./ErrorBoundary";
import { Icon } from "./Icon";
import { VenueBadge, VenueBanner, venueFromPath, VENUES, type Venue } from "./Venue";

export interface NavItem {
  /** Path relative to the venue's base path ("" = the venue dashboard). */
  path: string;
  label: string;
}

/**
 * Pages of a venue section. Both venues have the same set (COINBASE_CONTRACT §14);
 * link text is not prefixed with the venue because the group header (a VenueBadge)
 * is always visible above it.
 */
export const VENUE_PAGES: NavItem[] = [
  { path: "", label: "Dashboard" },
  { path: "positions", label: "Positions & Orders" },
  { path: "history", label: "History" },
  { path: "strategies", label: "Strategies" },
  { path: "signals", label: "Signals" },
  { path: "markets", label: "Markets" },
  { path: "analytics", label: "Analytics" },
  { path: "backtests", label: "Backtests" },
  { path: "settings", label: "Settings" },
];

/** Pre-venue Kalshi paths (/positions, /backtests/12, …) that now live under /kalshi. */
export const LEGACY_KALSHI_PAGES = VENUE_PAGES.map((p) => p.path).filter(Boolean);

export const venueHref = (venue: Venue, path = "") => (path ? `${VENUES[venue].basePath}/${path}` : VENUES[venue].basePath);

function PaperBadge() {
  return (
    <span
      className="paper-badge"
      title="Paper trading on both venues: every order is simulated against live public order books (Kalshi event contracts, Coinbase spot crypto). No real orders are ever placed and no exchange credentials are used."
    >
      <Icon name="shield" />
      PAPER TRADING
    </span>
  );
}

/** Server-wide problems (both venues): only "the backend cannot be reached". */
function GlobalBanners() {
  const { status, error, updatedAt } = useStatus();
  if (!error || !isUnreachable(error)) return null;
  // Absolute time (not "40s ago"): text inside role=alert must not change every tick.
  const lastSeen = updatedAt ? fmtAbsolute(new Date(updatedAt).toISOString(), { seconds: true }) : null;
  return (
    <div className="banners">
      <div className="banner banner-bad" role="alert">
        <Icon name="plug" />
        <div>
          <strong>Backend unreachable.</strong> {errorMessage(error)}.
          {status && lastSeen ? <> Showing the last known state from {lastSeen}.</> : null} Engine controls for both venues are disabled until it
          answers. Start it with <code>uv run kalshibot serve</code>, or run the UI with <code>npm run dev:mock</code> for demo data.
        </div>
      </div>
    </div>
  );
}

/** Kalshi-only alerts (from /api/status), shown at the top of every Kalshi page. */
export function KalshiAlerts() {
  const { status, error, updatedAt } = useStatus();
  const serverNow = useServerNow();
  const out = [];
  const lastSeen = updatedAt ? fmtAbsolute(new Date(updatedAt).toISOString(), { seconds: true }) : null;
  const stale = error ? " (last known state)" : "";
  const badge = <VenueBadge venue="kalshi" />;
  // Unreachable is the global banner; an HTTP error from /api/status is Kalshi's own.
  if (error && !isUnreachable(error)) {
    out.push(
      <div key="status" className="banner banner-serious" role="alert">
        <Icon name="alert" />
        <div>
          {badge} <strong>Kalshi status unavailable: {statusErrorSummary(error)}.</strong> <span className="mono wrap">{errorMessage(error)}</span>
          {status && lastSeen ? <> Showing the last known state from {lastSeen}.</> : null} The server is running but could not report the Kalshi engine
          status.
        </div>
      </div>,
    );
  }
  if (status?.engine.kill_switch) {
    const reason = status.engine.kill_switch_reason;
    out.push(
      <div key="kill" className="banner banner-bad" role="status">
        <Icon name="shield" />
        <div>
          {badge} <strong>Kalshi kill switch is ON{stale}.</strong> New Kalshi entries are blocked for every strategy; existing Kalshi paper positions still
          settle.
          {reason && (
            <>
              {" "}
              Reason: <span className="mono wrap">{reason}</span>
            </>
          )}
        </div>
      </div>,
    );
  }
  // Only a CURRENT error gets a banner: the backend keeps the last failure after the
  // job recovers, so an old one is history (Engine card + pill tooltip).
  if (status?.engine.last_error && engineErrorIsCurrent(status.engine, serverNow)) {
    const at = status.engine.last_error_at;
    out.push(
      <div key="err" className="banner banner-serious" role="status">
        <Icon name="alert" />
        <div>
          {badge}{" "}
          <strong>
            Kalshi engine reported an error{at ? ` at ${fmtAbsolute(at, { seconds: true })}` : ""}
            {stale}:
          </strong>{" "}
          <span className="mono wrap">{status.engine.last_error}</span>
          {at && <span className="muted"> Failed jobs retry automatically; this banner clears 10 minutes after the error if the engine keeps ticking.</span>}
        </div>
      </div>,
    );
  }
  if (status && status.exchange.trading_active === false) {
    out.push(
      <div key="exch" className="banner banner-warn" role="status">
        <Icon name="clock" />
        <div>
          {badge} <strong>Kalshi exchange trading is paused{stale}</strong> (maintenance or off-hours). Kalshi paper fills are not simulated while it is
          closed.
        </div>
      </div>,
    );
  }
  return out.length ? <div className="banners">{out}</div> : null;
}

/**
 * Wrapper route for every /kalshi page: the venue scope (labels tables, KPI tiles,
 * toasts and dialogs "Kalshi"), the Kalshi banner with its engine controls, Kalshi
 * alerts, then the page. The page has its own error boundary so a crashing page never
 * takes the Start/Stop and kill-switch buttons with it.
 */
export function KalshiSection() {
  const location = useLocation();
  return (
    <VenueScope venue="kalshi">
      <VenueBanner venue="kalshi">
        <EngineControls compact />
      </VenueBanner>
      <KalshiAlerts />
      <ErrorBoundary key={location.pathname}>
        <Outlet />
      </ErrorBoundary>
    </VenueScope>
  );
}

/** /positions → /kalshi/positions (keeps ?query and #hash). */
export function LegacyKalshiRedirect() {
  const { pathname, search, hash } = useLocation();
  return <Navigate to={`/kalshi${pathname}${search}${hash}`} replace />;
}

function VenueNavGroup({ venue, active }: { venue: Venue; active: boolean }) {
  const v = VENUES[venue];
  return (
    <li className={`nav-group venue-${venue}${active ? " is-active" : ""}`}>
      <Link to={v.basePath} className="nav-group-head" aria-current={active ? "true" : undefined} title={`${v.description} — separate paper account`}>
        <VenueBadge venue={venue} long />
      </Link>
      <ul aria-label={`${v.name} pages`}>
        {VENUE_PAGES.map((n) => (
          <li key={n.path}>
            <NavLink to={venueHref(venue, n.path)} end={n.path === ""} className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}>
              {n.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </li>
  );
}

export function Layout() {
  const location = useLocation();
  const activeVenue = venueFromPath(location.pathname);

  useEffect(() => {
    document.title = venuePageTitle(location.pathname);
  }, [location.pathname]);

  // Narrow screens: the nav is one horizontal row; keep the active link in view.
  useEffect(() => {
    const nav = document.querySelector<HTMLElement>(".sidenav");
    const a = nav?.querySelector<HTMLElement>(".nav-link.active");
    if (!nav || !a || nav.scrollWidth <= nav.clientWidth) return;
    const n = nav.getBoundingClientRect();
    const r = a.getBoundingClientRect();
    if (r.left < n.left || r.right > n.right) nav.scrollLeft += r.left - n.left - 16;
  }, [location.pathname]);

  return (
    <div className={activeVenue ? `shell in-venue venue-${activeVenue}` : "shell"}>
      <a href="#main" className="skip-link">
        Skip to content
      </a>
      <header className="topbar">
        <NavLink to="/" className="brand" aria-label="kalshibot overview">
          <svg viewBox="0 0 32 32" width="22" height="22" aria-hidden="true">
            <rect width="32" height="32" rx="7" className="brand-bg" />
            <path d="M6 22l6-7 5 4 9-10" fill="none" className="brand-line" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span>kalshibot</span>
        </NavLink>
        <PaperBadge />
        {IS_MOCK && (
          <span className="mock-badge" title="VITE_MOCK=1: all data is generated in the browser; nothing talks to the backend.">
            MOCK DATA
          </span>
        )}
        <div className="topbar-spacer" />
        <div className="topbar-status" aria-label="Engines">
          <KalshiVenueStatus />
          <CoinbaseVenueStatus />
        </div>
      </header>
      <nav className="sidenav" aria-label="Primary">
        <ul className="nav-root">
          <li>
            <NavLink to="/" end className={({ isActive }) => (isActive ? "nav-link nav-overview active" : "nav-link nav-overview")}>
              Overview
            </NavLink>
          </li>
          <VenueNavGroup venue="kalshi" active={activeVenue === "kalshi"} />
          <VenueNavGroup venue="coinbase" active={activeVenue === "coinbase"} />
        </ul>
      </nav>
      <main id="main" className="main" tabIndex={-1}>
        <GlobalBanners />
        <ErrorBoundary key={location.pathname}>
          <Outlet />
        </ErrorBoundary>
      </main>
    </div>
  );
}
