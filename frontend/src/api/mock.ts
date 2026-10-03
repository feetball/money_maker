/**
 * MOCK MODE (VITE_MOCK=1, `npm run dev:mock`).
 *
 * An in-browser fake of the §12 backend: a seeded, self-consistent paper account
 * (markets, positions across strategies, orders, fills, settlements, signals incl.
 * rejections, logs, analytics with bootstrap CIs + calibration + readiness, risk,
 * backtests) that keeps evolving while the "engine" runs, plus a simulated SSE stream.
 * Everything is generated at runtime — there are no fixture files.
 *
 * Responses are plain JSON-shaped objects (deep-copied) so they travel through the
 * exact same normalizers as real responses. Only loaded when IS_MOCK is true.
 */
import type {
  Account,
  BacktestDetail,
  BacktestMonth,
  BacktestSummary,
  BacktestTrade,
  CalibrationBucket,
  EquityPoint,
  Fill,
  LogEntry,
  MarketRow,
  Order,
  ParamSpec,
  ParamValue,
  Position,
  Settlement,
  Side,
  Signal,
  SignalDecision,
  StreamEventType,
} from "./types";
import type { StreamHandlers, StreamSource } from "./client";

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

let rand = mulberry32(20260926);
const r = () => rand();
const rint = (a: number, b: number) => Math.floor(a + r() * (b - a + 1));
const rbetween = (a: number, b: number) => a + r() * (b - a);
function rchoice<T>(xs: readonly T[]): T {
  const x = xs[Math.floor(r() * xs.length)];
  if (x === undefined) throw new Error("rchoice on empty list");
  return x;
}
function rgauss(): number {
  const u = Math.max(1e-9, r());
  const v = r();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}
const q4 = (x: number) => Math.round(x * 1e4) / 1e4;
const q2 = (x: number) => Math.round(x * 100) / 100;
const clampP = (p: number) => Math.min(0.99, Math.max(0.01, p));
const iso = (ms: number) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
const clone = <T>(x: T): T => JSON.parse(JSON.stringify(x)) as T;

function erf(x: number): number {
  const s = Math.sign(x);
  const a = Math.abs(x);
  const t = 1 / (1 + 0.3275911 * a);
  const y = 1 - ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-a * a);
  return s * y;
}
const normCdf = (x: number) => 0.5 * (1 + erf(x / Math.SQRT2));

/** Kalshi quadratic fee, rounded up to the cent: ceil(rate × C × P × (1 − P)). */
function fee(price: number, count: number, taker = true): number {
  const rate = taker ? 0.07 : 0.0175;
  return Math.ceil(rate * count * price * (1 - price) * 100 - 1e-9) / 100;
}

// ---------------------------------------------------------------------------
// Market universe
// ---------------------------------------------------------------------------

interface MMarket {
  ticker: string;
  event_ticker: string;
  series: string;
  title: string;
  category: string;
  fair: number;
  mid: number;
  spread: number;
  last: number;
  vol24: number;
  oi: number;
  close: number;
}

const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
function dayCode(ms: number): string {
  const d = new Date(ms);
  return `${String(d.getUTCFullYear()).slice(2)}${MONTHS[d.getUTCMonth()]}${String(d.getUTCDate()).padStart(2, "0")}`;
}
function dayLabel(ms: number): string {
  return new Date(ms).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

function mkMarket(p: Omit<MMarket, "mid" | "spread" | "last" | "vol24" | "oi"> & { liquid?: number }): MMarket {
  const liquid = p.liquid ?? 1;
  const mid = clampP(p.fair + rgauss() * 0.03);
  const spread = Math.max(0.01, Math.round((liquid > 1.5 ? rbetween(0.01, 0.03) : rbetween(0.01, 0.09)) * 100) / 100);
  return {
    ...p,
    mid,
    spread,
    last: clampP(q2(mid + rgauss() * 0.01)),
    vol24: Math.round(rbetween(50, 4000) * liquid * (0.3 + mid * (1 - mid) * 4)),
    oi: Math.round(rbetween(200, 30000) * liquid),
  };
}

function genMarkets(now: number): MMarket[] {
  const out: MMarket[] = [];
  const tomorrow = now + DAY;
  // Weather brackets (mutually exclusive events).
  const cities: [string, string, number][] = [
    ["KXHIGHNY", "NYC", 72],
    ["KXHIGHCHI", "Chicago", 66],
    ["KXHIGHAUS", "Austin", 89],
    ["KXHIGHMIA", "Miami", 88],
    ["KXHIGHLAX", "Los Angeles", 79],
  ];
  for (const [series, city, base] of cities) {
    const ev = `${series}-${dayCode(tomorrow)}`;
    const mu = base + rgauss() * 1.5;
    const sd = 2.2;
    for (let i = 0; i < 6; i++) {
      const lo = base - 5 + i * 2;
      const pLo = i === 0 ? 0 : normCdf((lo - mu) / sd);
      const pHi = i === 5 ? 1 : normCdf((lo + 2 - mu) / sd);
      const label = i === 0 ? `${lo + 1}° or below` : i === 5 ? `${lo}° or above` : `${lo}° to ${lo + 1}°`;
      out.push(
        mkMarket({
          ticker: `${ev}-${i === 0 ? "T" : i === 5 ? "T" : "B"}${lo + (i === 0 ? 2 : 0)}`,
          event_ticker: ev,
          series,
          title: `Highest temperature in ${city} on ${dayLabel(tomorrow)}: ${label}`,
          category: "Climate and Weather",
          fair: clampP(pHi - pLo),
          close: tomorrow - 6 * HOUR + rint(0, 60) * MIN,
          liquid: 1.2,
        }),
      );
    }
  }
  // Crypto thresholds.
  const crypto: [string, string, number, number][] = [
    ["KXBTCD", "Bitcoin", 112_000, 2500],
    ["KXETHD", "Ethereum", 4_150, 120],
  ];
  for (const [series, name, spot, step] of crypto) {
    for (const hoursAhead of [3, 27]) {
      const close = Math.ceil((now + hoursAhead * HOUR) / HOUR) * HOUR;
      const d = new Date(close);
      const ev = `${series}-${dayCode(close)}${String(d.getUTCHours()).padStart(2, "0")}`;
      const vol = spot * 0.012 * Math.sqrt(hoursAhead / 24);
      for (let k = -4; k <= 4; k++) {
        const strike = Math.round((spot + k * step) / step) * step - 0.01;
        out.push(
          mkMarket({
            ticker: `${ev}-T${strike.toFixed(2)}`,
            event_ticker: ev,
            series,
            title: `${name} above $${Math.round(strike + 0.01).toLocaleString("en-US")} at ${d.toISOString().slice(11, 16)} UTC ${dayLabel(close)}?`,
            category: "Crypto",
            fair: clampP(1 - normCdf((strike - spot) / vol)),
            close,
            liquid: 2,
          }),
        );
      }
    }
  }
  // S&P 500 range.
  {
    const ev = `KXINXU-${dayCode(now)}H1600`;
    const close = Math.floor(now / DAY) * DAY + 20 * HOUR + (now % DAY > 20 * HOUR ? DAY : 0);
    for (let i = 0; i < 6; i++) {
      const lo = 6600 + i * 25;
      out.push(
        mkMarket({
          ticker: `${ev}-B${lo + 12.5}`,
          event_ticker: ev,
          series: "KXINXU",
          title: `S&P 500 closes between ${lo.toLocaleString()} and ${(lo + 24.99).toLocaleString()} on ${dayLabel(close)}?`,
          category: "Financials",
          fair: clampP([0.05, 0.14, 0.31, 0.29, 0.15, 0.06][i] ?? 0.1),
          close,
          liquid: 1.5,
        }),
      );
    }
  }
  // Economics.
  const econ: [string, string, string, number, number][] = [
    ["KXFED-26OCT", "T3.75", "Fed funds upper bound above 3.75% after the Oct 2026 meeting?", 0.94, 32],
    ["KXFED-26OCT", "T4.00", "Fed funds upper bound above 4.00% after the Oct 2026 meeting?", 0.31, 32],
    ["KXFED-26OCT", "T4.25", "Fed funds upper bound above 4.25% after the Oct 2026 meeting?", 0.04, 32],
    ["KXCPI-26SEP", "T0.2", "CPI (MoM) for September 2026 above 0.2%?", 0.71, 18],
    ["KXCPI-26SEP", "T0.3", "CPI (MoM) for September 2026 above 0.3%?", 0.38, 18],
    ["KXCPI-26SEP", "T0.4", "CPI (MoM) for September 2026 above 0.4%?", 0.09, 18],
    ["KXPAYROLLS-26SEP", "T100000", "September 2026 nonfarm payrolls above 100k?", 0.62, 7],
    ["KXPAYROLLS-26SEP", "T150000", "September 2026 nonfarm payrolls above 150k?", 0.33, 7],
    ["KXU3-26SEP", "T4.3", "Unemployment rate for September 2026 above 4.3%?", 0.46, 7],
    ["KXGDP-26Q3", "T2.0", "Q3 2026 GDP growth (advance) above 2.0%?", 0.57, 35],
    ["KXGASPRICE-26OCT", "T3.20", "US average gas price above $3.20 on Oct 31?", 0.41, 35],
  ];
  for (const [ev, suffix, title, fair, days] of econ) {
    out.push(
      mkMarket({
        ticker: `${ev}-${suffix}`,
        event_ticker: ev,
        series: ev.split("-")[0] ?? ev,
        title,
        category: "Economics",
        fair,
        close: now + days * DAY + rint(0, 12) * HOUR,
        liquid: 1.6,
      }),
    );
  }
  // Sports (two-outcome, mutually exclusive).
  const games: [string, string, string][] = [
    ["KXNFLGAME", "KC", "BUF"],
    ["KXNFLGAME", "PHI", "DAL"],
    ["KXNFLGAME", "SF", "SEA"],
    ["KXNFLGAME", "DET", "GB"],
    ["KXMLBGAME", "LAD", "SD"],
    ["KXMLBGAME", "NYY", "BOS"],
    ["KXNHLGAME", "EDM", "VGK"],
  ];
  for (const [series, a, b] of games) {
    const close = now + rint(4, 70) * HOUR;
    const ev = `${series}-${dayCode(close)}${a}${b}`;
    const pa = clampP(rbetween(0.3, 0.75));
    out.push(
      mkMarket({ ticker: `${ev}-${a}`, event_ticker: ev, series, title: `${a} beat ${b}?`, category: "Sports", fair: pa, close, liquid: 1.8 }),
      mkMarket({ ticker: `${ev}-${b}`, event_ticker: ev, series, title: `${b} beat ${a}?`, category: "Sports", fair: clampP(1 - pa), close, liquid: 1.8 }),
    );
  }
  // Misc categories.
  const misc: [string, string, string, string, number, number][] = [
    ["Politics", "KXSHUTDOWN", "KXSHUTDOWN-26OCT01", "Government shutdown begins by Oct 1, 2026?", 0.22, 5],
    ["Politics", "KXAPPROVAL", "KXAPPROVAL-26OCT01-T45", "Presidential approval above 45% on Oct 1 (538 avg)?", 0.18, 5],
    ["Politics", "KXAPPROVAL", "KXAPPROVAL-26OCT01-T42", "Presidential approval above 42% on Oct 1 (538 avg)?", 0.61, 5],
    ["Politics", "KXSENATEVOTE", "KXSENATEVOTE-26OCT-NDAA", "Senate passes the FY27 NDAA by Oct 31?", 0.47, 35],
    ["Entertainment", "KXBOXOFFICE", "KXBOXOFFICE-26OCT04-T60", "Top film opening weekend above $60M (Oct 2–4)?", 0.35, 9],
    ["Entertainment", "KXHOT100", "KXHOT100-26OCT03-1", "Current #1 song stays #1 on next Hot 100?", 0.58, 7],
    ["Entertainment", "KXRTSCORE", "KXRTSCORE-26OCT-T80", "New fall blockbuster Rotten Tomatoes score above 80%?", 0.52, 20],
    ["Science and Technology", "KXSTARSHIP", "KXSTARSHIP-26OCT15", "Starship integrated flight launches before Oct 15?", 0.44, 19],
    ["Science and Technology", "KXAIMODEL", "KXAIMODEL-26DEC31", "A frontier lab releases a new flagship model by Dec 31?", 0.83, 96],
    ["Science and Technology", "KXHURRICANE", "KXHURRICANE-26OCT-T2", "At least 2 Atlantic hurricanes form in October 2026?", 0.39, 35],
    ["Companies", "KXTSLADELIV", "KXTSLADELIV-26Q3-T450", "Tesla Q3 2026 deliveries above 450k?", 0.41, 6],
    ["Companies", "KXAPPLEEVENT", "KXAPPLEEVENT-26OCT", "Apple holds an October product event?", 0.66, 34],
    ["Companies", "KXEARNINGS", "KXEARNINGS-26OCT-NKE", "Nike beats EPS consensus in its next report?", 0.63, 3],
  ];
  for (const [category, series, ticker, title, fair, days] of misc) {
    out.push(
      mkMarket({
        ticker,
        event_ticker: ticker.split("-").slice(0, 2).join("-"),
        series,
        title,
        category,
        fair,
        close: now + days * DAY + rint(0, 20) * HOUR,
        liquid: rbetween(0.4, 1.4),
      }),
    );
  }
  return out;
}

function bidAsk(m: MMarket): { bid: number; ask: number } {
  let bid = Math.round((m.mid - m.spread / 2) * 100) / 100;
  bid = Math.min(0.98, Math.max(0.01, bid));
  const ask = Math.min(0.99, Math.max(bid + 0.01, Math.round((bid + m.spread) * 100) / 100));
  return { bid, ask };
}

const marketUrl = (m: MMarket) => `https://kalshi.com/markets/${m.series.toLowerCase()}`;

function marketRow(m: MMarket): MarketRow {
  const { bid, ask } = bidAsk(m);
  return {
    ticker: m.ticker,
    event_ticker: m.event_ticker,
    title: m.title,
    category: m.category,
    yes_bid: bid,
    yes_ask: ask,
    spread: q4(ask - bid),
    last_price: m.last,
    volume_24h: m.vol24,
    open_interest: m.oi,
    close_time: iso(m.close),
    url: marketUrl(m),
  };
}

// ---------------------------------------------------------------------------
// Strategies
// ---------------------------------------------------------------------------

interface MStrategy {
  name: string;
  description: string;
  enabled: boolean;
  backtestable: boolean;
  params: Record<string, ParamValue>;
  param_schema: Record<string, ParamSpec>;
}

function genStrategies(): MStrategy[] {
  return [
    {
      name: "favorite_longshot",
      description:
        "Buys heavy favorites (90–97¢) close to resolution, exploiting the favorite–longshot bias; holds to settlement.",
      enabled: true,
      backtestable: true,
      params: { min_price: 0.9, max_price: 0.97, max_hours_to_close: 48, kelly_fraction: 0.25, max_contracts: 20, categories: "Climate and Weather,Economics,Financials" },
      param_schema: {
        min_price: { type: "float", min: 0.5, max: 0.99, step: 0.01, help: "Lowest YES/NO price considered a favorite ($)." },
        max_price: { type: "float", min: 0.5, max: 0.99, step: 0.01, help: "Highest price worth paying; above this fees eat the edge ($)." },
        max_hours_to_close: { type: "int", min: 1, max: 720, help: "Only enter markets closing within this many hours." },
        kelly_fraction: { type: "float", min: 0, max: 1, step: 0.05, help: "Fraction of full Kelly used for sizing." },
        max_contracts: { type: "int", min: 1, max: 500, help: "Hard cap on contracts per order." },
        categories: { type: "str", help: "Comma-separated Kalshi categories to scan (empty = all)." },
      },
    },
    {
      name: "mutex_arb",
      description:
        "Buys every leg of a mutually-exclusive event when the full basket costs less than the guaranteed $1 payout after fees (all-or-none).",
      enabled: true,
      backtestable: true,
      params: { min_edge_cents: 1.5, max_legs: 12, all_or_none: true, max_basket_cost: 40, side: "no" },
      param_schema: {
        min_edge_cents: { type: "float", min: 0.1, max: 20, step: 0.1, help: "Minimum locked-in profit per basket after fees (¢)." },
        max_legs: { type: "int", min: 2, max: 40, help: "Skip events with more outcomes than this." },
        all_or_none: { type: "bool", help: "Execute every leg or none (strongly recommended)." },
        max_basket_cost: { type: "float", min: 1, max: 500, step: 1, help: "Maximum $ spent per basket." },
        side: { type: "enum", enum: ["yes", "no", "both"], help: "Which basket to buy: all YES (<$1) or all NO (<$N−1)." },
      },
    },
    {
      name: "crypto_fv",
      description:
        "Prices BTC/ETH threshold markets from spot and realized volatility (lognormal); trades when edge after fees clears the threshold.",
      enabled: true,
      backtestable: true,
      params: { asset: "both", vol_lookback_min: 60, vol_multiplier: 1.1, min_edge: 0.03, use_maker: false, max_contracts: 25 },
      param_schema: {
        asset: { type: "enum", enum: ["BTC", "ETH", "both"], help: "Underlyings to trade." },
        vol_lookback_min: { type: "int", min: 5, max: 1440, help: "Realized-vol lookback window (minutes)." },
        vol_multiplier: { type: "float", min: 0.5, max: 3, step: 0.05, help: "Scale applied to realized vol (fat-tail cushion)." },
        min_edge: { type: "float", min: 0, max: 0.25, step: 0.005, help: "Minimum expected edge per contract after fees ($)." },
        use_maker: { type: "bool", help: "Post GTC limit orders instead of taking liquidity." },
        max_contracts: { type: "int", min: 1, max: 500, help: "Hard cap on contracts per order." },
      },
    },
    {
      name: "maker_spread",
      description:
        "Posts GTC bids inside wide spreads on liquid markets; fills only from real trades printed through our price (queue-aware).",
      enabled: false,
      backtestable: false,
      params: { min_spread: 0.05, improve_by: 0.01, order_ttl_s: 1800, max_open_orders: 10 },
      param_schema: {
        min_spread: { type: "float", min: 0.02, max: 0.5, step: 0.01, help: "Only quote markets with at least this spread ($)." },
        improve_by: { type: "float", min: 0, max: 0.05, step: 0.01, help: "Improve the best bid by this much ($)." },
        order_ttl_s: { type: "int", min: 60, max: 86400, help: "Cancel resting orders after this many seconds." },
        max_open_orders: { type: "int", min: 1, max: 100, help: "Maximum simultaneously resting orders." },
      },
    },
  ];
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

interface MPosition {
  ticker: string;
  side: Side;
  count: number;
  avg_price: number;
  fees: number;
  fair_value: number | null;
  expected_edge_total: number | null;
  strategy: string;
  opened_at: number;
}

/**
 * Mirrors the backend: cost_basis is principal EXCLUDING fees and
 * pnl = payout − cost_basis − fees. kind "close" = netted out before resolution
 * (result "closed", payout = exit proceeds, no outcome for calibration).
 */
interface MSettlement extends Settlement {
  fair_value: number | null;
  /** $/contract expected at entry; null when the strategy reported none. */
  expected_edge: number | null;
  won: boolean;
  entry_price: number;
  entry_fee: number;
}

function publicSettlement(s: MSettlement): Settlement {
  return {
    id: s.id,
    ticker: s.ticker,
    title: s.title,
    kind: s.kind,
    result: s.result,
    side: s.side,
    count: s.count,
    payout: s.payout,
    cost_basis: s.cost_basis,
    fees: s.fees,
    pnl: s.pnl,
    ts: s.ts,
    strategy: s.strategy,
  };
}

/** Mock thresholds reported as `params` by /analytics (differ from the backend defaults on purpose). */
const MOCK_MIN_SETTLED_TRADES = 150;
const MOCK_MAX_DRAWDOWN_PCT = 20;

interface MBacktest {
  summary: BacktestSummary;
  detail: BacktestDetail | null;
  finishAt: number | null;
}

interface MockState {
  startingBalance: number;
  engine: {
    running: boolean;
    started_at: number | null;
    last_tick_at: number | null;
    tick_count: number;
    last_error: string | null;
    last_error_at: number | null;
    kill_switch: boolean;
    kill_switch_reason: string;
  };
  markets: MMarket[];
  byTicker: Map<string, MMarket>;
  strategies: MStrategy[];
  positions: MPosition[];
  orders: Order[];
  fills: Fill[];
  settlements: MSettlement[];
  signals: Signal[];
  logs: LogEntry[];
  equity: EquityPoint[];
  realizedExtra: number;
  feesPaid: number;
  reservedProfit: number;
  profitSweepEnabled: boolean;
  profitSweepPct: number;
  /** Manual withdrawals from reserved_profit back to cash (mock doesn't simulate sweeping on settlement). */
  cashAdjustment: number;
  risk: Record<string, number>;
  ordersTimes: number[];
  backtests: MBacktest[];
  nextId: number;
  nextLogId: number;
  nextSignalId: number;
}

const STRATEGY_CATEGORY: Record<string, string[]> = {
  favorite_longshot: ["Climate and Weather", "Economics", "Financials"],
  mutex_arb: ["Climate and Weather", "Sports"],
  crypto_fv: ["Crypto"],
  maker_spread: ["Sports", "Politics", "Entertainment", "Economics"],
};

const REJECT_REASONS = [
  (m: MMarket) => `max_spread: spread ${Math.round(m.spread * 100)}¢ > 10¢ limit`,
  () => `min_seconds_to_close: closes in ${rint(40, 290)}s (< 300s)`,
  () => `max_position_cost_per_market: $${rbetween(46, 50).toFixed(2)} of $50.00 already committed`,
  () => `max_exposure_per_event: event exposure $${rbetween(92, 100).toFixed(2)} ≥ $100.00`,
  (_m: MMarket, s: string) => `max_strategy_allocation_pct: ${s} at ${rbetween(48, 55).toFixed(1)}% of equity (limit 50%)`,
  () => `min_cash_reserve: need $${rbetween(20, 60).toFixed(2)}, only $${rbetween(0, 15).toFixed(2)} above the $50.00 reserve`,
  () => `max_orders_per_minute: 30 orders in the last 60s`,
  () => `already holds this market (no re-entry)`,
];

const REASONS: Record<string, (m: MMarket, side: Side, price: number, fv: number) => string> = {
  favorite_longshot: (m, side, price, fv) =>
    `${side.toUpperCase()} at ${Math.round(price * 100)}¢ is a favorite; bias-adjusted P(win)=${(fv * 100).toFixed(1)}%, closes in ${Math.max(1, Math.round((m.close - Date.now()) / HOUR))}h`,
  mutex_arb: (_m, _side, price) =>
    `basket of NO legs costs $${(price * 5).toFixed(2)} vs guaranteed $5.00 payout; locked edge after fees`,
  crypto_fv: (_m, side, price, fv) =>
    `lognormal FV ${(fv * 100).toFixed(1)}¢ vs ask ${Math.round(price * 100)}¢ (${side.toUpperCase()}); σ=${rbetween(38, 61).toFixed(0)}% ann.`,
  maker_spread: (m, _side, price) => `quote ${Math.round(price * 100)}¢ inside ${Math.round(m.spread * 100)}¢ spread`,
};

function sidePrices(m: MMarket, side: Side): { bid: number; ask: number; mid: number } {
  const { bid, ask } = bidAsk(m);
  if (side === "yes") return { bid, ask, mid: (bid + ask) / 2 };
  return { bid: q2(1 - ask), ask: q2(1 - bid), mid: 1 - (bid + ask) / 2 };
}

function buildState(now: number, startingBalance = 1000, empty = false): MockState {
  rand = mulberry32(20260926);
  const markets = genMarkets(now);
  const byTicker = new Map(markets.map((m) => [m.ticker, m] as const));
  const st: MockState = {
    startingBalance,
    engine: {
      running: !empty,
      started_at: empty ? null : now - 5 * HOUR - 13 * MIN,
      last_tick_at: empty ? null : now - 11_000,
      tick_count: empty ? 0 : 624,
      // Like the backend, the last job failure is kept after the job recovered: an
      // old error is history (the UI must not show it as the current state).
      last_error: empty ? null : "universe: HTTPStatusError: Server error '503 Service Unavailable' for url 'https://api.elections.kalshi.com/trade-api/v2/markets'",
      last_error_at: empty ? null : now - 2 * HOUR - 7 * MIN,
      kill_switch: false,
      kill_switch_reason: "",
    },
    markets,
    byTicker,
    strategies: genStrategies(),
    positions: [],
    orders: [],
    fills: [],
    settlements: [],
    signals: [],
    logs: [],
    equity: [],
    realizedExtra: 0,
    feesPaid: 0,
    reservedProfit: 0,
    profitSweepEnabled: false,
    profitSweepPct: 100,
    cashAdjustment: 0,
    risk: {
      max_position_cost_per_market: 50,
      max_exposure_per_event: 100,
      max_total_exposure_pct: 80,
      max_strategy_allocation_pct: 50,
      min_cash_reserve: 50,
      max_orders_per_minute: 30,
      daily_loss_limit: 100,
      min_seconds_to_close: 300,
      max_spread: 0.1,
      kelly_fraction: 0.25,
    },
    ordersTimes: [],
    backtests: [],
    nextId: 1000,
    nextLogId: 1,
    nextSignalId: 1,
  };
  if (empty) {
    st.equity = [{ ts: iso(now), equity: startingBalance, equity_mid: startingBalance, cash: startingBalance, realized_pnl: 0, unrealized_pnl: 0 }];
    st.logs.push({ id: st.nextLogId++, ts: iso(now), level: "warning", kind: "account", message: `Paper account reset to $${startingBalance.toFixed(2)}; engine stopped.`, data: null });
    st.backtests = genBacktests(now);
    return st;
  }

  // --- Settled history (last ~30 days) ---
  const settleCount: Record<string, number> = { favorite_longshot: 84, mutex_arb: 41, crypto_fv: 31, maker_spread: 6 };
  for (const [strategy, n] of Object.entries(settleCount)) {
    for (let i = 0; i < n; i++) {
      const tsMs = now - rbetween(0.3, 30) * DAY;
      const side: Side = r() < 0.7 ? "yes" : "no";
      let price: number;
      let pWin: number;
      let count: number;
      let fvModel: number | null;
      if (strategy === "favorite_longshot") {
        price = q2(rbetween(0.9, 0.97));
        pWin = Math.min(0.995, price + 0.012);
        fvModel = Math.min(0.99, price + rbetween(0.015, 0.035));
        count = rint(5, 20);
      } else if (strategy === "mutex_arb") {
        price = q2(rbetween(0.8, 0.95));
        pWin = 0.985;
        fvModel = null;
        count = rint(3, 12);
      } else if (strategy === "crypto_fv") {
        price = q2(rbetween(0.18, 0.82));
        fvModel = clampP(price + rbetween(0.03, 0.08));
        pWin = clampP(fvModel - 0.02 + rgauss() * 0.03);
        count = rint(5, 25);
      } else {
        price = q2(rbetween(0.3, 0.7));
        pWin = price + 0.01;
        fvModel = null;
        count = rint(2, 8);
      }
      const won = r() < pWin;
      const f = fee(price, count, strategy !== "maker_spread");
      const cost = q4(count * price);
      // A few crypto_fv positions are netted out before resolution (kind "close").
      const closed = strategy === "crypto_fv" && r() < 0.12;
      const exitPrice = closed ? q2(clampP(price + rgauss() * 0.06)) : 0;
      const payout = closed ? q4(count * exitPrice) : won ? count : 0;
      const tickerPool = markets.filter((m) => STRATEGY_CATEGORY[strategy]?.includes(m.category));
      const m = rchoice(tickerPool.length ? tickerPool : markets);
      // maker_spread reports no expected edge ("not reported" in analytics, never $0).
      const expEdge = strategy === "maker_spread" ? null : fvModel !== null ? q4(fvModel - price - f / count) : q4(rbetween(0.008, 0.025));
      st.settlements.push({
        id: st.nextId++,
        ticker: `${m.event_ticker.replace(/-\w+$/, "")}-${dayCode(tsMs)}${m.ticker.slice(m.ticker.lastIndexOf("-"))}`,
        title: m.title.replace(/on \w{3} \d+/, `on ${dayLabel(tsMs)}`),
        kind: closed ? "close" : "settlement",
        result: closed ? "closed" : won ? side : side === "yes" ? "no" : "yes",
        side,
        count,
        payout,
        cost_basis: cost,
        fees: f,
        pnl: q4(payout - cost - f),
        ts: iso(tsMs),
        strategy,
        fair_value: fvModel,
        expected_edge: expEdge,
        won: closed ? false : won,
        entry_price: price,
        entry_fee: f,
      });
      st.feesPaid += f;
    }
  }
  st.settlements.sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts));

  // --- Open positions ---
  const pick = (strategy: string, count: number) => {
    const pool = markets.filter((m) => STRATEGY_CATEGORY[strategy]?.includes(m.category) && !st.positions.some((p) => p.ticker === m.ticker));
    for (let i = 0; i < count && pool.length; i++) {
      const idx = Math.floor(r() * pool.length);
      const m = pool.splice(idx, 1)[0];
      if (!m) continue;
      let side: Side = m.mid >= 0.5 ? "yes" : "no";
      if (strategy === "crypto_fv") side = r() < 0.5 ? "yes" : "no";
      const sp = sidePrices(m, side);
      const n = strategy === "mutex_arb" ? rint(4, 10) : rint(4, 22);
      const entry = q2(Math.min(0.99, sp.ask + rgauss() * 0.02));
      const f = fee(entry, n);
      const fv = strategy === "mutex_arb" || strategy === "maker_spread" ? null : clampP(entry + rbetween(0.01, 0.06));
      st.positions.push({
        ticker: m.ticker,
        side,
        count: n,
        avg_price: entry,
        fees: f,
        fair_value: fv,
        expected_edge_total: q4(n * (fv !== null ? fv - entry - f / n : rbetween(0.01, 0.02))),
        strategy,
        opened_at: now - rbetween(0.2, 40) * HOUR,
      });
      st.feesPaid += f;
    }
  };
  pick("favorite_longshot", 6);
  pick("mutex_arb", 4);
  pick("crypto_fv", 5);
  pick("maker_spread", 1);

  // --- Orders & fills (history + open GTC) ---
  for (const p of st.positions) {
    const m = byTicker.get(p.ticker);
    const oid = st.nextId++;
    const created = p.opened_at;
    const fv = p.fair_value;
    st.orders.push({
      id: oid,
      ticker: p.ticker,
      title: m?.title ?? p.ticker,
      side: p.side,
      action: "buy",
      count: p.count,
      filled_count: p.count,
      limit_price: q2(Math.min(0.99, p.avg_price + 0.01)),
      avg_fill_price: p.avg_price,
      tif: p.strategy === "maker_spread" ? "gtc" : "ioc",
      status: "filled",
      strategy: p.strategy,
      reason: REASONS[p.strategy]?.(m ?? markets[0]!, p.side, p.avg_price, fv ?? p.avg_price) ?? "",
      expected_edge: p.expected_edge_total !== null ? q4(p.expected_edge_total / p.count) : null,
      fair_value: fv,
      group_id: p.strategy === "mutex_arb" ? `basket-${m?.event_ticker ?? "x"}` : null,
      queue_ahead: p.strategy === "maker_spread" ? 0 : null,
      created_at: iso(created),
      updated_at: iso(created + 800),
      expires_at: null,
      fees: p.fees,
    });
    st.fills.push({
      id: st.nextId++,
      order_id: oid,
      ticker: p.ticker,
      title: m?.title ?? p.ticker,
      side: p.side,
      action: "buy",
      count: p.count,
      price: p.avg_price,
      fee: p.fees,
      is_taker: p.strategy !== "maker_spread",
      ts: iso(created + 800),
      strategy: p.strategy,
    });
  }
  for (const s of st.settlements.slice(0, 120)) {
    const oid = st.nextId++;
    const opened = Date.parse(s.ts) - rbetween(2, 60) * HOUR;
    const entry = s.entry_price;
    const f = s.entry_fee;
    const partial = r() < 0.08;
    st.orders.push({
      id: oid,
      ticker: s.ticker,
      title: s.title,
      side: s.side,
      action: "buy",
      count: partial ? s.count + rint(1, 6) : s.count,
      filled_count: s.count,
      limit_price: q2(Math.min(0.99, entry + 0.01)),
      avg_fill_price: entry,
      tif: s.strategy === "maker_spread" ? "gtc" : "ioc",
      status: partial ? "partially_filled" : "filled",
      strategy: s.strategy,
      reason: "",
      expected_edge: s.expected_edge,
      fair_value: s.fair_value,
      group_id: s.strategy === "mutex_arb" ? `basket-${s.ticker.split("-").slice(0, 2).join("-")}` : null,
      queue_ahead: null,
      created_at: iso(opened),
      updated_at: iso(opened + 600),
      expires_at: null,
      fees: f,
    });
    st.fills.push({
      id: st.nextId++,
      order_id: oid,
      ticker: s.ticker,
      title: s.title,
      side: s.side,
      action: "buy",
      count: s.count,
      price: entry,
      fee: f,
      is_taker: s.strategy !== "maker_spread",
      ts: iso(opened + 600),
      strategy: s.strategy,
    });
  }
  // Some cancelled / expired / rejected orders.
  for (let i = 0; i < 18; i++) {
    const m = rchoice(markets);
    const created = now - rbetween(0.5, 20) * DAY;
    const status = rchoice(["cancelled", "expired", "expired", "rejected"] as const);
    const side: Side = m.mid > 0.5 ? "yes" : "no";
    const price = q2(sidePrices(m, side).bid);
    st.orders.push({
      id: st.nextId++,
      ticker: m.ticker,
      title: m.title,
      side,
      action: "buy",
      count: rint(2, 15),
      filled_count: 0,
      limit_price: price,
      avg_fill_price: null,
      tif: "gtc",
      status,
      strategy: rchoice(["maker_spread", "crypto_fv", "favorite_longshot"]),
      reason: status === "rejected" ? "broker: insufficient cash for reservation" : "resting bid inside spread",
      expected_edge: q4(rbetween(0.005, 0.03)),
      fair_value: null,
      group_id: null,
      queue_ahead: rint(0, 400),
      created_at: iso(created),
      updated_at: iso(created + rbetween(5, 60) * MIN),
      expires_at: iso(created + HOUR),
      fees: 0,
    });
  }
  // Open resting orders.
  for (let i = 0; i < 5; i++) {
    const m = rchoice(markets.filter((x) => x.category === "Crypto" || x.category === "Sports"));
    const side: Side = r() < 0.5 ? "yes" : "no";
    const sp = sidePrices(m, side);
    const created = now - rbetween(1, 40) * MIN;
    const count = rint(3, 12);
    const filled = r() < 0.3 ? rint(1, count - 1) : 0;
    st.orders.push({
      id: st.nextId++,
      ticker: m.ticker,
      title: m.title,
      side,
      action: "buy",
      count,
      filled_count: filled,
      limit_price: q2(Math.max(0.01, sp.bid)),
      avg_fill_price: filled ? q2(Math.max(0.01, sp.bid)) : null,
      tif: "gtc",
      status: filled ? "partially_filled" : "open",
      strategy: "crypto_fv",
      reason: `maker bid ${Math.round(sp.bid * 100)}¢ below FV ${Math.round((sp.bid + 0.04) * 100)}¢`,
      expected_edge: q4(rbetween(0.02, 0.05)),
      fair_value: q4(Math.min(0.99, sp.bid + 0.04)),
      group_id: null,
      queue_ahead: rint(0, 250),
      created_at: iso(created),
      updated_at: iso(created + 30_000),
      expires_at: iso(created + HOUR),
      fees: 0,
    });
  }
  st.orders.sort((a, b) => Date.parse(b.created_at ?? "") - Date.parse(a.created_at ?? ""));
  st.fills.sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts));

  // --- Signals (last ~2 days) ---
  for (let i = 0; i < 260; i++) {
    st.signals.push(r() < 0.02 ? makeBadSignal(st, now - rbetween(0, 2) * DAY) : makeSignal(st, now - rbetween(0, 2) * DAY));
  }
  // Store ids grow with time, like the backend's autoincrement rows.
  st.signals.sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
  for (const x of st.signals) x.id = st.nextSignalId++;
  st.signals.reverse();

  // --- Logs ---
  const logTemplates: [string, string, () => string][] = [
    ["info", "universe", () => `Universe refreshed: ${rint(3100, 3400)} open markets, ${rint(900, 1000)} events (${rint(4, 9)}.${rint(0, 9)}s)`],
    ["info", "tick", () => `Tick #${rint(400, 620)}: 3 strategies, ${rint(0, 6)} intents, ${rint(0, 3)} executed`],
    ["info", "settlement", () => `Settled ${rchoice(markets).ticker}: ${rchoice(["won", "lost"])} ${rint(3, 20)} contracts`],
    ["warning", "risk", () => `Rejected intent: ${rchoice(REJECT_REASONS)(rchoice(markets), "crypto_fv")}`],
    ["warning", "marketdata", () => `HTTP 429 from Kalshi; backing off ${rint(1, 4)}.${rint(0, 9)}s (retry ${rint(1, 3)}/5)`],
    ["error", "strategy", () => `crypto_fv.on_tick raised TimeoutError: spot feed stale for ${rint(31, 90)}s — skipped this tick`],
    ["info", "order", () => `Maker order expired after 1h on ${rchoice(markets).ticker}`],
    ["debug", "engine", () => `Resting-order maintenance: ${rint(0, 6)} open, ${rint(0, 2)} filled from prints`],
  ];
  for (let i = 0; i < 90; i++) {
    const [level, kind, msg] = rchoice(logTemplates);
    st.logs.push({ id: null, ts: iso(now - rbetween(0, 2) * DAY), level, kind, message: msg(), data: null });
  }
  st.logs.push({ id: null, ts: iso(now - 5 * HOUR - 13 * MIN), level: "info", kind: "engine", message: "Engine started (paper mode)", data: null });
  st.logs.push({
    id: null,
    ts: iso(st.engine.last_error_at ?? now),
    level: "warning",
    kind: "engine",
    message: `${st.engine.last_error ?? "universe failed"}; retry in 60s`,
    data: null,
  });
  st.logs.sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
  for (const l of st.logs) l.id = st.nextLogId++;
  st.logs.reverse();

  // --- Equity series (10-minute points, ~40 days) consistent with the account ---
  const acct = accountOf(st);
  const n = 40 * 24 * 6;
  const pts: EquityPoint[] = [];
  let unreal = 0;
  const settledAsc = [...st.settlements].sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
  let si = 0;
  let realized = 0;
  for (let i = 0; i < n; i++) {
    const t = now - (n - 1 - i) * 10 * MIN;
    while (si < settledAsc.length && Date.parse(settledAsc[si]!.ts) <= t) {
      realized += settledAsc[si]!.pnl;
      si++;
    }
    unreal = unreal * 0.995 + rgauss() * 0.9;
    const u = i === n - 1 ? acct.unrealized_pnl : unreal + (acct.unrealized_pnl * i) / n;
    const eq = startingBalance + realized + u;
    pts.push({
      ts: iso(t),
      equity: q4(eq),
      equity_mid: q4(eq + 3 + Math.abs(rgauss()) * 2),
      cash: q4(eq - 180 - Math.abs(rgauss()) * 40),
      realized_pnl: q4(realized),
      unrealized_pnl: q4(u),
    });
  }
  st.realizedExtra = 0;
  st.equity = pts;
  st.backtests = genBacktests(now);
  return st;
}

/** A well-formed signal (count and limit_price always set). */
type MSignal = Signal & { count: number; limit_price: number };

/** A malformed intent the engine rejected before sizing: count / limit_price are null. */
function makeBadSignal(st: MockState, t: number): Signal {
  const m = rchoice(st.markets);
  const strategy = rchoice(st.strategies.filter((s) => s.enabled).map((s) => s.name).concat("crypto_fv"));
  return {
    id: st.nextSignalId++,
    ts: iso(t),
    strategy,
    ticker: m.ticker,
    title: "",
    side: "yes",
    action: "buy",
    count: null,
    limit_price: null,
    fair_value: null,
    expected_edge: null,
    reason: "",
    decision: "rejected",
    decision_raw: "rejected",
    decision_reason: rchoice(["invalid intent: limit_price 1.02 outside [0.01, 0.99]", "invalid intent: count must be a positive integer (got 0)"]),
  };
}

function makeSignal(st: MockState, t: number, forceDecision?: SignalDecision): MSignal {
  const enabled = st.strategies.filter((s) => s.enabled).map((s) => s.name);
  const strategy = rchoice(enabled.length ? enabled : ["favorite_longshot"]);
  const pool = st.markets.filter((m) => STRATEGY_CATEGORY[strategy]?.includes(m.category));
  const m = rchoice(pool.length ? pool : st.markets);
  const side: Side = strategy === "crypto_fv" ? (r() < 0.5 ? "yes" : "no") : m.mid >= 0.5 ? "yes" : "no";
  const sp = sidePrices(m, side);
  const price = strategy === "maker_spread" ? sp.bid : sp.ask;
  const fv = strategy === "mutex_arb" || strategy === "maker_spread" ? null : Math.min(0.995, price + rbetween(0.005, 0.07));
  const count = rint(2, 25);
  const edge = q4((fv ?? price + 0.02) - price - fee(price, 1));
  let decision: SignalDecision;
  if (forceDecision) decision = forceDecision;
  else {
    const x = r();
    decision = x < 0.46 ? "executed" : x < 0.54 ? "partial" : x < 0.88 ? "rejected" : "unfilled";
  }
  if (st.engine.kill_switch && decision !== "rejected") decision = "rejected";
  let decision_reason = "";
  if (decision === "executed") decision_reason = `filled ${count}/${count} @ avg ${Math.round(price * 100)}¢`;
  else if (decision === "partial") {
    const f = rint(1, Math.max(1, count - 1));
    decision_reason = `filled ${f}/${count}: book depth exhausted at ${Math.round(Math.min(0.99, price + 0.01) * 100)}¢ (after consumed liquidity)`;
  } else if (decision === "unfilled") decision_reason = `IOC: no ask at or below ${Math.round(price * 100)}¢ after consumed liquidity`;
  else decision_reason = st.engine.kill_switch ? "kill switch active: new entries blocked" : rchoice(REJECT_REASONS)(m, strategy);
  return {
    id: st.nextSignalId++,
    ts: iso(t),
    strategy,
    ticker: m.ticker,
    title: m.title,
    side,
    action: "buy",
    count,
    limit_price: q2(price),
    fair_value: fv === null ? null : q4(fv),
    expected_edge: edge,
    reason: REASONS[strategy]?.(m, side, price, fv ?? price) ?? "",
    decision,
    decision_reason,
  };
}

// ---------------------------------------------------------------------------
// Derived views
// ---------------------------------------------------------------------------

function positionRow(st: MockState, p: MPosition): Position {
  const m = st.byTicker.get(p.ticker);
  const sp = m ? sidePrices(m, p.side) : { bid: p.avg_price, ask: p.avg_price, mid: p.avg_price };
  const { bid, ask } = m ? bidAsk(m) : { bid: null, ask: null };
  // Backend convention: cost_basis is principal only; unrealized = liq − cost − open_fees.
  const cost = q4(p.count * p.avg_price);
  const liq = q4(p.count * sp.bid);
  return {
    ticker: p.ticker,
    title: m?.title ?? p.ticker,
    event_ticker: m?.event_ticker ?? p.ticker,
    side: p.side,
    count: p.count,
    avg_price: p.avg_price,
    cost_basis: cost,
    open_fees: q4(p.fees),
    mark_price: sp.bid,
    best_bid: sp.bid,
    mark_stale: false,
    liquidation_value: liq,
    unrealized_pnl: q4(liq - cost - p.fees),
    fair_value: p.fair_value,
    expected_edge_total: p.expected_edge_total,
    strategy: p.strategy,
    opened_at: iso(p.opened_at),
    close_time: m ? iso(m.close) : null,
    yes_bid: bid,
    yes_ask: ask,
    url: m ? marketUrl(m) : null,
  };
}

const isResting = (o: Order) => o.tif === "gtc" && (o.status === "open" || o.status === "partially_filled");

function reservedCash(st: MockState): number {
  return st.orders.filter(isResting).reduce((s, o) => s + (o.count - o.filled_count) * o.limit_price, 0);
}

function accountOf(st: MockState): Account {
  const rows = st.positions.map((p) => positionRow(st, p));
  const liq = rows.reduce((s, p) => s + p.liquidation_value, 0);
  const mid = st.positions.reduce((s, p) => {
    const m = st.byTicker.get(p.ticker);
    return s + p.count * (m ? sidePrices(m, p.side).mid : p.avg_price);
  }, 0);
  const cost = rows.reduce((s, p) => s + p.cost_basis + (p.open_fees ?? 0), 0);
  const realized = st.settlements.reduce((s, x) => s + x.pnl, 0) + st.realizedExtra;
  const unrealized = liq - cost;
  const reserved = reservedCash(st);
  const cash = st.startingBalance + realized - cost - reserved + st.cashAdjustment;
  const equity = cash + reserved + liq;
  const wins = st.settlements.filter((s) => s.pnl > 0).length;
  const dayStart = Math.floor(Date.now() / DAY) * DAY;
  const startPt = st.equity.find((p) => Date.parse(p.ts) >= dayStart);
  let peak = -Infinity;
  let mdd = 0;
  for (const p of st.equity) {
    peak = Math.max(peak, p.equity);
    if (peak > 0) mdd = Math.max(mdd, (peak - p.equity) / peak);
  }
  return {
    starting_balance: st.startingBalance,
    cash: q4(cash),
    reserved_cash: q4(reserved),
    positions_liquidation_value: q4(liq),
    positions_mid_value: q4(mid),
    equity: q4(equity),
    equity_mid: q4(cash + reserved + mid),
    realized_pnl: q4(realized),
    unrealized_pnl: q4(unrealized),
    fees_paid: q4(st.feesPaid),
    reserved_profit: q4(st.reservedProfit),
    net_worth: q4(equity + st.reservedProfit),
    profit_sweep_enabled: st.profitSweepEnabled,
    profit_sweep_pct: st.profitSweepPct,
    total_pnl: q4(realized + unrealized),
    total_return_pct: q4(((realized + unrealized) / st.startingBalance) * 100),
    todays_pnl: q4(startPt ? equity - startPt.equity : 0),
    max_drawdown_pct: q4(mdd * 100),
    open_positions: st.positions.length,
    open_orders: st.orders.filter(isResting).length,
    settled_trades: st.settlements.length,
    win_rate: st.settlements.length ? q4(wins / st.settlements.length) : null,
    ts: iso(Date.now()),
  };
}

function statusOf(st: MockState) {
  return {
    mode: "paper",
    engine: {
      running: st.engine.running,
      started_at: st.engine.started_at ? iso(st.engine.started_at) : null,
      last_tick_at: st.engine.last_tick_at ? iso(st.engine.last_tick_at) : null,
      tick_count: st.engine.tick_count,
      universe_size: st.engine.running || st.engine.tick_count ? 3287 : 0,
      last_error: st.engine.last_error,
      last_error_at: st.engine.last_error_at ? iso(st.engine.last_error_at) : null,
      kill_switch: st.engine.kill_switch,
      kill_switch_reason: st.engine.kill_switch ? st.engine.kill_switch_reason : "",
    },
    exchange: { trading_active: true },
    server_time: iso(Date.now()),
  };
}

function strategyRow(st: MockState, s: MStrategy) {
  const pos = st.positions.filter((p) => p.strategy === s.name).map((p) => positionRow(st, p));
  const settled = st.settlements.filter((x) => x.strategy === s.name);
  const wins = settled.filter((x) => x.pnl > 0).length;
  return {
    name: s.name,
    description: s.description,
    enabled: s.enabled,
    params: s.params,
    param_schema: s.param_schema,
    backtestable: s.backtestable,
    // Like the backend: the strategy's own cap, else the account-wide fallback.
    risk_limits: { max_allocation_pct: st.risk.max_strategy_allocation_pct ?? 50, daily_loss_limit: null, paused: null },
    stats: {
      orders: st.orders.filter((o) => o.strategy === s.name).length,
      fills: st.fills.filter((f) => f.strategy === s.name).length,
      open_positions: pos.length,
      settled: settled.length,
      realized_pnl: q4(settled.reduce((a, x) => a + x.pnl, 0)),
      unrealized_pnl: q4(pos.reduce((a, p) => a + p.unrealized_pnl, 0)),
      fees: q4(st.fills.filter((f) => f.strategy === s.name).reduce((a, f) => a + f.fee, 0)),
      win_rate: settled.length ? q4(wins / settled.length) : null,
      exposure: q4(pos.reduce((a, p) => a + p.cost_basis, 0)),
    },
  };
}

function riskOf(st: MockState) {
  const acct = accountOf(st);
  const pos = st.positions.map((p) => positionRow(st, p));
  const byEvent = new Map<string, number>();
  for (const p of pos) byEvent.set(p.event_ticker, (byEvent.get(p.event_ticker) ?? 0) + p.cost_basis);
  const byStrategy = new Map<string, number>();
  for (const p of pos) byStrategy.set(p.strategy, (byStrategy.get(p.strategy) ?? 0) + p.cost_basis);
  const total = pos.reduce((a, p) => a + p.cost_basis, 0) + acct.reserved_cash;
  const evLimit = st.risk.max_exposure_per_event ?? 100;
  const stLimit = ((st.risk.max_strategy_allocation_pct ?? 50) / 100) * acct.equity;
  const now = Date.now();
  return {
    limits: { ...st.risk },
    utilization: {
      total_exposure: q4(total),
      total_exposure_pct: q4((total / Math.max(1, acct.equity)) * 100),
      by_event: [...byEvent.entries()]
        .sort((a, b) => b[1] - a[1])
        .map(([event_ticker, exposure]) => ({ event_ticker, exposure: q4(exposure), limit: evLimit, pct: q4((exposure / evLimit) * 100) })),
      by_strategy: [...byStrategy.entries()]
        .sort((a, b) => b[1] - a[1])
        .map(([strategy, exposure]) => ({ strategy, exposure: q4(exposure), limit: q4(stLimit), pct: q4((exposure / stLimit) * 100) })),
      orders_last_minute: st.ordersTimes.filter((t) => now - t < MIN).length,
      daily_pnl: acct.todays_pnl,
    },
    kill_switch: st.engine.kill_switch,
    kill_switch_reason: st.engine.kill_switch ? st.engine.kill_switch_reason : null,
  };
}

function bootstrapCI(pnls: number[], contracts: number[], iters = 400): [number | null, number | null] {
  const n = pnls.length;
  if (n < 5) return [null, null];
  const means: number[] = [];
  const rr = mulberry32(n * 7919);
  for (let k = 0; k < iters; k++) {
    let sp = 0;
    let sc = 0;
    for (let i = 0; i < n; i++) {
      const j = Math.floor(rr() * n);
      sp += pnls[j] ?? 0;
      sc += contracts[j] ?? 0;
    }
    means.push(sc ? sp / sc : 0);
  }
  means.sort((a, b) => a - b);
  return [q4(means[Math.floor(iters * 0.025)] ?? 0), q4(means[Math.floor(iters * 0.975)] ?? 0)];
}

/** Same fields and readiness wording as the backend's analytics.summarize(). */
function statsOf(rows: MSettlement[], maxDdLimitPct = MOCK_MAX_DRAWDOWN_PCT, minTrades = MOCK_MIN_SETTLED_TRADES) {
  const count = rows.length;
  const contracts = rows.reduce((a, x) => a + x.count, 0);
  const total = rows.reduce((a, x) => a + x.pnl, 0);
  const pnls = rows.map((x) => x.pnl);
  // Per contract (ci_low/ci_high, ci_basis "contract") and per trade (ci_trade_*).
  const [lo, hi] = bootstrapCI(
    pnls,
    rows.map((x) => x.count),
  );
  const [tlo, thi] = bootstrapCI(
    pnls,
    rows.map(() => 1),
  );
  const withEdge = rows.filter((x) => x.expected_edge !== null);
  const expTotal = withEdge.length ? q4(withEdge.reduce((a, x) => a + (x.expected_edge ?? 0) * x.count, 0)) : null;
  const realWithEdge = withEdge.length ? q4(withEdge.reduce((a, x) => a + x.pnl, 0)) : null;
  // Closed-early rows have no outcome, so they are left out of the Brier score.
  const withFv = rows.filter((x) => x.fair_value !== null && x.kind === "settlement");
  const brier = withFv.length ? withFv.reduce((a, x) => a + ((x.fair_value ?? 0) - (x.won ? 1 : 0)) ** 2, 0) / withFv.length : null;
  const asc = [...rows].sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
  let cum = 0;
  let peak = 0;
  let mdd = 0;
  for (const x of asc) {
    cum += x.pnl;
    peak = Math.max(peak, cum);
    mdd = Math.max(mdd, peak - cum);
  }
  const mddPct = (mdd / 1000) * 100;
  const fails: string[] = [];
  const passes: string[] = [];
  if (count < minTrades) fails.push(`only ${count} settled trades (need >= ${minTrades})`);
  else passes.push(`${count} settled trades >= ${minTrades}`);
  if (tlo === null) fails.push("no confidence interval for mean P&L per trade yet (need trades in >= 2 events)");
  else if (tlo <= 0) fails.push(`95% CI lower bound of mean P&L per trade is $${tlo.toFixed(4)} (must be > 0)`);
  else passes.push(`95% CI lower bound of mean P&L per trade $${tlo.toFixed(4)} > 0`);
  if (mddPct > maxDdLimitPct) fails.push(`max drawdown ${mddPct.toFixed(2)}% exceeds ${maxDdLimitPct}%`);
  else passes.push(`max drawdown ${mddPct.toFixed(2)}% <= ${maxDdLimitPct}%`);
  return {
    count,
    settled_count: rows.filter((x) => x.kind === "settlement").length,
    closed_count: rows.filter((x) => x.kind === "close").length,
    contracts,
    total_pnl: q4(total),
    realized_pnl: q4(total),
    fees: q4(rows.reduce((a, x) => a + (x.fees ?? 0), 0)),
    mean_pnl_per_contract: contracts ? q4(total / contracts) : null,
    mean_pnl_per_trade: count ? q4(total / count) : null,
    ci_low: lo,
    ci_high: hi,
    ci_basis: "contract",
    ci_trade_low: tlo,
    ci_trade_high: thi,
    expected_edge_total: expTotal,
    realized_pnl_with_edge: realWithEdge,
    edge_capture: expTotal && realWithEdge !== null ? q4(realWithEdge / expTotal) : null,
    trades_with_edge: withEdge.length,
    brier: brier === null ? null : q4(brier),
    win_rate: count ? q4(rows.filter((x) => x.pnl > 0).length / count) : null,
    max_drawdown: q4(mdd),
    max_drawdown_pct: q4(mddPct),
    readiness: { ready: fails.length === 0, reasons: fails.length ? fails : passes },
  };
}

function analyticsOf(st: MockState) {
  const overall = statsOf(st.settlements);
  const by_strategy: Record<string, ReturnType<typeof statsOf>> = {};
  for (const s of st.strategies) {
    const rows = st.settlements.filter((x) => x.strategy === s.name);
    if (rows.length) by_strategy[s.name] = statsOf(rows);
  }
  const calibration: CalibrationBucket[] = [];
  const resolved = st.settlements.filter((x) => x.kind === "settlement");
  for (let b = 0; b < 10; b++) {
    const lo = b / 10;
    const hi = lo + 0.1;
    const rows = resolved.filter((x) => x.fair_value !== null && x.fair_value >= lo && (b === 9 ? x.fair_value <= hi : x.fair_value < hi));
    if (!rows.length) continue;
    calibration.push({
      bucket: `${lo.toFixed(1)}–${hi.toFixed(1)}`,
      n: rows.length,
      mean_fair_value: q4(rows.reduce((a, x) => a + (x.fair_value ?? 0), 0) / rows.length),
      realized_rate: q4(rows.filter((x) => x.won).length / rows.length),
    });
  }
  return {
    overall,
    by_strategy,
    calibration,
    readiness: overall.readiness,
    params: { min_settled_trades: MOCK_MIN_SETTLED_TRADES, max_drawdown_pct: MOCK_MAX_DRAWDOWN_PCT, n_boot: 400, ci: 0.95, cluster: "event_ticker" },
  };
}

// ---------------------------------------------------------------------------
// Backtests
// ---------------------------------------------------------------------------

function genBacktestDetail(summary: BacktestSummary, seed: number, startingBalance = 1000): BacktestDetail {
  const rr = mulberry32(seed * 104729);
  const g = () => {
    const u = Math.max(1e-9, rr());
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * rr());
  };
  const start = Date.parse(summary.start ?? "2025-01-01T00:00:00Z");
  const end = Date.parse(summary.end ?? "2025-12-31T00:00:00Z");
  const days = Math.max(10, Math.round((end - start) / DAY));
  const edge = summary.strategy === "mutex_arb" ? 0.012 : summary.strategy === "favorite_longshot" ? 0.006 : 0.004;
  const trades: BacktestTrade[] = [];
  const nTrades = Math.min(480, Math.round(days * (summary.strategy === "crypto_fv" ? 1.3 : 0.9)));
  for (let i = 0; i < nTrades; i++) {
    const t = start + rr() * (end - start);
    const price =
      summary.strategy === "favorite_longshot" ? 0.9 + rr() * 0.07 : summary.strategy === "mutex_arb" ? 0.85 + rr() * 0.1 : 0.2 + rr() * 0.6;
    const count = 1 + Math.floor(rr() * 20);
    const pWin = Math.min(0.995, price + edge + 0.01);
    const won = rr() < pWin;
    const f = fee(price, count);
    const pnl = q4((won ? count : 0) - count * price - f);
    const side = rr() < 0.7 ? "yes" : "no";
    const series = summary.strategy === "crypto_fv" ? "KXBTCD" : summary.strategy === "mutex_arb" ? "KXHIGHNY" : rr() < 0.5 ? "KXFED" : "KXHIGHCHI";
    const ev = `${series}-${dayCode(t)}`;
    trades.push({
      ts: iso(t),
      ticker: `${ev}-T${Math.round(price * 100)}`,
      event_ticker: ev,
      side,
      count,
      price: q2(price),
      fee: f,
      result: won ? side : side === "yes" ? "no" : "yes",
      pnl,
      settled_at: iso(t + (2 + rr() * 30) * HOUR),
      reason: summary.strategy === "crypto_fv" ? `FV ${(Math.min(0.99, price + 0.05) * 100).toFixed(1)}¢ vs ask ${Math.round(price * 100)}¢` : `entry at ${Math.round(price * 100)}¢`,
    });
  }
  trades.sort((a, b) => Date.parse(a.ts ?? "") - Date.parse(b.ts ?? ""));
  // Daily equity curve.
  const curve: { ts: string; equity: number }[] = [];
  let eq = startingBalance;
  let ti = 0;
  let peak = eq;
  let mdd = 0;
  let mddPct = 0;
  const daily: number[] = [];
  for (let d = 0; d <= days; d++) {
    const t = start + d * DAY;
    let dayPnl = 0;
    while (ti < trades.length && Date.parse(trades[ti]!.ts ?? "") < t) {
      dayPnl += trades[ti]!.pnl ?? 0;
      ti++;
    }
    eq += dayPnl + g() * 0.3;
    daily.push(dayPnl);
    peak = Math.max(peak, eq);
    mdd = Math.max(mdd, peak - eq);
    mddPct = Math.max(mddPct, ((peak - eq) / peak) * 100);
    curve.push({ ts: iso(t), equity: q4(eq) });
  }
  const months = new Map<string, { pnl: number; trades: number; contracts: number; wins: number }>();
  for (const tr of trades) {
    const k = (tr.ts ?? "").slice(0, 7);
    const m = months.get(k) ?? { pnl: 0, trades: 0, contracts: 0, wins: 0 };
    m.pnl += tr.pnl ?? 0;
    m.trades += 1;
    m.contracts += tr.count;
    m.wins += (tr.pnl ?? 0) > 0 ? 1 : 0;
    months.set(k, m);
  }
  const by_month: BacktestMonth[] = [...months.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([month, m]) => ({ month, pnl: q4(m.pnl), trades: m.trades, contracts: m.contracts, win_rate: q4(m.wins / m.trades) }));
  const total = trades.reduce((a, t) => a + (t.pnl ?? 0), 0);
  const contracts = trades.reduce((a, t) => a + t.count, 0);
  const [lo, hi] = bootstrapCI(
    trades.map((t) => t.pnl ?? 0),
    trades.map((t) => t.count),
    300,
  );
  const mean = daily.reduce((a, x) => a + x, 0) / Math.max(1, daily.length);
  const sd = Math.sqrt(daily.reduce((a, x) => a + (x - mean) ** 2, 0) / Math.max(1, daily.length - 1));
  const metrics = {
    total_pnl: q4(total),
    total_return_pct: q4((total / startingBalance) * 100),
    final_equity: q4(eq),
    n_trades: trades.length,
    contracts,
    ev_per_contract: contracts ? q4(total / contracts) : null,
    ev_ci_low: lo,
    ev_ci_high: hi,
    hit_rate: trades.length ? q4(trades.filter((t) => (t.pnl ?? 0) > 0).length / trades.length) : null,
    max_drawdown: q4(mdd),
    max_drawdown_pct: q4(mddPct),
    sharpe: sd > 0 ? q4((mean / sd) * Math.sqrt(365)) : null,
    fees: q4(trades.reduce((a, t) => a + (t.fee ?? 0), 0)),
    events: Math.round(trades.length * 0.6),
  };
  return {
    id: summary.id,
    strategy: summary.strategy,
    params: summary.params,
    status: "done",
    error: null,
    metrics,
    equity_curve: curve,
    trades,
    by_month,
    start: summary.start,
    end: summary.end,
    period_reported: true,
    created_at: summary.created_at,
  };
}

function genBacktests(now: number): MBacktest[] {
  const strategies = genStrategies();
  const params = (n: string) => strategies.find((s) => s.name === n)?.params ?? {};
  const defs: [number, string, string, string, number, string | null][] = [
    [1, "favorite_longshot", "2025-01-01", "2025-12-31", 6.2, null],
    [2, "mutex_arb", "2025-03-01", "2026-06-30", 3.1, null],
    [3, "crypto_fv", "2025-06-01", "2026-08-31", 1.4, null],
    [4, "crypto_fv", "2024-01-01", "2024-06-30", 0.8, "No candle data for series KXETHD before 2025-03-01; nothing to replay."],
  ];
  return defs.map(([id, strategy, start, end, daysAgo, error]) => {
    const summary: BacktestSummary = {
      id,
      strategy,
      params: params(strategy),
      start,
      end,
      period_reported: true,
      status: error ? "failed" : "done",
      created_at: iso(now - daysAgo * DAY),
      metrics: null,
    };
    if (error) {
      return {
        summary,
        detail: {
          id,
          strategy,
          params: summary.params,
          status: "failed",
          error,
          metrics: null,
          equity_curve: [],
          trades: [],
          by_month: [],
          start,
          end,
          period_reported: true,
          created_at: summary.created_at,
        },
        finishAt: null,
      };
    }
    const detail = genBacktestDetail(summary, id);
    summary.metrics = detail.metrics;
    return { summary, detail, finishAt: null };
  });
}

// ---------------------------------------------------------------------------
// Simulation + SSE
// ---------------------------------------------------------------------------

let state: MockState | null = null;
function S(): MockState {
  if (!state) {
    state = buildState(Date.now());
    startSimulation();
  }
  return state;
}

type Listener = (type: StreamEventType, data: unknown) => void;
const listeners = new Set<Listener>();
function emit(type: StreamEventType, data: unknown) {
  for (const l of listeners) l(type, data);
}

function pushLog(st: MockState, level: string, kind: string, message: string, data: Record<string, unknown> | null = null) {
  const e: LogEntry = { id: st.nextLogId++, ts: iso(Date.now()), level, kind, message, data };
  st.logs.unshift(e);
  if (st.logs.length > 1000) st.logs.length = 1000;
  // Like the backend's BusLogHandler, the live copy carries no store id.
  emit("log", { ts: e.ts, level, kind, message, data });
}

let simTimer: ReturnType<typeof setInterval> | null = null;
let step = 0;

function startSimulation() {
  if (simTimer) return;
  simTimer = setInterval(simulateStep, 2500);
}

function simulateStep() {
  const st = state;
  if (!st) return;
  step++;
  const now = Date.now();
  rand = mulberry32((now & 0xffffffff) ^ step);
  // Prices drift whether or not the engine runs.
  for (const m of st.markets) {
    if (r() < 0.3) {
      m.mid = clampP(m.mid + rgauss() * 0.006 + (m.fair - m.mid) * 0.02);
      if (r() < 0.2) m.last = q2(clampP(m.mid + rgauss() * 0.01));
      m.vol24 += r() < 0.3 ? rint(1, 30) : 0;
    }
  }
  // Expire settled/closed markets' open orders.
  for (const o of st.orders) {
    if ((o.status === "open" || o.status === "partially_filled") && o.tif === "gtc" && o.expires_at && Date.parse(o.expires_at) < now) {
      o.status = "expired";
      o.updated_at = iso(now);
      emit("order", o);
    }
  }
  if (st.engine.running) {
    if (step % 4 === 0) {
      st.engine.tick_count++;
      st.engine.last_tick_at = now;
      emit("tick", { ts: iso(now), tick_count: st.engine.tick_count, universe_size: 3287, duration_ms: rint(180, 900), intents: rint(0, 5) });
    }
    if (r() < 0.01) {
      const bad = makeBadSignal(st, now);
      st.signals.unshift(bad);
      emit("signal", bad);
      pushLog(st, "warning", "strategy", `${bad.strategy}: ${bad.decision_reason}`);
    }
    if (r() < 0.45) {
      const sig = makeSignal(st, now);
      st.signals.unshift(sig);
      if (st.signals.length > 2000) st.signals.length = 2000;
      emit("signal", sig);
      if (sig.decision === "executed" || sig.decision === "partial") {
        const m = st.byTicker.get(sig.ticker);
        const filled = sig.decision === "partial" ? Math.max(1, Math.floor(sig.count / 2)) : sig.count;
        const f = fee(sig.limit_price, filled);
        const oid = st.nextId++;
        const order: Order = {
          id: oid,
          ticker: sig.ticker,
          title: sig.title,
          side: sig.side,
          action: "buy",
          count: sig.count,
          filled_count: filled,
          limit_price: sig.limit_price,
          avg_fill_price: sig.limit_price,
          tif: "ioc",
          status: filled < sig.count ? "partially_filled" : "filled",
          strategy: sig.strategy,
          reason: sig.reason,
          expected_edge: sig.expected_edge,
          fair_value: sig.fair_value,
          group_id: sig.strategy === "mutex_arb" ? `basket-${m?.event_ticker ?? ""}` : null,
          queue_ahead: null,
          created_at: iso(now),
          updated_at: iso(now),
          expires_at: null,
          fees: f,
        };
        st.orders.unshift(order);
        st.ordersTimes.push(now);
        st.ordersTimes = st.ordersTimes.filter((t) => now - t < 5 * MIN);
        const fill: Fill = {
          id: st.nextId++,
          order_id: oid,
          ticker: sig.ticker,
          title: sig.title,
          side: sig.side,
          action: "buy",
          count: filled,
          price: sig.limit_price,
          fee: f,
          is_taker: true,
          ts: iso(now),
          strategy: sig.strategy,
        };
        st.fills.unshift(fill);
        st.feesPaid += f;
        const existing = st.positions.find((p) => p.ticker === sig.ticker && p.side === sig.side);
        if (existing) {
          const n = existing.count + filled;
          existing.avg_price = q4((existing.avg_price * existing.count + sig.limit_price * filled) / n);
          existing.count = n;
          existing.fees += f;
        } else {
          st.positions.push({
            ticker: sig.ticker,
            side: sig.side,
            count: filled,
            avg_price: sig.limit_price,
            fees: f,
            fair_value: sig.fair_value,
            expected_edge_total: sig.expected_edge === null ? null : q4(sig.expected_edge * filled),
            strategy: sig.strategy,
            opened_at: now,
          });
        }
        emit("order", order);
        emit("fill", fill);
        pushLog(st, "info", "fill", `${sig.strategy}: bought ${filled} ${sig.side.toUpperCase()} ${sig.ticker} @ ${Math.round(sig.limit_price * 100)}¢ (fee $${f.toFixed(2)})`);
      } else if (sig.decision === "rejected") {
        pushLog(st, "warning", "risk", `Rejected ${sig.strategy} ${sig.ticker}: ${sig.decision_reason}`);
      }
    }
    if (r() < 0.06 && st.positions.length > 3) {
      const idx = Math.floor(r() * st.positions.length);
      const p = st.positions[idx];
      if (p) {
        const m = st.byTicker.get(p.ticker);
        const pWin = m ? sidePrices(m, p.side).mid : 0.5;
        const won = r() < pWin;
        const cost = q4(p.count * p.avg_price);
        const payout = won ? p.count : 0;
        const s: MSettlement = {
          id: st.nextId++,
          ticker: p.ticker,
          title: m?.title ?? p.ticker,
          kind: "settlement",
          result: won ? p.side : p.side === "yes" ? "no" : "yes",
          side: p.side,
          count: p.count,
          payout,
          cost_basis: cost,
          fees: q4(p.fees),
          pnl: q4(payout - cost - p.fees),
          ts: iso(now),
          strategy: p.strategy,
          fair_value: p.fair_value,
          expected_edge: p.expected_edge_total !== null ? q4(p.expected_edge_total / p.count) : null,
          won,
          entry_price: p.avg_price,
          entry_fee: p.fees,
        };
        st.positions.splice(idx, 1);
        st.settlements.unshift(s);
        emit("settlement", publicSettlement(s));
        pushLog(st, "info", "settlement", `Settled ${p.ticker} → ${s.result.toUpperCase()}: ${s.pnl >= 0 ? "+" : "−"}$${Math.abs(s.pnl).toFixed(2)} (${p.strategy})`);
      }
    }
    if (r() < 0.03) pushLog(st, "info", "universe", `Universe refreshed: ${rint(3200, 3350)} open markets`);
    if (r() < 0.01) pushLog(st, "error", "strategy", "crypto_fv.on_tick raised TimeoutError: spot feed stale — skipped this tick");
    if (r() < 0.002) {
      // A failed job: recorded as the engine's last_error (kept after it recovers).
      st.engine.last_error = "marketdata: ReadTimeout: timed out reading /trade-api/v2/markets/orderbooks";
      st.engine.last_error_at = now;
      pushLog(st, "warning", "engine", "marketdata failed (ReadTimeout: timed out); retry in 20s");
    }
  }
  // Equity snapshot at most every 30 s (so the 1d chart visibly moves).
  const acct = accountOf(st);
  const last = st.equity[st.equity.length - 1];
  if (!last || now - Date.parse(last.ts) >= 30_000) {
    st.equity.push({
      ts: iso(now),
      equity: acct.equity,
      equity_mid: acct.equity_mid,
      cash: acct.cash,
      realized_pnl: acct.realized_pnl,
      unrealized_pnl: acct.unrealized_pnl,
    });
  }
  emit("account", acct);
  // Finish running backtests.
  for (const b of st.backtests) {
    if (b.finishAt !== null && now >= b.finishAt) {
      b.finishAt = null;
      const d = genBacktestDetail(b.summary, Number(b.summary.id), 1000);
      b.detail = d;
      b.summary.status = "done";
      b.summary.metrics = d.metrics;
      pushLog(st, "info", "backtest", `Backtest #${b.summary.id} (${b.summary.strategy}) finished: ${d.trades.length} trades`);
    }
  }
}

/** Simulated EventSource used by client.ts in mock mode. */
export function openMockStream(_url: string, h: StreamHandlers): StreamSource {
  S();
  let open = false;
  const listener: Listener = (type, data) => {
    if (open) h.onMessage(type, JSON.stringify(data));
  };
  const t = setTimeout(() => {
    open = true;
    listeners.add(listener);
    h.onOpen();
  }, 250);
  const handle: StreamSource & { fail: () => void } = {
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

// Dev helpers: `__kalshibotMock.dropStream()` in the console exercises reconnect/backoff;
// `__kalshibotMock.coinbaseUnavailable(true)` makes /overview report the Coinbase venue
// as unavailable (the Kalshi side must keep working).
if (typeof window !== "undefined") {
  (window as unknown as Record<string, unknown>).__kalshibotMock = {
    dropStream: () => [...failers].forEach((f) => f.fail()),
    state: () => S(),
    coinbaseUnavailable: (on = true) => {
      cbUnavailable = on ? "ImportError: kalshibot.coinbase.engine (simulated in mock mode)" : null;
    },
  };
}

// ---------------------------------------------------------------------------
// GET /overview — both venues (COINBASE_CONTRACT §13)
// ---------------------------------------------------------------------------

let cbUnavailable: string | null = null;

/**
 * The Coinbase mock (api/coinbase/mock.ts, owned by the Coinbase UI) when it exists, so
 * the Overview shows the same Coinbase account as the Coinbase pages. import.meta.glob
 * resolves to {} when the file is absent, so this never breaks the build.
 */
const CB_MOCK = import.meta.glob("./coinbase/mock.ts") as Record<string, () => Promise<Record<string, unknown>>>;
type CbMockFn = (method: string, path: string, body: unknown) => Promise<MockResponse>;

async function coinbaseMockRequest(path: string): Promise<unknown> {
  const load = CB_MOCK["./coinbase/mock.ts"];
  if (!load) return undefined;
  try {
    const mod = await load();
    const fn = (mod.cbMockRequest ?? mod.mockRequest) as CbMockFn | undefined;
    if (typeof fn !== "function") return undefined;
    const res = await fn("GET", path, undefined);
    return res.status < 400 ? res.body : undefined;
  } catch {
    return undefined;
  }
}

/**
 * Self-contained stand-in for the Coinbase account when the Coinbase mock is missing:
 * a slow trend-following book (~45 % BTC/ETH exposure, 1.2 % taker fees) started 21
 * days ago, hourly equity snapshots.
 */
interface CbFallback {
  series: { ts: string; equity: number }[];
  fees: number;
}
let cbFallback: CbFallback | null = null;
function coinbaseFallback(now: number): CbFallback {
  if (cbFallback) {
    // Keep it moving (one point per 30 s while the page is open).
    const last = cbFallback.series[cbFallback.series.length - 1];
    if (last && now - Date.parse(last.ts) >= 30_000) {
      cbFallback.series.push({ ts: iso(now), equity: q4(last.equity * (1 + rgauss() * 0.0006)) });
    }
    return cbFallback;
  }
  const rnd = mulberry32(84475);
  const g = () => {
    const u = Math.max(1e-9, rnd());
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * rnd());
  };
  const hours = 21 * 24;
  const series: { ts: string; equity: number }[] = [];
  let eq = 1000;
  let fees = 0;
  for (let i = 0; i <= hours; i++) {
    const t = now - (hours - i) * HOUR;
    if (i > 0) {
      // BTC-like hourly vol (~0.6 %) at ~45 % exposure, slight positive drift.
      eq *= 1 + 0.45 * (0.00012 + g() * 0.006);
      // A daily rebalance pays taker fees on ~8 % turnover.
      if (i % 24 === 0) {
        const fee = eq * 0.08 * 0.012;
        eq -= fee;
        fees += fee;
      }
    }
    series.push({ ts: iso(t), equity: q4(eq) });
  }
  cbFallback = { series, fees: q4(fees) };
  return cbFallback;
}

/** Every `every`-th point plus the last one (the overview chart does not need 10-min detail). */
function thin<T>(xs: T[], every: number): T[] {
  return xs.filter((_, i) => i % every === 0 || i === xs.length - 1);
}

async function overviewOf(st: MockState, now: number) {
  const a = accountOf(st);
  const s = statusOf(st);
  const kalshi = {
    venue: "kalshi",
    label: "KALSHI · prediction markets",
    available: true,
    engine_running: s.engine.running,
    kill_switch: s.engine.kill_switch,
    starting_balance: a.starting_balance,
    equity: a.equity,
    cash: a.cash,
    total_pnl: a.total_pnl,
    total_return_pct: a.total_return_pct,
    todays_pnl: a.todays_pnl,
    open_positions: a.open_positions,
    fees_paid: a.fees_paid,
    last_error: s.engine.last_error,
    last_error_at: s.engine.last_error_at,
    last_tick_at: s.engine.last_tick_at,
  };
  const kSeries = thin(st.equity.filter((p) => now - Date.parse(p.ts) <= 30 * DAY), 6).map((p) => ({ ts: p.ts, equity: p.equity }));

  let coinbase: Record<string, unknown>;
  let cSeries: { ts: string; equity: number }[] = [];
  if (cbUnavailable) {
    coinbase = { venue: "coinbase", label: "COINBASE · crypto spot", available: false, unavailable_reason: cbUnavailable };
  } else {
    const [acct, status, eq] = await Promise.all([
      coinbaseMockRequest("/account"),
      coinbaseMockRequest("/status"),
      coinbaseMockRequest("/equity?range=30d"),
    ]);
    const ca = (acct ?? null) as Record<string, unknown> | null;
    const cs = (status ?? null) as { engine?: Record<string, unknown> } | null;
    if (ca && typeof ca.equity === "number") {
      const e = cs?.engine ?? {};
      coinbase = {
        venue: "coinbase",
        label: "COINBASE · crypto spot",
        available: true,
        unavailable_reason: null,
        engine_running: e.running ?? true,
        kill_switch: e.kill_switch ?? false,
        starting_balance: ca.starting_balance,
        equity: ca.equity,
        cash: ca.cash,
        total_pnl: ca.total_pnl,
        total_return_pct: ca.total_return_pct,
        todays_pnl: ca.todays_pnl,
        open_positions: ca.open_positions,
        fees_paid: ca.fees_paid,
        last_error: e.last_error ?? null,
        last_error_at: e.last_error_at ?? null,
        last_tick_at: e.last_bar_at ?? e.last_tick_at ?? null,
      };
      const pts = Array.isArray(eq) ? (eq as { ts?: unknown; equity?: unknown }[]) : [];
      cSeries = pts
        .filter((p) => typeof p.ts === "string" && typeof p.equity === "number")
        .map((p) => ({ ts: p.ts as string, equity: p.equity as number }));
    } else {
      const fb = coinbaseFallback(now);
      const first = fb.series[0]?.equity ?? 1000;
      const last = fb.series[fb.series.length - 1]?.equity ?? first;
      const dayStart = Math.floor(now / DAY) * DAY;
      const open = fb.series.find((p) => Date.parse(p.ts) >= dayStart)?.equity ?? last;
      coinbase = {
        venue: "coinbase",
        label: "COINBASE · crypto spot",
        available: true,
        unavailable_reason: null,
        engine_running: true,
        kill_switch: false,
        starting_balance: 1000,
        equity: q4(last),
        cash: q4(last * 0.55),
        total_pnl: q4(last - 1000),
        total_return_pct: q4(((last - 1000) / 1000) * 100),
        todays_pnl: q4(last - open),
        open_positions: 2,
        fees_paid: fb.fees,
        last_error: null,
        last_error_at: null,
        last_tick_at: iso(Math.floor(now / HOUR) * HOUR + 30_000),
      };
      cSeries = fb.series;
    }
  }
  const venues = [kalshi, coinbase].filter((v) => v.available);
  const sum = (k: string) => q4(venues.reduce((acc, v) => acc + Number((v as Record<string, unknown>)[k] ?? 0), 0));
  const sb = sum("starting_balance");
  const pnl = sum("total_pnl");
  return {
    generated_at: iso(now),
    venues: { kalshi, coinbase },
    combined: {
      starting_balance: sb,
      equity: sum("equity"),
      total_pnl: pnl,
      total_return_pct: sb ? q4((pnl / sb) * 100) : 0,
      note: "Sum of two separate paper accounts",
    },
    equity_series: { kalshi: kSeries, coinbase: cSeries },
  };
}

// ---------------------------------------------------------------------------
// Request router
// ---------------------------------------------------------------------------

export interface MockResponse {
  status: number;
  body: unknown;
}

const ok = (body: unknown): MockResponse => ({ status: 200, body: clone(body) });
const err = (status: number, detail: string): MockResponse => ({ status, body: { detail } });
const sleep = (ms: number) => new Promise((res) => setTimeout(res, ms));

function limitParam(q: URLSearchParams, dflt = 200): number {
  const n = Number(q.get("limit") ?? dflt);
  return Number.isFinite(n) && n > 0 ? Math.min(5000, n) : dflt;
}

function validateParams(s: MStrategy, patch: Record<string, unknown>): string | null {
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

export async function mockRequest(method: string, rawPath: string, body: unknown): Promise<MockResponse> {
  await sleep(rbetween(90, 320));
  const st = S();
  const u = new URL(rawPath, "http://mock.local");
  const path = u.pathname.replace(/\/+$/, "");
  const q = u.searchParams;
  const b = (typeof body === "object" && body !== null ? body : {}) as Record<string, unknown>;
  const now = Date.now();

  if (method === "GET" && path === "/status") return ok(statusOf(st));
  if (method === "GET" && path === "/overview") return ok(await overviewOf(st, now));
  if (method === "POST" && path === "/engine/start") {
    if (!st.engine.running) {
      st.engine.running = true;
      st.engine.started_at = now;
      pushLog(st, "info", "engine", "Engine started (paper mode)");
    }
    return ok(statusOf(st));
  }
  if (method === "POST" && path === "/engine/stop") {
    if (st.engine.running) {
      st.engine.running = false;
      pushLog(st, "info", "engine", "Engine stopped by user");
    }
    return ok(statusOf(st));
  }
  if (method === "POST" && path === "/engine/kill-switch") {
    if (typeof b.on !== "boolean") return err(422, "on: field required (boolean)");
    const was = st.engine.kill_switch;
    st.engine.kill_switch = b.on;
    st.engine.kill_switch_reason = b.on ? "manual" : "";
    pushLog(st, b.on ? "warning" : "info", "risk", b.on ? "Kill switch ENGAGED by user: new entries blocked" : "Kill switch released by user");
    if (b.on && !was) {
      // Backend Engine.set_kill_switch(True) → broker.cancel_all(): every resting order.
      const resting = st.orders.filter(isResting);
      for (const o of resting) {
        o.status = "cancelled";
        o.updated_at = iso(now);
        emit("order", o);
      }
      if (resting.length) pushLog(st, "warning", "risk", `kill switch on: cancelled ${resting.length} resting orders`);
    }
    return ok(statusOf(st));
  }
  if (method === "GET" && path === "/account") return ok(accountOf(st));
  if (method === "PATCH" && path === "/account") {
    if (b.profit_sweep_enabled === undefined && b.profit_sweep_pct === undefined) return err(422, "no fields to update");
    if (b.profit_sweep_pct !== undefined) {
      const pct = Number(b.profit_sweep_pct);
      if (!Number.isFinite(pct) || pct < 0 || pct > 100) return err(422, "profit_sweep_pct: must be between 0 and 100");
      st.profitSweepPct = pct;
    }
    if (b.profit_sweep_enabled !== undefined) st.profitSweepEnabled = Boolean(b.profit_sweep_enabled);
    return ok(accountOf(st));
  }
  if (method === "POST" && path === "/account/withdraw-profit") {
    if (b.amount !== undefined && b.pct !== undefined) return err(422, "give amount or pct, not both");
    let moved = st.reservedProfit;
    if (b.pct !== undefined) {
      const pct = Number(b.pct);
      if (!Number.isFinite(pct) || pct < 0 || pct > 100) return err(422, "pct: must be between 0 and 100");
      moved = st.reservedProfit * (pct / 100);
    } else if (b.amount !== undefined) {
      const amt = Number(b.amount);
      if (!Number.isFinite(amt) || amt < 0) return err(422, "amount: must be >= 0");
      moved = amt;
    }
    moved = Math.min(moved, st.reservedProfit);
    st.reservedProfit = q4(st.reservedProfit - moved);
    st.cashAdjustment += moved;
    return ok(accountOf(st));
  }
  if (method === "POST" && path === "/account/reset") {
    const sb = b.starting_balance === undefined ? st.startingBalance : Number(b.starting_balance);
    if (!Number.isFinite(sb) || sb <= 0) return err(422, "starting_balance: must be a positive number");
    const bts = st.backtests;
    state = buildState(now, sb, true);
    state.backtests = bts;
    return ok(accountOf(state));
  }
  if (method === "GET" && path === "/equity") {
    const range = q.get("range") ?? "7d";
    const spans: Record<string, [number, number]> = { "1d": [DAY, 1], "7d": [7 * DAY, 2], "30d": [30 * DAY, 6], all: [Infinity, 12] };
    const spec = spans[range];
    if (!spec) return err(422, "range: must be one of 1d, 7d, 30d, all");
    const [span, every] = spec;
    const pts = st.equity.filter((p) => now - Date.parse(p.ts) <= span);
    const out = pts.filter((_, i) => i % every === 0 || i === pts.length - 1 || now - Date.parse(pts[i]?.ts ?? "") < 2 * HOUR);
    return ok(out);
  }
  if (method === "GET" && path === "/positions") return ok(st.positions.map((p) => positionRow(st, p)));
  if (method === "GET" && path === "/orders") {
    const status = q.get("status") ?? "open";
    const rows = status === "all" ? st.orders : st.orders.filter(isResting);
    return ok(rows.slice(0, limitParam(q)));
  }
  let m = /^\/orders\/([^/]+)\/cancel$/.exec(path);
  if (method === "POST" && m) {
    const id = decodeURIComponent(m[1] ?? "");
    const o = st.orders.find((x) => String(x.id) === id);
    if (!o) return err(404, `Order ${id} not found`);
    // Like POST /api/orders/{id}/cancel: an order that is no longer open is a 409.
    if (!isResting(o)) return err(409, `order ${id} is ${o.status}, not open`);
    o.status = "cancelled";
    o.updated_at = iso(now);
    pushLog(st, "info", "order", `Cancelled order ${id} on ${o.ticker} (user)`);
    emit("order", o);
    return ok(o);
  }
  if (method === "GET" && path === "/fills") return ok(st.fills.slice(0, limitParam(q)));
  if (method === "GET" && path === "/settlements") {
    return ok(st.settlements.slice(0, limitParam(q)).map(publicSettlement));
  }
  if (method === "GET" && path === "/strategies") return ok(st.strategies.map((s) => strategyRow(st, s)));
  m = /^\/strategies\/([^/]+)$/.exec(path);
  if (method === "PATCH" && m) {
    const name = decodeURIComponent(m[1] ?? "");
    const s = st.strategies.find((x) => x.name === name);
    if (!s) return err(404, `Unknown strategy '${name}'`);
    if (b.params !== undefined) {
      if (typeof b.params !== "object" || b.params === null) return err(422, "params: must be an object");
      const e = validateParams(s, b.params as Record<string, unknown>);
      if (e) return err(422, e);
      s.params = { ...s.params, ...(b.params as Record<string, ParamValue>) };
      pushLog(st, "info", "strategy", `${name}: parameters updated`, { params: s.params });
    }
    if (b.enabled !== undefined) {
      if (typeof b.enabled !== "boolean") return err(422, "enabled: must be a boolean");
      s.enabled = b.enabled;
      pushLog(st, "info", "strategy", `${name} ${b.enabled ? "enabled" : "disabled"}`);
    }
    return ok(strategyRow(st, s));
  }
  if (method === "GET" && path === "/risk") return ok(riskOf(st));
  if (method === "PATCH" && path === "/risk") {
    for (const [k, v] of Object.entries(b)) {
      if (!(k in st.risk)) return err(422, `${k}: unknown risk limit`);
      if (typeof v !== "number" || !Number.isFinite(v) || v < 0) return err(422, `${k}: must be a non-negative number`);
    }
    for (const [k, v] of Object.entries(b)) st.risk[k] = v as number;
    pushLog(st, "info", "risk", `Risk limits updated: ${Object.keys(b).join(", ")}`);
    return ok(riskOf(st));
  }
  if (method === "GET" && path === "/signals") return ok(st.signals.slice(0, limitParam(q)));
  if (method === "GET" && path === "/logs") return ok(st.logs.slice(0, limitParam(q)));
  if (method === "GET" && path === "/markets") {
    const search = (q.get("search") ?? "").trim().toLowerCase();
    const category = (q.get("category") ?? "").trim().toLowerCase();
    const sort = q.get("sort") ?? "volume_24h";
    let rows = st.markets.map(marketRow);
    if (search) rows = rows.filter((x) => x.ticker.toLowerCase().includes(search) || x.title.toLowerCase().includes(search));
    if (category) rows = rows.filter((x) => x.category.toLowerCase() === category);
    if (sort === "close_time") rows.sort((a, c) => Date.parse(a.close_time ?? "") - Date.parse(c.close_time ?? ""));
    else if (sort === "spread") rows.sort((a, c) => (a.spread ?? 9) - (c.spread ?? 9));
    else rows.sort((a, c) => c.volume_24h - a.volume_24h);
    return ok(rows.slice(0, limitParam(q, 100)));
  }
  if (method === "GET" && path === "/analytics") return ok(analyticsOf(st));
  if (method === "GET" && path === "/backtests") {
    return ok([...st.backtests].reverse().map((x) => x.summary));
  }
  if (method === "POST" && path === "/backtests") {
    const strategy = String(b.strategy ?? "");
    const s = st.strategies.find((x) => x.name === strategy);
    if (!s) return err(422, `strategy: unknown strategy '${strategy}'`);
    if (!s.backtestable) return err(422, `strategy: ${strategy} is not backtestable (needs order-book/trade replay)`);
    const params = { ...s.params, ...((b.params as Record<string, ParamValue> | undefined) ?? {}) };
    const e = validateParams(s, params);
    if (e) return err(422, e);
    const start = typeof b.start === "string" && b.start ? b.start : "2025-01-01";
    const end = typeof b.end === "string" && b.end ? b.end : "2026-08-31";
    if (Date.parse(start) >= Date.parse(end)) return err(422, "start must be before end");
    const id = Math.max(0, ...st.backtests.map((x) => Number(x.summary.id))) + 1;
    const summary: BacktestSummary = { id, strategy, params, start, end, period_reported: true, status: "running", created_at: iso(now), metrics: null };
    st.backtests.push({ summary, detail: null, finishAt: now + 6000 + rint(0, 3000) });
    pushLog(st, "info", "backtest", `Backtest #${id} (${strategy}) started for ${start} → ${end}`);
    return ok({ id, status: "running" });
  }
  m = /^\/backtests\/([^/]+)$/.exec(path);
  if (method === "GET" && m) {
    const id = decodeURIComponent(m[1] ?? "");
    const x = st.backtests.find((y) => String(y.summary.id) === id);
    if (!x) return err(404, `Backtest ${id} not found`);
    if (x.detail) return ok(x.detail);
    return ok({ ...x.summary, error: null, equity_curve: [], trades: [], by_month: [] });
  }
  return err(404, `Not Found: ${method} /api${path}`);
}
