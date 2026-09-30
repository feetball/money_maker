# Kalshi API and Market Rules Reference (for the paper-trading bot)

Status: verified 2026-09-26 (about 22:30–23:10 UTC) against the live production API with curl, the official docs at docs.kalshi.com (OpenAPI spec v3.31.0, AsyncAPI spec, API changelog through Oct 1, 2026), and the Kalshi fee schedule PDF (Feb 5, 2026 edition from the Wayback Machine). Kalshi's bot checkpoint blocked the July 7, 2026 PDF edition, so it could not be read directly.

Every claim is tagged:
- **[LIVE]**: checked by me against the production API on 2026-09-26.
- **[DOCS]**: stated in official Kalshi docs or specs.
- **[PDF-Feb26]**: stated in the Feb 5, 2026 fee schedule PDF.
- **[3P]**: third-party summary only, not confirmed.
- **[INFERRED]**: my reasoning from the above.

---

## 0. TL;DR for engineers

| Topic | Rule |
|---|---|
| REST base (prod) | `https://api.elections.kalshi.com/trade-api/v2`. Recommended alias: `https://external-api.kalshi.com/trade-api/v2`. Both serve all markets. [DOCS][LIVE] |
| REST base (demo) | `https://demo-api.kalshi.co/trade-api/v2` or `https://external-api.demo.kalshi.co/trade-api/v2` [DOCS] |
| Auth for market data | **None** for markets, events, series, orderbook(s), trades, candlesticks, historical/*, exchange/*, and fee changes. [LIVE] |
| WebSocket | **Always requires API-key auth.** An unauthenticated handshake returns HTTP 401 `token_authentication_failure`. [LIVE] **A key-less paper bot must poll REST.** |
| Unauth rate limit | Undocumented and per-IP. At about 4–5 req/s sequential from this box (with other agents sharing the IP) I got about 1 in 20 `429`s. Budget ≤ 3 req/s per process and use batch endpoints. [LIVE] |
| Prices | `*_dollars` strings with up to 4 dp (responses may emit up to 6 dp). Never use floats for money; use `Decimal` or integer "centi-cents" (×10 000). [DOCS][LIVE] |
| Sizes | `*_fp` strings with 2 dp. Fractional contracts are live, with a minimum of 0.01. 70% of recent BTC15M trades were fractional. [DOCS][LIVE] |
| Tick size | Read per market from `price_ranges[{start,end,step}]`. Do NOT key logic off `price_level_structure`. [DOCS][LIVE] |
| Orderbook | Bids only, per side, sorted **ascending**, so the best bid is the **last** element. YES ask = 1 − best NO bid. NO ask = 1 − best YES bid. [DOCS][LIVE] |
| Taker fee | `M × 0.07 × C × P × (1−P)`. The maximum is 1.75¢ per contract at P = 0.50 when M = 1. [PDF-Feb26][DOCS] |
| Maker fee | `0` for `fee_type=quadratic` (98.6% of series). `0.25 × taker` for `quadratic_with_maker_fees`. `0.5 × taker` for `quadratic_with_combo_maker_fees`. [DOCS][LIVE] |
| Fee rounding | Direct kalshi.com members: a per-order net balance change aligned to **$0.0001**. Non-direct (FCM or broker) members: aligned to **$0.01**. Conservative legacy rule: ceil to the cent per order. See §1.3. |
| Settlement | YES pays `settlement_value_dollars` per contract and NO pays `1 − settlement_value_dollars`. Result values are `yes`, `no`, or `scalar` (void or fair-price, about 0.7% of settled markets). There is no settlement fee apart from sub-precision rounding on scalar results. [LIVE][DOCS] |
| Always filter | `GET /markets?...&mve_filter=exclude` drops the `KXMVE*` combo markets. [LIVE] |

---

## 1. FEES

### 1.1 Fee formulas

Symbols:
- `C` = number of contracts in the fill. It may be fractional in 0.01 steps.
- `P` = execution price in dollars. Because `P(1−P)` is symmetric, it does not matter whether you use the YES or the NO price.
- `M` = the effective **fee multiplier**.

```
taker_fee_raw = M × 0.07 × C × P × (1 − P)
maker_fee_raw = M × k_maker × 0.07 × C × P × (1 − P)
    k_maker = 0     for fee_type "quadratic"                        (maker orders are free)
    k_maker = 0.25  for fee_type "quadratic_with_maker_fees"        (0.0175 × C × P(1−P))
    k_maker = 0.5   for fee_type "quadratic_with_combo_maker_fees"  (0.035 × C × P(1−P))
"flat" fee_type = "Specific Trading Fees Table" = 0.035 × C × P(1−P) (taker).  UNUSED: 0 of 14,393 series.
```

Sources:
- [PDF-Feb26]: "fees = round up(0.07 x C x P x (1-P))". Maker: "fees = round up(0.0175 x C x P x (1-P))". INX/NASDAQ100: "round up(0.035 x C x P x (1-P))".
- [DOCS] (OpenAPI `Series.fee_type`): "'quadratic' is described by the General Trading Fees Table, 'quadratic_with_maker_fees' is described by the General Trading Fees Table with maker fees described in the Maker Fees section, 'quadratic_with_combo_maker_fees' is the same maker-fee structure with a 0.5 maker multiplier instead of 0.25, 'flat' is described by the Specific Trading Fees Table."
- [DOCS] changelog, Aug 22, 2026: "The maker fee uses a fee multiplier of `0.5`, rather than the standard `0.25`."

**Effective M and fee_type.** Take the **event override** if one is present, otherwise the series value:
```
fee_type = event.fee_type_override       ?? series.fee_type
M        = event.fee_multiplier_override ?? series.fee_multiplier
```
- [DOCS] Event: "When present, takes precedence over the series-level fee for this event's markets."
- [INFERRED] M multiplies the maker fee as well as the taker fee. This is unverified for the single live case where it matters, KXMLBGAME with M = 0.5.

**Caps.** There is no explicit cap. The formula's maximum is `0.07 × 0.25 = $0.0175` per contract at P = 0.50 with M = 1, which is $1.75 per 100 contracts. At the extremes it is tiny: at P = 0.01 or 0.99 it is $0.000693 per contract before rounding.

**Who pays maker versus taker.**
- The part of an order that immediately matches resting liquidity is the taker part. The remainder rests and later fills as maker. [PDF-Feb26]
- Cancelling a resting order is free. [PDF-Feb26]
- Combo RFQs have a special case (since 2026-08-21). If an RFQ quoter executes against an order that has rested for less than 5 s, the fees swap: the quoter pays the maker fee and the resting party pays the taker fee. [DOCS changelog]

**Other fees.** [PDF-Feb26][DOCS]
- No settlement fee on yes/no results. Scalar results incur a tiny rounding "fee"; see §3.4.
- No membership fee. ACH deposits and withdrawals are free. Wire deposits are free. Debit-card deposits cost up to 2%.

**Fee waivers.** Markets have a nullable `fee_waiver_expiration_time` field. It was `null` on all 33,000 markets I sampled. [LIVE]

### 1.2 Live fee landscape (GET /series, 14,393 series, 2026-09-26) [LIVE]

| fee_type | fee_multiplier | # series | Notes |
|---|---|---|---|
| quadratic | 1 | 14,198 | Taker 7% formula; **makers pay 0**. Includes KXBTC15M, KXBTCD, KXBTC, KXETH*, all KXHIGH* weather series, KXINX, KXINXU, KXNASDAQ100, KXNASDAQ100U, and nearly every MVE combo series. |
| quadratic_with_maker_fees | 1 | 159 | Major sports game/spread/total series: NBA, NFL, NCAAF/NCAAMB, NHL, WNBA, ATP/WTA, soccer leagues, PGA, F1, NASCAR, KXMENWORLDCUP, KXWCGAME. Also KXFEDDECISION, KXFED, KXCPIYOY, KXRATECUTCOUNT, KXAAAGASM, KXINXY, KXNASDAQ100Y, KXLLM1, some KXBTCMAX*, and GPU-price series (added 2026-09-03). |
| quadratic_with_maker_fees | 0.5 | 1 | KXMLBGAME |
| quadratic | 0.5 | 18 | MLB props and derivatives: KXMLBTOTAL, KXMLBSPREAD, KXMLBF5, KXMLBHR, KXMLBKS, KXMLBRFI, and others. |
| quadratic | 0 | 14 | **Zero fees**: KXBTCY, KXETHY, KXTRUMPOUT, KXGREENLAND, KXDOED, KXEXPAND, KXGAMBLINGREPEAL, KXCITRINI, KXLAYOFFSYINFO, KXNEXTIRANLEADER, KXIRANDEMOCRACY, KXPAHLAVIHEAD, KXELECTIRAN, KXGDPYEAR |
| quadratic_with_combo_maker_fees | 1 | 3 | KXMVECROSSCATEGORY, KXMVECROSSCATEGORY-SHARD1, KXMVESPORTSMULTIGAMEEXTENDED (maker fees enabled 2026-08-20) |
| flat | – | 0 | – |

**MLB fee schedule quirk** [LIVE]:
- The MLB series carry M = 0.5. `GET /events/fee_changes` lists a per-event override for every upcoming MLB event, set to M = 1 at the scheduled first pitch.
  - Example: `KXMLBGAME-26SEP271510CLEKC` gets `quadratic_with_maker_fees ×1` at `2026-09-27T19:10:00Z`, which is 3:10 PM ET.
  - Example: `KXMLBTOTAL-...` gets `quadratic ×1`.
- **Pre-game MLB trading pays half fees and in-game trading pays full fees.**
- Once an override is active, it appears on `GET /events/{event_ticker}` as `fee_type_override` and `fee_multiplier_override`. For example, `KXMLBGAME-26SEP261610TEXMIN` showed `quadratic_with_maker_fees`, `1` after its start time.

**The S&P and Nasdaq half-rate is gone** [LIVE]:
- The Feb 2026 PDF charged INX/NASDAQ100 products `0.035`.
- `GET /series/fee_changes?show_historical=true` shows KXINX, KXINXU, KXNASDAQ100 and KXNASDAQ100U switched to `quadratic ×1` at `2026-07-03T17:00Z`, and KXINXY and KXNASDAQ100Y switched to `quadratic_with_maker_fees ×1`.
- The API is authoritative; the Feb PDF is stale on this point.
- A third-party summary of the "July 7, 2026" schedule says the formula is written as `round up(M × 0.07 × C × P × (1−P))` [3P].

**fee_type values seen anywhere:**
- The OpenAPI enum lists: `quadratic`, `quadratic_with_maker_fees`, `quadratic_with_combo_maker_fees`, `flat`.
- `GET /series/fee_changes` also returns `margin_market_maker_program_fees` (M = 0) for perps series (`KX*PERP`), which is not in the enum. Perps are out of scope, but **the parser must tolerate unknown fee types.** [LIVE]

### 1.3 Rounding rules (exact)

There are two regimes. Both are documented officially.

**(a) Legacy / fee-schedule rule** [PDF-Feb26]
- "round up = rounds to the next cent". `C` is "the number of contracts being traded", i.e. the whole order execution.
- My implementation reproduces **every row** of the PDF's official tables with 0 mismatches: 21 general rows at 1 and 100 contracts, plus the INX table.
  - Example: 100 contracts at $0.05 give 0.3325, which rounds to $0.34.
  - Example: 1 contract at $0.50 gives 0.0175, which rounds to $0.02.
- Maker-fee rounding overpayment above $10 per month is reimbursed the following month.

**(b) Current exchange mechanics** [DOCS "Fee Rounding", "Fixed-Point Representation", and the May 28, 2026 changelog]

Direct-member balances have been aligned to `$0.0001` since 2026-05-28. Non-direct balances (FCM-cleared, e.g. brokers) are aligned to `$0.01`. Per fill, where `revenue` is signed (buyer = −C·P):
```
trade_fee     = ceil_6dp(model_fee)                       # $0.000001 granularity
aligned_change= floor_to_precision(revenue - trade_fee)    # precision = 0.0001 (direct) or 0.01 (non-direct)
rounding_fee  = (revenue - trade_fee) - aligned_change
accumulator  += rounding_fee          # per ORDER, across taker and maker fills
rebate        = accumulator rounded down to precision, capped so the fill's net fee >= 0
net_fee       = trade_fee + rounding_fee - rebate          # always >= 0
```
- "The fee accumulator applies across all fills of an order so that the total fee converges to what a single equivalent fill would cost."
- The third-party summary of the July 2026 schedule [3P] agrees: "round up such that fee + position cost is rounded to a centicent ($0.0001)".
- **Practical meaning.** For a normal kalshi.com account (a direct member), the fee on an order is effectively `ceil_to_$0.0001(Σ model fees of the order's fills)`. Examples:
  - 1 contract at 50¢ costs $0.0175, not $0.02.
  - 10 contracts at 50¢ cost $0.175, not $0.18.

**Recommendation for the simulator:**
- Implement (b) with a `balance_precision` parameter. Default it to `0.0001` for realism.
- Also provide a `conservative=True` mode that applies regime (a), ceil to the cent per order, as a stress test.
- The two modes differ only on small orders. For strategies trading below about 20 contracts, always report P&L under both.

**Sub-penny prices and fractional contracts:**
- Principal `C × P` can need up to 6 decimals, e.g. 0.01 contracts × a $0.0001 tick.
- Because the balance change is **floored** to your precision, any sub-precision part of the principal also ends up in the rounding fee.
  - Example: a direct member buys 0.5 YES at $0.005. The cost is $0.0025, the fee is $0.000174 after ceil_6dp, and the balance changes by −$0.0027. The effective fee is $0.0002.
  - Example: a non-direct member makes the same trade. The balance changes by −$0.01, so the effective fee is $0.0075, three times the principal.
- **Practical meaning:** tiny fractional clips at sub-cent prices are fee-inefficient for cent-precision accounts.

### 1.4 Worked examples (computed with the exact algorithm above; buyer side)

Columns:
- "@1¢" = non-direct or conservative (balance aligned to $0.01).
- "@0.01¢" = direct member (aligned to $0.0001).
- MLB pre-game = M = 0.5.

| P | C | taker raw | taker @1¢ | taker @0.01¢ | maker (k=0.25) raw | maker @1¢ | maker @0.01¢ | MLB pre-game taker @0.01¢ |
|---|---|---|---|---|---|---|---|---|
| 0.01 | 1 | 0.000693 | 0.01 | 0.0007 | 0.000173 | 0.01 | 0.0002 | 0.0004 |
| 0.01 | 100 | 0.0693 | 0.07 | 0.0693 | 0.017325 | 0.02 | 0.0174 | 0.0347 |
| 0.05 | 1 | 0.003325 | 0.01 | 0.0034 | 0.000831 | 0.01 | 0.0009 | 0.0017 |
| 0.05 | 100 | 0.3325 | 0.34 | 0.3325 | 0.083125 | 0.09 | 0.0832 | 0.1663 |
| 0.10 | 10 | 0.063 | 0.07 | 0.0630 | 0.01575 | 0.02 | 0.0158 | 0.0315 |
| 0.25 | 1 | 0.013125 | 0.02 | 0.0132 | 0.003281 | 0.01 | 0.0033 | 0.0066 |
| 0.25 | 100 | 1.3125 | 1.32 | 1.3125 | 0.328125 | 0.33 | 0.3282 | 0.6563 |
| 0.50 | 1 | 0.0175 | 0.02 | 0.0175 | 0.004375 | 0.01 | 0.0044 | 0.0088 |
| 0.50 | 10 | 0.175 | 0.18 | 0.1750 | 0.04375 | 0.05 | 0.0438 | 0.0875 |
| 0.50 | 100 | 1.75 | 1.75 | 1.7500 | 0.4375 | 0.44 | 0.4375 | 0.8750 |
| 0.50 | 0.5 | 0.00875 | 0.01 | 0.0088 | 0.002188 | 0.01 | 0.0022 | 0.0044 |
| 0.90 | 100 | 0.63 | 0.63 | 0.6300 | 0.1575 | 0.16 | 0.1575 | 0.3150 |
| 0.99 | 100 | 0.0693 | 0.07 | 0.0693 | 0.017325 | 0.02 | 0.0174 | 0.0347 |
| 0.005 | 100 | 0.034825 | 0.04 | 0.0349 | 0.008706 | 0.01 | 0.0088 | 0.0175 |
| 0.999 | 100 | 0.006993 | 0.01 | 0.0070 | 0.001748 | 0.01 | 0.0018 | 0.0035 |

Reading the table:
- For a quadratic (no-maker-fee) series, **maker fills cost $0**.
- At 50¢ a taker round trip (buy, then sell) costs about 3.5¢ per contract. At 10¢ or 90¢ it costs about 1.26¢. The edge needed to overcome fees is price-dependent.

### 1.5 Reference implementation (Python, Decimal)

```python
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR

def _ceil(x, step):  return (x / step).to_integral_value(rounding=ROUND_CEILING) * step
def _floor(x, step): return (x / step).to_integral_value(rounding=ROUND_FLOOR) * step

MAKER_K = {"quadratic": D(0), "quadratic_with_maker_fees": D("0.25"),
           "quadratic_with_combo_maker_fees": D("0.5")}

def model_fee(C, P, fee_type, M, is_maker):
    C, P, M = D(str(C)), D(str(P)), D(str(M))
    if fee_type == "flat":                       # legacy "specific table"; unused today
        base = D("0.035")
    else:
        base = D("0.07")
    k = MAKER_K.get(fee_type, D("0.25")) if is_maker else D(1)   # unknown type -> assume maker fees (conservative)
    return M * k * base * C * P * (1 - P)

def order_fee(fills, fee_type, M, precision=D("0.0001"), conservative=False):
    """fills: list of (C, P, is_maker, side_sign) for ONE order; side_sign=-1 buyer (pays), +1 seller.
    Returns total fee charged to the order (>=0), consistent with Kalshi's per-order accumulator."""
    raw = sum(model_fee(C, P, fee_type, M, mk) for C, P, mk, _ in fills)
    if conservative:                                         # regime (a): ceil to cent per order
        return _ceil(raw, D("0.01"))
    trade_fee = sum(_ceil(model_fee(C, P, fee_type, M, mk), D("0.000001")) for C, P, mk, _ in fills)
    revenue = sum(s * D(str(C)) * D(str(P)) for C, P, _, s in fills)
    aligned = _floor(revenue - trade_fee, precision)
    return (revenue - aligned)  # = trade_fee + rounding (incl. sub-precision principal), accumulator-netted
```

---

## 2. Order-book semantics and everything a paper-fill simulator needs

### 2.1 Orderbook endpoints [LIVE]

`GET /markets/{ticker}/orderbook?depth=N`
- `depth` = 0 or omitted means all levels; 1–100 means the best N levels per side.
- Response:
  ```json
  {"orderbook_fp":{"yes_dollars":[["0.7200","1106.50"],...,["0.7600","136.62"]],
                   "no_dollars":[["0.1900","2059.42"],...,["0.2300","3459.10"]]}}
  ```
  - Each level is `[price_dollars, size_fp]`, aggregated by price.
  - Levels are sorted **ascending**, even when `depth` truncates; the best N are kept.
  - An empty side is `[]`, e.g. a closed market returns `{"no_dollars":[],"yes_dollars":[]}`.
- The response has **no per-level order counts**, despite the endpoint description.
- It has no CDN cache headers, so it is fresh.

`GET /markets/orderbooks?tickers=A&tickers=B&...` (batch, max 100)
- Works **unauthenticated**, even though the spec lists a security scheme. [LIVE]
- Tickers must be **repeated `tickers=` params**. A comma-joined value returned only 1 book. [LIVE]
- Response: `{"orderbooks":[{"ticker":"…","orderbook_fp":{…}}]}`, full depth. This is the best polling primitive.

### 2.2 Converting the book

- A YES bid at x is a NO ask at 1 − x. A NO bid at y is a YES ask at 1 − y. [DOCS]
- Best YES bid = `yes_dollars[-1]`. Best YES ask = `1 − no_dollars[-1].price`, with size = `no_dollars[-1].size`.
- **Buying YES with limit L:** walk `no_dollars` from the last element backwards while `1 − no_price ≤ L`. Each level fills at YES price `1 − no_price`, up to its size.
- **Buying NO with limit L:** walk `yes_dollars` from the back while `1 − yes_price ≤ L`.
- "Selling YES" is the same as buying NO at `1 − price`. "Selling NO" is the same as buying YES. [DOCS order_direction]
  - V2 orders use `side: bid` (buy YES) or `side: ask` (sell YES / buy NO), with a YES-denominated `price`.

Market snapshot fields (from `GET /markets` or `/markets/{t}`):
- `yes_bid_dollars`, `yes_ask_dollars`, `no_bid_dollars`, `no_ask_dollars`, `yes_bid_size_fp`, `yes_ask_size_fp`, `last_price_dollars`.
- **Sentinels:** no bid is shown as `"0.0000"` and no ask as `"1.0000"`. [LIVE, closed BTC15M market]
- `/markets` list responses carry `cache-control: public, max-age=15` from CloudFront, so quotes can be **up to 15 s stale.** [LIVE] Use the orderbook endpoints for fills.

### 2.3 Price grid (ticks) [DOCS][LIVE]

`price_ranges` is the source of truth: any on-grid price is valid and off-grid prices are rejected. Snap to the `step` of the band that contains the price. Whole-cent prices are valid in every structure. [DOCS]

| price_level_structure | bands (start–end: step) | Live usage (120,000 open non-MVE markets sampled) |
|---|---|---|
| linear_cent | 0–1: 0.01 | 113,930 (94.9%) |
| tapered_deci_cent | 0–0.10: 0.001; 0.10–0.90: 0.01; 0.90–1: 0.001 | 5,708 (all `*15M` crypto: KXBTC15M, KXETH15M, KXSOL15M, …) |
| center_centi_edge_centi_cent | 0–1: 0.0001 | 207 (e.g. KXNFLESCALATOR*) |
| deci_cent | 0–1: 0.001 | 116 |
| center_half_edge_half_cent | 0–1: 0.005 | 39 |
| center_deci_edge_centi_cent | 0–0.01: 0.0001; 0.01–0.99: 0.001; 0.99–1: 0.0001 | all KXMVE* combos |
| others documented, not seen live | deci/half/quint/centi combinations (see the docs table) | 0 |

- Valid order prices are strictly between 0 and 1: [min tick, 1 − min tick].
- A structure can change during a market's life (`price_level_structure_updated` lifecycle event) [DOCS]. Re-read `price_ranges` before each order.

### 2.4 Quantities [DOCS][LIVE]

- `count` is a string with 0–2 dp. The minimum granularity is **0.01 contracts** and responses always show 2 dp.
- Fractional trading is effectively universal: the `fractional_trading_enabled` field no longer appears on market objects [LIVE].
- For integer maths, use hundredths of a contract (`"1.55"` becomes 155) and centi-cents for price (`"0.0010"` becomes 10).
- Position limits exist per contract rules, but are irrelevant at paper sizes.

### 2.5 Order types and flags (V2: `POST /portfolio/events/orders`) [DOCS]

| Field | Values and semantics |
|---|---|
| side | `bid` (buy YES) \| `ask` (sell YES = buy NO) |
| price | YES price, fixed-point dollars, on the grid. **Every order is a limit order.** A "market order" is an IOC/FOK at an aggressive price. `Order.type` still reports `limit` or `market`. The legacy `buy_max_cost` (cents) forces FOK. |
| count | fixed-point string |
| time_in_force (required) | `fill_or_kill`, `good_till_canceled`, `immediate_or_cancel`. GTC + `expiration_time` (unix seconds) gives a good-till-time order. IOC cannot take `expiration_time`. |
| post_only | Rejected if it would take liquidity. It is not repriced. |
| reduce_only | Caps the size at the current position. **Only allowed with IOC.** |
| self_trade_prevention_type (required) | `taker_at_cross`: cancel the incoming order when it would hit your own resting order; fills already made stand. `maker`: cancel your resting order and keep matching. |
| cancel_order_on_pause | If true, the order is cancelled when a trading or exchange pause starts. By default it stays resting. |
| order_group_id | Order groups cap total fills; triggers cancel the whole group. |
| client_order_id, subaccount (0–63), exchange_index | Omit `exchange_index`: it auto-routes by ticker. |

Response fields: `fill_count`, `remaining_count`, `average_fill_price`, `average_fee_paid`, `ts_ms`.

Matching:
- **Price-time priority** [DOCS, queue-position endpoint].
- Trades print at the resting (maker) order's price [INFERRED, standard CLOB behaviour]. Public trades carry `yes_price_dollars` and `no_price_dollars` = 1 − yes.
- Amending keeps queue position **only if it decreases size**. Any price change or size increase sends the order to the back of the queue. [DOCS]

Trading-state rules:
- After `close_time`, every order operation, including cancels, is rejected with `MARKET_INACTIVE`. Resting orders are cancelled shortly after close. [DOCS]
- When a paused market (`inactive`) is re-activated, **all resting orders are cancelled.** [DOCS]
- Scheduled maintenance runs **every Thursday 3:00–5:00 AM ET** as a trading pause: no new orders or amends, but cancels are allowed. [DOCS][LIVE `GET /exchange/schedule`] Rare exchange pauses block cancels as well.
- Check `GET /exchange/status`, whose `trading_active` also appears per shard in `exchange_index_statuses`.

### 2.6 Positions and collateral [DOCS]

- Contracts are fully collateralized:
  - Buying YES at p locks p per contract.
  - Buying NO at q locks q.
  - Holding both YES and NO in the same market nets out: `position_fp` is signed, + for YES and − for NO.
  - Buying NO while long YES is a sale of YES.
- Events have `collateral_return_type`:
  - `MECNET`: mutually exclusive markets, and `mutually_exclusive=true`.
  - `DIRECNET`: directional netting.
  - `""`: none.
- Netting frees collateral across an event's markets when enabled for the account. The paper bot can ignore this at first; it is a capital-efficiency detail.

### 2.7 Recommended paper-fill model [INFERRED; conservative]

1. **Taker (IOC/FOK) orders**
   - Just before simulating, fetch a fresh book with `/markets/orderbooks`, not `/markets`, because the latter is CDN-cached for 15 s.
   - Walk the opposite side as in §2.2, never exceeding displayed size.
   - Add a latency haircut: re-price with the next poll's book, or skip if the book moved.
   - Charge the taker fee on each fill; then apply order-level rounding (§1.3).
2. **Maker (resting) orders**
   - On placement, record `queue_ahead` = the displayed size at your price level. If your price improves the best level, `queue_ahead` = 0.
   - Then consume `GET /markets/trades?ticker=…&min_ts=…`:
     - **YES bid at b** (`side=bid`):
       - A trade with `taker_book_side == "ask"` and `yes_price < b` fully fills you, because the market traded through your level.
       - A trade at `yes_price == b` first decrements `queue_ahead`; any excess fills you.
     - **YES ask at a** (a NO bid at 1 − a):
       - A trade with `taker_book_side == "bid"` and `yes_price > a` fills you.
       - A trade at `== a` consumes the queue first.
   - Charge maker fees only for maker-fee series.
   - Cancel all resting paper orders at `close_time`, on status `inactive`, and during Thursday pauses if `cancel_order_on_pause` is set.
3. **Taker and maker parts of one order**
   - Split an order that is partly marketable into its taker and resting parts.
   - The per-order fee accumulator spans both parts.
4. **Balance precision**
   - Keep all money in `Decimal`.
   - Store the ledger at $0.000001 and align balances to `balance_precision` (default $0.0001).

---

## 3. Market lifecycle and settlement

### 3.1 Statuses [DOCS; all seen LIVE except disputed and amended]

| REST `status` | Meaning | `GET /markets?status=` filter value |
|---|---|---|
| initialized | Created; `open_time` has not arrived yet | `unopened` |
| active | Trading | `open` |
| inactive | Paused by the exchange (e.g. an MLB player prop mid-game: KXMLBRBI-…STLMWINN0-1 was seen) | `paused` |
| closed | Past `close_time`, awaiting determination | `closed` (covers every non-finalized market past close) |
| determined | `result` set; the settlement timer is running | `closed` |
| disputed | Result challenged | `closed` |
| amended | Re-determined; the timer restarts | `closed` |
| finalized | Paid out (terminal). **There is no `settled` status in REST.** | `settled` |

Live check on `status=closed`, first 1,000 results: 610 `closed` (result `""`) and 390 `determined` (262 no, 128 yes).

### 3.2 Transitions and times [DOCS]

Transitions:
- `initialized → active` happens implicitly at `open_time`, with no WS event.
- `active → closed` happens at `close_time`.
- A market can be reopened by moving `close_time` later.

Time fields:
- `close_time`: when trading stops.
- `expected_expiration_time`: when the outcome is expected to be known. It can be **before** `close_time`, because sports markets set a distant `close_time` to allow for reschedules.
- `latest_expiration_time`: the hard deadline.
- `expiration_time`: deprecated; for example KXMLBGAME uses latest = close = start + 3 days.
- `occurrence_datetime`, `settlement_timer_seconds`, and `settlement_ts` (set when finalized) complete the set.

`can_close_early` and `early_close_condition` (e.g. "This market will close and expire after a winner is declared."):
- The exchange can move `close_time` earlier once the outcome is known. It emits a `close_date_updated` WS event.
- **All 25,000 recently settled markets had `can_close_early=true`, and 24,982 of them closed before `latest_expiration_time`.** [LIVE]
- For a KXMLBGAME game: `close_time` became 19:50:27Z, 15 minutes before `expected_expiration_time`, and it was finalized 3 minutes later (timer 120 s).
- **The bot must never assume it can trade until the scheduled `close_time`.** When the result becomes known, the market can close within seconds.

Settlement delay, `settlement_ts − close_time`, over 25,000 recent markets [LIVE]:
- min 6 s, p10 136 s, **median 359 s**, p90 3,798 s, max 19.3 h.
- KXBTC15M settles about 6 s after close (timer = 1 s).
- `settlement_timer_seconds` distribution: 60 s (31%), 300 s (27%), 3600 s (12%), 1800 s (11%), 180 s (11%), 1 s (4%).

### 3.3 Result values [DOCS][LIVE]

`result ∈ {"yes", "no", "scalar", ""}`. `market_type ∈ {binary, scalar}`; all sampled markets were `binary`.

In 25,000 recently finalized non-MVE markets: `no` 18,873 (75.5%), `yes` 5,951 (23.8%), **`scalar` 176 (0.7%)**.

**`scalar` on a binary market is Kalshi's "void, settle at fair price"** [LIVE]:
- Seen on cancelled or no-play player props (KXMLBHRR, KXMLBTB, KXMLBHIT with `expiration_value:"Cancelled"`), tennis walkovers ("Match NP"), and CS2 maps.
- `settlement_value_dollars` is an arbitrary price, e.g. 0.0100, 0.3700 or 0.5000. It is often close to `last_price`, but not always: one case had last 0.00 and settlement 0.79.
- Rule text, e.g. KXMLBGAME: "If the game is cancelled or rescheduled to over two days away, the market will resolve to a fair price in accordance with the rules."
- There is **no** `void` result string.

### 3.4 Payout [DOCS][INFERRED]

```
payout_yes_per_contract = settlement_value_dollars          # "1.0000", "0.0000", or fair price for scalar
payout_no_per_contract  = 1 − settlement_value_dollars
```
- Only net positions are settled.
- There is no settlement fee on yes/no results.
- On scalar results, the resulting available balance is rounded **down** to the member's precision ($0.0001 direct). The residual is reported as a settlement fee.
  - Docs example: 100.01 × $0.3060 = $30.60306, which pays out $30.6030 with a $0.00006 fee.

### 3.5 Detecting settlement via REST

Poll `GET /markets?tickers=A,B,…` (comma-separated) or `GET /markets/{ticker}`.
- `status == "determined"` means the result is known; `result` and `settlement_value_dollars` are set and `settlement_ts` is null.
- `status == "finalized"` means paid, and `settlement_ts` is set.
- For P&L you can mark to `settlement_value_dollars` at `determined`. For cash, book the payout at `finalized`.
- `GET /markets?status=settled&min_settled_ts=…&mve_filter=exclude` lists newly finalized markets.
- After the historical cutoff moves past `settlement_ts`, live `GET /markets/{t}` returns **404** and you must use `/historical/markets/{t}`. [LIVE: `KXHIGHNY-26FEB10-B36.5` gave 404 live and 200 historical]
- Authenticated alternatives: `GET /portfolio/settlements`, and the `market_lifecycle_v2` WS events `determined` (which carry `settlement_value`) and `settled`.

---

## 4. Read endpoints for the bot (all unauthenticated, verified LIVE unless noted)

### 4.1 Pagination [DOCS][LIVE]

- Pagination is cursor-based. Pass `cursor` from the previous response and stop when it is `""` or null.
- Limits:
  - `/markets`, `/markets/trades`, `/historical/markets`, `/historical/trades`: max **1000** (default 100).
  - `/events`: max **200** (default 200).
  - `/events/multivariate`: max 200.
  - `/series`: unpaginated. It returned all 14,393 series in one 18 MB response; filter with `category`, `tags`, or `min_updated_ts`.
- Scale:
  - `GET /markets?status=open&mve_filter=exclude` has **more than 120,000** open markets. I stopped after 120 pages of 1000 with the cursor still non-empty.
  - Do not full-scan on every loop. Filter by `series_ticker`, `event_ticker`, `min_close_ts`/`max_close_ts`, or use `min_updated_ts`.

### 4.2 Markets

`GET /markets` parameters:
- `limit`, `cursor`
- `event_ticker` (one only)
- `series_ticker`
- `tickers` (comma-separated)
- `status ∈ {unopened, open, paused, closed, settled}` (one only)
- `mve_filter ∈ {only, exclude}`
- `min_created_ts`/`max_created_ts`, `min_close_ts`/`max_close_ts`, `min_settled_ts`/`max_settled_ts`, `min_updated_ts`/`max_updated_ts` (unix seconds)

**Filter compatibility** [DOCS]:
- created_ts works with `unopened`, `open`, or empty status.
- close_ts works with `closed` or empty.
- settled_ts works with `settled` or empty.
- updated_ts works only alone, plus `mve_filter=exclude` (plus `series_ticker`, which requires `mve_filter=exclude`). It tracks **non-trading metadata changes only**.
- Timestamp families are mutually exclusive.

`GET /markets/{ticker}` returns a single market.

Market object keys [LIVE]:
- `can_close_early`, `cap_strike`, `close_time`, `created_time`, `custom_strike`, `early_close_condition`, `event_ticker`, `exchange_index`
- `expected_expiration_time`, `expiration_time`, `expiration_value`, `floor_strike`
- `last_price_dollars`, `latest_expiration_time`, `liquidity_dollars` (always "0.0000"), `market_type`
- `no_ask_dollars`, `no_bid_dollars`, `no_sub_title`, `notional_value_dollars` ("1.0000"), `open_interest_fp`, `open_time`
- `previous_price_dollars`, `previous_yes_ask_dollars`, `previous_yes_bid_dollars` (24 h ago)
- `price_level_structure`, `price_ranges`, `primary_participant_key`, `result`, `rules_primary`, `rules_secondary`
- `settlement_timer_seconds`, `status`, `strike_type` (greater, greater_or_equal, less, less_or_equal, between, functional, custom, structured), `subtitle`, `ticker`, `title`, `updated_time`
- `volume_24h_fp`, `volume_fp`, `yes_ask_dollars`, `yes_ask_size_fp`, `yes_bid_dollars`, `yes_bid_size_fp`, `yes_sub_title`
- Plus, when relevant: `settlement_value_dollars`, `settlement_ts`, `occurrence_datetime`, `fee_waiver_expiration_time`, `is_provisional`, and `mve_*`.

### 4.3 Events and series

`GET /events` parameters:
- `limit` ≤ 200, `cursor`
- `status ∈ {unopened, open, closed, settled}`. This matches if **any** child market matches.
- `with_nested_markets=true`
- `with_milestones`, `series_ticker`, `tickers` (event tickers), `min_close_ts`, `min_updated_ts`

Notes:
- It excludes multivariate events; use `/events/multivariate` for those.
- Event fields: `event_ticker`, `series_ticker`, `title`, `sub_title`, `category` (deprecated), `collateral_return_type`, `mutually_exclusive`, `exchange_index`, `product_metadata`, `settlement_sources`, `strike_period`, `fee_type_override`, `fee_multiplier_override`.
- `GET /events/{event_ticker}` returns `{event, markets}`. Old events are always available even when their markets are archived.

Series:
- `GET /series/{series_ticker}` returns `{series:{ticker, title, category, categories, tags, frequency, fee_type, fee_multiplier, settlement_sources, contract_url, contract_terms_url, additional_prohibitions, exchange_index, last_updated_ts[, volume_fp]}}`. It is CDN-cached for 15 s.
- `GET /series?category=&tags=&include_volume=true&min_updated_ts=` returns the series list.

Fee schedules:
- `GET /series/fee_changes?show_historical=true` returns `{"series_fee_change_arr":[{id, series_ticker, fee_type, fee_multiplier, scheduled_ts}]}`. Without `show_historical`, only upcoming changes are returned; the list was empty today.
- `GET /events/fee_changes?event_ticker=&limit=&cursor=` returns `{"event_fee_changes":[{id, event_ticker, series_ticker, fee_type_override, fee_multiplier_override, scheduled_ts}], cursor}`. It lists upcoming overrides: 242 today, all MLB.

### 4.4 Trades

`GET /markets/trades` parameters: `ticker`, `limit` ≤ 1000, `cursor`, `min_ts`, `max_ts` (unix s), `is_block_trade`.

Results are **newest first**. Example:
```json
{"count_fp":"8.94","created_time":"2026-09-26T22:52:42.728645Z","is_block_trade":false,
 "no_price_dollars":"0.2300","taker_book_side":"bid","taker_outcome_side":"yes","taker_side":"yes",
 "ticker":"KXBTC15M-26SEP261900-00","trade_id":"0723a045-55c1-a478-de4a-642a5deee757","yes_price_dollars":"0.7700"}
```
- `taker_side` is deprecated. Use `taker_outcome_side` (`yes`/`no`) or `taker_book_side` (`bid` means the taker bought YES; `ask` means the taker sold YES, i.e. bought NO).
- Omit `ticker` to get the global tape.
- Volume check: KXBTC15M printed 1,000 trades in about 22 s near expiry.

### 4.5 Candlesticks (real JSON pasted from live responses)

Live endpoints:
- `GET /series/{series}/markets/{ticker}/candlesticks?start_ts&end_ts&period_interval` with `period_interval ∈ {1, 60, 1440}` (minutes) and optional `include_latest_before_start=true`.
- Batch: `GET /markets/candlesticks?market_tickers=A,B` (comma-separated, ≤100 tickers, ≤10,000 candles). Response: `{"markets":[{"market_ticker","candlesticks":[…]}]}`.
- Event: `GET /series/{s}/events/{e}/candlesticks`. Response: `{"market_tickers":[…],"market_candlesticks":[[…],…],"adjusted_end_ts":…}`.

Live candle from `KXBTC15M-26SEP261845-45`, 1-minute:
```json
{"end_period_ts":1790462280,"open_interest_fp":"425751.35",
 "price":{"close_dollars":"0.0700","high_dollars":"0.1200","low_dollars":"0.0690","mean_dollars":"0.0846","open_dollars":"0.1100","previous_dollars":"0.1000"},
 "volume_fp":"128792.42",
 "yes_ask":{"close_dollars":"0.0700","high_dollars":"0.1200","low_dollars":"0.0700","open_dollars":"0.1100"},
 "yes_bid":{"close_dollars":"0.0690","high_dollars":"0.1000","low_dollars":"0.0690","open_dollars":"0.1000"}}
```

Period with no trades: the `price` OHLC keys are **absent**, not null, and only `previous_dollars` remains. The quote sentinels are 0 and 1:
```json
{"end_period_ts":1790462760,"open_interest_fp":"965300.19","price":{"previous_dollars":"0.0010"},"volume_fp":"0.00",
 "yes_ask":{"close_dollars":"1.0000","high_dollars":"1.0000","low_dollars":"0.0010","open_dollars":"0.0010"},
 "yes_bid":{"close_dollars":"0.0000","high_dollars":"0.0000","low_dollars":"0.0000","open_dollars":"0.0000"}}
```

**Historical candles use a DIFFERENT schema**, without the `_dollars` and `_fp` suffixes. From `GET /historical/markets/{ticker}/candlesticks` for `KXBTC15M-26JUL271945-45`:
```json
{"end_period_ts":1785195240,"open_interest":"229447.63",
 "price":{"close":"0.3700","high":"0.4500","low":"0.3600","mean":"0.4020","open":"0.4500","previous":"0.4500"},
 "volume":"98232.62",
 "yes_ask":{"close":"0.3700","high":"0.4500","low":"0.3700","open":"0.4500"},
 "yes_bid":{"close":"0.3600","high":"0.4400","low":"0.3600","open":"0.4400"}}
```

Notes:
- `end_period_ts` is the inclusive end of the period.
- Candles can be **sparse**: an hourly query over about 51 h of an MLB market returned 44 candles. Forward-fill.
- An illiquid market can return `[]`.
- In `yes_ask`/`yes_bid`, 1.0 and 0.0 mean an empty side.
- `min_dollars` and `max_dollars` exist in the spec but only on event candles.

### 4.6 Historical tier [DOCS][LIVE]

`GET /historical/cutoff` returned today:
```json
{"market_positions_last_updated_ts":"2026-07-28T00:00:00Z","market_settled_ts":"2026-07-28T00:00:00Z",
 "orders_updated_ts":"2026-09-12T00:00:00Z","trades_created_ts":"2026-07-28T00:00:00Z"}
```
- Markets settled before `market_settled_ts`, and their candles, are only available at `/historical/markets`, `/historical/markets/{t}`, and `/historical/markets/{t}/candlesticks`.
- Trades before `trades_created_ts` are only available at `/historical/trades` (same filters, public).
- `/historical/fills`, `/historical/orders`, and `/historical/positions` need auth.
- Each data type has its own cutoff, and the cutoffs advance over time. Always read them rather than hard-coding.
- Events and series are never archived.
- Boundary behaviour [LIVE]:
  - A market settled a few minutes before the cutoff (2026-07-27T23:45Z) was still served by both live and historical endpoints.
  - A Feb 2026 market returned **404** live and was found in historical.
  - Live `/markets/trades?ticker=<old>` returns `{"cursor":"","trades":[]}` rather than an error.
- `/historical/markets` supports `tickers`, `event_ticker`, `series_ticker`, and `mve_filter=exclude`. Results are sorted by newest settlement first. For example, KXHIGHNY: 1,000 markets from 2026-02-11 to 2026-07-27, 83% no.
- The archive reaches back at least to 2025. This gives backtest data for settled markets: full trade tapes plus 1-minute candles.

### 4.7 Exchange

`GET /exchange/status` [LIVE]:
```json
{"exchange_active":true,"trading_active":true,"intra_exchange_transfers_active":true,
 "exchange_index_statuses":[{"exchange_index":0,"description":"Default",...},{"exchange_index":1,"description":"Combos",...},
  {"exchange_index":2,"description":"Crypto & Commodities",...},{"exchange_index":3,"description":"Tennis, Baseball, Basketball",...}]}
```
- An `exchange_estimated_resume_time` field is present during outages.

`GET /exchange/schedule` [LIVE]:
- `standard_hours` is open 00:00–00:00 every day, except Thursday, which has a gap from 03:00 to 05:00 (ET).
- `maintenance_windows` was `[]`.

Sharding:
- Shard 0 is the default; shard 1 is combos; shard 2 is crypto and commodities; shard 3 is tennis, baseball and basketball.
- It affects only authenticated trading (collateral must be pre-allocated per shard). Reads are unaffected. [DOCS]

Other public endpoints I did not verify:
- `/events/multivariate`
- `/events/{e}/metadata`
- `/search/tags_by_categories`
- `/live_data/*`, including the Kalshi Weather Index `/live_data/weather/{city}`, which gives minute-level settlement-source temperatures.
- `/structured_targets`, `/milestones`
- `/cfbenchmarks/*` REST passthrough for BRTI and other indices.

These may matter to strategy researchers: BRTI underlies KXBTC15M and KXBTCD settlement.

---

## 5. Rate limits and WebSocket

### 5.1 Rate limits

**Authenticated REST** [DOCS]:
- Token buckets, with separate Read and Write buckets.
- Most requests cost 10 tokens. Cancels cost 2. Batch calls cost the per-item sum.
- Per-second budgets:

  | Tier | Read | Write |
  |---|---|---|
  | Basic | 200 | 100 |
  | Advanced | 300 | 300 |
  | Expert | 600 | 600 |
  | Premier | 1,200 | 1,200 |
  | Paragon | 2,400 | 2,400 |
  | Prime | 4,800 | 4,800 |
  | Prestige | 12,000 | 9,600 |

  (A Sep 24 changelog raised Premier and above by 20%; the docs table may lag.)
- A Basic account therefore sustains about 20 reads/s.
- Burst capacity is 3 s of budget for Basic and Advanced Read buckets, and for Write buckets above Basic.
- Advanced is free: call `POST /account/api_usage_level/upgrade`.
- A 429 has no `Retry-After` header. Back off exponentially.

**Unauthenticated REST:**
- **Not documented.** A third-party blog claims about 30 req/s; this is unverified [3P].
- Measured [LIVE] from this IP (shared with other agents), sequential curl at about 4–5 req/s against `/orderbook`: 39×200 and 1×429.
- 429 body: `{"error":{"code":"too_many_requests","message":"too many requests"}}`, `cache-control: no-store`. This differs from the docs' `{"error":"too many requests"}`, so parse both.
- **Design rule:** ≤ 3 req/s steady per bot process, exponential backoff with jitter on 429, and batch endpoints:
  - `/markets/orderbooks`: up to 100 books per call.
  - `/markets?tickers=`: many markets per call.
  - `/markets/candlesticks`: up to 100 markets.
- CloudFront caches some GETs: `/markets` and `/series` for 15 s, `/exchange/status` and `/historical/cutoff` for 1 s. `/orderbook` and `/trades` are uncached.

### 5.2 WebSocket [DOCS][LIVE]

URLs:
- Production: `wss://api.elections.kalshi.com/trade-api/ws/v2`, recommended `wss://external-api-ws.kalshi.com/trade-api/ws/v2`
- Demo: `wss://demo-api.kalshi.co/trade-api/ws/v2`

Auth:
- **The handshake requires the KALSHI-ACCESS-* headers.** The docs say "Some channels carry only public market data, but the connection itself still requires authentication."
- LIVE: an unauthenticated upgrade returned `401 {"code":"token_authentication_failure"}`.
- Sign `timestamp + "GET" + "/trade-api/ws/v2"`.

Channels:
- Public data: `orderbook_delta`, `ticker`, `trade`, `market_lifecycle_v2`, `multivariate_market_lifecycle`, `cfbenchmarks_value`, `cfbenchmarks_value_5hz`, `pyth_value`.
- Private: `fill`, `user_orders`, `market_positions`, `order_group_updates`, `communications`.

Subscribe and messages:
- Command: `{"id":1,"cmd":"subscribe","params":{"channels":["orderbook_delta"],"market_tickers":["…"],"use_yes_price":true}}`.
- The orderbook channel first sends `orderbook_snapshot` with `yes_dollars_fp` and `no_dollars_fp`, then `orderbook_delta` with `price_dollars`, `delta_fp`, `side`, `ts_ms`.
- By default, NO-side prices use **no-leg** pricing. Set `use_yes_price:true`; its default will flip later.
- Messages carry a per-subscription `seq`. On a gap, resubscribe.
- The server sends a Ping every 10 s; reply with Pong.
- `market_lifecycle_v2` event types: `created`, `activated`, `deactivated`, `close_date_updated`, `determined` (with `settlement_value`), `settled`, `metadata_updated`, `price_level_structure_updated`, `event_fee_update`.

### 5.3 Decision

The paper bot runs **without keys and polls REST**:
- `/markets/orderbooks` batches every 2–5 s for the watched set.
- `/markets/trades?min_ts=` for the maker-fill model.
- `/markets?tickers=` or `/events/{e}` every 15–30 s for status and result.
- Series and fee data hourly.

Upgrade path: a free Kalshi account plus an API key (Advanced tier, about 30 reads/s) unlocks the WebSocket for market data. This involves no trading, and it is the recommended path to realistic queue modelling.

---

## 6. Auth (FUTURE live mode only) [DOCS]

Headers on every authenticated REST call and on the WS handshake:
- `KALSHI-ACCESS-KEY`: the key ID.
- `KALSHI-ACCESS-TIMESTAMP`: ms since the epoch.
- `KALSHI-ACCESS-SIGNATURE`: `base64(sign(timestamp_ms + METHOD + path))`.

Signing rules:
- `path` is the full path from the API root **without the query string**, e.g. `/trade-api/v2/portfolio/orders`. The host does not matter.
- Key types:
  - **RSA-2048**: RSA-PSS, SHA-256, MGF1(SHA-256), salt length = digest length (32). This is the default, and the official SDKs support only RSA.
  - **Ed25519**: sign the message directly. Available since 2026-09-24 and recommended as cheaper. Create with `POST /api_keys/generate` and `{"key_type":"ed25519"}`, or register your own public key.
- Detect the key type from the parsed key, not from the PEM header.

Environment:
- Demo credentials are separate from production. Use demo for any live-mode dry run.

Relevant trading endpoints (V2):
- `POST /portfolio/events/orders`, `/batched`
- `DELETE /portfolio/events/orders/{id}`
- `POST …/{id}/amend` and `…/{id}/decrease`
- `DELETE /portfolio/events/orders` (cancel all)
- `GET /portfolio/balance` (`balance_dollars` at centi-cent precision), `/positions`, `/fills` (`fee_cost`), `/settlements` (`fee_cost`, `revenue`), `/orders/queue_positions`

The legacy `/portfolio/orders` is deprecated.

---

## 7. Gotchas checklist

1. Money handling:
   - Use `Decimal` everywhere.
   - Prices can have 4 dp, e.g. `"0.0010"` on BTC15M and `"0.0001"` on combos. Counts can be `"0.03"`.
2. Orderbook ordering:
   - Arrays are ascending: the best bid is `[-1]`, not `[0]`.
   - The batch orderbook needs repeated `tickers=` params.
3. Staleness:
   - `/markets` quotes can be 15 s stale because of the CDN.
   - Empty-side sentinels are bid `0.0000` and ask `1.0000`. Do not trade against them.
4. Market selection:
   - Use `mve_filter=exclude` for scanning.
   - There are more than 120,000 open non-combo markets. Scan by series, event, or close-time window.
5. Fees:
   - Fees depend on the **series AND the event override at trade time** (the MLB pre-game ×0.5 case).
   - Re-fetch `/events/fee_changes` daily.
6. Makers:
   - Maker orders are free on 98.6% of series. On the 163 maker-fee series, a maker pays 25% of the taker fee (50% on combos).
7. Settlement timing:
   - Markets close early when the outcome is known (`can_close_early` is true everywhere). Expect close-to-settle in minutes (median 6 min).
   - Resting paper orders must be cancelled at close.
8. Scalar results:
   - `result == "scalar"` means a fair-price settlement. Pay YES `settlement_value_dollars`, not 0 or 1.
9. Historical data:
   - Historical candles use unsuffixed keys (`open`, `volume`, `open_interest`), unlike live candles.
   - Live endpoints 404 once a market is archived past the cutoff.
10. Scheduled pause:
    - Thursday 03:00–05:00 ET trading pause: no new orders, and resting orders stay unless `cancel_order_on_pause`.
11. Deprecated fields:
    - `taker_side`, `side`/`action` on orders, `expiration_time`, `title`/`subtitle`, `category` on events, and `liquidity_dollars` (always 0) are deprecated or unused.
12. Rate limits:
    - The unauthenticated limit is shared per IP.
    - Cache raw responses and use batch endpoints.

---

## 8. Sources

- Kalshi docs index: https://docs.kalshi.com/llms.txt. Pages used:
  - fee_rounding, fixed_point_migration, market_lifecycle, market_settlement, maintenance_and_pauses
  - rate_limits, orderbook_responses, order_direction, historical_data, pagination
  - api_environments, api_keys, quick_start_websockets, exchange_sharding
  - websockets/* and fix/market-settlement
- OpenAPI spec: https://docs.kalshi.com/openapi.yaml (v3.31.0). AsyncAPI spec: https://docs.kalshi.com/asyncapi.yaml. Changelog: https://docs.kalshi.com/changelog
- Fee schedule PDF, effective Feb 5, 2026 (Wayback snapshot 20260612): https://web.archive.org/web/20260612075630/https://kalshi.com/docs/kalshi-fee-schedule.pdf. The current "July 2026 – 7.7.26 Update" edition at https://kalshi.com/docs/kalshi-fee-schedule.pdf is blocked by the Vercel bot checkpoint (429).
- Help center: https://help.kalshi.com/en/articles/13823805-fees (no formulas; points to the PDF).
- Third-party July 2026 schedule summaries [3P]: https://www.botforkalshi.com/blog/kalshi-fees-explained (updated 2026-09-16) and https://blog.polytrage.com/kalshis-fee-structure-explained/
- Live API: all [LIVE] tags. Raw responses are cached under the session scratchpad (`.../scratchpad/apinotes/cache`).
