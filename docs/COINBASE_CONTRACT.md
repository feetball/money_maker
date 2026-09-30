# Coinbase Venue — Binding Build Contract

Coinbase spot crypto is added as a **second, fully separate PAPER venue** next to Kalshi.
This document is the binding contract for everyone building it. If code and this doc
disagree, fix one of them explicitly. (`docs/COINBASE_DESIGN.md`, if present, is an
advisory design review — this contract wins where they differ.)

**Hard rules**
- **PAPER ONLY.** Public market data only; never real orders, never API keys or auth.
- **Isolation.** Separate account (own USD cash), own SQLite file, own engine/kill switch/
  risk limits, own API prefix, own SSE stream. A Coinbase failure (import error, outage,
  bad config) must never stop or slow the Kalshi venue, and vice versa.
- **Kalshi stays intact.** Existing `/api/*` endpoints, fields, and the 716 passing tests
  keep working. Touch Kalshi files only at the integration points named in §13.
- **Evident venues.** Every venue-scoped screen, number, row and event is labeled with
  its venue (§14). A user must never have to guess which account a figure belongs to.

API facts: `docs/coinbase_api_notes.md` (being written; verified shapes are also in
`kalshibot/coinbase/models.py`). Strategy evidence: `research/coinbase/` (in progress).

---

## 1. Layout & ownership

```
kalshibot/coinbase/
  __init__.py        (exists)
  models.py          (exists — Product, OrderBook, BookLevel, Trade, Candle, Ticker, Stats)
  config.py          CoinbaseSettings (pydantic)                              [B1]
  client.py          CoinbaseClient — public REST, rate limited               [B1]
  fees.py            FeeTier table + fee math                                 [B1]
  paper.py           SpotOrderIntent, SpotOrder, SpotFill, SpotPosition,
                     SpotAccountState, SpotPortfolioView                      [B2]
  broker.py          SpotPaperBroker                                          [B2]
  store.py           SpotStore (separate SQLite file + ProcessLock)           [B2]
  risk.py            SpotRiskManager                                          [B2]
  strategies/
    __init__.py      REGISTRY + auto-discovery (like kalshibot/strategies)    [B3a]
    base.py          SpotStrategy, TargetWeight, SpotContext                  [B3a]
    <name>.py        one module per strategy                                  [S*]
  rebalance.py       plan_rebalance() — targets -> order intents              [B3a]
  backtest.py        run_spot_backtest() over research/coinbase/data          [B3a]
  marketdata.py      SpotMarketData (products, books, trades, candles cache)  [B3b]
  engine.py          CoinbaseEngine                                           [B3b]
  services.py        build_coinbase_services(), CoinbaseServices             [B3b]
  api.py             FastAPI APIRouter for /api/coinbase/*                    [B3b]
kalshibot/api/overview.py   GET /api/overview                                 [B3b]
kalshibot/api/server.py     additive integration only (§13)                   [B3b]
kalshibot/config.py         additive: `coinbase: CoinbaseSettings` field      [B1]
config.example.yaml         additive `coinbase:` section                      [B1]
kalshibot/cli.py            additive: `kalshibot coinbase-reset`, `coinbase-backtest` [B3b]
tests/test_cb_*.py          prefix every Coinbase test module with test_cb_   [owner of the code]
tests/fixtures/coinbase/    trimmed live samples (< 200 KB total)             [B1]

frontend/src/components/Venue.tsx   (exists — VenueBadge, VenueBanner, VENUES)
frontend/src/{App.tsx, components/Layout.tsx, styles/*, pages/Overview.tsx,
             api/{client,types,normalize,mock}.ts, existing Kalshi pages}    [F1]
frontend/src/api/coinbase/*   types.ts, client.ts, mock.ts, stream.ts        [F2]
frontend/src/pages/coinbase/* all Coinbase pages + index.ts                  [F2]
```

## 2. Numeric conventions
Decimal for all money/price/quantity in broker, store, fees, risk and planner. Prices in
USD per 1 unit of base; quantities in base units (e.g. `0.01234567` BTC). Round **order
sizes down** to `product.base_increment`, limit prices to `quote_increment` (buys down,
sells up — never more aggressive than intended). JSON: numbers (float), prices/quantities
with up to 8 dp; timestamps ISO-8601 UTC with `Z`. Strategy math may use float.

## 3. Models
`kalshibot/coinbase/models.py` (done). Facts to rely on: book levels best-first; trade
`maker_side` ("buy" = a resting bid was hit, prints at the bid) and `taker_side`;
candles `start` = bar open, `end = start + granularity`, API returns newest first.

## 4. Client (`client.py`)
`CoinbaseClient(base_url="https://api.exchange.coinbase.com", max_rps=3, timeout=10)`,
async httpx, token bucket, retry with backoff+jitter on 429/5xx/timeouts (5 tries), GET only.

| method | endpoint |
|---|---|
| `get_products() -> list[Product]` | `/products` |
| `get_product(pid) -> Product` | `/products/{pid}` |
| `get_book(pid, level=2) -> OrderBook` | `/products/{pid}/book` |
| `get_trades(pid, *, after=None, limit=100) -> tuple[list[Trade], str\|None]` | `/products/{pid}/trades` (cursor = `cb-after` header) |
| `trades_since(pid, since_trade_id, max_pages=5) -> list[Trade]` | paginates until ≤ since id |
| `get_candles(pid, granularity_s, start, end) -> list[Candle]` | chunks of ≤ 300 bars, returns **oldest first**, deduped |
| `get_ticker(pid) -> Ticker`, `get_stats(pid) -> Stats` | `/ticker`, `/stats` |

## 5. Fees (`fees.py`)
```python
@dataclass(frozen=True)
class FeeTier: name: str; maker_rate: Decimal; taker_rate: Decimal   # e.g. 0.006 = 0.60%
FEE_TIERS: dict[str, FeeTier]   # Coinbase Advanced Trade retail schedule (from the notes)
DEFAULT_TIER = <lowest-volume retail tier>
def fee_for(notional: Decimal, *, is_taker: bool, tier: FeeTier) -> Decimal  # ceil to $0.01 (conservative)
```
Fees are charged in USD on every fill: buy cash debit = notional + fee; sell proceeds =
notional − fee. Tier is configurable (`coinbase.fee_tier` name, or explicit rates).

## 6. Paper broker (`paper.py`, `broker.py`) — spot semantics, all mandatory
```python
@dataclass
class SpotOrderIntent:
    product_id: str
    side: Literal["buy", "sell"]
    quote_size: Decimal | None = None   # buys: USD to spend (incl. fee), market/IOC
    base_size: Decimal | None = None    # sells (and optional limit buys): quantity
    order_type: Literal["market", "limit"] = "market"
    limit_price: Decimal | None = None
    tif: Literal["ioc", "gtc"] = "ioc"
    post_only: bool = False
    expires_in_s: int | None = None
    strategy: str = ""
    reason: str = ""
    target_weight: float | None = None
    expected_edge_bps: float | None = None
```
`SpotPaperBroker(md, store, *, settings, clock=None)`:
- `async place_order(intent) -> SpotOrder`, `async cancel_order(order_id, reason="") -> SpotOrder`,
  `async maintain() -> list[SpotFill]` (resting orders: fills + expiry), `async mark() -> None`,
  `account() -> SpotAccountState`, `positions(strategy=None) -> list[SpotPosition]`,
  `open_orders(strategy=None) -> list[SpotOrder]`, `portfolio_view(strategy) -> SpotPortfolioView`,
  `reset(starting_balance) -> SpotAccountState`, `equity_snapshot() -> dict`.
1. **Fresh book at execution** (`md.book(pid, max_age_s=2)`), never the strategy's view.
2. **Market/IOC** walk the opposite side best-first within the limit (market = no limit,
   but cap slippage at `coinbase.paper.max_slippage_bps`, default 100). Buys by
   `quote_size`: spend until quote (incl. fee) exhausted. Sells by `base_size`.
3. **Consumed liquidity** per (pid, side, price) with TTL like the Kalshi broker.
4. **Resting GTC** (optionally post-only: rejected if it would cross) records `queue_ahead`
   = displayed size at its price in a book fetched with `max_age_s=0`. Fills only from
   **later public trades**: a resting BUY at P fills from trades with `maker_side == "buy"`
   at price < P (trade-through, up to the trade size) or at P after `queue_ahead` is
   consumed; a resting SELL symmetric (`maker_side == "sell"`, price > P / at P).
   `queue_ahead` only shrinks with later books; a book-lowered bound remembers the book's
   time, and at-price prints stamped at or before it (the tape lags the book) do not burn
   the bound again. A book crossing the limit **without a print** fills at our limit only
   with `coinbase.paper.fill_on_book_cross: true` (default **false**, per
   `docs/coinbase_api_notes.md` §6.1 rule 4). Maker fee rate. Expiry via `expires_in_s`
   (default 3600 s).
5. **Constraints / rejections**: product not `tradable`; `limit_only` products reject
   market orders; size rounded down to `base_increment`; notional < `min_market_funds`
   → reject; **no shorting** (sell ≤ the strategy's held quantity); insufficient cash.
6. **Positions per (strategy, product)**: base qty, avg cost per unit **including buy
   fees**, realized P&L on sells (proceeds − fees − avg cost × qty). Shared USD cash pool;
   resting buy orders reserve cash.
7. **Marks**: `liquidation_value` = walk the bid ladder for the held qty (fall back to
   best bid × qty) **minus the exit taker fee** (`exit_fee`, reported separately; gross =
   `liquidation_value + exit_fee`); `mid_value` separately (no fee). Equity = cash +
   reserved + liquidation value, so equity, unrealized P&L, drawdown, the daily-loss kill
   switch and risk see what selling would actually return. Planner allocations
   (`alloc_equity`) are sized on equity before exit fees (holdings are planned at mids).
8. Every order/fill/position persists (restart restores exactly).

`SpotAccountState` fields = §13 `/api/coinbase/account`.

## 7. Store (`store.py`)
Separate SQLite file (default `data/coinbase.sqlite3`, WAL) with its own
`kalshibot.store.ProcessLock` (reuse the class; do not modify it). Tables: `account`,
`orders`, `fills`, `positions`, `equity_snapshots`, `signals`, `logs`, `strategy_state`,
`risk_limits`, `backtests`, `schema_version`. Same thread-safety approach as `kalshibot/store.py`.

## 8. Strategies (`strategies/base.py`) — bar-based target weights
```python
@dataclass
class TargetWeight:
    product_id: str
    weight: float          # fraction of THIS strategy's allocation, 0..1; sum over targets <= 1
    reason: str
    expected_edge_bps: float | None = None
    score: float | None = None

class SpotContext(Protocol):
    now: datetime
    bar_end: datetime                      # close time of the bar just completed
    products: Mapping[str, Product]        # tradable USD products
    params: Mapping[str, Any]
    def candles(self, product_id: str, n: int) -> list[Candle]  # CLOSED bars with end <= bar_end, oldest first
    def stats(self, product_id: str) -> Stats | None
    portfolio: SpotPortfolioView           # this strategy's holdings, allocation equity, cash
    def log(self, msg: str, **data) -> None

class SpotStrategy(ABC):
    name: ClassVar[str]; description: ClassVar[str]; experimental: ClassVar[bool] = False
    default_params: ClassVar[dict]; param_schema: ClassVar[dict]
    bar_granularity_s: ClassVar[int] = 86400          # 3600 or 86400
    history_bars: ClassVar[int] = 250                  # lookback the engine must provide
    execution: ClassVar[str] = "taker"                 # or "maker_then_taker"
    rebalance_band: ClassVar[float] = 0.02             # no-trade band (fraction of allocation)
    backtestable: ClassVar[bool] = True
    def universe(self, products: Mapping[str, Product]) -> list[str]: ...
    def on_bar(self, ctx: SpotContext) -> list[TargetWeight] | None: ...   # None = no change; [] = all cash
```
`on_bar` is **synchronous and pure** (no I/O): the engine/backtester pre-loads candles.
Registry auto-discovers modules in `kalshibot/coinbase/strategies/`.

## 9. Rebalance planner (`rebalance.py`)
`plan_rebalance(targets, holdings, prices, alloc_equity, products, *, band, min_trade_usd, strategy) -> list[SpotOrderIntent]`
— sells first, then buys; skip changes smaller than `band × alloc_equity` or
`max(min_trade_usd, product.min_market_funds)`; round increments; products not in
`targets` go to 0. Used identically by the engine and the backtester.

## 10. Engine (`engine.py`, `marketdata.py`, `services.py`)
`CoinbaseEngine` with `start() / stop() / status() / set_kill_switch(on, reason)`. Loops:
products refresh (1 h); **bar scheduler** — for each enabled strategy, at
`bar_end + bar_delay_s` (default 60 s) fetch/extend candles for its universe (one bar of
overlap; a final bar fetched < 120 s after its end is re-fetched; a product missing the
final bar while another series — or BTC-USD — has it had no trades and is not waited for),
build
`SpotContext`, `on_bar`, `plan_rebalance`, risk check, broker (sells then buys; for
`maker_then_taker`: post-only at best bid/ask for `maker_timeout_s`, then taker for the
remainder); resting-order maintenance (15 s); marks + equity snapshot (60 s); prune
logs/signals (1 h). `on_bar` runs in its own daemon thread (never asyncio's shared default
executor); a call that times out skips that strategy's later bars until it returns. On
shutdown the two venues close side by side, Coinbase capped at 10 s (engine loop 5 s,
jobs then cancelled). Exceptions in one strategy never kill the loop; Coinbase API outage →
backoff, status shows it. Rate limit `coinbase.max_rps` (default 3). Reuse
`kalshibot.engine.EventBus` (import only) for its own bus; every published event's data
includes `"venue": "coinbase"`. Every intent recorded as a signal with decision + reason.

## 11. Risk (`risk.py`)
`SpotRiskManager.check(intent, portfolio, account) -> RiskDecision(approved_quote|approved_base, reason)`:
`max_position_pct_per_product` (of Coinbase equity, default 50), `max_total_exposure_pct`
(default 90), `max_strategy_allocation_pct` (per strategy, overridable), `min_cash_reserve`
(default $20), `max_orders_per_minute` (20), `daily_loss_limit` ($100) → kill switch (blocks
buys; sells to reduce risk are always allowed), `max_spread_bps` (50), `min_trade_usd` (10).

## 12. Backtester (`backtest.py`)
`run_spot_backtest(strategy_cls, params=None, *, start=None, end=None, starting_balance=1000,
fee_tier=None, slippage="spread"|bps, data_dir=None) -> dict` — replays
`research/coinbase/data` (see its loader/README) through the same strategy + planner:
decide at bar close t, fill at bar t+1 **open** ± half-spread slippage (per-product from the
data's spread snapshot; else a point-in-time estimate from the product's trailing 30-day USD
volume, never below the widest measured half-spread nor 5 bps, capped at 300 bps; option
`slippage_multiplier`) + taker fee. Each fill is capped at `max_participation` (default 10%)
of the fill bar's USD volume (volume × (O+H+L+C)/4); the rest is unfilled. The open is a
zero-latency fill (documented in `known_biases`); `fill_price="pessimistic"` fills at the
worse of the open and (O+H+L+C)/4. Holdings are marked net of the exit taker fee. Metrics: total return, CAGR, vol, Sharpe,
Sortino, max drawdown, turnover/yr, fees paid, % time invested, trades, win rate;
benchmarks buy-and-hold BTC and equal-weight universe (same fees); equity curve (+benchmark
curves), trades, by_year, by_month. Shape extends the Kalshi backtest detail (§13).

## 13. REST API & server integration
**Server integration (only these edits to `kalshibot/api/server.py`)**: in `lifespan`,
after Kalshi services are built, `app.state.cb = await build_coinbase_services(settings)`
inside try/except (on failure or `coinbase.enabled: false`: `app.state.cb = None`,
`app.state.cb_error = str(e)`, Kalshi continues); start its engine if
`coinbase.engine.autostart` (`kalshibot serve --no-engine` starts neither venue's engine);
close it on shutdown, side by side with Kalshi (never before it). Include the router
(`app.include_router(coinbase_router, prefix="/api/coinbase")`) and `/api/overview`
**before** the `/api/{rest:path}` 404 catch-all. When Coinbase is unavailable every
`/api/coinbase/*` route returns 503 `{"detail": "coinbase venue unavailable: <reason>"}`.
Existing `/api/*` (Kalshi) routes and shapes are unchanged.

`/api/coinbase/*` (all JSON; every top-level object includes `"venue": "coinbase"`):

| Method & path | Response |
|---|---|
| `GET status` | `{venue, mode:"paper", engine:{running, started_at, last_tick_at, last_bar_at, tick_count, products_loaded, last_error, last_error_at, kill_switch, kill_switch_reason, coinbase_reachable, strategies_enabled:[...]}, fee_tier:{name, maker_rate, taker_rate}, server_time}` |
| `POST engine/start` · `engine/stop` · `engine/kill-switch {on}` | status |
| `GET account` | `{venue, starting_balance, cash, reserved_cash, positions_liquidation_value, positions_mid_value, equity, equity_mid, realized_pnl, unrealized_pnl, fees_paid, total_pnl, total_return_pct, todays_pnl, max_drawdown_pct, open_positions, open_orders, trades, win_rate, positions_exit_fee}` (`positions_liquidation_value` is net of `positions_exit_fee`) |
| `POST account/reset {starting_balance?}` | account |
| `GET equity?range=1d\|7d\|30d\|all` | `[{ts, equity, equity_mid, cash, realized_pnl, unrealized_pnl}]` |
| `GET positions` | `[{venue, product_id, base_currency, strategy, quantity, avg_cost, cost_basis, mark_price, best_bid, liquidation_value, liquidation_value_gross, exit_fee, mid_value, unrealized_pnl, unrealized_pnl_pct, realized_pnl, fees_paid, weight_of_strategy, opened_at, url}]` (`liquidation_value` net of `exit_fee`; `mark_price` = gross exit price per unit) |
| `GET orders?status=open\|all&limit=` | `[{venue, id, product_id, side, order_type, tif, post_only, quote_size, base_size, limit_price, filled_base, filled_quote, avg_fill_price, fees, status, strategy, reason, created_at, updated_at, expires_at}]` |
| `POST orders/{id}/cancel` | order (404 unknown, 409 not open) |
| `GET fills?limit=` | `[{venue, id, order_id, product_id, side, base_size, price, notional, fee, fee_rate, is_taker, ts, strategy}]` |
| `GET strategies` / `PATCH strategies/{name} {enabled?, params?}` | `[{venue, name, description, experimental, enabled, enabled_source, params, param_schema, bar_granularity_s, universe:[...], backtestable, stats:{orders, fills, open_positions, realized_pnl, unrealized_pnl, fees, exposure, allocation_pct, last_bar_at, last_error}}]` |
| `GET risk` / `PATCH risk` | `{venue, limits:{...§11}, utilization:{total_exposure, total_exposure_pct, by_product:[...], by_strategy:[...], orders_last_minute, daily_pnl}, kill_switch}` |
| `GET signals?limit=` | `[{venue, id, ts, strategy, product_id, side, target_weight, quote_size, base_size, limit_price, expected_edge_bps, reason, decision:"executed"\|"partial"\|"rejected"\|"unfilled"\|"resting", decision_reason, order_id}]` |
| `GET logs?limit=` | `[{venue, id, ts, level, kind, message, data}]` |
| `GET products?search=&sort=volume\|spread\|change&limit=` | `[{venue, product_id, base_currency, price, bid, ask, spread_bps, change_24h_pct, volume_24h_usd, tradable, url}]` |
| `GET analytics` | `{venue, overall:{trades, total_pnl, return_pct, sharpe, max_drawdown_pct, fees, turnover}, by_strategy:{name:{...}}, benchmark:{btc_buy_hold_return_pct, since}, readiness:{ready, reasons}}` |
| `GET backtests` / `POST backtests {strategy, params?, start?, end?, starting_balance?, fee_tier?}` / `GET backtests/{id}` | as Kalshi, plus `metrics` per §12 and `benchmarks:{btc:[{ts, equity}], equal_weight:[...]}` |
| `GET stream` | SSE like `/api/stream` (retry, replay, `Last-Event-ID`), types `tick, signal, order, fill, log, account, bar`; every data object has `venue:"coinbase"` |

`GET /api/overview` → `{generated_at, venues:{kalshi:{venue, label:"KALSHI · prediction markets", available:true, engine_running, kill_switch, starting_balance, equity, cash, total_pnl, total_return_pct, todays_pnl, open_positions, fees_paid, last_error}, coinbase:{venue, label:"COINBASE · crypto spot", available, unavailable_reason, ...same fields, last_tick_at (60 s snapshot tick), last_bar_at (last strategy bar close), coinbase_reachable}}, combined:{starting_balance, equity, total_pnl, total_return_pct, note:"Sum of two separate paper accounts"}, equity_series:{kalshi:[{ts, equity}], coinbase:[{ts, equity}]}}`.
`url` for products: `https://www.coinbase.com/advanced-trade/spot/{product_id}`.

## 14. Frontend — venues must be evident
- **Routes**: `/` = **Overview** (both venues). Kalshi pages move under `/kalshi`
  (`/kalshi`, `/kalshi/positions`, `/kalshi/history`, `/kalshi/strategies`, `/kalshi/signals`,
  `/kalshi/markets`, `/kalshi/analytics`, `/kalshi/backtests`, `/kalshi/backtests/:id`,
  `/kalshi/settings`); legacy paths (`/positions`, …) **redirect** to them. Coinbase under
  `/coinbase` (`/coinbase` dashboard, `/coinbase/positions`, `/coinbase/history`,
  `/coinbase/strategies`, `/coinbase/signals`, `/coinbase/markets`, `/coinbase/analytics`,
  `/coinbase/backtests`, `/coinbase/backtests/:id`, `/coinbase/settings`).
- **Nav**: "Overview", then a group headed by `<VenueBadge venue="kalshi" long />` with
  its links, then a group headed by `<VenueBadge venue="coinbase" long />` with its links.
  Link text is prefixed only by the group (no ambiguity because the group header is always
  visible); the active group is highlighted in the venue colour.
- **Every venue page** begins with `<VenueBanner venue=… />`; KPI tiles, table rows,
  activity-feed rows, toasts and confirm dialogs about a venue include `<VenueBadge>` or the
  venue name in text ("Stop the Coinbase engine?"). Top bar: two engine pills, each
  labeled with its VenueBadge. Browser tab title: "Coinbase · Positions — kalshibot".
- **Units**: Kalshi = contracts, prices in ¢, $ P&L. Coinbase = quantities with base
  symbol (`0.01234567 BTC`), USD prices (`$84,475.95`), fees as `$0.61 (0.60%)`.
- **Colours**: tokens `--venue-kalshi*` (teal) and `--venue-coinbase*` (violet) in
  `styles/tokens.css` for dark and light (F1). Never reuse P&L blue/red or status colours.
- **Overview page**: two side-by-side venue cards (label, equity, total P&L, today,
  open positions, engine state, "Open Kalshi →" / "Open Coinbase →"), a combined total
  labeled "Sum of two separate paper accounts", and an equity chart with one line per
  venue in its venue colour with a legend naming the venue.
- **Mock mode** (`VITE_MOCK=1`) must cover `/api/overview` (F1) and `/api/coinbase/*` (F2).
- F2 creates `src/pages/coinbase/index.ts` **first** (placeholder components) exporting:
  `CoinbaseDashboard, CoinbasePositions, CoinbaseHistory, CoinbaseStrategies,
  CoinbaseSignals, CoinbaseMarkets, CoinbaseAnalytics, CoinbaseBacktests,
  CoinbaseBacktestPage, CoinbaseSettings` — F1 routes to these names.

## 15. Config
```yaml
coinbase:
  enabled: true
  base_url: https://api.exchange.coinbase.com
  max_rps: 3
  starting_balance: 1000
  fee_tier: <default tier name>      # or fee_rates: {maker: 0.006, taker: 0.012}
  storage_path: data/coinbase.sqlite3
  paper: {max_slippage_bps: 100, consumed_liquidity_ttl_s: 300, default_gtc_expiry_s: 3600,
          fill_on_book_cross: false}
  engine: {autostart: true, bar_delay_s: 60, maintenance_s: 15, snapshot_s: 60,
           products_refresh_s: 3600, maker_timeout_s: 120}
  risk: {max_position_pct_per_product: 50, max_total_exposure_pct: 90, max_strategy_allocation_pct: 50,
         min_cash_reserve: 20, max_orders_per_minute: 20, daily_loss_limit: 100,
         max_spread_bps: 50, min_trade_usd: 10}
  strategies: {}
```
Env overrides `KALSHIBOT_COINBASE__…` via the existing mechanism. Relative
`storage_path` resolves like `storage.path`. A config.yaml without a `coinbase:` section
gets these defaults (the user's existing config.yaml must work unchanged).

## 16. Docker
`data/coinbase.sqlite3` lives in the already-mounted `data/`; `research/coinbase/` is under
the already-mounted `research/`. No Dockerfile changes expected beyond dependencies.
