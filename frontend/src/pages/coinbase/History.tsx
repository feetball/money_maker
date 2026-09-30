import { useMemo, useState } from "react";
import { cbApi } from "../../api/coinbase/client";
import type { CbFill, CbSide } from "../../api/coinbase/types";
import { DataTable } from "../../components/DataTable";
import { Card, EmptyState, Freshness, PageHeader, PollView, Segmented } from "../../components/ui";
import { StrategyTag, Time, Usd } from "../../components/values";
import { fmtInt, fmtUsd, parseTs } from "../../lib/format";
import { useDebounced, useStoredState } from "../../lib/hooks";
import { baseOf, fmtRate } from "./format";
import { CbPage, CbSideTag, CbStrategyFilter, Fee, Price, ProductCell, Qty, uniqSorted, useCbPolling } from "./shared";

const LIMITS = [200, 500, 1000] as const;
type SideFilter = "all" | CbSide;

function HistoryBody() {
  const [limit, setLimit] = useStoredState<number>("kalshibot.cb.historyLimit", 200);
  const [strategy, setStrategy] = useState("");
  const [side, setSide] = useState<SideFilter>("all");
  const [search, setSearch] = useState("");
  const q = useDebounced(search.trim().toLowerCase(), 200);
  const fills = useCbPolling((signal) => cbApi.fills(limit, { signal }), { intervalMs: 10_000, deps: [limit], label: "Coinbase fills", refreshOn: ["fill"] });

  const strategies = useMemo(() => uniqSorted((fills.data ?? []).map((f) => f.strategy)), [fills.data]);
  const rows = (fills.data ?? []).filter(
    (f) => (!strategy || f.strategy === strategy) && (side === "all" || f.side === side) && (!q || f.product_id.toLowerCase().includes(q)),
  );
  const sum = rows.reduce(
    (a, f) => ({
      buys: a.buys + (f.side === "buy" ? f.notional : 0),
      sells: a.sells + (f.side === "sell" ? f.notional : 0),
      fees: a.fees + f.fee,
      notional: a.notional + f.notional,
      maker: a.maker + (f.is_taker ? 0 : 1),
    }),
    { buys: 0, sells: 0, fees: 0, notional: 0, maker: 0 },
  );
  const avgRate = sum.notional > 0 ? sum.fees / sum.notional : null;

  return (
    <>
      <PageHeader
        title="Coinbase history"
        subtitle="Every simulated Coinbase fill. Fees are charged in USD on each fill: buys pay notional + fee, sells receive notional − fee."
        actions={<Freshness poll={fills} />}
      />
      <div className="toolbar">
        <input
          className="input input-sm search"
          type="search"
          placeholder="Filter product (e.g. BTC)…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="Filter Coinbase fills by product"
        />
        <Segmented<SideFilter>
          label="Side"
          value={side}
          onChange={setSide}
          options={[
            { value: "all", label: "All" },
            { value: "buy", label: "Buys" },
            { value: "sell", label: "Sells" },
          ]}
        />
        <CbStrategyFilter value={strategy} onChange={setStrategy} options={strategies} />
        <label className="inline-field">
          <span>Last</span>
          <select className="input input-sm" value={limit} onChange={(e) => setLimit(Number(e.target.value))} aria-label="Number of Coinbase fills to load">
            {LIMITS.map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
          </select>
        </label>
      </div>
      <Card
        title="Coinbase fills"
        subtitle={
          fills.data
            ? `${fmtInt(rows.length)} fills (${fmtInt(sum.maker)} maker) · bought ${fmtUsd(sum.buys)} · sold ${fmtUsd(sum.sells)} · fees ${fmtUsd(sum.fees)}${
                avgRate !== null ? ` (${fmtRate(avgRate)} of notional)` : ""
              }`
            : undefined
        }
        flush
      >
        <PollView<CbFill[]> poll={fills} isEmpty={(d) => d.length === 0} empty={<EmptyState title="No Coinbase fills yet" hint="Fills appear once a Coinbase strategy trades." />}>
          {() => (
            <DataTable
              caption="Coinbase fills"
              rows={rows}
              rowKey={(f) => String(f.id)}
              defaultSort={{ key: "ts", dir: "desc" }}
              empty={<EmptyState title="No fills match the filters" />}
              columns={[
                { key: "ts", header: "Time", sortValue: (f) => parseTs(f.ts), render: (f) => <Time value={f.ts} stack seconds /> },
                { key: "p", header: "Product", sortValue: (f) => f.product_id, render: (f) => <ProductCell pid={f.product_id} /> },
                { key: "s", header: "Strategy", sortValue: (f) => f.strategy, render: (f) => <StrategyTag name={f.strategy} /> },
                { key: "side", header: "Side", sortValue: (f) => f.side, render: (f) => <CbSideTag side={f.side} /> },
                { key: "q", header: "Quantity", align: "right", sortValue: (f) => f.base_size, render: (f) => <Qty value={f.base_size} base={baseOf(f.product_id)} /> },
                { key: "px", header: "Price", align: "right", sortValue: (f) => f.price, render: (f) => <Price value={f.price} /> },
                { key: "n", header: "Notional", align: "right", title: "Quantity × price, before the fee", sortValue: (f) => f.notional, render: (f) => <Usd value={f.notional} /> },
                { key: "fee", header: "Fee", align: "right", title: "USD fee and its rate on this fill's notional", sortValue: (f) => f.fee, render: (f) => <Fee fee={f.fee} rate={f.fee_rate} notional={f.notional} /> },
                {
                  key: "cash",
                  header: "Cash impact",
                  align: "right",
                  title: "Change in USD cash: buys −(notional + fee), sells +(notional − fee)",
                  sortValue: (f) => (f.side === "buy" ? -(f.notional + f.fee) : f.notional - f.fee),
                  render: (f) => {
                    const v = f.side === "buy" ? -(f.notional + f.fee) : f.notional - f.fee;
                    return <span className="num">{fmtUsd(v, { sign: true })}</span>;
                  },
                },
                { key: "liq", header: "Liquidity", sortValue: (f) => (f.is_taker ? 1 : 0), render: (f) => <span className="muted">{f.is_taker ? "taker" : "maker"}</span> },
                { key: "oid", header: "Order", render: (f) => <span className="mono muted">#{String(f.order_id)}</span> },
              ]}
            />
          )}
        </PollView>
      </Card>
    </>
  );
}

export function CoinbaseHistory() {
  return (
    <CbPage>
      <HistoryBody />
    </CbPage>
  );
}
