# Structural arbitrage on Kalshi — findings (single snapshot, 2026-09-26 22:44 UTC)

Scope: all open non-MVE events (14,091 events, ~133.8k markets; 4,998 mutually exclusive,
all with `collateral_return_type = MECNET`). ~1.19M candidate structures evaluated at top of
book with taker fees and per-series `fee_multiplier` (not including per-order rounding).
The planned 20–30 min repeated scan was cut short by a full disk, so frequency/persistence
figures are indicative only.

| Structure | Checked | Positive after fees | Best |
|---|---|---|---|
| Same-market YES ask + NO ask < 1 | ~131k | 0 | −0.001/contract (never crosses) |
| Mutually-exclusive NO basket | 4,986 | 0 | −0.0003/unit (KXNEXTTEAMNHL-26DLARKIN71) |
| Strike ladder YES(lower) + NO(higher) | ~1.02M | 1 | KXBRINFHIGH-27JAN01 ≈ +$0.005/unit pre-depth, settles Jan 2027 |
| Range pairs / tails | 25.6k | 0 | — |
| "Before date X" ladders | 2,081 | 0 | not risk-free anyway (early-close/timing rules) |
| YES basket on mutually-exclusive events | 4,986 | many *apparent* | almost all are **non-exhaustive traps** |

YES-basket traps: KXTOPMODEL (+0.90, not all models listed), KXBILLSCOUNT (+0.88, no "11+"),
Grammy categories (+0.27, nominees + Tie only), KXMODELHIGH-1550 (+0.65, "nobody reaches"
uncovered). Genuinely exhaustive numeric-range events: KXETHY-27JAN0100 (+$0.029/unit, capital
locked ~3 months) and KXGDPYEAR-36 (+$0.05/unit, locked until ~2037). MECNET returns collateral
immediately only on NO positions, so YES baskets lock the full cost.

**Bottom line:** at 2–3 minute REST polling, risk-free structural arbitrage is effectively
zero (< $10/day capacity). NO baskets get within $0.0003/unit of break-even, so any real
opportunities are fleeting and need sub-second monitoring.

## Pitfalls any arb detector must handle (a naive first pass reported ~$53M of fake profit)
- Group ladder markets by `custom_strike` (team/player) + close time; map both teams' spread
  markets onto one margin axis — never treat them as one ladder.
- Never treat a mutually-exclusive YES basket as risk-free unless exhaustiveness is proven.
- 370 events have legs with different close times; `can_close_early` is common; cancelled
  games settle at a "fair price" — all break risk-free-ness.

## Fee facts gathered
- Taker fee = 0.07 × fee_multiplier × C × P × (1−P). Series fee types: 14,198 `quadratic` (mult 1),
  159 `quadratic_with_maker_fees` (maker coefficient 0.0175), 18 with multiplier 0.5, 14 with 0.
- Rounding (docs.kalshi.com/getting_started/fee_rounding): trade fee rounded **up to
  $0.000001**; for non-direct members the balance change is floored to the cent with a
  per-order rebate accumulator — in practice ≤ ~$0.01 extra per order per leg.

## API facts
- Batch books: `GET /markets/orderbooks?tickers=A&tickers=B` (repeat param, ≤100). Comma list → empty.
- Batch candles: `GET /markets/candlesticks?market_tickers=A,B&start_ts&end_ts&period_interval`
  (comma-separated); candle fields `end_period_ts`, `open_interest_fp`, `volume_fp`,
  `price{open,high,low,close,mean,previous}_dollars`, `yes_bid{ohlc}`, `yes_ask{ohlc}`.
- Nested best bid/ask in `/events?with_nested_markets=true` matched live books for 499/500 markets.
- Full open-event listing: 72 pages × 200 (20–80 s). `GET /series` returns all 14,393 series
  with fee fields in one ~17.6 MB response (cache gzipped: `raw/series_all.json.gz`).

Sources: kalshi.com/docs/kalshi-fee-schedule.pdf · docs.kalshi.com/getting_started/fee_rounding ·
help.kalshi.com/en/articles/13823816-collateral-return · help.kalshi.com/en/articles/13823805-fees ·
docs.kalshi.com/changelog
