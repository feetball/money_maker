/**
 * Defensive normalization of backend JSON into the types of types.ts.
 *
 * The backend is built in parallel against ARCHITECTURE §12; these helpers make the UI
 * robust to: nulls, missing fields, empty arrays, Decimal values serialized as strings
 * (pydantic's default), naive timestamps without "Z", epoch-second timestamps, and a
 * handful of field-name aliases where §12 leaves the shape open ("{...}", "[...]").
 */
import type {
  Account,
  AnalyticsParams,
  AnalyticsResponse,
  AnalyticsStats,
  BacktestCreateResponse,
  BacktestDetail,
  BacktestEquityPoint,
  BacktestMetrics,
  BacktestMonth,
  BacktestSummary,
  BacktestTrade,
  CalibrationBucket,
  CiBasis,
  EquityPoint,
  ExposureRow,
  Fill,
  Id,
  LogEntry,
  MarketRow,
  Order,
  OrderAction,
  OverviewCombined,
  OverviewEquityPoint,
  OverviewResponse,
  OverviewVenue,
  OrderStatus,
  ParamSpec,
  ParamValue,
  Position,
  Readiness,
  RiskLimits,
  RiskResponse,
  Settlement,
  Side,
  Signal,
  SignalDecision,
  StatusResponse,
  Strategy,
  StrategyStats,
  TickEvent,
  TimeInForce,
  VenueId,
} from "./types";

export type Raw = Record<string, unknown>;

export const isObj = (v: unknown): v is Raw =>
  typeof v === "object" && v !== null && !Array.isArray(v);

export function numOrNull(v: unknown): number | null {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

export function num(v: unknown, fallback = 0): number {
  return numOrNull(v) ?? fallback;
}

export function str(v: unknown, fallback = ""): string {
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return fallback;
}

export function strOrNull(v: unknown): string | null {
  const s = str(v, "");
  return s === "" ? null : s;
}

export function bool(v: unknown, fallback = false): boolean {
  if (typeof v === "boolean") return v;
  if (v === 1 || v === "1" || v === "true") return true;
  if (v === 0 || v === "0" || v === "false") return false;
  return fallback;
}

export function boolOrNull(v: unknown): boolean | null {
  if (v === null || v === undefined) return null;
  return bool(v);
}

export function arr(v: unknown): unknown[] {
  return Array.isArray(v) ? v : [];
}

export function obj(v: unknown): Raw {
  return isObj(v) ? v : {};
}

/** First non-null value among `keys`. */
export function pick(o: Raw, ...keys: string[]): unknown {
  for (const k of keys) {
    const v = o[k];
    if (v !== undefined && v !== null) return v;
  }
  return undefined;
}

export function id(v: unknown): Id {
  return typeof v === "number" ? v : str(v, "");
}

const NAIVE_ISO = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/;
const BARE_DATE = /^\d{4}-\d{2}-\d{2}$/;
/** "2026-09-26 12:00:00+00" / "+0000" (e.g. Postgres text) → ISO with a "+00:00" offset. */
const SPACED_WITH_OFFSET = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)([+-]\d{2})(?::?(\d{2}))?$/;

/**
 * ISO string or null. Accepts epoch seconds/ms, naive ISO (assumed UTC), a space
 * instead of "T" and short "+00" offsets. Anything Date.parse cannot read becomes
 * null, so a bad value never reaches a chart as a NaN coordinate.
 */
export function ts(v: unknown): string | null {
  if (typeof v === "number" && Number.isFinite(v)) {
    const d = new Date(v > 1e12 ? v : v * 1000);
    return Number.isFinite(d.getTime()) ? d.toISOString() : null;
  }
  if (typeof v === "string" && v.trim() !== "") {
    let s = v.trim();
    if (NAIVE_ISO.test(s)) s = s.replace(" ", "T") + "Z";
    else {
      const m = SPACED_WITH_OFFSET.exec(s);
      if (m) s = `${m[1]}T${m[2]}${m[3]}:${m[4] ?? "00"}`;
    }
    return Number.isFinite(Date.parse(s)) ? s : null;
  }
  return null;
}

/** A calendar date ("YYYY-MM-DD") is kept verbatim; anything else goes through ts(). */
export function calDate(v: unknown): string | null {
  if (typeof v === "string" && BARE_DATE.test(v.trim())) return v.trim();
  return ts(v);
}

const hasTime = (s: string) => Number.isFinite(Date.parse(s));

/** Required timestamp field: "" when missing (renders as "—", sorts last). */
export function tsReq(v: unknown): string {
  return ts(v) ?? "";
}

/** Missing or unrecognised sides stay "unknown" (never silently "yes"). */
export function side(v: unknown): Side {
  const s = str(v).toLowerCase();
  return s === "yes" || s === "no" ? s : "unknown";
}

function action(v: unknown): OrderAction {
  return str(v).toLowerCase() === "sell" ? "sell" : "buy";
}

function tif(v: unknown): TimeInForce {
  return str(v).toLowerCase() === "gtc" ? "gtc" : "ioc";
}

const ORDER_STATUSES: readonly OrderStatus[] = [
  "open",
  "filled",
  "partially_filled",
  "cancelled",
  "expired",
  "rejected",
];

/** Unrecognised statuses become "unknown" (neutral badge, never cancellable). */
function orderStatus(v: unknown): OrderStatus {
  const s = str(v).toLowerCase().replace("canceled", "cancelled");
  return (ORDER_STATUSES as readonly string[]).includes(s) ? (s as OrderStatus) : "unknown";
}

const DECISIONS: readonly SignalDecision[] = ["executed", "partial", "rejected", "unfilled"];

/** Empty (not decided yet) or unrecognised decisions become "unknown", never "rejected". */
function decision(v: unknown): SignalDecision {
  const s = str(v).toLowerCase();
  return (DECISIONS as readonly string[]).includes(s) ? (s as SignalDecision) : "unknown";
}

/** A fraction that some backends may send as percentage points (e.g. 57 for 57%). */
function fraction(v: unknown): number | null {
  const n = numOrNull(v);
  if (n === null) return null;
  return n > 1.0001 && n <= 100 ? n / 100 : n;
}

function list<T>(v: unknown, f: (r: Raw) => T): T[] {
  return arr(v).filter(isObj).map(f);
}

// ---------------------------------------------------------------------------

export function normStatus(v: unknown): StatusResponse {
  const o = obj(v);
  const e = obj(o.engine);
  const x = obj(o.exchange);
  return {
    mode: "paper",
    engine: {
      running: bool(e.running),
      started_at: ts(e.started_at),
      last_tick_at: ts(e.last_tick_at),
      tick_count: num(e.tick_count),
      universe_size: num(e.universe_size),
      last_error: strOrNull(e.last_error),
      last_error_at: ts(e.last_error_at),
      kill_switch: bool(pick(e, "kill_switch") ?? o.kill_switch),
      kill_switch_reason: strOrNull(pick(e, "kill_switch_reason") ?? o.kill_switch_reason),
    },
    exchange: { trading_active: boolOrNull(x.trading_active) },
    server_time: ts(o.server_time) ?? new Date().toISOString(),
  };
}

export function normAccount(v: unknown): Account {
  const o = obj(v);
  const cash = num(o.cash);
  const reserved = num(o.reserved_cash);
  const liq = num(o.positions_liquidation_value);
  const midRaw = numOrNull(o.positions_mid_value);
  const mid = midRaw ?? liq;
  const equity = numOrNull(o.equity) ?? cash + reserved + liq;
  const reservedProfit = num(o.reserved_profit);
  return {
    starting_balance: num(o.starting_balance),
    cash,
    reserved_cash: reserved,
    positions_liquidation_value: liq,
    positions_mid_value: mid,
    equity,
    equity_mid: numOrNull(o.equity_mid) ?? (midRaw !== null ? cash + reserved + midRaw : equity),
    realized_pnl: num(o.realized_pnl),
    unrealized_pnl: num(o.unrealized_pnl),
    fees_paid: num(o.fees_paid),
    reserved_profit: reservedProfit,
    net_worth: numOrNull(o.net_worth) ?? equity + reservedProfit,
    profit_sweep_enabled: bool(o.profit_sweep_enabled, true),
    profit_sweep_pct: num(o.profit_sweep_pct, 100),
    total_pnl: num(o.total_pnl),
    total_return_pct: num(o.total_return_pct),
    todays_pnl: num(o.todays_pnl),
    max_drawdown_pct: Math.abs(num(o.max_drawdown_pct)),
    open_positions: num(o.open_positions),
    open_orders: num(o.open_orders),
    settled_trades: num(o.settled_trades),
    win_rate: fraction(o.win_rate),
    ts: ts(o.ts),
  };
}

/** Numeric Account fields (every key of §12's account payload except win_rate). */
const ACCOUNT_NUM_KEYS = [
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
  "reserved_profit",
  "net_worth",
  "profit_sweep_pct",
  "total_pnl",
  "total_return_pct",
  "todays_pnl",
  "max_drawdown_pct",
  "open_positions",
  "open_orders",
  "settled_trades",
] as const satisfies readonly (keyof Account)[];

/**
 * SSE `account` payload: only the fields that are present and finite, nothing filled
 * in with 0 (a subset must never read as "$0.00"). Tolerates a {account: {...}} wrapper.
 */
export function normAccountPartial(v: unknown): Partial<Account> {
  let o = obj(v);
  if (isObj(o.account) && !("equity" in o)) o = o.account;
  const out: Partial<Account> = {};
  for (const k of ACCOUNT_NUM_KEYS) {
    const n = numOrNull(o[k]);
    if (n !== null) out[k] = k === "max_drawdown_pct" ? Math.abs(n) : n;
  }
  if ("win_rate" in o) {
    const w = fraction(o.win_rate);
    if (w !== null || o.win_rate === null) out.win_rate = w;
  }
  if ("profit_sweep_enabled" in o) out.profit_sweep_enabled = bool(o.profit_sweep_enabled, true);
  const t = ts(o.ts);
  if (t) out.ts = t;
  return out;
}

/** A full Account when `p` carries every §12 key (so it can stand in for a poll). */
export function completeAccount(p: Partial<Account>): Account | null {
  for (const k of ACCOUNT_NUM_KEYS) if (p[k] === undefined) return null;
  if (p.profit_sweep_enabled === undefined) return null;
  return { ...(p as Account), win_rate: p.win_rate ?? null };
}

export function normEquity(v: unknown): EquityPoint[] {
  return list(v, (o) => {
    const equity = num(o.equity);
    return {
      ts: tsReq(o.ts),
      equity,
      equity_mid: numOrNull(o.equity_mid) ?? equity,
      cash: num(o.cash),
      realized_pnl: num(o.realized_pnl),
      unrealized_pnl: num(o.unrealized_pnl),
    };
  })
    .filter((p) => hasTime(p.ts))
    .sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
}

export function normPosition(o: Raw): Position {
  return {
    ticker: str(o.ticker),
    title: str(o.title),
    event_ticker: str(o.event_ticker),
    side: side(o.side),
    count: num(o.count),
    avg_price: num(o.avg_price),
    cost_basis: num(o.cost_basis),
    open_fees: numOrNull(pick(o, "open_fees", "fees")),
    mark_price: numOrNull(o.mark_price),
    best_bid: numOrNull(pick(o, "best_bid", "mark_price")),
    mark_stale: bool(o.mark_stale),
    liquidation_value: num(o.liquidation_value),
    unrealized_pnl: num(o.unrealized_pnl),
    fair_value: numOrNull(o.fair_value),
    expected_edge_total: numOrNull(o.expected_edge_total),
    strategy: str(o.strategy),
    opened_at: ts(o.opened_at),
    close_time: ts(o.close_time),
    yes_bid: numOrNull(o.yes_bid),
    yes_ask: numOrNull(o.yes_ask),
    url: strOrNull(o.url),
  };
}

export function normOrder(o: Raw): Order {
  return {
    id: id(o.id),
    ticker: str(o.ticker),
    title: str(o.title),
    side: side(o.side),
    action: action(o.action),
    count: num(o.count),
    filled_count: num(o.filled_count),
    limit_price: num(o.limit_price),
    avg_fill_price: numOrNull(o.avg_fill_price),
    tif: tif(o.tif),
    status: orderStatus(o.status),
    status_raw: str(o.status),
    strategy: str(o.strategy),
    reason: str(o.reason),
    expected_edge: numOrNull(o.expected_edge),
    fair_value: numOrNull(o.fair_value),
    group_id: strOrNull(o.group_id),
    queue_ahead: numOrNull(o.queue_ahead),
    created_at: ts(o.created_at),
    updated_at: ts(o.updated_at),
    expires_at: ts(o.expires_at),
    fees: num(o.fees),
  };
}

export function normFill(o: Raw): Fill {
  return {
    id: id(o.id),
    order_id: id(o.order_id),
    ticker: str(o.ticker),
    title: str(o.title),
    side: side(o.side),
    action: action(o.action),
    count: num(o.count),
    price: num(o.price),
    fee: num(o.fee),
    is_taker: bool(o.is_taker, true),
    ts: tsReq(o.ts),
    strategy: str(o.strategy),
  };
}

export function normSettlement(o: Raw): Settlement {
  const result = str(o.result);
  const kind = str(o.kind).toLowerCase() === "close" || result.toLowerCase() === "closed" ? "close" : "settlement";
  return {
    id: id(o.id),
    ticker: str(o.ticker),
    title: str(o.title),
    kind,
    result,
    side: side(o.side),
    count: num(o.count),
    payout: num(o.payout),
    cost_basis: num(o.cost_basis),
    fees: numOrNull(o.fees),
    pnl: num(o.pnl),
    ts: tsReq(o.ts),
    strategy: str(o.strategy),
  };
}

function normParamValue(v: unknown): ParamValue {
  if (v === null || v === undefined) return null;
  if (typeof v === "number" || typeof v === "string" || typeof v === "boolean") return v;
  if (Array.isArray(v)) return v.map(normParamValue);
  if (isObj(v)) {
    const out: Record<string, ParamValue> = {};
    for (const [k, x] of Object.entries(v)) out[k] = normParamValue(x);
    return out;
  }
  return null;
}

export function normParams(v: unknown): Record<string, ParamValue> {
  const out: Record<string, ParamValue> = {};
  for (const [k, x] of Object.entries(obj(v))) out[k] = normParamValue(x);
  return out;
}

function normParamSpec(v: unknown, current: ParamValue | undefined): ParamSpec {
  // Tolerate a bare type string: {"min_edge": "float"}.
  if (typeof v === "string") return { type: v };
  const o = obj(v);
  const choices = pick(o, "enum", "choices", "options");
  let type = str(o.type).toLowerCase();
  if (!type) {
    if (Array.isArray(choices)) type = "enum";
    else if (typeof current === "boolean") type = "bool";
    else if (typeof current === "number") type = Number.isInteger(current) ? "int" : "float";
    else if (typeof current === "string") type = "str";
    else type = "json";
  }
  return {
    type,
    min: numOrNull(pick(o, "min", "minimum")),
    max: numOrNull(pick(o, "max", "maximum")),
    step: numOrNull(o.step),
    help: strOrNull(pick(o, "help", "description")),
    enum: Array.isArray(choices) ? choices.map(normParamValue) : null,
    default: o.default === undefined ? undefined : normParamValue(o.default),
    title: strOrNull(pick(o, "title", "label")),
  };
}

function normStats(v: unknown): StrategyStats {
  const o = obj(v);
  return {
    orders: num(o.orders),
    fills: num(o.fills),
    open_positions: num(o.open_positions),
    settled: num(o.settled),
    realized_pnl: num(o.realized_pnl),
    unrealized_pnl: num(o.unrealized_pnl),
    fees: num(o.fees),
    win_rate: fraction(o.win_rate),
    exposure: num(o.exposure),
  };
}

export function normStrategy(o: Raw): Strategy {
  const params = normParams(o.params);
  const schemaRaw = obj(o.param_schema);
  const param_schema: Record<string, ParamSpec> = {};
  for (const [k, spec] of Object.entries(schemaRaw)) param_schema[k] = normParamSpec(spec, params[k]);
  // Params without a schema entry are still editable (inferred type).
  for (const k of Object.keys(params)) {
    if (!param_schema[k]) param_schema[k] = normParamSpec({}, params[k]);
  }
  const rl = isObj(o.risk_limits) ? o.risk_limits : null;
  return {
    name: str(o.name),
    description: str(o.description),
    enabled: bool(o.enabled),
    enabled_source: strOrNull(o.enabled_source),
    params,
    param_schema,
    backtestable: bool(o.backtestable),
    experimental: bool(o.experimental),
    risk_limits: rl
      ? { max_allocation_pct: numOrNull(rl.max_allocation_pct), daily_loss_limit: numOrNull(rl.daily_loss_limit), paused: strOrNull(rl.paused) }
      : null,
    last_tick_at: strOrNull(o.last_tick_at),
    last_error: strOrNull(o.last_error),
    stats: normStats(o.stats),
  };
}

function normExposureRow(o: Raw, keyFields: string[]): ExposureRow {
  return {
    key: str(pick(o, ...keyFields, "key", "name", "ticker")),
    exposure: num(pick(o, "exposure", "value", "cost", "amount")),
    limit: numOrNull(pick(o, "limit", "max", "cap")),
    pct: numOrNull(pick(o, "pct", "exposure_pct", "utilization_pct", "percent")),
    title: strOrNull(o.title),
  };
}

/** by_event/by_strategy may be a list of rows or a {key: exposure} map. */
function normExposureList(v: unknown, keyFields: string[]): ExposureRow[] {
  if (isObj(v)) {
    return Object.entries(v).map(([k, x]) =>
      isObj(x)
        ? { ...normExposureRow(x, keyFields), key: k }
        : { key: k, exposure: num(x), limit: null, pct: null, title: null },
    );
  }
  return list(v, (o) => normExposureRow(o, keyFields));
}

export function normRisk(v: unknown): RiskResponse {
  const o = obj(v);
  const u = obj(o.utilization);
  const limits: RiskLimits = {};
  for (const [k, x] of Object.entries(obj(o.limits))) {
    if (typeof x === "number" || typeof x === "boolean" || x === null) limits[k] = x;
    else if (typeof x === "string") limits[k] = numOrNull(x) ?? x;
  }
  return {
    limits,
    utilization: {
      total_exposure: num(u.total_exposure),
      total_exposure_pct: num(u.total_exposure_pct),
      by_event: normExposureList(u.by_event, ["event_ticker", "event"]),
      by_strategy: normExposureList(u.by_strategy, ["strategy"]),
      orders_last_minute: num(u.orders_last_minute),
      daily_pnl: num(u.daily_pnl),
    },
    kill_switch: bool(o.kill_switch),
    kill_switch_reason: bool(o.kill_switch) ? strOrNull(o.kill_switch_reason) : null,
  };
}

const idOrNull = (v: unknown): Id | null => (v === null || v === undefined || v === "" ? null : id(v));

/** "buy" / "sell", or null when missing or unrecognised (never guessed). */
function actionOrNull(v: unknown): OrderAction | null {
  const s = str(v).toLowerCase();
  return s === "buy" || s === "sell" ? s : null;
}

export function normSignal(o: Raw): Signal {
  return {
    id: idOrNull(o.id),
    ts: tsReq(o.ts),
    strategy: str(o.strategy),
    ticker: str(o.ticker),
    title: str(o.title),
    side: side(o.side),
    action: actionOrNull(o.action),
    // Malformed intents are recorded with count/limit_price null: keep them null ("—").
    count: numOrNull(o.count),
    limit_price: numOrNull(o.limit_price),
    fair_value: numOrNull(o.fair_value),
    expected_edge: numOrNull(o.expected_edge),
    reason: str(o.reason),
    decision: decision(o.decision),
    decision_raw: str(o.decision),
    decision_reason: str(o.decision_reason),
  };
}

export function normLog(o: Raw): LogEntry {
  return {
    id: idOrNull(o.id),
    ts: tsReq(o.ts),
    level: str(o.level, "info").toLowerCase(),
    kind: str(o.kind, "log"),
    message: str(pick(o, "message", "msg")),
    data: isObj(o.data) ? o.data : null,
  };
}

export function normMarket(o: Raw): MarketRow {
  const bid = numOrNull(o.yes_bid);
  const ask = numOrNull(o.yes_ask);
  return {
    ticker: str(o.ticker),
    event_ticker: str(o.event_ticker),
    title: str(o.title),
    category: str(o.category),
    yes_bid: bid,
    yes_ask: ask,
    spread: numOrNull(o.spread) ?? (bid !== null && ask !== null ? ask - bid : null),
    last_price: numOrNull(o.last_price),
    volume_24h: num(o.volume_24h),
    open_interest: num(o.open_interest),
    close_time: ts(o.close_time),
    url: strOrNull(o.url),
  };
}

function normReadiness(v: unknown): Readiness | null {
  if (!isObj(v)) return null;
  const reasons = arr(v.reasons).map((r) => str(r)).filter((r) => r !== "");
  const ready_strategies = arr(v.ready_strategies).map((r) => str(r)).filter((r) => r !== "");
  return { ready: bool(v.ready), reasons, ready_strategies };
}

function ciPair(o: Raw, keys: string[]): [number | null, number | null] {
  for (const k of keys) {
    const v = o[k];
    if (Array.isArray(v) && v.length >= 2) return [numOrNull(v[0]), numOrNull(v[1])];
    if (isObj(v)) {
      const lo = numOrNull(pick(v, "low", "lo", "lower"));
      const hi = numOrNull(pick(v, "high", "hi", "upper"));
      if (lo !== null || hi !== null) return [lo, hi];
    }
  }
  return [null, null];
}

function inside(m: number | null, lo: number, hi: number): boolean {
  if (m === null) return false;
  const eps = 1e-9 + (Math.abs(hi - lo) * 1e-6);
  return m >= Math.min(lo, hi) - eps && m <= Math.max(lo, hi) + eps;
}

/**
 * Which mean the CI belongs to. An explicit `ci_basis` wins. Otherwise the mean that
 * lies inside its own interval; §11 defines readiness on the per-trade mean, so a tie
 * (or no CI at all) resolves to "trade" when a per-trade mean exists. If neither mean
 * is inside the interval the basis is "unknown" and the UI shows the CI in dollars.
 */
export function ciBasis(
  explicit: string,
  lo: number | null,
  hi: number | null,
  perTrade: number | null,
  perContract: number | null,
): CiBasis {
  const e = explicit.toLowerCase().replace(/^per[_ -]?/, "");
  if (e === "trade" || e === "contract") return e;
  if (lo === null || hi === null) return perTrade !== null || perContract === null ? "trade" : "contract";
  const t = inside(perTrade, lo, hi);
  const c = inside(perContract, lo, hi);
  if (t) return "trade";
  if (c) return "contract";
  return "unknown";
}

export function normAnalyticsStats(v: unknown): AnalyticsStats {
  const o = obj(v);
  const [ciArrLo, ciArrHi] = ciPair(o, ["ci95", "ci", "ci_95", "mean_pnl_ci"]);
  const ci_low = numOrNull(pick(o, "ci_low", "ci95_low", "ci_lower", "ci_lo", "mean_pnl_ci_low")) ?? ciArrLo;
  const ci_high = numOrNull(pick(o, "ci_high", "ci95_high", "ci_upper", "ci_hi", "mean_pnl_ci_high")) ?? ciArrHi;
  const perContract = numOrNull(pick(o, "mean_pnl_per_contract", "mean_pnl_contract", "ev_per_contract"));
  const perTrade = numOrNull(pick(o, "mean_pnl_per_trade", "mean_pnl", "mean_pnl_trade"));
  const basis = ciBasis(str(o.ci_basis), ci_low, ci_high, perTrade, perContract);
  const total = num(pick(o, "total_pnl", "realized_pnl", "pnl"));
  // Per-trade CI (the one the backend's readiness verdict uses). When the main CI is
  // itself per trade it doubles as this pair.
  const [ctArrLo, ctArrHi] = ciPair(o, ["ci_trade", "ci_per_trade", "ci95_trade"]);
  const ciTradeLo = numOrNull(pick(o, "ci_trade_low", "ci_trade_lo", "ci95_trade_low", "ci_trade_lower")) ?? ctArrLo;
  const ciTradeHi = numOrNull(pick(o, "ci_trade_high", "ci_trade_hi", "ci95_trade_high", "ci_trade_upper")) ?? ctArrHi;
  const hasTradeCi = ciTradeLo !== null || ciTradeHi !== null;
  const expected = numOrNull(pick(o, "expected_edge_total", "expected_edge", "expected_pnl", "expected_total"));
  const realWithEdge = numOrNull(pick(o, "realized_pnl_with_edge", "realized_with_edge"));
  const captureRaw = numOrNull(o.edge_capture);
  const capture =
    captureRaw ?? (expected !== null && realWithEdge !== null && Math.abs(expected) > 1e-9 ? realWithEdge / expected : null);
  return {
    count: num(pick(o, "count", "n", "n_trades", "settled", "settled_trades", "trades")),
    contracts: numOrNull(pick(o, "contracts", "n_contracts")),
    total_pnl: total,
    mean_pnl_per_contract: perContract,
    mean_pnl_per_trade: perTrade,
    ci_low,
    ci_high,
    ci_trade_low: hasTradeCi ? ciTradeLo : basis === "trade" ? ci_low : null,
    ci_trade_high: hasTradeCi ? ciTradeHi : basis === "trade" ? ci_high : null,
    ci_basis: basis,
    expected_edge_total: expected,
    realized_pnl_with_edge: realWithEdge,
    edge_capture: capture,
    trades_with_edge: numOrNull(o.trades_with_edge),
    realized_pnl: num(pick(o, "realized_pnl", "total_pnl", "pnl")),
    brier: numOrNull(pick(o, "brier", "brier_score")),
    win_rate: fraction(pick(o, "win_rate", "hit_rate")),
    max_drawdown: (() => {
      const d = numOrNull(pick(o, "max_drawdown", "max_drawdown_usd"));
      return d === null ? null : Math.abs(d);
    })(),
    max_drawdown_pct: (() => {
      const d = numOrNull(o.max_drawdown_pct);
      return d === null ? null : Math.abs(d);
    })(),
    readiness: normReadiness(o.readiness),
  };
}

export function normAnalytics(v: unknown): AnalyticsResponse {
  const o = obj(v);
  const by: Record<string, AnalyticsStats> = {};
  const rawBy = o.by_strategy;
  if (Array.isArray(rawBy)) {
    for (const r of rawBy) if (isObj(r)) by[str(pick(r, "strategy", "name"), "?")] = normAnalyticsStats(r);
  } else {
    for (const [k, x] of Object.entries(obj(rawBy))) by[k] = normAnalyticsStats(x);
  }
  const calibration: CalibrationBucket[] = list(o.calibration, (r) => ({
    bucket: typeof r.bucket === "number" ? r.bucket : str(r.bucket),
    n: num(pick(r, "n", "count")),
    mean_fair_value: num(pick(r, "mean_fair_value", "fair_value", "predicted")),
    realized_rate: num(pick(r, "realized_rate", "realized", "observed")),
  })).filter((b) => b.n > 0);
  const overall = normAnalyticsStats(o.overall);
  let params: AnalyticsParams | null = null;
  if (isObj(o.params)) {
    const p = o.params;
    params = {
      min_settled_trades: numOrNull(pick(p, "min_settled_trades", "min_trades")),
      min_settled_trades_by_strategy: Object.fromEntries(
        Object.entries(isObj(p.min_settled_trades_by_strategy) ? p.min_settled_trades_by_strategy : {})
          .map(([k, v]) => [k, numOrNull(v)] as const)
          .filter((e): e is readonly [string, number] => e[1] !== null),
      ),
      max_drawdown_pct: numOrNull(pick(p, "max_drawdown_pct", "max_dd_pct")),
    };
  }
  return {
    overall,
    by_strategy: by,
    calibration,
    readiness: normReadiness(o.readiness) ?? overall.readiness ?? { ready: false, reasons: ["No readiness verdict reported by the backend."], ready_strategies: [] },
    params,
  };
}

export function normMetrics(v: unknown): BacktestMetrics | null {
  if (!isObj(v)) return null;
  const out: BacktestMetrics = {};
  for (const [k, x] of Object.entries(v)) {
    const n = numOrNull(x);
    out[k] = n ?? x;
  }
  // Alias common alternatives onto the canonical keys.
  const alias = (canon: string, ...alts: string[]) => {
    if (out[canon] === undefined || out[canon] === null) {
      for (const a of alts) if (out[a] !== undefined && out[a] !== null) { out[canon] = out[a]; break; }
    }
  };
  alias("total_pnl", "pnl", "net_pnl");
  alias("n_trades", "trades", "count", "num_trades");
  alias("ev_per_contract", "mean_pnl_per_contract", "ev");
  alias("hit_rate", "win_rate");
  alias("sharpe", "sharpe_like", "sharpe_ratio");
  const ci = v.ev_ci ?? v.ci95 ?? v.ci;
  if (Array.isArray(ci) && ci.length >= 2) {
    out.ev_ci_low ??= numOrNull(ci[0]);
    out.ev_ci_high ??= numOrNull(ci[1]);
  }
  alias("ev_ci_low", "ci_low", "ci95_low");
  alias("ev_ci_high", "ci_high", "ci95_high");
  const hr = out.hit_rate;
  if (typeof hr === "number") out.hit_rate = fraction(hr);
  for (const k of ["max_drawdown", "max_drawdown_pct"]) {
    const d = out[k];
    if (typeof d === "number") out[k] = Math.abs(d);
  }
  return out;
}

/** Backtest period: calendar dates kept verbatim; `start_at`/`end_at` accepted as aliases. */
function period(o: Raw): { start: string | null; end: string | null; period_reported: boolean } {
  const keys = ["start", "end", "start_at", "end_at"];
  return {
    start: calDate(pick(o, "start", "start_at")),
    end: calDate(pick(o, "end", "end_at")),
    period_reported: keys.some((k) => k in o),
  };
}

export function normBacktestSummary(o: Raw): BacktestSummary {
  return {
    id: id(o.id),
    strategy: str(o.strategy),
    params: normParams(o.params),
    ...period(o),
    status: str(o.status, "running").toLowerCase(),
    created_at: ts(o.created_at),
    metrics: normMetrics(o.metrics),
  };
}

export function normBacktestCreate(v: unknown): BacktestCreateResponse {
  const o = obj(v);
  return { id: id(o.id), status: str(o.status, "running") };
}

function normBtTrade(o: Raw): BacktestTrade {
  return {
    ts: ts(pick(o, "ts", "entry_ts", "opened_at", "time")),
    ticker: str(o.ticker),
    event_ticker: strOrNull(o.event_ticker),
    side: side(o.side),
    count: num(pick(o, "count", "contracts", "qty")),
    price: numOrNull(pick(o, "price", "entry_price", "avg_price", "limit_price")),
    fee: numOrNull(pick(o, "fee", "fees")),
    result: strOrNull(o.result),
    pnl: numOrNull(pick(o, "pnl", "realized_pnl")),
    settled_at: ts(pick(o, "settled_at", "exit_ts", "settlement_ts")),
    reason: strOrNull(o.reason),
  };
}

function normMonth(o: Raw): BacktestMonth {
  return {
    month: str(pick(o, "month", "period", "ym")),
    pnl: num(pick(o, "pnl", "total_pnl")),
    trades: numOrNull(pick(o, "trades", "n_trades", "count", "n")),
    contracts: numOrNull(o.contracts),
    win_rate: fraction(pick(o, "win_rate", "hit_rate")),
  };
}

export function normBacktestDetail(v: unknown): BacktestDetail {
  const o = obj(v);
  const curve: BacktestEquityPoint[] = list(o.equity_curve, (p) => ({
    ts: tsReq(p.ts),
    equity: num(p.equity),
  }))
    .filter((p) => hasTime(p.ts))
    .sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
  return {
    id: id(o.id),
    strategy: str(o.strategy),
    params: normParams(o.params),
    status: str(o.status, "running").toLowerCase(),
    error: strOrNull(o.error),
    metrics: normMetrics(o.metrics),
    equity_curve: curve,
    trades: list(o.trades, normBtTrade),
    by_month: list(o.by_month, normMonth),
    ...period(o),
    created_at: ts(o.created_at),
  };
}

export function normTick(v: unknown): TickEvent {
  const o = obj(v);
  const out: TickEvent = { ...o };
  const t = ts(o.ts);
  if (t) out.ts = t;
  const tc = numOrNull(o.tick_count);
  if (tc !== null) out.tick_count = tc;
  return out;
}

export const normList = {
  positions: (v: unknown) => list(v, normPosition),
  orders: (v: unknown) => list(v, normOrder),
  fills: (v: unknown) => list(v, normFill),
  settlements: (v: unknown) => list(v, normSettlement),
  strategies: (v: unknown) => list(v, normStrategy),
  signals: (v: unknown) => list(v, normSignal),
  logs: (v: unknown) => list(v, normLog),
  markets: (v: unknown) => list(v, normMarket),
  backtests: (v: unknown) => list(v, normBacktestSummary),
};

// ---------------------------------------------------------------------------
// GET /api/overview (COINBASE_CONTRACT §13)
// ---------------------------------------------------------------------------

export const VENUE_LABELS: Record<VenueId, string> = {
  kalshi: "KALSHI · prediction markets",
  coinbase: "COINBASE · crypto spot",
};

export const COMBINED_NOTE = "Sum of two separate paper accounts";

export function normOverviewVenue(v: unknown, venue: VenueId): OverviewVenue {
  const o = obj(v);
  // A venue block without `available` is available when it carries an equity figure
  // (Kalshi is always available per the contract).
  const available = "available" in o ? bool(o.available) : venue === "kalshi" || numOrNull(o.equity) !== null;
  const reason = strOrNull(pick(o, "unavailable_reason", "reason", "error"));
  return {
    venue,
    label: str(o.label) || VENUE_LABELS[venue],
    available,
    unavailable_reason: available ? null : (reason ?? "not reported by the server"),
    engine_running: bool(pick(o, "engine_running", "running")),
    kill_switch: bool(o.kill_switch),
    starting_balance: numOrNull(o.starting_balance),
    equity: numOrNull(o.equity),
    cash: numOrNull(o.cash),
    total_pnl: numOrNull(o.total_pnl),
    total_return_pct: numOrNull(o.total_return_pct),
    todays_pnl: numOrNull(o.todays_pnl),
    open_positions: numOrNull(o.open_positions),
    fees_paid: numOrNull(o.fees_paid),
    last_error: strOrNull(o.last_error),
    last_error_at: ts(o.last_error_at),
    last_tick_at: ts(pick(o, "last_tick_at", "last_bar_at")),
  };
}

/** [{ts, equity}] (also {ts, value} or [ts, equity] pairs), oldest first, bad rows dropped. */
export function normOverviewSeries(v: unknown): OverviewEquityPoint[] {
  const out: OverviewEquityPoint[] = [];
  for (const x of arr(v)) {
    let t: string | null;
    let e: number | null;
    if (Array.isArray(x)) {
      t = ts(x[0]);
      e = numOrNull(x[1]);
    } else {
      const o = obj(x);
      t = ts(pick(o, "ts", "t", "time"));
      e = numOrNull(pick(o, "equity", "value"));
    }
    if (t && hasTime(t) && e !== null) out.push({ ts: t, equity: e });
  }
  return out.sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
}

export function normOverview(v: unknown): OverviewResponse {
  const o = obj(v);
  const venues = obj(o.venues);
  const kalshi = normOverviewVenue(venues.kalshi, "kalshi");
  const coinbase = normOverviewVenue(venues.coinbase, "coinbase");
  const c = obj(o.combined);
  const sum = (k: "starting_balance" | "equity" | "total_pnl"): number | null => {
    const xs = [kalshi, coinbase].filter((x) => x.available).map((x) => x[k]);
    return xs.length && xs.every((x) => x !== null) ? xs.reduce<number>((a, b) => a + (b ?? 0), 0) : null;
  };
  const combined: OverviewCombined = {
    starting_balance: numOrNull(c.starting_balance) ?? sum("starting_balance"),
    equity: numOrNull(c.equity) ?? sum("equity"),
    total_pnl: numOrNull(c.total_pnl) ?? sum("total_pnl"),
    total_return_pct: numOrNull(c.total_return_pct),
    note: str(c.note) || COMBINED_NOTE,
  };
  if (combined.total_return_pct === null && combined.total_pnl !== null && combined.starting_balance) {
    combined.total_return_pct = (combined.total_pnl / combined.starting_balance) * 100;
  }
  const series = obj(pick(o, "equity_series", "equity"));
  return {
    generated_at: ts(o.generated_at),
    venues: { kalshi, coinbase },
    combined,
    equity_series: { kalshi: normOverviewSeries(series.kalshi), coinbase: normOverviewSeries(series.coinbase) },
  };
}
