/**
 * Coinbase SSE (GET /api/coinbase/stream, contract §13) — a separate connection from
 * the Kalshi stream, so neither venue's outage affects the other.
 *
 * One shared connection, reference counted: Coinbase pages (and any component that
 * shows live Coinbase state) call `useCbStreamConnection()`; the EventSource opens for
 * the first user and closes a few seconds after the last one leaves. It is also
 * closed while the tab is hidden (HTTP/1.1 allows ~6 connections per origin across
 * all tabs, and the Kalshi stream already holds one).
 *
 * Reconnects with exponential backoff + jitter (1 s → 30 s), and asks for `?replay=500`
 * after the first connection, dropping replayed events it already delivered (ids ≤
 * the last one seen) — the same gap-fill scheme as the Kalshi client.
 */
import { useEffect, useRef, useSyncExternalStore } from "react";
import { IS_MOCK, parseJsonLenient } from "../client";
import { isObj, obj, str } from "../normalize";
import {
  CB_STREAM_URL,
  normCbAccountPartial,
  normCbBar,
  normCbFill,
  normCbLog,
  normCbOrder,
  normCbSignal,
  normCbTick,
} from "./client";
import {
  CB_STREAM_EVENT_TYPES,
  type CbAccount,
  type CbStreamEvent,
  type CbStreamEventType,
  type CbStreamState,
} from "./types";

export interface CbStreamHandlers {
  onOpen: () => void;
  onError: () => void;
  onMessage: (type: string, data: string, lastEventId?: string) => void;
}

export interface CbStreamSource {
  close: () => void;
}

export interface CbStreamInfo {
  state: CbStreamState;
  attempts: number;
  nextRetryAt: number | null;
}

const MAX_EVENTS = 300;
const MIN_DELAY_MS = 1000;
const MAX_DELAY_MS = 30_000;
const STABLE_MS = 15_000;
const REPLAY_MAX = 500;
/** Keep the connection this long after the last user leaves (page-to-page navigation). */
const RELEASE_GRACE_MS = 5000;

let seq = 0;

/** Parse one SSE message into a typed, normalized Coinbase event (null if unusable or another venue's). */
export function parseCbStreamMessage(type: string, data: string): CbStreamEvent | null {
  let payload: unknown;
  try {
    payload = parseJsonLenient(data);
  } catch {
    return null;
  }
  let t = type;
  if (t === "message" && isObj(payload) && typeof payload.type === "string") {
    t = payload.type;
    payload = payload.data ?? payload.payload ?? {};
  }
  const o = obj(payload);
  // Defensive: an event explicitly labeled with another venue never enters the Coinbase feed.
  const venue = str(o.venue);
  if (venue && venue !== "coinbase") return null;
  const base = { receivedAt: Date.now(), seq: ++seq };
  switch (t as CbStreamEventType) {
    case "tick":
      return { type: "tick", data: normCbTick(o), ...base };
    case "signal":
      return { type: "signal", data: normCbSignal(o), ...base };
    case "order":
      return { type: "order", data: normCbOrder(o), ...base };
    case "fill":
      return { type: "fill", data: normCbFill(o), ...base };
    case "log":
      return { type: "log", data: normCbLog(o), ...base };
    case "account":
      return { type: "account", data: normCbAccountPartial(o), ...base };
    case "bar":
      return { type: "bar", data: normCbBar(o), ...base };
    default:
      return null;
  }
}

function openNative(url: string, h: CbStreamHandlers): CbStreamSource {
  const es = new EventSource(url);
  es.onopen = () => h.onOpen();
  es.onerror = () => h.onError();
  es.onmessage = (ev: MessageEvent) => h.onMessage("message", String(ev.data), ev.lastEventId);
  for (const t of CB_STREAM_EVENT_TYPES) {
    es.addEventListener(t, (ev) => h.onMessage(t, String((ev as MessageEvent).data), (ev as MessageEvent).lastEventId));
  }
  return { close: () => es.close() };
}

async function openSource(url: string, h: CbStreamHandlers): Promise<CbStreamSource> {
  if (IS_MOCK) {
    const mock = await import("./mock");
    return mock.openCbMockStream(url, h);
  }
  return openNative(url, h);
}

type Listener = (e: CbStreamEvent) => void;

class CbStreamStore {
  private events: CbStreamEvent[] = [];
  private account: { data: Partial<CbAccount>; receivedAt: number } | null = null;
  private info: CbStreamInfo = { state: "closed", attempts: 0, nextRetryAt: null };
  private listeners = new Set<Listener>();
  private snapshotSubs = new Set<() => void>();
  private infoSubs = new Set<() => void>();
  private invalidateSubs = new Set<(types: ReadonlySet<string>) => void>();

  // connection
  private users = 0;
  private releaseTimer: ReturnType<typeof setTimeout> | undefined;
  private source: CbStreamSource | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | undefined;
  private stableTimer: ReturnType<typeof setTimeout> | undefined;
  private attempts = 0;
  private generation = 0;
  private openedAt: number | null = null;
  private lastSeenId: number | null = null;
  private listening = false;

  // ---- data --------------------------------------------------------------

  push = (e: CbStreamEvent) => {
    this.events = [e, ...this.events].slice(0, MAX_EVENTS);
    if (e.type === "account") this.account = { data: e.data, receivedAt: e.receivedAt };
    for (const l of this.listeners) l(e);
    for (const s of this.snapshotSubs) s();
  };

  /** Forget buffered events and the cached account (after a Coinbase account reset). */
  clear = () => {
    this.events = [];
    this.account = null;
    for (const s of this.snapshotSubs) s();
  };

  /** Make every Coinbase poll that refreshes on these types refetch now. */
  invalidate = (types: readonly CbStreamEventType[]) => {
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

  subscribeInfo = (cb: () => void) => {
    this.infoSubs.add(cb);
    return () => {
      this.infoSubs.delete(cb);
    };
  };

  getEvents = () => this.events;
  getAccount = () => this.account;
  getInfo = () => this.info;

  private setInfo(i: CbStreamInfo) {
    this.info = i;
    for (const s of this.infoSubs) s();
  }

  // ---- connection lifecycle ---------------------------------------------

  acquire = () => {
    this.users += 1;
    clearTimeout(this.releaseTimer);
    this.releaseTimer = undefined;
    if (!this.listening && typeof document !== "undefined") {
      this.listening = true;
      document.addEventListener("visibilitychange", this.onVisibility);
      window.addEventListener("online", this.onOnline);
    }
    if (this.source === null && this.retryTimer === undefined && !this.hidden()) this.connect();
    return () => this.release();
  };

  private release() {
    this.users = Math.max(0, this.users - 1);
    if (this.users > 0) return;
    clearTimeout(this.releaseTimer);
    this.releaseTimer = setTimeout(() => {
      this.releaseTimer = undefined;
      if (this.users > 0) return;
      this.close();
      this.attempts = 0;
      this.setInfo({ state: "closed", attempts: 0, nextRetryAt: null });
      if (this.listening) {
        this.listening = false;
        document.removeEventListener("visibilitychange", this.onVisibility);
        window.removeEventListener("online", this.onOnline);
      }
    }, RELEASE_GRACE_MS);
  }

  private hidden() {
    return typeof document !== "undefined" && document.visibilityState === "hidden";
  }

  private close() {
    this.generation++;
    clearTimeout(this.retryTimer);
    this.retryTimer = undefined;
    clearTimeout(this.stableTimer);
    this.stableTimer = undefined;
    this.source?.close();
    this.source = null;
    this.openedAt = null;
  }

  private connect() {
    if (this.users === 0) return;
    this.close();
    const gen = this.generation;
    this.setInfo({ state: this.attempts === 0 ? "connecting" : "reconnecting", attempts: this.attempts, nextRetryAt: null });
    const resumeFrom = this.lastSeenId;
    let inBacklog = resumeFrom !== null;
    let firstBacklog = true;
    let prevEventId = "";
    const live = () => gen === this.generation;
    openSource(resumeFrom !== null ? `${CB_STREAM_URL}?replay=${REPLAY_MAX}` : CB_STREAM_URL, {
      onOpen: () => {
        if (!live()) return;
        this.openedAt = Date.now();
        this.setInfo({ state: "open", attempts: 0, nextRetryAt: null });
        clearTimeout(this.stableTimer);
        this.stableTimer = setTimeout(() => {
          if (live()) this.attempts = 0;
        }, STABLE_MS);
      },
      onError: () => {
        if (!live()) return;
        if (this.openedAt !== null && Date.now() - this.openedAt >= STABLE_MS) this.attempts = 0;
        this.close();
        this.scheduleReconnect();
      },
      onMessage: (type, data, eventId) => {
        if (!live()) return;
        const own = !!eventId && eventId !== prevEventId;
        if (eventId !== undefined) prevEventId = eventId;
        const idNum = own ? Number(eventId) : Number.NaN;
        if (inBacklog) {
          if (!own || !Number.isFinite(idNum)) inBacklog = false;
          else if (firstBacklog && resumeFrom !== null && idNum + REPLAY_MAX < resumeFrom) inBacklog = false;
          else if (resumeFrom !== null && idNum <= resumeFrom) {
            firstBacklog = false;
            return;
          }
          firstBacklog = false;
        }
        if (Number.isFinite(idNum)) this.lastSeenId = idNum;
        const ev = parseCbStreamMessage(type, data);
        if (ev) this.push(ev);
      },
    }).then(
      (s) => {
        if (!live()) s.close();
        else this.source = s;
      },
      () => {
        if (live()) this.scheduleReconnect();
      },
    );
  }

  private scheduleReconnect() {
    if (this.users === 0 || this.retryTimer !== undefined) return;
    if (this.hidden()) {
      this.setInfo({ state: "closed", attempts: this.attempts, nextRetryAt: null });
      return;
    }
    this.attempts += 1;
    const base = Math.min(MAX_DELAY_MS, MIN_DELAY_MS * 2 ** (this.attempts - 1));
    const delay = Math.round(base * (0.8 + Math.random() * 0.4));
    this.setInfo({ state: "reconnecting", attempts: this.attempts, nextRetryAt: Date.now() + delay });
    this.retryTimer = setTimeout(() => {
      this.retryTimer = undefined;
      this.connect();
    }, delay);
  }

  private onVisibility = () => {
    if (this.users === 0) return;
    if (this.hidden()) {
      this.close();
      this.setInfo({ state: "closed", attempts: this.attempts, nextRetryAt: null });
    } else if (this.source === null) {
      this.attempts = Math.max(0, this.attempts - 1);
      this.connect();
    }
  };

  private onOnline = () => {
    if (this.users === 0 || this.hidden() || this.retryTimer === undefined) return;
    this.attempts = Math.max(0, this.attempts - 1);
    this.connect();
  };
}

export const cbStreamStore = new CbStreamStore();

// ---------------------------------------------------------------------------
// Hooks
// ---------------------------------------------------------------------------

/** Hold the shared Coinbase SSE connection open while the calling component is mounted. */
export function useCbStreamConnection(): void {
  useEffect(() => cbStreamStore.acquire(), []);
}

export function useCbStreamInfo(): CbStreamInfo {
  return useSyncExternalStore(cbStreamStore.subscribeInfo, cbStreamStore.getInfo, cbStreamStore.getInfo);
}

/** Recent Coinbase events, newest first. */
export function useCbStreamEvents(): CbStreamEvent[] {
  return useSyncExternalStore(cbStreamStore.subscribeSnapshot, cbStreamStore.getEvents, cbStreamStore.getEvents);
}

/** Latest `account` event (only the fields it carried). */
export function useCbStreamAccount(): { data: Partial<CbAccount>; receivedAt: number } | null {
  return useSyncExternalStore(cbStreamStore.subscribeSnapshot, cbStreamStore.getAccount, cbStreamStore.getAccount);
}

/** Call `cb` when a Coinbase event of one of `types` arrives, or cbStreamStore.invalidate() names one. */
export function useCbRefreshSignal(types: readonly CbStreamEventType[] | undefined, cb: () => void) {
  const cbRef = useRef(cb);
  cbRef.current = cb;
  const key = types ? types.join(",") : "";
  useEffect(() => {
    if (!key) return;
    const set = new Set(key.split(","));
    const offEvents = cbStreamStore.subscribeEvents((e) => {
      if (set.has(e.type)) cbRef.current();
    });
    const offLocal = cbStreamStore.subscribeInvalidate((ts) => {
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

/** Call `cb` for every Coinbase event whose type is in `types`. */
export function useCbStreamSubscription(types: readonly CbStreamEventType[] | undefined, cb: (e: CbStreamEvent) => void) {
  const cbRef = useRef(cb);
  cbRef.current = cb;
  const key = types ? types.join(",") : "";
  useEffect(() => {
    if (!key) return;
    const set = new Set(key.split(","));
    return cbStreamStore.subscribeEvents((e) => {
      if (set.has(e.type)) cbRef.current(e);
    });
  }, [key]);
}
