import { useEffect, useRef, useState } from "react";
import { api, ApiError, errorMessage, isUnreachable } from "../api/client";
import type { EngineStatus, StatusResponse } from "../api/types";
import { useAction, useNow, useServerNow } from "../lib/hooks";
import { fmtRelative, parseTs } from "../lib/format";
import { useStatus, type EngineBusy } from "../lib/status";
import { streamStore, useStreamInfo } from "../lib/stream";
import { useToast } from "../lib/toast";
import { useConfirm } from "./ConfirmDialog";
import { Icon } from "./Icon";

export type EngineState = "unreachable" | "backend-error" | "loading" | "killed" | "error" | "stalled" | "running" | "stopped";

/** Ticks older than this while "running" are flagged as stalled. */
const STALL_MS = 5 * 60_000;
/** An engine error younger than this is shown as current even if ticks continue. */
export const ERROR_CURRENT_MS = 10 * 60_000;

/**
 * Whether `last_error` describes the CURRENT state. The backend keeps the last job
 * failure until an account reset (it is not cleared when the job recovers), so an error
 * counts as current only while it is recent (< 10 min) or no tick has run since it.
 * Without `last_error_at` we cannot tell, and treat it as current (the safe side).
 * `now` must be on the server's clock (useServerNow).
 */
export function engineErrorIsCurrent(e: EngineStatus | undefined, now: number): boolean {
  if (!e?.last_error) return false;
  const at = parseTs(e.last_error_at);
  if (at === null) return true;
  if (now - at < ERROR_CURRENT_MS) return true;
  const tick = parseTs(e.last_tick_at);
  return tick === null || at >= tick;
}

/**
 * Engine state for the header pill / Engine card. The error is checked FIRST:
 * usePolling keeps the last good payload after a failure, so a backend that dies
 * mid-session must not keep reading "Running". `now` is server time (useServerNow).
 */
export function engineState(status: StatusResponse | undefined, error: unknown, now: number): EngineState {
  if (error) return isUnreachable(error) ? "unreachable" : "backend-error";
  if (!status) return "loading";
  const e = status.engine;
  if (e.kill_switch) return "killed";
  if (!e.running) return "stopped";
  if (engineErrorIsCurrent(e, now)) return "error";
  const last = parseTs(e.last_tick_at) ?? parseTs(e.started_at);
  if (last !== null && now - last > STALL_MS) return "stalled";
  return "running";
}

/** "HTTP 500 from /api/status" / "no answer from the backend" for banners and tooltips. */
export function statusErrorSummary(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.kind === "timeout") return "/api/status did not answer in time";
    if (error.kind === "network") return "no answer from the backend";
    return `/api/status returned HTTP ${error.status}`;
  }
  return errorMessage(error);
}

const PILL: Record<EngineState, { cls: string; label: string; icon: "dot" | "stop" | "shield" | "alert" | "plug" | "clock" }> = {
  unreachable: { cls: "bad", label: "Backend unreachable", icon: "plug" },
  "backend-error": { cls: "serious", label: "Status unavailable", icon: "alert" },
  loading: { cls: "neutral", label: "Connecting…", icon: "clock" },
  killed: { cls: "bad", label: "Kill switch ON", icon: "shield" },
  error: { cls: "serious", label: "Running · error", icon: "alert" },
  stalled: { cls: "warn", label: "Running · stalled", icon: "alert" },
  running: { cls: "good", label: "Running", icon: "dot" },
  stopped: { cls: "neutral", label: "Stopped", icon: "stop" },
};

export function engineStateLabel(st: EngineState): string {
  return PILL[st].label;
}

/** One-line summary of the last status payload (for "last known" wording). */
export function lastKnownEngine(status: StatusResponse | undefined): string | null {
  if (!status) return null;
  const e = status.engine;
  return `${e.running ? "running" : "stopped"}${e.kill_switch ? ", kill switch on" : ""}`;
}

/** "Last error (2h ago): …" for tooltips; the age comes from last_error_at when sent. */
export function lastErrorLine(e: EngineStatus, serverNow: number): string {
  const age = e.last_error_at ? ` (${fmtRelative(e.last_error_at, serverNow)})` : "";
  return `Last error${age}: ${e.last_error ?? ""}`;
}

/**
 * Engine pill for the top bar: state + SSE indicator. State comes from /api/status
 * (StatusProvider): stalled ticks, current vs historical errors, exchange pauses.
 */
export function EngineStatusPill() {
  const { status, error, updatedAt } = useStatus();
  const now = useNow();
  const serverNow = useServerNow();
  const st = engineState(status, error, serverNow);
  const p = PILL[st];
  const e = status?.engine;
  // updatedAt is a browser timestamp, so it is compared with the browser clock.
  const lastSeen = updatedAt ? fmtRelative(new Date(updatedAt).toISOString(), now) : null;
  const detail = error
    ? [
        `${statusErrorSummary(error)}: ${errorMessage(error)}`,
        status ? `Last known state (${lastSeen}): Kalshi engine ${lastKnownEngine(status)}` : null,
      ]
        .filter(Boolean)
        .join("\n")
    : !status
      ? "Waiting for /api/status"
      : [
          `Kalshi engine ${e?.running ? "running" : "stopped"}${e?.kill_switch ? " · kill switch engaged (no new entries)" : ""}`,
          e?.last_tick_at ? `Last tick ${fmtRelative(e.last_tick_at, serverNow)} (#${e.tick_count})` : "No ticks yet",
          e?.last_error ? lastErrorLine(e, serverNow) : null,
          status.exchange.trading_active === false ? "Kalshi exchange: trading paused" : null,
        ]
          .filter(Boolean)
          .join("\n");

  // Announce real state transitions only. The pill itself is NOT a live region: its
  // relative tick time changes every few seconds and would be re-read constantly.
  const [announcement, setAnnouncement] = useState("");
  const prev = useRef<EngineState>(st);
  useEffect(() => {
    if (prev.current !== st && st !== "loading" && prev.current !== "loading") {
      setAnnouncement(`Kalshi engine: ${p.label}`);
    }
    prev.current = st;
  }, [st, p.label]);

  const sub =
    st === "running" && e?.last_tick_at
      ? `tick ${fmtRelative(e.last_tick_at, serverNow)}`
      : (st === "unreachable" || st === "backend-error") && lastSeen
        ? `last seen ${lastSeen}`
        : null;

  return (
    <>
      <span className="engine-status">
        <span className={`pill pill-${p.cls}`} title={detail}>
          <Icon name={p.icon} className={st === "running" ? "pulse" : undefined} />
          <span className="pill-label">{p.label}</span>
          {sub && (
            <span className="pill-sub" aria-hidden="true">
              {sub}
            </span>
          )}
        </span>
        <StreamIndicator />
      </span>
      <span className="sr-only" role="status" aria-live="polite">
        {announcement}
      </span>
    </>
  );
}

export function StreamIndicator() {
  const info = useStreamInfo();
  const now = useNow();
  const label = info.state === "open" ? "Live" : info.state === "reconnecting" ? "Reconnecting" : info.state === "connecting" ? "Connecting" : "Off";
  const retry = info.nextRetryAt && info.nextRetryAt > now ? ` · next attempt ${fmtRelative(new Date(info.nextRetryAt).toISOString(), now)}` : "";
  const title =
    info.state === "open"
      ? "Kalshi live event stream connected (/api/stream)"
      : `Kalshi event stream ${info.state}${info.attempts ? ` · ${info.attempts} failed attempt(s)` : ""}${retry}; data still refreshes by polling`;
  return (
    <span className={`stream stream-${info.state}`} title={title}>
      <span className="stream-dot" aria-hidden="true" />
      <span className="stream-label">{label}</span>
    </span>
  );
}

/** Shows why the kill switch is on, fetched when the release dialog opens. */
function KillSwitchReason({ known }: { known: string | null }) {
  const [reason, setReason] = useState<string | null | undefined>(known ?? undefined);
  useEffect(() => {
    if (known) return;
    const ctrl = new AbortController();
    api
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
      {/daily loss/i.test(reason) && " — this was tripped automatically by the daily loss limit. Check today's P&L before releasing."}
    </p>
  );
}

export function EngineControls({ compact }: { compact?: boolean }) {
  const { status, error, apply, refresh, engineBusy, beginEngineAction, endEngineAction } = useStatus();
  const confirm = useConfirm();
  const toast = useToast();
  const { busy: localBusy, run: runLocal } = useAction();
  // Busy state is shared through StatusProvider: the header and the Dashboard's Engine
  // card both render EngineControls, and neither may send while the other is pending.
  const busy: EngineBusy = engineBusy ?? (localBusy as EngineBusy);
  const run = async <R,>(key: Exclude<EngineBusy, null>, fn: () => Promise<R>, messages: { success?: string; error: string }) => {
    if (!beginEngineAction(key)) return undefined;
    try {
      return await runLocal(key, fn, messages);
    } finally {
      endEngineAction();
    }
  };
  const running = status?.engine.running ?? false;
  const killed = status?.engine.kill_switch ?? false;
  // While /api/status is failing the on-screen state is stale: disable the controls
  // whose meaning depends on it. Engaging the kill switch is always safe, so it stays
  // available when the server is answering (an HTTP error rather than unreachable).
  const staleNote = error ? ` — disabled: ${statusErrorSummary(error)}` : "";
  // live mode without a working key: the server refuses to start (stopping is always allowed)
  const liveLocked = !running && status?.live != null && !status.live.ready;
  const startStopDisabled = !status || !!error || busy !== null || liveLocked;
  const killDisabled = !status || busy !== null || (!!error && (isUnreachable(error) || killed));

  const startStop = async () => {
    if (running) {
      const ok = await confirm({
        title: "Stop the Kalshi engine?",
        body: (
          <>
            <p>The Kalshi paper trading loop stops: no new signals or orders until you start it again. Open Kalshi paper positions and resting orders are kept.</p>
          </>
        ),
        confirmLabel: "Stop Kalshi engine",
      });
      if (!ok) return;
    }
    const r = await run(
      "engine",
      () => (running ? api.stopEngine() : api.startEngine()),
      {
        success: running ? "Kalshi engine stopped" : "Kalshi engine started",
        error: running ? "Couldn't stop the Kalshi engine" : "Couldn't start the Kalshi engine",
      },
    );
    if (r) apply(r);
    else refresh();
  };

  const toggleKill = async () => {
    const on = !killed;
    const ok = await confirm(
      on
        ? {
            title: "Engage the Kalshi kill switch?",
            body: (
              <>
                <p>All Kalshi strategies stop opening new positions immediately, and every resting (GTC) Kalshi order is cancelled. Existing Kalshi paper positions stay open and still settle.</p>
                <p>The kill switch stays on until you release it (it also trips automatically when the Kalshi daily loss limit is hit).</p>
              </>
            ),
            confirmLabel: "Engage Kalshi kill switch",
            danger: true,
          }
        : {
            title: "Release the Kalshi kill switch?",
            body: (
              <>
                <KillSwitchReason known={status?.engine.kill_switch_reason ?? null} />
                <p>Kalshi strategies will be allowed to open new paper positions again on the next tick (if the Kalshi engine is running).</p>
              </>
            ),
            confirmLabel: "Release",
          },
    );
    if (!ok) return;
    const r = await run("kill", () => api.setKillSwitch(on), {
      error: on ? "Couldn't engage the Kalshi kill switch" : "Couldn't release the Kalshi kill switch",
    });
    if (r) {
      apply(r);
      if (on) {
        toast.info("Kalshi kill switch engaged — new entries blocked, resting orders cancelled");
        // The backend cancelled every resting order: refetch order/position views now
        // rather than waiting for their next poll (SSE may be down).
        streamStore.invalidate(["order", "fill"]);
      } else toast.success("Kalshi kill switch released");
    } else refresh();
  };

  return (
    <div className={`engine-controls${compact ? " compact" : ""}`}>
      <button
        className={running ? "btn" : "btn btn-primary"}
        onClick={startStop}
        disabled={startStopDisabled}
        aria-busy={busy === "engine"}
        aria-label={running ? "Stop Kalshi engine" : "Start Kalshi engine"}
        title={
          liveLocked
            ? `Live trading is locked: ${status?.live?.blocked_reason ?? "not ready"}`
            : (running ? "Stop the Kalshi trading loop (positions are kept)" : "Start the Kalshi trading loop") + staleNote
        }
      >
        <Icon name={running ? "stop" : "play"} />
        <span className="btn-label">{running ? "Stop" : "Start"}</span>
      </button>
      {/* The visible name changes with the state, so no aria-pressed (it would announce backwards). */}
      <button
        className={killed ? "btn btn-danger-solid" : "btn btn-danger"}
        onClick={toggleKill}
        disabled={killDisabled}
        aria-busy={busy === "kill"}
        aria-label={killed ? "Release kill switch (Kalshi)" : "Kill switch (Kalshi)"}
        title={(killed ? "Kalshi kill switch is ON — click to release" : "Block all new Kalshi entries and cancel resting Kalshi orders") + (killDisabled ? staleNote : "")}
      >
        <Icon name="shield" />
        <span className="btn-label">{killed ? "Release kill switch" : "Kill switch"}</span>
      </button>
    </div>
  );
}
