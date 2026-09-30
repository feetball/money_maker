/**
 * App-wide SSE connection (one EventSource for the whole app) feeding a small external
 * store. Pages read the recent-event ring buffer (activity log) or subscribe to event
 * types to refresh their polled data immediately.
 */
import { createContext, useContext, useEffect, useRef, useSyncExternalStore, type ReactNode } from "react";
import { useEventStream, type EventStreamInfo } from "../api/client";
import type { Account, StreamEvent, StreamEventType } from "../api/types";

const MAX_EVENTS = 300;

type Listener = (e: StreamEvent) => void;

class StreamStore {
  private events: StreamEvent[] = [];
  private account: { data: Partial<Account>; receivedAt: number } | null = null;
  private listeners = new Set<Listener>();
  private snapshotSubs = new Set<() => void>();
  private invalidateSubs = new Set<(types: ReadonlySet<string>) => void>();

  push = (e: StreamEvent) => {
    this.events = [e, ...this.events].slice(0, MAX_EVENTS);
    if (e.type === "account") this.account = { data: e.data, receivedAt: e.receivedAt };
    for (const l of this.listeners) l(e);
    for (const s of this.snapshotSubs) s();
  };

  /** Forget every buffered event and the cached account (e.g. after an account reset). */
  clear = () => {
    this.events = [];
    this.account = null;
    for (const s of this.snapshotSubs) s();
  };

  /**
   * Ask every poll that refreshes on these event types to refetch now, without adding
   * anything to the event feed (e.g. after the kill switch cancelled resting orders).
   */
  invalidate = (types: readonly StreamEventType[]) => {
    const set = new Set<string>(types);
    for (const s of this.invalidateSubs) s(set);
  };

  subscribeInvalidate = (cb: (types: ReadonlySet<string>) => void) => {
    this.invalidateSubs.add(cb);
    return () => {
      this.invalidateSubs.delete(cb);
    };
  };

  subscribeEvents = (l: Listener) => {
    this.listeners.add(l);
    return () => {
      this.listeners.delete(l);
    };
  };

  subscribeSnapshot = (cb: () => void) => {
    this.snapshotSubs.add(cb);
    return () => {
      this.snapshotSubs.delete(cb);
    };
  };

  getEvents = () => this.events;
  getAccount = () => this.account;
}

export const streamStore = new StreamStore();

const StreamInfoContext = createContext<EventStreamInfo>({ state: "connecting", attempts: 0, nextRetryAt: null });

export function StreamProvider({ children }: { children: ReactNode }) {
  const info = useEventStream(streamStore.push);
  return <StreamInfoContext.Provider value={info}>{children}</StreamInfoContext.Provider>;
}

export const useStreamInfo = () => useContext(StreamInfoContext);

/** Most recent SSE events, newest first. */
export function useStreamEvents(): StreamEvent[] {
  return useSyncExternalStore(streamStore.subscribeSnapshot, streamStore.getEvents, streamStore.getEvents);
}

/** Latest `account` event: only the fields it carried (merged over the polled account). */
export function useStreamAccount(): { data: Partial<Account>; receivedAt: number } | null {
  return useSyncExternalStore(streamStore.subscribeSnapshot, streamStore.getAccount, streamStore.getAccount);
}

/**
 * Call `cb` when an SSE event of one of `types` arrives OR a local
 * streamStore.invalidate() names one of them (used by usePolling's refreshOn).
 */
export function useRefreshSignal(types: readonly StreamEventType[] | undefined, cb: () => void) {
  const cbRef = useRef(cb);
  cbRef.current = cb;
  const key = types ? types.join(",") : "";
  useEffect(() => {
    if (!key) return;
    const set = new Set(key.split(","));
    const offEvents = streamStore.subscribeEvents((e) => {
      if (set.has(e.type)) cbRef.current();
    });
    const offLocal = streamStore.subscribeInvalidate((ts) => {
      for (const t of ts) {
        if (set.has(t)) {
          cbRef.current();
          return;
        }
      }
    });
    return () => {
      offEvents();
      offLocal();
    };
  }, [key]);
}

/** Call `cb` for every event whose type is in `types`. */
export function useStreamSubscription(types: readonly StreamEventType[] | undefined, cb: (e: StreamEvent) => void) {
  const cbRef = useRef(cb);
  cbRef.current = cb;
  const key = types ? types.join(",") : "";
  useEffect(() => {
    if (!key) return;
    const set = new Set(key.split(","));
    return streamStore.subscribeEvents((e) => {
      if (set.has(e.type)) cbRef.current(e);
    });
  }, [key]);
}
