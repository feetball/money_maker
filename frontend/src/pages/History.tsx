import { useMemo, useState } from "react";
import { api } from "../api/client";
import type { Fill, Settlement } from "../api/types";
import { DataTable } from "../components/DataTable";
import { Card, EmptyState, Freshness, PageHeader, PollView, Segmented } from "../components/ui";
import { Cents, MarketCell, Pnl, ResultTag, SideTag, StrategyTag, Time, Usd } from "../components/values";
import { useDebounced, usePolling, useStoredState } from "../lib/hooks";
import { fmtFrac, fmtInt, fmtPnl, fmtUsd, parseTs } from "../lib/format";
import { StrategyFilter } from "./PositionsOrders";

type Tab = "fills" | "settlements";
const LIMITS = [200, 500, 1000] as const;

function matches(q: string, ...fields: string[]) {
  if (!q) return true;
  const s = q.toLowerCase();
  return fields.some((f) => f.toLowerCase().includes(s));
}

export function History() {
  const [tab, setTab] = useStoredState<Tab>("kalshibot.historyTab", "fills");
  const [limit, setLimit] = useStoredState<number>("kalshibot.historyLimit", 200);
  const [strategy, setStrategy] = useState("");
  const [search, setSearch] = useState("");
  const q = useDebounced(search, 200);

  const fills = usePolling((signal) => api.fills(limit, { signal }), {
    intervalMs: 10_000,
    deps: [limit],
    enabled: tab === "fills",
    label: "fills",
    refreshOn: ["fill"],
  });
  const settlements = usePolling((signal) => api.settlements(limit, { signal }), {
    intervalMs: 10_000,
    deps: [limit],
    enabled: tab === "settlements",
    label: "settlements",
    refreshOn: ["settlement"],
  });

  const strategies = useMemo(
    () => [...new Set([...(fills.data ?? []).map((f) => f.strategy), ...(settlements.data ?? []).map((s) => s.strategy)].filter(Boolean))].sort(),
    [fills.data, settlements.data],
  );

  const fillRows = (fills.data ?? []).filter((f) => (!strategy || f.strategy === strategy) && matches(q, f.ticker, f.title));
  const setRows = (settlements.data ?? []).filter((s) => (!strategy || s.strategy === strategy) && matches(q, s.ticker, s.title));

  const fillSum = fillRows.reduce((a, f) => ({ n: a.n + f.count, fee: a.fee + f.fee, notional: a.notional + f.count * f.price }), { n: 0, fee: 0, notional: 0 });
  // Early closes (kind "close") are not resolutions: they are counted apart from the
  // won/settled figures but still included in the P&L and fee totals.
  const setSum = setRows.reduce(
    (a, s) =>
      s.kind === "close"
        ? { ...a, closed: a.closed + 1, pnl: a.pnl + s.pnl, fees: a.fees + (s.fees ?? 0), payout: a.payout + s.payout }
        : { ...a, settled: a.settled + 1, wins: a.wins + (s.pnl > 0 ? 1 : 0), pnl: a.pnl + s.pnl, fees: a.fees + (s.fees ?? 0), payout: a.payout + s.payout },
    { settled: 0, closed: 0, wins: 0, pnl: 0, fees: 0, payout: 0 },
  );

  const poll = tab === "fills" ? fills : settlements;

  return (
    <div className="page">
      <PageHeader title="History" subtitle="Every simulated fill and every settlement of a held market" actions={<Freshness poll={poll} />} />
      <div className="toolbar">
        <Segmented<Tab>
          label="History view"
          size="md"
          value={tab}
          onChange={setTab}
          options={[
            { value: "fills", label: "Fills" },
            { value: "settlements", label: "Settlements" },
          ]}
        />
        <input className="input input-sm search" type="search" placeholder="Filter ticker or title…" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Filter by ticker or title" />
        <StrategyFilter value={strategy} onChange={setStrategy} options={strategies} />
        <label className="inline-field">
          <span>Last</span>
          <select className="input input-sm" value={limit} onChange={(e) => setLimit(Number(e.target.value))} aria-label="Number of rows to load">
            {LIMITS.map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
          </select>
        </label>
      </div>

      {tab === "fills" ? (
        <Card
          title="Fills"
          subtitle={
            fills.data
              ? `${fmtInt(fillRows.length)} fills · ${fmtInt(fillSum.n)} contracts · notional ${fmtUsd(fillSum.notional)} · fees ${fmtUsd(fillSum.fee)}`
              : undefined
          }
          flush
        >
          <PollView<Fill[]> poll={fills} isEmpty={(d) => d.length === 0} empty={<EmptyState title="No fills yet" />}>
            {() => (
              <DataTable
                caption="Fills"
                rows={fillRows}
                rowKey={(f) => String(f.id)}
                defaultSort={{ key: "ts", dir: "desc" }}
                empty={<EmptyState title="No fills match the filters" />}
                columns={[
                  { key: "ts", header: "Time", sortValue: (f) => parseTs(f.ts), render: (f) => <Time value={f.ts} stack seconds /> },
                  { key: "m", header: "Market", minWidth: 220, sortValue: (f) => f.ticker, render: (f) => <MarketCell ticker={f.ticker} title={f.title} /> },
                  { key: "s", header: "Strategy", sortValue: (f) => f.strategy, render: (f) => <StrategyTag name={f.strategy} /> },
                  {
                    key: "side",
                    header: "Trade",
                    render: (f) => (
                      <span className="nowrap">
                        <span className="muted">{f.action.toUpperCase()}</span> <SideTag side={f.side} />
                      </span>
                    ),
                  },
                  { key: "n", header: "Qty", align: "right", sortValue: (f) => f.count, render: (f) => <span className="num">{fmtInt(f.count)}</span> },
                  { key: "px", header: "Price", align: "right", sortValue: (f) => f.price, render: (f) => <Cents value={f.price} /> },
                  { key: "notional", header: "Notional", align: "right", sortValue: (f) => f.count * f.price, render: (f) => <Usd value={f.count * f.price} /> },
                  { key: "fee", header: "Fee", align: "right", sortValue: (f) => f.fee, render: (f) => <Usd value={f.fee} /> },
                  { key: "liq", header: "Liquidity", sortValue: (f) => (f.is_taker ? 1 : 0), render: (f) => <span className="muted">{f.is_taker ? "taker" : "maker"}</span> },
                  { key: "oid", header: "Order", render: (f) => <span className="mono muted">#{String(f.order_id)}</span> },
                ]}
              />
            )}
          </PollView>
        </Card>
      ) : (
        <Card
          title="Settlements"
          subtitle={
            settlements.data ? (
              <>
                {fmtInt(setSum.settled)} settled · won {fmtInt(setSum.wins)} ({fmtFrac(setSum.settled ? setSum.wins / setSum.settled : null)})
                {setSum.closed > 0 && <> · {fmtInt(setSum.closed)} closed early</>} · payout {fmtUsd(setSum.payout)} · fees {fmtUsd(setSum.fees)} · P&L{" "}
                <span className="num">{fmtPnl(setSum.pnl)}</span>
              </>
            ) : undefined
          }
          flush
        >
          <PollView<Settlement[]> poll={settlements} isEmpty={(d) => d.length === 0} empty={<EmptyState title="Nothing has settled yet" hint="Settlements are recorded when a held market resolves." />}>
            {() => (
              <DataTable
                caption="Settlements"
                rows={setRows}
                rowKey={(s) => String(s.id)}
                defaultSort={{ key: "ts", dir: "desc" }}
                empty={<EmptyState title="No settlements match the filters" />}
                columns={[
                  { key: "ts", header: "Settled", sortValue: (s) => parseTs(s.ts), render: (s) => <Time value={s.ts} stack /> },
                  { key: "m", header: "Market", minWidth: 220, sortValue: (s) => s.ticker, render: (s) => <MarketCell ticker={s.ticker} title={s.title} /> },
                  { key: "st", header: "Strategy", sortValue: (s) => s.strategy, render: (s) => <StrategyTag name={s.strategy} /> },
                  { key: "side", header: "Held", render: (s) => <SideTag side={s.side} /> },
                  { key: "n", header: "Qty", align: "right", sortValue: (s) => s.count, render: (s) => <span className="num">{fmtInt(s.count)}</span> },
                  { key: "res", header: "Result", sortValue: (s) => s.result, render: (s) => <ResultTag result={s.result} side={s.side} kind={s.kind} /> },
                  { key: "cost", header: "Cost", align: "right", title: "Principal paid, excl. fees", sortValue: (s) => s.cost_basis, render: (s) => <Usd value={s.cost_basis} /> },
                  {
                    key: "fees",
                    header: "Fees",
                    align: "right",
                    title: "Fees on these contracts. P&L = Payout − Cost − Fees",
                    sortValue: (s) => s.fees,
                    render: (s) => (s.fees === null ? <span className="muted" title="Not reported by the backend">n/r</span> : <Usd value={s.fees} />),
                  },
                  {
                    key: "pay",
                    header: "Payout",
                    align: "right",
                    title: "Settlement payout, or exit proceeds for a position closed early",
                    sortValue: (s) => s.payout,
                    render: (s) => <Usd value={s.payout} />,
                  },
                  { key: "pnl", header: "P&L", align: "right", sortValue: (s) => s.pnl, render: (s) => <Pnl value={s.pnl} /> },
                  {
                    key: "pc",
                    header: "P&L / contract",
                    align: "right",
                    sortValue: (s) => (s.count ? s.pnl / s.count : null),
                    render: (s) => <Cents value={s.count ? s.pnl / s.count : null} sign tone dp={1} />,
                  },
                ]}
              />
            )}
          </PollView>
        </Card>
      )}
    </div>
  );
}
