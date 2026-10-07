/** Browser tab titles: "Positions — kalshibot", "Backtest #12 — kalshibot", "Page not found — kalshibot". */
export const APP_NAME = "kalshibot";

/** Page names per first path segment ("" = the dashboard at /). */
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

export function pageTitle(pathname: string): string {
  const parts = pathname.replace(/\/+$/, "").split("/").filter(Boolean);
  const [page = "", sub] = parts;
  if (page === "backtests" && sub !== undefined && parts.length === 2) {
    return `Backtest #${decodeURIComponentSafe(sub)} — ${APP_NAME}`;
  }
  const title = parts.length <= 1 && Object.prototype.hasOwnProperty.call(PAGE_TITLES, page) ? PAGE_TITLES[page] : undefined;
  return title ? `${title} — ${APP_NAME}` : `Page not found — ${APP_NAME}`;
}

function decodeURIComponentSafe(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}
