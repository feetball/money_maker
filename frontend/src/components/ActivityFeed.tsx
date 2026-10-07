import { useMemo, useState } from "react";
import { api } from "../api/client";
import type { LogEntry, StreamEvent } from "../api/types";
import { usePolling } from "../lib/hooks";
import { errorMessage } from "../api/client";
import { fmtCents, fmtInt, fmtPnl, fmtUsd, parseTs, pnlTone, sideLabel } from "../lib/format";
import { useStreamEvents, useStreamInfo } from "../lib/stream";
import { ErrorBlock, Segmented, Switch } from "./ui";
import { decisionLabel, Time } from "./values";

type FeedFilter = "all" | "trades" | "logs" | "problems";

/** A live (SSE) log and a polled log row are the same line when this close in time. */
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

function fromEvent(e: StreamEvent): FeedItem | null {
  const base = { key: `sse-${e.seq}`, live: true };
  const fallbackTs = new Date(e.receivedAt).toISOString();
  switch (e.type) {
    case "signal": {
      const s = e.data;
      const tone = s.decision === "executed" ? "good" : s.decision === "rejected" ? "bad" : s.decision === "unknown" ? "neutral" : "warn";
      return {
        ...base,
        ts: s.ts || fallbackTs,
        kind: `signal · ${decisionLabel(s.decision, s.decision_raw).toLowerCase()}`,
        tone,
        group: "trade",
        text: `${s.strategy}: ${s.action ? `${s.action.toUpperCase()} ` : ""}${sideLabel(s.side)} ${fmtInt(s.count)}× ${s.ticker || "?"} @ ${fmtCents(s.limit_price)}${s.decision_reason ? ` — ${s.decision_reason}` : ""}`,
      };
    }
    case "order": {
      const o = e.data;
      return {
        ...base,
        ts: o.updated_at ?? o.created_at ?? fallbackTs,
        kind: `order · ${(o.status === "unknown" ? o.status_raw || "unknown" : o.status).replace("_", " ")}`,
        tone: o.status === "rejected" ? "bad" : o.status === "filled" ? "good" : o.status === "unknown" ? "neutral" : "info",
        group: "trade",
        text: `#${o.id} ${o.action} ${o.filled_count}/${o.count} ${sideLabel(o.side)} ${o.ticker} @ ${fmtCents(o.limit_price)} (${o.tif.toUpperCase()}, ${o.strategy})`,
      };
    }
    case "fill": {
      const f = e.data;
      return {
        ...base,
        ts: f.ts || fallbackTs,
        kind: "fill",
        tone: "good",
        group: "trade",
        text: `${f.strategy}: ${f.action} ${f.count} ${sideLabel(f.side)} ${f.ticker} @ ${fmtCents(f.price)} · fee ${fmtUsd(f.fee)} · ${f.is_taker ? "taker" : "maker"}`,
      };
    }
    case "settlement": {
      const s = e.data;
      const t = pnlTone(s.pnl);
      const closed = s.kind === "close";
      return {
        ...base,
        ts: s.ts || fallbackTs,
        kind: closed ? "closed early" : "settlement",
        tone: t === "pos" ? "good" : t === "neg" ? "bad" : "neutral",
        group: "trade",
        text: closed
          ? `${s.ticker} closed before resolution: ${s.count} ${sideLabel(s.side)} → ${fmtPnl(s.pnl)} (${s.strategy})`
          : `${s.ticker} settled ${s.result.toUpperCase() || "?"}: ${s.count} ${sideLabel(s.side)} → ${fmtPnl(s.pnl)} (${s.strategy})`,
      };
    }
    case "log":
      return { ...fromLog(e.data, `sse-${e.seq}`), live: true };
    default:
      return null;
  }
}

function fromLog(l: LogEntry, key: string): FeedItem {
  const lvl = String(l.level).toLowerCase();
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

export function ActivityFeed({ maxItems = 150, height = 420 }: { maxItems?: number; height?: number }) {
  const events = useStreamEvents();
  const info = useStreamInfo();
  const logs = usePolling((signal) => api.logs(100, { signal }), { intervalMs: 10_000, label: "activity log" });
  const [filter, setFilter] = useState<FeedFilter>("all");
  const [paused, setPaused] = useState(false);
  const [frozen, setFrozen] = useState<FeedItem[] | null>(null);

  const items = useMemo(() => {
    const seen = new Set<string>();
    const out: FeedItem[] = [];
    const push = (it: FeedItem | null, dedupeKey?: string) => {
      if (!it) return;
      if (dedupeKey) {
        if (seen.has(dedupeKey)) return;
        seen.add(dedupeKey);
      }
      out.push(it);
    };
    // The backend sends every engine log twice: over SSE (no id, ts = logging record
    // time) and in GET /api/logs (store id, ts = engine clock, microseconds apart). Ids
    // therefore cannot match the two copies; a live copy is dropped when a polled row
    // with the same level/kind/message lies within ±2 s. Ids only give React keys.
    const rest = logs.data ?? [];
    const sig = (l: LogEntry) => `${String(l.level).toLowerCase()}|${l.kind}|${l.message}`;
    const restTimes = new Map<string, (number | null)[]>();
    for (const l of rest) {
      const k = sig(l);
      const arr = restTimes.get(k);
      if (arr) arr.push(parseTs(l.ts));
      else restTimes.set(k, [parseTs(l.ts)]);
    }
    const polledCopyExists = (l: LogEntry) => {
      const times = restTimes.get(sig(l));
      if (!times) return false;
      const t = parseTs(l.ts);
      return times.some((x) => t === null || x === null || Math.abs(x - t) <= LOG_DEDUPE_MS);
    };
    for (const e of events) {
      if (e.type === "log") {
        if (!polledCopyExists(e.data)) push(fromEvent(e));
      } else push(fromEvent(e));
    }
    const restKey = (l: LogEntry) => (l.id !== null ? `log-${l.id}` : `log-${l.ts}|${sig(l)}`);
    for (const l of rest) push(fromLog(l, restKey(l)), restKey(l));
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
          label="Activity filter"
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
            label="Pause feed"
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
            <div className="state-title">{logs.loading ? "Loading activity…" : "No activity yet"}</div>
            <div className="state-hint">Kalshi engine events stream here as they happen (signals, orders, fills, settlements, logs).</div>
          </div>
        </div>
      ) : (
        <ol className="feed-list" style={{ maxHeight: height }} aria-live="off">
          {filtered.map((it) => (
            <li key={it.key} className={`feed-item tone-${it.tone}${it.live ? " live" : ""}`}>
              <span className="feed-dot" aria-hidden="true" />
              <div className="feed-main">
                <div className="feed-line">
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
