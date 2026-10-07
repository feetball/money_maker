# kalshibot — Kalshi trading bot and dashboard

kalshibot runs trading strategies against **live Kalshi market data**. By default it fills
their orders in a **simulated (paper) account**. With `live.enabled` it sends them to Kalshi
as **real orders** (see [Live trading](#live-trading)). It is one Python process: a trading
engine, a REST + Server-Sent-Events API, and a React dashboard to watch and steer it.

> **Paper by default.** Out of the box kalshibot places no real orders and needs no
> credentials; market data comes from Kalshi's public REST API
> (`https://api.elections.kalshi.com/trade-api/v2`). Live trading is opt-in and uses real
> money on the production exchange. Nothing here is investment advice, and good paper
> results do not guarantee live results.

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
- **Health:** the image's `HEALTHCHECK` calls `GET /api/health`, which answers 503 with a plain
  reason when the engine is stopped or its task died, Kalshi is unreachable, the engine has not
  ticked within `max(120 s, 4 × engine.tick_s)` of its last tick or (re)start, or an enabled
  strategy's `on_tick` has raised on every tick for that long. `/api/status` stays 200 in all of
  these cases. A scheduled exchange pause is healthy (`gated: "trading_paused"`). Docker only
  labels the container `unhealthy`; `restart: unless-stopped` does not act on that.

### The btc15m paper run

[`config.paper-run.yaml`](config.paper-run.yaml) is the configuration for a long forward test of
`btc15m_favorite` alone: fixed 50 contracts, `max_spot_age_s: 5`, a $10,000 paper balance, the
profit sweep off, no daily loss stops, the other strategies and the market scanner
off, and every log row kept (`engine.keep_log_rows: 0`). The header of the file says why
each choice matters for the measurement. `deploy/deploy-smol.sh` installs it as `config.yaml`
(keeping the old one as `config.yaml.bak.<time>`), runs `./deploy.sh up`, waits for
`/api/health` and then **checks the running instance**: only `btc15m_favorite` enabled, the
params, no daily stops, sweep off, $10,000 balance. A dashboard toggle, a saved risk limit or an
existing account beat `config.yaml`, so the check reads the live values and prints the `curl`
that fixes each mismatch. `deploy/deploy-smol.sh verify` only checks. What the research data shows, the
power of the run, and its proposed pass/stop rules are in
[`research/paper_run/PREREGISTRATION.md`](research/paper_run/PREREGISTRATION.md);
`research/paper_run/data_review.py` recomputes its tables.

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

The former `/kalshi/*` paths (`/kalshi/positions`, …) redirect to the same page without
the prefix.

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

## Live trading

Live mode sends the same strategies' orders to Kalshi through its authenticated API
([kalshibot/live/broker.py](kalshibot/live/broker.py), [kalshibot/kalshi/trading.py](kalshibot/kalshi/trading.py)).
Everything after a fill (positions, P&L, analytics, risk limits, the kill switch, the dashboard)
works exactly as in paper mode, but from Kalshi's real fills and fees.

**Setup**

1. Create an API key on Kalshi (Account → API keys). Start on the **demo** exchange
   (`demo.kalshi.co`, fake money) with its own key.
2. Turn live mode on in `config.yaml` and restart:
   ```yaml
   live:
     enabled: true
     environment: demo          # prod = real money
     max_order_contracts: 100   # hard per-order caps, checked after the risk limits
     max_order_cost: 100
   ```
3. Add the key in the dashboard: **Settings → Kalshi API keys**. Choose the exchange, enter the
   key ID and upload the `.pem` file (or paste it). The server checks the key against Kalshi
   and only saves it if Kalshi accepts it. Live trading unlocks at once, with no restart.
   Until a key works, the top bar says live trading is **locked**: orders are refused and the
   engine cannot start.
4. The top bar shows **LIVE · DEMO** or **LIVE · REAL MONEY**. The engine stays stopped until
   you press Start (`live.autostart: false`). Review which strategies are enabled and the risk
   limits first.

**Where keys are kept, and how they stay safe**

- **Write-only.** A saved key is never sent back to a browser, never logged and never shown
  again. The dashboard shows only a masked key ID and the public-key fingerprint.
- **Owner-only file.** Keys are written to `live.secrets_path`
  (`data/secrets/kalshi-keys.json`) with mode 0600 in a 0700 directory. `data/` is git-ignored
  and kept out of the Docker image; it is the container's bind mount, so keys survive
  rebuilds. The file is not encrypted: anyone who can read it as the bot's OS user can use
  the key.
- **Requests from other websites are refused.** The key endpoints accept requests only when
  they are addressed to localhost (or a name in `server.allowed_hosts`), have no foreign
  `Origin`, and carry the dashboard's `X-Kalshibot-Request` header. That blocks other
  websites open in your browser, including DNS-rebinding tricks.
- **No login.** The dashboard has none. Anyone who can open it can start the engine and
  trade with the stored key, so keep it bound to `127.0.0.1` (the default).
- **Removing a key** deletes it from the file (refused while the engine runs on it). Revoke
  it on Kalshi too if it may have leaked.

**Alternatives to the dashboard.** You can set `live.api_key_id` plus `live.private_key_path`
(e.g. `keys/kalshi-demo.pem`; `*.pem` and `keys/` are git-ignored), or the env vars
`KALSHIBOT_LIVE__API_KEY_ID` and `KALSHIBOT_LIVE__PRIVATE_KEY_PEM`. A key set this way wins
over the dashboard's. `uv run kalshibot live-check` signs a few read-only requests with
whichever key applies and prints your balance, positions and resting orders.

**How it behaves**

- **Separate ledger.** The live ledger is `live.storage_path` (`data/kalshibot-live.sqlite3`);
  paper history stays where it was. A new ledger starts at your Kalshi balance.
- **Orders.** Taker intents go out as `fill_or_kill` by default (all or nothing; set
  `taker_time_in_force: immediate_or_cancel` for partial fills). Resting intents are
  `good_till_canceled` with an expiration time. Every order is written to the database
  before it is sent, with a unique `client_order_id`. A timed-out submission is never resent:
  the next order pass finds it on Kalshi by that id.
- **Fills** come from `GET /portfolio/fills` at Kalshi's prices and fees. Kalshi matches in
  hundredths of a contract and the ledger counts whole contracts. An `immediate_or_cancel`
  order that ends on a fraction leaves that fraction outside the ledger. It is logged, and
  `live.residue` tracks it.
- **Not supported live:** multi-leg baskets (`no_basket_arb`). Kalshi has no atomic multi-market
  order, so they are rejected rather than legged.
- **Reconciliation.** Every equity snapshot (60 s) compares the ledger with the Kalshi balance
  and positions. A difference shows as a banner and in Settings → Live trading. The bot never
  "fixes" its ledger by itself. After a deposit or withdrawal, press **Sync cash to Kalshi**
  (`POST /api/live/sync-cash`), which books the gap as a transfer so P&L is unchanged.
  Positions you open by hand in the same account show up as mismatches.
- **Reset** (`POST /api/account/reset`) starts a new live ledger at the current Kalshi balance.
  It is refused while the ledger holds positions or open orders. The CLI `reset` refuses in
  live mode.
- **Kill switch** cancels every resting order on Kalshi.

## Configuration

Settings come from `config.yaml`, or from `--config PATH` / `$KALSHIBOT_CONFIG`.
[`config.example.yaml`](config.example.yaml) documents every key. Sections:

| Section | Holds |
|---|---|
| `kalshi` | API base URL, client-side rate limit `max_rps` (keep it **≤ 3**: the public limit is shared per IP), timeout |
| `account` | `starting_balance` |
| `engine` | `autostart`, job intervals (`tick_s`, `order_poll_s`, …), the Markets-page window `scanner_days_to_close`, `keep_log_rows` (rows kept in `logs` and `signals`, default 50,000; 0 = never prune) |
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
  stored in the database and override the config (as do strategy toggles, and, once the
  account exists, its starting balance and profit sweep).
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
