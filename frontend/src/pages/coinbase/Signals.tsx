import { useMemo, useState } from "react";
import { cbApi } from "../../api/coinbase/client";
import type { CbDecision, CbSignal } from "../../api/coinbase/types";
import { DataTable } from "../../components/DataTable";
import { Card, EmptyState, Freshness, PageHeader, PollView, Segmented } from "../../components/ui";
import { ClampText, StrategyTag, Time } from "../../components/values";
import { fmtInt, parseTs } from "../../lib/format";
import { useDebounced, useStoredState } from "../../lib/hooks";
import { Bps, CbDecisionBadge, CbPage, CbSideTag, CbStrategyFilter, Price, ProductCell, sizeText, uniqSorted, useCbPolling, Weight } from "./shared";

type DecisionFilter = "all" | CbDecision;

function SignalsBody() {
  const [limit, setLimit] = useStoredState<number>("kalshibot.cb.signalsLimit", 500);
  const poll = useCbPolling((signal) => cbApi.signals(limit, { signal }), { intervalMs: 5000, deps: [limit], label: "Coinbase signals", refreshOn: ["signal"] });
  const [decision, setDecision] = useStoredState<DecisionFilter>("kalshibot.cb.signalsDecision", "all");
  const [strategy, setStrategy] = useState("");
  const [search, setSearch] = useState("");
  const q = useDebounced(search, 200).toLowerCase();

  const all = poll.data ?? [];
  const strategies = useMemo(() => uniqSorted(all.map((s) => s.strategy)), [all]);
  const base = all.filter(
    (s) =>
      (!strategy || s.strategy === strategy) &&
      (!q || s.product_id.toLowerCase().includes(q) || s.decision_reason.toLowerCase().includes(q) || s.reason.toLowerCase().includes(q)),
  );
  const counts = base.reduce<Record<string, number>>((a, s) => ({ ...a, [s.decision]: (a[s.decision] ?? 0) + 1 }), {});
  const rows = decision === "all" ? base : base.filter((s) => s.decision === decision);

  const topReasons = useMemo(() => {
    const m = new Map<string, number>();
    for (const s of base) {
      if (s.decision !== "rejected") continue;
      const key = (s.decision_reason.split(/[:(]/)[0] ?? s.decision_reason).trim() || "unspecified";
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
    <>
      <PageHeader
        title="Coinbase signals"
        subtitle="Every order intent a Coinbase strategy's rebalance produced — target weight, size, and what happened to it (including risk rejections and resting maker orders)."
        actions={<Freshness poll={poll} />}
      />
      <div className="toolbar">
        <Segmented<DecisionFilter>
          label="Decision"
          value={decision}
          onChange={setDecision}
          options={[opt("all", "All"), opt("executed", "Executed"), opt("partial", "Partial"), opt("resting", "Resting"), opt("rejected", "Rejected"), opt("unfilled", "Unfilled")]}
        />
        <input
          className="input input-sm search"
          type="search"
          placeholder="Product or reason…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="Search Coinbase signals"
        />
        <CbStrategyFilter value={strategy} onChange={setStrategy} options={strategies} />
        <label className="inline-field">
          <span>Last</span>
          <select className="input input-sm" value={limit} onChange={(e) => setLimit(Number(e.target.value))} aria-label="Number of Coinbase signals to load">
            {[200, 500, 1000].map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
          </select>
        </label>
      </div>
      {topReasons.length > 0 && (decision === "all" || decision === "rejected") && (
        <div className="reason-chips" aria-label="Most common Coinbase rejection reasons">
          <span className="muted">Top rejection reasons:</span>
          {topReasons.map(([r, n]) => (
            <button key={r} type="button" className="chip" onClick={() => setSearch(r)} title="Filter by this reason">
              <span className="mono">{r}</span> <span className="chip-count">{n}</span>
            </button>
          ))}
        </div>
      )}
      <Card flush>
        <PollView<CbSignal[]>
          poll={poll}
          isEmpty={(d) => d.length === 0}
          empty={<EmptyState title="No Coinbase signals yet" hint="Signals appear when an enabled strategy's bar closes and its target weights differ from the holdings by more than the rebalance band." />}
        >
          {() => (
            <DataTable
              caption="Coinbase signals"
              rows={rows}
              rowKey={(s, i) => (s.id !== null ? `cbsig-${String(s.id)}` : `${s.ts}-${s.product_id}-${s.strategy}-${i}`)}
              defaultSort={{ key: "ts", dir: "desc" }}
              maxHeight={680}
              rowClassName={(s) => (s.decision === "rejected" ? "row-rejected" : undefined)}
              empty={<EmptyState title="No signals match the filters" />}
              columns={[
                { key: "ts", header: "Time", sortValue: (s) => parseTs(s.ts), render: (s) => <Time value={s.ts} stack seconds /> },
                { key: "st", header: "Strategy", sortValue: (s) => s.strategy, render: (s) => <StrategyTag name={s.strategy} /> },
                { key: "p", header: "Product", sortValue: (s) => s.product_id, render: (s) => <ProductCell pid={s.product_id} /> },
                { key: "side", header: "Side", sortValue: (s) => s.side, render: (s) => <CbSideTag side={s.side} /> },
                {
                  key: "tw",
                  header: "Target weight",
                  align: "right",
                  title: "Target share of the strategy's allocation (0 % = sell out)",
                  sortValue: (s) => s.target_weight,
                  render: (s) => <Weight value={s.target_weight} />,
                },
                {
                  key: "size",
                  header: "Size",
                  align: "right",
                  title: "Buys: USD to spend including the fee; sells (and limit buys): coin quantity",
                  sortValue: (s) => s.quote_size ?? s.base_size,
                  render: (s) => <span className="num nowrap">{sizeText(s)}</span>,
                },
                { key: "dec", header: "Decision", sortValue: (s) => s.decision, render: (s) => <CbDecisionBadge decision={s.decision} raw={s.decision_raw} /> },
                {
                  key: "dr",
                  header: "Decision reason",
                  minWidth: 240,
                  render: (s) => <ClampText text={s.decision_reason} className={s.decision === "rejected" ? "reject-reason" : undefined} />,
                },
                { key: "why", header: "Strategy reason", minWidth: 220, render: (s) => <ClampText text={s.reason} className="muted" /> },
                { key: "px", header: "Limit", align: "right", sortValue: (s) => s.limit_price, render: (s) => <Price value={s.limit_price} /> },
                {
                  key: "edge",
                  header: "Exp. edge",
                  align: "right",
                  title: "Expected edge after fees, basis points of notional (when the strategy reports one)",
                  sortValue: (s) => s.expected_edge_bps,
                  render: (s) => <Bps value={s.expected_edge_bps} sign tone />,
                },
                { key: "oid", header: "Order", render: (s) => (s.order_id !== null ? <span className="mono muted">#{String(s.order_id)}</span> : <span className="muted">—</span>) },
              ]}
            />
          )}
        </PollView>
      </Card>
    </>
  );
}

export function CoinbaseSignals() {
  return (
    <CbPage>
      <SignalsBody />
    </CbPage>
  );
}
