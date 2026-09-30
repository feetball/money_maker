import { useMemo, useState } from "react";
import { api } from "../api/client";
import type { Signal, SignalDecision } from "../api/types";
import { DataTable } from "../components/DataTable";
import { Card, EmptyState, Freshness, PageHeader, PollView, Segmented } from "../components/ui";
import { Cents, ClampText, DecisionBadge, MarketCell, SideTag, StrategyTag, Time } from "../components/values";
import { useDebounced, usePolling, useStoredState } from "../lib/hooks";
import { fmtInt, parseTs } from "../lib/format";
import { StrategyFilter } from "./PositionsOrders";

type DecisionFilter = "all" | SignalDecision;

export function Signals() {
  const [limit, setLimit] = useStoredState<number>("kalshibot.signalsLimit", 500);
  const poll = usePolling((signal) => api.signals(limit, { signal }), {
    intervalMs: 5000,
    deps: [limit],
    label: "signals",
    refreshOn: ["signal"],
  });
  const [decision, setDecision] = useStoredState<DecisionFilter>("kalshibot.signalsDecision", "all");
  const [strategy, setStrategy] = useState("");
  const [search, setSearch] = useState("");
  const q = useDebounced(search, 200).toLowerCase();

  const all = poll.data ?? [];
  const strategies = useMemo(() => [...new Set(all.map((s) => s.strategy).filter(Boolean))].sort(), [all]);
  const base = all.filter((s) => (!strategy || s.strategy === strategy) && (!q || s.ticker.toLowerCase().includes(q) || s.title.toLowerCase().includes(q) || s.decision_reason.toLowerCase().includes(q)));
  const counts = base.reduce<Record<string, number>>((a, s) => ({ ...a, [s.decision]: (a[s.decision] ?? 0) + 1 }), {});
  const rows = decision === "all" ? base : base.filter((s) => s.decision === decision);

  const topReasons = useMemo(() => {
    const m = new Map<string, number>();
    for (const s of base) {
      if (s.decision !== "rejected") continue;
      const key = (s.decision_reason.split(":")[0] ?? s.decision_reason).trim() || "unspecified";
      m.set(key, (m.get(key) ?? 0) + 1);
    }
    return [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6);
  }, [base]);

  const opt = (value: DecisionFilter, label: string) => ({
    value,
    label: (
      <>
        {label} <span className="seg-count">{fmtInt(value === "all" ? base.length : (counts[value] ?? 0))}</span>
      </>
    ),
  });

  return (
    <div className="page">
      <PageHeader
        title="Signals"
        subtitle="Every order intent a strategy produced — including the ones the risk manager or the book rejected, with the reason."
        actions={<Freshness poll={poll} />}
      />
      <div className="toolbar">
        <Segmented<DecisionFilter>
          label="Decision"
          value={decision}
          onChange={setDecision}
          options={[opt("all", "All"), opt("executed", "Executed"), opt("partial", "Partial"), opt("rejected", "Rejected"), opt("unfilled", "Unfilled")]}
        />
        <input className="input input-sm search" type="search" placeholder="Ticker, title or reason…" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Search signals" />
        <StrategyFilter value={strategy} onChange={setStrategy} options={strategies} />
        <label className="inline-field">
          <span>Last</span>
          <select className="input input-sm" value={limit} onChange={(e) => setLimit(Number(e.target.value))} aria-label="Number of signals to load">
            {[200, 500, 1000].map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
          </select>
        </label>
      </div>
      {topReasons.length > 0 && (decision === "all" || decision === "rejected") && (
        <div className="reason-chips" aria-label="Most common rejection reasons">
          <span className="muted">Top rejection reasons:</span>
          {topReasons.map(([r, n]) => (
            <button key={r} type="button" className="chip" onClick={() => setSearch(r)} title="Filter by this reason">
              <span className="mono">{r}</span> <span className="chip-count">{n}</span>
            </button>
          ))}
        </div>
      )}
      <Card flush>
        <PollView<Signal[]> poll={poll} isEmpty={(d) => d.length === 0} empty={<EmptyState title="No signals yet" hint="Signals appear once the engine is running and a strategy finds an opportunity." />}>
          {() => (
            <DataTable
              caption="Signals"
              rows={rows}
              // Store id when present, so a new signal at the top does not remount every row.
              rowKey={(s, i) => (s.id !== null ? `sig-${String(s.id)}` : `${s.ts}-${s.ticker}-${s.strategy}-${s.side}-${String(s.limit_price)}-${i}`)}
              defaultSort={{ key: "ts", dir: "desc" }}
              maxHeight={680}
              rowClassName={(s) => (s.decision === "rejected" ? "row-rejected" : undefined)}
              empty={<EmptyState title="No signals match the filters" />}
              columns={[
                { key: "ts", header: "Time", sortValue: (s) => parseTs(s.ts), render: (s) => <Time value={s.ts} stack seconds /> },
                { key: "st", header: "Strategy", sortValue: (s) => s.strategy, render: (s) => <StrategyTag name={s.strategy} /> },
                { key: "m", header: "Market", minWidth: 200, sortValue: (s) => s.ticker, render: (s) => <MarketCell ticker={s.ticker} title={s.title} /> },
                {
                  key: "side",
                  header: "Intent",
                  title: "Buy (open / add) or sell (close) — and the side",
                  sortValue: (s) => `${s.action ?? ""}${s.side}`,
                  render: (s) => (
                    <span className="nowrap">
                      {s.action && <span className="muted">{s.action.toUpperCase()}</span>} <SideTag side={s.side} />
                    </span>
                  ),
                },
                { key: "n", header: "Qty", align: "right", sortValue: (s) => s.count, render: (s) => <span className="num">{fmtInt(s.count)}</span> },
                { key: "px", header: "Limit", align: "right", sortValue: (s) => s.limit_price, render: (s) => <Cents value={s.limit_price} /> },
                { key: "fv", header: "Fair", align: "right", title: "Model fair value P(side wins), in ¢", sortValue: (s) => s.fair_value, render: (s) => <Cents value={s.fair_value} /> },
                {
                  key: "edge",
                  header: "Edge/ct",
                  align: "right",
                  title: "Expected edge per contract after fees at the limit price",
                  sortValue: (s) => s.expected_edge,
                  render: (s) => <Cents value={s.expected_edge} sign tone />,
                },
                { key: "dec", header: "Decision", sortValue: (s) => s.decision, render: (s) => <DecisionBadge decision={s.decision} raw={s.decision_raw} /> },
                {
                  key: "dr",
                  header: "Decision reason",
                  minWidth: 240,
                  render: (s) => <ClampText text={s.decision_reason} className={s.decision === "rejected" ? "reject-reason" : undefined} />,
                },
                {
                  key: "why",
                  header: "Strategy reason",
                  minWidth: 240,
                  render: (s) => <ClampText text={s.reason} className="muted" />,
                },
              ]}
            />
          )}
        </PollView>
      </Card>
    </div>
  );
}
