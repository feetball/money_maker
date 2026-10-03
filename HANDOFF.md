# Handoff: changes made for Daniel's btc15m paper run (2026-10-02)

Daniel is paper trading `btc15m_favorite` on his own copy of this repo. Before starting, three
independent AI reviewers (GPT-6 Astra, Claude Fable 5.1, Claude Opus 5.5) read the code and the
research. Every finding below was then checked against the code at commit `9c1770e`. This file
lists what changed, why, and where. Each change is its own commit on the `paper-run-fixes` branch.

## Changes

| # | Change | Why | Files |
|---|---|---|---|
| 1 | The decision fetches the Coinbase spot fresh (`spot(max_age_s=0)`) instead of taking the feed's 5 s cache. | Kalshi absorbs a BTC move in about 1.5 s. A 5 s old spot after a sharp move makes a favourite that just weakened look cheap, which is adverse selection the research did not have (it compared same-second candles). | `kalshibot/strategies/btc15m_favorite.py` |
| 2 | `EngineContext.clock()` exposes the engine's real clock. btc15m re-checks the deadline after its model and book reads (unrounded seconds) and skips with `skip="late"` when it is past the 15 s limit. The logged `lag_s` is now the real lag. | `ctx.now` is frozen at the tick start, so a decision whose network reads took 20 s still traded and was logged as on time. Backtest contexts have no clock, so replay results are unchanged. | `kalshibot/engine.py`, `kalshibot/strategies/btc15m_favorite.py` |
| 3 | Bootstrap CIs resample **UTC days**, not events, in the dashboard analytics and in backtest metrics. Each event belongs to one day, the UTC day of its earliest entry (`opened_at`, else the row time), so a partial close and its settlement never split; ISO-string times are parsed; an event with no time is its own cluster. | Every KXBTC15M window is its own event, so the event-clustered CI was really a per-trade bootstrap. One market regime moves every window of a day together. The same 18 losses in 300 trades pass when spread out and now fail when they arrive three to a day (see the updated readiness test). | `kalshibot/analytics.py`, `kalshibot/backtest/runner.py` |
| 4 | New `engine.keep_log_rows` setting (default 50,000 as before; 0 = never prune). | Each window's skip or trade record lives in `logs`, and the hard-coded 50,000-row cap would drop the early weeks of a long run. `prune(table, 0)` would have deleted everything, so housekeeping skips it at 0 and `Store.prune` itself now treats `keep_last <= 0` as keep-all. | `kalshibot/config.py`, `kalshibot/engine.py`, `kalshibot/store.py` |
| 5 | New `GET /api/health`: 503 with a plain reason when the engine is stopped or its task died, Kalshi is unreachable, the engine has not ticked within `max(120 s, 4 x tick_s)` of its last tick or (re)start, or an enabled strategy's `on_tick` has raised on every tick for that long (new `StrategyRuntime.last_ok_at`). A scheduled exchange pause returns 200 with `gated: "trading_paused"`. The Docker HEALTHCHECK uses it. | `/api/status` returns 200 even after the engine loop has crashed, and a strategy that raises every tick still advanced `last_tick_at`, so a 12-week run could look healthy while deciding nothing. | `kalshibot/api/server.py`, `Dockerfile` |
| 6 | `config.paper-run.yaml`: btc15m only, fixed 50 contracts, `max_spot_age_s: 5`, scanner off, keep all logs, Coinbase venue off, **$10,000 paper balance, profit sweep off, no daily loss stops** (account and btc15m). `deploy/deploy-smol.sh` checks after start that only btc15m is on, the params took effect and the sweep is off. | The side strategies' daily limits add up to $195, above the account's $150 kill switch, and share the 3 req/s budget. **The profit sweep (default 100%) moves every win out of the equity used for sizing while losses stay in it, so a fixed 50-lot shrinks to 0 within weeks** (10% allocation of a falling equity). Daily stops drop the rest of exactly the bad-regime days the run must measure. Dashboard toggles stored in the DB beat config.yaml, hence the post-start check. | `config.paper-run.yaml`, `deploy/deploy-smol.sh` |
| 7 | `test_experimental_strategies_cannot_starve_the_primary` pins the risk clock to `T0`. | Its market closes `T0 + 1 day` (2026-09-28). Against the real clock that date has passed, so the test failed everywhere. | `tests/test_regress_review_execution.py` |
| 8 | `.env.example` for `KALSHIBOT_PORT` / `KALSHIBOT_BIND`. | Documents the only env keys `deploy.sh` reads. | `.env.example` |

New tests: `tests/test_paper_run_fixes.py` and the "paper-run fixes" block at the end of
`tests/test_strategy_btc15m.py`.

## Found but not changed (worth your look)

- **Selection.** `fav15.py` sweeps 75 lag/band/threshold cells and `backtest.py` adds 105 model
  variants. On the holdout, lag 10 earned +4.9c while its neighbours (lag 9 +2.1c, lag 11 -0.8c) are
  flat. A planning prior of about +2c seems safer than +4c. The `verify_*` folders cited in the
  docstrings are gitignored, so the clone cannot check the "untouched holdout" claim.
- **Power.** At about 25c per-contract SD, 300 trades give roughly 30% power for a true +2c edge.
  About 1,200 trades (5 to 6 months) give 80%. The dashboard's 300-trade gate is about a coin flip.
- **`tail_stats`** still treats each event as an independent draw for the loss-rate bound.
- **Backtest CIs changed meaning.** The Backtests page now day-clusters too (P&L and trades are
  unchanged), so the event-clustered CIs quoted in the strategy docstrings and FINDINGS.md no
  longer reproduce there. Day-clustered research CIs come from `research/paper_run/data_review.py`.
- **The profit sweep shrinks every equity-sized strategy**, not just btc15m: with the default 100%
  sweep, equity can only fall, so percentage caps tighten forever. Worth a look for live use.
- **Dashboard overrides beat config.yaml** (`strategy_state` rows survive `reset`). A startup
  warning when a stored override differs from the config would make that visible.
- **Fill rule.** The research filled at the next minute's ask regardless of price, while live
  sends an IOC at the decision ask. `--fill same` and `next` agree closely, but the IOC's unfilled
  windows are a selection effect worth logging.
- **Settlement index.** Settlement is the 60 s BRTI average. The model uses Coinbase plus a
  48-window median basis, and that basis error is about the size of the 1c entry hurdle at p of about 0.9.
- **No exit rule.** Hold-to-settlement takes the full ~$47 loss. The research panel has bids at
  lags 5/3/2/1, so an exit rule (for example, sell when model P < 0.5 with 3+ minutes left) can be tested
  offline.
- **Kelly is decorative.** The $50 cap always binds, and the stacked model gives the spot model
  about 0.094 weight next to the market mid.
- **Log every window.** All 96 windows a day are observable for free. Logging the in-band
  favourite's ask and model P at every minute from 12 down to 5 would test the lag-10 peak at about
  12 times the sample of the trades alone.
- **Windows-only test failures.** Six tests fail on Windows because the `fcntl` process lock is a
  no-op there. They are not affected by these changes.

## Data review on your research data (2026-10-03)

The rule reproduces exactly (555 trades +3.70c, holdout 203 trades +4.94c) and survives UTC-day
clustering in both periods (in-sample CI +1.46..+5.81, holdout +1.69..+8.00). The vol-tercile ranking
flips between periods and every early-exit variant lowers the holdout mean, so neither was added. Full
table and the run's pass/stop rules: `research/paper_run/PREREGISTRATION.md`.
