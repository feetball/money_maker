/** Global engine status (GET /api/status every 5 s) shared by the header and pages. */
import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "../api/client";
import type { StatusResponse } from "../api/types";
import { setServerClockOffset, usePolling } from "./hooks";
import { useStreamSubscription } from "./stream";

/** Which engine action is in flight ("engine" = start/stop, "kill" = kill switch). */
export type EngineBusy = "engine" | "kill" | null;

interface StatusContextValue {
  status: StatusResponse | undefined;
  error: unknown;
  loading: boolean;
  updatedAt: number | null;
  refresh: () => void;
  /** Apply a status payload returned by start/stop/kill-switch. */
  apply: (s: StatusResponse) => void;
  /**
   * In-flight engine action, shared by EVERY EngineControls instance (header + the
   * Dashboard's Engine card) so a second start/stop cannot be sent while one is pending.
   */
  engineBusy: EngineBusy;
  /** Claim the engine-action slot; false when another action is already in flight. */
  beginEngineAction: (b: Exclude<EngineBusy, null>) => boolean;
  endEngineAction: () => void;
}

const StatusContext = createContext<StatusContextValue>({
  status: undefined,
  error: null,
  loading: true,
  updatedAt: null,
  refresh: () => undefined,
  apply: () => undefined,
  engineBusy: null,
  beginEngineAction: () => true,
  endEngineAction: () => undefined,
});

export function StatusProvider({ children }: { children: ReactNode }) {
  const poll = usePolling(
    async (signal) => {
      const sent = Date.now();
      const s = await api.status({ signal });
      // Server clock offset from server_time, measured at the midpoint of the request.
      const server = Date.parse(s.server_time);
      if (Number.isFinite(server)) setServerClockOffset(server - (sent + Date.now()) / 2);
      return s;
    },
    { intervalMs: 5000 },
  );
  const { mutate } = poll;
  const [engineBusy, setEngineBusy] = useState<EngineBusy>(null);
  // Synchronous guard: two clicks in the same frame (header + Engine card) must not
  // both get through before React re-renders with the busy state.
  const busyRef = useRef<EngineBusy>(null);
  const beginEngineAction = useCallback((b: Exclude<EngineBusy, null>) => {
    if (busyRef.current !== null) return false;
    busyRef.current = b;
    setEngineBusy(b);
    return true;
  }, []);
  const endEngineAction = useCallback(() => {
    busyRef.current = null;
    setEngineBusy(null);
  }, []);

  // Live tick info from SSE between polls.
  useStreamSubscription(["tick"], (e) => {
    if (e.type !== "tick") return;
    mutate((prev) =>
      prev
        ? {
            ...prev,
            engine: {
              ...prev.engine,
              last_tick_at: e.data.ts ?? new Date(e.receivedAt).toISOString(),
              tick_count: typeof e.data.tick_count === "number" ? e.data.tick_count : prev.engine.tick_count + 1,
              universe_size: typeof e.data.universe_size === "number" ? e.data.universe_size : prev.engine.universe_size,
            },
          }
        : prev,
    );
  });

  const apply = useCallback((s: StatusResponse) => mutate(() => s), [mutate]);

  const value = useMemo<StatusContextValue>(
    () => ({
      status: poll.data,
      error: poll.error,
      loading: poll.loading,
      updatedAt: poll.updatedAt,
      refresh: poll.refresh,
      apply,
      engineBusy,
      beginEngineAction,
      endEngineAction,
    }),
    [poll.data, poll.error, poll.loading, poll.updatedAt, poll.refresh, apply, engineBusy, beginEngineAction, endEngineAction],
  );
  return <StatusContext.Provider value={value}>{children}</StatusContext.Provider>;
}

export const useStatus = () => useContext(StatusContext);
