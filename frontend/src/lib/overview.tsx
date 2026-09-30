/**
 * GET /api/overview every 5 s, shared by the top bar's venue pills and the Overview
 * page (one request stream for both). Silent on failure (no toast): the pills and the
 * Overview page show the error state themselves, and /api/status already raises the
 * "backend unreachable" banner.
 */
import { createContext, useContext, type ReactNode } from "react";
import { api } from "../api/client";
import type { OverviewResponse } from "../api/types";
import { usePolling, type PollResult } from "./hooks";

const noop = () => undefined;

const OverviewContext = createContext<PollResult<OverviewResponse>>({
  data: undefined,
  error: null,
  loading: true,
  refreshing: false,
  updatedAt: null,
  requestedAt: null,
  refresh: noop,
  mutate: noop,
});

export function OverviewProvider({ children }: { children: ReactNode }) {
  const poll = usePolling((signal) => api.overview({ signal }), {
    intervalMs: 5000,
    // Kalshi fills/settlements move its equity: refresh right away (debounced).
    refreshOn: ["fill", "settlement"],
  });
  return <OverviewContext.Provider value={poll}>{children}</OverviewContext.Provider>;
}

export const useOverview = () => useContext(OverviewContext);
