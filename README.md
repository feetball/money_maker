# kalshibot — Kalshi paper-trading bot and dashboard

kalshibot runs trading strategies against **live Kalshi market data** and fills their
orders in a **simulated (paper) account**. It is one Python process: a trading engine, a
REST + Server-Sent-Events API, and a React dashboard to watch and steer it.

A second, fully separate paper venue for **Coinbase spot crypto** runs in the same process
with its own account. See [Coinbase (crypto spot)](#coinbase-crypto-spot).

> **PAPER TRADING ONLY.** kalshibot never places a real order. It has no code path for
> real orders and never asks for Kalshi credentials. All market data comes from Kalshi's
> public, unauthenticated REST API
> (`https://api.elections.kalshi.com/trade-api/v2`). Every balance, fill, position and
> P&L figure it shows is simulated. It is not investment advice, and good paper results
> do not guarantee live results.

The paper broker tries hard not to flatter a strategy:

- It re-reads fresh order books at execution time.
- Taker orders walk the real depth.
- Liquidity it has taken stays used up.
- Resting orders fill only from real trades printed after they were placed, behind the
  real queue.
- Fees use Kalshi's formulas and rounding.
- Positions are marked at what selling into the bid ladder would bring.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §6 for the rules.

## Run with Docker (recommended)

You need Docker with the compose plugin. One container runs everything (engine, API and
dashboard):

```bash
./deploy.sh up        # build + start in the background, waits until healthy
```

Open **http://127.0.0.1:8765**. The paper account (`data/`), `config.yaml` and the
research datasets (`research/`, read-only, for backtests) are bind-mounted from this
directory, so they survive rebuilds and restarts. The container restarts automatically
(`restart: unless-stopped`).

| Command | What it does |
|---|---|
| `./deploy.sh up` | Build if needed, start, wait for the health check |
| `./deploy.sh update` | Rebuild from the current code and recreate the container (use after pulling changes) |
| `./deploy.sh status` | Container state plus engine summary |
| `./deploy.sh logs` | Follow the logs |
| `./deploy.sh restart` / `down` | Restart / stop and remove the container (data is kept) |
| `./deploy.sh backtest --strategy btc15m_favorite` | Run a backtest in a one-off container |
| `./deploy.sh shell` | Shell inside the running container |

- **Port / exposure:** `KALSHIBOT_PORT=9000 ./deploy.sh up` changes the host port. The port
  is bound to `127.0.0.1` by default; `KALSHIBOT_BIND=0.0.0.0` exposes it on the network,
  but the dashboard has **no authentication**, so only do that on a trusted network.
- **One writer per account:** `deploy.sh` refuses to start while a native
  `kalshibot serve` is running, because both would use the same paper account in `data/`.
  Stop the native one first (Ctrl-C).
- Logs are capped at 3 × 10 MB. The image is about 700 MB.

## Quickstart (without Docker)

You need Python ≥ 3.11 with [uv](https://docs.astral.sh/uv/), and Node ≥ 20.19 to build
the dashboard.

```bash
uv sync                                         # backend + dev dependencies into .venv
cd frontend && npm install && npm run build     # dashboard -> frontend/dist
cd ..
uv run kalshibot serve                          # API + dashboard + engine
```

Open **http://127.0.0.1:8765**.

- **First run:** `serve` copies `config.example.yaml` to `config.yaml` and creates the
  paper account (`$1,000`) in `data/kalshibot.sqlite3`.
- **Port:** defaults to 8765 (8000 is used by Docker on this host); override with `--port N`.
- **Stopping:** Ctrl-C shuts everything down cleanly. The account resumes where it left
  off on the next start.

**Already running an older version?** Stop `kalshibot serve` (Ctrl-C) and start it again
to load new code: a running server keeps the code it started with. The paper account, its
positions and the dashboard's settings carry over.

## Strategies

> **No strategy here is guaranteed to make money, on paper or with real money.** Kalshi
> prices are well calibrated once the book is tight, and taking liquidity loses to the
> spread plus fees almost everywhere. One rule held up on data it was not tuned on. The
> experimental strategies either failed that test or were never given it, and they run
> only as small forward paper-tests. The full research write-up is
> [research/FINDINGS.md](research/FINDINGS.md).

Five strategies ship in `kalshibot/strategies/`. All five are **on by default**, with small
dollar caps, so after an upgrade the bot starts paper-trading as soon as `serve` restarts.

| Strategy | Role | What it does | Evidence (¢ per contract, after fees) |
|---|---|---|---|
| `btc15m_favorite` | **Primary** | KXBTC15M only. 10 minutes before each 15-minute window closes, buy the side priced 0.85–0.97 as a taker when a Coinbase spot model says it wins at least 1¢ more often than ask + fee. Hold to settlement. | In-sample (Jul 19–Sep 26) **+3.7** (n=555, 95% CI +1.4…+5.8). **Untouched holdout** (Jun 21–Jul 18) **+4.9** (n=203, CI +1.7…+7.8). An independent look-ahead audit rebuilt it from raw data: +3.8 / +4.8 with every possible leak removed. The backtester reproduces the research trade for trade. |
| `ladder_favorite` | Experimental | At each UTC hour, buy YES at the ask on Commodities / Financials / Crypto / Economics ladder markets whose YES bid is ≥ 0.97 within 72 h of expiry. Hold. | Author's window: +1.08 / +1.07 (train / test). **Failed the independent May–Jul holdout: −0.9** (CI −3.8…+1.0). Many small wins; one ladder gap can erase ~700 of them. |
| `maker_favorite` | Experimental | Rest small bids at the best bid of the 0.85–0.96 favourite in maker-fee-free markets closing in 6 h–3 d. Re-quote or cancel as the book moves. Hold fills. | **Literature only** (makers earn about +1% per trade on Kalshi). This repo's own candle model is *negative* in this band (−1.2 / −2.4 per filled contract), so the strategy reports a prior edge of 0. Not backtestable: candles carry no trade tape or queue. |
| `no_basket_arb` | Arbitrage | On a mutually-exclusive event, buy one NO on every leg when the basket pays ≥ 1¢ per unit after fees and rounding, all legs or none. | **Locks in a profit when it fires, but it almost never fires:** 0 of 4,986 events were positive at REST speed. Not riskless: a voided event, settled at Kalshi's "fair prices", can still lose. Paper fills the legs all-or-none; real orders could leave one leg unfilled. |
| `alt15m_stale` | Experimental | DOGE/SOL/XRP 15-minute markets, polled every 2 s (window 4–12.5 min before close). Buy as a taker when a fresh Coinbase quote, stacked with the market mid, says a side is worth ≥ 2¢ more than its ask plus fee. Hold. | **Unproven.** A ~1 h live 2-second study found 2¢+ gaps in 1–30% of samples, about half still open 2–4 s later. The minute-candle backtest could not confirm it (+3…+5¢ at the same minute's quote, ≈0 a minute later), and it cannot be backtested at seconds resolution. Forward paper-test only. |

Rejected or dropped by the research and **not** built: fading longshots, backing 80–93¢ favourites,
spot fair-value takers on hourly crypto and index markets, stale-quote takers on other
15-minute coins, weather and sportsbook models ([FINDINGS.md](research/FINDINGS.md)).

**What to expect from the primary rule.** The planning estimate is +2…+3¢ per contract on
about 7–8 trades a day, so roughly $1.10–1.65 of expected profit per trade at the default size
(about 55 contracts). That is well below the day-to-day noise: expect losing days and losing
weeks. In the replay a win paid about $5.50 and a loss cost about $47, the profit of eight or
nine wins, and the worst drawdown at the default size was $170. The edge is timing-sensitive. Deciding at 9 or 11 minutes before close earned about half, or nothing, and
an order that arrived a minute late would have missed most fills. The strategy therefore
ticks every 5 s and skips a window rather than decide more than 15 s late. The dashboard's
go-live check (Analytics) judges each strategy separately. It wants at least 300 settled
btc15m trades (about six weeks) and far more for the rare-loss strategies (ladder 1,500, maker
1,000) before it can say "ready".

**Default size and risk** ($1,000 paper account). Each strategy has its own cap and a daily
loss pause. They total 55% of equity, so the experimental strategies can never crowd out the
primary one.

| Strategy | Max share of equity at risk | Pauses for the day after losing | Per-trade size |
|---|---|---|---|
| `btc15m_favorite` | 10% | $100 | ¼-Kelly, $50 cap, ≥ 10 contracts |
| `ladder_favorite` | 15% | $45 | $10 per market, $20 per event, $40 per underlying, $150 total |
| `maker_favorite` | 10% | $30 | $10 per bid, ≤ 8 resting, $100 open in all |
| `no_basket_arb` | 10% | never (hedged) | ≤ $90 per basket |
| `alt15m_stale` | 10% | $20 | ≤ 25 contracts and $12 per entry, one entry per window |

On top of those, the account stops all new entries for the rest of the UTC day after a $150
loss (`risk.daily_loss_limit`).

**Turning strategies on and off.** The first setting found wins:

1. The switch on the dashboard's **Strategies** page. It is saved in the database and
   survives restarts.
2. `strategies.<name>.enabled` in `config.yaml`, or `KALSHIBOT_STRATEGIES__<NAME>__ENABLED`.
3. The strategy's built-in default, which is **on** for all four.

A `config.yaml` created before the strategies existed (`strategies: {}`) therefore runs all
four after a restart. To keep one off, flip its switch or add, for example:

```yaml
strategies:
  maker_favorite: {enabled: false}
```

Parameters (each with help text) can be edited on the Strategies page. The documented
defaults are in [`config.example.yaml`](config.example.yaml). The Strategies page shows
whether each switch state comes from the dashboard, the config or the default.

### Backtests

The backtester replays the research data in `research/` through the **same strategy
classes**, the real risk manager and the real paper broker
([ARCHITECTURE.md](docs/ARCHITECTURE.md) §10). `btc15m_favorite` uses 1-minute candles plus
Coinbase spot; `ladder_favorite` uses hourly candles. `maker_favorite` and `no_basket_arb`
cannot be backtested: candles carry no trade tape and no simultaneous multi-leg depth.

```bash
# primary rule, in-sample and holdout, with the default (live) sizing and risk limits
uv run kalshibot backtest --strategy btc15m_favorite --start 2026-07-19 --end 2026-09-26
uv run kalshibot backtest --strategy btc15m_favorite --start 2026-06-21 --end 2026-07-18
# the research convention: 100 contracts a trade, no risk limits
uv run kalshibot backtest --strategy btc15m_favorite --start 2026-07-19 --end 2026-09-26 \
    --param sizing=fixed --param contracts=100 --no-risk
# execution variants: --fill same (the live-equivalent for btc15m), next, next_ask; --latency S
uv run kalshibot backtest --strategy ladder_favorite --end 2026-09-03 --trades ladder.csv
```

- `--end` is inclusive. `--param k=v` sets one parameter; `--params '{...}'` takes JSON.
- `--save` stores the run so the dashboard's **Backtests** page lists it. It writes only
  the backtests table of the database at `storage.path`. The page can also launch runs
  itself.
- Time and memory: a btc15m in-sample run takes about 20 s. A ladder run takes about 60 s
  and briefly needs ~1.4 GB of RAM (its parquet data needs `pyarrow`, installed by `uv sync`).
  A run started from the dashboard executes in a thread of the server process; while a ladder
  run loaded, live strategy ticks started at most 0.3 s late (measured).

What the replication shows (¢ per contract after fees):

| Run | Trades | Mean | 95% CI |
|---|---|---|---|
| btc15m in-sample, research convention | 555 | +3.69 | +1.49…+5.72 |
| btc15m holdout, research convention | 203 | +4.93 | +1.81…+7.79 |
| btc15m in-sample / holdout, default live sizing and risk | 551 / 203 | +3.54 / +5.20 | |
| ladder test window (Sep 4–25), research convention | 1,619 | +1.08 | +0.87…+1.22 |
| ladder holdout (May 20–Jul 27), research convention | 1,852 | +0.11 | −1.37…+1.00 |
| ladder holdout, default caps and risk | 391 | −0.28 | |
| ladder, all data (May 20–Sep 25), default caps and risk | 836 | +0.39 | −0.44…+1.02 |

A backtest shows that the code matches the research. It does not show future profit.

## Dashboard

| Page | What it shows |
|---|---|
| Dashboard | Equity KPIs, equity curve, P&L by strategy, top positions, live activity feed, engine start/stop and kill switch |
| Positions & Orders | Open positions (best bid, average exit price, liquidation value, fees) and resting orders, which you can cancel |
| History | Fills and settlements (including positions closed early) |
| Strategies | Enable toggles, parameter editors built from each strategy's `param_schema`, per-strategy stats |
| Signals | Every order intent with its decision (executed / partial / rejected / unfilled) and the reason |
| Markets | Scanner over the loaded universe (search, category, sort by volume / close time / spread) |
| Analytics | P&L with bootstrap CIs, expected vs realized edge, calibration, go-live readiness |
| Backtests | Launch and inspect backtests (needs the backtest runner, see Status below) |
| Settings | Risk limits, kill switch, account reset |

The UI polls the REST API every 5–10 s and applies live events from `GET /api/stream`
(SSE). After a reconnect, it asks the server to replay the events it missed.

These Kalshi pages live under `/kalshi` (`/kalshi/positions`, …); the old paths redirect
there. `/` is the **Overview** of both venues, and the Coinbase pages live under `/coinbase`
(see below).

## Coinbase (crypto spot)

Coinbase spot crypto is a **second, fully separate paper venue**. It runs in the same
process as Kalshi but shares nothing with it:

- **Own paper account:** its own USD cash (`coinbase.starting_balance`, default $1,000),
  its own SQLite file (`data/coinbase.sqlite3`), engine, kill switch and risk limits.
- **Isolated failures:** a Coinbase outage, bad config or crash never stops or slows Kalshi,
  and Kalshi problems don't affect Coinbase. If the venue can't start, every
  `/api/coinbase/*` route returns 503 with the reason, and the dashboard shows that reason.
- **Paper only:** it reads Coinbase Exchange's public, unauthenticated REST API
  (`https://api.exchange.coinbase.com`). It has no API keys, no auth code and no code path
  for real orders.
- **Realistic fills:** the paper broker re-reads the live order book at execution and walks
  its depth. Sizes are rounded down to the product's `base_increment` and prices to its
  `quote_increment`. It charges the configured fee tier in USD on every fill. Resting
  orders fill only from later public trades, behind the displayed queue. Positions are
  marked at what selling into the bid ladder would return after the taker fee.

The binding spec is [docs/COINBASE_CONTRACT.md](docs/COINBASE_CONTRACT.md). Verified API and
fee facts are in [docs/coinbase_api_notes.md](docs/coinbase_api_notes.md).

**How venues are labeled in the UI.** Every screen, figure, row, toast and confirm dialog
names its venue. There is never a figure you have to guess about.

- Kalshi is **teal** and Coinbase is **violet**. These colours are never used for P&L or
  status.
- Every venue page starts with a banner naming its venue.
- KPI tiles and table rows carry a venue badge. On phones the badge shrinks to its
  monogram, and the full name stays in the tooltip and for screen readers.
- The top bar has one engine pill per venue.
- The nav groups links under a Kalshi header and a Coinbase header.
- Browser tabs read like "Coinbase · Positions — kalshibot".
- The Overview (`/`) shows one card per venue, a combined total labeled "Sum of two separate
  paper accounts", and an equity chart with one line per venue.
- Units differ by venue:
  - Kalshi: contracts and ¢.
  - Coinbase: base-currency quantities (`0.00029327 BTC`), USD prices and fees shown as
    `$0.23 (0.90%)`.

**Config.** The `coinbase:` section of [`config.example.yaml`](config.example.yaml) documents
every key and its default. A `config.yaml` without that section runs on the defaults, so
existing configs work unchanged. Main keys:

- `enabled`
- `max_rps`: default 3. The public limit is 10 req/s per IP, shared with everything else on
  the host.
- `starting_balance`
- `fee_tier`: default `intro`, the US Intro tier at 0.50% maker / 0.90% taker. You can set
  explicit rates with `fee_rates: {maker, taker}` instead.
- `storage_path`
- `paper.*`: slippage cap, consumed-liquidity TTL, GTC expiry, and `fill_on_book_cross`.
- `engine.*`: `bar_delay_s` (default 60 s after each bar closes), plus maintenance and
  snapshot intervals.
- `risk.*`: per-product and total exposure caps, cash reserve, and the daily loss limit
  (trips the Coinbase kill switch, which blocks buys but still allows sells).
- `strategies.<name>`: `enabled`, `params` and `max_allocation_pct`.

Environment overrides use the prefix `KALSHIBOT_COINBASE__`, for example
`KALSHIBOT_COINBASE__ENABLED=false`. To reset only the Coinbase account, use the dashboard
(`POST /api/coinbase/account/reset`) or `kalshibot coinbase-reset` while the server is
stopped. Kalshi is not affected.

**Backtests.** Coinbase backtests replay `research/coinbase/data` (daily and hourly candles)
through the same strategy classes and rebalance planner as the live engine:

- The strategy decides at the bar close and the order fills at the next bar's open.
- Slippage is half the product's spread (or an estimate from its volume when there is no
  measured spread), plus the taker fee.
- Each fill is capped at 10% of the bar's USD volume.
- Holdings are valued after the exit fee.
- Results are compared with buy-and-hold BTC and an equal-weight universe, both paying the
  same fees.

Run one from the Coinbase Backtests page, or from the command line:

```bash
uv run kalshibot coinbase-backtest --strategy btc_trend --start 2024-01-01
uv run kalshibot coinbase-backtest --strategy btc_trend --fee-tier intro_pre_2026_09 --save
```

The result lists the known biases, including the zero-latency fill at the open.
`--opt fill_price=pessimistic` fills at the worse of the open and the bar's average price.

**Strategies: pending research, none enabled.** Three strategies are registered, and all
three are **off by default**:

- `btc_hold`: a buy-and-hold benchmark.
- `btc_trend`: a BTC trend filter.
- `eth_trend_vt`: experimental.

The research in [research/coinbase/FINDINGS.md](research/coinbase/FINDINGS.md) finds that,
after US retail fees, **no tested strategy beats simply holding BTC**:

- The trend filters are risk overlays, not an edge. They roughly halve drawdowns but add
  no return you can rely on.
- Enable one from the Coinbase Strategies page only to paper-track it. Research is still
  under way.

**Upgrading a Docker install.** The image contains the code. After pulling the Coinbase
changes, run `./deploy.sh update`. Restarting or rebooting reuses the old image, which has
no `/api/overview` or `/api/coinbase/*` routes. If `update` refuses because "a native
'kalshibot serve' is running" and that pid is the container's own server, rebuild directly:

```bash
docker compose build && docker compose up -d --force-recreate
```

## Development

Run the backend and the Vite dev server (hot reload) side by side:

```bash
uv run kalshibot serve --no-engine          # API on :8765; start the engine from the UI when ready
cd frontend && npm run dev                  # http://127.0.0.1:5173, proxies /api (REST + SSE) to :8765
```

- **Other backend port:** `KALSHIBOT_API_URL=http://127.0.0.1:9000 npm run dev`.
- **No backend:** `npm run dev:mock` runs the UI on generated data.
- **More frontend detail:** [frontend/README.md](frontend/README.md).

Tests and checks:

```bash
uv run pytest -q                  # backend (no network: fakes for Kalshi)
cd frontend && npm run build      # type-check (tsc -b) + production build
```

Other CLI commands (`uv run kalshibot --help`):

| Command | Effect |
|---|---|
| `kalshibot serve [--host H] [--port P] [--storage PATH] [--no-engine]` | Run everything |
| `kalshibot reset [--starting-balance X] [-y]` | Wipe the paper account; refuses while `serve` holds the database, so use Settings → Reset on a running server |
| `kalshibot backtest --strategy NAME [--start D] [--end D] [--param K=V] [--fill MODE] [--save]` | Run a backtest from the command line (see [Backtests](#backtests)) |

## Configuration

Settings come from `config.yaml`, or from `--config PATH` / `$KALSHIBOT_CONFIG`.
[`config.example.yaml`](config.example.yaml) documents every key. Sections:

| Section | Holds |
|---|---|
| `kalshi` | API base URL, client-side rate limit `max_rps` (keep it **≤ 3**: the public limit is shared per IP), timeout |
| `account` | `starting_balance` |
| `engine` | `autostart`, job intervals (`tick_s`, `order_poll_s`, …), the Markets-page window `scanner_days_to_close` |
| `paper` | Consumed-liquidity TTL, default resting-order expiry, fee rounding precision, simulated order latency, trade-tape reads per pass |
| `risk` | Per-market / per-event / total / per-strategy exposure limits, cash reserve, order rate (per strategy), daily loss limit (trips the kill switch until the next UTC day), close-time and spread guards, Kelly fraction |
| `strategies` | `<name>: {enabled, params, max_allocation_pct, daily_loss_limit}` (see [Strategies](#strategies)) |
| `analytics` | Readiness thresholds (`min_settled_trades`, per-strategy `min_settled_trades_by_strategy`, `max_drawdown_pct`) |
| `feeds` | External data for strategies (Coinbase/Kraken crypto spot, recently settled Kalshi markets) |
| `server` | Host and port |
| `storage` | SQLite path, relative to the config file |

Any key can be overridden from the environment with `KALSHIBOT_<SECTION>__<KEY>`, for
example `KALSHIBOT_KALSHI__MAX_RPS=2` or `KALSHIBOT_SERVER__PORT=9000`.

At runtime:

- Risk limits and strategy parameters can be changed from the dashboard. The changes are
  stored in the database and override the config.
- Engaging the kill switch blocks new entries and cancels every resting order. The daily
  loss limit engages it automatically and releases it at the next UTC day; a manual kill
  switch stays on until you release it.

## Architecture

```
            Kalshi public REST API (read-only, ≤ 3 req/s)
                          │
              kalshi/client.py  (rate limit, retries)
                          │
   marketdata.py  universe · order books · trades · fee schedule
        │                                    │
   strategies/*  ──intents──▶  risk.py  ──▶  paper/broker.py  ──▶  store.py (SQLite)
        ▲                                    │
        └──────────── engine.py (scheduler, event bus) ─────────┘
                          │
            api/server.py  REST /api/* · SSE /api/stream · serves frontend/dist
                          │
                  frontend/ (React dashboard)
```

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the binding contract (modules, broker
  rules, REST/SSE API, config).
- [docs/COINBASE_CONTRACT.md](docs/COINBASE_CONTRACT.md): the Coinbase venue. It has a
  parallel stack under `kalshibot/coinbase/` (client, market data, spot paper broker, risk,
  bar-based strategies, engine, and `/api/coinbase/*` with SSE at `/api/coinbase/stream`),
  plus `GET /api/overview` across both venues.
- [docs/kalshi_api_notes.md](docs/kalshi_api_notes.md): verified Kalshi API facts
  (formats, fees and fee rounding, lifecycle, rate limits).
- [research/](research/): empirical strategy research (arbitrage, calibration, crypto
  fair value).
- The interactive API docs of a running server are at `/docs`.

## Status

- **Strategies:** four ship and run by default (see [Strategies](#strategies)); only
  `btc15m_favorite` is backed by an out-of-sample result.
- **Universe scans:** the strategies' 3-day market window held 116 pages of 1,000 markets on
  2026-09-27 (26k active; most of the rest are not-yet-open crypto markets closing within a
  day). A full scan reads the window nearest close first, up to `engine.universe_max_pages`
  (150 by default), and costs about a minute of the request budget every 2–3 minutes. A
  `config.yaml` created from the old example pins `universe_max_pages: 100`; with that cap the
  markets closing 1.5–3 days out are not loaded. The first strategy ticks wait for the first
  scan, so trading starts about a minute after `serve`.
- **Markets page categories:** categories come from series metadata that is fetched a
  few series per refresh. Right after a start, some rows show no category for about
  10 minutes.
