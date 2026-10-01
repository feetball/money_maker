/**
 * Typed client for every endpoint of ARCHITECTURE §12 plus the SSE hook.
 *
 * - Same-origin base `/api` (Vite proxies it to :8765 in dev; FastAPI serves it in prod).
 * - Errors are thrown as `ApiError` carrying the `{"detail": str}` message. `kind`
 *   separates "the backend could not be reached" (network / proxy 5xx without a
 *   body) from timeouts and real HTTP errors, so the UI can word each correctly.
 * - Every response goes through normalize.ts so pages can rely on the declared types.
 * - With VITE_MOCK=1 (`npm run dev:mock`) requests and the stream are answered by
 *   mock.ts, which is only loaded in that mode.
 */
import { useEffect, useRef, useState } from "react";
import {
  normAccount,
  normAccountPartial,
  normAnalytics,
  normBacktestCreate,
  normBacktestDetail,
  normEquity,
  normFill,
  normList,
  normLog,
  normOrder,
  normOverview,
  normRisk,
  normSettlement,
  normSignal,
  normStatus,
  normStrategy,
  normTick,
  isObj,
  obj,
  str,
  COMBINED_NOTE,
  VENUE_LABELS,
} from "./normalize";
import {
  STREAM_EVENT_TYPES,
  type Account,
  type AccountPatch,
  type AccountResetRequest,
  type AccountWithdrawProfitRequest,
  type AnalyticsResponse,
  type BacktestCreateRequest,
  type BacktestCreateResponse,
  type BacktestDetail,
  type BacktestSummary,
  type EquityPoint,
  type EquityRange,
  type Fill,
  type Id,
  type KillSwitchRequest,
  type LogEntry,
  type MarketRow,
  type MarketsQuery,
  type Order,
  type OrderStatusFilter,
  type OverviewResponse,
  type Position,
  type RiskPatch,
  type RiskResponse,
  type Settlement,
  type Signal,
  type StatusResponse,
  type Strategy,
  type StrategyPatch,
  type StreamConnectionState,
  type StreamEvent,
  type StreamEventType,
} from "./types";

export const API_BASE = "/api";
export const STREAM_URL = `${API_BASE}/stream`;
export const IS_MOCK = import.meta.env.VITE_MOCK === "1";

const REQUEST_TIMEOUT_MS = 20_000;

/**
 * - "network": no usable answer from the backend (fetch rejected, connection lost
 *   mid-body, or a dev/preview proxy 5xx with no body). `status` is 0.
 * - "timeout": no complete answer within REQUEST_TIMEOUT_MS. `status` is 0.
 * - "http": the backend answered with a 4xx/5xx (or a non-JSON body). `status` is set.
 */
export type ApiErrorKind = "network" | "timeout" | "http";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly path: string;
  readonly kind: ApiErrorKind;
  constructor(status: number, detail: string, path: string, kind: ApiErrorKind = status === 0 ? "network" : "http") {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.path = path;
    this.kind = kind;
  }
}

export function isAbortError(e: unknown): boolean {
  return e instanceof DOMException && e.name === "AbortError";
}

/** The backend could not be reached at all (not an HTTP error it answered with). */
export function isUnreachable(e: unknown): boolean {
  return e instanceof ApiError && e.kind === "network";
}

/** Human-readable message for any thrown value. */
export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.status ? `${e.detail} (HTTP ${e.status})` : e.detail;
  if (e instanceof Error) return e.message;
  return String(e);
}

/** Replace bare NaN / Infinity / -Infinity tokens (outside strings) with null. */
function nullNonFinite(text: string): string {
  let out = "";
  let inStr = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]!;
    if (inStr) {
      out += ch;
      if (ch === "\\") {
        out += text[i + 1] ?? "";
        i++;
      } else if (ch === '"') inStr = false;
      continue;
    }
    if (ch === '"') {
      inStr = true;
      out += ch;
      continue;
    }
    const m = /^-?(?:NaN|Infinity)/.exec(text.slice(i, i + 9));
    if (m) {
      out += "null";
      i += m[0].length - 1;
      continue;
    }
    out += ch;
  }
  return out;
}

/**
 * JSON.parse that also accepts Python's default `json.dumps` output for non-finite
 * floats (bare NaN / Infinity), turning them into null so normalize renders "—".
 */
export function parseJsonLenient(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch (e) {
    if (!/NaN|Infinity/.test(text)) throw e;
    return JSON.parse(nullNonFinite(text));
  }
}

/** `{"detail": str}`; also FastAPI 422 `{"detail": [{loc, msg}]}` and plain text bodies. */
function detailOf(payload: unknown, fallback: string): string {
  if (isObj(payload)) {
    const d = payload.detail;
    if (typeof d === "string" && d) return d;
    if (Array.isArray(d)) {
      const parts = d.map((x) => {
        const o = obj(x);
        const loc = Array.isArray(o.loc) ? o.loc.filter((p) => p !== "body").join(".") : "";
        return loc ? `${loc}: ${str(o.msg)}` : str(o.msg, JSON.stringify(x));
      });
      if (parts.length) return parts.join("; ");
    }
  }
  if (typeof payload === "string" && payload.trim()) return payload.trim().slice(0, 240);
  return fallback;
}

const UNREACHABLE = "Cannot reach the backend (is `kalshibot serve` running on :8765?)";

/**
 * Gateway-style failures that mean "nothing answered": 502/503/504 without a JSON
 * body, and the empty 500 that Vite's dev/preview proxy returns on ECONNREFUSED.
 */
function looksUnreachable(res: Response, text: string): boolean {
  const ctype = (res.headers.get("content-type") ?? "").toLowerCase();
  if (ctype.includes("application/json")) return false;
  if (res.status === 502 || res.status === 503 || res.status === 504) return true;
  return res.status === 500 && text.trim() === "" && (ctype === "" || ctype.startsWith("text/plain"));
}

export type Method = "GET" | "POST" | "PATCH" | "DELETE";

/**
 * Raw JSON request against `/api` + `path` (path starts with "/", e.g. "/coinbase/status").
 * Shared by every venue client: same timeout, error kinds and lenient JSON parsing. In
 * mock mode it is answered by mock.ts (which forwards "/coinbase/*" to the Coinbase
 * mock when that module exists).
 */
export async function apiRequest(method: Method, path: string, body?: unknown, signal?: AbortSignal): Promise<unknown> {
  return request(method, path, body, signal);
}

async function request(method: Method, path: string, body?: unknown, signal?: AbortSignal): Promise<unknown> {
  if (IS_MOCK) {
    const mock = await import("./mock");
    const res = await mock.mockRequest(method, path, body);
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    if (res.status >= 400) throw new ApiError(res.status, detailOf(res.body, `HTTP ${res.status}`), path);
    return res.body;
  }

  const ctrl = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    ctrl.abort();
  }, REQUEST_TIMEOUT_MS);
  const onAbort = () => ctrl.abort();
  signal?.addEventListener("abort", onAbort, { once: true });
  const timeoutError = () => new ApiError(0, `Request timed out: ${method} ${API_BASE}${path}`, path, "timeout");

  let res: Response;
  let text = "";
  try {
    try {
      res = await fetch(API_BASE + path, {
        method,
        headers:
          body === undefined
            ? { Accept: "application/json" }
            : { Accept: "application/json", "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: ctrl.signal,
        cache: "no-store",
      });
    } catch (e) {
      if (timedOut) throw timeoutError();
      if (signal?.aborted || isAbortError(e)) throw new DOMException("Aborted", "AbortError");
      throw new ApiError(0, UNREACHABLE, path, "network");
    }
    // The timeout stays armed while the body streams in; a failure here must still
    // surface as an ApiError (a raw AbortError would look like a deliberate cancel).
    try {
      text = await res.text();
    } catch {
      if (timedOut) throw timeoutError();
      if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
      throw new ApiError(0, `Connection lost while reading ${method} ${API_BASE}${path}`, path, "network");
    }
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onAbort);
  }

  if (!res.ok && looksUnreachable(res, text)) {
    throw new ApiError(0, `${UNREACHABLE} — the proxy answered HTTP ${res.status}`, path, "network");
  }
  let payload: unknown = null;
  const ctype = res.headers.get("content-type") ?? "";
  if (text) {
    try {
      payload = parseJsonLenient(text);
    } catch {
      payload = text;
    }
  }
  if (!res.ok) throw new ApiError(res.status, detailOf(payload, `${res.status} ${res.statusText}`), path);
  if (typeof payload === "string" && (ctype.includes("text/html") || payload.trimStart().startsWith("<"))) {
    // Typically the SPA fallback answering an /api route that does not exist yet.
    throw new ApiError(res.status, `Expected JSON from ${API_BASE}${path} but received HTML`, path);
  }
  return payload;
}

/** `?a=1&b=x` from the defined, non-empty params ("" when none). */
export function qs(params: Record<string, string | number | undefined | null>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    u.set(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : "";
}

const enc = (v: Id | string) => encodeURIComponent(String(v));

export interface ReqOpts {
  signal?: AbortSignal;
}

/** One typed wrapper per §12 endpoint. */
export const api = {
  // --- engine / status ---
  status: (o?: ReqOpts): Promise<StatusResponse> =>
    request("GET", "/status", undefined, o?.signal).then(normStatus),
  startEngine: (): Promise<StatusResponse> => request("POST", "/engine/start", {}).then(normStatus),
  stopEngine: (): Promise<StatusResponse> => request("POST", "/engine/stop", {}).then(normStatus),
  setKillSwitch: (on: boolean): Promise<StatusResponse> => {
    const body: KillSwitchRequest = { on };
    return request("POST", "/engine/kill-switch", body).then(normStatus);
  },

  // --- account ---
  account: (o?: ReqOpts): Promise<Account> => request("GET", "/account", undefined, o?.signal).then(normAccount),
  resetAccount: (startingBalance?: number): Promise<Account> => {
    const body: AccountResetRequest = startingBalance === undefined ? {} : { starting_balance: startingBalance };
    return request("POST", "/account/reset", body).then(normAccount);
  },
  patchAccount: (patch: AccountPatch): Promise<Account> => request("PATCH", "/account", patch).then(normAccount),
  withdrawProfit: (body: AccountWithdrawProfitRequest = {}): Promise<Account> =>
    request("POST", "/account/withdraw-profit", body).then(normAccount),
  equity: (range: EquityRange, o?: ReqOpts): Promise<EquityPoint[]> =>
    request("GET", `/equity${qs({ range })}`, undefined, o?.signal).then(normEquity),

  // --- portfolio ---
  positions: (o?: ReqOpts): Promise<Position[]> =>
    request("GET", "/positions", undefined, o?.signal).then(normList.positions),
  orders: (p: { status?: OrderStatusFilter; limit?: number } = {}, o?: ReqOpts): Promise<Order[]> =>
    request("GET", `/orders${qs({ status: p.status ?? "open", limit: p.limit ?? 200 })}`, undefined, o?.signal).then(
      normList.orders,
    ),
  cancelOrder: (id: Id): Promise<Order> =>
    request("POST", `/orders/${enc(id)}/cancel`, {}).then((v) => normOrder(obj(v))),
  fills: (limit = 200, o?: ReqOpts): Promise<Fill[]> =>
    request("GET", `/fills${qs({ limit })}`, undefined, o?.signal).then(normList.fills),
  settlements: (limit = 200, o?: ReqOpts): Promise<Settlement[]> =>
    request("GET", `/settlements${qs({ limit })}`, undefined, o?.signal).then(normList.settlements),

  // --- strategies ---
  strategies: (o?: ReqOpts): Promise<Strategy[]> =>
    request("GET", "/strategies", undefined, o?.signal).then(normList.strategies),
  patchStrategy: (name: string, patch: StrategyPatch): Promise<Strategy> =>
    request("PATCH", `/strategies/${enc(name)}`, patch).then((v) => normStrategy(obj(v))),

  // --- risk ---
  risk: (o?: ReqOpts): Promise<RiskResponse> => request("GET", "/risk", undefined, o?.signal).then(normRisk),
  patchRisk: (patch: RiskPatch): Promise<RiskResponse> => request("PATCH", "/risk", patch).then(normRisk),

  // --- feeds ---
  signals: (limit = 200, o?: ReqOpts): Promise<Signal[]> =>
    request("GET", `/signals${qs({ limit })}`, undefined, o?.signal).then(normList.signals),
  logs: (limit = 200, o?: ReqOpts): Promise<LogEntry[]> =>
    request("GET", `/logs${qs({ limit })}`, undefined, o?.signal).then(normList.logs),
  markets: (q: MarketsQuery = {}, o?: ReqOpts): Promise<MarketRow[]> =>
    request(
      "GET",
      `/markets${qs({ search: q.search?.trim(), category: q.category, sort: q.sort, limit: q.limit ?? 100 })}`,
      undefined,
      o?.signal,
    ).then(normList.markets),

  // --- analytics / backtests ---
  analytics: (o?: ReqOpts): Promise<AnalyticsResponse> =>
    request("GET", "/analytics", undefined, o?.signal).then(normAnalytics),
  backtests: (o?: ReqOpts): Promise<BacktestSummary[]> =>
    request("GET", "/backtests", undefined, o?.signal).then(normList.backtests),
  createBacktest: (req: BacktestCreateRequest): Promise<BacktestCreateResponse> =>
    request("POST", "/backtests", req).then(normBacktestCreate),
  backtest: (id: Id, o?: ReqOpts): Promise<BacktestDetail> =>
    request("GET", `/backtests/${enc(id)}`, undefined, o?.signal).then(normBacktestDetail),

  // --- both venues (COINBASE_CONTRACT §13) ---
  /**
   * GET /api/overview. An older backend without the endpoint (404, or the SPA fallback
   * answering with HTML) gets a Kalshi-only overview assembled from /status, /account
   * and /equity, with Coinbase marked unavailable, so the shell keeps working.
   */
  overview: async (o?: ReqOpts): Promise<OverviewResponse> => {
    // An older server is re-probed once a minute, not on every 5 s poll.
    if (Date.now() < overviewMissingUntil) return synthesizeOverview(o?.signal);
    try {
      const r = normOverview(await request("GET", "/overview", undefined, o?.signal));
      overviewMissingUntil = 0;
      return r;
    } catch (e) {
      if (!(e instanceof ApiError) || e.kind !== "http" || !(e.status === 404 || /received HTML/.test(e.detail))) throw e;
      overviewMissingUntil = Date.now() + 60_000;
      return synthesizeOverview(o?.signal);
    }
  },
};

let overviewMissingUntil = 0;

/** Equity history for the synthesized overview, refetched at most once a minute. */
let fallbackEquity: { at: number; points: EquityPoint[] } | null = null;

async function synthesizeOverview(signal?: AbortSignal): Promise<OverviewResponse> {
  const cached = fallbackEquity && Date.now() - fallbackEquity.at < 60_000 ? fallbackEquity.points : null;
  const [status, account, equity] = await Promise.all([
    api.status({ signal }),
    api.account({ signal }),
    cached ??
      api.equity("30d", { signal }).then(
        (points) => {
          fallbackEquity = { at: Date.now(), points };
          return points;
        },
        () => [] as EquityPoint[],
      ),
  ]);
  const unavailable = "The running server has no Coinbase backend (GET /api/overview is missing), so restarting it will not help. Rebuild and redeploy once the Coinbase backend is in the code (Docker: ./deploy.sh update).";
  return {
    generated_at: status.server_time,
    synthesized: true,
    venues: {
      kalshi: {
        venue: "kalshi",
        label: VENUE_LABELS.kalshi,
        available: true,
        unavailable_reason: null,
        engine_running: status.engine.running,
        kill_switch: status.engine.kill_switch,
        starting_balance: account.starting_balance,
        equity: account.equity,
        cash: account.cash,
        total_pnl: account.total_pnl,
        total_return_pct: account.total_return_pct,
        todays_pnl: account.todays_pnl,
        open_positions: account.open_positions,
        fees_paid: account.fees_paid,
        last_error: status.engine.last_error,
        last_error_at: status.engine.last_error_at,
        last_tick_at: status.engine.last_tick_at,
      },
      coinbase: {
        venue: "coinbase",
        label: VENUE_LABELS.coinbase,
        available: false,
        unavailable_reason: unavailable,
        engine_running: false,
        kill_switch: false,
        starting_balance: null,
        equity: null,
        cash: null,
        total_pnl: null,
        total_return_pct: null,
        todays_pnl: null,
        open_positions: null,
        fees_paid: null,
        last_error: null,
        last_error_at: null,
        last_tick_at: null,
      },
    },
    combined: {
      starting_balance: account.starting_balance,
      equity: account.equity,
      total_pnl: account.total_pnl,
      total_return_pct: account.total_return_pct,
      note: COMBINED_NOTE,
    },
    equity_series: { kalshi: equity.map((p) => ({ ts: p.ts, equity: p.equity })), coinbase: [] },
  };
}

// ---------------------------------------------------------------------------
// Server-Sent Events
// ---------------------------------------------------------------------------

export interface StreamHandlers {
  onOpen: () => void;
  onError: () => void;
  /**
   * `type` is the SSE event name ("message" for unnamed events). `lastEventId` is the
   * EventSource's last event id (it carries over to events that have no `id:` line).
   */
  onMessage: (type: string, data: string, lastEventId?: string) => void;
}

export interface StreamSource {
  close: () => void;
}

function openNativeStream(url: string, h: StreamHandlers): StreamSource {
  const es = new EventSource(url);
  es.onopen = () => h.onOpen();
  es.onerror = () => h.onError();
  es.onmessage = (ev: MessageEvent) => h.onMessage("message", String(ev.data), ev.lastEventId);
  for (const t of STREAM_EVENT_TYPES) {
    es.addEventListener(t, (ev) => h.onMessage(t, String((ev as MessageEvent).data), (ev as MessageEvent).lastEventId));
  }
  return { close: () => es.close() };
}

async function openStream(url: string, h: StreamHandlers): Promise<StreamSource> {
  if (IS_MOCK) {
    const mock = await import("./mock");
    return mock.openMockStream(url, h);
  }
  return openNativeStream(url, h);
}

let seqCounter = 0;

/** Parse one SSE message into a typed, normalized event (null if unusable). */
export function parseStreamMessage(type: string, data: string): StreamEvent | null {
  let payload: unknown;
  try {
    payload = parseJsonLenient(data);
  } catch {
    return null;
  }
  let t = type;
  // Unnamed events may carry an envelope: {"type": "fill", "data": {...}}.
  if (t === "message" && isObj(payload) && typeof payload.type === "string") {
    t = payload.type;
    payload = payload.data ?? payload.payload ?? {};
  }
  const base = { receivedAt: Date.now(), seq: ++seqCounter };
  const o = obj(payload);
  switch (t as StreamEventType) {
    case "tick":
      return { type: "tick", data: normTick(payload), ...base };
    case "signal":
      return { type: "signal", data: normSignal(o), ...base };
    case "order":
      return { type: "order", data: normOrder(o), ...base };
    case "fill":
      return { type: "fill", data: normFill(o), ...base };
    case "settlement":
      return { type: "settlement", data: normSettlement(o), ...base };
    case "log":
      return { type: "log", data: normLog(o), ...base };
    case "account":
      return { type: "account", data: normAccountPartial(o), ...base };
    default:
      return null;
  }
}

export interface EventStreamInfo {
  state: StreamConnectionState;
  /** Consecutive failed attempts (0 when connected). */
  attempts: number;
  /** ms epoch of the next reconnect attempt, when reconnecting. */
  nextRetryAt: number | null;
}

const MIN_DELAY_MS = 1000;
const MAX_DELAY_MS = 30_000;
/** A connection counts as healthy (backoff reset) only after staying open this long. */
const STABLE_MS = 15_000;
/** Buffered events requested on a reconnect (the backend's `replay` maximum). */
const REPLAY_MAX = 500;

/**
 * Subscribe to GET /api/stream.
 *
 * - Reconnects with exponential backoff + jitter (1 s → 30 s) on any error; the
 *   native EventSource retry is replaced so a 5xx/closed stream also backs off.
 * - The backoff only resets once a connection has stayed open for STABLE_MS. Not on
 *   the first message: the backend always sends an `account` event right after
 *   connecting, so a server that accepts, greets and closes (e.g. its bus shutting
 *   down) would otherwise be hit about once per second forever.
 * - Gap fill: every reconnect after the first connection asks for `?replay=500` and
 *   drops the replayed events it already delivered (SSE ids ≤ the last one seen), so
 *   events that happened while disconnected or hidden still reach the feed. A fresh
 *   EventSource cannot send Last-Event-ID, hence the query parameter.
 * - The stream is closed while the tab is hidden and reopened when it is visible
 *   again. uvicorn and the Vite proxy speak HTTP/1.1, where browsers allow only ~6
 *   connections per origin across ALL tabs; one idle SSE per background tab would
 *   otherwise starve REST requests. Polls refetch on visibility, so nothing is lost.
 */
export function useEventStream(onEvent: (e: StreamEvent) => void): EventStreamInfo {
  const handler = useRef(onEvent);
  handler.current = onEvent;
  const [info, setInfo] = useState<EventStreamInfo>({ state: "connecting", attempts: 0, nextRetryAt: null });

  useEffect(() => {
    let disposed = false;
    let source: StreamSource | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    let generation = 0;
    let openedAt: number | null = null;
    let stableTimer: ReturnType<typeof setTimeout> | undefined;
    /** SSE id of the last event delivered (across reconnects); null = none seen yet. */
    let lastSeenId: number | null = null;

    const hidden = () => document.visibilityState === "hidden";

    const close = () => {
      generation++;
      clearTimeout(timer);
      timer = undefined;
      clearTimeout(stableTimer);
      stableTimer = undefined;
      source?.close();
      source = null;
      openedAt = null;
    };

    const connect = () => {
      if (disposed) return;
      close();
      const gen = generation;
      setInfo({ state: attempts === 0 ? "connecting" : "reconnecting", attempts, nextRetryAt: null });
      const resumeFrom = lastSeenId;
      // Backlog phase: replayed events come first (each with its own id), then the
      // id-less `account` greeting, then live events. (On a plain first connection the
      // greeting carries the latest event id instead, which becomes the resume point.)
      let inBacklog = resumeFrom !== null;
      let firstBacklog = true;
      let prevEventId = "";
      openStream(resumeFrom !== null ? `${STREAM_URL}?replay=${REPLAY_MAX}` : STREAM_URL, {
        onOpen: () => {
          if (disposed || gen !== generation) return;
          openedAt = Date.now();
          setInfo({ state: "open", attempts: 0, nextRetryAt: null });
          clearTimeout(stableTimer);
          stableTimer = setTimeout(() => {
            if (!disposed && gen === generation) attempts = 0;
          }, STABLE_MS);
        },
        onError: () => {
          if (disposed || gen !== generation) return;
          if (openedAt !== null && Date.now() - openedAt >= STABLE_MS) attempts = 0;
          close();
          scheduleReconnect();
        },
        onMessage: (type, data, eventId) => {
          if (disposed || gen !== generation) return;
          // lastEventId carries over to events without an `id:` line, so an event has
          // its OWN id only when the value changed.
          const own = !!eventId && eventId !== prevEventId;
          if (eventId !== undefined) prevEventId = eventId;
          const idNum = own ? Number(eventId) : Number.NaN;
          if (inBacklog) {
            if (!own || !Number.isFinite(idNum)) inBacklog = false;
            else if (firstBacklog && resumeFrom !== null && idNum + REPLAY_MAX < resumeFrom) {
              // Ids far below the last one seen: the server restarted and its sequence
              // began again, so nothing in this backlog was delivered before.
              inBacklog = false;
            } else if (resumeFrom !== null && idNum <= resumeFrom) {
              firstBacklog = false;
              return; // delivered before the connection dropped
            }
            firstBacklog = false;
          }
          if (Number.isFinite(idNum)) lastSeenId = idNum;
          const ev = parseStreamMessage(type, data);
          if (ev) handler.current(ev);
        },
      }).then(
        (s) => {
          if (disposed || gen !== generation) s.close();
          else source = s;
        },
        () => {
          if (!disposed && gen === generation) scheduleReconnect();
        },
      );
    };

    const scheduleReconnect = () => {
      if (disposed || timer !== undefined) return;
      if (hidden()) {
        setInfo({ state: "closed", attempts, nextRetryAt: null });
        return;
      }
      attempts += 1;
      const base = Math.min(MAX_DELAY_MS, MIN_DELAY_MS * 2 ** (attempts - 1));
      const delay = Math.round(base * (0.8 + Math.random() * 0.4));
      setInfo({ state: "reconnecting", attempts, nextRetryAt: Date.now() + delay });
      timer = setTimeout(() => {
        timer = undefined;
        connect();
      }, delay);
    };

    const onVisibility = () => {
      if (disposed) return;
      if (hidden()) {
        close();
        setInfo({ state: "closed", attempts, nextRetryAt: null });
      } else if (source === null) {
        // Visible again: reconnect now (one step less backoff if we were failing).
        attempts = Math.max(0, attempts - 1);
        connect();
      }
    };

    const onOnline = () => {
      if (disposed || hidden() || timer === undefined) return;
      attempts = Math.max(0, attempts - 1);
      connect();
    };

    if (hidden()) setInfo({ state: "closed", attempts: 0, nextRetryAt: null });
    else connect();
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("online", onOnline);
    return () => {
      disposed = true;
      close();
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("online", onOnline);
    };
  }, []);

  return info;
}
