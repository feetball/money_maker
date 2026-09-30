# kalshibot frontend

Web dashboard for the **Kalshi paper-trading bot**. It never places real orders; the
persistent **PAPER TRADING** badge is there so nobody forgets.

Stack: Vite 7 + React 18 + TypeScript (strict) + react-router 7 + recharts 2. No UI
kit. Styling is plain CSS driven by design tokens (`src/styles/tokens.css`). It is
dark-first, with a light theme via `prefers-color-scheme`, and Settings → Theme can
override the choice.

The binding contracts are `docs/ARCHITECTURE.md` §12 (REST + SSE) and §13 (pages).

## Quick start

```bash
cd frontend
npm install

npm run dev        # http://127.0.0.1:5173, proxies /api (REST + SSE) to http://127.0.0.1:8765
                   # (KALSHIBOT_API_URL=http://127.0.0.1:8765 npm run dev for another backend port)
npm run dev:mock   # same UI with generated data, no backend needed (VITE_MOCK=1)
npm run build      # tsc -b (zero type errors required) && vite build -> frontend/dist
npm run preview    # serve the built dist/ on :4173 (also proxies /api)
npm run lint       # type-check only (tsc --noEmit for app + vite config)
```

To use `npm run dev`, first start the backend from the repo root with
`uv run kalshibot serve`. Node >= 20.19 is required, which Vite 7 also needs.

## Mock mode

`npm run dev:mock` runs `vite --mode mock`, which loads `.env.mock` (`VITE_MOCK=1`).
In this mode, every `/api` call is answered in the browser by `src/api/mock.ts`, which
holds a seeded paper account that stays consistent with itself:

- about 130 markets across 9 categories;
- 4 strategies with `param_schema`;
- open positions, resting and historical orders, fills, settlements (including a few
  positions closed early);
- signals, including risk rejections with their reasons and a few malformed intents
  (`count` / `limit_price` null);
- logs and a 40-day equity curve;
- an engine `last_error` from two hours ago that has since recovered (history, not a
  current error);
- analytics with per-contract and per-trade bootstrap CIs, edge capture, calibration
  buckets, `params` thresholds and a readiness verdict;
- risk utilization;
- backtests, including one failed run.

The account keeps changing while the mock engine runs. `/api/stream` is simulated with
tick, signal, order, fill, settlement, log and account events. Start/stop, the kill
switch (which cancels resting orders, as the backend does), parameter edits, risk
edits, order cancellation, backtest launch (it finishes after about 7 s) and account
reset all work, including 404 and 422 errors. The mock follows the backend's
conventions: `cost_basis` excludes fees, logs have store ids but their live SSE copies
do not, and cancelling an order that is no longer open answers HTTP 409 (shown as an
info toast, not an error).

To test SSE reconnect and backoff, run this in the browser console:
`__kalshibotMock.dropStream()`.

`npm run build:mock` produces a static demo build in `dist/` with no backend. The mock
module is loaded with a dynamic import behind `import.meta.env.VITE_MOCK`, so it is
left out of normal builds.

## Serving from FastAPI (deep links)

Routing uses real paths such as `/strategies` and `/backtests/42` (history API, not
`#/` hashes). Assets are referenced from the absolute base `/`, e.g.
`/assets/index-*.js`. The backend (`kalshibot/api/server.py`) does exactly what this
needs, so `uv run kalshibot serve` serves the built dashboard at `/`:

1. Every `/api/*` route is registered first; an unknown `/api/...` path is a JSON 404,
   never `index.html` (the client reports an HTML response on an `/api` path as an
   error).
2. Real files in `frontend/dist` are served as-is (`/assets/*` content-hashed and cached
   for a year, `/favicon.svg`); a missing `/assets/*` file is a 404.
3. Every other GET returns `frontend/dist/index.html` (`Cache-Control: no-cache`), so a
   reload or a pasted deep link opens the right page.

`dist/` is looked up per request: rebuilding while the server runs needs no restart.
Before the first build, `/` shows a placeholder page explaining how to build.
`/api/stream` is sent unbuffered (`Cache-Control: no-cache`, `X-Accel-Buffering: no`).

## How the UI uses the API

- `src/api/types.ts` mirrors §12. `src/api/client.ts` has one typed wrapper per
  endpoint (`api.status()`, `api.patchStrategy()`, …) and `useEventStream()`.
  `useEventStream()` opens an EventSource on `/api/stream`, reconnects with
  exponential backoff (1 s → 30 s, ±20 % jitter; reset only after 15 s connected) and
  delivers typed, normalized events. Every backend event carries an SSE `id:`
  (strictly increasing, also across server restarts). Reconnects ask for `?replay=500` and skip the
  events already delivered, so the feed has no gap.
- `src/api/normalize.ts` makes every response safe to render. It handles nulls,
  missing fields, empty arrays, Decimals serialized as strings, naive or epoch
  timestamps, and common aliases where §12 leaves shapes open.
- Each page polls every 5–10 s. Polling pauses while the tab is hidden and resumes
  with an immediate refresh. Relevant SSE events (fill, settlement, signal, order)
  trigger an immediate debounced refetch. `account` events update the dashboard KPIs
  live, and the activity feed is built from SSE plus `/api/logs`.
- Errors raise a toast once per failure streak. When the backend is unreachable, the
  failures collapse into a single toast and a banner appears. Stale data stays on
  screen at full contrast, marked by a warning stripe and a "Refresh failed" note.
- `engine.last_error` is history: the backend keeps it after the failing job recovers.
  The UI treats it as current (header pill, page banner) only while it is under
  10 minutes old or no tick has run since; otherwise the Engine card shows it muted.
- Server timestamps are compared with the server's clock (offset measured from
  `/api/status` `server_time`), so a skewed browser clock does not show "stalled".

## Display conventions

- Dollars always have 2 dp. P&L is always signed (`+$3.20`, `−$1.05`, with a real
  minus sign), so meaning never depends on colour alone.
- Prices and fair values are shown in cents (`93¢`); 1¢ equals 1 % implied probability.
  Per-contract edges and means are shown as signed cents; their colour follows the
  value as displayed, so `+0.4¢` is never grey.
- P&L polarity is the same in text and charts: gains blue, losses red. Green is kept
  for status (running, filled, ready).
- `cost_basis` is principal **excluding** fees. Positions and settlements show a
  separate Fees column: unrealized = liquidation value − cost − fees, and settlement
  P&L = payout − cost − fees.
- Analytics: the per-strategy table shows the per-contract CI (¢). The readiness
  verdict and its wording use the per-trade CI (`ci_trade_low/high`, $), and the
  criteria text uses the thresholds the backend reports in `params`.
- Equity is the **liquidation value**: what selling every position into its side's
  bid ladder would bring now (before exit fees). Positions show the best bid (`Bid`)
  and the average exit price (`Exit` = liquidation value / contracts, the best bid when
  the top level covers the position). **Mid-marked equity** is shown separately.
- Timestamps show relative time plus local absolute time. Hovering shows the full
  local time and UTC.
- Every chart has a **Table** toggle that shows the same data as a table. Colours
  come from a CVD-validated palette (`src/charts/palette.ts`), and chart text uses
  ink colours, never series colours.

## Layout

```
src/
  api/        types.ts (contract) · client.ts (wrappers + SSE hook) · normalize.ts · mock.ts
  lib/        format.ts · hooks.ts (usePolling, useNow, …) · stream.tsx · status.tsx · toast.tsx · theme.tsx
  components/ Layout, Engine (status pill / controls), DataTable, ParamEditor, ActivityFeed, ConfirmDialog, ui, values
  charts/     TimeSeriesChart, BarCharts, CalibrationChart, CiBar, ChartFrame, palette
  pages/      Dashboard, PositionsOrders, History, Strategies, Signals, Markets, Analytics, Backtests, Settings, NotFound
  styles/     tokens.css · app.css
```
