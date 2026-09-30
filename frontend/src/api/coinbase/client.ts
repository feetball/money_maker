/**
 * Typed client for the Coinbase PAPER venue (docs/COINBASE_CONTRACT.md §13):
 * every `/api/coinbase/*` endpoint, normalized into ./types.ts.
 *
 * - Its own small fetch wrapper (timeouts, abort, lenient JSON) but the SAME ApiError
 *   class as the Kalshi client, so shared UI (ErrorBlock, toasts, errorMessage) reads
 *   both venues' failures identically.
 * - A Coinbase outage never looks like a Kalshi outage: a 503 with a JSON
 *   `{"detail": "coinbase venue unavailable: …"}` body is an HTTP error ("venue
 *   unavailable"), not "backend unreachable". `isCbUnavailable()` detects it.
 * - VITE_MOCK=1 answers from ./mock.ts (loaded only in that mode).
 */
import { ApiError, IS_MOCK, parseJsonLenient } from "../client";
import { arr, bool, calDate, id, isObj, normStrategy, num, numOrNull, obj, pick, str, strOrNull, ts, tsReq, type Raw } from "../normalize";
import type {
  CbAccount,
  CbAnalytics,
  CbAnalyticsStats,
  CbBacktestCreateRequest,
  CbBacktestCreateResponse,
  CbBacktestDetail,
  CbBacktestMetrics,
  CbBacktestSummary,
  CbBacktestTrade,
  CbBarEvent,
  CbCurvePoint,
  CbDecision,
  CbEngineStatus,
  CbEquityPoint,
  CbEquityRange,
  CbExposureRow,
  CbFeeTier,
  CbFill,
  CbLog,
  CbOrder,
  CbOrderStatus,
  CbOrderStatusFilter,
  CbPeriodReturn,
  CbPosition,
  CbProductRow,
  CbProductsQuery,
  CbRisk,
  CbRiskLimits,
  CbRiskPatch,
  CbSide,
  CbSignal,
  CbStatus,
  CbStrategy,
  CbStrategyPatch,
  CbTickEvent,
  Id,
} from "./types";

export const CB_API_BASE = "/api/coinbase";
export const CB_STREAM_URL = `${CB_API_BASE}/stream`;
export { IS_MOCK };

const REQUEST_TIMEOUT_MS = 20_000;

/**
 * True when the running server has no Coinbase backend at all (an older build): its
 * `/api/{rest}` catch-all answers 404 "Not Found: /api/coinbase/…", or an SPA fallback
 * answers with HTML. Restarting such a server does not help; it must be rebuilt.
 */
export function isCbMissing(e: unknown): boolean {
  if (!(e instanceof ApiError) || e.kind !== "http") return false;
  if (e.status === 404 && e.detail.startsWith("Not Found: /api/coinbase/")) return true;
  return /received HTML \(is the Coinbase API mounted\?\)/.test(e.detail);
}

/** Why the Coinbase venue is missing from this server, shown instead of the raw 404. */
export const CB_MISSING_REASON =
  "The running server has no Coinbase backend (its /api/coinbase/* routes do not exist), so restarting or rebooting it will not help: the build it runs predates the Coinbase code. Rebuild and redeploy once the Coinbase backend is in the code (Docker: ./deploy.sh update; the image bakes the code in).";

/**
 * True when the Coinbase venue itself is down/disabled (503 + JSON detail), or absent
 * from this server build (see isCbMissing). Either way one venue-level banner explains it.
 */
export function isCbUnavailable(e: unknown): boolean {
  return (e instanceof ApiError && e.kind === "http" && e.status === 503) || isCbMissing(e);
}

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

const UNREACHABLE = "Cannot reach the backend (is `kalshibot serve` running?)";

type Method = "GET" | "POST" | "PATCH";

async function request(method: Method, path: string, body?: unknown, signal?: AbortSignal): Promise<unknown> {
  if (IS_MOCK) {
    const mock = await import("./mock");
    const res = await mock.cbMockRequest(method, path, body);
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    if (res.status >= 400) throw new ApiError(res.status, detailOf(res.body, `HTTP ${res.status}`), CB_API_BASE + path);
    return res.body;
  }
  const url = CB_API_BASE + path;
  const ctrl = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    ctrl.abort();
  }, REQUEST_TIMEOUT_MS);
  const onAbort = () => ctrl.abort();
  signal?.addEventListener("abort", onAbort, { once: true });
  let res: Response;
  let text = "";
  try {
    try {
      res = await fetch(url, {
        method,
        headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: ctrl.signal,
        cache: "no-store",
      });
    } catch (e) {
      if (timedOut) throw new ApiError(0, `Request timed out: ${method} ${url}`, url, "timeout");
      if (signal?.aborted || (e instanceof DOMException && e.name === "AbortError")) throw new DOMException("Aborted", "AbortError");
      throw new ApiError(0, UNREACHABLE, url, "network");
    }
    try {
      text = await res.text();
    } catch {
      if (timedOut) throw new ApiError(0, `Request timed out: ${method} ${url}`, url, "timeout");
      if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
      throw new ApiError(0, `Connection lost while reading ${method} ${url}`, url, "network");
    }
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onAbort);
  }
  const ctype = (res.headers.get("content-type") ?? "").toLowerCase();
  const json = ctype.includes("application/json");
  if (!res.ok && !json) {
    // Gateway failures without a JSON body: nothing (or only a proxy) answered.
    if (res.status === 502 || res.status === 504 || (res.status === 503 && !text.includes("coinbase")) || (res.status === 500 && text.trim() === "")) {
      throw new ApiError(0, `${UNREACHABLE} — the proxy answered HTTP ${res.status}`, url, "network");
    }
  }
  let payload: unknown = null;
  if (text) {
    try {
      payload = parseJsonLenient(text);
    } catch {
      payload = text;
    }
  }
  if (!res.ok) throw new ApiError(res.status, detailOf(payload, `${res.status} ${res.statusText}`), url);
  if (typeof payload === "string" && (ctype.includes("text/html") || payload.trimStart().startsWith("<"))) {
    throw new ApiError(res.status, `Expected JSON from ${url} but received HTML (is the Coinbase API mounted?)`, url);
  }
  return payload;
}

function qs(params: Record<string, string | number | undefined | null>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    u.set(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : "";
}

const enc = (v: Id | string) => encodeURIComponent(String(v));

// ---------------------------------------------------------------------------
// Normalizers
// ---------------------------------------------------------------------------

function list<T>(v: unknown, f: (r: Raw) => T): T[] {
  // Tolerate {items: [...]} / {rows: [...]} envelopes.
  const a = Array.isArray(v) ? v : isObj(v) ? arr(pick(v, "items", "rows", "data", "results")) : [];
  return a.filter(isObj).map(f);
}

function fraction(v: unknown): number | null {
  const n = numOrNull(v);
  if (n === null) return null;
  return n > 1.0001 && n <= 100 ? n / 100 : n;
}

function cbSide(v: unknown): CbSide | null {
  const s = str(v).toLowerCase();
  return s === "buy" || s === "sell" ? s : null;
}

const ORDER_STATUSES: readonly CbOrderStatus[] = ["open", "partially_filled", "filled", "cancelled", "expired", "rejected"];
function orderStatus(v: unknown): CbOrderStatus {
  let s = str(v).toLowerCase().replace("canceled", "cancelled").replace(/\s+/g, "_");
  if (s === "pending" || s === "resting" || s === "active") s = "open";
  if (s === "partial" || s === "partially-filled") s = "partially_filled";
  if (s === "done") s = "filled";
  return (ORDER_STATUSES as readonly string[]).includes(s) ? (s as CbOrderStatus) : "unknown";
}

const DECISIONS: readonly CbDecision[] = ["executed", "partial", "rejected", "unfilled", "resting"];
function decision(v: unknown): CbDecision {
  const s = str(v).toLowerCase();
  return (DECISIONS as readonly string[]).includes(s) ? (s as CbDecision) : "unknown";
}

function feeTier(v: unknown): CbFeeTier {
  const o = obj(v);
  const name = str(pick(o, "name", "tier"), "—");
  return {
    name,
    label: str(o.label, name),
    maker_rate: num(pick(o, "maker_rate", "maker")),
    taker_rate: num(pick(o, "taker_rate", "taker")),
  };
}

export function normCbStatus(v: unknown): CbStatus {
  const o = obj(v);
  const e = obj(o.engine);
  const engine: CbEngineStatus = {
    running: bool(e.running),
    started_at: ts(e.started_at),
    last_tick_at: ts(e.last_tick_at),
    last_bar_at: ts(e.last_bar_at),
    tick_count: num(e.tick_count),
    products_loaded: num(pick(e, "products_loaded", "universe_size")),
    last_error: strOrNull(e.last_error),
    last_error_at: ts(e.last_error_at),
    kill_switch: bool(e.kill_switch),
    kill_switch_reason: strOrNull(e.kill_switch_reason),
    coinbase_reachable: e.coinbase_reachable === null || e.coinbase_reachable === undefined ? null : bool(e.coinbase_reachable),
    strategies_enabled: arr(e.strategies_enabled).map((x) => str(x)).filter(Boolean),
  };
  const tiers = pick(o, "fee_tiers", "available_fee_tiers");
  const tierList = Array.isArray(tiers)
    ? tiers.map(feeTier)
    : isObj(tiers)
      ? Object.entries(tiers).map(([name, t]) => ({ ...feeTier(t), name }))
      : [];
  return {
    venue: "coinbase",
    mode: "paper",
    engine,
    fee_tier: feeTier(o.fee_tier),
    fee_tiers: tierList,
    server_time: tsReq(o.server_time),
  };
}

const ACCOUNT_KEYS: (keyof CbAccount)[] = [
  "starting_balance",
  "cash",
  "reserved_cash",
  "positions_liquidation_value",
  "positions_mid_value",
  "equity",
  "equity_mid",
  "realized_pnl",
  "unrealized_pnl",
  "fees_paid",
  "total_pnl",
  "total_return_pct",
  "todays_pnl",
  "max_drawdown_pct",
  "open_positions",
  "open_orders",
  "trades",
];

export function normCbAccount(v: unknown): CbAccount {
  const o = obj(v);
  const equity = num(o.equity);
  const start = num(o.starting_balance);
  return {
    venue: "coinbase",
    starting_balance: start,
    cash: num(o.cash),
    reserved_cash: num(o.reserved_cash),
    positions_liquidation_value: num(pick(o, "positions_liquidation_value", "positions_value")),
    positions_mid_value: num(o.positions_mid_value),
    equity,
    equity_mid: num(o.equity_mid, equity),
    realized_pnl: num(o.realized_pnl),
    unrealized_pnl: num(o.unrealized_pnl),
    fees_paid: num(pick(o, "fees_paid", "fees")),
    total_pnl: num(o.total_pnl, equity - start),
    total_return_pct: num(o.total_return_pct),
    todays_pnl: num(o.todays_pnl),
    max_drawdown_pct: Math.abs(num(o.max_drawdown_pct)),
    open_positions: num(o.open_positions),
    open_orders: num(o.open_orders),
    trades: num(pick(o, "trades", "trade_count", "fills")),
    win_rate: fraction(o.win_rate),
    ts: ts(o.ts),
  };
}

/** Only the finite fields an SSE `account` event carried. */
export function normCbAccountPartial(v: unknown): Partial<CbAccount> {
  const o = obj(v);
  const out: Partial<CbAccount> = {};
  for (const k of ACCOUNT_KEYS) {
    const n = numOrNull(o[k]);
    if (n !== null) (out as Record<string, number>)[k] = k === "max_drawdown_pct" ? Math.abs(n) : n;
  }
  if (o.win_rate !== undefined) out.win_rate = fraction(o.win_rate);
  const t = ts(o.ts);
  if (t) out.ts = t;
  return out;
}

/** A partial SSE account is usable on its own only when it carries every field. */
export function completeCbAccount(p: Partial<CbAccount>): CbAccount | null {
  return ACCOUNT_KEYS.every((k) => typeof p[k] === "number") ? normCbAccount(p) : null;
}

export function normCbEquity(v: unknown): CbEquityPoint[] {
  return list(v, (o) => ({
    ts: tsReq(pick(o, "ts", "time", "t")),
    equity: num(o.equity),
    equity_mid: numOrNull(o.equity_mid),
    cash: numOrNull(o.cash),
    realized_pnl: numOrNull(o.realized_pnl),
    unrealized_pnl: numOrNull(o.unrealized_pnl),
  })).filter((p) => p.ts !== "");
}

const baseOf = (pid: string) => pid.split("-")[0] ?? pid;

export function coinbaseProductUrl(pid: string): string {
  return `https://www.coinbase.com/advanced-trade/spot/${encodeURIComponent(pid)}`;
}

function urlOr(v: unknown, pid: string): string | null {
  const u = str(v);
  if (/^https?:\/\//i.test(u)) return u;
  return pid ? coinbaseProductUrl(pid) : null;
}

export function normCbPosition(o: Raw): CbPosition {
  const pid = str(pick(o, "product_id", "product"));
  const quantity = num(pick(o, "quantity", "base_size", "qty"));
  const cost = num(o.cost_basis);
  const unreal = num(o.unrealized_pnl);
  return {
    venue: "coinbase",
    product_id: pid,
    base_currency: str(o.base_currency, baseOf(pid)),
    strategy: str(o.strategy),
    quantity,
    avg_cost: num(o.avg_cost),
    cost_basis: cost,
    mark_price: numOrNull(o.mark_price),
    best_bid: numOrNull(o.best_bid),
    liquidation_value: num(o.liquidation_value),
    mid_value: numOrNull(o.mid_value),
    unrealized_pnl: unreal,
    unrealized_pnl_pct: numOrNull(o.unrealized_pnl_pct) ?? (cost > 0 ? (unreal / cost) * 100 : null),
    realized_pnl: num(o.realized_pnl),
    fees_paid: num(pick(o, "fees_paid", "fees")),
    weight_of_strategy: fraction(o.weight_of_strategy),
    opened_at: ts(o.opened_at),
    url: urlOr(o.url, pid),
  };
}

export function normCbOrder(o: Raw): CbOrder {
  const raw = str(o.status);
  return {
    venue: "coinbase",
    id: id(o.id),
    product_id: str(pick(o, "product_id", "product")),
    side: cbSide(o.side) ?? "buy",
    order_type: str(o.order_type).toLowerCase() === "limit" ? "limit" : "market",
    tif: str(o.tif).toLowerCase() === "gtc" ? "gtc" : "ioc",
    post_only: bool(o.post_only),
    quote_size: numOrNull(o.quote_size),
    base_size: numOrNull(o.base_size),
    limit_price: numOrNull(o.limit_price),
    filled_base: num(o.filled_base),
    filled_quote: num(o.filled_quote),
    avg_fill_price: numOrNull(o.avg_fill_price),
    fees: num(pick(o, "fees", "fee")),
    status: orderStatus(raw),
    status_raw: raw,
    strategy: str(o.strategy),
    reason: str(o.reason),
    created_at: ts(o.created_at),
    updated_at: ts(o.updated_at),
    expires_at: ts(o.expires_at),
  };
}

export function normCbFill(o: Raw): CbFill {
  const base = num(pick(o, "base_size", "size", "quantity"));
  const price = num(o.price);
  const notional = numOrNull(o.notional) ?? base * price;
  const fee = num(o.fee);
  return {
    venue: "coinbase",
    id: id(o.id),
    order_id: id(o.order_id),
    product_id: str(pick(o, "product_id", "product")),
    side: cbSide(o.side) ?? "buy",
    base_size: base,
    price,
    notional,
    fee,
    fee_rate: numOrNull(o.fee_rate) ?? (notional > 0 ? fee / notional : null),
    is_taker: bool(o.is_taker, true),
    ts: tsReq(o.ts),
    strategy: str(o.strategy),
  };
}

export function normCbStrategy(o: Raw): CbStrategy {
  // params / param_schema follow the Kalshi conventions: reuse that normalizer.
  const k = normStrategy(o);
  const s = obj(o.stats);
  return {
    venue: "coinbase",
    name: k.name,
    description: k.description,
    experimental: k.experimental,
    enabled: k.enabled,
    enabled_source: k.enabled_source,
    params: k.params,
    param_schema: k.param_schema,
    bar_granularity_s: num(pick(o, "bar_granularity_s", "granularity_s", "bar_s"), 86400),
    universe: arr(o.universe).map((x) => str(x)).filter(Boolean),
    backtestable: k.backtestable,
    stats: {
      orders: num(s.orders),
      fills: num(s.fills),
      open_positions: num(s.open_positions),
      realized_pnl: num(s.realized_pnl),
      unrealized_pnl: num(s.unrealized_pnl),
      fees: num(s.fees),
      exposure: num(s.exposure),
      allocation_pct: numOrNull(s.allocation_pct),
      last_bar_at: ts(s.last_bar_at),
      last_error: strOrNull(s.last_error),
      trades: numOrNull(pick(s, "trades", "round_trips")),
      win_rate: fraction(s.win_rate),
    },
  };
}

function exposureRows(v: unknown, keyFields: string[]): CbExposureRow[] {
  // Either a list of rows or a {key: exposure | {...}} map.
  if (isObj(v)) {
    return Object.entries(v).map(([key, x]) => {
      if (isObj(x)) return { ...exposureRow(x, keyFields), key };
      return { key, exposure: num(x), pct: null, limit_pct: null };
    });
  }
  return list(v, (o) => exposureRow(o, keyFields));
}

function exposureRow(o: Raw, keyFields: string[]): CbExposureRow {
  return {
    key: str(pick(o, ...keyFields, "key", "name")),
    exposure: num(pick(o, "exposure", "value", "market_value", "amount")),
    // `pct` from the backend is exposure ÷ limit (percent OF THE LIMIT), not of equity;
    // prefer the explicit %-of-equity fields and only fall back to `pct` when absent.
    // `limit` is the USD cap, so it is never read as a percentage.
    pct: numOrNull(pick(o, "equity_pct", "pct_of_equity", "exposure_pct", "weight_pct", "pct")),
    limit_pct: numOrNull(pick(o, "limit_pct", "max_pct")),
  };
}

export function normCbRisk(v: unknown): CbRisk {
  const o = obj(v);
  const limitsRaw = obj(o.limits);
  const limits: CbRiskLimits = {};
  for (const [k, x] of Object.entries(limitsRaw)) {
    if (typeof x === "boolean" || x === null) limits[k] = x;
    else {
      const n = numOrNull(x);
      limits[k] = n ?? (typeof x === "string" ? x : null);
    }
  }
  const u = obj(o.utilization);
  return {
    venue: "coinbase",
    limits,
    utilization: {
      total_exposure: num(u.total_exposure),
      total_exposure_pct: num(u.total_exposure_pct),
      by_product: exposureRows(u.by_product, ["product_id", "product"]),
      by_strategy: exposureRows(u.by_strategy, ["strategy"]),
      orders_last_minute: num(u.orders_last_minute),
      daily_pnl: num(u.daily_pnl),
    },
    kill_switch: bool(o.kill_switch),
    kill_switch_reason: strOrNull(o.kill_switch_reason),
  };
}

export function normCbSignal(o: Raw): CbSignal {
  const raw = str(o.decision);
  return {
    venue: "coinbase",
    id: o.id === null || o.id === undefined ? null : id(o.id),
    ts: tsReq(o.ts),
    strategy: str(o.strategy),
    product_id: str(pick(o, "product_id", "product")),
    side: cbSide(o.side),
    target_weight: fraction(o.target_weight),
    quote_size: numOrNull(o.quote_size),
    base_size: numOrNull(o.base_size),
    limit_price: numOrNull(o.limit_price),
    expected_edge_bps: numOrNull(o.expected_edge_bps),
    reason: str(o.reason),
    decision: decision(raw),
    decision_raw: raw,
    decision_reason: str(o.decision_reason),
    order_id: o.order_id === null || o.order_id === undefined || o.order_id === "" ? null : id(o.order_id),
  };
}

export function normCbLog(o: Raw): CbLog {
  return {
    venue: "coinbase",
    id: o.id === null || o.id === undefined ? null : id(o.id),
    ts: tsReq(o.ts),
    level: str(o.level, "info").toLowerCase(),
    kind: str(o.kind, "engine"),
    message: str(o.message),
    data: isObj(o.data) ? o.data : null,
  };
}

export function normCbProduct(o: Raw): CbProductRow {
  const pid = str(pick(o, "product_id", "id"));
  const bid = numOrNull(o.bid);
  const ask = numOrNull(o.ask);
  const mid = bid !== null && ask !== null ? (bid + ask) / 2 : null;
  return {
    venue: "coinbase",
    product_id: pid,
    base_currency: str(o.base_currency, baseOf(pid)),
    price: numOrNull(pick(o, "price", "last")),
    bid,
    ask,
    spread_bps: numOrNull(o.spread_bps) ?? (mid && bid !== null && ask !== null ? ((ask - bid) / mid) * 10_000 : null),
    change_24h_pct: numOrNull(o.change_24h_pct),
    volume_24h_usd: numOrNull(pick(o, "volume_24h_usd", "volume_usd")),
    tradable: bool(o.tradable, true),
    url: urlOr(o.url, pid),
  };
}

function normAnalyticsStats(v: unknown): CbAnalyticsStats {
  const o = obj(v);
  const known = new Set(["trades", "total_pnl", "return_pct", "sharpe", "max_drawdown_pct", "fees", "turnover"]);
  const extra: Record<string, number> = {};
  for (const [k, x] of Object.entries(o)) {
    const n = typeof x === "number" && Number.isFinite(x) ? x : null;
    if (!known.has(k) && n !== null) extra[k] = n;
  }
  const dd = numOrNull(o.max_drawdown_pct);
  return {
    trades: num(pick(o, "trades", "n_trades", "count")),
    total_pnl: num(pick(o, "total_pnl", "pnl")),
    return_pct: numOrNull(pick(o, "return_pct", "total_return_pct")),
    sharpe: numOrNull(o.sharpe),
    max_drawdown_pct: dd === null ? null : Math.abs(dd),
    fees: num(pick(o, "fees", "fees_paid")),
    turnover: numOrNull(pick(o, "turnover", "turnover_per_year")),
    extra,
  };
}

export function normCbAnalytics(v: unknown): CbAnalytics {
  const o = obj(v);
  const bs = obj(o.by_strategy);
  const by_strategy: Record<string, CbAnalyticsStats> = {};
  if (Array.isArray(o.by_strategy)) {
    for (const r of o.by_strategy.filter(isObj)) by_strategy[str(pick(r, "name", "strategy"))] = normAnalyticsStats(r);
  } else for (const [k, x] of Object.entries(bs)) by_strategy[k] = normAnalyticsStats(x);
  const b = obj(o.benchmark);
  const r = obj(o.readiness);
  return {
    venue: "coinbase",
    overall: normAnalyticsStats(o.overall),
    by_strategy,
    benchmark: { btc_buy_hold_return_pct: numOrNull(pick(b, "btc_buy_hold_return_pct", "btc_return_pct")), since: ts(b.since) },
    readiness: { ready: bool(r.ready), reasons: arr(r.reasons).map((x) => str(x)).filter(Boolean) },
  };
}

/** Metric aliases folded onto the canonical names of CbBacktestMetrics. */
const METRIC_ALIASES: Record<string, string> = {
  return_pct: "total_return_pct",
  total_return: "total_return_pct",
  cagr: "cagr_pct",
  vol_pct: "volatility_pct",
  vol: "volatility_pct",
  volatility: "volatility_pct",
  annual_vol_pct: "volatility_pct",
  max_dd_pct: "max_drawdown_pct",
  turnover: "turnover_per_year",
  turnover_yr: "turnover_per_year",
  fees_paid: "fees",
  time_invested_pct: "pct_time_invested",
  n_trades: "trades",
  hit_rate: "win_rate",
};

export function normCbMetrics(v: unknown): CbBacktestMetrics | null {
  if (!isObj(v)) return null;
  const out: CbBacktestMetrics = {};
  for (const [k0, x] of Object.entries(v)) {
    const k = METRIC_ALIASES[k0] && !(METRIC_ALIASES[k0]! in v) ? METRIC_ALIASES[k0]! : k0;
    if (typeof x === "number" || typeof x === "string") {
      const n = numOrNull(x);
      out[k] = n ?? x;
    } else if (x === null) out[k] = null;
  }
  if (typeof out.max_drawdown_pct === "number") out.max_drawdown_pct = Math.abs(out.max_drawdown_pct);
  if (typeof out.win_rate === "number") out.win_rate = fraction(out.win_rate);
  return Object.keys(out).length ? out : null;
}

function curve(v: unknown): CbCurvePoint[] {
  return list(v, (o) => ({
    ts: tsReq(pick(o, "ts", "t", "time", "date")),
    equity: num(pick(o, "equity", "value")),
    exposure_pct: numOrNull(o.exposure_pct),
    drawdown_pct: numOrNull(o.drawdown_pct),
  })).filter((p) => p.ts !== "");
}

function period(v: unknown, kind: "year" | "month"): CbPeriodReturn[] {
  const rows: Raw[] = isObj(v)
    ? Object.entries(v).map(([k, x]): Raw => (isObj(x) ? { ...x, period: k } : { period: k, return_pct: x }))
    : arr(v).filter(isObj);
  return rows.map((o) => ({
    period: str(pick(o, "period", kind, "month", "year", "label")),
    return_pct: numOrNull(pick(o, "return_pct", "strategy_return_pct", "return")),
    pnl: numOrNull(o.pnl),
    btc_return_pct: numOrNull(pick(o, "btc_return_pct", "btc_pct", "btc")),
    equal_weight_return_pct: numOrNull(pick(o, "equal_weight_return_pct", "equal_weight_pct", "ew_return_pct", "equal_weight")),
    trades: numOrNull(o.trades),
    fees: numOrNull(o.fees),
  }));
}

function btTrade(o: Raw): CbBacktestTrade {
  return {
    ts: ts(pick(o, "ts", "time", "date")),
    product_id: str(pick(o, "product_id", "product")),
    side: cbSide(o.side),
    base_size: numOrNull(pick(o, "base_size", "quantity", "size")),
    price: numOrNull(o.price),
    notional: numOrNull(o.notional),
    fee: numOrNull(o.fee),
    pnl: numOrNull(pick(o, "pnl", "realized_pnl")),
    fee_rate: numOrNull(o.fee_rate),
    slippage_bps: numOrNull(o.slippage_bps),
    target_weight: fraction(o.target_weight),
    reason: strOrNull(o.reason),
  };
}

export function normCbBacktestSummary(o: Raw): CbBacktestSummary {
  return {
    id: id(o.id),
    strategy: str(o.strategy),
    params: normStrategy({ params: o.params }).params,
    start: calDate(o.start),
    end: calDate(o.end),
    period_reported: "start" in o || "end" in o,
    status: str(o.status, "running").toLowerCase(),
    created_at: ts(o.created_at),
    fee_tier: strOrNull(isObj(o.fee_tier) ? o.fee_tier.name : (o.fee_tier ?? (isObj(obj(o.metrics).details) ? obj(obj(obj(o.metrics).details).fee_tier).name : undefined))),
    starting_balance: numOrNull(o.starting_balance),
    metrics: normCbMetrics(o.metrics),
  };
}

export function normCbBacktestDetail(v: unknown): CbBacktestDetail {
  const o = obj(v);
  const b = obj(o.benchmarks);
  const m = obj(o.metrics);
  // The backtester nests benchmark metrics and run details inside `metrics`.
  const bm = obj(pick(o, "benchmark_metrics", "benchmarks_metrics") ?? m.benchmarks);
  const details = isObj(m.details) ? m.details : isObj(o.details) ? o.details : null;
  return {
    ...normCbBacktestSummary(o),
    error: strOrNull(o.error),
    equity_curve: curve(o.equity_curve),
    benchmarks: { btc: curve(pick(b, "btc", "btc_buy_hold")), equal_weight: curve(pick(b, "equal_weight", "ew")) },
    benchmark_metrics: { btc: normCbMetrics(pick(bm, "btc", "btc_buy_hold")), equal_weight: normCbMetrics(pick(bm, "equal_weight", "ew")) },
    trades: list(o.trades, btTrade),
    by_year: period(o.by_year, "year"),
    by_month: period(o.by_month, "month"),
    universe: arr(pick(o, "universe") ?? details?.universe).map((x) => str(x)).filter(Boolean),
    granularity_s: numOrNull(pick(o, "granularity_s") ?? details?.granularity_s),
    signals: list(o.signals, normCbSignal),
    details,
  };
}

export function normCbTick(v: unknown): CbTickEvent {
  const o = obj(v);
  const out: CbTickEvent = { ...o };
  const t = ts(o.ts);
  if (t) out.ts = t;
  else delete out.ts;
  return out;
}

export function normCbBar(v: unknown): CbBarEvent {
  const o = obj(v);
  return {
    ...o,
    ts: ts(o.ts),
    strategy: str(o.strategy),
    bar_end: ts(o.bar_end),
    granularity_s: numOrNull(pick(o, "granularity_s", "bar_granularity_s")),
    products: numOrNull(o.products),
    intents: numOrNull(o.intents),
  };
}

// ---------------------------------------------------------------------------
// Endpoints
// ---------------------------------------------------------------------------

export interface CbReqOpts {
  signal?: AbortSignal;
}

/** One typed wrapper per /api/coinbase endpoint (contract §13). */
export const cbApi = {
  status: (o?: CbReqOpts): Promise<CbStatus> => request("GET", "/status", undefined, o?.signal).then(normCbStatus),
  startEngine: (): Promise<CbStatus> => request("POST", "/engine/start", {}).then(normCbStatus),
  stopEngine: (): Promise<CbStatus> => request("POST", "/engine/stop", {}).then(normCbStatus),
  setKillSwitch: (on: boolean): Promise<CbStatus> => request("POST", "/engine/kill-switch", { on }).then(normCbStatus),

  account: (o?: CbReqOpts): Promise<CbAccount> => request("GET", "/account", undefined, o?.signal).then(normCbAccount),
  resetAccount: (startingBalance?: number): Promise<CbAccount> =>
    request("POST", "/account/reset", startingBalance === undefined ? {} : { starting_balance: startingBalance }).then(normCbAccount),
  equity: (range: CbEquityRange, o?: CbReqOpts): Promise<CbEquityPoint[]> =>
    request("GET", `/equity${qs({ range })}`, undefined, o?.signal).then(normCbEquity),

  positions: (o?: CbReqOpts): Promise<CbPosition[]> => request("GET", "/positions", undefined, o?.signal).then((v) => list(v, normCbPosition)),
  orders: (p: { status?: CbOrderStatusFilter; limit?: number } = {}, o?: CbReqOpts): Promise<CbOrder[]> =>
    request("GET", `/orders${qs({ status: p.status ?? "open", limit: p.limit ?? 200 })}`, undefined, o?.signal).then((v) => list(v, normCbOrder)),
  cancelOrder: (orderId: Id): Promise<CbOrder> => request("POST", `/orders/${enc(orderId)}/cancel`, {}).then((v) => normCbOrder(obj(v))),
  fills: (limit = 200, o?: CbReqOpts): Promise<CbFill[]> => request("GET", `/fills${qs({ limit })}`, undefined, o?.signal).then((v) => list(v, normCbFill)),

  strategies: (o?: CbReqOpts): Promise<CbStrategy[]> => request("GET", "/strategies", undefined, o?.signal).then((v) => list(v, normCbStrategy)),
  patchStrategy: (name: string, patch: CbStrategyPatch): Promise<CbStrategy> =>
    request("PATCH", `/strategies/${enc(name)}`, patch).then((v) => normCbStrategy(obj(v))),

  risk: (o?: CbReqOpts): Promise<CbRisk> => request("GET", "/risk", undefined, o?.signal).then(normCbRisk),
  patchRisk: (patch: CbRiskPatch): Promise<CbRisk> => request("PATCH", "/risk", patch).then(normCbRisk),

  signals: (limit = 200, o?: CbReqOpts): Promise<CbSignal[]> =>
    request("GET", `/signals${qs({ limit })}`, undefined, o?.signal).then((v) => list(v, normCbSignal)),
  logs: (limit = 200, o?: CbReqOpts): Promise<CbLog[]> => request("GET", `/logs${qs({ limit })}`, undefined, o?.signal).then((v) => list(v, normCbLog)),
  products: (q: CbProductsQuery = {}, o?: CbReqOpts): Promise<CbProductRow[]> =>
    request("GET", `/products${qs({ search: q.search?.trim(), sort: q.sort, limit: q.limit ?? 100 })}`, undefined, o?.signal).then((v) =>
      list(v, normCbProduct),
    ),

  analytics: (o?: CbReqOpts): Promise<CbAnalytics> => request("GET", "/analytics", undefined, o?.signal).then(normCbAnalytics),
  backtests: (o?: CbReqOpts): Promise<CbBacktestSummary[]> =>
    request("GET", "/backtests", undefined, o?.signal).then((v) => list(v, normCbBacktestSummary)),
  createBacktest: (req: CbBacktestCreateRequest): Promise<CbBacktestCreateResponse> =>
    request("POST", "/backtests", req).then((v) => {
      const o = obj(v);
      return { id: id(o.id), status: str(o.status, "running").toLowerCase() };
    }),
  backtest: (btId: Id, o?: CbReqOpts): Promise<CbBacktestDetail> =>
    request("GET", `/backtests/${enc(btId)}`, undefined, o?.signal).then(normCbBacktestDetail),
};
