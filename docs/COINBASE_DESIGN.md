# Coinbase Venue: Design Review and Completion Plan (rev. 2)

Status: advisory, revised 2026-09-27 after review (see "Review log" at the end).
`docs/COINBASE_CONTRACT.md` is the binding contract. Where this document disagrees with
it, the contract wins until someone edits it on purpose. Task T0 (section 13) applies the
contract amendments listed in section 12, so that after T0 the two documents agree.
API facts come from `docs/coinbase_api_notes.md`.

PAPER ONLY. The venue uses public market data and a simulated USD account. It never
places real orders and never uses API keys.

---

## 0. Why the Overview still shows the "no /api/overview" banner after a reboot

**Symptom.** The Overview says *"This server has no /api/overview yet, so only the Kalshi
account is shown ... Restart kalshibot serve on the new version to add the Coinbase
account."* The banner stays after a restart and after a reboot.

**Cause.** The Coinbase backend was never written, so no restart can add it. Checked
read-only:

| Check | Result |
|---|---|
| What serves port 8765 | Docker container `kalshibot`, image `kalshibot:latest`, built 2026-09-27 11:24 CDT. Compose has `restart: unless-stopped`, so a reboot starts **the same image** again |
| `curl localhost:8765/api/overview`, `/api/coinbase/status` | both **404**, from the JSON catch-all `/api/{rest:path}` (`kalshibot/api/server.py:814`) |
| `kalshibot/coinbase/{marketdata,engine,services,api}.py`, `kalshibot/api/overview.py` | **do not exist** anywhere |
| `kalshibot/api/server.py` | last changed 08:38, before the Coinbase work; no Coinbase hook |
| Files changed **after** the 11:24 build | `frontend/src/pages/Overview.tsx` and `frontend/src/api/client.ts` (11:32: corrected banner text), `kalshibot/coinbase/backtest.py` and `risk.py` (11:33) |

So the container runs a build that is **older than the source tree**. The banner you see
is the pre-11:32 text ("Restart kalshibot serve on the new version…"), which wrongly
suggests a restart helps. The source already has the corrected wording, but it only
reaches the browser after a rebuild.

**What each action does**

| Action | Effect |
|---|---|
| Restart or reboot | Nothing: the same 11:24 image starts again |
| `./deploy.sh update` **now** | Fixes the banner wording (it ships the 11:32 frontend). Coinbase is still missing |
| Build Phase 1 (section 13), then `./deploy.sh update` | Adds the Coinbase venue and `/api/overview`; the banner disappears |

**The fix is code, then one redeploy.**
1. Build Phase 1 (tasks T0–T3, F1, F2 in section 13).
2. The **user** runs `./deploy.sh update` (it rebuilds the image and recreates the
   container). Build agents never run it: it replaces the live server on port 8765.
3. No hard reload is needed. `index.html` is served `Cache-Control: no-cache`, and the
   client re-checks `/api/overview` every 60 s (`overviewMissingUntil` in
   `frontend/src/api/client.ts`), so an open tab switches over within a minute.
4. Smoke test:
   - `curl -s localhost:8765/api/status | jq '.version, .venues'` returns `"0.2.0"` and
     `["kalshi","coinbase"]`;
   - `curl -s localhost:8765/api/overview | jq '.venues.coinbase.available'` returns `true`;
   - `curl -s -o /dev/null -w '%{http_code}' localhost:8765/api/coinbase/status` returns `200`;
   - `./deploy.sh status` prints a `Kalshi:` line and a `Coinbase:` line.

---

## 1. Package layout: keep the additive `kalshibot/coinbase/` package

Decision (unchanged): one self-contained `kalshibot/coinbase/` package, as the contract
says. No `venues/` abstraction and no refactor of Kalshi code.

Reasons:
- The Kalshi side has 717 passing tests and runs live (paper). A `venues/` refactor would
  touch `engine.py` (1,693 lines), `paper/broker.py` and `store.py`, the files those tests
  protect, for no gain now. The venues differ in kind: binary contracts in ¢ with
  settlement, versus spot quantities in base units with no settlement.
- A shared abstraction can be extracted later, once two working implementations show
  what is actually common.

**Test baseline today:** `uv run pytest --collect-only -q` collects **1,059** tests:
717 Kalshi tests and 342 in the 13 `tests/test_cb_*.py` modules.

**Shared pieces.** Coinbase code only imports these, never modifies them:
`kalshibot.store.ProcessLock` / `StoreLockedError`, `kalshibot.engine.EventBus` (through
the `SpotEventBus` subclass, section 5.2), the helpers in `kalshibot.money`, and
`kalshibot.analytics.drawdown`.

**Not shared** (changed from rev. 1):
- `kalshibot.engine.jsonable` and `kalshibot.api.server.sse` round every Decimal to 4 dp
  (`f4`, `engine.py:136-137`). Coinbase has its own `cb_jsonable` / `cb_sse` (section 5.2).
- `kalshibot.engine.STREAM_EVENT_TYPES` has no `bar`. Coinbase uses its own
  `CB_STREAM_EVENT_TYPES`.
- `_daemon_call` and `_fail_interrupted_backtests` in `server.py` are private. Coinbase
  keeps its own copies (about 25 lines) in `coinbase/api.py` and `coinbase/services.py`,
  so `coinbase/*` never imports `kalshibot.api.server` (that would be a circular import).
- Sharpe: `kalshibot.analytics` has none. The live Coinbase Sharpe is defined in
  `coinbase/analytics.py` (section 9.4).

**File status**

| File | Status | Phase 1 task |
|---|---|---|
| `coinbase/models.py`, `config.py`, `client.py`, `fees.py` | done | `client.py`: additive `get_raw` (T1). `config.py`: `max_rps` default 3 → 2 (T2) |
| `coinbase/paper.py`, `broker.py`, `store.py`, `risk.py` | done | none (unchanged) |
| `coinbase/strategies/{__init__,base}.py`, `rebalance.py`, `backtest.py` | done, 0 strategies registered | none |
| `coinbase/interfaces.py`, `coinbase/events.py` | **new** | T0 |
| `coinbase/marketdata.py`, `coinbase/engine.py` | **new** | T1 |
| `coinbase/services.py`, `coinbase/api.py`, `coinbase/analytics.py` | **new** | T2 |
| `api/overview.py`, `api/looplag.py`, `api/server.py` hook, `cli.py`, `deploy.sh`, version | **new / edit** | T3 |
| frontend shared and Kalshi files | edit | F1 |
| frontend Coinbase files | edit | F2 |
| `coinbase/strategies/<name>.py` | none yet | Phase 2 (S*) |

---

## 2. Paper account (B2 is built): review points

`broker.py` implements the contract's §6: a separate USD account with a $1,000 default and
its own reset; positions per (strategy, product) in base quantity with fee-inclusive
average cost; long only; market-by-quote IOC that walks a fresh L2 book; limit GTC/IOC;
post-only; consumed-liquidity tracking; maker fills only from later public trades after
`queue_ahead`; marks at liquidation value (walking up to `MARK_LEVELS` = 50 bid levels)
and at mid. No settlement. No changes to `broker.py` in Phase 1.

1. **Fee tiers.** `fees.py` is authoritative: `intro` = **0.50% maker / 0.90% taker**
   (US, effective 2026-09-16, label "Intro (US)"). `intro_pre_2026_09` (0.60/1.20) is kept
   for old backtests. A round trip costs **1.80%** taker/taker and **1.00%** maker/maker.
   The contract examples that still say 0.60% / 1.20% are amended by T0 (section 12).
2. **Fee rounding.** The broker charges `fee_for(cumulative notional)` rounded **up** to
   the cent, once per order: deliberately pessimistic. The UI tooltip says "fees rounded
   up to $0.01 per order (pessimistic)".
3. **Off-grid sizes and prices.** The real API rejects them. The broker rounds sizes down
   and prices away from crossing, which is fine because only the planner creates intents.
   Manual orders are out of scope for v1; if added later, reject off-grid values instead.
4. **Price protection.** `paper.max_slippage_bps` (100 = 1%) is tighter than every
   observed Coinbase `max_slippage_percentage` (2% BTC/ETH, 3% most alts), so v1 applies
   only the paper cap and does **not** change `broker.py` or `models.py`. Instead,
   `SpotMarketData.refresh_products()` reads `product.raw.get("max_slippage_percentage")`
   and logs one warning per product whose value × 100 bps is below
   `paper.max_slippage_bps` ("coinbase: BTC-XYZ price protection 0.5% is tighter than the
   paper cap 1.00%"). Enforcing `min()` is a Phase 3 item.
5. **Bandwidth and CPU of books** are handled inside `SpotMarketData.book()`
   (section 6.2). No `mark_book` method exists; the broker keeps calling
   `md.book(pid, max_age_s=…)` as it does today.
6. **Maker-fill latency.** The first page of Exchange `/trades` can be 1–6 s old (CDN), so
   maker fills are detected late, never early: the conservative direction. Poll
   `trades_since` only for products with open resting orders (the broker already does).
7. **Trade side.** Use `maker_side` from REST `/trades`. Never the side from the WS
   `ticker` channel (that is the taker's side).

---

## 3. Strategy interface (B3a is built)

Contract §8 is in place (`SpotStrategy.on_bar(ctx) -> list[TargetWeight] | None`, pure and
synchronous, bar-close decisions, target weights, `param_schema`, `backtestable`,
auto-discovered `REGISTRY`). `rebalance.plan_from_view` / `plan_rebalance` are shared by
the engine and the backtester, so live and backtest runs size orders the same way.

- The registry is **empty**. The engine, API and UI must work with zero strategies:
  status `strategies_enabled: []`, `GET strategies` returns `[]`, the Strategies page
  shows "No Coinbase strategies installed", and the engine still ticks (section 7.3).
- With 1.80% taker round trips, the first strategies should trade rarely: daily bars,
  a no-trade band of 5% or more, `execution = "maker_then_taker"`, and a universe of the
  deepest books (BTC, ETH, SOL, XRP). Every strategy ships `experimental = True` and
  disabled by default, and is enabled only after a backtest with Intro fees beats
  buy-and-hold BTC after fees.
- Each strategy owner (Phase 2) adds `coinbase/strategies/<name>.py` and
  `tests/test_cb_strat_<name>.py` and edits nothing else.

---

## 4. Persistence and restart behaviour

**Separate SQLite file** (built): `data/coinbase.sqlite3` with its own `ProcessLock`
(`data/coinbase.sqlite3.lock`). A migration, lock timeout or corruption on one venue
cannot touch the other; a Coinbase reset can never reach Kalshi rows; no Kalshi schema
change; it lives in the already-mounted `data/`.

**What restores itself already** (do not re-implement):
- the broker: cash, reserved cash, open orders (`queue_ahead`, trade cursors), positions,
  marks (`test_cb_broker_restore.py`);
- the risk manager: kill switch, its reason and auto flag (`kv['risk.kill_switch']`,
  `risk.py:171-176`) and PATCHed limits (`risk_limits` table, `risk.py:157`).

**What the engine persists** (T1), all in the Coinbase store:

| Key | Where | Content |
|---|---|---|
| `last_bar_at` per strategy | `strategy_state.state` JSON: `{"last_bar_at": iso, "strategy": <dump_state()>}` | close time of the last bar acted on |
| maker-then-taker fallbacks | `kv['engine.fallbacks']` | `{order_id: {strategy, product_id, side, reason, signal_id}}` for resting maker orders that need a taker follow-up |
| BTC benchmark reference | `kv['account.btc_ref']` | `{"ts": iso, "price": float}`, section 9.4 |

**Missed bars.** On `start()`, for each enabled strategy with granularity `g`, let
`B = floor(now / g) × g` (the latest closed bar end). If `last_bar_at` is missing or
older than `B`, run the strategy **once** on bar `B` at `max(now, B + bar_delay_s)`, with
`catch_up = true` in the `bar` event and the signals. Earlier missed bars are never
replayed; if more than one was missed, log one `info` line: "coinbase: <name> skipped N
bars while stopped". Example: a reboot at 00:10 UTC with a daily strategy last run on
the previous day's bar acts once on the 00:00 bar, at 00:10.

**Interrupted backtests.** `build_coinbase_services` marks every `backtests` row with
status `running` or `queued` as `failed` with error `"interrupted (server restarted)"` and
`finished_at = now` (its own copy of Kalshi's `_fail_interrupted_backtests`).

**Lock conflict.** If `coinbase.sqlite3.lock` is held, `build_coinbase_services` catches
`StoreLockedError` and raises `CoinbaseUnavailable("store locked by pid N (pid as seen by
the holding process; under Docker usually the kalshibot container). Stop that process,
or reset from its dashboard: POST /api/coinbase/account/reset")`. Only Coinbase becomes
unavailable; Kalshi starts normally. `store.py` itself is not changed.

---

## 5. Interfaces fixed before parallel work (task T0)

The repo is **not a git repository**. All owners write into the same working tree, so
there is no merge step. Instead:
1. T0 writes `coinbase/interfaces.py`, `coinbase/events.py` and
   `tests/cb_service_fakes.py` **first**, alone.
2. T1, T2, T3, F1 and F2 then run in parallel. Each touches only the paths it owns
   (section 13). T2 and T3 build and test against the Protocols and fakes from T0, never
   against T1's files while T1 is still writing them.
3. T9 (integration) runs after all of them.

Before T1–T3 start, T0 takes a baseline copy of the tree **outside the repo** (so any
owner can diff their own edits):
`tar czf /root/money_maker_pre_cb_phase1.tgz --exclude=./data --exclude=./frontend/node_modules --exclude=./frontend/dist --exclude=./research --exclude='__pycache__' -C /root/money_maker .`
(a few MB; check `df` first).

### 5.1 `kalshibot/coinbase/interfaces.py` (T0)

```python
from __future__ import annotations
import asyncio, contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from kalshibot.coinbase.models import OrderBook, Product, Stats, Candle, Trade

VENUE = "coinbase"
#: detail prefix for every 503 of /api/coinbase/* (must contain lowercase "coinbase")
UNAVAILABLE_PREFIX = "coinbase venue unavailable: "
NOT_CONFIGURED = "not configured in this app"   # reason when tests inject Kalshi services

class CoinbaseUnavailable(RuntimeError):
    """The venue cannot be built. ``reason`` is shown to users verbatim."""
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

@runtime_checkable
class SpotMarketDataLike(Protocol):
    clock: Callable[[], datetime]
    reachable: bool | None           # None = not probed yet
    last_error: str | None
    last_error_at: datetime | None
    def products(self) -> Mapping[str, Product]: ...            # cached, sync, {} before first load
    async def refresh_products(self) -> dict[str, Product]: ...
    async def product(self, product_id: str) -> Product: ...
    async def book(self, product_id: str, max_age_s: float = 2.0) -> OrderBook: ...
    async def trades_since(self, product_id: str, since_trade_id: int | None) -> list[Trade]: ...
    async def candles(self, product_id: str, granularity_s: int, n: int, *, end: datetime) -> list[Candle]: ...
    def stats(self, product_id: str) -> Stats | None: ...          # from the scanner cache
    def touch_scanner(self) -> None: ...                          # section 6.4
    async def scanner_ready(self, timeout: float) -> bool: ...
    def scanner_rows(self, *, search: str = "", sort: str = "volume", limit: int = 100) -> list[dict[str, Any]]: ...
    def status(self) -> dict[str, Any]: ...
    async def aclose(self) -> None: ...

@runtime_checkable
class CoinbaseEngineLike(Protocol):
    running: bool
    current_task: str | None         # name of the supervised task step running right now
    async def start(self) -> None: ...
    async def stop(self, timeout: float = 10.0) -> None: ...
    async def close(self) -> None: ...
    async def set_kill_switch(self, on: bool, reason: str = "manual") -> None: ...
    def status(self) -> dict[str, Any]: ...                       # contract §13 engine object
    def strategies_json(self) -> list[dict[str, Any]]: ...
    async def update_strategy(self, name: str, *, enabled: bool | None = None,
                              params: Mapping[str, Any] | None = None) -> dict[str, Any]: ...
        # KeyError: unknown name -> 404; ParamError: bad params -> 422
    async def reset(self, starting_balance: Any = None) -> Any: ...  # -> SpotAccountState; engine left stopped
    def log(self, level: str, kind: str, message: str, **data: Any) -> None: ...

@dataclass
class CoinbaseServices:
    settings: Any                    # kalshibot.config.Settings
    store: Any                       # SpotStore
    client: Any                      # CoinbaseClient
    md: SpotMarketDataLike
    broker: Any                      # SpotPaperBroker
    risk: Any                        # SpotRiskManager
    engine: CoinbaseEngineLike
    bus: Any                         # SpotEventBus
    backtests: dict[int, asyncio.Task[Any]] = field(default_factory=dict)
    owns_resources: bool = True

    async def aclose(self) -> None:
        """Engine first, then backtests, market data, client, store. Never raises."""
        with contextlib.suppress(Exception):
            await self.engine.close()
        for t in list(self.backtests.values()):
            t.cancel()
        with contextlib.suppress(Exception):
            await self.md.aclose()
        if self.owns_resources:
            with contextlib.suppress(Exception):
                await self.client.aclose()
            with contextlib.suppress(Exception):
                self.store.close()
```

`build_coinbase_services` is declared here only as a docstring reference; T2 implements it
in `services.py` with exactly this signature:

```python
def build_coinbase_services(settings: Settings, *, client: CoinbaseClient | None = None,
                            store: SpotStore | None = None,
                            strategies: Mapping[str, type[SpotStrategy]] | None = None,
                            clock: Callable[[], datetime] | None = None) -> CoinbaseServices
```

- **Synchronous. No network I/O.** It opens the store (`exclusive=True` unless a store is
  passed), restores the broker, fails interrupted backtests and returns. The first
  products fetch happens in the engine's `products` task.
- Raises `CoinbaseUnavailable(reason)` when the venue should be off:
  - `"disabled in config"` when `settings.coinbase.enabled` is False and there is no
    `load_error`;
  - `settings.coinbase.load_error` verbatim (for example `"invalid coinbase config: …"`);
  - the store-locked message from section 4.
- Any other exception propagates; the server turns it into `"<Type>: <message>"`.
- Pytest guard: if the environment has `PYTEST_CURRENT_TEST` and the resolved
  `storage_path` is inside `<repo>/data/`, raise
  `CoinbaseUnavailable("refusing to open the production data/ directory under pytest")`.

### 5.2 `kalshibot/coinbase/events.py` (T0)

```python
CB_STREAM_EVENT_TYPES: tuple[str, ...] = ("tick", "bar", "signal", "order", "fill", "log", "account")
REPLAY_MAX = 500

def cb_jsonable(x: Any) -> Any:
    """Like kalshibot.engine.jsonable but Decimal -> f8 (8 dp, via coinbase.paper.f8),
    datetime -> ISO Z (coinbase.paper.iso), NaN/inf -> None; recurses into
    Mapping / list / tuple / dataclass (dataclasses.asdict) / objects with to_json()."""

class SpotEventBus(EventBus):
    """kalshibot.engine.EventBus with 8-dp encoding and the venue tag."""
    def __init__(self, maxsize: int = 1000, history: int = REPLAY_MAX) -> None: ...
    def publish(self, type_: str, data: Any) -> None:
        d = cb_jsonable(data)
        if isinstance(d, dict):
            d.setdefault("venue", VENUE)
        super().publish(type_, d)    # jsonable() now sees only floats/str/None: no rounding

def cb_sse(event: str, data: Any, event_id: int | None = None) -> str:
    """One SSE message; json.dumps(cb_jsonable(data), separators=(",", ":"), allow_nan=False)."""
```

Rule: Coinbase code publishes only through `SpotEventBus` and encodes SSE only through
`cb_sse`. It never calls `kalshibot.engine.jsonable` or `kalshibot.api.server.sse`.

### 5.3 `tests/cb_service_fakes.py` (T0)

A helper module (not collected; imported as `from cb_service_fakes import ...`, like
`test_cb_broker_fakes.py`):
- `FakeSpotMarketData` implements `SpotMarketDataLike` from in-memory dicts
  (`set_product`, `set_book`, `add_trades`, `set_candles`, `set_stats`,
  `fail_next(exc)`), with a call log for assertions.
- `FakeCoinbaseEngine` implements `CoinbaseEngineLike` (records calls; `status()`
  returns every contract field).
- `make_fake_cb_services(tmp_path, *, settings=None, clock=None) -> CoinbaseServices`
  builds a real `SpotStore(tmp_path / "cb.sqlite3")`, a real `SpotPaperBroker` over
  `FakeSpotMarketData`, a real `SpotRiskManager`, a `SpotEventBus` and a
  `FakeCoinbaseEngine`. No network, nothing under `data/`.

---

## 6. Market data (`coinbase/marketdata.py`, task T1)

### 6.1 Constructor and client

```python
class SpotMarketData:
    def __init__(self, client: CoinbaseClient, settings: Any, *,
                 clock: Callable[[], datetime] | None = None,
                 mono: Callable[[], float] = time.monotonic) -> None
```

`settings` is a full `Settings` or a `CoinbaseSettings`. `self.clock` defaults to
`lambda: datetime.now(UTC)`; the broker picks it up through `md.clock`. It implements
every member of `SpotMarketDataLike`.

T1 adds one **additive** method to `coinbase/client.py`, so large bodies can be decoded
off the event loop:

```python
async def get_raw(self, path: str, params: Mapping[str, Any] | None = None) -> tuple[bytes, httpx.Headers]
```

It uses the same token bucket, retries, counters and error mapping as `request()`, but
returns `resp.content` without decoding. Implement it by moving the retry loop of
`request()` into a private `_send(path, params) -> httpx.Response`, which both methods
call. The behaviour of every existing method is unchanged, and `tests/test_cb_client.py`
passes unedited.

### 6.2 `book(pid, max_age_s=2.0)`: the one place books are fetched

The broker calls `book(pid, max_age_s=2)` for execution and maintenance
(`broker.py:889,1225`) and `book(pid, max_age_s=10)` for marks (`broker.py:1349`). The
rules below cut bandwidth and loop time without touching `broker.py`.

| Call | Served from |
|---|---|
| `max_age_s < 10` (execution, maintain) | the trimmed L2 cache if it is younger than `max_age_s`; otherwise a fresh `level=2` fetch (single-flight per product: concurrent callers await the same fetch) |
| `max_age_s >= 10` (marks) | the L2 cache if it is younger than `max_age_s`. Otherwise a fresh `level=1` fetch (about 200 bytes), **merged** with the depth of the last L2 book if that is younger than `MARK_L2_MAX_AGE_S = 300`. If there is no such L2, and the last L2 fetch for this product was 300 s ago or more, fetch a fresh L2 instead |

**Merged mark book:** top level from the fresh L1; below it, the cached L2 bid levels with
price < L1 best bid and ask levels with price > L1 best ask; `time` = the L1 time.

Why not a bare L1 book for marks: the broker reconciles consumed liquidity against every
book it sees (`_reconcile`, `broker.py:590-599`). A level that is missing from the book
counts as displayed size 0, and its consumed entry is deleted. A bare L1 book would
therefore erase consumed-liquidity tracking for every deeper level each minute. The
merged book keeps those levels at their last known size.

**Trim and decode off the loop.** A fresh L2 is fetched with `client.get_raw(...)`, then
`await asyncio.to_thread(_parse_book, pid, body, trim_bps, max_levels)`:
- `json.loads` the body;
- keep bids while `float(price) >= best_bid × (1 − trim_bps / 10⁴)` and asks while
  `float(price) <= best_ask × (1 + trim_bps / 10⁴)`, at most `max_levels` per side, but
  always at least 50 per side (`MARK_LEVELS`) when the book has them;
- build `OrderBook.from_api(pid, trimmed_dict)` from the kept rows only.

Defaults: `trim_bps = paper.max_slippage_bps + 200` (300 with the default 100) and
`max_levels = 1000`. Market orders cannot walk past `max_slippage_bps`, so the trimmed
book holds everything the broker can consume. A limit IOC priced beyond the window
underfills: the conservative direction.

Measured on a BTC-sized book (41.7k levels): `OrderBook.from_api` on the full book took
about 83 ms; trimmed to 1,000 levels per side, about 7 ms; `json.loads` about 4 ms per
355 KB. Both run in the worker thread, so the loop is never blocked for more than a GIL
switch interval.

**Bandwidth bound.** One BTC L2 is about 355 KB. Execution: one L2 per order. Maintain:
one L2 every 15 s per product **with a resting order**. Resting orders live at most
`maker_timeout_s` (120 s, maker-then-taker) or `default_gtc_expiry_s` (3600 s), so that is
at most about 85 MB per resting-order-hour. Marks: 1,440 L1 fetches a day (about 0.3 MB)
plus at most one L2 every 5 minutes per held product (about 100 MB/day for BTC). Section
7.5 has the request budget.

### 6.3 Other methods

- `refresh_products()`: `GET /products` (Exchange), keep every product; `products()`
  returns the cached map (`{}` before the first load). Logs the price-protection warning
  (section 2, item 4) and marks products that became `delisted` or `trading_disabled`.
- `product(pid)`: cached; on a miss, `GET /products/{pid}` (404 raises `CoinbaseNotFound`).
- `trades_since(pid, since_trade_id)`: `client.trades_since(pid, since_trade_id)`; no
  cache (the broker owns the cursors).
- `candles(pid, g, n, *, end)`: an in-memory append-only cache per `(pid, g)` of
  **closed** bars (`candle.start + g <= end`), oldest first, no duplicates. The first call
  fetches `n` bars; later calls fetch only bars after the last cached one. Cap: `max(n,
  history_bars) + 10` bars per key.
- `reachable` / `last_error` / `last_error_at`: `True` after any successful request,
  `False` after a request that failed after retries, `None` before the first request.
- `status()`: `{reachable, last_error, last_error_at, products_loaded, requests,
  retries, l2_fetches, l1_fetches, scanner_active, scanner_last_refresh}`.
- `aclose()`: cancel the scanner task. Does **not** close the client (services owns it).

### 6.4 Market scanner (`GET /api/coinbase/products`)

`/products/stats` has no bid or ask, and per-product ticker calls are not allowed, so the
scanner is a lazily started background task owned by `SpotMarketData`, independent of the
engine running:

- `touch_scanner()` records `last_touch = mono()` and starts the task if it is not
  running. The products route calls it on every request.
- The task stops by itself when `mono() − last_touch > 300` s.
- Every 60 s: `GET /products/stats` (one call, about 109 KB, fetched with `get_raw` and
  parsed in `to_thread`). Each entry becomes `Stats.from_api(pid, {**e["stats_24hour"],
  "volume_30day": e["stats_30day"]["volume"]})`. The product list comes from the hourly
  `products()` cache, not a refetch.
- Level-1 quotes: round-robin over the top `SCANNER_L1_TOP_N = 40` USD products by 24 h USD
  volume, one `level=1` book every 2 s (0.5 req/s), so each quote is refreshed about every
  80 s. Other products get `bid = ask = spread_bps = null`.
- `scanner_ready(timeout)` waits (without making a request) until the first stats refresh
  has finished; the route waits at most 5 s.
- `scanner_rows(search, sort, limit)` is a **sync** read of the cache:
  - rows only for products with `quote_currency == "USD"` and `status != "delisted"`;
  - `search`: case-insensitive substring of `product_id`, `base_currency` or
    `display_name`;
  - `sort`: `volume` (desc), `spread` (asc, nulls last), `change` (desc, nulls last);
  - `limit` is 1–500, default 100.
- Row fields: the contract's list (`venue, product_id, base_currency, price, bid, ask,
  spread_bps, change_24h_pct, volume_24h_usd, tradable, url`) plus the extra
  `quote_age_s`. `price` = `stats.last`, `change_24h_pct` = `(last / open − 1) × 100`,
  `volume_24h_usd` = `volume_24h × last`.
- **No request handler ever makes an upstream call.**

---

## 7. Engine (`coinbase/engine.py`, task T1)

### 7.1 Constructor

```python
class CoinbaseEngine:
    def __init__(self, settings: Any, md: SpotMarketDataLike, broker: SpotPaperBroker,
                 risk: SpotRiskManager, store: SpotStore,
                 strategies: Mapping[str, type[SpotStrategy]] | None = None, *,
                 bus: SpotEventBus, clock: Callable[[], datetime] | None = None) -> None
```

`strategies=None` means `kalshibot.coinbase.strategies.REGISTRY`. It implements
`CoinbaseEngineLike`. Enabled state and params are resolved with the existing helpers
(`resolve_enabled`, `resolve_strategy_params`, `allocation_pct` in `strategies/base.py`)
from class defaults, `coinbase.strategies.<name>` config and `strategy_state`.
Dashboard toggles win.

### 7.2 Supervised tasks

`start()` launches one asyncio task per row below, each wrapped in a supervisor. The
supervisor sets `self.current_task = name` around each step, catches and logs any
exception (store log plus a `log` event), backs off 1 s, 2 s, 4 s … up to 60 s, and
loops again. One failing task never ends another. `stop(timeout)` cancels them and
waits; `close()` = `stop()` plus detaching log handlers.

| Task | Period | Work |
|---|---|---|
| `tick` | `engine.snapshot_s` (60 s); **runs with zero strategies and zero positions** | `broker.mark()` (no request when nothing is held), `broker.equity_snapshot()`, `risk.evaluate(account)`, refresh the BTC reference price (one L1 `BTC-USD` book, `max_age_s=10`), write `kv['account.btc_ref']` if it is missing. Then `last_tick_at = now`, `tick_count += 1`, publish `tick` and `account` |
| `products` | `engine.products_refresh_s` (3600 s), first run right away | `md.refresh_products()`; `products_loaded = len(...)` |
| `bars` | sleeps until the next `bar_end + bar_delay_s` over all enabled strategies | section 7.4 |
| `maintain` | `engine.maintenance_s` (15 s), and only while `broker.open_orders()` is non-empty | `broker.maintain()`; publish `fill` / `order` / `account`; handle maker-then-taker fallbacks (section 7.4) |
| `housekeeping` | 3600 s | `store.prune("logs", 20000)`, `store.prune("signals", 20000)`, `store.downsample_equity(older_than=now − 7 d, bucket_s=3600)` |

A **tick** is one completed run of the `tick` task. The UI shows "Running · stalled"
when `last_tick_at` is more than 5 minutes old (`pages/coinbase/shared.tsx:215,250`).
With a 60 s tick that only happens when the engine is really stuck. An upstream outage
does not stall it: `broker.mark()` logs per-product book failures and completes, and the
pill shows "Running · Coinbase API down" from `coinbase_reachable = false`.

### 7.3 `status()`

Contract §13 engine object, all keys always present:
`running, started_at, last_tick_at, last_bar_at, tick_count, products_loaded, last_error,
last_error_at, kill_switch, kill_switch_reason, coinbase_reachable, strategies_enabled`.
Extras: `current_task`, and `tasks: {name: {runs, failures, last_run, last_duration_s,
last_error, next_in_s}}`, the same shape as Kalshi's `jobs`. `coinbase_reachable` =
`md.reachable`; `kill_switch*` come from `risk`. The API adds `loop_lag_ms` (section 8.4).

### 7.4 Bar pipeline

For each enabled strategy whose bar `B` closed (and `last_bar_at < B`):
1. Universe: `strategy.universe({pid: p for tradable USD products})`.
2. Candles: `md.candles(pid, g, strategy.history_bars, end=B)` per universe product
   (sequentially, sharing the token bucket). A product whose fetch fails is left out of
   `ctx.products` for this bar, and a `warning` is logged.
3. `LiveSpotContext` (defined in `engine.py`, satisfies `SpotContext`): `now`, `bar_end=B`,
   `products`, `params`, `portfolio = broker.portfolio_view(name)`, `candles()` from the
   prefetched lists, `stats()` from `md.stats()`, `log()` into the store and the bus.
4. `targets = await asyncio.wait_for(asyncio.to_thread(strategy.on_bar, ctx), 30)`. A
   timeout or exception: log `error`, record `last_error` for the strategy, set
   `last_bar_at = B` (never retry the same bar), move to the next strategy.
5. Prices: mid of `md.book(pid, max_age_s=10)` for each product in targets plus holdings.
   `plan = plan_from_view(targets, view, products, band=strategy.rebalance_band,
   min_trade_usd=risk.limits.min_trade_usd, strategy=name, fee_rate=tier.taker_rate,
   prices=mids)`.
6. For each intent in `plan.sells` then `plan.buys`:
   - `risk.check(intent, view, account, spread_bps=<from L1>)`;
   - rejected: signal with `decision="rejected"`;
   - approved: `intent = decision.apply(intent)`. For `execution == "maker_then_taker"`,
     turn it into a post-only limit GTC at best bid (buy) or best ask (sell) with
     `expires_in_s = engine.maker_timeout_s`, and record it in `kv['engine.fallbacks']`;
   - `order = await broker.place_order(intent)`;
   - signal with decision `executed` / `partial` / `unfilled` / `resting` /
     `rejected` from `order.decision`, and `order_id`;
   - publish `signal`, `order`, and `fill` for each new fill.
7. `store.save_strategy_state(name, state={"last_bar_at": iso(B), "strategy":
   strategy.dump_state()})`, then publish `bar` with `{ts, strategy, bar_end, granularity_s,
   products, intents, catch_up, duration_ms}`.

**Maker-then-taker fallback** (in `maintain`): when a fallback order leaves `open` with
remaining size (expired or cancelled), re-check risk and place a market IOC for the
remainder (`quote_size` for buys = remaining × best ask × (1 + taker rate); `base_size`
for sells), then remove the kv entry. Under the kill switch, only sells fall back.

**Kill switch.** `set_kill_switch(on, reason)` calls `risk.set_kill_switch(on, reason)`.
When turning it on, it also cancels every open **buy** order (`broker.cancel_order(id,
"kill switch")`). Sells stay allowed. Publish `account` and a `log` event.

**Reset.** `reset(starting_balance)` = `stop()`, cancel open orders,
`broker.reset(starting_balance)`, `risk.reset()`, clear `last_bar_at` in every
`strategy_state`, delete `kv['engine.fallbacks']`, set `kv['account.btc_ref']` to the
current BTC mid (or delete it when no book is available; the next tick writes it).
Publish `account`. The engine stays stopped, as Kalshi's does.

**Logs to the stream.** `start()` attaches a `logging.Handler` at INFO to the loggers
`kalshibot.coinbase.broker` and `kalshibot.coinbase.risk`. It publishes each record as a
`log` event only; they already write their own store rows.

### 7.5 Rate budget (one host IP)

| Client | Cap | Typical |
|---|---|---|
| Coinbase venue (`CoinbaseClient`, one token bucket for everything in 6.x and 7.x) | **`coinbase.max_rps` default 2** (changed from 3) | under 0.1 req/s idle; 0.5 req/s more while the Markets page is open |
| Kalshi `crypto` feed (`feeds/crypto.py:117`, own bucket, lazy) | 3 req/s | under 1 req/s, only while a strategy that uses it runs |
| **Sum from this process** | **≤ 5 req/s** worst case | about 1 req/s |

5 req/s is the budget in the API notes, half the Exchange's 10 req/s per IP. The two
clients stay separate, and the Kalshi feed is untouched. Other agents on the host share
the IP, so do not raise the venue cap above 2 without checking. The client already backs
off on 429.

---

## 8. Server integration (task T3)

### 8.1 `create_app` signature (additive)

```python
def create_app(settings: Settings | None = None, *, services: AppServices | None = None,
               autostart: bool | None = None, frontend_dist: Path | None = None,
               cb_services: CoinbaseServices | None | Literal["auto"] = "auto") -> FastAPI
```

| `services` | `cb_services` | Coinbase in this app |
|---|---|---|
| `None` (production, `kalshibot serve`) | `"auto"` | `build_coinbase_services(settings)`; on failure unavailable with its reason |
| injected (every existing test) | `"auto"` | **not built**; unavailable, reason `"not configured in this app"` |
| any | `None` | unavailable, `"not configured in this app"` |
| any | a `CoinbaseServices` | used as given (new tests) |

So the existing tests, which all pass `services=`, never open `data/coinbase.sqlite3`,
never take its lock and never call Coinbase.

### 8.2 Lifespan (additions after the existing Kalshi code, before `yield`)

```python
app.state.cb, app.state.cb_error = None, None
if cb_services == "auto" and services is None:
    try:
        from kalshibot.coinbase.services import build_coinbase_services
        app.state.cb = build_coinbase_services(settings)
    except CoinbaseUnavailable as e:        # imported inside the same try
        app.state.cb_error = e.reason
    except Exception as e:                  # import error, bug: Kalshi continues
        app.state.cb_error = f"{type(e).__name__}: {e}"
        log.exception("coinbase venue unavailable")
elif cb_services in ("auto", None):
    app.state.cb_error = NOT_CONFIGURED
else:
    app.state.cb = cb_services
cb = app.state.cb
if cb is not None:
    cb.bus.bind(asyncio.get_running_loop())
    if autostart is not False and settings.coinbase.engine.autostart:
        try:
            await asyncio.wait_for(cb.engine.start(), 5)
        except Exception as e:
            log.warning("coinbase engine did not start: %s", e)   # venue stays available, engine stopped
app.state.loop_lag = LoopLagProbe(on_lag=_lag_warning)    # api/looplag.py
app.state.loop_lag.start()
```

On shutdown, in the existing `finally`, after `await svc.aclose()` (Kalshi first):
`app.state.loop_lag.stop()`, then
`with suppress(Exception): await asyncio.wait_for(cb.aclose(), 10)`. A stuck Coinbase
shutdown cannot stop Kalshi from saving its state.

`autostart` rule: the Coinbase engine starts only when the `autostart` argument is not
`False` **and** `coinbase.engine.autostart` is true. `kalshibot serve --no-engine`
therefore starts neither engine. Kalshi's `engine.autostart` setting governs only Kalshi.

### 8.3 Routers (inside `create_app`, **before** the `/api/{rest:path}` catch-all)

```python
try:
    from kalshibot.coinbase.api import router as coinbase_router
except Exception as e:                      # import error -> every /api/coinbase/* is a 503
    coinbase_router = _cb_unavailable_router(f"import error: {type(e).__name__}: {e}")
app.include_router(coinbase_router, prefix="/api/coinbase")
try:
    from kalshibot.api.overview import router as overview_router
    app.include_router(overview_router)     # GET /api/overview
except Exception:
    log.exception("GET /api/overview unavailable")
```

`_cb_unavailable_router(reason)` is defined in `server.py` (about 8 lines): one
`api_route("/{rest:path}")` for all methods that returns 503
`{"detail": "coinbase venue unavailable: " + reason}`.

### 8.4 Other `server.py` edits (all additive)

- `/api/status` adds `"venues": ["kalshi", "coinbase"]`. This is the list of venues **this
  build contains**; availability is in `/api/overview`. It is allowed by
  `StatusResponse(_Out, extra="allow")`. `status_payload` gets the key, so the engine
  start/stop responses carry it too.
- `__version__` becomes `"0.2.0"` in `kalshibot/__init__.py` and `pyproject.toml`.
- **Loop-lag probe** (`kalshibot/api/looplag.py`, new):
  `LoopLagProbe(interval_s=1.0, warn_ms=250, on_lag=None)` with `start()`, `stop()`,
  `last_ms: float | None` and `max_ms(window_s=300) -> float | None`. It sleeps
  `interval_s` and measures the overshoot. `on_lag(ms)` is called when the overshoot is
  above `warn_ms`. `_lag_warning` logs `"event loop lag %d ms (coinbase task: %s)"` with
  `app.state.cb.engine.current_task` when Coinbase is up. Exposed as
  `/api/overview.server.loop_lag_ms` and as `loop_lag_ms` in `/api/coinbase/status`.
  Kalshi's `status_payload` is **not** changed for this.
- No other Kalshi route, schema or shape changes.

### 8.5 CLI (`kalshibot/cli.py`) and `deploy.sh`

- `serve`: after the existing Kalshi lock pre-check, if `coinbase.enabled` and the path is
  a file path, try `ProcessLock(settings.coinbase.storage_path).acquire().release()`. On
  `StoreLockedError`, log a **warning** ("Coinbase store is locked (…); the Coinbase
  venue will be unavailable, Kalshi starts normally") and continue. Never fatal.
- `reset`: the prompt becomes "Wipe the **Kalshi** paper account in {path} and restart at
  ${start}? [y/N]". Nothing else changes.
- New `coinbase-reset [--starting-balance X] [--yes]`: opens
  `SpotStore(path, exclusive=True)`, confirms with "Wipe the **Coinbase** paper account in
  {path} and restart at ${start}? [y/N]", runs
  `SpotPaperBroker(_NoMarketData(), store, settings=settings).reset(start)` (sync;
  `_NoMarketData` is a 6-line class in `cli.py` whose methods raise) and
  `SpotRiskManager(settings, store=store).reset()`, and
  deletes `kv['account.btc_ref']` and `kv['engine.fallbacks']`. On `StoreLockedError` it
  prints "The Coinbase paper store {path} is in use by a running kalshibot server (pid N
  as seen by that process; under Docker, the kalshibot container). Reset from the
  dashboard (Coinbase → Settings) or `POST /api/coinbase/account/reset`." and exits 1.
- New `coinbase-backtest --strategy NAME [--start D] [--end D] [--fee-tier T]
  [--starting-balance X] [--json PATH]`: calls `run_spot_backtest` and prints the metrics
  table. It opens no store.
- `deploy.sh status_line` reads `/api/status` and `/api/overview` and prints:

  ```
    Kalshi:   engine running=True ticks=812 markets=143 kill_switch=False equity=$1,003.12
    Coinbase: engine running=True ticks=41 kill_switch=False equity=$1,000.00 reachable=True
  ```

  If Coinbase is unavailable: `  Coinbase: unavailable (<unavailable_reason>)`. If
  `/api/overview` returns 404: `  Coinbase: not in this build (GET /api/overview 404; run
  ./deploy.sh update after building the Coinbase venue)`. `strategies:` and `last error:`
  lines follow per venue.

---

## 9. REST API (`coinbase/api.py`, `coinbase/analytics.py`, task T2; `api/overview.py`, task T3)

### 9.1 General rules for `/api/coinbase/*`

- Exactly the contract §13 table, with the additions below. `router = APIRouter()` is
  module-level in `coinbase/api.py`. Pydantic response models (`extra="allow"`) live in
  `coinbase/api.py`, not in `api/schemas.py`.
- Dependency `get_cb(request) -> CoinbaseServices`: if `request.app.state.cb` is None,
  raise `HTTPException(503, "coinbase venue unavailable: " + (cb_error or "unknown"))`.
  **Every 503 detail starts with `"coinbase venue unavailable: "`**; the client needs the
  lowercase word "coinbase" (`api/coinbase/client.ts:134`).
- Any unexpected exception inside a route is caught by the router and returned as 500
  `{"detail": "coinbase: <Type>: <message>"}`. It never propagates to Kalshi.
- Every top-level object and every row has `"venue": "coinbase"`. Numbers are floats with
  up to 8 dp (use the models' `to_json()` / `f8`); timestamps are ISO-8601 with `Z`.
- Routes only read caches, the store and the broker. They never call Coinbase directly.

### 9.2 Additions to the contract shapes

| Route | Addition |
|---|---|
| `GET status` (also engine start/stop/kill-switch) | `fee_tier: {name, label, maker_rate, taker_rate}` from `FeeTier.as_dict()`; `fee_tiers: [...]` = `as_dict()` of every `FEE_TIERS` entry (plus `custom` if `fee_rates` is set); `engine.loop_lag_ms`; `engine.current_task`; `engine.tasks`; `version` |
| `POST engine/kill-switch` | body `{on: bool, reason?: str}` |
| `POST account/reset` | stops the engine and resets (section 7.4); returns the account |
| `GET products` | calls `md.touch_scanner()`, waits `md.scanner_ready(5)`, returns `md.scanner_rows(...)`; row extra `quote_age_s` |
| `PATCH strategies/{name}` | 404 unknown name; 422 bad params (`ParamError`) |
| `POST backtests` | runs `run_spot_backtest` in a daemon thread (its own `_daemon_call` copy); tasks kept in `cb.backtests`; 422 unknown strategy or `backtestable = False` |
| `GET backtests/{id}` | 404 unknown |

### 9.3 `GET stream`

Same wire behaviour as Kalshi's `/api/stream`, with its own bus:
- query `max_events` (≥ 1), `duration` (> 0 s), `replay` (0–500); header `Last-Event-ID`;
- order on the wire: `retry: 3000`, the backlog from `cb.bus.replay(after=, last=)`, an
  `account` greeting (`cb.broker.account().to_json()`, with an `id:` only on a plain
  connection), then live events and a `: keepalive` every 15 s;
- ends when `request.app.state.stopping` is set, when `duration` passes, or when
  `max_events` have been sent;
- only types in `CB_STREAM_EVENT_TYPES` (includes `bar`), encoded with `cb_sse`;
- returns 503 before streaming if Coinbase is unavailable.

The client reconnects with `?replay=500` (`api/coinbase/stream.ts:56,259`), which is why
`SpotEventBus` keeps `history=500`.

Event data (every object also has `venue`):

| Type | Fields |
|---|---|
| `tick` | `ts, tick_count, products_loaded, equity, cash, open_positions, open_orders, kill_switch, coinbase_reachable` |
| `bar` | `ts, strategy, bar_end, granularity_s, products, intents, catch_up, duration_ms` |
| `signal` / `order` / `fill` / `account` | the same row shapes as the REST routes |
| `log` | `id, ts, level, kind, message, data` |

### 9.4 `GET analytics` (`coinbase/analytics.py`)

`compute_cb_analytics(store, broker, risk, settings, *, now, btc_price) -> dict`, cached in
the route for 60 s.

- `overall.trades` = **closed trades** = finished sell orders with fills
  (`store.trade_counts()`); `win_rate` = wins / trades (realized P&L > 0). This is the
  same definition as `account.trades` / `win_rate`.
- `overall.total_pnl`, `return_pct`, `max_drawdown_pct`, `fees` come from
  `broker.account()`.
- `overall.turnover` = sum of fill notional since the reset ÷ starting balance.
- `overall.sharpe`: last equity snapshot per UTC day; daily simple returns r;
  `mean(r) / stdev(r, ddof=1) × √365`. `null` below 7 daily points or when stdev = 0.
  This is the same annualisation as `coinbase/backtest.py:1044`.
- `by_strategy.{name}`: `store.strategy_summary()` plus the same trade and win-rate
  definitions per strategy.
- `benchmark`: `{btc_buy_hold_return_pct, since, btc_ref_price, btc_price}` with `since` =
  `kv['account.btc_ref'].ts` and
  `btc_buy_hold_return_pct = ((btc_price / ref_price) × (1 − taker_rate)² − 1) × 100`
  (buy and sell as taker). `null` fields until the first tick writes the reference.
- `readiness: {ready, reasons, criteria}`. The Kalshi rules (settlements, Brier score) are
  **not** used. Ready only if all of these hold:
  1. at least 30 days between `btc_ref.ts` and now, with equity snapshots on at least 25
     of those days;
  2. at least 20 closed trades;
  3. `return_pct > btc_buy_hold_return_pct` (both after fees);
  4. `max_drawdown_pct ≤ 20`.

  `reasons` lists every unmet criterion in plain words, and always ends with "Paper only:
  live trading needs explicit authorisation from the user". `criteria` echoes the
  thresholds.

### 9.5 `GET /api/overview` (`kalshibot/api/overview.py`, task T3)

`router = APIRouter()` with `@router.get("/api/overview")`. Shape = contract §13 plus the
additions in bold:

```
{generated_at,
 venues: {kalshi: {venue, label:"KALSHI · prediction markets", available, unavailable_reason,
                   engine_running, kill_switch, starting_balance, equity, cash, total_pnl,
                   total_return_pct, todays_pnl, open_positions, fees_paid, last_error,
                   **last_error_at, last_tick_at**},
          coinbase: {venue, label:"COINBASE · crypto spot", ...same fields}},
 combined: {starting_balance, equity, total_pnl, total_return_pct,
            note:"Sum of two separate paper accounts", **venues_included:[...]**},
 equity_series: {kalshi:[{ts, equity}], coinbase:[{ts, equity}]},
 **server: {version, venues:["kalshi","coinbase"], loop_lag_ms:{last, max_5m}}**}
```

- Kalshi block: `svc.broker.account().to_json()` + `svc.engine.status()`.
- Coinbase block: `cb.broker.account().to_json()` + `cb.engine.status()`. If `cb` is None:
  `{venue, label, available:false, unavailable_reason: app.state.cb_error, every number
  null}`.
- Each block is built in its own `try`. A failure becomes `available:false` with
  `unavailable_reason = "<Type>: <msg>"`, and the endpoint still returns 200.
- `combined` sums only the available venues; `venues_included` names them.
- `equity_series`: `store.list_equity(since=now − 30 d, max_points=500)` per venue plus
  the live point, **cached for 60 s** per venue in `app.state.overview_cache`. The page
  polls every 5 s (`frontend/src/lib/overview.tsx:27`); the account blocks are cheap and
  are not cached.

---

## 10. Frontend (F1 and F2)

Already built and unchanged: routes `/`, `/kalshi/*`, `/coinbase/*` with legacy
redirects; nav groups headed by `<VenueBadge long>`; `<VenueBanner>` on every venue page;
the combined card already shows "Kalshi only — the other venue is unavailable" when
Coinbase is down (`pages/Overview.tsx:140-151`); the empty state "No Coinbase strategies
registered" exists (`pages/coinbase/Strategies.tsx:198`).

**Colour tokens** (`styles/tokens.css`, unchanged; never reuse P&L or status colours):

| Token | Dark | Light |
|---|---|---|
| `--venue-kalshi` / `--venue-kalshi-text` | `#19a6a1` / `#3cc9c3` | `#00918b` / `#006b66` |
| `--venue-coinbase` / `--venue-coinbase-text` | `#9a7cf2` / `#b39cf6` | `#6e4fd6` / `#5535be` |
| `--venue-*-bg` | tint of the base colour | tint of the base colour |

Labels: "KALSHI · prediction markets" and "COINBASE · crypto spot", each with its monogram
glyph (never colour alone). Units: Kalshi contracts and ¢; Coinbase base quantity
(`0.01234567 BTC`), USD prices, fees as `$0.50 (0.50%)`.

### F1: shared and Kalshi-side files

`frontend/src/api/client.ts`, `api/types.ts`, `api/normalize.ts`, `api/mock.ts`,
`pages/Overview.tsx`:
1. `Status` type: optional `venues?: string[]`. `Overview` type: optional
   `server?: {version, venues, loop_lag_ms: {last, max_5m}}`,
   `combined.venues_included?: string[]`. Normalise both (missing → undefined).
2. The overview fallback (`client.ts` near line 372) records the `/api/status` result it
   already fetches. The synthesized overview carries `server_version` and
   `server_has_coinbase = status.venues?.includes("coinbase") ?? false`.
3. Banner in `Overview.tsx`, by case:
   - 404 and the server has no `venues` key → "This server runs kalshibot {version}, a
     build without the Coinbase venue, so only the Kalshi account is shown. Restarting or
     rebooting will not add it: the new code has to be built and deployed (Docker:
     `./deploy.sh update`). This page checks again every minute."
   - 404 and `venues` includes `coinbase` → "The server has the Coinbase venue, but
     `GET /api/overview` failed to load. Only the Kalshi account is shown; check the
     server log for 'GET /api/overview unavailable'."
   - 200 with `coinbase.available = false` → no banner; the Coinbase card shows
     `unavailable_reason`.
4. Mock (`VITE_MOCK=1`): `/api/status` returns `version: "0.2.0"` and `venues`;
   `/api/overview` returns `server` and `venues_included`.

### F2: Coinbase files

`frontend/src/api/coinbase/client.ts`, `api/coinbase/types.ts`, `api/coinbase/mock.ts`,
`pages/coinbase/shared.tsx`, `Strategies.tsx`, `Markets.tsx`, `Dashboard.tsx`:
1. `isCbNotInstalled(e)`: `ApiError` with `kind === "http"` and `status === 404`.
   `cbEngineState` returns a new state `"not-installed"` before the other error checks.
   `CB_STATE["not-installed"] = {cls: "neutral", label: "Not on this server", icon:
   "plug"}`. Pages render `EmptyState title="Coinbase is not installed on this server"
   hint="This build has no /api/coinbase endpoints. Deploy a build with the Coinbase
   venue (./deploy.sh update)."`.
2. `Strategies.tsx`:
   - empty state title becomes "No Coinbase strategies installed", hint unchanged;
   - each strategy card shows "Round trip at {tier.label}: {taker×2}% taker / {maker×2}%
     maker" from `status.fee_tier` (for example "Round trip at Intro (US): 1.80% taker /
     1.00% maker").
3. `Markets.tsx`:
   - the spread tooltip uses `status.fee_tier` instead of the hard-coded "~240 bps"
     (which is wrong: Intro taker round trip = 180 bps);
   - `spread_bps: null` renders "—" and sorts last;
   - the caption notes "Bid/ask shown for the 40 most-traded USD products".
4. `Dashboard.tsx:73`: the win-rate tile sub-label becomes "`{trades}` closed trades"
   (it says "fills" today, which is wrong).
5. Mock: `fee_tier.label`, `fee_tiers`, products with some `spread_bps: null`.

---

## 11. Config, Docker, tests

**Config.** `coinbase:` stays optional. The user's `config.yaml` has none, so the defaults
apply: `enabled: true`, `engine.autostart: true`, $1,000, `fee_tier: intro`,
`data/coinbase.sqlite3`, and now `max_rps: 2`. This is safe: no strategy is enabled by
default, so the engine only loads products, ticks and marks an empty account. T2 changes
the `max_rps` default in `coinbase/config.py`, the comment in `config.example.yaml`
("# max_rps: 2 # ... the Kalshi crypto feed may add up to 3; keep the sum <= 5") and the
expectation in `tests/test_cb_config.py:32` (`cb.max_rps == 2`).

**Docker.** No Dockerfile or compose change: `data/` is mounted read-write and
`research/` read-only. The user deploys with `./deploy.sh update`.

**Rules for builders** (environment):
- Never touch `data/` or port 8765.
- Manual smoke runs use another port and temp storage:
  `KALSHIBOT_STORAGE__PATH=$TMP/k.sqlite3 KALSHIBOT_COINBASE__STORAGE_PATH=$TMP/cb.sqlite3 uv run kalshibot serve --port 8766 --no-engine`.
- Tests never use the network: `CoinbaseClient(transport=httpx.MockTransport(...))` or
  the fakes.

**Tests** (new files, except the one-line edit to `test_cb_config.py`):

| File | Owner | Covers |
|---|---|---|
| `tests/test_cb_interfaces.py` | T0 | fakes satisfy the Protocols (`isinstance` with `runtime_checkable`); `cb_jsonable` keeps `Decimal("0.00001234")` as `1.234e-05`; `SpotEventBus.publish` adds `venue` and does not round; `cb_sse` framing |
| `tests/test_cb_client_raw.py` | T1 | `get_raw` shares the bucket, retries on 429 and 5xx, and updates counters (MockTransport) |
| `tests/test_cb_marketdata.py` | T1 | execution vs mark book rules; merged mark book keeps deeper cached levels; trim window and 50-level floor; single-flight; candles append without duplicates or open bars; scanner starts on touch, stops after 300 s idle, round-robins L1 over the top 40, `spread_bps` null for the rest, USD filter; no upstream call from `scanner_rows` |
| `tests/test_cb_engine.py` | T1 | fake clock and md: a bar runs `on_bar` exactly once; a restart with `last_bar_at` older than the latest bar runs one catch-up (`catch_up=true`), never two; one strategy raising does not stop another; `on_bar` timeout; kill switch cancels resting buys and still allows sells; daily loss trips it; **zero strategies: ticks advance `tick_count` and `last_tick_at` and publish `tick`**; maker-then-taker fallback survives a restart |
| `tests/test_cb_services.py` | T2 | disabled → `CoinbaseUnavailable("disabled in config")`; `load_error` passthrough; lock held → reason names `/api/coinbase/account/reset`; interrupted backtests marked failed; no network during build (MockTransport that fails on any request); pytest guard refuses `<repo>/data/` |
| `tests/test_cb_api.py` | T2 | TestClient + `create_app(settings, services=<kalshi fakes>, cb_services=make_fake_cb_services(tmp_path))`: every route's field names against the contract plus section 9.2; 404/409 on cancel; 404/422 on PATCH strategies; 503 detail prefix when `cb_services=None` |
| `tests/test_cb_stream.py` | T2 | `?replay=500`, `Last-Event-ID`, `max_events`, `duration`, the `bar` type delivered, stop on `app.state.stopping`; a SHIB-sized price `0.00001234` and quantity `12345678.12345678` survive to the wire unrounded |
| `tests/test_cb_analytics.py` | T2 | trades/win-rate definition; Sharpe `null` below 7 days; benchmark formula; readiness reasons |
| `tests/test_overview.py` | T3 | both venues up; `cb=None` with the reason; Coinbase block raising; Kalshi block raising; `combined.venues_included`; `equity_series` cached 60 s; `server.version` / `venues` |
| `tests/test_server_isolation.py` | T3 | see below |
| `tests/test_cb_cli.py` | T3 | `coinbase-reset` on a tmp store; locked store message and exit 1; `reset` prompt names Kalshi; `serve` Coinbase lock pre-check is non-fatal |

`tests/test_server_isolation.py` must assert:
- a plain `TestClient(create_app(settings, services=svc))` run leaves
  `<repo>/data/coinbase.sqlite3` and its `.lock` **exactly as before**: same existence,
  `st_mtime_ns` and size, checked by `os.stat` only (the live container writes these
  files, so "does not exist" would be wrong);
- `build_coinbase_services` raising (monkeypatched) → every existing `GET /api/*` route
  still returns 200, and `/api/coinbase/status` returns 503 with the prefix;
- `kalshibot.coinbase.api` failing to import (monkeypatched `sys.modules`) → 503 with
  `"import error:"`, and Kalshi is unaffected;
- `GET /api/coinbase/status` is not 404 (the router comes before the catch-all);
- `/api/status` has `venues` and `version == "0.2.0"`;
- `autostart=False` → the Coinbase engine is not started.

**Kalshi regression.** `uv run pytest` passes; all 717 Kalshi tests pass unchanged, and
no existing Kalshi test file is edited.

**Frontend.** `npm run build` (tsc + vite) and `VITE_MOCK=1` smoke of `/`, `/kalshi` and
`/coinbase`.

---

## 12. Contract amendments (applied by T0 to `docs/COINBASE_CONTRACT.md`)

1. §1: add `interfaces.py`, `events.py` [T0], `analytics.py` [T2], and
   `kalshibot/api/looplag.py` [T3].
2. §4 and §15: `max_rps` default 2 (was 3), with the budget note from section 7.5.
3. §10: `build_coinbase_services` is **sync** and raises `CoinbaseUnavailable`; the
   `CoinbaseServices` fields and `aclose()` method are as in section 5.1; a tick is
   defined as in section 7.2.
4. §13 server integration: `app.state.cb = build_coinbase_services(settings)` (no
   `await`); the `create_app(cb_services=...)` table from section 8.1; the autostart rule
   from section 8.2.
5. §13 table: the additions in sections 9.2, 9.3, 9.5; `analytics.readiness` rule from
   section 9.4; `/api/status.venues`.
6. §14: fee example `$0.50 (0.50%)` (was `$0.61 (0.60%)`).
7. §15: `fee_rates: {maker: 0.005, taker: 0.009}` example (was 0.006 / 0.012);
   `fee_tier: intro`.
8. Header: "the 716 passing tests" → "the 717 Kalshi tests (1,059 total including
   `test_cb_*`, 2026-09-27)".

---

## 13. Build tasks and file ownership

No file has two owners. Nobody edits a file outside their row. "New" means the file must
not exist yet; "edit" means an existing file.

| Task | Runs | Owns (only these paths) |
|---|---|---|
| **T0** Interfaces and contract | first, alone | new `kalshibot/coinbase/interfaces.py`, new `kalshibot/coinbase/events.py`, new `tests/cb_service_fakes.py`, new `tests/test_cb_interfaces.py`, edit `docs/COINBASE_CONTRACT.md` (section 12 only); baseline tarball outside the repo |
| **T1** Market data and engine (was B3b-1) | after T0 | new `kalshibot/coinbase/marketdata.py`, new `kalshibot/coinbase/engine.py`, edit `kalshibot/coinbase/client.py` (additive `get_raw` / `_send` only), new `tests/test_cb_marketdata.py`, new `tests/test_cb_engine.py`, new `tests/test_cb_client_raw.py` |
| **T2** Services, router, analytics (was B3b-2) | after T0, parallel with T1 | new `kalshibot/coinbase/services.py`, new `kalshibot/coinbase/api.py`, new `kalshibot/coinbase/analytics.py`, edit `kalshibot/coinbase/config.py` (`max_rps` default only), edit `config.example.yaml` (Coinbase `max_rps` comment only), edit `tests/test_cb_config.py` (line 32 only), new `tests/test_cb_services.py`, `tests/test_cb_api.py`, `tests/test_cb_stream.py`, `tests/test_cb_analytics.py` |
| **T3** Server integration (was B3b-3) | after T0, parallel | new `kalshibot/api/overview.py`, new `kalshibot/api/looplag.py`, edit `kalshibot/api/server.py` (sections 8.1–8.4 only), edit `kalshibot/cli.py` (section 8.5), edit `deploy.sh` (`status_line` only), edit `kalshibot/__init__.py` and `pyproject.toml` (version only), new `tests/test_overview.py`, `tests/test_server_isolation.py`, `tests/test_cb_cli.py` |
| **F1** Frontend shared | after T0, parallel | edit `frontend/src/api/client.ts`, `api/types.ts`, `api/normalize.ts`, `api/mock.ts`, `pages/Overview.tsx` |
| **F2** Frontend Coinbase | after T0, parallel | edit `frontend/src/api/coinbase/client.ts`, `api/coinbase/types.ts`, `api/coinbase/mock.ts`, `pages/coinbase/shared.tsx`, `pages/coinbase/Strategies.tsx`, `pages/coinbase/Markets.tsx`, `pages/coinbase/Dashboard.tsx` |
| **T9** Integration check | after all above | edits nothing. Runs `uv run pytest` (expect 1,059 + new, 0 failures), `cd frontend && npm run build`, and a smoke run on port 8766 with temp storage; reports failures to the owning task. Then hands the user the `./deploy.sh update` step and the smoke test from section 0 |

Dependencies in practice:
- T2's `services.py` imports `SpotMarketData` and `CoinbaseEngine` **inside**
  `build_coinbase_services`, so T2 can import and test its other code before T1 finishes.
  Its tests that need the real classes use
  `pytest.importorskip("kalshibot.coinbase.engine")` until T1 lands.
- T3 tests against `make_fake_cb_services` and a monkeypatched `build_coinbase_services`.
- F1 and F2 work against mocks (`VITE_MOCK=1`).

**Phase 2: strategies.** Each S* owner writes `coinbase/strategies/<name>.py` and
`tests/test_cb_strat_<name>.py`, then runs a backtest with Intro fees. A strategy is
enabled only after the user reviews it.

**Phase 3 (optional):**
- Advanced Trade WebSocket (`level2`, `market_trades`, no keys) to remove REST lag and
  book polling;
- Advanced Trade `/market/product_book?limit=` as a smaller book source (at most 1,000
  levels, about 1 s fresh, no order counts);
- enforce `min(paper cap, product max_slippage_percentage)` in the broker;
- hourly Advanced Trade `base_min_size` / `quote_min_size` refresh;
- manual orders from the UI.

---

## Open questions

1. **Fee tier.** Is Intro (US) 0.50% / 0.90% right for you? If you have a higher tier,
   set `coinbase.fee_tier` or `coinbase.fee_rates`.
2. **Autostart.** The Coinbase engine autostarts with the server (it only ticks and marks
   while no strategy is enabled). Should it start stopped instead?
3. **Starting balance.** Is $1,000 right, and should the Overview show the combined total
   at all?
4. **Kalshi aliases.** No `/api/kalshi/*` aliases in v1. Do your scripts need them?
5. **Rate cap.** Is 2 req/s for the venue (5 req/s worst case with the Kalshi crypto
   feed) acceptable given the other services on this IP?
6. **Version control.** The repo has no git. Should T0 run `git init` and commit a
   baseline, instead of the tarball?

---

## Review log

Review of rev. 1 (16 findings plus a correction to section 0). Unless noted, each is
accepted and the fix is in the section named.

| # | Finding | Decision |
|---|---|---|
| §0 | Container is not "the newest code": the banner fix (11:32) and `backtest.py` / `risk.py` (11:33) postdate the 11:24 image; no hard reload needed | **Accepted.** Section 0 rewritten: the wording is fixed by `./deploy.sh update` today, Coinbase needs Phase 1; hard-reload step removed |
| P0-1 | Wiring Coinbase from settings breaks tests and writes to live `data/` | **Accepted.** `create_app(cb_services="auto")` builds only when `services is None` (8.1); pytest guard (5.1); isolation test compares stat of `data/coinbase.sqlite3` before and after rather than asserting absence, because the deployed container creates that file (11) |
| P0-2 | Bandwidth fix unreachable; L2 parsing blocks the loop | **Accepted with one change.** All rules live in `SpotMarketData.book()` with no `broker.py` edit, and decode plus trim run in `to_thread` (6.2). Rejected: serving marks from a **bare** level-1 book. `broker._reconcile` (`broker.py:590-599`) treats missing levels as size 0 and deletes their consumed-liquidity entries, so marks use L1 merged with the cached L2 depth. The Advanced Trade `product_book` is moved to Phase 3 (second host with an unpublished limit, no order counts). Note: the 2 GB/day figure assumes a permanently resting order; expiry caps it at about 85 MB per resting-order-hour |
| P0-3 | Interfaces unpinned; no git | **Accepted.** T0 writes `interfaces.py`, `events.py` and fakes first (5); `build_coinbase_services` is sync and raises `CoinbaseUnavailable`; `aclose()` is a method; `backtests` dict lives on `CoinbaseServices`; `app.state.cb` / `cb_error` are fixed names; ordering replaces "merge order" (13) |
| P1-4 | "Running · stalled" with zero strategies | **Accepted.** Tick = the 60 s `tick` task, which always runs (7.2); test added |
| P1-5 | SSE spec mismatched the client | **Accepted.** `replay` ≤ 500, `history=500`, `max_events`, `duration`, `stopping`, own type set with `bar` (5.2, 9.3) |
| P1-6 | `jsonable` / `sse()` round to 4 dp | **Accepted.** `SpotEventBus`, `cb_jsonable`, `cb_sse` (5.2); SHIB-sized round-trip test (11) |
| P1-7 | Scanner not buildable | **Accepted with one change.** Lazy scanner, stats every 60 s, L1 round-robin over the top 40 at 0.5 req/s, `spread_bps` null otherwise, USD only, no upstream calls in handlers (6.4). The product list comes from the hourly cache rather than being refetched every 60 s: same data, much less bandwidth |
| P1-8 | Rate budget understated | **Accepted:** venue default `max_rps` 2, sum documented (7.5). Rejected the alternative of a process-wide shared bucket: it would mean editing Kalshi's `feeds/crypto.py` |
| P1-9 | Missing owners | **Accepted:** F2 owns the Coinbase frontend items, the loop-lag probe goes in T3 and is exposed through `/api/overview` and the Coinbase status, not Kalshi's `status_payload` (8.4, 10, 13). **Rejected** that `models.py` must change: `Product.raw` already holds `max_slippage_percentage`. The `min()` rule itself is deferred to Phase 3, because the 1% paper cap is tighter than every observed value (2–3%) and enforcing it would need a `broker.py` edit; T1 logs a warning instead (2, item 4) |
| P1-10 | `--no-engine` / autostart don't cover Coinbase; lock pre-check | **Accepted** (8.2, 8.5) |
| P2-11 | Venue labels in lock message, CLI prompt, deploy status | **Accepted.** The lock message is rewritten in `build_coinbase_services` and `coinbase-reset`, so `store.py` / `ProcessLock` stay unchanged (4, 8.5) |
| P2-12 | UI can't tell old server from new | **Accepted.** `__version__` 0.2.0 and `/api/status.venues`; banner cases in F1 (8.4, 10). Section 0's claim that this can't be done is removed |
| P2-13 | Analytics underspecified / reused Kalshi | **Accepted.** `coinbase/analytics.py` with trades, win rate, Sharpe, benchmark from `kv['account.btc_ref']`, spot readiness rule (9.4) |
| P2-14 | Restart behaviour unspecified | **Accepted.** One catch-up bar, interrupted backtests failed, risk persistence left to `risk.py` (4) |
| P2-15 | Contract details the UI needs | **Accepted.** `fee_tier.label`, `fee_tiers`, `last_error_at` / `last_tick_at` in overview blocks, 503 prefix, 60 s `equity_series` cache (9). Note: the "coinbase" substring check in `client.ts:134` applies only to non-JSON 503s, and our 503s are JSON; the prefix rule is kept anyway as defence |
| P3-16 | Stale statements | **Accepted.** Baseline 1,059 = 717 + 342 (1); fee examples follow `fees.py` and the contract is amended (12); hard-reload step removed (0). Also fixed: the Markets tooltip's "~240 bps" (F2) |
