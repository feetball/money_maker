/**
 * Building blocks shared by every Coinbase page (F2): the page shell (venue scope +
 * banner + live stream), Coinbase-specific polling, engine state and controls, the
 * activity feed, and value cells in Coinbase units (contract §14).
 */
import "./coinbase.css";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ApiError, errorMessage, isUnreachable } from "../../api/client";
import { CB_MISSING_REASON, cbApi, completeCbAccount, isCbMissing, isCbUnavailable } from "../../api/coinbase/client";
import { useCbStatus } from "../../api/coinbase/status";
import {
  cbStreamStore,
  useCbRefreshSignal,
  useCbStreamAccount,
  useCbStreamConnection,
  useCbStreamEvents,
  useCbStreamInfo,
} from "../../api/coinbase/stream";
import type {
  CbAccount,
  CbDecision,
  CbEngineStatus,
  CbLog,
  CbSide,
  CbStatus,
  CbStreamEvent,
  CbStreamEventType,
} from "../../api/coinbase/types";
import { useConfirm } from "../../components/ConfirmDialog";
import { Icon } from "../../components/Icon";
import { Badge, ErrorBlock, Segmented, Switch, type Tone } from "../../components/ui";
import { VenueBadge, VenueBanner } from "../../components/Venue";
import { Time } from "../../components/values";
import { DASH, fmtAbsolute, fmtPnl, fmtRelative, fmtUsd, parseTs, pnlTone } from "../../lib/format";
import { useAction, useNow, usePolling, useServerNow, type PollOptions, type PollResult } from "../../lib/hooks";
import { useOverview } from "../../lib/overview";
import { useToast } from "../../lib/toast";
import { VenueScope } from "../../lib/venueScope";
import { baseOf, fmtBps, fmtFee, fmtPrice, fmtQty, fmtRate, fmtWeight } from "./format";

/** Every Coinbase route lives under this prefix (contract §14). */
export const CB_BASE = "/coinbase";

// ---------------------------------------------------------------------------
// Page shell
// ---------------------------------------------------------------------------

/**
 * Wraps a Coinbase page: marks the subtree as the Coinbase paper account (tables,
 * KPI tiles, toasts and dialogs get the Coinbase badge), puts the Coinbase banner at
 * the very top, holds the Coinbase SSE connection, and shows Coinbase-only alerts.
 * Hooks that raise toasts/dialogs must run in components rendered INSIDE this.
 */
export function CbPage({ children }: { children: ReactNode }) {
  useCbStreamConnection();
  return (
    <VenueScope venue="coinbase">
      <div className="page">
        <VenueBanner venue="coinbase">
          <CbBannerExtra />
        </VenueBanner>
        <CbAlerts />
        {children}
      </div>
    </VenueScope>
  );
}

function CbBannerExtra() {
  const { status, error } = useCbStatus();
  const now = useServerNow();
  const st = cbEngineState(status, error, now);
  const tier = status?.fee_tier;
  return (
    <span className="cb-banner-extra">
      <span>
        Engine: <strong>{CB_STATE[st].label}</strong>
      </span>
      {tier && tier.name !== "—" && (
        <span title="Coinbase fee tier used by the paper broker (fees charged in USD on every fill)">
          Fees {tier.name}: maker {fmtRate(tier.maker_rate)} · taker {fmtRate(tier.taker_rate)}
        </span>
      )}
      <CbStreamIndicator />
      {/* Same place as the Kalshi banner's controls: the emergency stop is on every Coinbase page. */}
      <CbEngineControls compact />
    </span>
  );
}

function CbAlerts() {
  const { status, error, updatedAt } = useCbStatus();
  const serverNow = useServerNow();
  const out: ReactNode[] = [];
  const lastSeen = updatedAt ? fmtAbsolute(new Date(updatedAt).toISOString(), { seconds: true }) : null;
  if (error) {
    if (isCbMissing(error)) {
      out.push(
        <div key="unavail" className="banner banner-bad" role="alert">
          <Icon name="plug" />
          <div>
            <strong>Coinbase venue unavailable on this server.</strong> {CB_MISSING_REASON} The Kalshi paper account is not affected.
          </div>
        </div>,
      );
    } else if (isCbUnavailable(error)) {
      out.push(
        <div key="unavail" className="banner banner-bad" role="alert">
          <Icon name="plug" />
          <div>
            <strong>Coinbase venue unavailable.</strong> <span className="mono wrap">{errorMessage(error)}</span> The Kalshi paper account is not affected.
            {status && lastSeen ? ` Showing the last known Coinbase state from ${lastSeen}.` : ""}
          </div>
        </div>,
      );
    } else if (!isUnreachable(error)) {
      out.push(
        <div key="err" className="banner banner-serious" role="alert">
          <Icon name="alert" />
          <div>
            <strong>Coinbase status failed:</strong> <span className="mono wrap">{errorMessage(error)}</span>
          </div>
        </div>,
      );
    }
    // "Backend unreachable" is announced once for the whole app by the shell.
  }
  const e = status?.engine;
  if (e?.kill_switch) {
    out.push(
      <div key="kill" className="banner banner-bad" role="status">
        <Icon name="shield" />
        <div>
          <strong>Coinbase kill switch is ON{error ? " (last known state)" : ""}.</strong> Coinbase strategies cannot buy; sells that reduce risk are still
          allowed. The Kalshi account is not affected.
          {e.kill_switch_reason && (
            <>
              {" "}
              Reason: <span className="mono wrap">{e.kill_switch_reason}</span>
            </>
          )}
        </div>
      </div>,
    );
  }
  if (e && e.coinbase_reachable === false) {
    out.push(
      <div key="api" className="banner banner-warn" role="status">
        <Icon name="plug" />
        <div>
          <strong>Coinbase public API unreachable.</strong> The engine is backing off and retrying; prices, marks and fills may be stale until it answers.
        </div>
      </div>,
    );
  }
  if (e?.last_error && cbErrorIsCurrent(e, serverNow)) {
    out.push(
      <div key="lasterr" className="banner banner-serious" role="status">
        <Icon name="alert" />
        <div>
          <strong>Coinbase engine error{e.last_error_at ? ` at ${fmtAbsolute(e.last_error_at, { seconds: true })}` : ""}:</strong>{" "}
          <span className="mono wrap">{e.last_error}</span>
          {e.last_error_at && <span className="muted"> Clears 10 minutes after the error if the engine keeps running.</span>}
        </div>
      </div>,
    );
  }
  return out.length ? <div className="banners">{out}</div> : null;
}

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------

export type CbPollOptions<T> = Omit<PollOptions<T>, "refreshOn"> & {
  /** Coinbase SSE event types that trigger an immediate (debounced) refetch. */
  refreshOn?: readonly CbStreamEventType[];
};

/**
 * usePolling for Coinbase endpoints: `refreshOn` listens to the COINBASE stream
 * (usePolling's own refreshOn would react to Kalshi events).
 */
export function useCbPolling<T>(fetcher: (signal: AbortSignal) => Promise<T>, opts: CbPollOptions<T> = {}): PollResult<T> {
  const { refreshOn, silentWhen, ...rest } = opts;
  // A down/missing Coinbase venue fails every endpoint at once; CbAlerts shows one
  // venue-level banner for it, so no per-panel "Couldn't load …" toasts.
  const poll = usePolling(fetcher, { ...rest, silentWhen: (e) => isCbUnavailable(e) || (silentWhen?.(e) ?? false) });
  const { refresh } = poll;
  const debounce = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useCbRefreshSignal(refreshOn, () => {
    clearTimeout(debounce.current);
    debounce.current = setTimeout(refresh, 600);
  });
  useEffect(() => () => clearTimeout(debounce.current), []);
  return poll;
}

/** Polled Coinbase account with a newer SSE `account` event merged over it field by field. */
export function useCbLiveAccount() {
  const poll = useCbPolling((signal) => cbApi.account({ signal }), { intervalMs: 5000, label: "Coinbase account", refreshOn: ["fill"] });
  const stream = useCbStreamAccount();
  const live = stream && (poll.requestedAt === null || stream.receivedAt > poll.requestedAt) ? stream : null;
  const data: CbAccount | undefined = useMemo(() => {
    if (!live) return poll.data;
    if (poll.data) return { ...poll.data, ...live.data };
    return completeCbAccount(live.data) ?? undefined;
  }, [live, poll.data]);
  return { poll, data, live };
}

// ---------------------------------------------------------------------------
// Engine state + controls
// ---------------------------------------------------------------------------

export type CbEngineState =
  | "unavailable"
  | "unreachable"
  | "backend-error"
  | "loading"
  | "killed"
  | "api-down"
  | "error"
  | "stalled"
  | "running"
  | "stopped";

const STALL_MS = 5 * 60_000;
const ERROR_CURRENT_MS = 10 * 60_000;

export const CB_STATE: Record<CbEngineState, { cls: string; label: string; icon: "dot" | "stop" | "shield" | "alert" | "plug" | "clock" }> = {
  unavailable: { cls: "bad", label: "Venue unavailable", icon: "plug" },
  unreachable: { cls: "bad", label: "Backend unreachable", icon: "plug" },
  "backend-error": { cls: "serious", label: "Status unavailable", icon: "alert" },
  loading: { cls: "neutral", label: "Connecting…", icon: "clock" },
  killed: { cls: "bad", label: "Kill switch ON", icon: "shield" },
  "api-down": { cls: "warn", label: "Running · Coinbase API down", icon: "plug" },
  error: { cls: "serious", label: "Running · error", icon: "alert" },
  stalled: { cls: "warn", label: "Running · stalled", icon: "alert" },
  running: { cls: "good", label: "Running", icon: "dot" },
  stopped: { cls: "neutral", label: "Stopped", icon: "stop" },
};

/** An engine error is current while < 10 min old or when nothing has ticked since. */
export function cbErrorIsCurrent(e: CbEngineStatus | undefined, now: number): boolean {
  if (!e?.last_error) return false;
  const at = parseTs(e.last_error_at);
  if (at === null) return true;
  if (now - at < ERROR_CURRENT_MS) return true;
  const tick = parseTs(e.last_tick_at);
  return tick === null || at >= tick;
}

/** `now` must be server time (useServerNow). The error is checked first (stale data is kept). */
export function cbEngineState(status: CbStatus | undefined, error: unknown, now: number): CbEngineState {
  if (error) return isCbUnavailable(error) ? "unavailable" : isUnreachable(error) ? "unreachable" : "backend-error";
  if (!status) return "loading";
  const e = status.engine;
  if (e.kill_switch) return "killed";
  if (!e.running) return "stopped";
  if (e.coinbase_reachable === false) return "api-down";
  if (cbErrorIsCurrent(e, now)) return "error";
  const last = parseTs(e.last_tick_at) ?? parseTs(e.started_at);
  if (last !== null && now - last > STALL_MS) return "stalled";
  return "running";
}

export function CbEnginePill() {
  const { status, error } = useCbStatus();
  const now = useServerNow();
  const st = cbEngineState(status, error, now);
  const p = CB_STATE[st];
  const e = status?.engine;
  const detail = error
    ? `Coinbase status: ${errorMessage(error)}`
    : e
      ? [
          `Coinbase engine ${e.running ? "running" : "stopped"}${e.kill_switch ? " · kill switch engaged (no buys)" : ""}`,
          e.last_tick_at ? `Last tick ${fmtRelative(e.last_tick_at, now)} (#${e.tick_count})` : "No ticks yet",
          e.last_bar_at ? `Last bar evaluated ${fmtRelative(e.last_bar_at, now)}` : null,
        ]
          .filter(Boolean)
          .join("\n")
      : "Waiting for /api/coinbase/status";
  return (
    <span className={`pill pill-${p.cls}`} title={detail}>
      <Icon name={p.icon} className={st === "running" ? "pulse" : undefined} />
      <span className="pill-label">{p.label}</span>
    </span>
  );
}

function KillReason({ known }: { known: string | null }) {
  const [reason, setReason] = useState<string | null | undefined>(known ?? undefined);
  useEffect(() => {
    if (known) return;
    const ctrl = new AbortController();
    cbApi
      .risk({ signal: ctrl.signal })
      .then((r) => setReason(r.kill_switch_reason))
      .catch(() => setReason(null));
    return () => ctrl.abort();
  }, [known]);
  if (reason === undefined) return <p className="muted">Looking up why it was engaged…</p>;
  if (!reason) return <p className="muted">The backend did not report a reason (usually a manual engage).</p>;
  return (
    <p>
      Reason: <strong className="mono wrap">{reason}</strong>
      {/daily loss/i.test(reason) && " — tripped automatically by the Coinbase daily loss limit. Check today's Coinbase P&L before releasing."}
    </p>
  );
}

/** Start/stop + kill switch for the COINBASE engine only; every dialog names the venue. */
export function CbEngineControls({ compact }: { compact?: boolean }) {
  const { status, error, apply, refresh, busy, begin, end } = useCbStatus();
  const confirm = useConfirm();
  const toast = useToast();
  const overview = useOverview();
  const { run } = useAction();
  const running = status?.engine.running ?? false;
  const killed = status?.engine.kill_switch ?? false;
  const staleNote = error ? ` — disabled: ${errorMessage(error)}` : "";
  const startStopDisabled = !status || !!error || busy !== null;
  const killDisabled = !status || busy !== null || (!!error && (isUnreachable(error) || isCbUnavailable(error) || killed));

  const act = async <R,>(key: "engine" | "kill", fn: () => Promise<R>, messages: { success?: string; error: string }) => {
    if (!begin(key)) return undefined;
    try {
      return await run(key, fn, messages);
    } finally {
      end();
    }
  };

  const startStop = async () => {
    const ok = await confirm(
      running
        ? {
            title: "Stop the Coinbase engine?",
            body: (
              <>
                <p>Coinbase strategies stop evaluating bars and placing orders. Coinbase positions and resting orders are kept (resting orders can still fill).</p>
                <p>The Kalshi engine is separate and keeps running.</p>
              </>
            ),
            confirmLabel: "Stop Coinbase engine",
          }
        : {
            title: "Start the Coinbase engine?",
            body: (
              <>
                <p>
                  Enabled Coinbase strategies ({status?.engine.strategies_enabled.length ? status.engine.strategies_enabled.join(", ") : "none enabled"}) resume at the
                  next bar close. Orders are simulated against live public Coinbase books — paper only, no real orders.
                </p>
              </>
            ),
            confirmLabel: "Start Coinbase engine",
          },
    );
    if (!ok) return;
    const r = await act("engine", () => (running ? cbApi.stopEngine() : cbApi.startEngine()), {
      success: running ? "Coinbase engine stopped" : "Coinbase engine started",
      error: running ? "Couldn't stop the Coinbase engine" : "Couldn't start the Coinbase engine",
    });
    if (r) apply(r);
    else refresh();
    overview.refresh();
  };

  const toggleKill = async () => {
    const on = !killed;
    const ok = await confirm(
      on
        ? {
            title: "Engage the Coinbase kill switch?",
            body: (
              <>
                <p>Coinbase strategies stop buying immediately and every resting Coinbase order is cancelled. Sells that reduce risk are still allowed; Coinbase holdings stay open.</p>
                <p>The Kalshi account has its own kill switch and is not affected.</p>
              </>
            ),
            confirmLabel: "Engage Coinbase kill switch",
            danger: true,
          }
        : {
            title: "Release the Coinbase kill switch?",
            body: (
              <>
                <KillReason known={status?.engine.kill_switch_reason ?? null} />
                <p>Coinbase strategies may buy again at their next bar (if the Coinbase engine is running).</p>
              </>
            ),
            confirmLabel: "Release Coinbase kill switch",
          },
    );
    if (!ok) return;
    const r = await act("kill", () => cbApi.setKillSwitch(on), {
      error: on ? "Couldn't engage the Coinbase kill switch" : "Couldn't release the Coinbase kill switch",
    });
    if (r) {
      apply(r);
      if (on) {
        toast.info("Coinbase kill switch engaged — buys blocked, resting Coinbase orders cancelled");
        cbStreamStore.invalidate(["order", "fill"]);
      } else toast.success("Coinbase kill switch released");
    } else refresh();
    overview.refresh();
  };

  return (
    <div className={`engine-controls${compact ? " compact" : ""}`}>
      <button
        className={running ? "btn" : "btn btn-primary"}
        onClick={startStop}
        disabled={startStopDisabled}
        aria-busy={busy === "engine"}
        title={(running ? "Stop the Coinbase trading loop (holdings are kept)" : "Start the Coinbase trading loop") + staleNote}
      >
        <Icon name={running ? "stop" : "play"} />
        <span className="btn-label">{running ? "Stop Coinbase" : "Start Coinbase"}</span>
      </button>
      <button
        className={killed ? "btn btn-danger-solid" : "btn btn-danger"}
        onClick={toggleKill}
        disabled={killDisabled}
        aria-busy={busy === "kill"}
        title={(killed ? "Coinbase kill switch is ON — click to release" : "Block all Coinbase buys and cancel resting Coinbase orders") + (killDisabled ? staleNote : "")}
      >
        <Icon name="shield" />
        <span className="btn-label">{killed ? "Release Coinbase kill switch" : "Coinbase kill switch"}</span>
      </button>
    </div>
  );
}

export function CbStreamIndicator() {
  const info = useCbStreamInfo();
  const now = useNow();
  const label = info.state === "open" ? "Live" : info.state === "reconnecting" ? "Reconnecting" : info.state === "connecting" ? "Connecting" : "Off";
  const retry = info.nextRetryAt && info.nextRetryAt > now ? ` · next attempt ${fmtRelative(new Date(info.nextRetryAt).toISOString(), now)}` : "";
  const title =
    info.state === "open"
      ? "Coinbase live event stream connected (/api/coinbase/stream)"
      : `Coinbase event stream ${info.state}${info.attempts ? ` · ${info.attempts} failed attempt(s)` : ""}${retry}; data still refreshes by polling`;
  return (
    <span className={`stream stream-${info.state}`} title={title}>
      <span className="stream-dot" aria-hidden="true" />
      <span className="stream-label">{label}</span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Value cells (Coinbase units)
// ---------------------------------------------------------------------------

export function Qty({ value, base, sign }: { value: number | null | undefined; base?: string | null; sign?: boolean }) {
  return <span className="num nowrap">{fmtQty(value, base, { sign })}</span>;
}

export function Price({ value }: { value: number | null | undefined }) {
  return <span className="num nowrap">{fmtPrice(value)}</span>;
}

export function Fee({ fee, rate, notional }: { fee: number | null | undefined; rate?: number | null; notional?: number | null }) {
  return <span className="num nowrap">{fmtFee(fee, rate, notional)}</span>;
}

export function Bps({ value, sign, tone }: { value: number | null | undefined; sign?: boolean; tone?: boolean }) {
  return <span className={`num nowrap${tone ? ` tone-${pnlTone(value, 0.05)}` : ""}`}>{fmtBps(value, { sign })}</span>;
}

export function Weight({ value }: { value: number | null | undefined }) {
  return <span className="num">{fmtWeight(value)}</span>;
}

/** BUY / SELL in words (never colour alone, never P&L or status colours). */
export function CbSideTag({ side }: { side: CbSide | null | undefined }) {
  if (side !== "buy" && side !== "sell") return <span className="muted">{DASH}</span>;
  return <span className={`cb-side cb-side-${side}`}>{side.toUpperCase()}</span>;
}

export function coinbaseUrl(pid: string, url?: string | null): string {
  return url && /^https?:\/\//i.test(url) ? url : `https://www.coinbase.com/advanced-trade/spot/${encodeURIComponent(pid)}`;
}

/** Product id linked to Coinbase Advanced Trade, with an optional sub-line. */
export function ProductCell({ pid, url, sub }: { pid: string; url?: string | null; sub?: ReactNode }) {
  if (!pid) return <span className="muted">{DASH}</span>;
  return (
    <span className="cb-product">
      <a className="ext-link" href={coinbaseUrl(pid, url)} target="_blank" rel="noopener noreferrer" title={`Open ${pid} on coinbase.com (new tab)`}>
        <span className="ticker">{pid}</span>
        <Icon name="external" />
        <span className="sr-only"> (opens coinbase.com in a new tab)</span>
      </a>
      {sub && <span className="cb-product-sub">{sub}</span>}
    </span>
  );
}

const DECISION: Record<CbDecision, { tone: Tone; label: string; icon: "check" | "alert" | "x" | "dot" | "clock" }> = {
  executed: { tone: "good", label: "Executed", icon: "check" },
  partial: { tone: "warn", label: "Partial", icon: "dot" },
  rejected: { tone: "bad", label: "Rejected", icon: "x" },
  unfilled: { tone: "neutral", label: "Unfilled", icon: "dot" },
  resting: { tone: "info", label: "Resting", icon: "clock" },
  unknown: { tone: "neutral", label: "Pending", icon: "clock" },
};

export function cbDecisionLabel(d: CbDecision, raw?: string): string {
  return d === "unknown" && raw ? raw : DECISION[d].label;
}

export function CbDecisionBadge({ decision, raw }: { decision: CbDecision; raw?: string }) {
  const d = DECISION[decision];
  const title =
    decision === "resting"
      ? "Posted as a resting (maker) order; fills only from later public trades through its price"
      : decision === "unknown"
        ? raw
          ? `Unrecognised decision "${raw}"`
          : "No decision reported yet"
        : undefined;
  return (
    <Badge tone={d.tone} icon={d.icon} title={title}>
      {cbDecisionLabel(decision, raw)}
    </Badge>
  );
}

/** "0.5 of BTC" style summary of an order's size: base qty or USD amount. */
export function sizeText(p: { side: CbSide | null; quote_size: number | null; base_size: number | null; product_id: string }): string {
  const base = baseOf(p.product_id);
  if (p.base_size !== null && (p.side === "sell" || p.quote_size === null)) return fmtQty(p.base_size, base);
  if (p.quote_size !== null) return `${fmtUsd(p.quote_size)} of ${base}`;
  return DASH;
}

// ---------------------------------------------------------------------------
// Activity feed (Coinbase SSE + /api/coinbase/logs)
// ---------------------------------------------------------------------------

type FeedFilter = "all" | "trades" | "logs" | "problems";
const LOG_DEDUPE_MS = 2000;

interface FeedItem {
  key: string;
  ts: string;
  kind: string;
  tone: "neutral" | "good" | "warn" | "bad" | "info";
  text: string;
  live: boolean;
  group: "trade" | "log";
}

function fromLog(l: CbLog, key: string): FeedItem {
  const lvl = l.level.toLowerCase();
  return {
    key,
    ts: l.ts,
    kind: `${l.kind}${lvl !== "info" ? ` · ${lvl}` : ""}`,
    tone: lvl === "error" || lvl === "critical" ? "bad" : lvl === "warning" ? "warn" : lvl === "debug" ? "neutral" : "info",
    text: l.message,
    live: false,
    group: "log",
  };
}

function fromEvent(e: CbStreamEvent): FeedItem | null {
  const base = { key: `cbsse-${e.seq}`, live: true };
  const fallbackTs = new Date(e.receivedAt).toISOString();
  switch (e.type) {
    case "signal": {
      const s = e.data;
      const tone = s.decision === "executed" ? "good" : s.decision === "rejected" ? "bad" : s.decision === "resting" ? "info" : s.decision === "unknown" ? "neutral" : "warn";
      const w = s.target_weight !== null ? ` → target ${fmtWeight(s.target_weight)}` : "";
      return {
        ...base,
        ts: s.ts || fallbackTs,
        kind: `signal · ${cbDecisionLabel(s.decision, s.decision_raw).toLowerCase()}`,
        tone,
        group: "trade",
        text: `${s.strategy}: ${s.side ? s.side.toUpperCase() : "?"} ${s.product_id}${w} (${sizeText(s)})${s.decision_reason ? ` — ${s.decision_reason}` : ""}`,
      };
    }
    case "order": {
      const o = e.data;
      const base2 = baseOf(o.product_id);
      const px = o.limit_price !== null ? ` @ ${fmtPrice(o.limit_price)}` : " market";
      return {
        ...base,
        ts: o.updated_at ?? o.created_at ?? fallbackTs,
        kind: `order · ${(o.status === "unknown" ? o.status_raw || "unknown" : o.status).replace("_", " ")}`,
        tone: o.status === "rejected" ? "bad" : o.status === "filled" ? "good" : o.status === "unknown" ? "neutral" : "info",
        group: "trade",
        text: `#${String(o.id)} ${o.side.toUpperCase()} ${o.product_id}${px}: filled ${fmtQty(o.filled_base, base2)} (${o.tif.toUpperCase()}${o.post_only ? ", post-only" : ""}, ${o.strategy || "no strategy"})`,
      };
    }
    case "fill": {
      const f = e.data;
      return {
        ...base,
        ts: f.ts || fallbackTs,
        kind: `fill · ${f.is_taker ? "taker" : "maker"}`,
        tone: "good",
        group: "trade",
        text: `${f.strategy}: ${f.side.toUpperCase()} ${fmtQty(f.base_size, baseOf(f.product_id))} @ ${fmtPrice(f.price)} = ${fmtUsd(f.notional)} · fee ${fmtFee(f.fee, f.fee_rate, f.notional)}`,
      };
    }
    case "bar": {
      const b = e.data;
      return {
        ...base,
        ts: b.ts ?? fallbackTs,
        kind: "bar",
        tone: "neutral",
        group: "log",
        text: `${b.strategy || "strategy"}: bar closed ${b.bar_end ? fmtAbsolute(b.bar_end) : ""}${b.intents !== null ? ` · ${b.intents} order intent(s)` : ""}`,
      };
    }
    case "log":
      return { ...fromLog(e.data, `cbsse-${e.seq}`), live: true };
    default:
      return null;
  }
}

export function CbActivityFeed({ maxItems = 150, height = 420 }: { maxItems?: number; height?: number }) {
  const events = useCbStreamEvents();
  const info = useCbStreamInfo();
  const logs = useCbPolling((signal) => cbApi.logs(100, { signal }), { intervalMs: 10_000, label: "Coinbase activity log" });
  const [filter, setFilter] = useState<FeedFilter>("all");
  const [paused, setPaused] = useState(false);
  const [frozen, setFrozen] = useState<FeedItem[] | null>(null);

  const items = useMemo(() => {
    const out: FeedItem[] = [];
    const seen = new Set<string>();
    const rest = logs.data ?? [];
    const sig = (l: CbLog) => `${l.level}|${l.kind}|${l.message}`;
    const restTimes = new Map<string, (number | null)[]>();
    for (const l of rest) {
      const arr = restTimes.get(sig(l));
      if (arr) arr.push(parseTs(l.ts));
      else restTimes.set(sig(l), [parseTs(l.ts)]);
    }
    const polledCopy = (l: CbLog) => {
      const times = restTimes.get(sig(l));
      if (!times) return false;
      const t = parseTs(l.ts);
      return times.some((x) => t === null || x === null || Math.abs(x - t) <= LOG_DEDUPE_MS);
    };
    for (const e of events) {
      if (e.type === "account" || e.type === "tick") continue;
      if (e.type === "log" && polledCopy(e.data)) continue;
      const it = fromEvent(e);
      if (it) out.push(it);
    }
    for (const l of rest) {
      const k = l.id !== null ? `cblog-${String(l.id)}` : `cblog-${l.ts}|${sig(l)}`;
      if (seen.has(k)) continue;
      seen.add(k);
      out.push(fromLog(l, k));
    }
    out.sort((a, b) => (parseTs(b.ts) ?? 0) - (parseTs(a.ts) ?? 0));
    return out;
  }, [events, logs.data]);

  const source = paused && frozen ? frozen : items;
  const filtered = source
    .filter((it) =>
      filter === "all" ? true : filter === "trades" ? it.group === "trade" : filter === "logs" ? it.group === "log" : it.tone === "bad" || it.tone === "warn",
    )
    .slice(0, maxItems);

  return (
    <div className="feed">
      <div className="feed-tools">
        <Segmented<FeedFilter>
          label="Coinbase activity filter"
          value={filter}
          onChange={setFilter}
          options={[
            { value: "all", label: "All" },
            { value: "trades", label: "Trades" },
            { value: "logs", label: "Logs" },
            { value: "problems", label: "Problems" },
          ]}
        />
        <span className="inline-toggle">
          <Switch
            checked={paused}
            onChange={(v) => {
              setPaused(v);
              setFrozen(v ? items : null);
            }}
            label="Pause Coinbase feed"
            text="Pause"
          />
        </span>
        <span className="feed-meta" title={logs.error ? `Log history: ${errorMessage(logs.error)}` : undefined}>
          {info.state === "open" ? "live" : `stream ${info.state}`}
          {logs.error && logs.data === undefined && events.length > 0 ? " · log history unavailable" : ""}
        </span>
      </div>
      {filtered.length === 0 && logs.error && logs.data === undefined ? (
        <ErrorBlock error={logs.error} onRetry={logs.refresh} />
      ) : filtered.length === 0 ? (
        <div className="state-block empty">
          <div>
            <div className="state-title">{logs.loading ? "Loading Coinbase activity…" : "No Coinbase activity yet"}</div>
            <div className="state-hint">Coinbase engine events stream here as they happen (bars, signals, orders, fills, logs).</div>
          </div>
        </div>
      ) : (
        <ol className="feed-list" style={{ maxHeight: height }} aria-live="off">
          {filtered.map((it) => (
            <li key={it.key} className={`feed-item tone-${it.tone}${it.live ? " live" : ""}`}>
              <span className="feed-dot" aria-hidden="true" />
              <div className="feed-main">
                <div className="feed-line">
                  <VenueBadge venue="coinbase" />
                  <span className="feed-kind">{it.kind}</span>
                  <Time value={it.ts} seconds />
                </div>
                <div className="feed-text">{it.text}</div>
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Misc
// ---------------------------------------------------------------------------

/** "is 409 not open" handling for cancels: returns null instead of throwing on 409. */
export function tolerate409<T>(p: Promise<T>, onConflict: (detail: string) => void): Promise<T | null> {
  return p.catch((e: unknown) => {
    if (e instanceof ApiError && e.status === 409) {
      onConflict(e.detail);
      return null;
    }
    throw e;
  });
}

export const uniqSorted = (xs: string[]) => [...new Set(xs.filter(Boolean))].sort();

/** Strategy filter; the selected value stays listed ("no rows") after its last row disappears. */
export function CbStrategyFilter({ value, onChange, options }: { value: string; onChange: (v: string) => void; options: string[] }) {
  const missing = value !== "" && !options.includes(value);
  return (
    <label className="inline-field">
      <span className="sr-only">Coinbase strategy</span>
      <select className="input input-sm" value={value} onChange={(e) => onChange(e.target.value)} aria-label="Filter by Coinbase strategy">
        <option value="">All strategies</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
        {missing && <option value={value}>{value} (no rows)</option>}
      </select>
    </label>
  );
}

/** Signed P&L text with a percentage: "+$12.30 (+4.1%)". */
export function pnlWithPct(v: number | null | undefined, pct: number | null | undefined): string {
  const p = typeof pct === "number" && Number.isFinite(pct) ? ` (${pct >= 0 ? "+" : "−"}${Math.abs(pct).toFixed(1)}%)` : "";
  return `${fmtPnl(v)}${p}`;
}
