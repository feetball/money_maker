# Coinbase API and Market Rules Reference (for the paper spot-trading simulator)

Status: verified 2026-09-27 (Sunday), about 15:40–16:10 UTC, against the live production APIs
with Python/curl probes, plus the official developer docs at docs.cdp.coinbase.com (fetched the
same day), the Coinbase blog post of 2026-09-16 that announced the current Advanced fee schedule,
the Coinbase Help page on Advanced fees, and the Coinbase Exchange web app.

> **Scope: PAPER TRADING ONLY.** Everything here uses public, unauthenticated market data. The
> simulator must never place real orders, and it must never ask for or store Coinbase API keys.
> Order rules and fees are documented so the paper broker can imitate them, not so it can call
> order endpoints.

Every claim is tagged:
- **[LIVE]**: I checked it against the production API on 2026-09-27. The probe script is named
  where useful (`research/coinbase/api_probe/pNN_*.py`).
- **[DOCS]**: stated in the official developer docs (docs.cdp.coinbase.com: Exchange and Advanced
  Trade API references, OpenAPI and AsyncAPI specs).
- **[CB-BLOG]**: Coinbase blog, "We're lowering fees for many active traders on Coinbase
  Advanced", 2026-09-16, including its two tier and fee tables, which are published as images.
- **[CB-HELP]**: help.coinbase.com, "Coinbase Advanced fees" (the public part; the per-tier table
  is visible only after sign-in).
- **[CB-UI]**: text in the Coinbase Exchange web app (exchange.coinbase.com) bundle.
- **[3P]**: third-party summaries only, not confirmed. Where they disagree, that is noted.
- **[INFERRED]**: my reasoning from the above.

---

## 0. TL;DR for engineers

| Topic | Rule |
|---|---|
| Market-data host (recommended) | **Coinbase Exchange public REST** `https://api.exchange.coinbase.com`. No auth is needed for `/products*`, `/book`, `/ticker`, `/trades`, `/candles`, `/stats`, `/time`, `/currencies`. [LIVE] |
| Secondary host | **Advanced Trade public REST** `https://api.coinbase.com/api/v3/brokerage/market/...` plus `/time`. No JWT is needed. Use it for retail order-size metadata (`base_min_size`, `quote_min_size`, `*_max_size`) and USDC→USD alias mapping. [LIVE][DOCS] |
| Same book | Both APIs expose the **same Coinbase Exchange ("CBE") order book and trade tape**. `product_venue: "CBE"` appears on Advanced Trade (AT) products. Trade IDs are identical across APIs. [LIVE] |
| Symbols | Use `BASE-USD` (e.g. `BTC-USD`). On AT, the 405 `*-USDC` products are **aliases of the USD book** (`alias: "BTC-USD"`). On Exchange, the `-USDC` books for BTC/ETH are `delisted`. **Treat USDC = USD.** [LIVE] |
| Trade `side` | On **every** public trade feed (Exchange REST `/trades`, Exchange WS `matches`, AT REST ticker, AT WS `market_trades`), `side` is the **maker's** side. `buy` means a resting bid was hit (a down-tick). **Exception:** on the Exchange WS `ticker` channel, `side` is the **taker's** side. [LIVE][DOCS] |
| Default fees (US retail, fresh account) | **Maker 0.50%, taker 0.90%** of notional, charged in the quote currency. This is the "Intro" tier, effective 2026-09-16 (it was 0.60% / 1.20% before). [CB-BLOG] |
| Round-trip cost | Taker in and out costs **1.80%**. Maker both ways costs **1.00%**. BTC-USD's spread is about 0.001 bps, so **fees dominate everything**. [INFERRED] |
| Minimum order | `min_market_funds` on Exchange = `quote_min_size` on AT = **$1 notional** on 485 of 488 USD pairs. It applies to limit orders (`size × price`) and to market `funds`/`quote_size`. [LIVE][DOCS] |
| Increments | Price must be a multiple of `quote_increment` (called `price_increment` on AT). Size must be a multiple of `base_increment`. Violations are **rejected**, not rounded (`INVALID_PRICE_PRECISION`, `INVALID_SIZE_PRECISION`). [DOCS] |
| Slippage guard | `max_slippage_percentage` (the "Price Protection Point") is 2% for BTC-USD and ETH-USD and 3% for most alts. Market and limit orders fill only up to that distance from the mid at order time, and the **rest is cancelled**. [CB-UI][LIVE] |
| Spot only | `margin_enabled=false` on all 838 products. There is no shorting: a sell needs base-currency balance. [LIVE] |
| Rate limits | Exchange public REST: **10 req/s per IP, burst 15**, token bucket, HTTP 429 on excess, and **no rate-limit headers**. AT public REST limit: not published on the current docs pages. Budget ≤ 5 req/s total. [DOCS][LIVE] |
| CDN caching | Exchange L1/L2 book: up to about 2 s old. Ticker: about 1–2 s. `/trades` first page: **up to about 6 s**. Candles without start/end: **up to 5 min**. AT public: about 1 s. [LIVE] |
| WebSocket without auth | Exchange `wss://ws-feed.exchange.coinbase.com`: `heartbeat`, `ticker`, `ticker_batch`, `matches`, **`level2_batch`**, `status`, `auctionfeed`, `rfq_matches` are open. `level2`, `level3` and `full` are **refused**. AT `wss://advanced-trade-ws.coinbase.com`: `heartbeats`, `ticker`, `ticker_batch`, `market_trades`, **`level2`**, `candles`, `status` are all open. Only `user` needs auth. [LIVE] |
| History | Exchange candles for BTC-USD: **1-minute data from 2015-01-20, hourly and daily from 2015-07-20**. ETH-USD from 2016-05-18. The trade tape (`/trades`) pages back to **trade_id 1 (2015-01-08)**. [LIVE] |

---

## 1. Which public API to use

### 1.1 Hosts and authentication [LIVE][DOCS]

| API | REST base | Public WS | Auth for market data |
|---|---|---|---|
| Coinbase Exchange (institutional venue, same book) | `https://api.exchange.coinbase.com` | `wss://ws-feed.exchange.coinbase.com` | None for REST market data or most WS channels. `level2`, `level3`, `full` and `user` need an Exchange API key. |
| Advanced Trade (retail API) | `https://api.coinbase.com/api/v3/brokerage` (public paths under `/market/...` and `/time`) | `wss://advanced-trade-ws.coinbase.com` (user data: `wss://advanced-trade-ws-user.coinbase.com`) | None. JWT is "optional" on market channels [DOCS AsyncAPI]; only `user` needs a CDP JWT. |

The authenticated AT paths (`/best_bid_ask`, `/products/{id}`, `/transaction_summary`) return
`401 Unauthorized` without a key [LIVE]. The same is true of Exchange `/fees`, `/accounts`,
`/orders` and `/users/self/trailing-volume` [LIVE]. **Do not use any of them.**

### 1.2 Coinbase Exchange public REST: endpoint shapes [LIVE]

All numbers are strings unless noted. Timestamps are RFC 3339 with nanoseconds on book and
ticker responses and microseconds on trades.

| Endpoint | Response shape (verified) | CDN `cache-control` |
|---|---|---|
| `GET /products` | Array of 838 objects: `{id, base_currency, quote_currency, quote_increment, base_increment, display_name, min_market_funds, margin_enabled, post_only, limit_only, cancel_only, status, status_message, trading_disabled, fx_stablecoin, max_slippage_percentage, auction_mode, high_bid_limit_percentage}`. **No** `base_min_size`, `base_max_size` or `max_market_funds`; those were removed 2022-06-30 [DOCS]. | max-age=5 |
| `GET /products/{id}` | The same single object. An unknown id returns `404 {"message":"NotFound"}`. | max-age=5 |
| `GET /products/{id}/book?level=1` (the default) | `{bids:[[price,size,num_orders]], asks:[[...]], sequence:<int>, auction_mode:false, auction:null, time}` | max-age=2 |
| `GET /products/{id}/book?level=2` | The **full aggregated book** with the same tuple shape. BTC-USD had 23,429 bids + 18,261 asks, about **355 KB** of JSON. The worst bid was $0.01 and the worst ask $138,991,023. | max-age=2 |
| `GET /products/{id}/book?level=3` | The full per-order book, `[price, size, order_id]`, about **3 MB** for BTC-USD. It **works without auth**, but the docs say "abuse of Level 3 via polling can cause your access to be limited or blocked". **Do not poll it.** | max-age=1 |
| `GET /products/{id}/ticker` | `{ask, bid, volume, trade_id:<int>, price, size, time, rfq_volume}` | max-age=1 |
| `GET /products/{id}/stats` | `{open, high, low, last, volume, volume_30day, rfq_volume_24hour, rfq_volume_30day}`. Volumes are in base units. | max-age=5 |
| `GET /products/stats` | All products in **one call** (about 109 KB): `{ "BTC-USD": {stats_30day:{volume, rfq_volume}, stats_24hour:{open,high,low,last,volume,rfq_volume}}, ...}` | max-age=5 |
| `GET /products/volume-summary` | `[{id, base_currency, quote_currency, display_name, market_types:["spot"|"rfq"...], spot_volume_24hour, spot_volume_30day, rfq_volume_*, conversion_volume_*}]` | max-age=5 |
| `GET /products/{id}/trades?limit=&before=&after=` | `[{trade_id:<int>, side:"buy"\|"sell", size, price, time}]`, **newest first**. `limit` ≤ 1000: the default is 1000, and larger values are clamped to 1000. Response headers `cb-before` (the newest id on the page) and `cb-after` (the oldest id). See §1.6 for the cursor quirk. | max-age=5 |
| `GET /products/{id}/candles?granularity=&start=&end=` | `[[time, low, high, open, close, volume], ...]`. These are **JSON numbers, not strings**, newest first, and volume is in base units. See §7. | **max-age=300** |
| `GET /time` | `{iso, epoch}` | no-store |
| `GET /currencies/{id}` | `{id, name, min_size, status, max_precision, details{...}, supported_networks[...]}` | max-age=10 |
| `GET /fee-rates` (undocumented, public) | The Exchange fee schedule, `[{usd_from, usd_to, maker_fee_rate, taker_fee_rate}]`. See §3.4. | |

Errors: `level=4` returns `400 {"message":"unexpected level: _"}`, and an unknown product returns
`404 {"message":"NotFound"}` [LIVE].

### 1.3 Advanced Trade public REST: endpoint shapes [LIVE][DOCS]

| Endpoint | Response shape (verified) |
|---|---|
| `GET /time` | `{iso, epochSeconds, epochMillis}` (all strings) |
| `GET /market/products` | `{products:[...], num_products, pagination}`. By default this returns **923 SPOT** products (915 online, 8 delisted). Filters that work: `product_type=SPOT\|FUTURE`, `product_ids=A&product_ids=B`, `limit`. Each product has 54 fields, including `price_increment`, `base_increment`, `quote_increment`, `base_min_size`, `base_max_size`, `quote_min_size`, `quote_max_size`, `status`, `trading_disabled`, `cancel_only`, `limit_only`, `post_only`, `auction_mode`, `alias`, `alias_to`, `product_venue`, `price`, `volume_24h`, `approximate_quote_24h_volume`, `high_24h` and `low_24h`. `best_bid_price` and `best_ask_price` are **empty strings** in the public listing. |
| `GET /market/products/{id}` | The same single object. Unknown id: `404 {"error":"NOT_FOUND","message":"Product NOPE-USD not supported"}` |
| `GET /market/product_book?product_id=&limit=&aggregation_price_increment=` | `{pricebook:{product_id, bids:[{price,size}], asks:[...], time}, last, mid_market, spread_bps, spread_absolute}`. It returns **at most 1000 levels per side**, both by default and when `limit=5000`. `aggregation_price_increment=10` buckets BTC into $10 levels. There is **no order count per level.** |
| `GET /market/products/{id}/ticker?limit=&start=&end=` | `{trades:[{trade_id (string), product_id, price, size, time, side:"BUY"\|"SELL", bid:"", ask:"", exchange:"coinbase"}], best_bid, best_ask}`. It returns **at most 100 trades** (values from 101 to 1000 are silently capped at 100) and `limit=1001` → `500 INTERNAL`. There is no cursor. `start`/`end` (UNIX seconds) return the newest ≤100 trades inside that window. |
| `GET /market/products/{id}/candles?start=&end=&granularity=&limit=` | `{candles:[{start, low, high, open, close, volume}]}` as strings, newest first. `start`/`end` are **UNIX seconds**. At most **350 candles**; 351 → `400 INVALID_ARGUMENT "number of candles requested should be less than 350"`. |

Caching: every AT public response carries `cache-control: public, max-age=14400`, but the data is
fresh, which matches the docs: "Public responses are cached for 1 second. For live data, use the
WebSocket, send `cache-control: no-cache`, or call the private product endpoints." [DOCS] The
book `time` on AT was 0.6–1.7 s old [LIVE, p03]. **Ignore the 14400.**

### 1.4 Differences that matter

| Aspect | Exchange public REST | Advanced Trade public REST |
|---|---|---|
| Symbols | `BTC-USD`. USDC books are delisted. 838 products including 324 delisted ones; 402 online USD pairs have stats. | `BTC-USD` **and** `BTC-USDC`. The USDC product is an **alias** of the USD book: same trade IDs, returned with `product_id:"BTC-USDC"` [LIVE p09]. Delisted products are mostly absent. |
| Book depth | Full aggregated book (L2) with **order count per level**. The full per-order L3 book is also available but must not be polled. | Top 1000 levels per side, sizes only. It adds `mid_market` and `spread_bps`. |
| Trades | 1000 per page, cursor pagination back to 2015. | 100 per call, time window only, no cursor. |
| Candles | 300 buckets per request. The **default request (no start/end) is CDN-cached for up to 5 min.** Numbers are floats. | 350 buckets per request, about 1 s cache, strings. It has 30-minute, 2-hour, 4-hour and weekly buckets. |
| Order-size metadata | Only `min_market_funds`, which has been repurposed as the limit-order notional minimum. | `base_min_size`, `base_max_size`, `quote_min_size` and `quote_max_size` (e.g. BTC-USD: 3400 BTC and $150,000,000 max). |
| Freshness (measured) | L1 book 0.2–2.4 s. Ticker 0.6–2.9 s. `/trades` first page **1.3–6.1 s** (cf `age` header up to 4). | Book 0.6–1.7 s. Newest trade on the ticker 0.2–2.0 s. |
| Rate limit | 10 req/s per IP, burst 15 [DOCS] | Not published on the current docs pages [3P says about 10 req/s per IP] |

**Recommendation for a REST-polling paper bot:**

1. **Use Coinbase Exchange public REST as the primary market-data source.**
   - Use `/products/{id}/book?level=2` for taker fills and for queue position. It returns the full
     depth with `num_orders` in one call.
   - Use `/products/{id}/trades` for maker fills. It is gap-free with cursor pagination (§1.6).
   - Use `/products/stats` to rank the whole universe in one call.
   - Use `/candles` for bars.
2. **Pull product rules from AT `/market/products` once per hour.** This gives `base_min_size`,
   `quote_min_size`, the max sizes and the alias mapping. Join it to Exchange `/products` on
   `product_id`; the Exchange side supplies `max_slippage_percentage`, `fx_stablecoin` and
   `high_bid_limit_percentage`.
3. **Upgrade path, not required for v1.** One Exchange WS connection with `heartbeat`, `matches`
   and `level2_batch` gives a real-time tape and a real-time book with no REST cost. Heartbeats
   carry `last_trade_id` for gap detection.

### 1.5 Measured freshness details [LIVE p03, p07, p12]

- Exchange `/book?level=1`: `time` inside the payload was 0.17–2.43 s older than the wall clock
  (cf `HIT`, `age` 0–1).
- Exchange `/products/{id}/trades` (default page): the newest trade was **1.3–6.1 s** old, and the
  CDN `age` header was 0–4 s. `/ticker` at the same moment was fresher (0.6–2.2 s).
  Requests with `after=` or `before=` were cf `MISS`, i.e. fresh.
- Exchange `/candles` **without** start/end returned the **350** newest candles with CDN `age`
  255–299 s, so the data was up to 5 minutes old. With explicit start/end it was a cache MISS.
  Sending `Cache-Control: no-cache` returned `age: 0` on Exchange candles too.
  **Always pass explicit `start` and `end`.**
- AT endpoints: about 1 s. The docs promise a 1 s cache.

### 1.6 `/trades` cursor semantics, and a gap-free polling loop [LIVE p05][DOCS]

- Pages are newest-first. `cb-before` is the newest id on the page and `cb-after` is the oldest.
- `after=X` returns trades **older** than X, the page immediately below X. This works as
  documented: `after=1099145648` → `1099145647..1099145638`.
- **Quirk:** `before=X` does **not** return the page just above X. It returns the **newest**
  `limit` trades with id > X. For example, `before=newest-5000&limit=10` returned the 10 newest
  trades, not `X+1..X+10`. The docs describe it as "page before (newer)", which is wrong for gap
  filling.
- Trade IDs are **per product and contiguous**: a 1000-trade page spanned exactly 1000
  consecutive ids. A jump means a gap.

Gap-free loop (per product):
```
last = <id of newest trade already processed>
page = GET /products/P/trades?limit=1000                  # newest first
new  = [t for t in page if t.trade_id > last]
while page and page[-1].trade_id > last + 1:               # more than 1000 new trades: walk back
    page = GET /products/P/trades?limit=1000&after=<page[-1].trade_id>
    new += [t for t in page if t.trade_id > last]
process sorted(new, key=trade_id); last = max(last, *ids)
```
At BTC-USD's Sunday rate (about 230 trades/min) one 1000-trade page covers about 4 minutes, so
polling every 2–5 s never needs the walk-back.

---

## 2. Product metadata that governs orders

### 2.1 Fields and live values [LIVE p01, p02, p09]

Counts of USD pairs in this table cover all 488 Exchange USD products, delisted ones included.

| Field (Exchange / AT) | Meaning for the simulator | Live values (2026-09-27) |
|---|---|---|
| `quote_increment` / `price_increment` (AT also has `quote_increment`) | Price tick. Limit and stop prices must be multiples of it. | USD pairs: `0.00001` (152 pairs), `0.0001` (145), `0.000001` (86), `0.01` (42), `0.001` (30), `1e-7` (20), `1e-8` (13). BTC/ETH/SOL-USD `0.01`, XRP `0.0001`, DOGE `0.00001`, SHIB `0.00000001`. |
| `base_increment` | Size step. | `0.01` (159 pairs), `0.1` (138), `1` (101), `0.001` (48), `1e-8` (14), … BTC/ETH/SOL `1e-8`, XRP `1e-6`, DOGE `0.1`, SHIB `1`. |
| `min_market_funds` (Exchange) / `quote_min_size` (AT) | **Notional minimum** in quote currency. Limit: `size × price ≥ min`. Market: `funds`/`quote_size ≥ min`. [DOCS][CB-UI] | `1` on 485 of 488 USD pairs, `5` on 3. |
| `base_min_size` (AT only) | Minimum base size. | Equals `base_increment` on the majors (BTC `1e-8`, DOGE `0.1`, SHIB `1`). |
| `base_max_size`, `quote_max_size` (AT only) | Per-order maximums. | BTC-USD 3400 / $150M. ETH 42000 / $150M. SOL 1,274,000 / $25M. DOGE 141.8M / $10M. |
| `max_slippage_percentage` (Exchange) | **Price Protection Point (PPP).** "Market and Limit Orders will fill at prices up to the PPP from the mid-point price between the best bid and best offer on the Order Book at the time the Order was placed … the Order will partially fill up to the PPP level and the matching engine will cancel all remaining portions." [CB-UI] It appears as `done` cancel reason `104: Price Bound Order Protection` [DOCS]. | `0.03` (760 products), `0.05` (36), `0.01` (33, stables), `0.10` (5), `0.02` (3, incl. BTC-USD and ETH-USD), `0.07` (1). |
| `high_bid_limit_percentage` | Stablecoin pairs only: the cap on a limit **buy** price (a percentage above the reference). [DOCS] | Set on 26 products (e.g. USDT-USD `0.03`), empty elsewhere. |
| `status` | `online`, `offline`, `internal` or `delisted` [DOCS]. Trade only `online`. | Exchange: 514 online, 324 delisted. AT: 915 online, 8 delisted. |
| `trading_disabled` | No new orders. | True exactly for the 324 delisted products. |
| `cancel_only` | Cancels only, no new orders. | 0 products. |
| `limit_only` | Market orders rejected; limit orders allowed. | 21 online, including `STORJ-USD`, `DIEM-USD`, `GNO-USD`, `INV-USD`, `BADGER-USD`, `SYND-USD`, `PAX-USD`, `USD1-USD`, `USDS-USD`, `USDT-USDC`, `BTC-INR`… |
| `post_only` | Only post-only limit orders are accepted. | 0 products. |
| `auction_mode` | Book is in auction (collection/opening). No continuous matching; the book may appear crossed. [DOCS] | 0 products. |
| `fx_stablecoin` | "Stable Pair" (special fee, §3.3). | 32 products (17 online): e.g. `USDC-EUR`, `USDC-GBP`, `EURC-USDC`, `PAX-USD`, `USD1-USD`, `USDS-USD`, `CBETH-ETH`. **`USDT-USD` is `false`.** |

### 2.2 Rounding rules for the simulator [DOCS][INFERRED]

The venue **rejects** bad precision; it does not round. AT reasons include `INVALID_SIZE_PRECISION`,
`INVALID_PRICE_PRECISION`, `PREVIEW_INVALID_QUOTE_SIZE_TOO_SMALL`,
`PREVIEW_INVALID_BASE_SIZE_TOO_SMALL`/`_TOO_LARGE`, `PREVIEW_INVALID_QUOTE_SIZE_TOO_LARGE`,
`PREVIEW_LIMIT_PRICE_TOO_FAR_FROM_MARKET`, `INSUFFICIENT_FUND`, `INVALID_NO_LIQUIDITY` and
`INVALID_LIMIT_PRICE_POST_ONLY`. So the **strategy/order layer** rounds before submitting, and the
**paper broker** rejects anything that is still off-grid.

1. **Size:** `size = floor(size / base_increment) × base_increment`. Always floor, so you never
   buy more than you can pay for or sell more than you hold.
2. **Limit price:** round **down** to `quote_increment` for buys and **up** for sells. This is
   never more aggressive than intended.
3. **Notional check** after rounding: `size × price ≥ min_market_funds` (limit), or
   `funds ≥ min_market_funds` (market). Otherwise reject with "below minimum".
4. **Market buy by quote (`funds`/`quote_size`):** the amount **includes fees**. "A market buy for
   BTC-USD with funds specified as 150.00 will spend 150 USD to buy BTC (including any fees)."
   [DOCS] Walk the asks with `funds / (1 + taker_rate)` of notional and floor the filled base to
   `base_increment`. "Orders may be filled at less than the notional specified due to fees and
   truncation of product base increment." [DOCS]
5. Use `Decimal` everywhere. `base_increment` can be `1e-8` and some `quote_increment`s are `1e-8`.
6. **Dust:** a position worth less than $1 **cannot be sold** by any order type.
   `min_market_funds` also applies to market sells, and a sell with `size × price < $1` fails the
   notional minimum [INFERRED from the DOCS rules]. The simulator should mark dust as unsellable
   instead of liquidating it for free.

### 2.3 USD vs USDC [LIVE][DOCS]

- The Exchange has **no `USDC-USD` product**. `BTC-USDC` and `ETH-USDC` are `delisted` there, and
  USD and USDC books are unified.
- On AT, 405 `*-USDC` products carry `alias: "<BASE>-USD"`. `GET /market/products/BTC-USDC/ticker`
  returned the same trade ids as BTC-USD. Retail USD↔USDC conversion is 1:1 via "Convert"
  (applicable for USDC-USD, PYUSD-USD, EURC-EUR and PYUSD-USDC [DOCS]).
- **Simulator:** keep one USD cash balance and trade `-USD` product ids. Map any `-USDC` symbol to
  its `alias` before looking up data.
- `USDT-USD` is a normal (non-stable-pair) market with `max_slippage_percentage` 0.01 and a 3%
  `high_bid_limit_percentage`.

### 2.4 Liquid USD pairs [LIVE p06; full CSV: `research/coinbase/api_probe/liquidity_snapshot.csv`]

Ranked by 24h USD notional (`/products/stats`: `volume × last`) at 2026-09-27 ~15:50 UTC, a
**Sunday**, so 24h volume runs light; the 30-day average is the steadier guide. Spread and depth
are medians of 3 full-L2 snapshots about 20 s apart. "±10 bps depth" is the USD notional resting
within 10 bps of mid on each side. The total over all 402 online USD pairs was $1,327M in 24h.

| # | Product | 24h $M | 30d avg $M/day | Tick (bps) | Median spread (bps) | ±10 bps bid $k | ±10 bps ask $k | quote / base increment |
|---|---|---:|---:|---:|---:|---:|---:|---|
| 1 | BTC-USD | 201.2 | 498.8 | 0.001 | 0.00 (1 tick) | 1,921 | 1,674 | 0.01 / 1e-8 |
| 2 | ZEC-USD | 154.6 | 193.2 | 0.063 | 4.18 | 104 | 122 | 0.01 / 1e-8 |
| 3 | ETH-USD | 124.8 | 263.8 | 0.037 | 0.04 | 395 | 712 | 0.01 / 1e-8 |
| 4 | XRP-USD | 103.1 | 164.6 | 0.66 | 1.32 | 210 | 523 | 0.0001 / 1e-6 |
| 5 | SOL-USD | 96.2 | 121.0 | 0.82 | 0.82 (1 tick) | 292 | 387 | 0.01 / 1e-8 |
| 6 | NEAR-USD | 70.8 | 55.3 | 0.19 | 4.80 | 16 | 24 | 0.0001 / 0.001 |
| 7 | QNT-USD ⚠ | 70.6 | 4.5 | 0.57 | 15.91 | 0.4 | 0.7 | 0.01 / 0.001 |
| 8 | SUI-USD | 45.6 | 35.5 | 0.81 | 3.24 | 30 | 264 | 0.0001 / 0.1 |
| 9 | USDT-USD | 39.6 | 137.1 | 0.10 | 0.10 | 3,054 | 5,446 | 0.00001 / 0.01 |
| 10 | LINK-USD | 20.2 | 25.8 | 0.71 | 2.13 | 35 | 26 | 0.001 / 0.01 |
| 11 | AVAX-USD | 18.1 | 14.6 | 0.91 | 3.64 | 32 | 7 | 0.001 / 1e-8 |
| 12 | UNI-USD | 18.1 | 27.3 | 0.10 | 2.39 | 12 | 6 | 0.0001 / 1e-6 |
| 13 | TAO-USD | 15.6 | 15.2 | 0.31 | 1.55 | 8 | 49 | 0.01 / 0.0001 |
| 14 | DOGE-USD | 14.8 | 23.9 | 1.03 | 2.07 | 36 | 28 | 0.00001 / 0.1 |
| 15 | HYPE-USD | 14.3 | 42.0 | 1.09 | 1.10 (1 tick) | 99 | 92 | 0.01 / 0.001 |
| 16 | ONDO-USD | 13.5 | 12.6 | 0.19 | 6.10 | 12 | 5 | 0.00001 / 0.01 |
| 17 | ADA-USD | 13.0 | 20.1 | 0.39 | 1.18 | 14 | 11 | 0.00001 / 1e-8 |
| 18 | WLD-USD ⚠ | 12.8 | 3.7 | 1.81 | 7.24 | 2 | 3 | 0.0001 / 0.01 |
| 19 | LTC-USD | 12.8 | 14.1 | 0.14 | 1.55 | 33 | 22 | 0.001 / 1e-8 |
| 20 | AERO-USD | 9.6 | 9.2 | 0.12 | 3.27 | 2 | 11 | 0.00001 / 0.1 |
| 21 | XLM-USD | 9.4 | 15.0 | 0.05 | 2.71 | 23 | 43 | 0.000001 / 1e-8 |
| 22 | HBAR-USD | 9.2 | 8.2 | 1.07 | 4.29 | 12 | 79 | 0.00001 / 0.1 |

⚠ QNT and WLD had a one-day volume spike (24h far above the 30d average) and thin books. Treat
them as illiquid. All of these pairs have `min_market_funds` = 1.

**Takeaways** [INFERRED]:

- Only **BTC, ETH, SOL, XRP, HYPE, USDT** (and ZEC today) have hundreds of $k within 10 bps.
- BTC, ETH, SOL and HYPE sit at a **1-tick spread**, so posting at the touch means joining a queue.
- On SOL, XRP, DOGE and HYPE the tick itself is about 0.7–1.1 bps.
- A sensible v1 universe is BTC, ETH, SOL, XRP, DOGE, LTC, LINK, ADA, AVAX, SUI (all -USD).

---

## 3. Fees

### 3.1 Advanced Trade retail schedule, current since 2026-09-16 [CB-BLOG][CB-HELP]

**Tier qualification:** trailing 30 days, in USD. Your tier is whichever of the three columns is
most favourable. Derivatives volume counts only for "eligible clients/geos".

| Tier | Spot volume | or Derivatives volume | or USDC balance |
|---|---|---|---|
| **Intro** | ≥ $0 | ≥ $0 | ≥ $0 |
| Advanced 1 | ≥ $10,000 | ≥ $100,000 | ≥ $500,000 |
| Advanced 2 | ≥ $50,000 | ≥ $500,000 | ≥ $1,000,000 |
| Advanced 3 | ≥ $250,000 | ≥ $2,500,000 | ≥ $5,000,000 |
| VIP 1 | ≥ $1,000,000 | ≥ $10,000,000 | ≥ $10,000,000 |
| VIP 2 | ≥ $5,000,000 | ≥ $25,000,000 | ≥ $15,000,000 |
| VIP 3 | ≥ $20,000,000 | ≥ $100,000,000 | – |
| VIP 4 | ≥ $50,000,000 | ≥ $250,000,000 | – |
| VIP 5 | ≥ $100,000,000 | ≥ $500,000,000 | – |
| VIP 6 | ≥ $200,000,000 | ≥ $1,000,000,000 | – |
| VIP 7 | ≥ $500,000,000 | ≥ $2,500,000,000 | – |
| VIP 8 | ≥ $1,000,000,000 | ≥ $5,000,000,000 | – |

**Spot rates published by Coinbase** [CB-BLOG]:

| Region | Intro maker | Intro taker | Before 2026-09-16 |
|---|---|---|---|
| **US** | **0.50%** | **0.90%** | 0.60% / 1.20% |
| EU / UK / CA | 0.25% | 0.50% | 0.60% / 1.20% |
| Rest of world (e.g. AU, SG, BR, IN) | 0.09% | 0.10% | 0.60% / 1.20% |
| VIP 8 (best case) | "as low as" 0.00% | 0.02% | |

- "Advanced tiers now start at $10,000 in qualifying volume, down from $25,000."
- **The per-tier maker/taker rates for Advanced 1 through VIP 7 are not public.** The help page
  says "To see the complete fee structure, sign in … Coinbase Advanced fees page" [CB-HELP], and
  coinbase.com/advanced-fees redirects to sign-in [LIVE].
- Third-party tables disagree with each other (for example Advanced 1 at 0.25%/0.40% versus
  0.25%/0.50%) and predate the change. **Do not hard-code them.**

**Fee mechanics** [CB-HELP, verbatim or near-verbatim]:
- "Your fee tier at the time of the order determines the fees, not the tier after the order."
- "Tiers update hourly based on trading volume."
- "Fees are based on total USD trading volume over the past 30 days across all order books.
  Non-USD transactions are converted to USD using the most recent fill price."
- Taker = "orders filled immediately at market price (ex: market order, immediately fulfilled
  limit order)". Maker = orders "placed on the order book [that] pay the maker fee when matched".
- "Partially matched orders: The immediate portion pays the taker fee. The remaining portion on
  the order book pays the maker fee when matched."
- "Advanced fees are subject to change but will always be displayed on the order preview page."

### 3.2 How fees are charged [DOCS][INFERRED]

- **Currency:** the quote currency (USD for `-USD` pairs). The Exchange fill object has
  `fee` in quote units and `usd_volume`. AT fills carry `commission` plus a `commission_detail_total`
  breakdown [DOCS].
- **Formula:** `fee = fill_price × fill_size × rate`, per fill.
  - The Exchange docs example is exact to 10 decimal places with no cent rounding:
    `price 8087.38 × size 0.006018 = 48.66985284`, `fee "0.2433492642000000"` = 0.5% × 48.66985284.
  - The field is printed with 16 decimals. **Simulator: compute in `Decimal` at full precision.**
    An optional pessimistic mode rounds up to $0.01 per fill.
- **Buys:**
  - A limit buy holds `price × size × (1 + fee_rate)` of quote [DOCS].
  - A market buy by `funds`/`quote_size` spends exactly that amount including the fee (§2.2).
  - A buy **by base size** costs `notional + fee`.
- **Sells:** proceeds are `notional − fee`.
- **Cancels** are free. There is no fee for an unfilled post-only rejection [DOCS: "the order will
  be rejected and no part of it will execute"].

### 3.3 Coinbase One, stable pairs [CB-BLOG][CB-UI][3P]

- **Coinbase One:** the 2026-09-16 post mentions Coinbase One only for its "unlimited 3.5% APY on
  USDC". Coinbase markets Coinbase One as "zero trading fees" on simple (non-Advanced) trades.
  Third-party sources disagree on whether it changes Advanced fees: one says no effect, another
  says a 25% USDC rebate capped at $100 a month. There is **no primary evidence of any
  maker/taker change.** Simulator default: **no effect.**
- **Stable pairs, Exchange:** "When you place an order for Stable Pairs, the maker and taker will
  pay a fee of 0.00% and 0.0045%, respectively." Tier volume "does not include volume for trading
  Stable Pairs" [CB-UI]. The stable-pair set is the `fx_stablecoin=true` products [LIVE]; it
  **excludes USDT-USD**.
- **Stable pairs, AT retail:** 0.00% maker and a taker fee of 0.10–0.45 bps by liquidity-program
  tier. "USDT-USDC and USDT-USD are no longer eligible for stablepair pricing" [3P]. This is
  consistent with `USDT-USD` having `fx_stablecoin=false` [LIVE]. None of the stable pairs matter
  for a USD-quoted crypto bot.

### 3.4 Coinbase Exchange (institutional) schedule, for comparison [LIVE: `GET https://api.exchange.coinbase.com/fee-rates`, public, undocumented; also shown on exchange.coinbase.com/fees]

| 30-day USD volume | Maker | Taker |
|---|---:|---:|
| $0 – $10K | 0.40% | 0.60% |
| $10K – $50K | 0.25% | 0.40% |
| $50K – $100K | 0.15% | 0.25% |
| $100K – $1M | 0.10% | 0.20% |
| $1M – $15M | 0.08% | 0.18% |
| $15M – $75M | 0.06% | 0.16% |
| $75M – $250M | 0.03% | 0.10% |
| $250M – $400M | 0.00% | 0.06% |
| $400M+ | 0.00% | 0.04% |

Stable pairs: 0.00% maker and 0.0045% taker, and their volume is excluded from tier volume
[CB-UI]. The Exchange needs institutional onboarding, so it is **not** the user's venue.

### 3.5 Default a retail user starting fresh pays → simulator config

```yaml
coinbase:
  fees:
    region: US
    maker_rate: "0.0050"   # Intro tier, 0.50 %  [CB-BLOG 2026-09-16]
    taker_rate: "0.0090"   # Intro tier, 0.90 %
    fee_currency: quote
    rounding: exact        # or "ceil_cent" for a pessimistic mode
```

- Keep the rates **config-only** and stay on Intro. Paper volume does not move the user's real
  tier, and the Advanced 1+ rates are unknown until the user reads their signed-in fee page.
- Implication: a taker round trip costs **180 bps**, and a maker in plus maker out costs
  **100 bps**. A strategy must expect gross edge above that per round trip.

---

## 4. Order semantics to imitate

### 4.1 Order types

| Real type | Exchange REST | AT `order_configuration` | Paper-broker behaviour |
|---|---|---|---|
| Market by quote (buy) | `type=market, funds` | `market_market_ioc.quote_size` | Walk the asks up to `funds/(1+taker)` notional and **stop at the PPP** (mid × (1 + max_slippage)). The unfilled rest is cancelled. Taker fee. |
| Market by base | `type=market, size` | `market_market_ioc.base_size` | Walk the book for `size` and stop at the PPP. The rest is cancelled. |
| Market FOK | – | `market_market_fok` | All or nothing within the PPP. |
| Limit GTC | `type=limit, time_in_force=GTC` (default) | `limit_limit_gtc{base_size\|quote_size, limit_price, post_only}` | The marketable part fills immediately as taker (walking levels ≤ limit, capped by the PPP). The rest rests as maker. |
| Limit GTD | `GTT` + `cancel_after` ∈ {min, hour, day}. The docs also name GTD, capped at 90 days. | `limit_limit_gtd{…, end_time}` | Like GTC, and auto-cancelled at `end_time`. |
| Limit IOC | `time_in_force=IOC` | `sor_limit_ioc` | Fill what is marketable now; cancel the rest. |
| Limit FOK | `time_in_force=FOK` | `limit_limit_fok` | Fill everything now or reject. |
| Post-only | `post_only=true` (invalid with IOC or FOK) | `post_only:true` on GTC/GTD. FOK + post_only → `POST_ONLY_NOT_ALLOWED_WITH_FOK`. | **Reject the whole order** if any part would take: `INVALID_LIMIT_PRICE_POST_ONLY` / "the order will be rejected and no part of it will execute" [DOCS]. There is no slide or re-price. |
| Stop-limit, bracket, TWAP, scaled | `stop: loss\|entry` + `stop_price` | `stop_limit_stop_limit_gtc/gtd`, `trigger_bracket_gtc/gtd`, `twap_limit_gtd`, `scaled_limit_gtc` | Out of scope for v1. Stops trigger on the **last trade price** [DOCS]. |

### 4.2 Matching and fill rules [DOCS]

- **Price-time priority:** "continuous first-come, first-serve order book. Orders are executed in
  price-time priority."
- **Price improvement:** trades print at the **resting (maker) order's price**. A taker walking the
  book gets each level's price.
- **Partial fills:** the immediate portion is taker, and the rested remainder fills later as maker
  at its limit price. Status goes `received → open → done`. Open orders never expire unless
  GTD/GTT.
- **Self-trade prevention** (Exchange `stp`): `dc` (the default) decrements the larger order and
  cancels the smaller; `co` cancels the oldest; `cn` cancels the newest; `cb` cancels both. "The
  STP instruction on the taker order (latest order) takes precedence." AT exposes no `stp` field.
  **Paper broker:** if a new order would cross the strategy's own resting order, apply `dc`. Real
  fills come only from other people's liquidity.
- **Open-order cap:** 500 open orders per product per profile.
- **Holds (long-only spot):**
  - A buy holds quote (`price × size × (1+fee)` for a limit; `funds` for a market buy).
  - A sell holds base.
  - `available = balance − holds`. Reject with `INSUFFICIENT_FUND` otherwise.
  - There is no margin (`margin_enabled=false` everywhere [LIVE]), so no shorting.
- **Product-state gates:**
  - Refuse all new orders when `status≠online`, `trading_disabled` or `cancel_only`.
  - Refuse market orders when `limit_only`.
  - Refuse non-post-only orders when `post_only`.
  - Skip matching entirely while `auction_mode` (the book may be crossed).
- **Cancel reasons** as seen on the feed: `101 Time In Force`, `102 Self Trade Prevention`,
  `103 Admin`, `104 Price Bound Order Protection`, `105 Insufficient Funds`,
  `106 Insufficient Liquidity`, `107 Broker`. Mirror 101, 104, 105 and 106 in paper.

---

## 5. Rate limits and WebSockets

### 5.1 REST rate limits

| Surface | Limit | Source |
|---|---|---|
| Exchange public REST | **10 req/s per IP, bursts up to 15.** Lazy-fill token bucket: `tokens = min(burst, tokens + dt × rate)`, minus 1 per request. Excess returns **HTTP 429**. | [DOCS] |
| Exchange private REST (n/a) | 15/s per profile, burst 30. `/fills` 10/s, burst 20. | [DOCS] |
| AT public REST | Not stated on the current docs pages. Responses are cached 1 s. | [DOCS]; [3P] about 10/s per IP |
| Response headers | **No rate-limit headers** on either API (no `x-ratelimit-*` or `cb-*` limit headers); only `cache-control`, `cf-cache-status`, `age`, `etag`. You learn you are limited only by a 429. | [LIVE] |
| This probe | Ran at ≤ 2.5 req/s all day and saw **0 × 429**. | [LIVE] |

**Budget for the bot:** one shared token bucket at **5 req/s, burst 8**, plus exponential backoff
on 429 (1 s, 2 s, 4 s…). The IP is shared with the Kalshi bot and other agents.

A worked budget for 10 products, polled every 5 s:
- `/trades` for 10 products: 2 req/s.
- L2 for 10 products: 2 req/s. That is heavy: BTC L2 is about 355 KB. Prefer L1 plus L2 only for
  products with resting orders.
- `/products/stats` once every 60 s.
- Candles once a minute with explicit ranges.

### 5.2 WebSocket: unauthenticated subscribe test [LIVE p04, p13]

**Exchange** `wss://ws-feed.exchange.coinbase.com`. Subscribe with
`{"type":"subscribe","product_ids":["BTC-USD"],"channels":["<ch>"]}`.

| Channel | Result without auth | Message shape / notes |
|---|---|---|
| `heartbeat` | ✅ | `{type:"heartbeat", last_trade_id, product_id, sequence, time}` every 1 s. Use `last_trade_id` for gap detection. |
| `ticker` | ✅ | `{type:"ticker", sequence, product_id, price, open_24h, volume_24h, low_24h, high_24h, volume_30d, best_bid, best_bid_size, best_ask, best_ask_size, side, time, trade_id, last_size}`. **`side` here is the TAKER side**: it was opposite to `matches.side` on 28 of 28 shared trade_ids. |
| `ticker_batch` | ✅ | Subscription echo is `ticker_1000`. Same schema, sent every 5 s if changed [DOCS]. |
| `matches` | ✅ | The first message is `last_match`, then `{type:"match", trade_id, maker_order_id, taker_order_id, side (MAKER side), size, price, product_id, sequence, time}`. Messages **can be dropped**; the docs say to use heartbeat plus REST to backfill. |
| `level2_batch` | ✅ | Echo `level2_50`. `snapshot` (BTC-USD about **1.1 MB**, `[price,size]` pairs) then `l2update {changes:[[side, price, new_size]], time}`, batched every 50 ms. Size is absolute and `"0"` removes the level. |
| `status` | ✅ | A single `status` message with all products and currencies (about 740 KB). |
| `auctionfeed` | ✅ (subscribed; no messages because no auctions) | |
| `rfq_matches` | ✅ | `{type:"rfq_match", maker_order_id, taker_order_id, side:"BUY", size, price, product_id, time, trade_id}`. **A separate id space** (e.g. 138,740,126). RFQ trades are **not** CLOB prints; exclude them from fill logic. |
| `level2` | ❌ | `{"type":"error","message":"Failed to subscribe","reason":"level2, level3, and full channels now require authentication. …"}` |
| `full` | ❌ | Same error. |
| `level3` | ❌ | Same error. |

Exchange WS limits [DOCS]:
- 8 connection requests/s per IP (burst 20), and 100 client→server messages/s per IP.
- 10 subscriptions per product per channel for accounts.
- Use `Sec-WebSocket-Extensions: permessage-deflate`, and prefer the `*_batch` channels.

**Advanced Trade** `wss://advanced-trade-ws.coinbase.com`. Subscribe with
`{"type":"subscribe","product_ids":["BTC-USD"],"channel":"<ch>"}`: **one channel per message**,
and it must be sent within 5 s or the server disconnects [DOCS]. Messages are
`{channel, timestamp, sequence_num, events:[{type:"snapshot"|"update", …}]}`, and `sequence_num`
is per connection.

| Channel | Result without auth | Notes |
|---|---|---|
| `heartbeats` | ✅ | `events:[{current_time, heartbeat_counter}]` |
| `ticker` | ✅ | `tickers:[{product_id, price, volume_24_h, low_24_h, high_24_h, low_52_w, high_52_w, price_percent_chg_24_h, best_bid, best_ask, best_bid_quantity, best_ask_quantity}]` |
| `ticker_batch` | ✅ | The same fields without the best bid/ask. |
| `market_trades` | ✅ | A snapshot of recent trades (about 14 KB), then `trades:[{product_id, trade_id, price, size, time, side}]`. **`side` = MAKER side**: identical to Exchange `matches.side` on 59 of 59 shared ids. |
| `level2` | ✅ **(unauthenticated full L2!)** | Messages arrive on channel `l2_data`. The snapshot is about **4.6 MB** for BTC-USD: `updates:[{side:"bid"\|"offer", event_time, price_level, new_quantity}]`. `new_quantity` is absolute and `"0"` removes the level. |
| `candles` | ✅ | **5-minute** candles, `{start, high, low, open, close, volume, product_id}`. |
| `status` | ✅ | `products:[{product_type, id, base_currency, quote_currency, base_increment, quote_increment, display_name, status, status_message, min_market_funds}]` |
| `user` | ❌ | `{"type":"error","message":"authentication failure"}` |

AT WS limits [DOCS]: "WebSocket connections and unauthenticated messages are each limited to
**8 per second per IP**."

**If the bot moves to streaming**, one AT WS `level2` connection plus one `market_trades`
connection gives a real-time full book and tape without keys. That is the best free data source
[INFERRED].

---

## 6. Maker-fill simulation guidance

**Principle:** a resting paper order fills **only from real prints after it went live**, at its
own limit price, **after the real queue ahead of it has traded**. This is the same rule the Kalshi
paper broker uses (ARCHITECTURE §6).

### 6.1 Algorithm (per resting paper order O: side, price P, remaining R)

1. **On acceptance at time t0:**
   - Record `cursor = newest trade_id seen`. Only trades with `trade_id > cursor` and
     `time ≥ t0 + latency` count. Use a latency of 250–500 ms.
   - Take a fresh L2 snapshot.
   - If a buy has `P ≥ best_ask` (or a sell has `P ≤ best_bid`):
     - with `post_only`, reject the order;
     - otherwise the crossing part fills **now as taker**, walking levels ≤ P (and within the
       PPP), and only the rest rests.
   - Set `queue_ahead Q = L2 size at exactly P` (the Exchange L2 tuple `[price, size, num_orders]`).
     If P is a new price level, for example inside the spread, `Q = 0`.
2. **On each new print t (from `/trades`, oldest first).** The side is the **maker side**: a
   `buy` print hit a resting bid, and a `sell` print lifted a resting ask.
   - For a **buy** O, count only prints with `side == "buy"`; for a **sell** O, only `side == "sell"`.
   - `t.price == P`: first consume the queue, `Q -= t.size`. Once `Q < 0`, fill
     `min(−Q, R)` at P and set `Q = 0`.
   - `t.price` strictly **through** P (below P for a buy O, above P for a sell O): under price-time
     priority every order at P traded first, so **fill all of R at P**.
   - `t.price` worse than P (above P for a buy O, below for a sell O): no effect.
3. **On each L2 refresh:** `Q = min(Q, current L2 size at P)`.
   - Our order is not in the real book, so everything displayed at P belongs to others. The queue
     ahead cannot exceed it; the shrinkage came from cancels or fills.
   - **Never increase Q.** Size added later at P is behind us.
4. **Never fill from book movement alone.** A best bid dropping below P without prints means
   cancels, not fills.
5. **Fees:** each maker fill pays `P × size × maker_rate`. Floor fill sizes to `base_increment`.
   Fills smaller than `base_increment` are carried as residual queue credit.
6. **Exclusions:**
   - `rfq_match` prints.
   - Prints while `auction_mode` is true.
   - Prints before t0 plus latency.
   - Dust prints are harmless: 35% of the last 1000 BTC-USD prints were under $1, which was
     0.001% of notional. Use size, never print counts.
7. **Conservative extras (recommended defaults):**
   - Require one extra tick of trade-through before counting a level as exhausted. This is
     optional, for "pessimistic" mode.
   - Cap total maker fills per order per print at the print size.
   - When the tape shows a gap (a jump in trade_id that cannot be backfilled), freeze fills for
     that window.

### 6.2 What the numbers imply [LIVE p10: 60 s of Exchange WS `matches` + `level2_batch`, Sunday 16:01 UTC; illustrative only]

| Product | Taker orders/min | $ hitting bids/min | $ lifting asks/min | Takers sweeping >1 level | Median L1 bid $ | Median L1 ask $ | Median spread (bps) | L1 bid ÷ hit-rate (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| BTC-USD | 157 | 17,488 | 39,116 | 4.5% | 2,870 | 6,065 | 0.00 | 9.8 |
| ETH-USD | 46 | 9,925 | 4,345 | 15.2% | 448 | 300 | 0.04 | 2.7 |
| SOL-USD | 94 | 104,107 | 3,886 | 3.2% | 12,891 | 515 | 0.82 | 7.4 |
| XRP-USD | 76 | 46,175 | 33,445 | 5.3% | 4,453 | 3,974 | 0.66 | 5.8 |
| DOGE-USD | 30 | 10,942 | 11,469 | 3.3% | 1,036 | 614 | 2.06 | 5.7 |

- Joining the touch at BTC/SOL/XRP/DOGE means waiting behind roughly 3–13 k$ of queue, which
  clears in about 5–10 s at typical flow. That is fast.
- The real risk is **adverse selection**: the touch clears exactly when price moves through it.
  The simulator must therefore mark resting-order fills against the **post-fill** mid, not the
  fill price.
- Polling REST `/trades` every 2–5 s is adequate for fill accounting, because it is gap-free.
  Fills are simply booked with 2–6 s delay; stamp each with the **print time**, not the poll time.

---

## 7. Data history for backtests

### 7.1 Candles [LIVE p07, p08]

**Exchange** `/products/{id}/candles`:
- **Granularities** `{60, 300, 900, 3600, 21600, 86400}` only; 120, 1800, 14400 and 604800 return
  `400 {"message":"Unsupported granularity"}`.
- **Ranges:** at most **300 intervals** per request (`(end−start)/g ≤ 300`). Start and end are
  inclusive, so a 300-interval span returns **301** candles; 301 intervals →
  `400 "granularity too small for the requested time range. Count of aggregations requested exceeds 300"`.
  `start`/`end` are ISO 8601. Without start/end you get the **350** newest candles, CDN-cached for
  up to 300 s.
- **Row format:** `[time, low, high, open, close, volume]` as **floats**, newest first.
  **Buckets with no trades are omitted**: 1-minute density on 2026-09-20 was BTC 300/300, SOL
  300/300, AERO 242/300, DASH 225/300. Forward-fill closes and set volume to 0.
- The newest bucket may be in progress. Treat it as incomplete. A lag of up to about 60 s was
  observed.

**AT** `/market/products/{id}/candles`: up to 350 per request. Granularities `ONE_MINUTE`,
`FIVE_MINUTE`, `FIFTEEN_MINUTE`, `THIRTY_MINUTE`, `ONE_HOUR`, `TWO_HOUR`, `FOUR_HOUR`, `SIX_HOUR`
and `ONE_DAY` were all verified; `ONE_WEEK` worked for a 10-week span. It uses the same store:
`ONE_DAY` from 2015-07-20 and `ONE_MINUTE` in 2016 both returned data.

Earliest available candle per product and granularity (binary search over non-empty 300-bucket
windows):

| Product | Daily | Hourly | 1-minute |
|---|---|---|---|
| BTC-USD | 2015-07-20 | 2015-07-20 21:00 | **2015-01-20 18:13** (sparse until about Feb 2015: 3/300 minutes on 2015-01-21, 295/300 on 2015-02-15) |
| ETH-USD | 2016-05-18 | 2016-05-18 00:00 | 2016-05-18 00:14 |
| DOGE-USD | 2021-06-03 | 2021-06-03 16:00 | 2021-06-03 16:10 |
| SOL-USD | 2021-06-17 | 2021-06-17 16:00 | 2021-06-17 16:08 |
| HYPE-USD | 2026-02-05 | 2026-02-05 19:00 | 2026-02-05 19:24 |

Note: BTC-USD daily and hourly candles **start 2015-07-20**, even though 1-minute candles and the
trade tape go back to January 2015.

### 7.2 Download cost

**Candles:**
- 1-minute bars: one request covers 300 minutes, so a year is **1,753 requests**. At 3 req/s that
  is about 10 minutes per product-year.
- Hourly bars for all of BTC history (about 11.2 years) take about 330 requests.
- Store as compressed parquet. A year of 1-minute OHLCV is about 525k rows, a few MB.

### 7.3 Tick history (for maker-fill backtests)

- `/products/{id}/trades?after=<id>` pages back to the beginning: BTC-USD `trade_id` 999 is dated
  2015-01-08T08:23Z, 10,000,000 is 2016-07-07, 100,000,000 is 2020-08-14 and 500,000,000 is
  2023-02-17 [LIVE p05].
- BTC-USD printed about **230 trades/min** on this Sunday afternoon, about 330k a day, which is
  about 330 requests of 1000.
- **A week of BTC ticks costs about 2,300 requests (~15 min at 2.5 req/s) and about 2.3M rows.**
  That is fine for targeted maker-fill validation, but not for multi-year tick studies.
- There is **no historical order book** from Coinbase: L2 exists only live. Queue-position
  backtests therefore need live-recorded L2 (Exchange `level2_batch` or AT `level2` over WS) or a
  proxy, such as queue = displayed size at P, drawn from the live distribution in §6.2.

---

## 8. Probe scripts (reproducible, ≤ 2.5 req/s, no auth)

All scripts live in `research/coinbase/api_probe/` (about 76 KB). Run each with
`uv run --with httpx --with websockets python pNN_*.py`.

| Script | What it verifies |
|---|---|
| `cbprobe.py` | Shared rate-limited GET (0.4 s gap, 429 backoff) |
| `p01_exchange_rest.py` | Exchange products, flags, book L1/L2/L3, ticker, stats, `/products/stats`, volume-summary, trades cursors, candle granularities and the 300 limit |
| `p02_advanced_trade_rest.py` | AT products (fields, aliases, filters), product_book limits, ticker, candles (350 limit), auth-only 401s |
| `p03_freshness.py` | Book/ticker staleness on both APIs; candle cache lag |
| `p04_ws.py` | Unauthenticated subscribe to every Exchange and AT channel; ticker vs matches side |
| `p05_trades_semantics.py` | REST side equality across APIs; the `before`/`after` quirk; tape depth to 2015; 401 on fee endpoints |
| `p06_liquidity.py` → `liquidity_snapshot.csv` | Top 25 USD pairs: volume, spread, ±5/±10 bps depth |
| `p07_history.py`, `p08_early_btc.py` | Earliest candles; 1-minute gap density; AT candle history; no-cache header |
| `p09_misc.py` | Stable pairs, `limit_only` list, USDC aliasing, AT size limits, dust prints |
| `p10_ws_microstructure.py` | 60 s WS capture: taker flow, sweep share, L1 queue, time to clear |
| `p11_limits.py` | AT ticker 100-trade cap; error shapes |
| `p12_trades_freshness.py` | `/trades` CDN staleness vs `/ticker` vs AT |
| `p13_at_ws_side.py` | AT WS `market_trades.side` equals Exchange `matches.side` (maker side) |

---

## 9. Open questions

1. **Advanced 1 to VIP 7 retail rates after 2026-09-16.** Only Intro (US 0.50%/0.90%) and VIP 8
   (0/2 bps) are published. The user can read their tier and rates at
   coinbase.com/advanced-portfolio?tab=fees and put them in config.
2. **AT public REST rate limit.** It is not on the current docs pages. Assume ≤ 10 req/s per IP
   and stay ≤ 5.
3. **AT retail fee rounding.** The Exchange example shows exact (unrounded) fees. It is not
   verified whether retail AT ledgers round `commission` to cents.
4. **Exchange error text for off-grid size or price** (e.g. "size is too accurate"). This cannot
   be tested without keys; the AT failure-reason enum is documented instead.
