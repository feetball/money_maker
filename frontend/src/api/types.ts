/**
 * Types mirroring the REST contract in docs/ARCHITECTURE.md §12 (binding).
 *
 * Conventions (ARCHITECTURE §2):
 *  - Prices are dollars in [0, 1] per contract for the side named (0.93 = 93¢).
 *  - Money / P&L are dollars as JSON numbers (4 dp).
 *  - Timestamps are ISO-8601 UTC strings ending in "Z".
 *  - Fields suffixed `_pct` are percentage points (12.5 = 12.5 %), matching the
 *    config (`max_total_exposure_pct: 80`). `win_rate`, `fair_value`, `realized_rate`
 *    and `mean_fair_value` are fractions in [0, 1].
 *
 * Where §12 leaves a shape open ("{...}", "[...]"), the interface below documents the
 * resolution the UI expects; the client normalizes aliases and missing fields (see
 * client.ts) so every value here is safe to read even if the backend omits it.
 */

export type IsoDateTime = string;
/** Identifiers are opaque; the backend may use integers or strings. */
export type Id = string | number;

/** "unknown" when the backend sent no or an unrecognised side (rendered as "?"). */
export type Side = "yes" | "no" | "unknown";
export type OrderAction = "buy" | "sell";
export type TimeInForce = "ioc" | "gtc";
export type OrderStatus =
  | "open"
  | "filled"
  | "partially_filled"
  | "cancelled"
  | "expired"
  | "rejected"
  /** Unrecognised status from the backend; the raw value is kept in `status_raw`. */
  | "unknown";
/** "unknown" = empty (decision not made yet) or unrecognised; raw value in `decision_raw`. */
export type SignalDecision = "executed" | "partial" | "rejected" | "unfilled" | "unknown";
export type EquityRange = "1d" | "7d" | "30d" | "all";
export type OrderStatusFilter = "open" | "all";
export type MarketSort = "volume_24h" | "close_time" | "spread";
export type LogLevel = "debug" | "info" | "warning" | "error" | "critical";

// ---------------------------------------------------------------------------
// GET /api/status, POST /api/engine/start|stop|kill-switch
// ---------------------------------------------------------------------------

export interface EngineStatus {
  running: boolean;
  started_at: IsoDateTime | null;
  last_tick_at: IsoDateTime | null;
  tick_count: number;
  universe_size: number;
  /**
   * Most recent job/strategy failure. The backend does not clear it when the failing
   * job recovers, so it is HISTORY, not the current state: use `last_error_at` to decide
   * whether it is still relevant (see engineErrorIsCurrent in components/Engine.tsx).
   */
  last_error: string | null;
  /** When `last_error` was recorded (backend extra); null when not reported. */
  last_error_at: IsoDateTime | null;
  kill_switch: boolean;
  /** Why the kill switch is on (not in §12; read when the backend sends it). */
  kill_switch_reason: string | null;
}

export interface ExchangeStatus {
  /** null when the exchange status has not been fetched yet. */
  trading_active: boolean | null;
}

export interface StatusResponse {
  mode: "paper";
  engine: EngineStatus;
  exchange: ExchangeStatus;
  server_time: IsoDateTime;
}

export interface KillSwitchRequest {
  on: boolean;
}

// ---------------------------------------------------------------------------
// GET /api/account, POST /api/account/reset
// ---------------------------------------------------------------------------

export interface Account {
  starting_balance: number;
  cash: number;
  /** Cash reserved for resting (open) orders. */
  reserved_cash: number;
  /**
   * What selling every open position into its side's bid ladder would bring now (walks the
   * displayed depth, net of bids we consumed; contracts beyond the depth count as 0), before exit fees.
   */
  positions_liquidation_value: number;
  /** Positions marked at mid. */
  positions_mid_value: number;
  /** cash + reserved_cash + positions_liquidation_value (the headline number). */
  equity: number;
  /** cash + reserved_cash + positions_mid_value. */
  equity_mid: number;
  realized_pnl: number;
  unrealized_pnl: number;
  fees_paid: number;
  /** Profit moved out of `cash`/`equity` (never spent on new orders); see account settings. */
  reserved_profit: number;
  /** equity + reserved_profit: true account value including profit set aside. */
  net_worth: number;
  total_pnl: number;
  /** Percentage points. */
  total_return_pct: number;
  todays_pnl: number;
  /** Percentage points, reported as a positive magnitude. */
  max_drawdown_pct: number;
  open_positions: number;
  open_orders: number;
  settled_trades: number;
  /** Fraction in [0, 1]; null when there are no settled trades. */
  win_rate: number | null;
  /** Server time the snapshot was taken (backend extra; absent when not reported). */
  ts?: IsoDateTime | null;
}

export interface AccountResetRequest {
  starting_balance?: number;
}

// ---------------------------------------------------------------------------
// GET /api/equity?range=
// ---------------------------------------------------------------------------

export interface EquityPoint {
  ts: IsoDateTime;
  equity: number;
  equity_mid: number;
  cash: number;
  realized_pnl: number;
  unrealized_pnl: number;
}

// ---------------------------------------------------------------------------
// GET /api/positions
// ---------------------------------------------------------------------------

export interface Position {
  ticker: string;
  title: string;
  event_ticker: string;
  side: Side;
  count: number;
  avg_price: number;
  /** Principal of the open contracts, EXCLUDING fees (backend Position.cost_basis). */
  cost_basis: number;
  /** Entry fees of the open contracts (backend extra); unrealized = liq − cost − open_fees. */
  open_fees: number | null;
  /**
   * Average exit price: liquidation_value / count when selling the whole position into the
   * side's bid ladder (equals best_bid whenever the top level covers the position); null when
   * the side has no bid.
   */
  mark_price: number | null;
  /** Top of book (best bid) for the position's side; null when the side has no bid. */
  best_bid: number | null;
  /**
   * True while the market has closed without a result: mark_price / liquidation_value are the
   * last pre-close book (frozen until the result), not a live one.
   */
  mark_stale: boolean;
  liquidation_value: number;
  unrealized_pnl: number;
  /** Model P(side wins) at entry, if the strategy has a model. */
  fair_value: number | null;
  /** Sum of expected $ edge of the opening intents. */
  expected_edge_total: number | null;
  strategy: string;
  opened_at: IsoDateTime | null;
  close_time: IsoDateTime | null;
  yes_bid: number | null;
  yes_ask: number | null;
  url: string | null;
}

// ---------------------------------------------------------------------------
// Orders / fills / settlements (fields of ARCHITECTURE §6 + title)
// ---------------------------------------------------------------------------

export interface Order {
  id: Id;
  ticker: string;
  title: string;
  side: Side;
  action: OrderAction;
  count: number;
  filled_count: number;
  limit_price: number;
  avg_fill_price: number | null;
  tif: TimeInForce;
  status: OrderStatus;
  /** Status string exactly as the backend sent it (differs from `status` when "unknown"). */
  status_raw?: string;
  strategy: string;
  reason: string;
  expected_edge: number | null;
  fair_value: number | null;
  group_id: string | null;
  queue_ahead: number | null;
  created_at: IsoDateTime | null;
  updated_at: IsoDateTime | null;
  expires_at: IsoDateTime | null;
  fees: number;
}

/** Required timestamps (`ts`) are "" when the backend omitted them. */
export interface Fill {
  id: Id;
  order_id: Id;
  ticker: string;
  title: string;
  side: Side;
  action: OrderAction;
  count: number;
  price: number;
  fee: number;
  is_taker: boolean;
  ts: IsoDateTime;
  strategy: string;
}

export interface Settlement {
  id: Id;
  ticker: string;
  title: string;
  /**
   * "settlement" = the market resolved; "close" = the position was netted out before
   * resolution (result "closed", payout = exit proceeds). Defaults to "settlement".
   */
  kind: "settlement" | "close";
  /** Market result: "yes" | "no" | "closed" | "" | other (e.g. "void"). */
  result: string;
  side: Side;
  count: number;
  payout: number;
  /** Principal, EXCLUDING fees; pnl = payout − cost_basis − fees. */
  cost_basis: number;
  /** Fees allocated to these contracts (null when not reported). */
  fees: number | null;
  pnl: number;
  ts: IsoDateTime;
  strategy: string;
}

// ---------------------------------------------------------------------------
// GET /api/strategies, PATCH /api/strategies/{name}
// ---------------------------------------------------------------------------

export type ParamValue = number | string | boolean | null | ParamValue[] | { [k: string]: ParamValue };

/**
 * "JSON-schema-ish {name: {type, min, max, help}}" (ARCHITECTURE §7).
 * `type` is one of int|integer|float|number|bool|boolean|str|string|enum|list|array|object;
 * unknown types are edited as JSON.
 */
export interface ParamSpec {
  type: string;
  min?: number | null;
  max?: number | null;
  step?: number | null;
  help?: string | null;
  /** Allowed values (accepted aliases from the backend: `choices`, `options`). */
  enum?: ParamValue[] | null;
  default?: ParamValue;
  title?: string | null;
}

export interface StrategyStats {
  orders: number;
  fills: number;
  open_positions: number;
  settled: number;
  realized_pnl: number;
  unrealized_pnl: number;
  fees: number;
  /** Fraction in [0, 1]; null when nothing has settled. */
  win_rate: number | null;
  /** Dollars currently at risk (cost basis of open positions + reserved cash). */
  exposure: number;
}

/** The strategy's own risk limits (ARCHITECTURE §8); null fields when not reported. */
export interface StrategyRiskLimits {
  /** Cap on the strategy's exposure, % of equity. */
  max_allocation_pct: number | null;
  /** Dollars; pauses only this strategy's entries until the next UTC day. null = off. */
  daily_loss_limit: number | null;
  /** Why the strategy is paused today, or null. */
  paused: string | null;
}

export interface Strategy {
  name: string;
  description: string;
  enabled: boolean;
  /** Where `enabled` comes from: "dashboard" (saved toggle), "config", "default" (built-in); null if not reported. */
  enabled_source: string | null;
  params: Record<string, ParamValue>;
  param_schema: Record<string, ParamSpec>;
  backtestable: boolean;
  /** Not validated out of sample: forward paper-test only. */
  experimental: boolean;
  risk_limits: StrategyRiskLimits | null;
  last_tick_at: string | null;
  last_error: string | null;
  stats: StrategyStats;
}

export interface StrategyPatch {
  enabled?: boolean;
  params?: Record<string, ParamValue>;
}

// ---------------------------------------------------------------------------
// GET/PATCH /api/risk
// ---------------------------------------------------------------------------

/** Known limits from ARCHITECTURE §8 / §14; the backend may add more keys. */
export interface RiskLimits {
  max_position_cost_per_market?: number;
  max_exposure_per_event?: number;
  max_total_exposure_pct?: number;
  max_strategy_allocation_pct?: number;
  min_cash_reserve?: number;
  max_orders_per_minute?: number;
  daily_loss_limit?: number;
  min_seconds_to_close?: number;
  max_spread?: number;
  kelly_fraction?: number;
  [key: string]: number | string | boolean | null | undefined;
}

/**
 * One row of `utilization.by_event` / `utilization.by_strategy` (shape unspecified in
 * §12). `key` is filled by the client from event_ticker / strategy / name.
 */
export interface ExposureRow {
  key: string;
  exposure: number;
  /** The applicable limit in dollars, when the backend reports it. */
  limit: number | null;
  /** exposure / limit in percentage points, when known. */
  pct: number | null;
  title: string | null;
}

export interface RiskUtilization {
  total_exposure: number;
  /** Percentage points of equity. */
  total_exposure_pct: number;
  by_event: ExposureRow[];
  by_strategy: ExposureRow[];
  orders_last_minute: number;
  daily_pnl: number;
}

export interface RiskResponse {
  limits: RiskLimits;
  utilization: RiskUtilization;
  kill_switch: boolean;
  /** e.g. "daily loss limit: today's P&L -104.20 <= -100"; null when off or not reported. */
  kill_switch_reason: string | null;
}

export type RiskPatch = Partial<RiskLimits>;

// ---------------------------------------------------------------------------
// GET /api/signals, GET /api/logs
// ---------------------------------------------------------------------------

export interface Signal {
  /** Store row id (null when the backend omits it, e.g. some SSE payloads). */
  id: Id | null;
  ts: IsoDateTime;
  strategy: string;
  ticker: string;
  title: string;
  side: Side;
  /** "buy" opens / adds, "sell" closes; null when the backend did not report it. */
  action: OrderAction | null;
  /** null for malformed intents the engine rejected before sizing (never shown as 0). */
  count: number | null;
  limit_price: number | null;
  fair_value: number | null;
  /** $/contract after fees at limit_price. */
  expected_edge: number | null;
  reason: string;
  decision: SignalDecision;
  /** Decision string exactly as sent (differs from `decision` when "unknown"). */
  decision_raw?: string;
  decision_reason: string;
}

export interface LogEntry {
  id: Id | null;
  ts: IsoDateTime;
  level: LogLevel | string;
  kind: string;
  message: string;
  data: Record<string, unknown> | null;
}

// ---------------------------------------------------------------------------
// GET /api/markets
// ---------------------------------------------------------------------------

export interface MarketsQuery {
  search?: string;
  category?: string;
  sort?: MarketSort;
  limit?: number;
}

export interface MarketRow {
  ticker: string;
  event_ticker: string;
  title: string;
  category: string;
  yes_bid: number | null;
  yes_ask: number | null;
  spread: number | null;
  last_price: number | null;
  volume_24h: number;
  open_interest: number;
  close_time: IsoDateTime | null;
  url: string | null;
}

// ---------------------------------------------------------------------------
// GET /api/analytics  (ARCHITECTURE §11)
// ---------------------------------------------------------------------------

export interface Readiness {
  ready: boolean;
  reasons: string[];
  /** Headline verdict only: the strategies that are individually ready (the headline is ready when any is). */
  ready_strategies: string[];
}

/**
 * Per-strategy / overall stats over SETTLED trades. §12 only says "{...§11}", so the
 * canonical names are defined here; client.ts accepts common aliases
 * (e.g. `n`/`n_trades`/`settled` for `count`, `ci95: [lo, hi]` for `ci_low/ci_high`).
 */
export interface AnalyticsStats {
  /** Settled trades. */
  count: number;
  /** Settled contracts (null if not reported). */
  contracts: number | null;
  total_pnl: number;
  mean_pnl_per_contract: number | null;
  mean_pnl_per_trade: number | null;
  /** 95% bootstrap CI of the mean P&L, in the units of `ci_basis`. */
  ci_low: number | null;
  ci_high: number | null;
  /**
   * 95% CI of the mean P&L PER TRADE ($). The backend's readiness verdict is decided on
   * `ci_trade_low`, so the readiness wording uses this pair (null when not reported).
   */
  ci_trade_low: number | null;
  ci_trade_high: number | null;
  /**
   * Which mean the CI belongs to. Taken from the backend's `ci_basis` when sent;
   * otherwise inferred from which mean lies inside [ci_low, ci_high] (§11 defines
   * readiness on the per-trade mean, so "trade" wins ties). "unknown" when neither
   * mean is consistent with the interval: the UI then shows it in plain dollars.
   */
  ci_basis: CiBasis;
  /** Sum of expected $ edge over the trades that REPORTED one (null when none did). */
  expected_edge_total: number | null;
  /** Realized P&L of exactly those trades (like-for-like partner of expected_edge_total). */
  realized_pnl_with_edge: number | null;
  /** realized_pnl_with_edge / expected_edge_total as a fraction (backend-computed when sent). */
  edge_capture: number | null;
  /** Trades that carried an expected edge (null when not reported). */
  trades_with_edge: number | null;
  /** Realized P&L of ALL settled/closed trades. */
  realized_pnl: number;
  brier: number | null;
  win_rate: number | null;
  /** Dollars (positive magnitude). */
  max_drawdown: number | null;
  /** Percentage points (positive magnitude). */
  max_drawdown_pct: number | null;
  readiness: Readiness | null;
}

export type CiBasis = "contract" | "trade" | "unknown";

export interface CalibrationBucket {
  /** Bucket label ("0.6–0.7") or lower edge. */
  bucket: string | number;
  n: number;
  mean_fair_value: number;
  realized_rate: number;
}

/** Thresholds the backend used for the readiness verdict (`params` of GET /api/analytics). */
export interface AnalyticsParams {
  min_settled_trades: number | null;
  /** Per-strategy overrides of min_settled_trades (rare-loss strategies need far more trades). */
  min_settled_trades_by_strategy: Record<string, number>;
  /** Percentage points. */
  max_drawdown_pct: number | null;
}

export interface AnalyticsResponse {
  overall: AnalyticsStats;
  by_strategy: Record<string, AnalyticsStats>;
  calibration: CalibrationBucket[];
  readiness: Readiness;
  /** null when the backend did not send `params` (the UI then says "backend defaults"). */
  params: AnalyticsParams | null;
}

// ---------------------------------------------------------------------------
// Backtests
// ---------------------------------------------------------------------------

/** "running" is guaranteed by §12; the other states are the UI's resolution. */
export type BacktestStatus = "queued" | "running" | "done" | "failed" | (string & {});

/**
 * Metrics are open-ended (§10: P&L, per-contract EV with bootstrap CI clustered by
 * event, hit rate, max drawdown, Sharpe-like ratio). Known keys are rendered with
 * labels/units; any other numeric key is rendered generically.
 */
export interface BacktestMetrics {
  total_pnl?: number | null;
  total_return_pct?: number | null;
  final_equity?: number | null;
  n_trades?: number | null;
  contracts?: number | null;
  ev_per_contract?: number | null;
  ev_ci_low?: number | null;
  ev_ci_high?: number | null;
  hit_rate?: number | null;
  max_drawdown?: number | null;
  max_drawdown_pct?: number | null;
  sharpe?: number | null;
  fees?: number | null;
  [key: string]: unknown;
}

export interface BacktestSummary {
  id: Id;
  strategy: string;
  params: Record<string, ParamValue>;
  /** ISO date (YYYY-MM-DD) or datetime. Format with fmtCalendarDate (no TZ shift). */
  start: string | null;
  end: string | null;
  /** True when the payload had a `start` or `end` key (null there = unbounded / full dataset). */
  period_reported: boolean;
  status: BacktestStatus;
  created_at: IsoDateTime | null;
  metrics: BacktestMetrics | null;
}

export interface BacktestCreateRequest {
  strategy: string;
  params?: Record<string, ParamValue>;
  /** ISO date (YYYY-MM-DD). */
  start?: string;
  end?: string;
  starting_balance?: number;
}

export interface BacktestCreateResponse {
  id: Id;
  status: "running" | BacktestStatus;
}

export interface BacktestEquityPoint {
  ts: IsoDateTime;
  equity: number;
}

/** Trade rows are unspecified in §12; these are the fields the UI renders if present. */
export interface BacktestTrade {
  ts: IsoDateTime | null;
  ticker: string;
  event_ticker: string | null;
  side: Side | string;
  count: number;
  price: number | null;
  fee: number | null;
  /** Market result ("yes"/"no"/"void"). */
  result: string | null;
  pnl: number | null;
  settled_at: IsoDateTime | null;
  reason: string | null;
}

export interface BacktestMonth {
  /** "YYYY-MM". */
  month: string;
  pnl: number;
  trades: number | null;
  contracts: number | null;
  win_rate: number | null;
}

export interface BacktestDetail {
  id: Id;
  strategy: string;
  params: Record<string, ParamValue>;
  status: BacktestStatus;
  error: string | null;
  metrics: BacktestMetrics | null;
  equity_curve: BacktestEquityPoint[];
  trades: BacktestTrade[];
  by_month: BacktestMonth[];
  /** Not listed in §12 for the detail route; shown when present, else taken from the list row. */
  start: string | null;
  end: string | null;
  period_reported: boolean;
  created_at: IsoDateTime | null;
}

// ---------------------------------------------------------------------------
// GET /api/stream (SSE): `event: <type>\ndata: <json>`
// ---------------------------------------------------------------------------

/** Payload of `tick` (unspecified in §12; all fields optional). */
export interface TickEvent {
  ts?: IsoDateTime;
  tick_count?: number;
  universe_size?: number;
  duration_ms?: number;
  intents?: number;
  [key: string]: unknown;
}

export interface StreamEventMap {
  tick: TickEvent;
  signal: Signal;
  order: Order;
  fill: Fill;
  settlement: Settlement;
  log: LogEntry;
  /**
   * §12 does not define the SSE account payload, so only the fields actually present
   * (and finite) are kept; consumers merge it over the polled GET /api/account.
   */
  account: Partial<Account>;
}

export type StreamEventType = keyof StreamEventMap;

export const STREAM_EVENT_TYPES: readonly StreamEventType[] = [
  "tick",
  "signal",
  "order",
  "fill",
  "settlement",
  "log",
  "account",
] as const;

/** Discriminated union delivered to subscribers. `receivedAt` is client time (ms). */
export type StreamEvent = {
  [K in StreamEventType]: { type: K; data: StreamEventMap[K]; receivedAt: number; seq: number };
}[StreamEventType];

export type StreamConnectionState = "connecting" | "open" | "reconnecting" | "closed";

// ---------------------------------------------------------------------------
// GET /api/overview (docs/COINBASE_CONTRACT.md §13) — both paper venues side by side
// ---------------------------------------------------------------------------

export type VenueId = "kalshi" | "coinbase";

/**
 * One venue's summary. Money fields are null when the venue is unavailable (or the
 * backend omitted them) so the UI shows "—", never a misleading "$0.00".
 */
export interface OverviewVenue {
  venue: VenueId;
  /** "KALSHI · prediction markets" / "COINBASE · crypto spot". */
  label: string;
  available: boolean;
  /** Why the venue is unavailable (import error, disabled in config, …); null when available. */
  unavailable_reason: string | null;
  engine_running: boolean;
  kill_switch: boolean;
  starting_balance: number | null;
  equity: number | null;
  cash: number | null;
  total_pnl: number | null;
  /** Percentage points. */
  total_return_pct: number | null;
  todays_pnl: number | null;
  open_positions: number | null;
  fees_paid: number | null;
  last_error: string | null;
  /** Backend extra (not in §13): when `last_error` was recorded. */
  last_error_at: IsoDateTime | null;
  /** Backend extra (not in §13): the engine's last tick / bar. */
  last_tick_at: IsoDateTime | null;
}

export interface OverviewCombined {
  starting_balance: number | null;
  equity: number | null;
  total_pnl: number | null;
  /** Percentage points. */
  total_return_pct: number | null;
  /** Always shown next to the combined figure: "Sum of two separate paper accounts". */
  note: string;
}

export interface OverviewEquityPoint {
  ts: IsoDateTime;
  equity: number;
}

export interface OverviewResponse {
  generated_at: IsoDateTime | null;
  venues: Record<VenueId, OverviewVenue>;
  combined: OverviewCombined;
  equity_series: Record<VenueId, OverviewEquityPoint[]>;
  /**
   * Client-side marker: true when the server has no /api/overview (an older backend)
   * and this payload was assembled from the Kalshi endpoints instead.
   */
  synthesized?: boolean;
}
