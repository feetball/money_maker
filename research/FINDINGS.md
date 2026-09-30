# Research findings: which Kalshi strategies hold up?

Consolidated 2026-09-27 from the research workflow: an API/fee reference, a literature survey,
a historical dataset of about 117k settled markets, a walk-forward calibration study, a live
structural-arbitrage scan and a crypto fair-value study. Independent skeptic agents re-checked
every claimed edge. The detailed scripts and CSVs are in the subdirectories.

**Bottom line: nothing here is riskless or guaranteed.** Kalshi prices are well calibrated
once the book is tight, and taking liquidity loses to the spread plus fees almost everywhere.
One rule replicated on untouched data. Everything else is either negative or unproven and
belongs in forward paper-testing only.

## Verdicts

| Strategy | Evidence (¢/contract after fees) | Verdict |
|---|---|---|
| **BTC 15-min favourite at 10 min to close** (KXBTC15M; buy the side priced 0.85–0.97 when the spot model agrees by ≥ 1¢; taker; hold) | In-sample (Jul 19–Sep 26) **+3.7** (n=555, CI +1.4…+5.8). **Untouched holdout** (Jun 21–Jul 18) **+4.9** (n=203, CI +1.7…+7.8, 5/5 weeks positive). Pooled +4.0 (CI +2.1…+5.8). Still +2.9 with 2¢ slippage. | **Primary. Only rule that replicated out of sample.** Timing-sensitive: lags 7 and 11 min are ≈0 or negative. The model filter adds little over the blind favourite (+2.1 / +3.5 holdout). Did not replicate on ETH15M. Forward estimate +2…+3¢, about 7–8 trades/day, deep book. |
| Ladder favourites near expiry (B4-ladder-72h; Commodities/Financials/Crypto/Economics ladders, buy YES at ask when YES bid ≥ 0.97 within 72h of expected expiry) | TRAIN +1.08, TEST +1.07 (CI +0.86…+1.22), **but the independent May–Jul holdout was −0.9** (CI −3.8…+1.0); pooled +0.6 (CI −0.04…+1.1). Rests on 5 losses; one ladder gap can erase ~700 wins. | **Experimental.** Post-hoc cuts, failed holdout, thin capacity. Forward paper-test only, small size. |
| Maker favourite harvest (rest bids on the 0.85–0.97 side, fee-free series) | Literature: makers +1.1% vs takers −1.1% per trade (Becker, 72M trades); makers buying ≥50¢ +1.9% post-fee (Bürgi–Deng–Whelan). Our data: maker variant per *signal* +0.2…+0.4 (CI touches 0); adverse selection is heavy. | **Experimental.** The best-documented edge in the literature, but hinges on fill realism; our broker only fills resting orders from real trades after the queue ahead. |
| Mutually-exclusive NO-basket arbitrage | 0 of 4,986 events positive after fees at REST speed; best −0.03¢/unit. | **Risk-free when it fires, but it almost never fires.** Cheap to run. |
| Fade longshots (buy NO when YES ≤ X) | Negative at every X from 2¢ to 20¢ out of sample (−0.3…−3.3). | **Rejected.** |
| Back favourites at 80–93¢ / tennis favourite fade / best-of-4,253 grid | All negative out of sample (the grid's best TRAIN config: +2.85 → TEST −0.86). | **Rejected (overfitting).** |
| Spot fair-value taker on BTC/ETH hourly, BTC ranges, S&P/Nasdaq hourly | −0.5…−2.5 out of sample; Kalshi absorbs spot moves in about 1.5 s. | **Rejected.** |
| Stale-quote taker on DOGE/SOL/XRP 15-min | +3…+5 only if filled at the same minute's quote; ≈0 one minute later. | **Rejected** (needs a sub-second WebSocket feed and an API key). |
| Weather forecast models, sports vs sportsbook lines, cross-venue arbitrage | Literature: negative or inaccessible (weather bots lost 7–38%; value vs sharp lines lasts 30–90 s). | **Not built.** |

## Fees and fills (what every number above assumes)
- Taker fee `ceil_cent(0.07 · M · C · P · (1−P))` per order (conservative cent rounding;
  direct members are actually aligned to $0.0001). Maker orders are free on 98.6% of series.
- A 1-contract order at extreme prices loses to fee rounding: size ≥ 10 contracts.
- Positions held to settlement pay no settlement fee.

## How much evidence is enough
Detecting a 1¢/contract edge needs roughly 1,200–3,600 independent bets. The dashboard's
go-live readiness check requires ≥ 200 settled trades **and** a 95% CI on mean P&L per trade
that excludes zero. Expect weeks of paper trading before that verdict means anything.

## Sources
- `docs/kalshi_api_notes.md` — API, fee and settlement reference
- `research/calibration/` — walk-forward study (`oos_headline.csv`, `survivor_checks.csv`, `verify_*`)
- `research/crypto_fv/` — crypto study (`results/KXBTC15M_*`, `fav15.py`, `verify_stats/`)
- `research/arbitrage/REPORT.md` — structural arbitrage scan
- `research/literature/sources/` — Bürgi–Deng–Whelan (2026), Bartlett–O'Hara, Becker, Moshrefi, and others

## Follow-up 2026-09-30: two more ideas tested, both rejected
Scripts: `research/new_ideas/` (run from the repo root / `research/crypto_fv` as noted in each file).

| Idea | Result (¢/contract after fees) | Verdict |
|---|---|---|
| **Fade weather longshots** (buy NO when YES ask is 3–20¢ on Climate & Weather, taker, one trade per market; train < 2026-08-15 ≤ test). The pooled calibration table hints at +1.6¢ for the 5–15¢ bucket. | Train +0.4…+1.7 (n≈1,000 per cell), **test −0.3…−0.8** in every price/horizon cell (n≈1,600–2,400, all CIs include 0). | **Rejected.** The in-sample bias does not survive the split. |
| **Maker entry for the BTC-15m lag-10 favourite** (rest a bid at the touch instead of crossing; fill only when the market trades *through* our price; same 555 signals as the taker rule). | Taker on those signals: +3.5. Maker, 3-min window: fills 53%, **+0.8** per fill (+0.4 per signal); 5-min: −0.1; 9-min: −0.4. | **Rejected.** Adverse selection eats the saved spread: a resting bid fills mostly when the favourite is sliding. The edge needs immediacy. |
