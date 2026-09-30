/**
 * MOCK MODE for the Coinbase venue (VITE_MOCK=1): an in-browser fake of every
 * /api/coinbase/* endpoint (contract §13) plus a simulated SSE stream.
 *
 * The state is self-consistent: hourly price paths for a spot universe, a trade log
 * replayed into positions (avg cost incl. buy fees, realized P&L on sells), a USD cash
 * pool with cash reserved by a resting post-only order, an equity history computed
 * from those holdings, signals with every decision kind, risk utilization, analytics
 * vs BTC buy-and-hold, and backtests whose strategy/BTC/equal-weight curves come from
 * a simulated market. Fees follow the default "intro" tier (maker 0.50 %, taker 0.90 %).
 *
 * Responses are plain JSON objects (deep-copied) that go through the same
 * normalizers as real responses. Only loaded when IS_MOCK is true.
 */
import type { CbStreamHandlers, CbStreamSource } from "./stream";
import type { CbStreamEventType, ParamSpec, ParamValue } from "./types";

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

let rand = mulberry32(20260927);
const r = () => rand();
const rbetween = (a: number, b: number) => a + r() * (b - a);
const rint = (a: number, b: number) => Math.floor(rbetween(a, b + 1));
function rgauss(): number {
  const u = Math.max(1e-9, r());
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * r());
}
function rchoice<T>(xs: readonly T[]): T {
  const x = xs[Math.floor(r() * xs.length)];
  if (x === undefined) throw new Error("rchoice on empty list");
  return x;
}
const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
const iso = (ms: number) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
const clone = <T>(x: T): T => JSON.parse(JSON.stringify(x)) as T;
const q2 = (x: number) => Math.round(x * 100) / 100;
const q4 = (x: number) => Math.round(x * 1e4) / 1e4;
const floor8 = (x: number) => Math.floor(x * 1e8 + 1e-6) / 1e8;
const ceilCent = (x: number) => Math.ceil(x * 100 - 1e-9) / 100;
/** Price rounded to a plausible quote increment for its magnitude. */
const qPrice = (p: number) => (p >= 1 ? q2(p) : p >= 0.01 ? q4(p) : Math.round(p * 1e8) / 1e8);

/** Mirrors kalshibot/coinbase/fees.py FEE_TIERS (default "intro"). */
const FEE_TIERS = [
  { name: "intro", label: "Intro (US)", maker_rate: 0.005, taker_rate: 0.009 },
  { name: "intro_eu", label: "Intro (EU/UK/CA)", maker_rate: 0.0025, taker_rate: 0.005 },
  { name: "intro_intl", label: "Intro (rest of world)", maker_rate: 0.0009, taker_rate: 0.001 },
  { name: "intro_pre_2026_09", label: "Intro (before 2026-09-16)", maker_rate: 0.006, taker_rate: 0.012 },
  { name: "vip_8", label: "VIP 8", maker_rate: 0, taker_rate: 0.0002 },
];
const MAKER = 0.005;
const TAKER = 0.009;

// ---------------------------------------------------------------------------
// Products & price paths
// ---------------------------------------------------------------------------

interface MProduct {
  product_id: string;
  base: string;
  price: number;
  spreadBps: number;
  vol24: number;
  /** Hourly volatility (fraction). */
  sigma: number;
  tradable: boolean;
  /** Hourly closes, oldest first, the last one = current price at build time. */
  path: number[];
}

const HISTORY_H = 30 * 24;

const PRODUCT_SEED: [string, number, number, number, number, boolean?][] = [
  // id, price, spread bps, 24h USD volume, hourly sigma, tradable
  ["BTC-USD", 84475.95, 0.4, 1.9e9, 0.006],
  ["ETH-USD", 3251.37, 0.9, 9.4e8, 0.008],
  ["SOL-USD", 182.44, 2.1, 4.1e8, 0.011],
  ["XRP-USD", 0.6231, 3.2, 2.6e8, 0.011],
  ["DOGE-USD", 0.1234, 4.1, 1.9e8, 0.013],
  ["ADA-USD", 0.4518, 6.6, 7.2e7, 0.012],
  ["AVAX-USD", 32.17, 6.2, 5.9e7, 0.013],
  ["LINK-USD", 15.23, 6.6, 6.4e7, 0.012],
  ["LTC-USD", 82.46, 4.9, 4.8e7, 0.01],
  ["DOT-USD", 6.112, 16.4, 2.1e7, 0.012],
  ["BCH-USD", 421.8, 9.5, 2.4e7, 0.011],
  ["SHIB-USD", 0.00001812, 55.2, 1.6e7, 0.015],
  ["UNI-USD", 8.412, 23.8, 1.2e7, 0.013],
  ["XLM-USD", 0.1107, 18.1, 9.8e6, 0.011],
  ["AMP-USD", 0.004312, 92.8, 4.1e5, 0.016, false],
];

function buildProducts(): MProduct[] {
  return PRODUCT_SEED.map(([pid, price, spread, vol, sigma, tradable]) => {
    const path = new Array<number>(HISTORY_H + 1);
    path[HISTORY_H] = price;
    for (let i = HISTORY_H; i > 0; i--) path[i - 1] = path[i]! / Math.exp(0.00004 + sigma * rgauss());
    return { product_id: pid, base: pid.split("-")[0] ?? pid, price, spreadBps: spread, vol24: vol, sigma, tradable: tradable ?? true, path };
  });
}

const bidOf = (p: MProduct) => qPrice(p.price * (1 - p.spreadBps / 2 / 1e4));
const askOf = (p: MProduct) => qPrice(p.price * (1 + p.spreadBps / 2 / 1e4));
/** Price `hoursAgo` hours before build time (from the path; live price when 0). */
function priceAt(p: MProduct, hoursAgo: number): number {
  if (hoursAgo <= 0) return p.price;
  return p.path[Math.max(0, HISTORY_H - Math.round(hoursAgo))] ?? p.price;
}
/** What selling `qty` into the bids fetches (thin ladders cost a little depth). */
function liquidation(p: MProduct, qty: number): number {
  const notional = qty * bidOf(p);
  const depthUsd = p.vol24 / 2000;
  const slip = Math.min(0.01, (notional / Math.max(depthUsd, 1)) * 0.0005);
  return notional * (1 - slip);
}

// ---------------------------------------------------------------------------
// Strategies
// ---------------------------------------------------------------------------

interface MStrategy {
  name: string;
  description: string;
  experimental: boolean;
  enabled: boolean;
  enabled_source: string;
  params: Record<string, ParamValue>;
  param_schema: Record<string, ParamSpec>;
  bar_granularity_s: number;
  universe: string[];
  backtestable: boolean;
  allocation_pct: number;
  last_error: string | null;
}

function buildStrategies(): MStrategy[] {
  return [
    {
      name: "trend_sma",
      description:
        "Daily trend filter: hold BTC and ETH while the daily close is above its simple moving average, otherwise sit in cash. Aims to cut drawdowns, not to beat holding in bull markets.",
      experimental: false,
      enabled: true,
      enabled_source: "config",
      params: { sma_days: 100, weights: { "BTC-USD": 0.6, "ETH-USD": 0.4 }, confirm_bars: 1 },
      param_schema: {
        sma_days: { type: "int", min: 20, max: 300, help: "Moving-average length in daily bars" },
        weights: { type: "object", help: "Target weight per product while its trend is up (sum ≤ 1)" },
        confirm_bars: { type: "int", min: 1, max: 5, help: "Closes beyond the SMA needed before switching" },
      },
      bar_granularity_s: 86400,
      universe: ["BTC-USD", "ETH-USD"],
      backtestable: true,
      allocation_pct: 50,
      last_error: null,
    },
    {
      name: "momentum_rotation",
      description:
        "Weekly rotation into the top-K large caps by trailing return, equal-weighted, with an absolute-momentum filter that moves to cash when the leaders are falling.",
      experimental: false,
      enabled: true,
      enabled_source: "dashboard",
      params: { lookback_days: 30, top_k: 3, rebalance_days: 7, abs_momentum_filter: true },
      param_schema: {
        lookback_days: { type: "int", min: 7, max: 180, help: "Trailing return window" },
        top_k: { type: "int", min: 1, max: 6, help: "Products held at a time" },
        rebalance_days: { type: "int", min: 1, max: 30, help: "Days between rebalances" },
        abs_momentum_filter: { type: "bool", help: "Hold cash instead of a leader whose own trailing return is negative" },
      },
      bar_granularity_s: 86400,
      universe: ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD", "ADA-USD", "AVAX-USD", "LINK-USD", "LTC-USD", "DOT-USD"],
      backtestable: true,
      allocation_pct: 45,
      last_error: null,
    },
    {
      name: "vol_target_btc",
      description: "Holds BTC sized so its trailing volatility matches a target (e.g. 40 % a year); de-risks automatically in turbulent markets.",
      experimental: false,
      enabled: false,
      enabled_source: "default",
      params: { target_vol_pct: 40, lookback_days: 30, max_weight: 1 },
      param_schema: {
        target_vol_pct: { type: "float", min: 5, max: 150, step: 5, help: "Annualized volatility target, %" },
        lookback_days: { type: "int", min: 10, max: 120 },
        max_weight: { type: "float", min: 0.1, max: 1, step: 0.05, help: "Cap on the BTC weight" },
      },
      bar_granularity_s: 86400,
      universe: ["BTC-USD"],
      backtestable: true,
      allocation_pct: 30,
      last_error: null,
    },
    {
      name: "mean_reversion_1h",
      description:
        "Hourly z-score reversion on BTC and ETH with post-only (maker) entries. With 0.50 % maker and 0.90 % taker fees the edge per trade must be large; forward paper-test only.",
      experimental: true,
      enabled: false,
      enabled_source: "default",
      params: { z_entry: 2.5, lookback_bars: 48, max_hold_bars: 24, execution: "maker_then_taker" },
      param_schema: {
        z_entry: { type: "float", min: 1, max: 5, step: 0.1, help: "Enter when price is this many std devs below its mean" },
        lookback_bars: { type: "int", min: 12, max: 240 },
        max_hold_bars: { type: "int", min: 1, max: 168 },
        execution: { type: "enum", enum: ["maker_then_taker", "taker"], help: "Post at the bid first, then cross after the timeout" },
      },
      bar_granularity_s: 3600,
      universe: ["BTC-USD", "ETH-USD"],
      backtestable: true,
      allocation_pct: 10,
      last_error: "ETH-USD: candles 2 bars stale after Coinbase 503 (retrying)",
    },
    {
      name: "btc_hold",
      description:
        "Benchmark: buys BTC once and holds — the bar every other Coinbase strategy has to beat. Target 100% BTC-USD of its allocation; never sells, never tops up.",
      experimental: false,
      enabled: false,
      enabled_source: "dashboard",
      params: {},
      param_schema: {},
      bar_granularity_s: 86400,
      universe: ["BTC-USD"],
      backtestable: true,
      allocation_pct: 15,
      last_error: null,
    },
  ];
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

type Raw = Record<string, unknown>;

interface MTrade {
  t: number;
  strategy: string;
  pid: string;
  side: "buy" | "sell";
  qty: number;
  price: number;
  fee: number;
  taker: boolean;
  orderId: number;
}

interface MPos {
  strategy: string;
  pid: string;
  qty: number;
  cost: number;
  fees: number;
  realized: number;
  openedAt: number | null;
}

interface MBacktest {
  summary: Raw;
  detail: Raw | null;
  finishAt: number;
  seed: number;
}

interface MState {
  startedAt: number;
  startingBalance: number;
  cash: number;
  products: MProduct[];
  strategies: MStrategy[];
  trades: MTrade[];
  /** Public fill rows, newest first. */
  fills: Raw[];
  orders: Raw[];
  signals: Raw[];
  logs: Raw[];
  equity: Raw[];
  risk: Record<string, number>;
  backtests: MBacktest[];
  /** Realized P&L of each closed round trip, by strategy. */
  closedTrips: { strategy: string; pnl: number }[];
  engine: {
    running: boolean;
    started_at: number | null;
    last_tick_at: number | null;
    last_bar_at: number | null;
    tick_count: number;
    kill_switch: boolean;
    kill_switch_reason: string;
    last_error: string | null;
    last_error_at: number | null;
  };
  nextOrderId: number;
  nextFillId: number;
  nextSignalId: number;
  nextLogId: number;
}

let state: MState | null = null;

const DEFAULT_RISK = {
  max_position_pct_per_product: 50,
  max_total_exposure_pct: 90,
  max_strategy_allocation_pct: 50,
  min_cash_reserve: 20,
  max_orders_per_minute: 20,
  daily_loss_limit: 100,
  max_spread_bps: 50,
  min_trade_usd: 10,
};

const productOf = (st: MState, pid: string) => st.products.find((p) => p.product_id === pid);

function positionsOf(st: MState): MPos[] {
  const m = new Map<string, MPos>();
  for (const t of [...st.trades].sort((a, b) => a.t - b.t)) {
    const k = `${t.strategy}|${t.pid}`;
    let p = m.get(k);
    if (!p) {
      p = { strategy: t.strategy, pid: t.pid, qty: 0, cost: 0, fees: 0, realized: 0, openedAt: null };
      m.set(k, p);
    }
    const notional = t.qty * t.price;
    p.fees += t.fee;
    if (t.side === "buy") {
      if (p.qty <= 1e-12) p.openedAt = t.t;
      p.qty += t.qty;
      p.cost += notional + t.fee;
    } else {
      const avg = p.qty > 0 ? p.cost / p.qty : 0;
      const q = Math.min(t.qty, p.qty);
      p.realized += notional - t.fee - avg * q;
      p.cost -= avg * q;
      p.qty -= q;
      if (p.qty <= 1e-10) {
        p.qty = 0;
        p.cost = 0;
      }
    }
  }
  return [...m.values()];
}

const isResting = (o: Raw) => o.tif === "gtc" && (o.status === "open" || o.status === "partially_filled");
function reservedCash(st: MState): number {
  return st.orders.filter((o) => isResting(o) && o.side === "buy").reduce((a, o) => a + (Number(o.quote_size) || 0) - (Number(o.filled_quote) || 0), 0);
}

/** Record a fill: trade log, cash, order + fill rows. Returns the fill row. */
function execute(st: MState, t: Omit<MTrade, "fee" | "orderId">, order: Raw): Raw {
  const notional = t.qty * t.price;
  const fee = ceilCent(notional * (t.taker ? TAKER : MAKER));
  const trade: MTrade = { ...t, fee, orderId: Number(order.id) };
  st.trades.push(trade);
  if (t.side === "buy") st.cash -= notional + fee;
  else st.cash += notional - fee;
  const filledBase = (Number(order.filled_base) || 0) + t.qty;
  const filledQuote = (Number(order.filled_quote) || 0) + notional + (t.side === "buy" ? fee : 0);
  order.filled_base = floor8(filledBase);
  order.filled_quote = q2(filledQuote);
  order.avg_fill_price = qPrice(t.price);
  order.fees = q2((Number(order.fees) || 0) + fee);
  order.updated_at = iso(t.t);
  const fill = {
    venue: "coinbase",
    id: st.nextFillId++,
    order_id: order.id,
    product_id: t.pid,
    side: t.side,
    base_size: floor8(t.qty),
    price: qPrice(t.price),
    notional: q2(notional),
    fee,
    fee_rate: t.taker ? TAKER : MAKER,
    is_taker: t.taker,
    ts: iso(t.t),
    strategy: t.strategy,
  };
  return fill;
}

function newOrder(st: MState, p: Partial<Raw> & { product_id: string; side: string; strategy: string; t: number }): Raw {
  const o: Raw = {
    venue: "coinbase",
    id: st.nextOrderId++,
    product_id: p.product_id,
    side: p.side,
    order_type: p.order_type ?? "market",
    tif: p.tif ?? "ioc",
    post_only: p.post_only ?? false,
    quote_size: p.quote_size ?? null,
    base_size: p.base_size ?? null,
    limit_price: p.limit_price ?? null,
    filled_base: 0,
    filled_quote: 0,
    avg_fill_price: null,
    fees: 0,
    status: p.status ?? "filled",
    strategy: p.strategy,
    reason: p.reason ?? "",
    created_at: iso(p.t),
    updated_at: iso(p.t),
    expires_at: p.expires_at ?? null,
  };
  st.orders.unshift(o);
  return o;
}

function pushSignal(st: MState, s: Raw & { t: number }): Raw {
  const { t, ...rest } = s;
  const row: Raw = {
    venue: "coinbase",
    id: st.nextSignalId++,
    ts: iso(t),
    strategy: "",
    product_id: "",
    side: null,
    target_weight: null,
    quote_size: null,
    base_size: null,
    limit_price: null,
    expected_edge_bps: null,
    reason: "",
    decision: "executed",
    decision_reason: "",
    order_id: null,
    ...rest,
  };
  st.signals.unshift(row);
  if (st.signals.length > 2000) st.signals.length = 2000;
  return row;
}

function pushLog(st: MState, level: string, kind: string, message: string, data: Raw | null = null, t = Date.now(), live = true) {
  const e = { venue: "coinbase", id: st.nextLogId++, ts: iso(t), level, kind, message, data };
  st.logs.unshift(e);
  if (st.logs.length > 1500) st.logs.length = 1500;
  if (live) emit("log", { ...e, id: null });
}

/** Buy by quote (USD incl. fee) as a taker at the ask. */
function scriptedBuy(st: MState, strategy: string, pid: string, quote: number, t: number, hoursAgo: number, reason: string, weight: number) {
  const p = productOf(st, pid)!;
  const px = qPrice(priceAt(p, hoursAgo) * (1 + p.spreadBps / 2 / 1e4));
  const qty = floor8(quote / (1 + TAKER) / px);
  const o = newOrder(st, { product_id: pid, side: "buy", strategy, t, quote_size: quote, reason });
  const f = execute(st, { t, strategy, pid, side: "buy", qty, price: px, taker: true }, o);
  st.fills.unshift(f);
  pushSignal(st, {
    t: t - 400,
    strategy,
    product_id: pid,
    side: "buy",
    target_weight: weight,
    quote_size: quote,
    reason,
    expected_edge_bps: null,
    decision: "executed",
    decision_reason: `filled ${f.base_size} ${p.base} @ $${px} (taker, fee $${f.fee})`,
    order_id: o.id,
  });
}

function scriptedSell(st: MState, strategy: string, pid: string, t: number, hoursAgo: number, reason: string) {
  const p = productOf(st, pid)!;
  const pos = positionsOf(st).find((x) => x.strategy === strategy && x.pid === pid);
  if (!pos || pos.qty <= 0) return;
  const px = qPrice(priceAt(p, hoursAgo) * (1 - p.spreadBps / 2 / 1e4));
  const o = newOrder(st, { product_id: pid, side: "sell", strategy, t, base_size: pos.qty, reason });
  const avg = pos.cost / pos.qty;
  const f = execute(st, { t, strategy, pid, side: "sell", qty: pos.qty, price: px, taker: true }, o);
  st.fills.unshift(f);
  st.closedTrips.push({ strategy, pnl: Number(f.notional) - Number(f.fee) - avg * pos.qty });
  pushSignal(st, {
    t: t - 400,
    strategy,
    product_id: pid,
    side: "sell",
    target_weight: 0,
    base_size: pos.qty,
    reason,
    decision: "executed",
    decision_reason: `sold ${floor8(pos.qty)} ${p.base} @ $${px} (taker, fee $${f.fee})`,
    order_id: o.id,
  });
}

function buildState(now: number, startingBalance = 1000, empty = false): MState {
  rand = mulberry32(20260927);
  const products = state?.products ?? buildProducts();
  const st: MState = {
    startedAt: empty ? now : now - 30 * DAY,
    startingBalance,
    cash: startingBalance,
    products,
    strategies: state?.strategies ?? buildStrategies(),
    trades: [],
    fills: [],
    orders: [],
    signals: [],
    logs: [],
    equity: [],
    risk: state?.risk ?? { ...DEFAULT_RISK },
    backtests: state?.backtests ?? [],
    closedTrips: [],
    engine: {
      running: !empty,
      started_at: empty ? null : now - 3 * DAY - 2 * HOUR,
      last_tick_at: empty ? null : now - 4000,
      last_bar_at: empty ? null : Math.floor(now / DAY) * DAY,
      tick_count: empty ? 0 : 5190,
      kill_switch: false,
      kill_switch_reason: "",
      last_error: empty ? null : "GET /products/ETH-USD/candles: HTTP 503 (retry 2/5 succeeded)",
      last_error_at: empty ? null : now - 5 * HOUR,
    },
    nextOrderId: 1,
    nextFillId: 1,
    nextSignalId: 1,
    nextLogId: 1,
  };
  if (empty) {
    pushLog(st, "info", "account", `Coinbase paper account reset to $${startingBalance.toFixed(2)}`, null, now, false);
    st.equity.push({ ts: iso(now), equity: startingBalance, equity_mid: startingBalance, cash: startingBalance, realized_pnl: 0, unrealized_pnl: 0 });
    return st;
  }

  const at = (daysAgo: number, hour = 0) => Math.floor((now - daysAgo * DAY) / DAY) * DAY + hour * HOUR + 30_000 + rint(0, 20_000);
  const h = (t: number) => (now - t) / HOUR;
  // trend_sma: BTC/ETH above their SMA at the start; ETH drops below on day -10, recovers day -3.
  let t = at(29);
  scriptedBuy(st, "trend_sma", "BTC-USD", 300, t, h(t), "BTC close above 100-day SMA: target 60 % of allocation", 0.6);
  scriptedBuy(st, "trend_sma", "ETH-USD", 200, t + 2000, h(t), "ETH close above 100-day SMA: target 40 % of allocation", 0.4);
  t = at(10);
  scriptedSell(st, "trend_sma", "ETH-USD", t, h(t), "ETH closed 1.8 % below its 100-day SMA: exit to cash");
  t = at(3);
  scriptedBuy(st, "trend_sma", "ETH-USD", 190, t, h(t), "ETH back above its 100-day SMA (confirmed 1 bar): re-enter", 0.4);
  // momentum_rotation: weekly top-3.
  t = at(28);
  scriptedBuy(st, "momentum_rotation", "AVAX-USD", 150, t, h(t), "Top-3 by 30-day return (AVAX +21.4 %)", 1 / 3);
  scriptedBuy(st, "momentum_rotation", "DOGE-USD", 150, t + 1500, h(t), "Top-3 by 30-day return (DOGE +18.9 %)", 1 / 3);
  scriptedBuy(st, "momentum_rotation", "SOL-USD", 150, t + 3000, h(t), "Top-3 by 30-day return (SOL +16.2 %)", 1 / 3);
  t = at(21);
  scriptedSell(st, "momentum_rotation", "DOGE-USD", t, h(t), "Dropped out of the top-3 (rank 6)");
  scriptedBuy(st, "momentum_rotation", "LINK-USD", 140, t + 1800, h(t), "Entered the top-3 by 30-day return (LINK +12.7 %)", 1 / 3);
  t = at(14);
  scriptedSell(st, "momentum_rotation", "AVAX-USD", t, h(t), "Dropped out of the top-3 (rank 4)");
  scriptedBuy(st, "momentum_rotation", "XRP-USD", 140, t + 1600, h(t), "Entered the top-3 by 30-day return (XRP +9.8 %)", 1 / 3);

  // Rejections / unfilled / partial examples.
  const rejects: [number, string, string, string, string, number | null][] = [
    [at(21, 0), "momentum_rotation", "SHIB-USD", "buy", "spread 55.2 bps > max_spread_bps 50", 1 / 3],
    [at(14, 0), "momentum_rotation", "DOT-USD", "buy", "max_orders_per_minute 20 reached (throttled)", 1 / 3],
    [at(7, 0), "trend_sma", "BTC-USD", "buy", "top-up $6.12 below min_trade_usd $10 (rebalance band 2 %)", 0.6],
    [at(5, 0), "momentum_rotation", "SOL-USD", "buy", "insufficient cash: would leave $14.80 < min_cash_reserve $20", 1 / 3],
    [at(2, 0), "momentum_rotation", "AMP-USD", "buy", "product not tradable (status: delisted)", 1 / 3],
  ];
  for (const [ts, strategy, pid, side, why, w] of rejects) {
    pushSignal(st, { t: ts, strategy, product_id: pid, side, target_weight: w, quote_size: 25, reason: "rebalance toward target weight", decision: "rejected", decision_reason: why });
  }
  pushSignal(st, {
    t: at(9, 0),
    strategy: "momentum_rotation",
    product_id: "LINK-USD",
    side: "buy",
    target_weight: 1 / 3,
    quote_size: 18,
    limit_price: 14.61,
    reason: "rebalance toward target weight",
    decision: "unfilled",
    decision_reason: "IOC: ask moved to $14.69 above limit $14.61 (slippage cap 100 bps); nothing filled",
  });
  // Resting post-only buy (reserves cash).
  const sol = productOf(st, "SOL-USD")!;
  const restT = now - 18 * MIN;
  const rest = newOrder(st, {
    product_id: "SOL-USD",
    side: "buy",
    strategy: "momentum_rotation",
    t: restT,
    order_type: "limit",
    tif: "gtc",
    post_only: true,
    quote_size: 25,
    limit_price: bidOf(sol),
    status: "open",
    reason: "maker top-up toward 33 % weight (post-only at best bid)",
    expires_at: iso(restT + 3600_000),
  });
  rest.base_size = floor8(25 / (1 + MAKER) / bidOf(sol));
  st.cash -= 25;
  pushSignal(st, {
    t: restT - 300,
    strategy: "momentum_rotation",
    product_id: "SOL-USD",
    side: "buy",
    target_weight: 1 / 3,
    quote_size: 25,
    limit_price: bidOf(sol),
    expected_edge_bps: 60,
    reason: "maker top-up toward 33 % weight",
    decision: "resting",
    decision_reason: `post-only GTC at best bid $${bidOf(sol)}; queue ahead ${(rbetween(40, 180)).toFixed(3)} SOL`,
    order_id: rest.id,
  });
  // Historical cancelled / expired / rejected orders.
  const cx = newOrder(st, { product_id: "LINK-USD", side: "buy", strategy: "momentum_rotation", t: at(9, 1), order_type: "limit", tif: "gtc", post_only: true, quote_size: 20, limit_price: 14.52, status: "expired", reason: "maker top-up (expired after 3600 s)", expires_at: iso(at(9, 2)) });
  cx.base_size = 1.36;
  newOrder(st, { product_id: "XRP-USD", side: "buy", strategy: "momentum_rotation", t: at(6, 3), order_type: "limit", tif: "gtc", post_only: true, quote_size: 15, limit_price: 0.5912, status: "cancelled", reason: "cancelled by user" });
  newOrder(st, { product_id: "BTC-USD", side: "buy", strategy: "trend_sma", t: at(4, 2), order_type: "limit", tif: "gtc", post_only: true, quote_size: 30, limit_price: 83012.5, status: "rejected", reason: "post-only would cross the book (ask $83,010.01)" });
  st.orders.sort((a, b) => Date.parse(String(b.created_at)) - Date.parse(String(a.created_at)));
  st.fills.sort((a, b) => Date.parse(String(b.ts)) - Date.parse(String(a.ts)));
  st.signals.sort((a, b) => Date.parse(String(b.ts)) - Date.parse(String(a.ts)));

  // Equity history (hourly) replayed from the trade log and price paths.
  const trades = [...st.trades].sort((a, b) => a.t - b.t);
  const reservedFrom = restT;
  for (let i = 0; i <= HISTORY_H; i++) {
    const ts = now - (HISTORY_H - i) * HOUR;
    let cash = startingBalance;
    const qty = new Map<string, number>();
    const cost = new Map<string, number>();
    let realized = 0;
    for (const tr of trades) {
      if (tr.t > ts) break;
      const k = `${tr.strategy}|${tr.pid}`;
      const n = tr.qty * tr.price;
      if (tr.side === "buy") {
        cash -= n + tr.fee;
        qty.set(k, (qty.get(k) ?? 0) + tr.qty);
        cost.set(k, (cost.get(k) ?? 0) + n + tr.fee);
      } else {
        cash += n - tr.fee;
        const q = qty.get(k) ?? 0;
        const c = cost.get(k) ?? 0;
        realized += n - tr.fee - (q > 0 ? (c / q) * tr.qty : 0);
        qty.set(k, 0);
        cost.set(k, 0);
      }
    }
    let liq = 0;
    let mid = 0;
    let open = 0;
    for (const [k, q] of qty) {
      if (q <= 0) continue;
      const p = productOf(st, k.split("|")[1]!)!;
      const px = priceAt(p, HISTORY_H - i);
      liq += q * px * (1 - p.spreadBps / 2 / 1e4);
      mid += q * px;
      open += cost.get(k) ?? 0;
    }
    const reserved = ts >= reservedFrom ? 25 : 0;
    st.equity.push({
      ts: iso(ts),
      equity: q2(cash + liq),
      equity_mid: q2(cash + mid),
      cash: q2(cash - reserved),
      realized_pnl: q2(realized),
      unrealized_pnl: q2(liq - open),
    });
  }

  // Logs.
  const logLines: [number, string, string, string][] = [
    [now - 3 * DAY - 2 * HOUR, "info", "engine", "Coinbase engine started (paper mode, fee tier intro: maker 0.50 %, taker 0.90 %)"],
    [now - 3 * DAY - 2 * HOUR + 4000, "info", "products", "Loaded 15 USD products (14 tradable); next refresh in 1 h"],
    [at(3, 0), "info", "bar", "trend_sma: daily bar closed 00:00 UTC; ETH-USD above SMA → target 40 %"],
    [at(2, 0), "info", "bar", "momentum_rotation: daily bar closed; not a rebalance day (next in 5 d)"],
    [now - 5 * HOUR, "warning", "marketdata", "GET /products/ETH-USD/candles: HTTP 503 (retry 2/5 succeeded)"],
    [at(1, 0), "info", "bar", "trend_sma: daily bar closed; no change (within 2 % rebalance band)"],
    [at(0, 0), "info", "bar", "momentum_rotation: daily bar closed; no change"],
    [now - 40 * MIN, "info", "snapshot", "Equity snapshot (liquidation) recorded"],
    [restT, "info", "order", "Resting post-only BUY SOL-USD $25.00 @ best bid (momentum_rotation)"],
    [now - 2 * HOUR, "error", "strategy", "mean_reversion_1h: ETH-USD candles 2 bars stale after Coinbase 503 (retrying)"],
  ];
  for (const [ts, lvl, kind, msg] of logLines.sort((a, b) => a[0] - b[0])) pushLog(st, lvl, kind, msg, null, ts, false);
  for (const f of [...st.fills].reverse()) {
    pushLog(st, "info", "fill", `${f.strategy}: ${String(f.side).toUpperCase()} ${f.base_size} ${String(f.product_id).split("-")[0]} @ $${f.price} · fee $${Number(f.fee).toFixed(2)}`, null, Date.parse(String(f.ts)), false);
  }
  st.logs.sort((a, b) => Date.parse(String(b.ts)) - Date.parse(String(a.ts)));

  if (st.backtests.length === 0) st.backtests = seedBacktests(now);
  return st;
}

// ---------------------------------------------------------------------------
// Derived views
// ---------------------------------------------------------------------------

function equityAt(st: MState, ms: number): number | null {
  let best: Raw | null = null;
  for (const p of st.equity) {
    if (Date.parse(String(p.ts)) <= ms) best = p;
    else break;
  }
  return best ? Number(best.equity) : null;
}

function accountOf(st: MState) {
  const pos = positionsOf(st);
  let liq = 0;
  let mid = 0;
  let realized = 0;
  let unreal = 0;
  let fees = 0;
  let open = 0;
  for (const p of pos) {
    realized += p.realized;
    fees += p.fees;
    if (p.qty <= 0) continue;
    const prod = productOf(st, p.pid)!;
    const l = liquidation(prod, p.qty);
    liq += l;
    mid += p.qty * prod.price;
    unreal += l - p.cost;
    open++;
  }
  const reserved = reservedCash(st);
  const equity = st.cash + reserved + liq;
  const now = Date.now();
  const dayStart = Math.floor(now / DAY) * DAY;
  const eq0 = equityAt(st, dayStart) ?? st.startingBalance;
  let peak = st.startingBalance;
  let dd = 0;
  for (const p of [...st.equity.map((x) => Number(x.equity)), equity]) {
    peak = Math.max(peak, p);
    dd = Math.max(dd, (peak - p) / peak);
  }
  const wins = st.closedTrips.filter((x) => x.pnl > 0).length;
  return {
    venue: "coinbase",
    starting_balance: st.startingBalance,
    cash: q2(st.cash),
    reserved_cash: q2(reserved),
    positions_liquidation_value: q2(liq),
    positions_mid_value: q2(mid),
    equity: q2(equity),
    equity_mid: q2(st.cash + reserved + mid),
    realized_pnl: q2(realized),
    unrealized_pnl: q2(unreal),
    fees_paid: q2(fees),
    total_pnl: q2(equity - st.startingBalance),
    total_return_pct: q4(((equity - st.startingBalance) / st.startingBalance) * 100),
    todays_pnl: q2(equity - eq0),
    max_drawdown_pct: q4(dd * 100),
    open_positions: open,
    open_orders: st.orders.filter(isResting).length,
    trades: st.trades.length,
    win_rate: st.closedTrips.length ? wins / st.closedTrips.length : null,
    ts: iso(now),
  };
}

function statusOf(st: MState) {
  const e = st.engine;
  return {
    venue: "coinbase",
    mode: "paper",
    engine: {
      running: e.running,
      started_at: e.started_at ? iso(e.started_at) : null,
      last_tick_at: e.last_tick_at ? iso(e.last_tick_at) : null,
      last_bar_at: e.last_bar_at ? iso(e.last_bar_at) : null,
      tick_count: e.tick_count,
      products_loaded: st.products.length,
      last_error: e.last_error,
      last_error_at: e.last_error_at ? iso(e.last_error_at) : null,
      kill_switch: e.kill_switch,
      kill_switch_reason: e.kill_switch ? e.kill_switch_reason || "manual" : null,
      coinbase_reachable: true,
      strategies_enabled: st.strategies.filter((s) => s.enabled).map((s) => s.name),
    },
    fee_tier: FEE_TIERS[0],
    fee_tiers: FEE_TIERS,
    server_time: iso(Date.now()),
  };
}

function positionRows(st: MState) {
  const acct = accountOf(st);
  const rows: Raw[] = [];
  for (const p of positionsOf(st)) {
    if (p.qty <= 0) continue;
    const prod = productOf(st, p.pid)!;
    const liq = liquidation(prod, p.qty);
    const s = st.strategies.find((x) => x.name === p.strategy);
    const alloc = ((s?.allocation_pct ?? 50) / 100) * acct.equity;
    rows.push({
      venue: "coinbase",
      product_id: p.pid,
      base_currency: prod.base,
      strategy: p.strategy,
      quantity: floor8(p.qty),
      avg_cost: qPrice(p.cost / p.qty),
      cost_basis: q2(p.cost),
      mark_price: qPrice(liq / p.qty),
      best_bid: bidOf(prod),
      liquidation_value: q2(liq),
      mid_value: q2(p.qty * prod.price),
      unrealized_pnl: q2(liq - p.cost),
      unrealized_pnl_pct: q4(((liq - p.cost) / p.cost) * 100),
      realized_pnl: q2(p.realized),
      fees_paid: q2(p.fees),
      weight_of_strategy: alloc > 0 ? q4(liq / alloc) : null,
      opened_at: p.openedAt ? iso(p.openedAt) : null,
      url: `https://www.coinbase.com/advanced-trade/spot/${p.pid}`,
    });
  }
  return rows;
}

function strategyRow(st: MState, s: MStrategy) {
  const pos = positionsOf(st).filter((p) => p.strategy === s.name);
  let unreal = 0;
  let exposure = 0;
  for (const p of pos) {
    if (p.qty <= 0) continue;
    const l = liquidation(productOf(st, p.pid)!, p.qty);
    unreal += l - p.cost;
    exposure += l;
  }
  const lastBar = st.engine.last_bar_at;
  const trips = st.closedTrips.filter((x) => x.strategy === s.name);
  return {
    venue: "coinbase",
    name: s.name,
    description: s.description,
    experimental: s.experimental,
    enabled: s.enabled,
    enabled_source: s.enabled_source,
    params: s.params,
    param_schema: s.param_schema,
    bar_granularity_s: s.bar_granularity_s,
    universe: s.universe,
    backtestable: s.backtestable,
    stats: {
      orders: st.orders.filter((o) => o.strategy === s.name).length,
      fills: st.trades.filter((t) => t.strategy === s.name).length,
      open_positions: pos.filter((p) => p.qty > 0).length,
      realized_pnl: q2(pos.reduce((a, p) => a + p.realized, 0)),
      unrealized_pnl: q2(unreal),
      fees: q2(pos.reduce((a, p) => a + p.fees, 0)),
      exposure: q2(exposure),
      allocation_pct: s.allocation_pct,
      last_bar_at: s.enabled && lastBar ? iso(s.bar_granularity_s === 3600 ? Math.floor(Date.now() / HOUR) * HOUR : lastBar) : null,
      last_error: s.last_error,
      trades: trips.length,
      win_rate: trips.length ? q4(trips.filter((x) => x.pnl > 0).length / trips.length) : null,
    },
  };
}

function riskOf(st: MState) {
  const acct = accountOf(st);
  const rows = positionRows(st);
  const byProduct = new Map<string, number>();
  const byStrategy = new Map<string, number>();
  for (const p of rows) {
    byProduct.set(String(p.product_id), (byProduct.get(String(p.product_id)) ?? 0) + Number(p.liquidation_value));
    byStrategy.set(String(p.strategy), (byStrategy.get(String(p.strategy)) ?? 0) + Number(p.liquidation_value));
  }
  const total = [...byProduct.values()].reduce((a, b) => a + b, 0) + acct.reserved_cash;
  const expRow = (ids: Record<string, string>, v: number, limitPct: number) => {
    const limitUsd = (limitPct / 100) * acct.equity;
    return {
      ...ids,
      exposure: q2(v),
      limit: q2(limitUsd),
      pct: limitUsd > 0 ? q4((v / limitUsd) * 100) : null,
      limit_pct: limitPct,
      equity_pct: acct.equity > 0 ? q4((v / acct.equity) * 100) : null,
    };
  };
  const now = Date.now();
  return {
    venue: "coinbase",
    limits: st.risk,
    utilization: {
      total_exposure: q2(total),
      total_exposure_pct: q4((total / acct.equity) * 100),
      // Mirrors the backend: `pct` = exposure ÷ limit × 100, `limit` in USD, and the
      // UI extras `limit_pct` / `equity_pct` as % of Coinbase equity.
      by_product: [...byProduct].map(([k, v]) => expRow({ key: k, product_id: k }, v, Number(st.risk.max_position_pct_per_product ?? 0))),
      by_strategy: [...byStrategy].map(([k, v]) =>
        expRow(
          { key: k, strategy: k },
          v,
          Math.min(Number(st.risk.max_strategy_allocation_pct ?? 50), Number(st.strategies.find((s) => s.name === k)?.allocation_pct ?? 50)),
        ),
      ),
      orders_last_minute: st.orders.filter((o) => now - Date.parse(String(o.created_at)) < 60_000).length,
      daily_pnl: acct.todays_pnl,
    },
    kill_switch: st.engine.kill_switch,
    kill_switch_reason: st.engine.kill_switch ? st.engine.kill_switch_reason || "manual" : null,
  };
}

function sharpeOf(eq: number[], periodsPerYear: number): number | null {
  if (eq.length < 3) return null;
  const rets: number[] = [];
  for (let i = 1; i < eq.length; i++) if (eq[i - 1]! > 0) rets.push(eq[i]! / eq[i - 1]! - 1);
  const m = rets.reduce((a, b) => a + b, 0) / rets.length;
  const sd = Math.sqrt(rets.reduce((a, b) => a + (b - m) ** 2, 0) / Math.max(1, rets.length - 1));
  return sd > 0 ? (m / sd) * Math.sqrt(periodsPerYear) : null;
}

function analyticsOf(st: MState) {
  const acct = accountOf(st);
  const eq = st.equity.map((p) => Number(p.equity));
  const notional = st.trades.reduce((a, t) => a + t.qty * t.price, 0);
  const days = Math.max(1, (Date.now() - st.startedAt) / DAY);
  const btc = productOf(st, "BTC-USD")!;
  const btc0 = priceAt(btc, (Date.now() - st.startedAt) / HOUR);
  const by: Record<string, Raw> = {};
  for (const s of st.strategies) {
    const row = strategyRow(st, s);
    const pnl = row.stats.realized_pnl + row.stats.unrealized_pnl;
    const alloc = (s.allocation_pct / 100) * st.startingBalance;
    const tr = st.trades.filter((t) => t.strategy === s.name);
    by[s.name] = {
      trades: tr.length,
      total_pnl: q2(pnl),
      return_pct: tr.length ? q4((pnl / alloc) * 100) : null,
      sharpe: tr.length ? q2((sharpeOf(eq, 24 * 365) ?? 0) * (pnl >= 0 ? 1.1 : 0.7)) : null,
      max_drawdown_pct: tr.length ? q2(acct.max_drawdown_pct * (s.name === "trend_sma" ? 0.8 : 1.3)) : null,
      fees: row.stats.fees,
      turnover: tr.length ? q2((tr.reduce((a, t) => a + t.qty * t.price, 0) / alloc) * (365 / days)) : null,
    };
  }
  const reasons: string[] = [];
  if (days < 180) reasons.push(`Only ${Math.floor(days)} days of paper history; at least 180 days are needed to judge a daily-bar strategy.`);
  const btcRet = (btc.price / btc0 - 1) * 100;
  if (acct.total_return_pct < btcRet) reasons.push(`Trails BTC buy-and-hold by ${(btcRet - acct.total_return_pct).toFixed(1)} percentage points over the same period (after fees).`);
  reasons.push(`Fees are ${((acct.fees_paid / Math.max(1, Math.abs(acct.total_pnl) + acct.fees_paid)) * 100).toFixed(0)} % of gross P&L at the 0.90 % taker rate.`);
  return {
    venue: "coinbase",
    overall: {
      trades: st.trades.length,
      total_pnl: acct.total_pnl,
      return_pct: acct.total_return_pct,
      sharpe: sharpeOf(eq, 24 * 365) === null ? null : q2(sharpeOf(eq, 24 * 365)!),
      max_drawdown_pct: acct.max_drawdown_pct,
      fees: acct.fees_paid,
      turnover: q2((notional / st.startingBalance) * (365 / days)),
    },
    by_strategy: by,
    benchmark: { btc_buy_hold_return_pct: q4(btcRet), since: iso(st.startedAt) },
    readiness: { ready: false, reasons },
  };
}

function productRow(p: MProduct) {
  const open24 = p.path[Math.max(0, HISTORY_H - 24)] ?? p.price;
  return {
    venue: "coinbase",
    product_id: p.product_id,
    base_currency: p.base,
    price: qPrice(p.price),
    bid: bidOf(p),
    ask: askOf(p),
    spread_bps: q2(p.spreadBps),
    change_24h_pct: q4((p.price / open24 - 1) * 100),
    volume_24h_usd: Math.round(p.vol24),
    tradable: p.tradable,
    url: `https://www.coinbase.com/advanced-trade/spot/${p.product_id}`,
  };
}

// ---------------------------------------------------------------------------
// Backtests (simulated daily market)
// ---------------------------------------------------------------------------

function simulateBacktest(summary: Raw, seed: number): Raw {
  const rng = mulberry32(seed);
  const g = () => {
    const u = Math.max(1e-9, rng());
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * rng());
  };
  const start = Date.parse(String(summary.start ?? "2023-01-01"));
  const end = Date.parse(String(summary.end ?? "2026-08-31"));
  const n = Math.max(30, Math.round((end - start) / DAY));
  const bal = Number(summary.starting_balance ?? 1000);
  const tierName = String(summary.fee_tier ?? "intro");
  const tier = FEE_TIERS.find((f) => f.name === tierName) ?? FEE_TIERS[0]!;
  const fee = tier.taker_rate;
  const universe = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "ADA-USD", "AVAX-USD", "LINK-USD", "LTC-USD"];
  // Daily price paths: a shared market factor + idiosyncratic noise, with regimes.
  const paths = universe.map(() => [1]);
  let regime = 0.0008;
  for (let d = 1; d <= n; d++) {
    if (rng() < 0.012) regime = rng() < 0.55 ? 0.0016 : -0.0019;
    const mkt = regime + 0.03 * g();
    universe.forEach((_, i) => {
      const beta = i === 0 ? 1 : 1.25;
      const idio = i === 0 ? 0 : 0.03 * g();
      const p = paths[i]!;
      p.push(p[p.length - 1]! * Math.exp(beta * mkt + idio - (i === 0 ? 0 : 0.0006)));
    });
  }
  const btc = paths[0]!;
  /** Scales the simulated BTC index to real-looking USD prices. */
  const scale = 84475.95 / btc[n]!;
  const name = String(summary.strategy);
  const sma = Number((summary.params as Raw | undefined)?.sma_days ?? 100);
  const eq: number[] = [bal];
  const trades: Raw[] = [];
  let invested = false;
  let investedDays = 0;
  let turnover = 0;
  let fees = 0;
  let cashV = bal;
  let units = 0;
  const wins: number[] = [];
  let entryCost = 0;
  for (let d = 1; d <= n; d++) {
    const px = btc[d]!;
    const lookback = btc.slice(Math.max(0, d - Math.min(sma, 60)), d);
    const avg = lookback.reduce((a, b) => a + b, 0) / lookback.length;
    const want = name === "momentum_rotation" ? btc[d - 1]! > (btc[Math.max(0, d - 30)] ?? btc[0]!) : btc[d - 1]! > avg;
    const ts = iso(start + d * DAY);
    if (want && !invested) {
      const f = cashV * fee / (1 + fee);
      units = (cashV - f) / px;
      fees += f;
      turnover += cashV - f;
      entryCost = cashV;
      trades.push({ ts, product_id: "BTC-USD", side: "buy", base_size: floor8(units / scale), price: qPrice(px * scale), notional: q2(cashV - f), fee: q2(f), pnl: null, target_weight: 1, reason: "trend up: close above SMA" });
      cashV = 0;
      invested = true;
    } else if (!want && invested) {
      const gross = units * px;
      const f = gross * fee;
      fees += f;
      turnover += gross;
      cashV = gross - f;
      wins.push(cashV - entryCost);
      trades.push({ ts, product_id: "BTC-USD", side: "sell", base_size: floor8(units / scale), price: qPrice(px * scale), notional: q2(gross), fee: q2(f), pnl: q2(cashV - entryCost), target_weight: 0, reason: "trend down: close below SMA" });
      units = 0;
      invested = false;
    }
    if (invested) investedDays++;
    eq.push(cashV + units * px);
  }
  const bh = btc.map((p) => bal * (1 - fee) * (p / btc[0]!));
  const ew = paths[0]!.map((_, d) => (bal * (1 - fee) * paths.reduce((a, p) => a + p[d]! / p[0]!, 0)) / paths.length);
  const curve = (xs: number[]) => xs.map((v, d) => ({ ts: iso(start + d * DAY), equity: q2(v) }));
  const metricsOf = (xs: number[], extra: Raw = {}): Raw => {
    const years = n / 365;
    const final = xs[xs.length - 1]!;
    let peak = xs[0]!;
    let dd = 0;
    const rets: number[] = [];
    for (let i = 1; i < xs.length; i++) {
      peak = Math.max(peak, xs[i]!);
      dd = Math.max(dd, (peak - xs[i]!) / peak);
      rets.push(xs[i]! / xs[i - 1]! - 1);
    }
    const m = rets.reduce((a, b) => a + b, 0) / rets.length;
    const sd = Math.sqrt(rets.reduce((a, b) => a + (b - m) ** 2, 0) / (rets.length - 1));
    const dsd = Math.sqrt(rets.filter((x) => x < 0).reduce((a, b) => a + b * b, 0) / rets.length);
    return {
      total_return_pct: q2((final / bal - 1) * 100),
      total_pnl: q2(final - bal),
      final_equity: q2(final),
      cagr_pct: q2(((final / bal) ** (1 / years) - 1) * 100),
      volatility_pct: q2(sd * Math.sqrt(365) * 100),
      sharpe: q2((m / sd) * Math.sqrt(365)),
      sortino: q2(dsd > 0 ? (m / dsd) * Math.sqrt(365) : 0),
      max_drawdown_pct: q2(dd * 100),
      ...extra,
    };
  };
  const byPeriod = (len: number) => {
    const out = new Map<string, { s0: number; s1: number; b0: number; b1: number; e0: number; e1: number; trades: number; fees: number }>();
    for (let d = 0; d <= n; d++) {
      const k = iso(start + d * DAY).slice(0, len);
      const row = out.get(k);
      if (!row) out.set(k, { s0: eq[Math.max(0, d - 1)]!, s1: eq[d]!, b0: bh[Math.max(0, d - 1)]!, b1: bh[d]!, e0: ew[Math.max(0, d - 1)]!, e1: ew[d]!, trades: 0, fees: 0 });
      else {
        row.s1 = eq[d]!;
        row.b1 = bh[d]!;
        row.e1 = ew[d]!;
      }
    }
    for (const t of trades) {
      const row = out.get(String(t.ts).slice(0, len));
      if (row) {
        row.trades++;
        row.fees += Number(t.fee);
      }
    }
    return [...out].map(([k, v]) => ({
      period: k,
      return_pct: q2((v.s1 / v.s0 - 1) * 100),
      pnl: q2(v.s1 - v.s0),
      btc_return_pct: q2((v.b1 / v.b0 - 1) * 100),
      equal_weight_return_pct: q2((v.e1 / v.e0 - 1) * 100),
      trades: v.trades,
      fees: q2(v.fees),
    }));
  };
  const w = wins.filter((x) => x > 0).length;
  const btcM = metricsOf(bh, { fees: q2(bal * fee), trades: 1, pct_time_invested: 100 });
  const ewM = metricsOf(ew, { fees: q2(bal * fee), trades: universe.length, pct_time_invested: 100 });
  const main = metricsOf(eq, {
    turnover_per_year: q2(turnover / bal / (n / 365)),
    fees: q2(fees),
    pct_time_invested: q2((investedDays / n) * 100),
    trades: trades.length,
    win_rate: wins.length ? q4(w / wins.length) : null,
  });
  // Same nesting as kalshibot/coinbase/backtest.py: benchmarks + details inside metrics.
  const metrics: Raw = {
    ...main,
    excess_return_vs_btc_pct: q2(Number(main.total_return_pct) - Number(btcM.total_return_pct)),
    excess_return_vs_equal_weight_pct: q2(Number(main.total_return_pct) - Number(ewM.total_return_pct)),
    benchmarks: { btc: btcM, equal_weight: ewM },
    details: {
      strategy: name,
      granularity_s: 86400,
      universe: name === "momentum_rotation" ? universe : ["BTC-USD"],
      fee_tier: tier,
      slippage: { mode: "spread", default_bps: 5, by_product_bps: { "BTC-USD": 0.2 } },
      dataset: { source: "research/coinbase/data (mock)", granularity_s: 86400, products: universe.length, bars: n * universe.length },
      decisions: { executed: trades.length, rejected: rint(0, 6), skipped: rint(3, 20) },
      skip_reasons: { "within rebalance band": rint(10, 60), "below min_trade_usd": rint(0, 8) },
      look_ahead: "decisions at each bar close see only bars with end <= bar_end; fills at the next bar's open +/- half-spread + taker fee",
      known_biases: [
        "Fills at the next bar's open +/- a half-spread from a recent order-book snapshot: no depth impact and today's spreads applied to all history.",
        "Current increments and minimum funds are applied to all history.",
        "Benchmarks hold uncapped weights; live risk caps are only applied to the strategy when the 'limits' option is set.",
      ],
      errors: [],
    },
  };
  let peak = eq[0]!;
  const curvePts = eq.map((v, d) => {
    peak = Math.max(peak, v);
    return { ts: iso(start + d * DAY), equity: q2(v), drawdown_pct: q4(((peak - v) / peak) * 100) };
  });
  const periods = (len: number, key: "year" | "month") => byPeriod(len).map(({ period, ...rest }) => ({ [key]: period, ...rest }));
  return {
    ...summary,
    venue: "coinbase",
    status: "done",
    error: null,
    granularity_s: 86400,
    metrics,
    equity_curve: curvePts,
    benchmarks: { btc: curve(bh), equal_weight: curve(ew) },
    trades: trades.map((t) => ({ ...t, realized_pnl: t.pnl, fee_rate: fee, is_taker: true, slippage_bps: 0.2, strategy: name })),
    by_year: periods(4, "year"),
    by_month: periods(7, "month"),
    signals: [
      { ts: iso(start + 40 * DAY), product_id: "BTC-USD", side: "buy", target_weight: 1, quote_size: 12, decision: "rejected", decision_reason: "below min_trade_usd $10 after rounding", reason: "top-up" },
    ],
  };
}

/** List rows carry the flat metrics only (no nested benchmarks / details). */
function summaryMetrics(m: Raw): Raw {
  const { benchmarks: _b, details: _d, ...flat } = m;
  void _b;
  void _d;
  return flat;
}

function seedBacktests(now: number): MBacktest[] {
  const mk = (id: number, strategy: string, params: Raw, start: string, end: string, feeTier: string, ageH: number, seed: number): MBacktest => {
    const summary: Raw = { id, strategy, params, start, end, status: "done", created_at: iso(now - ageH * HOUR), fee_tier: feeTier, starting_balance: 1000, metrics: null };
    const detail = simulateBacktest(summary, seed);
    summary.metrics = summaryMetrics(detail.metrics as Raw);
    return { summary, detail, finishAt: 0, seed };
  };
  const failed: MBacktest = {
    summary: { id: 4, strategy: "mean_reversion_1h", params: { z_entry: 2.5, lookback_bars: 48, max_hold_bars: 24, execution: "maker_then_taker" }, start: "2026-01-01", end: "2026-08-31", status: "failed", created_at: iso(now - 3 * HOUR), fee_tier: "intro", starting_balance: 1000, metrics: null },
    detail: null,
    finishAt: 0,
    seed: 4,
  };
  failed.detail = { ...failed.summary, error: "research/coinbase/data has no hourly candles for ETH-USD before 2026-03-02 (run fetch.py --granularity 3600)", equity_curve: [], benchmarks: { btc: [], equal_weight: [] }, trades: [], by_year: [], by_month: [] };
  return [
    mk(1, "trend_sma", { sma_days: 100, weights: { "BTC-USD": 0.6, "ETH-USD": 0.4 }, confirm_bars: 1 }, "2022-01-01", "2026-08-31", "intro", 50, 11),
    mk(2, "momentum_rotation", { lookback_days: 30, top_k: 3, rebalance_days: 7, abs_momentum_filter: true }, "2022-01-01", "2026-08-31", "intro_pre_2026_09", 26, 23),
    mk(3, "trend_sma", { sma_days: 50, weights: { "BTC-USD": 0.6, "ETH-USD": 0.4 }, confirm_bars: 2 }, "2024-01-01", "2026-08-31", "intro_eu", 6, 37),
    failed,
  ];
}

// ---------------------------------------------------------------------------
// Simulation + SSE
// ---------------------------------------------------------------------------

function S(): MState {
  if (!state) {
    state = buildState(Date.now());
    startSimulation();
  }
  return state;
}

type Listener = (type: CbStreamEventType, data: unknown, id: number) => void;
const listeners = new Set<Listener>();
let eventId = 0;
function emit(type: CbStreamEventType, data: unknown) {
  eventId++;
  for (const l of listeners) l(type, clone(data), eventId);
}

let simTimer: ReturnType<typeof setInterval> | null = null;
let step = 0;

function startSimulation() {
  if (simTimer) return;
  simTimer = setInterval(simulateStep, 3000);
}

function simulateStep() {
  const st = state;
  if (!st) return;
  step++;
  const now = Date.now();
  rand = mulberry32((now & 0xffffffff) ^ (step * 7919));
  for (const p of st.products) {
    p.price = qPrice(p.price * Math.exp(p.sigma * 0.08 * rgauss()));
    p.spreadBps = Math.max(0.2, p.spreadBps * Math.exp(0.05 * rgauss()));
  }
  const e = st.engine;
  if (e.running) {
    e.tick_count++;
    e.last_tick_at = now;
    emit("tick", { venue: "coinbase", ts: iso(now), tick_count: e.tick_count, products_loaded: st.products.length });

    // Resting order: fills from later public prints through our price.
    const rest = st.orders.find((o) => isResting(o));
    if (rest && r() < 0.1) {
      const prod = productOf(st, String(rest.product_id))!;
      const qty = Number(rest.base_size) || floor8(Number(rest.quote_size) / (1 + MAKER) / Number(rest.limit_price));
      const f = execute(st, { t: now, strategy: String(rest.strategy), pid: prod.product_id, side: "buy", qty, price: Number(rest.limit_price), taker: false }, rest);
      rest.status = "filled";
      // The full quote was reserved up front and execute() debited the fill again:
      // release the reservation (the order is no longer resting).
      st.cash += Number(rest.quote_size);
      st.fills.unshift(f);
      emit("order", rest);
      emit("fill", f);
      pushLog(st, "info", "fill", `${rest.strategy}: maker BUY ${f.base_size} ${prod.base} @ $${f.price} filled from a public print · fee $${Number(f.fee).toFixed(2)} (0.50 %)`);
      const sig = st.signals.find((s) => s.order_id === rest.id);
      if (sig) {
        sig.decision = "executed";
        sig.decision_reason = `maker fill ${f.base_size} ${prod.base} @ $${f.price} after queue ahead was consumed`;
        emit("signal", sig);
      }
    } else if (rest && Date.parse(String(rest.expires_at)) < now) {
      rest.status = "expired";
      rest.updated_at = iso(now);
      st.cash += Number(rest.quote_size) - (Number(rest.filled_quote) || 0);
      emit("order", rest);
      pushLog(st, "info", "order", `Resting order #${rest.id} expired (${rest.product_id})`);
    }

    // Occasional rebalance top-up / rejection.
    if (!e.kill_switch && r() < 0.09) {
      const s = rchoice(st.strategies.filter((x) => x.enabled));
      const pid = rchoice(s.universe);
      const prod = productOf(st, pid)!;
      const quote = q2(rbetween(11, 28));
      const w = q4(s.name === "trend_sma" ? (pid === "BTC-USD" ? 0.6 : 0.4) : 1 / 3);
      if (r() < 0.35 || prod.spreadBps > st.risk.max_spread_bps!) {
        const why =
          prod.spreadBps > st.risk.max_spread_bps!
            ? `spread ${prod.spreadBps.toFixed(1)} bps > max_spread_bps ${st.risk.max_spread_bps}`
            : rchoice([
                `max_position_pct_per_product ${st.risk.max_position_pct_per_product} % would be exceeded`,
                `top-up $${(quote / 3).toFixed(2)} below min_trade_usd $${st.risk.min_trade_usd}`,
                `insufficient cash: would leave < min_cash_reserve $${st.risk.min_cash_reserve}`,
              ]);
        const sig = pushSignal(st, { t: now, strategy: s.name, product_id: pid, side: "buy", target_weight: w, quote_size: quote, reason: "rebalance toward target weight", decision: "rejected", decision_reason: why });
        emit("signal", sig);
        pushLog(st, "info", "risk", `${s.name}: BUY ${pid} $${quote.toFixed(2)} rejected — ${why}`);
      } else if (st.cash - quote > (st.risk.min_cash_reserve ?? 20)) {
        const px = askOf(prod);
        const qty = floor8(quote / (1 + TAKER) / px);
        const o = newOrder(st, { product_id: pid, side: "buy", strategy: s.name, t: now, quote_size: quote, reason: "rebalance toward target weight" });
        const f = execute(st, { t: now, strategy: s.name, pid, side: "buy", qty, price: px, taker: true }, o);
        st.fills.unshift(f);
        const sig = pushSignal(st, {
          t: now,
          strategy: s.name,
          product_id: pid,
          side: "buy",
          target_weight: w,
          quote_size: quote,
          reason: "rebalance toward target weight",
          decision: "executed",
          decision_reason: `filled ${f.base_size} ${prod.base} @ $${f.price} (taker, fee $${Number(f.fee).toFixed(2)})`,
          order_id: o.id,
        });
        emit("signal", sig);
        emit("order", o);
        emit("fill", f);
        pushLog(st, "info", "fill", `${s.name}: BUY ${f.base_size} ${prod.base} @ $${f.price} · fee $${Number(f.fee).toFixed(2)} (0.90 %)`);
      }
    }
    if (r() < 0.04) {
      const s = rchoice(st.strategies.filter((x) => x.enabled));
      const barEnd = s.bar_granularity_s === 3600 ? Math.floor(now / HOUR) * HOUR : Math.floor(now / DAY) * DAY;
      e.last_bar_at = barEnd;
      emit("bar", { venue: "coinbase", ts: iso(now), strategy: s.name, bar_end: iso(barEnd), granularity_s: s.bar_granularity_s, products: s.universe.length, intents: 0 });
      pushLog(st, "info", "bar", `${s.name}: bar ${iso(barEnd)} evaluated for ${s.universe.length} products; within rebalance band, no orders`);
    }
  }
  if (step % 10 === 0) {
    const a = accountOf(st);
    st.equity.push({ ts: iso(now), equity: a.equity, equity_mid: a.equity_mid, cash: a.cash, realized_pnl: a.realized_pnl, unrealized_pnl: a.unrealized_pnl });
  }
  if (e.running || step % 2 === 0) emit("account", accountOf(st));
  // Backtests finish after a few seconds.
  for (const b of st.backtests) {
    if (b.summary.status === "running" && b.finishAt <= now) {
      b.detail = simulateBacktest(b.summary, b.seed);
      b.summary.status = "done";
      b.summary.metrics = summaryMetrics(b.detail.metrics as Raw);
      pushLog(st, "info", "backtest", `Backtest #${b.summary.id} (${b.summary.strategy}) finished: return ${Number((b.detail.metrics as Raw).total_return_pct).toFixed(1)} %`);
    }
  }
}

/** Simulated EventSource used by ./stream.ts in mock mode. */
export function openCbMockStream(_url: string, h: CbStreamHandlers): CbStreamSource {
  S();
  let open = false;
  const listener: Listener = (type, data, id) => {
    if (open) h.onMessage(type, JSON.stringify(data), String(id));
  };
  const t = setTimeout(() => {
    open = true;
    listeners.add(listener);
    h.onOpen();
    h.onMessage("account", JSON.stringify(accountOf(S())), String(eventId));
  }, 250);
  const handle: CbStreamSource & { fail: () => void } = {
    close: () => {
      clearTimeout(t);
      open = false;
      listeners.delete(listener);
      failers.delete(handle);
    },
    fail: () => {
      handle.close();
      h.onError();
    },
  };
  failers.add(handle);
  return handle;
}
const failers = new Set<{ fail: () => void }>();

if (typeof window !== "undefined") {
  (window as unknown as Record<string, unknown>).__coinbaseMock = {
    dropStream: () => [...failers].forEach((f) => f.fail()),
    state: () => S(),
    /** Make every /api/coinbase route answer 503 (venue unavailable) until called with false. */
    unavailable: (on = true) => {
      unavailable = on;
    },
  };
}
let unavailable = false;

// ---------------------------------------------------------------------------
// Request router
// ---------------------------------------------------------------------------

export interface CbMockResponse {
  status: number;
  body: unknown;
}

const ok = (body: unknown): CbMockResponse => ({ status: 200, body: clone(body) });
const err = (status: number, detail: string): CbMockResponse => ({ status, body: { detail } });
const sleep = (ms: number) => new Promise((res) => setTimeout(res, ms));

function limitParam(q: URLSearchParams, dflt = 200): number {
  const n = Number(q.get("limit") ?? dflt);
  return Number.isFinite(n) && n > 0 ? Math.min(5000, n) : dflt;
}

function validateParams(s: MStrategy, patch: Raw): string | null {
  for (const [k, v] of Object.entries(patch)) {
    const spec = s.param_schema[k];
    if (!spec) return `params.${k}: unknown parameter for ${s.name}`;
    const t = spec.type;
    if (t === "int" || t === "float") {
      if (typeof v !== "number" || !Number.isFinite(v)) return `params.${k}: must be a number`;
      if (t === "int" && !Number.isInteger(v)) return `params.${k}: must be an integer`;
      if (spec.min != null && v < spec.min) return `params.${k}: must be ≥ ${spec.min}`;
      if (spec.max != null && v > spec.max) return `params.${k}: must be ≤ ${spec.max}`;
    } else if (t === "bool" && typeof v !== "boolean") return `params.${k}: must be a boolean`;
    else if (t === "enum" && !(spec.enum ?? []).includes(v as ParamValue)) return `params.${k}: must be one of ${(spec.enum ?? []).join(", ")}`;
  }
  return null;
}

function cancelResting(st: MState, o: Raw, now: number, why: string) {
  o.status = "cancelled";
  o.updated_at = iso(now);
  o.reason = `${String(o.reason)} · ${why}`;
  if (o.side === "buy") st.cash += Number(o.quote_size) - (Number(o.filled_quote) || 0);
  emit("order", o);
}

export async function cbMockRequest(method: string, rawPath: string, body: unknown): Promise<CbMockResponse> {
  await sleep(rbetween(80, 280));
  if (unavailable) return err(503, "coinbase venue unavailable: mock outage (window.__coinbaseMock.unavailable(false) to restore)");
  const st = S();
  const u = new URL(rawPath, "http://mock.local");
  const path = u.pathname.replace(/\/+$/, "");
  const q = u.searchParams;
  const b = (typeof body === "object" && body !== null ? body : {}) as Raw;
  const now = Date.now();

  if (method === "GET" && path === "/status") return ok(statusOf(st));
  if (method === "POST" && path === "/engine/start") {
    if (!st.engine.running) {
      st.engine.running = true;
      st.engine.started_at = now;
      pushLog(st, "info", "engine", "Coinbase engine started (paper mode)");
    }
    return ok(statusOf(st));
  }
  if (method === "POST" && path === "/engine/stop") {
    if (st.engine.running) {
      st.engine.running = false;
      pushLog(st, "info", "engine", "Coinbase engine stopped by user");
    }
    return ok(statusOf(st));
  }
  if (method === "POST" && path === "/engine/kill-switch") {
    if (typeof b.on !== "boolean") return err(422, "on: field required (boolean)");
    const was = st.engine.kill_switch;
    st.engine.kill_switch = b.on;
    st.engine.kill_switch_reason = b.on ? "manual" : "";
    pushLog(st, b.on ? "warning" : "info", "risk", b.on ? "Coinbase kill switch ENGAGED by user: buys blocked, resting orders cancelled" : "Coinbase kill switch released by user");
    if (b.on && !was) {
      const resting = st.orders.filter(isResting);
      for (const o of resting) cancelResting(st, o, now, "kill switch");
    }
    return ok(statusOf(st));
  }
  if (method === "GET" && path === "/account") return ok(accountOf(st));
  if (method === "POST" && path === "/account/reset") {
    const sb = b.starting_balance === undefined ? st.startingBalance : Number(b.starting_balance);
    if (!Number.isFinite(sb) || sb <= 0) return err(422, "starting_balance: must be a positive number");
    state = buildState(now, sb, true);
    return ok(accountOf(state));
  }
  if (method === "GET" && path === "/equity") {
    const range = q.get("range") ?? "7d";
    const spans: Record<string, number> = { "1d": DAY, "7d": 7 * DAY, "30d": 30 * DAY, all: Infinity };
    const span = spans[range];
    if (span === undefined) return err(422, "range: must be one of 1d, 7d, 30d, all");
    const pts = st.equity.filter((p) => now - Date.parse(String(p.ts)) <= span);
    return ok(pts);
  }
  if (method === "GET" && path === "/positions") return ok(positionRows(st));
  if (method === "GET" && path === "/orders") {
    const status = q.get("status") ?? "open";
    const rows = status === "all" ? st.orders : st.orders.filter(isResting);
    return ok(rows.slice(0, limitParam(q)));
  }
  let m = /^\/orders\/([^/]+)\/cancel$/.exec(path);
  if (method === "POST" && m) {
    const oid = decodeURIComponent(m[1] ?? "");
    const o = st.orders.find((x) => String(x.id) === oid);
    if (!o) return err(404, `order ${oid} not found`);
    if (!isResting(o)) return err(409, `order ${oid} is ${String(o.status)}, not open`);
    cancelResting(st, o, now, "cancelled by user");
    pushLog(st, "info", "order", `Cancelled Coinbase order #${oid} on ${String(o.product_id)} (user)`);
    return ok(o);
  }
  if (method === "GET" && path === "/fills") return ok(st.fills.slice(0, limitParam(q)));
  if (method === "GET" && path === "/strategies") return ok(st.strategies.map((s) => strategyRow(st, s)));
  m = /^\/strategies\/([^/]+)$/.exec(path);
  if (method === "PATCH" && m) {
    const name = decodeURIComponent(m[1] ?? "");
    const s = st.strategies.find((x) => x.name === name);
    if (!s) return err(404, `unknown Coinbase strategy '${name}'`);
    if (b.params !== undefined) {
      if (typeof b.params !== "object" || b.params === null) return err(422, "params: must be an object");
      const e = validateParams(s, b.params as Raw);
      if (e) return err(422, e);
      s.params = { ...s.params, ...(b.params as Record<string, ParamValue>) };
      pushLog(st, "info", "strategy", `${name}: parameters updated`, { params: s.params });
    }
    if (b.enabled !== undefined) {
      if (typeof b.enabled !== "boolean") return err(422, "enabled: must be a boolean");
      s.enabled = b.enabled;
      s.enabled_source = "dashboard";
      pushLog(st, "info", "strategy", `${name} ${b.enabled ? "enabled" : "disabled"}`);
    }
    return ok(strategyRow(st, s));
  }
  if (method === "GET" && path === "/risk") return ok(riskOf(st));
  if (method === "PATCH" && path === "/risk") {
    for (const [k, v] of Object.entries(b)) {
      if (!(k in st.risk)) return err(422, `${k}: unknown Coinbase risk limit`);
      if (typeof v !== "number" || !Number.isFinite(v) || v < 0) return err(422, `${k}: must be a non-negative number`);
    }
    for (const [k, v] of Object.entries(b)) st.risk[k] = v as number;
    pushLog(st, "info", "risk", `Coinbase risk limits updated: ${Object.keys(b).join(", ")}`);
    return ok(riskOf(st));
  }
  if (method === "GET" && path === "/signals") return ok(st.signals.slice(0, limitParam(q)));
  if (method === "GET" && path === "/logs") return ok(st.logs.slice(0, limitParam(q)));
  if (method === "GET" && path === "/products") {
    const search = (q.get("search") ?? "").trim().toLowerCase();
    const sort = q.get("sort") ?? "volume";
    let rows = st.products.map(productRow);
    if (search) rows = rows.filter((x) => x.product_id.toLowerCase().includes(search));
    if (sort === "spread") rows.sort((a, c) => a.spread_bps - c.spread_bps);
    else if (sort === "change") rows.sort((a, c) => c.change_24h_pct - a.change_24h_pct);
    else rows.sort((a, c) => c.volume_24h_usd - a.volume_24h_usd);
    return ok(rows.slice(0, limitParam(q, 100)));
  }
  if (method === "GET" && path === "/analytics") return ok(analyticsOf(st));
  if (method === "GET" && path === "/backtests") return ok([...st.backtests].reverse().map((x) => x.summary));
  if (method === "POST" && path === "/backtests") {
    const strategy = String(b.strategy ?? "");
    const s = st.strategies.find((x) => x.name === strategy);
    if (!s) return err(422, `strategy: unknown Coinbase strategy '${strategy}'`);
    if (!s.backtestable) return err(422, `strategy: ${strategy} is not backtestable`);
    const params = { ...s.params, ...((b.params as Record<string, ParamValue> | undefined) ?? {}) };
    const e = validateParams(s, params);
    if (e) return err(422, e);
    const feeTier = b.fee_tier === undefined || b.fee_tier === "" ? "intro" : String(b.fee_tier);
    if (!FEE_TIERS.some((f) => f.name === feeTier)) return err(422, `fee_tier: unknown tier '${feeTier}' (one of ${FEE_TIERS.map((f) => f.name).join(", ")})`);
    const start = typeof b.start === "string" && b.start ? b.start : "2022-01-01";
    const end = typeof b.end === "string" && b.end ? b.end : "2026-08-31";
    if (Date.parse(start) >= Date.parse(end)) return err(422, "start must be before end");
    const bid = Math.max(0, ...st.backtests.map((x) => Number(x.summary.id))) + 1;
    const summary: Raw = { id: bid, strategy, params, start, end, status: "running", created_at: iso(now), fee_tier: feeTier, starting_balance: Number(b.starting_balance ?? 1000), metrics: null };
    st.backtests.push({ summary, detail: null, finishAt: now + 5000 + rint(0, 3000), seed: 1000 + bid * 17 });
    pushLog(st, "info", "backtest", `Coinbase backtest #${bid} (${strategy}) started for ${start} → ${end}, fee tier ${feeTier}`);
    return ok({ id: bid, status: "running" });
  }
  m = /^\/backtests\/([^/]+)$/.exec(path);
  if (method === "GET" && m) {
    const bid = decodeURIComponent(m[1] ?? "");
    const x = st.backtests.find((y) => String(y.summary.id) === bid);
    if (!x) return err(404, `backtest ${bid} not found`);
    if (x.detail) return ok(x.detail);
    return ok({ ...x.summary, error: null, equity_curve: [], benchmarks: { btc: [], equal_weight: [] }, trades: [], by_year: [], by_month: [] });
  }
  return err(404, `Not Found: ${method} /api/coinbase${path}`);
}
