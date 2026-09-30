/**
 * Types for the Coinbase PAPER venue, mirroring docs/COINBASE_CONTRACT.md §13
 * (/api/coinbase/*). Every value is normalized by ./client.ts, so pages can read any
 * field without null checks beyond the ones declared here.
 *
 * Units (contract §2 / §14):
 *  - prices are USD per 1 unit of the base currency ("$84,475.95");
 *  - quantities are in base units ("0.01234567 BTC");
 *  - money / P&L / fees are USD;
 *  - fields suffixed `_pct` are percentage points (12.5 = 12.5 %);
 *  - `*_rate` fee fields and `win_rate` / `weight*` are fractions (0.006 = 0.60 %);
 *  - timestamps are ISO-8601 UTC strings.
 */
import type { Id, IsoDateTime, ParamSpec, ParamValue } from "../types";

export type { Id, IsoDateTime, ParamSpec, ParamValue };

export type CbVenue = "coinbase";
export type CbSide = "buy" | "sell";
export type CbOrderType = "market" | "limit";
export type CbTif = "ioc" | "gtc";
export type CbOrderStatus = "open" | "partially_filled" | "filled" | "cancelled" | "expired" | "rejected" | "unknown";
export type CbDecision = "executed" | "partial" | "rejected" | "unfilled" | "resting" | "unknown";
export type CbEquityRange = "1d" | "7d" | "30d" | "all";
export type CbOrderStatusFilter = "open" | "all";
export type CbProductSort = "volume" | "spread" | "change";

// ---------------------------------------------------------------------------
// GET status · POST engine/start|stop|kill-switch
// ---------------------------------------------------------------------------

export interface CbFeeTier {
  /** Config key, e.g. "intro". */
  name: string;
  /** Human label, e.g. "Intro (US)"; falls back to the name. */
  label: string;
  /** Fractions: 0.006 = 0.60 %. */
  maker_rate: number;
  taker_rate: number;
}

export interface CbEngineStatus {
  running: boolean;
  started_at: IsoDateTime | null;
  last_tick_at: IsoDateTime | null;
  /** Close time of the last bar a strategy was evaluated on. */
  last_bar_at: IsoDateTime | null;
  tick_count: number;
  products_loaded: number;
  last_error: string | null;
  last_error_at: IsoDateTime | null;
  kill_switch: boolean;
  kill_switch_reason: string | null;
  /** null = not probed yet. */
  coinbase_reachable: boolean | null;
  strategies_enabled: string[];
}

export interface CbStatus {
  venue: CbVenue;
  mode: "paper";
  engine: CbEngineStatus;
  fee_tier: CbFeeTier;
  /** Optional backend extra: every tier the backtester accepts (see client normalization). */
  fee_tiers: CbFeeTier[];
  server_time: IsoDateTime;
}

// ---------------------------------------------------------------------------
// GET account · POST account/reset · GET equity
// ---------------------------------------------------------------------------

export interface CbAccount {
  venue: CbVenue;
  starting_balance: number;
  cash: number;
  /** Cash reserved by resting buy orders. */
  reserved_cash: number;
  /** Selling every holding into the bid ladder now (before exit fees). */
  positions_liquidation_value: number;
  positions_mid_value: number;
  /** cash + reserved_cash + positions_liquidation_value. */
  equity: number;
  equity_mid: number;
  realized_pnl: number;
  unrealized_pnl: number;
  fees_paid: number;
  total_pnl: number;
  total_return_pct: number;
  todays_pnl: number;
  /** Positive magnitude, percentage points. */
  max_drawdown_pct: number;
  open_positions: number;
  open_orders: number;
  trades: number;
  /** Fraction; null when no round trip has closed. */
  win_rate: number | null;
  /** Snapshot time (SSE extra). */
  ts?: IsoDateTime | null;
}

export interface CbEquityPoint {
  ts: IsoDateTime;
  equity: number;
  equity_mid: number | null;
  cash: number | null;
  realized_pnl: number | null;
  unrealized_pnl: number | null;
}

// ---------------------------------------------------------------------------
// Portfolio
// ---------------------------------------------------------------------------

export interface CbPosition {
  venue: CbVenue;
  product_id: string;
  base_currency: string;
  strategy: string;
  /** Base units held. */
  quantity: number;
  /** USD per unit, INCLUDING buy fees. */
  avg_cost: number;
  cost_basis: number;
  /** Liquidation price per unit (walk of the bid ladder / quantity); null without bids. */
  mark_price: number | null;
  best_bid: number | null;
  liquidation_value: number;
  mid_value: number | null;
  unrealized_pnl: number;
  unrealized_pnl_pct: number | null;
  realized_pnl: number;
  fees_paid: number;
  /** Fraction of the strategy's allocation equity. */
  weight_of_strategy: number | null;
  opened_at: IsoDateTime | null;
  url: string | null;
}

export interface CbOrder {
  venue: CbVenue;
  id: Id;
  product_id: string;
  side: CbSide;
  order_type: CbOrderType;
  tif: CbTif;
  post_only: boolean;
  /** Buys by USD amount (incl. fee). */
  quote_size: number | null;
  base_size: number | null;
  limit_price: number | null;
  filled_base: number;
  filled_quote: number;
  avg_fill_price: number | null;
  fees: number;
  status: CbOrderStatus;
  status_raw: string;
  strategy: string;
  reason: string;
  created_at: IsoDateTime | null;
  updated_at: IsoDateTime | null;
  expires_at: IsoDateTime | null;
}

export interface CbFill {
  venue: CbVenue;
  id: Id;
  order_id: Id;
  product_id: string;
  side: CbSide;
  base_size: number;
  price: number;
  notional: number;
  fee: number;
  /** Fraction (0.012 = 1.20 %); derived from fee / notional when not reported. */
  fee_rate: number | null;
  is_taker: boolean;
  ts: IsoDateTime;
  strategy: string;
}

// ---------------------------------------------------------------------------
// Strategies
// ---------------------------------------------------------------------------

export interface CbStrategyStats {
  orders: number;
  fills: number;
  open_positions: number;
  realized_pnl: number;
  unrealized_pnl: number;
  fees: number;
  /** Market value currently held (USD). */
  exposure: number;
  /** Share of Coinbase equity this strategy may use (percentage points); null = not reported. */
  allocation_pct: number | null;
  last_bar_at: IsoDateTime | null;
  last_error: string | null;
  /** Closed round trips (sells); null when the backend does not report it. */
  trades: number | null;
  /** Fraction in [0, 1] of closed round trips with positive realized P&L; null = none / not reported. */
  win_rate: number | null;
}

export interface CbStrategy {
  venue: CbVenue;
  name: string;
  description: string;
  experimental: boolean;
  enabled: boolean;
  enabled_source: string | null;
  params: Record<string, ParamValue>;
  param_schema: Record<string, ParamSpec>;
  /** Seconds per bar (3600 = 1h, 86400 = 1d). */
  bar_granularity_s: number;
  universe: string[];
  backtestable: boolean;
  stats: CbStrategyStats;
}

export interface CbStrategyPatch {
  enabled?: boolean;
  params?: Record<string, ParamValue>;
}

// ---------------------------------------------------------------------------
// Risk
// ---------------------------------------------------------------------------

/** Contract §11 limits; the backend may add more keys. */
export interface CbRiskLimits {
  max_position_pct_per_product?: number;
  max_total_exposure_pct?: number;
  max_strategy_allocation_pct?: number;
  min_cash_reserve?: number;
  max_orders_per_minute?: number;
  daily_loss_limit?: number;
  max_spread_bps?: number;
  min_trade_usd?: number;
  [key: string]: number | string | boolean | null | undefined;
}

export interface CbExposureRow {
  /** product_id or strategy name. */
  key: string;
  exposure: number;
  /** Percentage points of Coinbase equity, when known. */
  pct: number | null;
  /** Applicable limit, % of equity, when known. */
  limit_pct: number | null;
}

export interface CbRiskUtilization {
  total_exposure: number;
  total_exposure_pct: number;
  by_product: CbExposureRow[];
  by_strategy: CbExposureRow[];
  orders_last_minute: number;
  daily_pnl: number;
}

export interface CbRisk {
  venue: CbVenue;
  limits: CbRiskLimits;
  utilization: CbRiskUtilization;
  kill_switch: boolean;
  kill_switch_reason: string | null;
}

export type CbRiskPatch = Partial<CbRiskLimits>;

// ---------------------------------------------------------------------------
// Signals, logs, products
// ---------------------------------------------------------------------------

export interface CbSignal {
  venue: CbVenue;
  id: Id | null;
  ts: IsoDateTime;
  strategy: string;
  product_id: string;
  side: CbSide | null;
  /** Fraction of the strategy's allocation (0..1). */
  target_weight: number | null;
  quote_size: number | null;
  base_size: number | null;
  limit_price: number | null;
  expected_edge_bps: number | null;
  reason: string;
  decision: CbDecision;
  decision_raw: string;
  decision_reason: string;
  order_id: Id | null;
}

export interface CbLog {
  venue: CbVenue;
  id: Id | null;
  ts: IsoDateTime;
  level: string;
  kind: string;
  message: string;
  data: Record<string, unknown> | null;
}

export interface CbProductsQuery {
  search?: string;
  sort?: CbProductSort;
  limit?: number;
}

export interface CbProductRow {
  venue: CbVenue;
  product_id: string;
  base_currency: string;
  price: number | null;
  bid: number | null;
  ask: number | null;
  spread_bps: number | null;
  change_24h_pct: number | null;
  volume_24h_usd: number | null;
  tradable: boolean;
  url: string | null;
}

// ---------------------------------------------------------------------------
// Analytics
// ---------------------------------------------------------------------------

export interface CbAnalyticsStats {
  trades: number;
  total_pnl: number;
  /** Percentage points; null when not reported. */
  return_pct: number | null;
  sharpe: number | null;
  max_drawdown_pct: number | null;
  fees: number;
  /** Traded notional / average equity (× per year when the backend annualizes). */
  turnover: number | null;
  /** Other numeric keys the backend sent (shown generically). */
  extra: Record<string, number>;
}

export interface CbReadiness {
  ready: boolean;
  reasons: string[];
}

export interface CbAnalytics {
  venue: CbVenue;
  overall: CbAnalyticsStats;
  by_strategy: Record<string, CbAnalyticsStats>;
  benchmark: { btc_buy_hold_return_pct: number | null; since: IsoDateTime | null };
  readiness: CbReadiness;
}

// ---------------------------------------------------------------------------
// Backtests
// ---------------------------------------------------------------------------

export type CbBacktestStatus = "queued" | "running" | "done" | "failed" | (string & {});

/**
 * Open-ended (contract §12). Known keys are labeled in the UI; others are shown
 * generically. Aliases are folded by the client (e.g. `cagr` → `cagr_pct`).
 */
export interface CbBacktestMetrics {
  total_return_pct?: number | null;
  total_pnl?: number | null;
  final_equity?: number | null;
  cagr_pct?: number | null;
  volatility_pct?: number | null;
  sharpe?: number | null;
  sortino?: number | null;
  max_drawdown_pct?: number | null;
  turnover_per_year?: number | null;
  fees?: number | null;
  pct_time_invested?: number | null;
  trades?: number | null;
  win_rate?: number | null;
  [key: string]: unknown;
}

export interface CbBacktestSummary {
  id: Id;
  strategy: string;
  params: Record<string, ParamValue>;
  start: string | null;
  end: string | null;
  period_reported: boolean;
  status: CbBacktestStatus;
  created_at: IsoDateTime | null;
  fee_tier: string | null;
  starting_balance: number | null;
  metrics: CbBacktestMetrics | null;
}

export interface CbBacktestCreateRequest {
  strategy: string;
  params?: Record<string, ParamValue>;
  start?: string;
  end?: string;
  starting_balance?: number;
  fee_tier?: string;
}

export interface CbBacktestCreateResponse {
  id: Id;
  status: CbBacktestStatus;
}

export interface CbCurvePoint {
  ts: IsoDateTime;
  equity: number;
  /** Backtest extras (strategy curve only). */
  exposure_pct?: number | null;
  drawdown_pct?: number | null;
}

export interface CbBacktestTrade {
  ts: IsoDateTime | null;
  product_id: string;
  side: CbSide | null;
  base_size: number | null;
  price: number | null;
  notional: number | null;
  fee: number | null;
  /** Realized P&L of a sell (null for buys / not reported). */
  pnl: number | null;
  fee_rate: number | null;
  slippage_bps: number | null;
  target_weight: number | null;
  reason: string | null;
}

/** One row of by_year / by_month. `period` is "2025" or "2025-03". */
export interface CbPeriodReturn {
  period: string;
  /** Strategy return over the period, percentage points. */
  return_pct: number | null;
  pnl: number | null;
  btc_return_pct: number | null;
  equal_weight_return_pct: number | null;
  trades: number | null;
  fees: number | null;
}

export interface CbBacktestDetail extends CbBacktestSummary {
  error: string | null;
  equity_curve: CbCurvePoint[];
  benchmarks: { btc: CbCurvePoint[]; equal_weight: CbCurvePoint[] };
  /** Benchmark metrics when the backend reports them (else computed from the curves). */
  benchmark_metrics: { btc: CbBacktestMetrics | null; equal_weight: CbBacktestMetrics | null };
  trades: CbBacktestTrade[];
  by_year: CbPeriodReturn[];
  by_month: CbPeriodReturn[];
  universe: string[];
  granularity_s: number | null;
  /** Strategy decisions during the replay (rejections, skips…), when reported. */
  signals: CbSignal[];
  /**
   * `metrics.details` from the backtester (dataset, slippage, options, known biases,
   * skip reasons, errors…): shown as reported.
   */
  details: Record<string, unknown> | null;
}

// ---------------------------------------------------------------------------
// GET stream (SSE)
// ---------------------------------------------------------------------------

export interface CbTickEvent {
  ts?: IsoDateTime;
  tick_count?: number;
  products_loaded?: number;
  [key: string]: unknown;
}

export interface CbBarEvent {
  ts: IsoDateTime | null;
  strategy: string;
  bar_end: IsoDateTime | null;
  granularity_s: number | null;
  products: number | null;
  intents: number | null;
  [key: string]: unknown;
}

export interface CbStreamEventMap {
  tick: CbTickEvent;
  signal: CbSignal;
  order: CbOrder;
  fill: CbFill;
  log: CbLog;
  /** Only the fields actually present (merged over the polled account). */
  account: Partial<CbAccount>;
  bar: CbBarEvent;
}

export type CbStreamEventType = keyof CbStreamEventMap;

export const CB_STREAM_EVENT_TYPES: readonly CbStreamEventType[] = ["tick", "signal", "order", "fill", "log", "account", "bar"] as const;

export type CbStreamEvent = {
  [K in CbStreamEventType]: { type: K; data: CbStreamEventMap[K]; receivedAt: number; seq: number };
}[CbStreamEventType];

export type CbStreamState = "connecting" | "open" | "reconnecting" | "closed";
