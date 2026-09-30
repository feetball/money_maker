/**
 * Shared Coinbase engine status (GET /api/coinbase/status every 5 s while anything
 * shows it), without a React provider: the top bar pill, the Coinbase dashboard and
 * Settings all read one poll via `useCbStatus()`, and it stops when nothing is
 * mounted or the tab is hidden. Live `tick` / `bar` SSE events update the timestamps
 * between polls.
 */
import { useEffect, useSyncExternalStore } from "react";
import { cbApi } from "./client";
import { cbStreamStore } from "./stream";
import type { CbStatus } from "./types";

const POLL_MS = 5000;

export type CbEngineBusy = "engine" | "kill" | null;

export interface CbStatusSnapshot {
  status: CbStatus | undefined;
  error: unknown;
  loading: boolean;
  /** Browser ms of the last successful poll. */
  updatedAt: number | null;
  /** In-flight engine action (shared by every CoinbaseEngineControls instance). */
  busy: CbEngineBusy;
}

class CbStatusStore {
  private snap: CbStatusSnapshot = { status: undefined, error: null, loading: true, updatedAt: null, busy: null };
  private subs = new Set<() => void>();
  private users = 0;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private ctrl: AbortController | null = null;
  private gen = 0;
  private offStream: (() => void) | null = null;
  private listening = false;

  getSnapshot = () => this.snap;

  subscribe = (cb: () => void) => {
    this.subs.add(cb);
    return () => {
      this.subs.delete(cb);
    };
  };

  private set(p: Partial<CbStatusSnapshot>) {
    this.snap = { ...this.snap, ...p };
    for (const s of this.subs) s();
  }

  acquire = () => {
    this.users += 1;
    if (this.users === 1) {
      if (!this.listening) {
        this.listening = true;
        document.addEventListener("visibilitychange", this.onVisibility);
      }
      this.offStream = cbStreamStore.subscribeEvents((e) => {
        const s = this.snap.status;
        if (!s) return;
        if (e.type === "tick") {
          this.set({
            status: {
              ...s,
              engine: {
                ...s.engine,
                last_tick_at: e.data.ts ?? new Date(e.receivedAt).toISOString(),
                tick_count: typeof e.data.tick_count === "number" ? e.data.tick_count : s.engine.tick_count + 1,
              },
            },
          });
        } else if (e.type === "bar" && e.data.bar_end) {
          this.set({ status: { ...s, engine: { ...s.engine, last_bar_at: e.data.bar_end } } });
        }
      });
      this.refresh();
    }
    return () => this.release();
  };

  private release() {
    this.users = Math.max(0, this.users - 1);
    if (this.users > 0) return;
    clearTimeout(this.timer);
    this.timer = undefined;
    this.ctrl?.abort();
    this.ctrl = null;
    this.offStream?.();
    this.offStream = null;
    if (this.listening) {
      this.listening = false;
      document.removeEventListener("visibilitychange", this.onVisibility);
    }
  }

  private onVisibility = () => {
    if (this.users > 0 && document.visibilityState !== "hidden") this.refresh();
  };

  /** Fetch now (and keep polling while in use and visible). */
  refresh = () => {
    clearTimeout(this.timer);
    this.timer = undefined;
    this.ctrl?.abort();
    if (this.users === 0) return;
    const ctrl = new AbortController();
    this.ctrl = ctrl;
    const g = this.gen;
    cbApi.status({ signal: ctrl.signal }).then(
      (s) => {
        if (ctrl.signal.aborted) return;
        // A start/stop/kill response applied meanwhile is newer: refetch instead.
        if (g !== this.gen) return this.schedule(0);
        this.set({ status: s, error: null, loading: false, updatedAt: Date.now() });
        this.schedule(POLL_MS);
      },
      (e: unknown) => {
        if (ctrl.signal.aborted) return;
        this.set({ error: e, loading: false });
        this.schedule(POLL_MS);
      },
    );
  };

  private schedule(ms: number) {
    clearTimeout(this.timer);
    if (this.users === 0 || document.visibilityState === "hidden") return;
    this.timer = setTimeout(this.refresh, ms);
  }

  /** Apply a status returned by start/stop/kill-switch. */
  apply = (s: CbStatus) => {
    this.gen += 1;
    this.set({ status: s, error: null, loading: false, updatedAt: Date.now() });
  };

  /** Claim the engine-action slot; false while another action is in flight. */
  begin = (b: Exclude<CbEngineBusy, null>) => {
    if (this.snap.busy !== null) return false;
    this.set({ busy: b });
    return true;
  };

  end = () => this.set({ busy: null });
}

export const cbStatusStore = new CbStatusStore();

export interface CbStatusHook extends CbStatusSnapshot {
  refresh: () => void;
  apply: (s: CbStatus) => void;
  begin: (b: Exclude<CbEngineBusy, null>) => boolean;
  end: () => void;
}

/** Coinbase engine status, shared by every caller (polls only while something uses it). */
export function useCbStatus(): CbStatusHook {
  useEffect(() => cbStatusStore.acquire(), []);
  const snap = useSyncExternalStore(cbStatusStore.subscribe, cbStatusStore.getSnapshot, cbStatusStore.getSnapshot);
  return { ...snap, refresh: cbStatusStore.refresh, apply: cbStatusStore.apply, begin: cbStatusStore.begin, end: cbStatusStore.end };
}
