import { useEffect } from "react";
import { Navigate, NavLink, Outlet, useLocation } from "react-router";
import { errorMessage, IS_MOCK, isUnreachable } from "../api/client";
import { fmtAbsolute } from "../lib/format";
import { useServerNow } from "../lib/hooks";
import { useStatus } from "../lib/status";
import { pageTitle } from "../lib/title";
import { EngineControls, EngineStatusPill, engineErrorIsCurrent, statusErrorSummary } from "./Engine";
import { ErrorBoundary } from "./ErrorBoundary";
import { Icon } from "./Icon";

export interface NavItem {
  /** Path relative to the root ("" = the dashboard at /). */
  path: string;
  label: string;
}

export const NAV_PAGES: NavItem[] = [
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

function ModeBadge() {
  const { status } = useStatus();
  if (status?.mode === "live") {
    const prod = status.live?.environment === "prod";
    return (
      <span
        className={`live-badge${prod ? " live-badge-prod" : ""}`}
        title={
          prod
            ? "LIVE trading: strategies place real orders on Kalshi with real money."
            : "Live trading against Kalshi's demo exchange (demo-api.kalshi.co): real order flow, fake money."
        }
      >
        <Icon name="alert" />
        {prod ? "LIVE · REAL MONEY" : "LIVE · DEMO"}
      </span>
    );
  }
  return (
    <span
      className="paper-badge"
      title="Paper trading: every order is simulated against live public Kalshi order books. No real orders are ever placed and no exchange credentials are used."
    >
      <Icon name="shield" />
      PAPER TRADING
    </span>
  );
}

/** Live mode: the ledger and the Kalshi account disagree (or could not be compared). */
function LiveReconcileBanner() {
  const { status } = useStatus();
  const live = status?.live;
  if (live && !live.ready) {
    return (
      <div className="banners">
        <div className="banner banner-bad" role="status">
          <Icon name="alert" />
          <div>
            <strong>Live trading is locked</strong> ({live.blocked_reason ?? "not started"}). Orders are refused and the engine cannot start. Add or fix
            the key in <NavLink to="/settings">Settings → Kalshi API keys</NavLink>.
          </div>
        </div>
      </div>
    );
  }
  const x = live?.exchange;
  if (!x) return null;
  const drift = x.cash_drift ?? 0;
  const n = x.position_mismatches.length;
  if (!x.error && n === 0 && Math.abs(drift) < 1) return null;
  return (
    <div className="banners">
      <div className="banner banner-warn" role="status">
        <Icon name="alert" />
        <div>
          <strong>Ledger and Kalshi disagree.</strong>{" "}
          {x.error
            ? `The last comparison failed: ${x.error}.`
            : [
                Math.abs(drift) >= 1 ? `The Kalshi balance differs from the ledger's cash by $${drift.toFixed(2)}.` : "",
                n ? `${n} market(s) hold a different position on Kalshi: ${x.position_mismatches.map((m) => m.ticker).slice(0, 4).join(", ")}${n > 4 ? "…" : ""}.` : "",
              ].join(" ")}{" "}
          See Settings → Live trading.
        </div>
      </div>
    </div>
  );
}

/** Server-wide problems: only "the backend cannot be reached". */
function GlobalBanners() {
  const { status, error, updatedAt } = useStatus();
  if (!error || !isUnreachable(error)) return <LiveReconcileBanner />;
  // Absolute time (not "40s ago"): text inside role=alert must not change every tick.
  const lastSeen = updatedAt ? fmtAbsolute(new Date(updatedAt).toISOString(), { seconds: true }) : null;
  return (
    <div className="banners">
      <div className="banner banner-bad" role="alert">
        <Icon name="plug" />
        <div>
          <strong>Backend unreachable.</strong> {errorMessage(error)}.
          {status && lastSeen ? <> Showing the last known state from {lastSeen}.</> : null} Engine controls are disabled until it answers. Start it with <code>uv run kalshibot serve</code>, or run the UI with <code>npm run dev:mock</code> for demo data.
        </div>
      </div>
    </div>
  );
}

/** Engine and exchange alerts (from /api/status), shown at the top of every page. */
export function KalshiAlerts() {
  const { status, error, updatedAt } = useStatus();
  const serverNow = useServerNow();
  const out = [];
  const lastSeen = updatedAt ? fmtAbsolute(new Date(updatedAt).toISOString(), { seconds: true }) : null;
  const stale = error ? " (last known state)" : "";
  // Unreachable is the global banner; an HTTP error from /api/status is Kalshi's own.
  if (error && !isUnreachable(error)) {
    out.push(
      <div key="status" className="banner banner-serious" role="alert">
        <Icon name="alert" />
        <div>
          <strong>Kalshi status unavailable: {statusErrorSummary(error)}.</strong> <span className="mono wrap">{errorMessage(error)}</span>
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
          <strong>Kalshi kill switch is ON{stale}.</strong> New Kalshi entries are blocked for every strategy; existing Kalshi paper positions still
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
          <strong>Kalshi exchange trading is paused{stale}</strong> (maintenance or off-hours). Kalshi paper fills are not simulated while it is
          closed.
        </div>
      </div>,
    );
  }
  return out.length ? <div className="banners">{out}</div> : null;
}

/** /kalshi/positions → /positions (keeps ?query and #hash): the former /kalshi/* paths, for old bookmarks. */
export function LegacyKalshiRedirect() {
  const { pathname, search, hash } = useLocation();
  return <Navigate to={`${pathname.replace(/^\/kalshi/, "") || "/"}${search}${hash}`} replace />;
}

export function Layout() {
  const location = useLocation();

  useEffect(() => {
    document.title = pageTitle(location.pathname);
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
    <div className="shell">
      <a href="#main" className="skip-link">
        Skip to content
      </a>
      <header className="topbar">
        <NavLink to="/" className="brand" aria-label="kalshibot dashboard">
          <svg viewBox="0 0 32 32" width="22" height="22" aria-hidden="true">
            <rect width="32" height="32" rx="7" className="brand-bg" />
            <path d="M6 22l6-7 5 4 9-10" fill="none" className="brand-line" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span>kalshibot</span>
        </NavLink>
        <ModeBadge />
        {IS_MOCK && (
          <span className="mock-badge" title="VITE_MOCK=1: all data is generated in the browser; nothing talks to the backend.">
            MOCK DATA
          </span>
        )}
        <div className="topbar-spacer" />
        <div className="topbar-status" aria-label="Engine">
          <EngineStatusPill />
        </div>
      </header>
      <nav className="sidenav" aria-label="Primary">
        <ul>
          {NAV_PAGES.map((n) => (
            <li key={n.path}>
              <NavLink to={n.path ? `/${n.path}` : "/"} end={n.path === ""} className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}>
                {n.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <main id="main" className="main" tabIndex={-1}>
        <GlobalBanners />
        {/* Outside the page's error boundary: a crashing page never takes Start/Stop and the kill switch with it. */}
        <div className="engine-bar">
          <EngineControls compact />
        </div>
        <KalshiAlerts />
        <ErrorBoundary key={location.pathname}>
          <Outlet />
        </ErrorBoundary>
      </main>
    </div>
  );
}
