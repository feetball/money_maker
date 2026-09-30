/**
 * Overview (`/`) tables that put BOTH paper venues in one place
 * (docs/COINBASE_CONTRACT.md §13–§14):
 *
 *  - Strategy scoreboard: every Kalshi + Coinbase strategy in one sortable table.
 *  - Open positions: both venues, each in its own units (Kalshi contracts & ¢,
 *    Coinbase coin quantities & USD prices).
 *  - Recent activity: Kalshi fills + settlements and Coinbase fills, newest first.
 *
 * Every row carries a VenueBadge (text + monogram, never colour alone), and no table
 * adds numbers across venues: they are two separate paper accounts. Each venue's
 * part of a table loads, fails and refreshes on its own: a Coinbase venue that is
 * unavailable (overview `available: false`, 503, or a server without the Coinbase
 * backend) shows "Coinbase unavailable: <reason>" and the Kalshi rows still render.
 */
import { type ReactNode } from "react";
import { Link } from "react-router";
import { ApiError, api, errorMessage } from "../api/client";
import { CB_MISSING_REASON, cbApi, isCbMissing, isCbUnavailable } from "../api/coinbase/client";
import type { CbFill, CbPosition, CbStrategy } from "../api/coinbase/types";
import type { Fill, Position, Settlement, Strategy } from "../api/types";
import { DataTable, type Column } from "../components/DataTable";
import { Icon } from "../components/Icon";
import { Badge, Card, EmptyState, Freshness, LoadingBlock, Spinner } from "../components/ui";
import { Cents, MarketCell, Pnl, SideTag, StrategyTag, Time, Usd } from "../components/values";
import { VenueBadge, VENUES, type Venue } from "../components/Venue";
import { DASH, fmtAbsolute, fmtCents, fmtFrac, fmtInt, fmtPct, fmtUsd, parseTs, pnlTone, sideLabel } from "../lib/format";
import { usePolling, type PollResult } from "../lib/hooks";
import { useOverview } from "../lib/overview";
import { fmtPrice, fmtQty } from "./coinbase/format";
import { CbSideTag, Fee, Price, ProductCell, Qty, useCbPolling } from "./coinbase/shared";

/** Poll interval for every table on the Overview (paused while the tab is hidden). */
const POLL_MS = 10_000;
/** Rows fetched per feed for "Recent activity". */
const FEED_LIMIT = 50;

// ---------------------------------------------------------------------------
// Per-venue parts: each venue's slice of a table has its own state
// ---------------------------------------------------------------------------

/** The read-only view of a poll that the notes and freshness need (covariant in T). */
type PollLike = Pick<PollResult<unknown>, "error" | "refreshing" | "updatedAt" | "refresh">;

type Part<T> =
  | { venue: Venue; state: "ok"; data: T; poll: PollLike }
  | { venue: Venue; state: "loading" }
  | { venue: Venue; state: "unavailable"; reason: string }
  | { venue: Venue; state: "error"; poll: PollLike };

/** Why Coinbase is unavailable, from a failed /api/coinbase/* request. */
function cbReason(e: unknown): string {
  if (isCbMissing(e)) return CB_MISSING_REASON;
  const d = e instanceof ApiError ? e.detail : errorMessage(e);
  return d.replace(/^coinbase venue unavailable:\s*/i, "") || "the server did not report a reason";
}

/**
 * Coinbase availability as the shared /api/overview poll reports it. While it says
 * `available: false`, the Overview does not poll /api/coinbase/* at all.
 */
function useCbOff(): string | null {
  const { data } = useOverview();
  const cb = data?.venues.coinbase;
  if (!cb || cb.available) return null;
  return cb.unavailable_reason || "the server did not report a reason";
}

function partOf<T>(venue: Venue, poll: PollResult<T>, offReason: string | null): Part<T> {
  if (offReason !== null) return { venue, state: "unavailable", reason: offReason };
  if (poll.data !== undefined) {
    // A venue that went down after loading: say so, keep the last rows (marked stale).
    return { venue, state: "ok", data: poll.data, poll };
  }
  if (poll.loading) return { venue, state: "loading" };
  if (venue === "coinbase" && isCbUnavailable(poll.error)) return { venue, state: "unavailable", reason: cbReason(poll.error) };
  return { venue, state: "error", poll };
}

/** Freshness over several polls: oldest update, any error, refresh all. */
function combinedFreshness(polls: PollLike[]) {
  const updated = polls.map((p) => p.updatedAt).filter((x): x is number => x !== null);
  return {
    updatedAt: updated.length ? Math.min(...updated) : null,
    error: polls.find((p) => p.error)?.error ?? null,
    refreshing: polls.some((p) => p.refreshing),
    refresh: () => polls.forEach((p) => p.refresh()),
  };
}

/** One status line per venue whose part is not a clean, fresh load. */
function VenueNotes({ parts, what }: { parts: Part<unknown>[]; what: string }) {
  const notes: ReactNode[] = [];
  for (const p of parts) {
    const name = VENUES[p.venue].name;
    if (p.state === "unavailable") {
      notes.push(
        <div key={p.venue} className="ov-note ov-note-bad" role="status">
          <VenueBadge venue={p.venue} />
          <Icon name="plug" />
          <span>
            <strong>{name} unavailable:</strong> <span className="wrap">{p.reason}</span>
            <span className="muted"> The other venue's rows are unaffected.</span>
          </span>
        </div>,
      );
    } else if (p.state === "error") {
      notes.push(
        <div key={p.venue} className="ov-note ov-note-bad" role="alert">
          <VenueBadge venue={p.venue} />
          <Icon name="alert" />
          <span>
            <strong>Couldn't load {name} {what}:</strong> <span className="mono wrap">{errorMessage(p.poll.error)}</span>
          </span>
          <button className="btn btn-sm" onClick={p.poll.refresh}>
            <Icon name="refresh" /> Retry
          </button>
        </div>,
      );
    } else if (p.state === "loading") {
      notes.push(
        <div key={p.venue} className="ov-note" role="status">
          <VenueBadge venue={p.venue} />
          <Spinner label={`Loading ${name} ${what}`} />
          <span className="muted">Loading {name} {what}…</span>
        </div>,
      );
    } else if (p.poll.error) {
      const at = p.poll.updatedAt ? fmtAbsolute(new Date(p.poll.updatedAt).toISOString(), { seconds: true }) : null;
      const down = p.venue === "coinbase" && isCbUnavailable(p.poll.error);
      notes.push(
        <div key={p.venue} className="ov-note ov-note-warn" role="status">
          <VenueBadge venue={p.venue} />
          <Icon name="alert" />
          <span>
            {down ? (
              <>
                <strong>{name} unavailable:</strong> <span className="wrap">{cbReason(p.poll.error)}</span> —{" "}
              </>
            ) : (
              <>Refresh failed — </>
            )}
            showing the last loaded {name} {what}
            {at ? ` (from ${at})` : ""}.
          </span>
        </div>,
      );
    }
  }
  return notes.length ? <div className="ov-notes">{notes}</div> : null;
}

/**
 * Card body for a two-venue table: status lines for the venues that are not loaded,
 * then the merged rows (or loading / empty state).
 */
function TwoVenueBody<R>({
  parts,
  what,
  rows,
  empty,
  children,
}: {
  parts: Part<unknown>[];
  what: string;
  rows: R[];
  empty: { title: string; hint?: ReactNode };
  children: (rows: R[]) => ReactNode;
}) {
  const loaded = parts.filter((p) => p.state === "ok");
  const allLoading = parts.every((p) => p.state === "loading");
  if (allLoading) return <LoadingBlock label={`Loading ${what}…`} />;
  const venuesText = loaded.map((p) => VENUES[p.venue].name).join(" and ");
  return (
    <>
      <VenueNotes parts={parts} what={what} />
      {loaded.length === 0 ? null : rows.length === 0 ? (
        <EmptyState title={empty.title} hint={empty.hint ?? `${venuesText} ${loaded.length > 1 ? "report" : "reports"} none.`} />
      ) : (
        children(rows)
      )}
    </>
  );
}

const venueColumn = <R extends { venue: Venue }>(): Column<R> => ({
  key: "venue",
  header: "Venue",
  title: "Paper account this row belongs to (two separate accounts)",
  sortValue: (r) => VENUES[r.venue].name,
  render: (r) => <VenueBadge venue={r.venue} compact="phone" />,
});

const SEPARATE_NOTE = "Rows from two separate paper accounts — nothing here is added across venues.";

// ---------------------------------------------------------------------------
// Strategy scoreboard
// ---------------------------------------------------------------------------

interface ScoreRow {
  venue: Venue;
  name: string;
  description: string;
  experimental: boolean;
  benchmark: boolean;
  enabled: boolean;
  paused: string | null;
  realized: number;
  unrealized: number;
  total: number;
  /** P&L ÷ (allocation cap × the venue's starting balance), percentage points. */
  returnPct: number | null;
  returnTitle: string;
  closed: number | null;
  fills: number;
  winRate: number | null;
  fees: number;
  error: string | null;
}

/** Benchmark strategies (e.g. Coinbase `btc_hold`) say so in their description. */
const isBenchmark = (name: string, description: string) => /^benchmark\b/i.test(description.trim()) || /(^|_)benchmark($|_)/i.test(name);

function returnOn(total: number, allocPct: number | null, start: number | null, venue: Venue): { pct: number | null; title: string } {
  const name = VENUES[venue].name;
  if (allocPct === null || !(allocPct > 0)) return { pct: null, title: `No allocation cap reported by ${name}` };
  if (start === null || !(start > 0)) return { pct: null, title: `${name} starting balance not known yet` };
  const capital = (start * allocPct) / 100;
  return {
    pct: (total / capital) * 100,
    title: `Total P&L ÷ its allocation: ${fmtPct(allocPct, { dp: 0 })} of the ${name} starting balance ${fmtUsd(start)} = ${fmtUsd(capital)}`,
  };
}

function kalshiScore(s: Strategy, start: number | null): ScoreRow {
  const st = s.stats;
  const total = st.realized_pnl + st.unrealized_pnl;
  const r = returnOn(total, s.risk_limits?.max_allocation_pct ?? null, start, "kalshi");
  return {
    venue: "kalshi",
    name: s.name,
    description: s.description,
    experimental: s.experimental,
    benchmark: isBenchmark(s.name, s.description),
    enabled: s.enabled,
    paused: s.risk_limits?.paused ?? null,
    realized: st.realized_pnl,
    unrealized: st.unrealized_pnl,
    total,
    returnPct: r.pct,
    returnTitle: r.title,
    closed: st.settled,
    fills: st.fills,
    winRate: st.win_rate,
    fees: st.fees,
    error: s.last_error,
  };
}

function cbScore(s: CbStrategy, start: number | null): ScoreRow {
  const st = s.stats;
  const total = st.realized_pnl + st.unrealized_pnl;
  const r = returnOn(total, st.allocation_pct, start, "coinbase");
  return {
    venue: "coinbase",
    name: s.name,
    description: s.description,
    experimental: s.experimental,
    benchmark: isBenchmark(s.name, s.description),
    enabled: s.enabled,
    paused: null,
    realized: st.realized_pnl,
    unrealized: st.unrealized_pnl,
    total,
    returnPct: r.pct,
    returnTitle: r.title,
    closed: st.trades,
    fills: st.fills,
    winRate: st.win_rate,
    fees: st.fees,
    error: st.last_error,
  };
}

const SCORE_COLUMNS: Column<ScoreRow>[] = [
  venueColumn<ScoreRow>(),
  {
    key: "name",
    header: "Strategy",
    minWidth: 180,
    sortValue: (r) => r.name,
    render: (r) => (
      <div className="ov-strategy">
        <Link to={`${VENUES[r.venue].basePath}/strategies`} className="mono" title={`${r.description}\n\nOpen ${VENUES[r.venue].name} strategies`}>
          {r.name}
        </Link>
        {(r.experimental || r.benchmark) && (
          <span className="ov-tags">
            {r.benchmark && (
              <Badge tone="info" title="Benchmark: the bar the other strategies on this venue have to beat">
                Benchmark
              </Badge>
            )}
            {r.experimental && (
              <Badge tone="warn" title="Not validated out of sample: a forward paper test at small size, not an expected profit">
                Experimental
              </Badge>
            )}
          </span>
        )}
      </div>
    ),
  },
  {
    key: "enabled",
    header: "State",
    sortValue: (r) => (r.enabled ? (r.paused ? 1 : 2) : 0),
    render: (r) =>
      !r.enabled ? (
        <Badge tone="neutral">Off</Badge>
      ) : r.paused ? (
        <Badge tone="bad" icon="stop" title={r.paused}>
          Paused
        </Badge>
      ) : (
        <Badge tone="good" icon="check">
          On
        </Badge>
      ),
  },
  {
    key: "err",
    header: "Error",
    title: "The strategy's last error, if any (hover for the message)",
    sortValue: (r) => (r.error ? 1 : 0),
    render: (r) =>
      r.error ? (
        <span title={r.error}>
          <Badge tone="serious" icon="alert">
            Error
          </Badge>
          <span className="sr-only">: {r.error}</span>
        </span>
      ) : (
        <span className="muted" title="No error reported">
          {DASH}
        </span>
      ),
  },
  {
    key: "total",
    header: "Total P&L",
    align: "right",
    title: "Realized + unrealized, after fees, in this strategy's own venue account",
    sortValue: (r) => r.total,
    render: (r) => <Pnl value={r.total} />,
  },
  { key: "realized", header: "Realized", align: "right", sortValue: (r) => r.realized, render: (r) => <Pnl value={r.realized} /> },
  {
    key: "unrealized",
    header: "Unrealized",
    align: "right",
    title: "Open positions marked at what selling now would fetch",
    sortValue: (r) => r.unrealized,
    render: (r) => <Pnl value={r.unrealized} />,
  },
  {
    key: "ret",
    header: "Return",
    align: "right",
    title: "Total P&L ÷ the strategy's allocation (its cap in % of its venue's starting balance). Hover a value for the basis.",
    sortValue: (r) => r.returnPct,
    render: (r) => (
      <span className={`num tone-${pnlTone(r.returnPct, 0.005)}`} title={r.returnTitle}>
        {fmtPct(r.returnPct, { sign: true, dp: 2 })}
      </span>
    ),
  },
  {
    key: "closed",
    header: "Trades",
    align: "right",
    title: "Closed trades: settled / closed markets on Kalshi, round trips (sells) on Coinbase",
    sortValue: (r) => r.closed,
    render: (r) => <span className="num">{fmtInt(r.closed)}</span>,
  },
  { key: "fills", header: "Fills", align: "right", sortValue: (r) => r.fills, render: (r) => <span className="num">{fmtInt(r.fills)}</span> },
  {
    key: "win",
    header: "Win rate",
    align: "right",
    title: "Share of closed trades with positive P&L",
    sortValue: (r) => r.winRate,
    render: (r) => <span className="num">{fmtFrac(r.winRate)}</span>,
  },
  { key: "fees", header: "Fees", align: "right", sortValue: (r) => r.fees, render: (r) => <Usd value={r.fees} /> },
];

export function StrategyScoreboard() {
  const cbOff = useCbOff();
  const ov = useOverview().data;
  const kPoll = usePolling((signal) => api.strategies({ signal }), { intervalMs: POLL_MS, refreshOn: ["fill", "settlement"] });
  const cPoll = useCbPolling((signal) => cbApi.strategies({ signal }), { intervalMs: POLL_MS, enabled: cbOff === null, refreshOn: ["fill"] });
  const parts: [Part<Strategy[]>, Part<CbStrategy[]>] = [partOf("kalshi", kPoll, null), partOf("coinbase", cPoll, cbOff)];
  const kStart = ov?.venues.kalshi.starting_balance ?? null;
  const cStart = ov?.venues.coinbase.starting_balance ?? null;
  const rows: ScoreRow[] = [
    ...(parts[0].state === "ok" ? parts[0].data.map((s) => kalshiScore(s, kStart)) : []),
    ...(parts[1].state === "ok" ? parts[1].data.map((s) => cbScore(s, cStart)) : []),
  ];
  return (
    <Card
      title="Strategy scoreboard"
      subtitle={`Every strategy on both venues. ${SEPARATE_NOTE}`}
      actions={<Freshness poll={combinedFreshness(cbOff === null ? [kPoll, cPoll] : [kPoll])} />}
      flush
    >
      <TwoVenueBody parts={parts} what="strategies" rows={rows} empty={{ title: "No strategies registered" }}>
        {(r) => (
          <DataTable
            caption="Strategy scoreboard, both venues"
            venue={null}
            rows={r}
            rowKey={(x) => `${x.venue}:${x.name}`}
            defaultSort={{ key: "total", dir: "desc" }}
            maxHeight={520}
            columns={SCORE_COLUMNS}
          />
        )}
      </TwoVenueBody>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Open positions, both venues
// ---------------------------------------------------------------------------

type PosRow = {
  key: string;
  /** Paid for the open position INCLUDING entry fees (comparable across venues). */
  paid: number;
  value: number;
  upnl: number;
  upct: number | null;
  strategy: string;
  label: string;
} & ({ venue: "kalshi"; p: Position } | { venue: "coinbase"; p: CbPosition });

function kalshiPos(p: Position): PosRow {
  const paid = p.cost_basis + (p.open_fees ?? 0);
  return {
    venue: "kalshi",
    p,
    key: `k:${p.ticker}:${p.side}:${p.strategy}`,
    paid,
    value: p.liquidation_value,
    upnl: p.unrealized_pnl,
    upct: paid > 0 ? (p.unrealized_pnl / paid) * 100 : null,
    strategy: p.strategy,
    label: p.ticker,
  };
}

function cbPos(p: CbPosition): PosRow {
  return {
    venue: "coinbase",
    p,
    key: `c:${p.product_id}:${p.strategy}`,
    paid: p.cost_basis,
    value: p.liquidation_value,
    upnl: p.unrealized_pnl,
    upct: p.unrealized_pnl_pct ?? (p.cost_basis > 0 ? (p.unrealized_pnl / p.cost_basis) * 100 : null),
    strategy: p.strategy,
    label: p.product_id,
  };
}

const POS_COLUMNS: Column<PosRow>[] = [
  venueColumn<PosRow>(),
  {
    key: "m",
    header: "Market / product",
    minWidth: 200,
    sortValue: (r) => r.label,
    render: (r) =>
      r.venue === "kalshi" ? (
        <MarketCell ticker={r.p.ticker} title={r.p.title} url={r.p.url} eventTicker={r.p.event_ticker} />
      ) : (
        <ProductCell pid={r.p.product_id} url={r.p.url} />
      ),
  },
  {
    key: "pos",
    header: "Position",
    title: "Kalshi: side and contracts @ average entry price (¢). Coinbase: coin quantity @ average cost per coin (USD, incl. buy fees).",
    render: (r) =>
      r.venue === "kalshi" ? (
        <span className="nowrap">
          <SideTag side={r.p.side} /> <span className="num">{fmtInt(r.p.count)}</span> <span className="muted">contracts @</span>{" "}
          <Cents value={r.p.avg_price} />
        </span>
      ) : (
        <span className="nowrap">
          <Qty value={r.p.quantity} base={r.p.base_currency} /> <span className="muted">@</span> <Price value={r.p.avg_cost} />
        </span>
      ),
  },
  {
    key: "mark",
    header: "Mark",
    align: "right",
    title: "Exit price now: Kalshi ¢ per contract when selling into the bid ladder; Coinbase USD per coin (liquidation price)",
    render: (r) =>
      r.venue === "kalshi" ? (
        r.p.mark_stale ? (
          <span title="Market closed, result pending: last pre-close value">
            <Cents value={r.p.mark_price} /> <span className="muted">(pre-close)</span>
          </span>
        ) : (
          <Cents value={r.p.mark_price} />
        )
      ) : (
        <Price value={r.p.mark_price} />
      ),
  },
  {
    key: "paid",
    header: "Cost basis",
    align: "right",
    title: "Paid for the open position including entry fees",
    sortValue: (r) => r.paid,
    render: (r) => <Usd value={r.paid} />,
  },
  {
    key: "value",
    header: "Value now",
    align: "right",
    title: "What selling the whole position now would fetch (after exit fees on Coinbase)",
    sortValue: (r) => r.value,
    render: (r) => <Usd value={r.value} />,
  },
  { key: "upnl", header: "Unrealized", align: "right", sortValue: (r) => r.upnl, render: (r) => <Pnl value={r.upnl} /> },
  {
    key: "upct",
    header: "Unreal. %",
    align: "right",
    title: "Unrealized P&L ÷ cost basis",
    sortValue: (r) => r.upct,
    render: (r) => <span className={`num tone-${pnlTone(r.upct, 0.005)}`}>{fmtPct(r.upct, { sign: true, dp: 1 })}</span>,
  },
  { key: "s", header: "Strategy", sortValue: (r) => r.strategy, render: (r) => <StrategyTag name={r.strategy} /> },
];

export function OpenPositionsBoth() {
  const cbOff = useCbOff();
  const kPoll = usePolling((signal) => api.positions({ signal }), { intervalMs: POLL_MS, refreshOn: ["fill", "settlement"] });
  const cPoll = useCbPolling((signal) => cbApi.positions({ signal }), { intervalMs: POLL_MS, enabled: cbOff === null, refreshOn: ["fill"] });
  const parts: [Part<Position[]>, Part<CbPosition[]>] = [partOf("kalshi", kPoll, null), partOf("coinbase", cPoll, cbOff)];
  const rows: PosRow[] = [
    ...(parts[0].state === "ok" ? parts[0].data.map(kalshiPos) : []),
    ...(parts[1].state === "ok" ? parts[1].data.filter((p) => p.quantity > 0).map(cbPos) : []),
  ];
  return (
    <Card
      title="Open positions, both venues"
      subtitle={`Each in its venue's own units: Kalshi contracts & ¢, Coinbase coins & USD. ${SEPARATE_NOTE}`}
      actions={<Freshness poll={combinedFreshness(cbOff === null ? [kPoll, cPoll] : [kPoll])} />}
      flush
    >
      <TwoVenueBody parts={parts} what="positions" rows={rows} empty={{ title: "No open positions" }}>
        {(r) => (
          <DataTable
            caption="Open positions, both venues"
            venue={null}
            rows={r}
            rowKey={(x) => x.key}
            defaultSort={{ key: "value", dir: "desc" }}
            maxHeight={480}
            columns={POS_COLUMNS}
          />
        )}
      </TwoVenueBody>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Recent activity, both venues
// ---------------------------------------------------------------------------

type ActRow = {
  key: string;
  t: number;
  ts: string;
  strategy: string;
  /** Cash in (+) / out (−) of the venue's account; null when none. */
  cash: number | null;
  /** Realized P&L of a settlement / close; null for fills. */
  pnl: number | null;
  fee: number | null;
} & ({ venue: "kalshi"; kind: "fill"; f: Fill } | { venue: "kalshi"; kind: "settlement"; s: Settlement } | { venue: "coinbase"; kind: "fill"; f: CbFill });

const plural = (n: number, one: string, many = `${one}s`) => `${fmtInt(n)} ${n === 1 ? one : many}`;

function kalshiActs(d: [Fill[], Settlement[]]): ActRow[] {
  const [fills, settlements] = d;
  return [
    ...fills.map(
      (f): ActRow => ({
        venue: "kalshi",
        kind: "fill",
        f,
        key: `kf:${f.id}`,
        t: parseTs(f.ts) ?? 0,
        ts: f.ts,
        strategy: f.strategy,
        cash: (f.action === "sell" ? 1 : -1) * f.count * f.price,
        pnl: null,
        fee: f.fee,
      }),
    ),
    ...settlements.map(
      (s): ActRow => ({
        venue: "kalshi",
        kind: "settlement",
        s,
        key: `ks:${s.id}`,
        t: parseTs(s.ts) ?? 0,
        ts: s.ts,
        strategy: s.strategy,
        cash: s.payout,
        pnl: s.pnl,
        fee: s.fees,
      }),
    ),
  ];
}

function cbActs(fills: CbFill[]): ActRow[] {
  return fills.map(
    (f): ActRow => ({
      venue: "coinbase",
      kind: "fill",
      f,
      key: `cf:${f.id}`,
      t: parseTs(f.ts) ?? 0,
      ts: f.ts,
      strategy: f.strategy,
      cash: (f.side === "sell" ? 1 : -1) * f.notional,
      pnl: null,
      fee: f.fee,
    }),
  );
}

/** What happened, in plain words. */
function ActWhat({ r }: { r: ActRow }) {
  if (r.venue === "coinbase") {
    const f = r.f;
    const base = f.product_id.split("-")[0] ?? f.product_id;
    return (
      <div className="ov-what">
        <span>
          <CbSideTag side={f.side} /> {f.side === "sell" ? "Sold" : "Bought"} <span className="num">{fmtQty(f.base_size, base)}</span> @{" "}
          <span className="num">{fmtPrice(f.price)}</span> <span className="muted">({f.is_taker ? "taker" : "maker"})</span>
        </span>
        <ProductCell pid={f.product_id} />
      </div>
    );
  }
  if (r.kind === "fill") {
    const f = r.f;
    return (
      <div className="ov-what">
        <span>
          {f.action === "sell" ? "Sold" : "Bought"} {plural(f.count, "contract")} <SideTag side={f.side} /> @ <span className="num">{fmtCents(f.price)}</span>
        </span>
        <MarketCell ticker={f.ticker} title={f.title} />
      </div>
    );
  }
  const s = r.s;
  const result = (s.result ?? "").toUpperCase();
  const held = s.side === "yes" || s.side === "no" ? s.side.toUpperCase() : null;
  const won = held && (result === "YES" || result === "NO") ? result === held : null;
  return (
    <div className="ov-what">
      <span>
        {s.kind === "close" ? (
          <>
            Closed {plural(s.count, "contract")} {sideLabel(s.side)} before the market resolved
          </>
        ) : (
          <>
            Settled {plural(s.count, "contract")} {sideLabel(s.side)}: market resolved {result || "?"}
            {won !== null && <strong className={won ? "tone-pos" : "tone-neg"}>{won ? " — won" : " — lost"}</strong>}
          </>
        )}
      </span>
      <MarketCell ticker={s.ticker} title={s.title} />
    </div>
  );
}

const ACT_COLUMNS: Column<ActRow>[] = [
  { key: "t", header: "Time", sortValue: (r) => r.t, render: (r) => <Time value={r.ts} stack /> },
  venueColumn<ActRow>(),
  { key: "what", header: "What happened", minWidth: 260, render: (r) => <ActWhat r={r} /> },
  {
    key: "cash",
    header: "Amount",
    align: "right",
    title: "Cash into (+) or out of (−) that venue's paper account: price × size for fills, the payout for settlements",
    sortValue: (r) => r.cash,
    render: (r) => <span className="num">{fmtUsd(r.cash, { sign: true })}</span>,
  },
  {
    key: "pnl",
    header: "P&L",
    align: "right",
    title: "Realized P&L of a Kalshi settlement or early close (fills show —)",
    sortValue: (r) => r.pnl,
    render: (r) => (r.pnl === null ? <span className="muted">{DASH}</span> : <Pnl value={r.pnl} />),
  },
  {
    key: "fee",
    header: "Fee",
    align: "right",
    sortValue: (r) => r.fee,
    render: (r) => (r.venue === "coinbase" ? <Fee fee={r.f.fee} rate={r.f.fee_rate} notional={r.f.notional} /> : <Usd value={r.fee} />),
  },
  { key: "s", header: "Strategy", sortValue: (r) => r.strategy, render: (r) => <StrategyTag name={r.strategy} /> },
];

export function RecentActivityBoth() {
  const cbOff = useCbOff();
  const kPoll = usePolling(
    (signal) => Promise.all([api.fills(FEED_LIMIT, { signal }), api.settlements(FEED_LIMIT, { signal })]) as Promise<[Fill[], Settlement[]]>,
    { intervalMs: POLL_MS, refreshOn: ["fill", "settlement"] },
  );
  const cPoll = useCbPolling((signal) => cbApi.fills(FEED_LIMIT, { signal }), { intervalMs: POLL_MS, enabled: cbOff === null, refreshOn: ["fill"] });
  const parts: [Part<[Fill[], Settlement[]]>, Part<CbFill[]>] = [partOf("kalshi", kPoll, null), partOf("coinbase", cPoll, cbOff)];
  const rows: ActRow[] = [...(parts[0].state === "ok" ? kalshiActs(parts[0].data) : []), ...(parts[1].state === "ok" ? cbActs(parts[1].data) : [])].sort(
    (a, b) => b.t - a.t,
  );
  return (
    <Card
      title="Recent activity, both venues"
      subtitle={`Newest first: Kalshi fills and settlements, Coinbase fills (last ${FEED_LIMIT} of each). Amounts are in each venue's own account.`}
      actions={<Freshness poll={combinedFreshness(cbOff === null ? [kPoll, cPoll] : [kPoll])} />}
      flush
    >
      <TwoVenueBody parts={parts} what="activity" rows={rows} empty={{ title: "No trades yet" }}>
        {(r) => <DataTable caption="Recent activity, both venues" venue={null} rows={r} rowKey={(x) => x.key} maxHeight={520} pageSize={40} columns={ACT_COLUMNS} />}
      </TwoVenueBody>
    </Card>
  );
}
