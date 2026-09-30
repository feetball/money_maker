# Coinbase spot strategies — research findings (2026-09-27)

Setup: 246 daily + 12 hourly pre-registered configurations (`research/coinbase/strategies/prereg.json`),
point-in-time universe including delisted products, decisions at the daily close, fills at the next
open, costs = US Intro taker **0.90%** (Coinbase blog 2026-09-16) + per-product slippage.
Parameters chosen on 2018–2023, tested on **2024-01-01 → 2026-09-25**. Three independent skeptics
(leakage, costs, statistics) re-implemented the results.

**Bottom line: nothing beats simply holding BTC after retail fees.** No strategy's excess return
over BTC buy-and-hold has a 95% CI that excludes zero; neither does any strategy's (or BTC's own)
return over cash in the 2.7-year test.

| Strategy | TEST (2024→2026-09) | Verdict |
|---|---|---|
| BTC buy-and-hold (benchmark) | 28.6%/yr, Sharpe 0.76, max DD −53% | Baseline |
| **BTC trend filter** (hold BTC when close > 1.02×SMA100, cash when close < 0.98×SMA100; trade at the next open) | 29.1%/yr, Sharpe 0.92, max DD **−32%**, ~3 round trips/yr; excess vs BTC +0.5%/yr, CI [−27, +42] | **Risk overlay, not an edge.** Roughly halves drawdowns (smaller DD in 5 of 6 regimes). Fragile: one day of fill delay → 22%/yr; parameter neighbours mostly worse. Maker fills at 0.50% would add ~3 pts/yr. |
| ETH 200-day trend + 50% vol target | 18.0%/yr, max DD −33% | Beats holding ETH (6.2%), loses to BTC by ~8–10 pts/yr. Risk strategy only. |
| Alt basket, 50-day trend + 40% vol target | 2.1%/yr, max DD −40% | Avoids the basket's collapse (−13.9%/yr) but trails BTC by 26 pts. |
| Weekly cross-sectional momentum | **−43%/yr**, max DD −90% | Rejected (negative even before fees). |
| Hourly mean reversion | −31% to −96%/yr | Rejected (fees 1.8% per round trip ≫ 7–25 bps edge). |

Sources: `research/coinbase/strategies/` (analysis + `verify_*`), `research/coinbase/data/README.md`,
`docs/coinbase_api_notes.md` (fees, API), `research/coinbase/literature/`.
