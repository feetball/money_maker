# Kalshi Paper Trader — Architecture Contract

This document is the **binding interface contract** for everyone building this repo.
If code and this doc disagree, fix one of them explicitly — do not silently diverge.

Hard rule: **PAPER TRADING ONLY.** No code path may place a real order or require
Kalshi API credentials. Market data comes from Kalshi's public, unauthenticated REST
API. (A future live mode is out of scope; do not scaffold it.)

Kalshi API facts (formats, fees, lifecycle, rate limits) live in
[`docs/kalshi_api_notes.md`](kalshi_api_notes.md). Empirical strategy research lives in
`research/`.

**Second venue (Coinbase spot crypto).** This document covers the Kalshi venue. A second,
fully separate paper venue for Coinbase spot lives in `kalshibot/coinbase/`. It has its own
account, SQLite file, engine, risk limits and kill switch, plus `/api/coinbase/*`,
`/api/coinbase/stream` and the cross-venue `GET /api/overview`. Its binding contract is
[`docs/COINBASE_CONTRACT.md`](COINBASE_CONTRACT.md). That contract lists the only
integration points in Kalshi files (`api/server.py`, `config.py`, `cli.py`,
`config.example.yaml`). Everything else in this document is unchanged by it.

---

## 1. Stack & layout

- Backend: Python 3.11, managed with **uv** (`pyproject.toml` at repo root).
  Deps: `fastapi`, `uvicorn[standard]`, `httpx`, `pydantic>=2`, `pyyaml`, `numpy`, `pyarrow`
  (backtests: the parquet research candles)
  (plus `pytest`, `pytest-asyncio`, `respx` for dev). Persistence: stdlib `sqlite3`
  (WAL mode). No ORM.
- Frontend: Vite + React 18 + TypeScript in `frontend/`, charts with `recharts`.
  Built output `frontend/dist/` is served by FastAPI at `/`, so the whole thing runs
  as **one process**: `uv run kalshibot serve` → http://localhost:8765.
- Tests: `uv run pytest` (backend), `npm run build` must type-check cleanly (frontend).

```
money_maker/
  pyproject.toml            # [project.scripts] kalshibot = "kalshibot.cli:main"
  config.example.yaml       # every tunable, documented; copied to config.yaml on first run
  kalshibot/
    __init__.py
    cli.py                  # `kalshibot serve|backtest|reset` (argparse)
    config.py               # pydantic Settings loaded from config.yaml (+ env KALSHIBOT_*)
    money.py                # Decimal helpers
    fees.py                 # Kalshi fee formulas
    kalshi/
      client.py             # async public REST client
      models.py             # Market, Event, Series, Orderbook, Trade, Candle
    marketdata.py           # MarketDataService: universe cache, orderbook cache, trade tape
    feeds/                  # external data (e.g. crypto spot) used by strategies
      __init__.py           # FeedRegistry
    paper/
      models.py             # Order, Fill, Position, Settlement, AccountState
      broker.py             # PaperBroker (fill simulation, positions, settlement)
    store.py                # SQLite persistence
    risk.py                 # RiskManager
    strategies/
      __init__.py           # REGISTRY: dict[str, type[Strategy]]
      base.py               # Strategy ABC, OrderIntent, StrategyContext
      <one module per strategy>.py
    engine.py               # Engine: the trading loop
    analytics.py            # performance stats (CIs, expected-vs-realized, calibration)
    backtest/
      __init__.py
      runner.py             # replays historical data through Strategy objects
    api/
      server.py             # FastAPI app (REST + SSE), serves frontend/dist
  frontend/
  tests/
  docs/  research/
  data/                     # runtime: kalshibot.sqlite3 (gitignored)
```

---

## 2. Numeric conventions

- **All prices are dollars in [0, 1]**, per-contract, for the side named. A NO price
  of 0.93 means paying $0.93 for a NO contract. Internally use `decimal.Decimal`
  for prices, cash, fees and P&L (`money.D("0.93")`). Never use float for ledger math.
- Strategy/model math (probabilities, vol) may use float; convert with
  `money.price(x)` which rounds to the market's tick (see §5) before creating intents.
- Contract counts: the bot trades **whole contracts** (`int`). Order-book and trade
  sizes from Kalshi may be fractional (`*_fp`) → parse as `Decimal`.
- Timestamps: timezone-aware UTC `datetime` internally; ISO-8601 strings with `Z` in JSON.
- JSON over the REST API: money/prices as JSON numbers (float), rounded to 4 dp.

`money.py` exports: `D(x) -> Decimal`, `ZERO`, `ONE`, `CENT = D("0.01")`,
`ceil_cent(x: Decimal) -> Decimal`, `q4(x) -> Decimal` (quantize 0.0001),
`price(x: float|Decimal, tick: Decimal = CENT, mode="nearest"|"down"|"up") -> Decimal`.

---

## 3. Kalshi client & models (`kalshibot/kalshi/`)

`KalshiClient(base_url, max_rps=3, timeout=15)` — async, `httpx.AsyncClient`, token-bucket
rate limiter, retry with exponential backoff + jitter on 429/5xx/timeouts (max 5 tries).
Methods (all `async`):

| method | endpoint |
|---|---|
| `get_markets(**filters) -> tuple[list[Market], cursor|None]` | `GET /markets` |
| `iter_markets(**filters) -> AsyncIterator[Market]` | paginates `/markets` (limit=1000) |
| `get_market(ticker) -> Market` | `GET /markets/{ticker}` |
| `get_events(**filters) / iter_events(**filters)` | `GET /events` (supports `with_nested_markets`) |
| `get_event(event_ticker) -> Event` | `GET /events/{event_ticker}` |
| `get_series(series_ticker) -> Series` | `GET /series/{series_ticker}` |
| `get_orderbook(ticker, depth=0) -> Orderbook` | `GET /markets/{ticker}/orderbook` |
| `get_trades(ticker, min_ts=None, limit=1000, cursor=None) -> tuple[list[Trade], cursor]` | `GET /markets/trades` |
| `get_candlesticks(series, ticker, start_ts, end_ts, period) -> list[Candle]` | per notes |
| `get_exchange_status() -> dict` | `GET /exchange/status` |

Default filters: `iter_markets` passes `mve_filter="exclude"` unless overridden.

Models are frozen pydantic v2 models (or dataclasses) parsed from raw API JSON by
`Model.from_api(d)`. They keep the raw dict as `.raw`. Required fields:

- `Market`: ticker, event_ticker, series_ticker (derive from event if absent), title,
  yes_sub_title, status, market_type, open_time, close_time, expected_expiration_time,
  can_close_early, yes_bid, yes_ask, no_bid, no_ask (Decimal|None — None when the
  side is empty; Kalshi reports 0/1 sentinels, normalize them), yes_bid_size,
  yes_ask_size, last_price, volume, volume_24h, open_interest, liquidity,
  result ("yes"|"no"|""|other), settlement_value (Decimal|None), price_ranges
  (list of (start,end,step) Decimals), rules_primary, rules_secondary, strike_type,
  floor_strike, cap_strike, custom_strike.
  Helpers: `tick_at(price) -> Decimal`, `mid -> Decimal|None`, `spread -> Decimal|None`,
  `is_open -> bool`.
- `Event`: event_ticker, series_ticker, title, sub_title, category, mutually_exclusive,
  collateral_return_type, markets: list[Market] (when nested).
- `Series`: ticker, title, category, frequency, fee_type, fee_multiplier (Decimal).
- `Orderbook`: ticker, ts, `yes_bids: list[Level]`, `no_bids: list[Level]` sorted
  **best-first** (descending price). `Level = (price: Decimal, size: Decimal)`.
  Derived: `yes_asks` = [(1 − p, s) for (p, s) in no_bids] (ascending price, best-first),
  `no_asks` likewise from yes_bids. `best_yes_bid`, `best_yes_ask`, `best_no_bid`,
  `best_no_ask`. Method `asks(side) / bids(side)`.
- `Trade`: trade_id, ticker, ts, yes_price, no_price, count (Decimal), taker_side.
- `Candle`: end_ts, yes_bid OHLC, yes_ask OHLC, price OHLC (nullable), volume,
  open_interest.

---

## 4. Market data (`kalshibot/marketdata.py`)

`MarketDataService(client, settings)`:
- **The full open universe is > 120,000 non-MVE markets (> 120 pages × ~2 MB) — never
  scan it all.** Each strategy declares a `UniverseSpec` (§7): `max_days_to_close`
  (markets closing within N days — query `/markets` with **no status** plus
  `min_close_ts=now`, `max_close_ts=now+N·86400`, `mve_filter=exclude`; the close-ts
  filters only combine with an empty status), and/or explicit `series_tickers`
  (`/markets?series_ticker=…&status=open`). The universe is the union of enabled
  strategies' specs, filtered to `status == "active"`.
- **Window scans read nearest close first** (integration, 2026-09-27). `/markets` lists a
  close-time window **latest close first**, so one query cut off at `engine.universe_max_pages`
  dropped the markets closing *soonest* (measured: a 3-day window was 116 pages; the cut at 100
  left out all 3,447 active markets closing within the next ~7 h). The window is now read as
  ascending chunks (`window_chunks`: [0, ½ d], [½, 1 d], then one per day) sharing one page
  budget, so a cap drops the far end; truncation is reported per chunk
  (`close<3d[1-2d]`) and for unread chunks (`close<3d[2-3d]`). Default cap 150 pages.
- `async refresh_series(series_tickers)` — series-scoped refresh (one
  `/markets?series_ticker=…&status=open` request per series, no minimum interval) merged into
  the universe: newly listed active markets are added, markets of those series that stopped
  being active are dropped. The engine runs it every `UniverseSpec.refresh_s` (§7) for specs
  that set it; a full refresh that started before it keeps the newer series data.
- `async refresh_universe()` — fetches the union above (≥ 60 s interval; `/markets`
  lists are CDN-cached 15 s anyway), keeps `markets: dict[ticker, Market]`, and
  `events: dict[event_ticker, Event]` populated **lazily** via `GET /events/{event_ticker}`
  (cached ~1 h; needed for `mutually_exclusive`, category), with `.markets` filled from
  the universe; `last_refresh`, `last_refresh_duration_s`, `universe_size`.
- Fee parameters resolve at **fill time** (event `fee_type_override`/`fee_multiplier_override`
  take precedence over the series; MLB flips from M=0.5 to 1.0 at first pitch).
  `async fee_params(market, at)` implements this: it applies the scheduled changes from
  `GET /events/fee_changes` and `GET /series/fee_changes` (re-read every 30 min) that took
  effect after the cached series/event was fetched, and re-reads an event used for fees at
  least every 5 min. `fee_params_cached(market, at)` is the request-free variant used by
  `StrategyContext.fee`.
- `async series(series_ticker) -> Series` — cached 24h (fees).
- `async orderbook(ticker, max_age_s=5) -> Orderbook` — TTL cache.
- `async trades_since(ticker, since: datetime) -> list[Trade]` — for maker-fill simulation.
- `async market(ticker, fresh=False) -> Market` — single-market refresh (used for
  settlement polling of held positions that may have left the open universe).

---

## 5. Fees (`kalshibot/fees.py`)

Implement exactly per `docs/kalshi_api_notes.md`. Signature:

```python
def trading_fee(price: Decimal, count: int|Decimal, *, is_taker: bool,
                fee_type: str = "quadratic", fee_multiplier: Decimal = D(1)) -> Decimal
```
Returns total fee in dollars for one order execution at one price. For multi-level
fills, the broker charges fees **cumulatively per order** (notes §1.3): after each fill,
total charged = ceil_to(precision, Σ raw fees so far). Default `precision = 0.01`
(conservative, matches the published tables); `paper.fee_precision: 0.0001` is a
sensitivity option. Maker fee is non-zero only for `quadratic_with_maker_fees` (0.25×)
and `quadratic_with_combo_maker_fees` (0.5×); `flat` uses 0.035. Unit tests must include
the worked examples from the notes.

---

## 6. Paper broker (`kalshibot/paper/`)

The broker is where paper bots lie to themselves. Rules — **all mandatory**:

1. **Fresh data at execution.** On every order the broker re-reads the market
   (`marketdata.market(ticker, fresh=True)`: status/close time ≤ 5 s old), the exchange status
   and the fee parameters, and fetches the order book itself **last**
   (`marketdata.orderbook(ticker, max_age_s=2)`), never trusting the strategy's snapshot. A
   basket fetches all its books together after every other lookup. A book that has aged past
   `2 s + 3 s` by the time the walk starts is re-fetched once, else the order is rejected.
   **Latency (engine orders):** the engine stamps `decided_at` when a strategy's `on_tick`
   returns and passes it to `place_order` / `place_basket`; the order reaches the paper exchange
   at `decided_at + paper.taker_latency_s` (default 0.25 s) and walks only a book **received at
   or after** that moment (a cached book from before it is refused and re-fetched with
   `max_age_s=0`) - never the book the decision was made on. That book is fetched concurrently
   with the market/fee lookups, so slow lookups (a busy rate limiter) do not stretch the
   simulated latency. Calls without `decided_at` (tests, the backtester) keep the plain fresh read.
2. **Taker orders (`tif="ioc"`)** walk the opposite side's levels best-first, only at
   prices ≤ limit (for buys), up to `count`. Unfilled remainder is cancelled. Fees per
   §5 with `is_taker=True`.
3. **Consumed liquidity.** Paper fills don't remove real liquidity, so the broker keeps
   a `consumed[(ticker, side, price)] -> qty` map that is subtracted from displayed depth
   on later orders (available = `max(0, displayed − qty)`). Others' trades and cancels at
   that level come out of the part we did not take, so when the displayed size drops below
   `qty`, `qty` shrinks to the displayed size (it is **not** forgotten); the entry goes away
   when the level disappears. Contracts joining the level are new liquidity. An entry is
   dropped wholesale only after `consumed_liquidity_ttl_s` (default 300s) since our last
   take **and** once the level has been seen larger than right after that take (re-quoted);
   an unchanged stale level is never re-harvested. Entries of closed/settled markets and
   entries not observed for a day are garbage-collected.
4. **Resting maker orders (`tif="gtc"`)** are recorded with `queue_ahead` = displayed
   size at our price on our side when placed (0 if we improve the best bid). They fill
   only from **real trades printed after placement** (polled via `trades_since`) whose
   taker **sold our side into the bids** (`taker_outcome_side` is the other side; for a YES
   bid, `taker_book_side == "ask"`); prints where the taker bought our side lifted offers
   (possibly ones we already took) and never fill a bid. One print is shared by our orders
   in price-time priority (best limit, then time): a trade strictly through an order's
   price fills it (up to what is left of the print); a trade *at* its price first burns
   `queue_ahead` (the real contracts ahead of it, shared by our orders at that price), and
   only the excess fills it. If the live book crosses our limit, we fill at our limit
   (price-time priority, after consumed liquidity, only while the market — re-read with
   `fresh=True` — is tradable). Fees with `is_taker=False`. Orders expire after
   `expires_at` (strategy-provided, default 1h) or when the market closes or stops being
   active. **Request budget:** a trade-tape read costs one request per market, so one
   resting-order pass reads at most `paper.max_trade_polls_per_pass` (default 8; 0 = no cap)
   tapes — markets with an order at/after its expiry or close first, then the least recently
   read. Markets and books are still read for all (batched). A skipped market loses no prints
   (per-ticker cursor; fills keep the print's timestamp) and its expiries, its book-crossing
   fills and its `queue_ahead` bound from the book all wait for the pass that reads its prints
   (a print before the expiry can still fill; the book already reflects prints not yet
   processed, so applying it first would let those prints fill the order again after
   `queue_ahead` was zeroed). **Strategy cancels**
   (`cancel_orders(ids, strategy=…)`, §7 `CancelIntent`/`replaces`) first run a pass limited to
   the orders' markets (all their tapes), so prints that would have reached the exchange before
   the cancel still fill; only what is still open is then cancelled.
5. **Positions** are per market and per side, Kalshi-style netting: buying NO while
   holding YES first closes YES contracts (a YES+NO pair is worth exactly $1): realized
   P&L = count × (1 − avg_yes_cost − no_price) − fees. Symmetric for YES vs NO.
   `sell` actions are supported and equivalent to buying the opposite side at (1 − price).
6. **Cash**: buying reserves `count × price + fee` immediately (rejected if insufficient
   cash). Resting orders reserve cash at placement; released on cancel/expiry.
7. **Settlement**: poll `GET /markets/{ticker}` for held markets; settle when
   `status == "finalized"` (optionally `determined`). `result` ∈ yes/no/scalar — there is
   no "void": cancelled events settle as `scalar` at `settlement_value_dollars`
   (YES pays that value per contract, NO pays 1 − value; round total payout **down** to
   the cent). Record a `Settlement`, realize P&L. Track `expected_edge` from the opening
   intents so analytics can compare expected vs realized.
10. **Reject** orders when the market isn't `active`, `now ≥ close_time`, the exchange /
   the market's `exchange_index` shard has `trading_active == false` (e.g. Thursday
   03:00–05:00 ET maintenance), the price is off the market's tick grid, or it isn't
   strictly inside (0, 1). Exclude `is_block_trade` trades from maker-fill simulation;
   use `taker_outcome_side` (not the deprecated `taker_side`).
8. **Mark-to-market**: `liquidation_value` walks the side's bid ladder for the contracts
   held, net of bids we consumed ourselves (all strategies' positions in the same market and
   side walk it together and share the proceeds pro rata; contracts beyond the displayed
   depth are worth 0) — what selling now would bring, before exit fees. Also expose
   `mid_value`. Equity = cash + reserved cash for open orders + liquidation value of
   positions (report the mid version separately). **Closed markets:** once a market is
   determined/finalized with a result it is marked at its payout. While it is closed without a
   result (its book is empty) it keeps the ladder last observed **before** `close_time`, frozen
   net of the bids we had consumed ourselves (persisted), so it is never marked above what
   selling into that pre-close book would have paid — and never at $0 just because the book
   emptied. Books seen at/after `close_time` never update a mark (the engine takes one extra
   mark of each held market in its last 5 s before close). Such marks are flagged
   `mark_stale` in `GET /api/positions` (§12). A market with no pre-close observation keeps its
   cost basis.
9. **Multi-leg baskets**: `place_basket(intents, all_or_none=True)` pre-checks every
   leg against fresh books (after consumed liquidity) and executes all legs or none. This is
   **optimistic**: Kalshi has no atomic multi-market order (real legs are independent IOCs), so a
   basket some of whose legs are gone would leave a partial, unhedged position live. Such
   "would have legged" baskets (a fully fillable leg next to a short one) are logged
   (`kind="basket_legged"`) and counted per strategy (`legged_baskets` in the strategy stats), so
   the arb's paper P&L reads as the best case it is.

Models (`paper/models.py`): `Order(id, ticker, side, action, count, filled_count,
limit_price, avg_fill_price, tif, status: "open"|"filled"|"partially_filled"|
"cancelled"|"expired"|"rejected", strategy, reason, expected_edge, fair_value,
group_id, queue_ahead, created_at, updated_at, expires_at, fees)`,
`Fill(id, order_id, ticker, side, action, count, price, fee, is_taker, ts, strategy)`,
`Position(ticker, event_ticker, side, count, avg_price, cost_basis, realized_pnl,
fees_paid, strategy, opened_at, expected_edge_total)`,
`Settlement(id, ticker, result, side, count, payout, cost_basis, pnl, ts, strategy)`.

All broker state persists via `store.py` so a restart resumes the paper account. Every
mutation is atomic with the store: if the SQLite transaction fails (disk full, database
locked), the in-memory ledger is restored (orders come back `rejected`, "not recorded").
One process owns a database: `Store(path, exclusive=True)` holds `<path>.lock`
(`serve` and `reset`; `reset` refuses while a server runs — use `POST /api/account/reset`).

---

## 7. Strategies (`kalshibot/strategies/`)

```python
@dataclass
class OrderIntent:
    ticker: str
    side: Literal["yes", "no"]
    action: Literal["buy", "sell"] = "buy"
    count: int = 1                   # desired; risk manager may reduce
    limit_price: Decimal             # price for `side`
    tif: Literal["ioc", "gtc"] = "ioc"
    expires_in_s: int | None = None  # gtc only
    strategy: str = ""
    reason: str = ""                 # human-readable, shown in UI
    fair_value: float | None = None  # model P(side wins), if any
    expected_edge: Decimal | None = None  # $/contract after fees at limit_price
    group_id: str | None = None      # all-or-none basket id

class StrategyContext(Protocol):
    now: datetime
    markets: Mapping[str, Market]          # open universe
    events: Mapping[str, Event]
    async def series(self, series_ticker) -> Series: ...
    async def orderbook(self, ticker) -> Orderbook: ...
    portfolio: PortfolioView               # positions, open orders, cash, equity
    feeds: FeedRegistry                    # external data (spot prices, etc.)
    def fee(self, market: Market, price, count, is_taker=True) -> Decimal: ...
    def log(self, msg: str, **data) -> None: ...

@dataclass
class UniverseSpec:
    max_days_to_close: float | None = None   # markets closing within N days
    series_tickers: list[str] = field(default_factory=list)

class Strategy(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    default_params: ClassVar[dict]
    param_schema: ClassVar[dict]           # JSON-schema-ish {name: {type, min, max, help}}
    def __init__(self, params: dict | None = None): ...
    def universe(self) -> UniverseSpec: ...  # what market data this strategy needs
    async def on_tick(self, ctx: StrategyContext) -> list[OrderIntent]: ...
    def on_fill(self, fill: Fill) -> None: ...          # optional hook
    def on_settlement(self, s: Settlement) -> None: ... # optional hook
    # for backtests (§10): strategies declare whether they can run on candle data
    backtestable: ClassVar[bool] = False
```

A strategy must be **pure decision logic**: no direct HTTP (use ctx), no broker calls.
It must not re-enter a market it already holds unless its rules say so (check
`ctx.portfolio`). It must attach `reason`, `expected_edge` and (if it has a model)
`fair_value` to every intent. Registered in `strategies/__init__.py::REGISTRY`.

**Additive extensions (2026-09-27, engine/broker for the research strategies)** — all
optional, defaults keep the behaviour above:

```python
class Strategy(ABC):
    tick_interval_s: ClassVar[float | None] = None   # seconds between ticks; None = engine.tick_s

@dataclass
class UniverseSpec:
    ...
    refresh_s: float | None = None    # also re-read series_tickers every refresh_s (>= 15 s)

@dataclass
class OrderIntent:
    ...
    replaces: int | None = None       # cancel/replace: this strategy's resting order to cancel first

@dataclass
class CancelIntent:                   # returned from on_tick next to OrderIntents
    order_id: int | None = None       # one resting order ...
    ticker: str | None = None         # ... or all of this strategy's resting orders in a market
    reason: str = ""                  # becomes the cancelled order's status_reason
    strategy: str = ""
```

- **Timing.** Each strategy ticks on its own fixed grid of `tick_interval_s` (class or
  instance attribute, ≥ 1 s; `None` → `engine.tick_s`, default 30 s — which already gives 2
  evaluations in any 60 s window). No drift: a late start runs the latest slot once; a slot
  that comes while the previous tick is still running is skipped (`skipped_ticks` in
  `/api/strategies`), never queued. Strategies tick concurrently, so a slow strategy cannot
  delay another; risk check + placement of each order hold one engine lock. A strategy with a
  short decision window should use `tick_interval_s` ≤ a third of it (e.g. 10–15 s for a 60 s
  window).
- **New markets of short-lived series.** `UniverseSpec(series_tickers=[...], refresh_s=20)`
  makes the engine re-read those series every `refresh_s` (one request per series; §4
  `refresh_series`), so e.g. a newly opened 15-minute window is in `ctx.markets` within
  ~`refresh_s` + 15 s (CDN) instead of up to `engine.universe_refresh_s`.
- **Cancels.** Return `CancelIntent(order_id=…)` / `CancelIntent(ticker=…)` (or call
  `ctx.cancel(order_id=None, *, ticker=None, reason="")` on the engine context — same thing).
  The engine applies a tick's cancels before its new orders, only to the strategy's own open
  orders (others are ignored and logged), after syncing the orders' markets with the trade
  tape (§6 rule 4), so a cancel can come back `filled`; `on_fill` runs for such fills.
- **Cancel/replace.** `OrderIntent(..., tif="gtc", replaces=old_id)`: `old_id` is cancelled
  first; the new order is placed with `count` reduced by what `old_id` filled since the
  tick's `ctx.portfolio` snapshot, and **not** placed (signal `rejected`) if `old_id` was no
  longer open (filled/expired/cancelled) or is not the strategy's. Not allowed on basket legs.
- **Baskets.** Intents sharing a `group_id` (even a single one) are all-or-none:
  `RiskManager.check_basket` (any trimmed leg rejects the basket) then
  `PaperBroker.place_basket(all_or_none=True)` (IOC legs only). The per-tick cap
  (`max_intents_per_tick`, 50) keeps or drops a basket whole, never splits it.
- Backtests (§10) only need `OrderIntent`s; a backtestable strategy should not rely on
  cancels.

**Additive extensions (2026-09-27, review fixes)** - all optional:

```python
class Strategy(ABC):
    experimental: ClassVar[bool] = False     # not validated out of sample: forward paper-test only
    risk_defaults: ClassVar[dict] = {}       # {"max_allocation_pct": %, "daily_loss_limit": $} (§8)
```

- `ctx.orderbook(ticker, max_age_s=...)` on the engine context (`max_age_s=0`: fetched now, never
  a cached book); other contexts may ignore it.
- **Default on/off (2026-09-27, integration).** `Strategy.enabled_by_default: ClassVar[bool] = False`.
  Whether a strategy runs is decided by the first of: the dashboard toggle stored in the database
  (`PATCH /api/strategies/{name}` `{enabled}`), `strategies.<name>.enabled` in the config / env
  (`StrategySettings.enabled` is `None` when unset), then `enabled_by_default`. The four research
  strategies set it to True, so a `config.yaml` written before they existed (`strategies: {}`)
  runs them after a restart; `enabled: false` or the switch turns one off. `GET /api/strategies`
  rows carry `enabled_source: "dashboard" | "config" | "default"`, and the engine's start log
  lists every strategy's state and source. `no_basket_arb`'s `max_days_to_close` default is 3
  (was 14): the engine scans one close-time window, the widest of the enabled specs.
- Research strategies: `btc15m_favorite` (primary) ticks every 5 s (`tick_interval_s`), re-reads
  KXBTC15M every 20 s (`refresh_s`), decides within 15 s after the 10:00 mark (entry window
  (9.75, 10] min; later decisions are skipped), reads the book after the spot and reports the
  decision lag; a minute before the window it fetches the window's event so the order's fee
  lookup is a cache hit. `ladder_favorite` (experimental) evaluates its trigger only on the first tick at
  or after each UTC hour (`decision_grid_s` 3600, the research's hourly candle close;
  `decision_window_s` 120) and caps its cost per position ($10), event ($20), underlying group
  ($40: all AAA gas series, WTI+Brent, gold, US equity indexes, ...) and in total ($150).
  `maker_favorite` (experimental) caps positions + resting bids (`max_open_cost` $100), rests at
  most 8 orders, excludes Sports/Crypto/Mentions and carries a zero prior edge.

---

## 8. Risk manager (`kalshibot/risk.py`)

`RiskManager(settings.risk).check(intent, market, portfolio) -> RiskDecision(approved_count, reason)`.
Limits (all configurable, all enforced): `max_position_cost_per_market`,
`max_exposure_per_event`, `max_total_exposure_pct` (of equity; default 60),
`max_strategy_allocation_pct` (per strategy; the fallback for strategies without their own
`max_allocation_pct`), `min_cash_reserve`, `max_orders_per_minute` (**per strategy**: one
strategy's burst never uses up another's budget), `daily_loss_limit` (default 150) → **kill
switch** that blocks new entries (existing positions still settle), `min_seconds_to_close` (no
entries within N s of close), `max_spread` guard.

Per-strategy limits (`StrategyLimits`; `kalshibot.risk.strategy_limits(settings, classes)`: the
class's `risk_defaults` overridden by `strategies.<name>.max_allocation_pct` /
`daily_loss_limit` in the config), installed by the engine and the backtester with
`RiskManager.set_strategy_limits`: `max_allocation_pct` caps the strategy's exposure (defaults:
btc15m_favorite 10, ladder_favorite 15, maker_favorite 10, no_basket_arb 10 - 45% in all, so
experimental strategies can never crowd out the primary); `daily_loss_limit` pauses **only that
strategy's** entries until the next UTC day once its P&L today (`PortfolioView.strategy_daily_pnl`:
realized + change in unrealized since the day's first observation) reaches -limit (defaults 100 /
45 / 30 / 0 = off; pauses persist in `kv['risk.strategy_paused']`). A kill switch tripped by the
account `daily_loss_limit` is released at the next UTC day when `risk.kill_switch_auto_release`
(default true; the backtester assumes the same); a manual trip stays on. Sizing helper: `kelly_count(p, price, equity, kelly_fraction,
cap)` for binary contracts, f* = (p − price) / (1 − price).
Rejections are logged with reason and shown in the UI signals feed.
Engaging the kill switch (manually via the API/dashboard, or tripped by `daily_loss_limit`)
also **cancels every resting paper order** (`Engine.set_kill_switch`), releasing their
reserved cash; releasing it cancels nothing.
Exits (contracts closing the strategy's opposite position) are **net of the strategy's
resting orders that already close it**; anything beyond is an entry. Basket legs are
checked with `check_basket(...)`: each leg against the portfolio plus the legs approved
before it, so every dollar limit applies to the basket as a whole. Dollar limits are
validated `>= 0` (`max_spread` in [0, 1]).

---

## 9. Engine (`kalshibot/engine.py`)

`Engine(settings, client, marketdata, broker, risk, store, strategies)` with
`start()`, `stop()`, `status()`. Single asyncio task; loops (all intervals configurable):

- universe refresh (default 120s)
- series refresh (every `UniverseSpec.refresh_s`, ≥ 15 s, only for specs that set it; §4
  `refresh_series`)
- strategy tick (default 30s; per strategy `tick_interval_s`, §7): for each enabled strategy
  → `on_tick(ctx)` → its cancels (§7) → risk check → broker (orders carry `decided_at`, §6 rule
  1; the tick's books are batch-fetched once the orders "arrive"). Each strategy has its own fixed grid (no
  drift; busy/late slots are skipped and counted, never queued) and its own task, so
  strategies tick concurrently; each risk check + placement holds one execution lock (limits
  always see earlier orders). The scheduler only *starts* ticks, so maintenance jobs never
  delay them. Exceptions (and `on_tick` timeouts) in one strategy are caught, logged, recorded
  on that strategy, and affect neither the others nor the loop. `tick_count` / the `tick`
  event advance once per round of strategies started together (every `tick_s` when none is
  enabled).
- resting-order maintenance (default 15s, own task): maker fill simulation, expiries, at most
  `paper.max_trade_polls_per_pass` trade-tape reads per pass (§6 rule 4).
- settlement poll (default 60s) for every market with a position; plus every 10 s while a held
  market is in its first 10 min after close (KXBTC15M is finalized ~6 s after close, so the
  payout and the freed cash land within seconds; no request otherwise).
- equity snapshot (default 60s) → store.
- pre-close mark: one extra mark of each held market in its last 5 s before close (no request
  otherwise), so the mark that stays frozen until the result (§6 rule 8) is recent.
- publishes events (`tick`, `signal`, `order`, `fill`, `settlement`, `log`, `account`)
  to an in-process pub/sub the API streams over SSE.

The engine starts automatically with `kalshibot serve` if `engine.autostart: true`.

---

## 10. Backtester (`kalshibot/backtest/`)

Replays the historical dataset in `research/data/` (see its README/loader; the hourly adapter
also reads the archived-era holdout candles `research/calibration/verify_leakage/candles_hist_hourly.parquet`,
May 20 - Jul 27, so ladder backtests are not confined to the window the rule was selected on)
through the **same `Strategy` classes** that declare `backtestable = True`, using a
`BacktestContext` that implements `StrategyContext` from candle data at walk-forward
snapshot times (no look-ahead: only candles whose period ended ≤ snapshot), a synthetic
order book from candle bid/ask with a configurable per-level size cap, the same
`fees.py`, and settlement from the recorded result. With a fill latency the risk check runs at
the decision time (as live) and the approved orders execute as timed events in clock order with
the settlements, so the clock never runs ahead of the tick being evaluated. The strategy's own
risk limits (§8) apply. Output: metrics (P&L, per-contract
EV with bootstrap CI clustered by event, hit rate, max drawdown, Sharpe-like ratio,
per-month breakdown), equity curve, trade list. Results are stored and exposed via the API.

---

## 11. Analytics (`kalshibot/analytics.py`)

Per strategy and overall, over **settled** trades: count, total/mean P&L per contract,
95% CI (bootstrap), expected edge vs realized P&L, Brier score & calibration buckets of
`fair_value`, win rate, max drawdown, and a **go-live readiness** verdict:
`{"ready": bool, "reasons": [...]}` — ready only if ≥ `min_settled_trades` (default 200;
`analytics.min_settled_trades_by_strategy`: btc15m_favorite 300, ladder_favorite 1500,
maker_favorite 1000, no_basket_arb 200), the CI lower bound of mean P&L per trade is > 0, the
**tail check** passes (events = clusters; the one-sided 95% Clopper-Pearson upper bound on the
loss-event rate must be below break-even `W / (W + L)`, with `L` = the mean event cost while no
loss has been seen - a bootstrap cannot represent a loss it has not seen), and max drawdown is
within limits; per strategy too, the drawdown is measured against the account's starting balance
(how far the strategy alone drew the account down). The drawdown against the strategy's
**allocation** (`max_allocation_pct` of the starting balance) is reported as `allocation` /
`max_drawdown_pct_of_allocation` but does not gate (integration change, 2026-09-27): the allocation
caps concurrent exposure, not cumulative loss, and btc15m_favorite stakes up to $50 of its $100 per
trade, so its in-sample replay (+3.5c/contract, CI above 0) drew 54% of the allocation but 14% of
the account and could never have passed. The headline `readiness` is **per strategy**:
ready only when at least one strategy is individually ready (and the account drawdown is within
the limit), with every strategy's verdict in `reasons` and `ready_strategies`; the pooled
`overall` stats are informational.
The overall max drawdown is the worst of the realized-P&L curve and the account equity curve
over **every** equity snapshot (never a thinned series; the broker's persisted peak/drawdown
covers snapshots that housekeeping has downsampled — full resolution is kept for 7 days,
older snapshots keep the first/lowest/highest/last row per hour).

---

## 12. REST API (`kalshibot/api/server.py`) — contract for the frontend

Base `/api`. All responses JSON. Errors: `{"detail": str}` with 4xx/5xx.

> **Contract change (backend review fixes, 2026-09-26):** in `GET /api/positions`,
> `mark_price` is now the depth-weighted average exit price (`liquidation_value / count`)
> instead of always the best bid, and `liquidation_value` walks the bid ladder instead of
> `count × best bid` (the account's `positions_liquidation_value`, `equity`,
> `unrealized_pnl`, `todays_pnl` and drawdown follow). A new `best_bid` field carries the old
> top-of-book value. Field names and types are unchanged; the UI's "mark = best bid"
> tooltips should say "average exit price when selling into the bids".

> **Contract change (integration, 2026-09-26)** — found by driving the built dashboard
> against a live `kalshibot serve`:
> - `GET /api/stream` events carry an `id:` line (strictly increasing, also across server
>   restarts: the sequence starts at the process start time in epoch ms). `?replay=N`
>   (N ≤ 500) re-sends the last N buffered events and a `Last-Event-ID` header re-sends the
>   buffered events after that id; the order on the wire is: replayed events (with ids),
>   then the `account` greeting, then live events (with ids). After a replay the greeting has
>   no id (it ends the backlog); on a plain connection it carries the latest event id, so a
>   client that saw no event yet still has a resume point. The dashboard asks for
>   `?replay=500` on every reconnect and drops ids it has already seen (gap fill).
> - `POST /api/orders/{id}/cancel`: 404 for an unknown id, **409** when the order is no
>   longer open (the UI shows that as information, not an error).
> - `POST /api/engine/kill-switch {on: true}` (and `PATCH /api/risk {kill_switch: true}`)
>   cancels every resting order (§8).
> - `title` fields are plain text (Kalshi's Markdown `**bold**` is stripped) and the
>   Markets search matches the plain text.
> - Any other `GET` outside `/api` returns a real file from `frontend/dist` or
>   `dist/index.html` (client-side routes); `dist` is looked up per request, so a build made
>   while the server runs is served without a restart. A missing `/assets/*` file is a 404.

> **Contract change (engine/broker for the research strategies, 2026-09-27)** — additive
> fields only; nothing renamed or removed:
> - `GET /api/positions`: `mark_stale: bool` — true while the market has closed without a
>   result: `mark_price`/`liquidation_value` are then the last **pre-close** ladder (net of our
>   own consumption), not a live book; `mark_ts` (ISO | null) — when the marked book was
>   observed. Once the market is determined the mark is the payout and `mark_stale` is false
>   (§6 rule 8). The UI can show "last pre-close value" for stale rows.
> - `GET /api/strategies` rows also carry `tick_interval_s`, `skipped_ticks`,
>   `last_tick_lag_ms`, `last_duration_ms` and `cancels` (resting orders cancelled on the
>   strategy's request) next to the existing extras (`last_tick_at`, `ticks`, `errors`, …).
> - `GET /api/status` `engine.jobs` also lists `series`, `preclose` and `postclose`.

> **Contract change (review fixes, 2026-09-27)** — additive fields only; nothing renamed or removed:
> - `GET /api/strategies` rows: `enabled_source: "dashboard" | "config" | "default"` (integration,
>   §7 "Default on/off"), `experimental: bool` (badge these as forward-test only),
>   `risk_limits: {max_allocation_pct, daily_loss_limit, paused}`, and `stats.legged_baskets`.
> - `GET /api/analytics`: the headline `readiness` is per strategy (see §11) and also carries
>   `ready_strategies: [name]` and `basis: "per_strategy"`; `overall` and each `by_strategy` entry
>   carry `tail: {events, loss_events, loss_event_rate, loss_rate_upper, break_even_loss_rate,
>   avg_win_event, avg_loss_event, loss_basis}`, `starting_capital` and `min_settled_trades`.
>   The pooled `overall.readiness` is informational only — the dashboard's go-live card should
>   show the headline `readiness`.
> - `GET /api/risk`: `utilization.by_strategy` rows also carry `allocation_pct`,
>   `orders_last_minute`, `daily_pnl`, `daily_loss_limit` and `paused` (reason or null); `limit`
>   is the strategy's own allocation. `limits` includes `kill_switch_auto_release`.

| Method & path | Response |
|---|---|
| `GET /api/status` | `{mode:"paper", engine:{running, started_at, last_tick_at, tick_count, universe_size, last_error, kill_switch}, exchange:{trading_active}, server_time}` |
| `POST /api/engine/start` / `POST /api/engine/stop` | `status` payload |
| `POST /api/engine/kill-switch` `{on: bool}` | `status` payload (engaging it cancels every resting order) |
| `GET /api/account` | `{starting_balance, cash, reserved_cash, positions_liquidation_value, positions_mid_value, equity, equity_mid, realized_pnl, unrealized_pnl, fees_paid, total_pnl, total_return_pct, todays_pnl, max_drawdown_pct, open_positions, open_orders, settled_trades, win_rate}` |
| `POST /api/account/reset` `{starting_balance?: number}` | `account` payload (stops engine, wipes paper state) |
| `GET /api/equity?range=1d\|7d\|30d\|all` | `[{ts, equity, equity_mid, cash, realized_pnl, unrealized_pnl}]` |
| `GET /api/positions` | `[{ticker, title, event_ticker, side, count, avg_price, cost_basis, mark_price, liquidation_value, unrealized_pnl, fair_value, expected_edge_total, strategy, opened_at, close_time, yes_bid, yes_ask, url}]` — **`mark_price` = `liquidation_value / count`**, the average exit price when selling the whole position into the bid ladder (equals the best bid whenever the top level covers the position); extra field `best_bid` = top of book for the position's side (§6 rule 8); extra fields `mark_stale`, `mark_ts` (closed, not yet determined: frozen pre-close mark — see the 2026-09-27 note) |
| `GET /api/orders?status=open\|all&limit=200` | `[Order JSON]` (fields of §6 + `title`) |
| `POST /api/orders/{id}/cancel` | `Order JSON` (404 unknown id, 409 when the order is no longer open) |
| `GET /api/fills?limit=200` | `[Fill JSON + title]` |
| `GET /api/settlements?limit=200` | `[Settlement JSON + title]` |
| `GET /api/strategies` | `[{name, description, enabled, params, param_schema, backtestable, stats:{orders, fills, open_positions, settled, realized_pnl, unrealized_pnl, fees, win_rate, exposure}}]` |
| `PATCH /api/strategies/{name}` `{enabled?, params?}` | strategy JSON |
| `GET /api/risk` | `{limits:{...}, utilization:{total_exposure, total_exposure_pct, by_event:[...], by_strategy:[...], orders_last_minute, daily_pnl}, kill_switch}` |
| `PATCH /api/risk` `{...limits}` | risk JSON (422 for non-numeric or negative dollar limits) |
| `GET /api/signals?limit=200` | `[{ts, strategy, ticker, title, side, count, limit_price, fair_value, expected_edge, reason, decision:"executed"\|"partial"\|"rejected"\|"unfilled", decision_reason}]` |
| `GET /api/logs?limit=200` | `[{ts, level, kind, message, data}]` |
| `GET /api/markets?search=&category=&sort=volume_24h\|close_time\|spread&limit=100` | `[{ticker, event_ticker, title, category, yes_bid, yes_ask, spread, last_price, volume_24h, open_interest, close_time, url}]` |
| `GET /api/analytics` | `{overall:{...§11}, by_strategy:{name:{...}}, calibration:[{bucket, n, mean_fair_value, realized_rate}], readiness:{ready, reasons}}` |
| `GET /api/backtests` | `[{id, strategy, params, start, end, status, created_at, metrics}]` |
| `POST /api/backtests` `{strategy, params?, start?, end?, starting_balance?}` | `{id, status:"running"}` (runs in a background task) |
| `GET /api/backtests/{id}` | `{id, strategy, params, status, error, metrics, equity_curve:[{ts, equity}], trades:[...], by_month:[...]}` |
| `GET /api/stream?replay=N` | SSE: `event: <type>\ndata: <json>\nid: <n>` for types `tick, signal, order, fill, settlement, log, account`; `?replay=N` / `Last-Event-ID` re-send buffered events first, then an `account` greeting (see the note above); `: keepalive` every 15 s |

`url` fields link to `https://kalshi.com/markets/{series_ticker_lower}` (best effort).

---

## 13. Frontend (`frontend/`)

Dark-first, dense trading-dashboard UI; persistent **"PAPER TRADING"** badge. Pages
(react-router): **Dashboard** (KPI tiles, equity curve, P&L by strategy, open
positions, live activity feed via SSE, engine start/stop/kill switch), **Positions &
Orders**, **History** (fills, settlements), **Strategies** (enable toggles, param
editors generated from `param_schema`, per-strategy stats), **Signals** (all intents
incl. rejections + reasons), **Markets** (scanner), **Analytics** (CIs, calibration
chart, expected vs realized, go-live readiness), **Backtests** (launch + results),
**Settings** (risk limits, account reset). Polls REST every 5–10 s and applies SSE
events live. API base is same-origin `/api`; Vite dev server proxies `/api` to :8765
(`KALSHIBOT_API_URL=http://127.0.0.1:<port>` points it elsewhere).

---

## 14. Config (`config.example.yaml`)

```yaml
kalshi:  {base_url: https://api.elections.kalshi.com/trade-api/v2, max_rps: 3, timeout: 15}  # keep <= 3 req/s
account: {starting_balance: 1000}
engine:  {autostart: true, universe_refresh_s: 120, tick_s: 30, order_poll_s: 15,
          settlement_poll_s: 60, snapshot_s: 60,
          scanner_days_to_close: 0.5,       # Markets-page baseline window (0 = strategies only)
          universe_max_pages: 150,          # for the whole window scan, read nearest close first (§4)
          universe_window_rescan_s: 900}
paper:   {consumed_liquidity_ttl_s: 300, default_gtc_expiry_s: 3600, fee_precision: 0.01,
          max_trade_polls_per_pass: 8,   # trade-tape reads per resting-order pass (0 = no cap)
          taker_latency_s: 0.25}         # engine orders walk only books received this long after the decision
risk:    {max_position_cost_per_market: 50, max_exposure_per_event: 100,
          max_total_exposure_pct: 60, max_strategy_allocation_pct: 50,
          min_cash_reserve: 50, max_orders_per_minute: 30,   # per strategy
          daily_loss_limit: 150, kill_switch_auto_release: true,
          min_seconds_to_close: 300, max_spread: 0.10, kelly_fraction: 0.25}
strategies: {<name>: {enabled: <bool | unset = the class's enabled_by_default; the dashboard toggle wins>,
                      params: {...},
                      max_allocation_pct: <% | unset = class risk_defaults>,
                      daily_loss_limit: <$ | unset = class risk_defaults; 0 = off>}}
            # the example lists btc15m_favorite, ladder_favorite, maker_favorite, no_basket_arb (all enabled)
analytics: {min_settled_trades: 200, max_drawdown_pct: 20,   # go-live readiness thresholds
            min_settled_trades_by_strategy: {btc15m_favorite: 300, ladder_favorite: 1500,
                                             maker_favorite: 1000, no_basket_arb: 200}}
feeds:   {crypto: {symbols: [BTC, ETH], ttl_s: 5, candle_ttl_s: 30, sources: [coinbase, kraken]},
          kalshi_settled: {ttl_s: 60}}   # shares the engine's Kalshi client (counts toward kalshi.max_rps)
# backtest: {fill, latency_s, book_size, risk, ...}   # optional runner defaults (§10); CLI / API win
server:  {host: 127.0.0.1, port: 8765}
storage: {path: data/kalshibot.sqlite3}   # relative paths are relative to the config file
```

Empty sections (every key commented out) are treated as absent. Invalid values make the
CLI exit with a one-line `error: invalid config ...` (exit code 2).
