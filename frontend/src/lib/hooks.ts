import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { errorMessage, isUnreachable } from "../api/client";
import type { StreamEventType } from "../api/types";
import { useRefreshSignal } from "./stream";
import { useToast } from "./toast";

// ---------------------------------------------------------------------------
// Page visibility
// ---------------------------------------------------------------------------

function subscribeVisibility(cb: () => void) {
  document.addEventListener("visibilitychange", cb);
  return () => document.removeEventListener("visibilitychange", cb);
}
const getVisible = () => document.visibilityState !== "hidden";

export function useDocumentVisible(): boolean {
  return useSyncExternalStore(subscribeVisibility, getVisible, () => true);
}

// ---------------------------------------------------------------------------
// Shared clock (relative timestamps re-render every 5 s without per-cell timers)
// ---------------------------------------------------------------------------

let nowValue = Date.now();
const nowSubs = new Set<() => void>();
let nowTimer: ReturnType<typeof setInterval> | undefined;

function subscribeNow(cb: () => void) {
  nowSubs.add(cb);
  if (!nowTimer) {
    nowTimer = setInterval(() => {
      nowValue = Date.now();
      for (const s of nowSubs) s();
    }, 5000);
  }
  return () => {
    nowSubs.delete(cb);
    if (nowSubs.size === 0 && nowTimer) {
      clearInterval(nowTimer);
      nowTimer = undefined;
    }
  };
}

/** Shared clock for relative times; never behind the real clock at render time. */
export function useNow(): number {
  const tick = useSyncExternalStore(subscribeNow, () => nowValue, () => nowValue);
  return Math.max(tick, Date.now());
}

// ---------------------------------------------------------------------------
// Server clock offset (serverTime − browserTime), measured from /api/status
// ---------------------------------------------------------------------------

let clockOffset = 0;
const offsetSubs = new Set<() => void>();

function subscribeOffset(cb: () => void) {
  offsetSubs.add(cb);
  return () => {
    offsetSubs.delete(cb);
  };
}

/**
 * Record the measured offset. Skews under 2 s are network jitter and ignored, and the
 * value only changes when it moves by a second or more, so subscribers (every <Time>)
 * are not re-rendered on each status poll.
 */
export function setServerClockOffset(ms: number) {
  if (!Number.isFinite(ms)) return;
  const v = Math.abs(ms) < 2000 ? 0 : Math.round(ms);
  if (Math.abs(v - clockOffset) < 1000) return;
  clockOffset = v;
  for (const s of offsetSubs) s();
}

export const serverClockOffset = () => clockOffset;

/**
 * "Now" on the SERVER's clock: compare server timestamps (last tick, fills, logs)
 * against this, not the browser clock, so a skewed browser clock never shows
 * "stalled" or "in 6m" for events that just happened.
 */
export function useServerNow(): number {
  const off = useSyncExternalStore(subscribeOffset, serverClockOffset, () => 0);
  return useNow() + off;
}

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------

const NETWORK_TOAST = "network-unreachable";

export interface PollOptions<T = unknown> {
  /** Poll interval (ms). 0 = fetch once. Default 7 500. */
  intervalMs?: number;
  /** Refetch immediately (reset timer) when any dependency changes. */
  deps?: readonly unknown[];
  /** SSE event types that trigger an immediate (debounced) refetch. */
  refreshOn?: readonly StreamEventType[];
  enabled?: boolean;
  /** Label used in the error toast ("Couldn't load <label>"). Omit to stay silent. */
  label?: string;
  /** Errors for which no toast is raised even with a `label` (a page banner covers them). */
  silentWhen?: (e: unknown) => boolean;
  /**
   * Stop polling once this returns true for a result (e.g. a finished backtest, or a
   * 404). It is read through a ref, so it never restarts the effect; after it fires,
   * only a manual refresh() or a change of `deps` fetches again (not tab visibility).
   */
  until?: (r: { data: T | undefined; error: unknown }) => boolean;
}

export interface PollResult<T> {
  data: T | undefined;
  error: unknown;
  /** True until the first response (success or failure). */
  loading: boolean;
  /** A request is in flight (previous data is kept on screen). */
  refreshing: boolean;
  updatedAt: number | null;
  /**
   * When the request that produced `data` was SENT (client ms). Compare pushed (SSE)
   * data against this, not `updatedAt`: a response that arrives after an SSE event may
   * still describe the server state from before it.
   */
  requestedAt: number | null;
  refresh: () => void;
  /** Optimistically replace data (e.g. with a mutation response). */
  mutate: (fn: (prev: T | undefined) => T | undefined) => void;
}

/**
 * Poll `fetcher` every `intervalMs`, paused while the tab is hidden (and refetched as
 * soon as it becomes visible). Requests never overlap; the previous data stays on
 * screen during refetches and after errors. Errors raise one toast per failure streak.
 *
 * `mutate()` bumps a generation counter: a response to a request that was already in
 * flight when the mutation landed is dropped (it may predate the mutation) and a fresh
 * request is made straight away, so a toggle never flips back to the old server state.
 */
export function usePolling<T>(fetcher: (signal: AbortSignal) => Promise<T>, opts: PollOptions<T> = {}): PollResult<T> {
  const { intervalMs = 7500, deps = [], refreshOn, enabled = true, label, until, silentWhen } = opts;
  const silentRef = useRef(silentWhen);
  silentRef.current = silentWhen;
  const untilRef = useRef(until);
  untilRef.current = until;
  /** Trigger (`deps|nonce`) at which `until` stopped the loop; null while polling. */
  const stoppedAt = useRef<string | null>(null);
  const fetchRef = useRef(fetcher);
  fetchRef.current = fetcher;
  const toast = useToast();
  const toastRef = useRef(toast);
  toastRef.current = toast;
  const labelRef = useRef(label);
  labelRef.current = label;
  const failing = useRef(false);
  const gen = useRef(0);
  const toastKey = useRef(`poll-${Math.random().toString(36).slice(2)}`);

  const [state, setState] = useState<{
    data: T | undefined;
    error: unknown;
    loading: boolean;
    refreshing: boolean;
    updatedAt: number | null;
    requestedAt: number | null;
  }>({ data: undefined, error: null, loading: true, refreshing: false, updatedAt: null, requestedAt: null });
  const [nonce, setNonce] = useState(0);
  const visible = useDocumentVisible();
  const depsKey = JSON.stringify(deps);

  useEffect(() => {
    if (!enabled || !visible) return;
    const trigger = `${depsKey}|${nonce}`;
    // Stopped by `until`: a visibility change or a new interval must not refetch; only
    // refresh() or new deps (a different trigger) start it again.
    if (stoppedAt.current !== null && stoppedAt.current === trigger) return;
    stoppedAt.current = null;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const ctrl = new AbortController();
    const stopNow = (r: { data: T | undefined; error: unknown }) => {
      try {
        return untilRef.current?.(r) ?? false;
      } catch {
        return false;
      }
    };

    const run = async () => {
      const startedGen = gen.current;
      const sentAt = Date.now();
      setState((s) => (s.refreshing ? s : { ...s, refreshing: true }));
      try {
        const data = await fetchRef.current(ctrl.signal);
        if (cancelled) return;
        if (gen.current !== startedGen) {
          // A mutation landed while this request was in flight; refetch instead of
          // overwriting it with possibly older server state.
          timer = setTimeout(run, 0);
          return;
        }
        setState({ data, error: null, loading: false, refreshing: false, updatedAt: Date.now(), requestedAt: sentAt });
        if (failing.current) {
          failing.current = false;
          toastRef.current.dismiss(toastKey.current);
          toastRef.current.dismiss(NETWORK_TOAST);
        }
        if (stopNow({ data, error: null })) {
          stoppedAt.current = trigger;
          return;
        }
      } catch (e) {
        // Only our own cleanup aborts deliberately (and it sets `cancelled` first). Any
        // other error, including a stray AbortError, is a failure: record it and keep
        // polling, otherwise the spinner and the disabled Refresh button would stick.
        if (cancelled) return;
        setState((s) => ({ ...s, error: e, loading: false, refreshing: false }));
        let silent = false;
        try {
          silent = silentRef.current?.(e) ?? false;
        } catch {
          silent = false;
        }
        if (silent && failing.current) {
          // The failure became one a banner covers: drop the toast raised earlier.
          toastRef.current.dismiss(toastKey.current);
        }
        if (!failing.current && labelRef.current && !silent) {
          // Unreachable backend: every poll fails at once, so collapse into one toast.
          const offline = isUnreachable(e);
          // "Backend unreachable" is about the whole server (both venues): no venue badge.
          toastRef.current.error(offline ? "Backend unreachable" : `Couldn't load ${labelRef.current}`, {
            key: offline ? NETWORK_TOAST : toastKey.current,
            message: errorMessage(e),
            ...(offline ? { venue: null } : {}),
          });
        }
        failing.current = true;
        if (stopNow({ data: undefined, error: e })) {
          stoppedAt.current = trigger;
          return;
        }
      }
      if (!cancelled && intervalMs > 0) timer = setTimeout(run, intervalMs);
    };
    void run();
    return () => {
      cancelled = true;
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [enabled, visible, intervalMs, depsKey, nonce]);

  // SSE-triggered (or locally invalidated) refresh, debounced so a burst of events
  // causes one request.
  const debounce = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useRefreshSignal(refreshOn, () => {
    clearTimeout(debounce.current);
    debounce.current = setTimeout(() => setNonce((n) => n + 1), 600);
  });
  useEffect(() => () => clearTimeout(debounce.current), []);

  const refresh = useCallback(() => setNonce((n) => n + 1), []);
  const mutate = useCallback((fn: (prev: T | undefined) => T | undefined) => {
    gen.current += 1;
    setState((s) => ({ ...s, data: fn(s.data), requestedAt: Date.now() }));
  }, []);

  return { ...state, refresh, mutate };
}

// ---------------------------------------------------------------------------
// Misc
// ---------------------------------------------------------------------------

export function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

/** localStorage-backed state for per-viewer conveniences; never throws. */
export function useStoredState<T>(key: string, initial: T): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? initial : (JSON.parse(raw) as T);
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (v: T) => {
      setValue(v);
      try {
        localStorage.setItem(key, JSON.stringify(v));
      } catch {
        /* storage unavailable: keep in memory only */
      }
    },
    [key],
  );
  return [value, set];
}

/** Run an async action with busy state + toasts. Returns the result or undefined. */
export function useAction() {
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const run = useCallback(
    async <R,>(key: string, fn: () => Promise<R>, messages: { success?: string; error: string }): Promise<R | undefined> => {
      setBusy(key);
      try {
        const r = await fn();
        if (messages.success) toast.success(messages.success);
        return r;
      } catch (e) {
        toast.error(messages.error, { message: errorMessage(e) });
        return undefined;
      } finally {
        setBusy(null);
      }
    },
    [toast],
  );
  return { busy, run };
}
