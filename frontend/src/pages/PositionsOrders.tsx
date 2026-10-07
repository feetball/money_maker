import { useMemo, useState } from "react";
import { ApiError, api } from "../api/client";
import type { Order, OrderStatusFilter, Position } from "../api/types";
import { useConfirm } from "../components/ConfirmDialog";
import { DataTable } from "../components/DataTable";
import { Card, EmptyState, Freshness, PageHeader, PollView, Segmented } from "../components/ui";
import { Cents, ClampText, MarketCell, OrderStatusBadge, Pnl, SideTag, StrategyTag, Time, Usd } from "../components/values";
import { useAction, usePolling, useStoredState } from "../lib/hooks";
import { fmtCents, fmtInt, fmtPnl, fmtUsd, parseTs, sideLabel } from "../lib/format";
import { useToast } from "../lib/toast";

/**
 * Strategy filter. The selected value always stays listed (marked "no rows") even
 * after its last row disappears, so the select never shows "All strategies" while
 * the table is still filtered.
 */
export function StrategyFilter({ value, onChange, options }: { value: string; onChange: (v: string) => void; options: string[] }) {
  const missing = value !== "" && !options.includes(value);
  return (
    <label className="inline-field">
      <span className="sr-only">Strategy</span>
      <select className="input input-sm" value={value} onChange={(e) => onChange(e.target.value)} aria-label="Filter by strategy">
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

const uniq = (xs: string[]) => [...new Set(xs.filter(Boolean))].sort();

/** "BUY YES 7 left of 10 KXBTCD-… @ 41¢ (crypto_fv)" — names the order in labels and dialogs. */
function orderSummary(o: Order): string {
  const left = Math.max(0, o.count - o.filled_count);
  return `${o.action.toUpperCase()} ${sideLabel(o.side)} ${fmtInt(left)} left of ${fmtInt(o.count)} ${o.ticker} @ ${fmtCents(o.limit_price)} (${o.strategy || "no strategy"})`;
}
const isResting = (o: Order) => o.tif === "gtc" && (o.status === "open" || o.status === "partially_filled");

function PositionsSection() {
  const poll = usePolling((signal) => api.positions({ signal }), { intervalMs: 5000, label: "positions", refreshOn: ["fill", "settlement"] });
  const [strategy, setStrategy] = useState("");
  const strategies = useMemo(() => uniq((poll.data ?? []).map((p) => p.strategy)), [poll.data]);
  const rows = (poll.data ?? []).filter((p) => !strategy || p.strategy === strategy);
  // cost_basis is principal EXCLUDING fees; unrealized = liquidation − cost − fees.
  const tot = rows.reduce(
    (a, p) => ({
      cost: a.cost + p.cost_basis,
      fees: a.fees + (p.open_fees ?? 0),
      feesKnown: a.feesKnown && p.open_fees !== null,
      liq: a.liq + p.liquidation_value,
      u: a.u + p.unrealized_pnl,
      edge: a.edge + (p.expected_edge_total ?? 0),
    }),
    { cost: 0, fees: 0, feesKnown: true, liq: 0, u: 0, edge: 0 },
  );
  return (
    <Card
      title="Open positions"
      subtitle={
        poll.data ? (
          <>
            {rows.length} positions · cost {fmtUsd(tot.cost)} + entry fees {tot.feesKnown ? fmtUsd(tot.fees) : "n/r"} · liquidation value {fmtUsd(tot.liq)} ·
            unrealized <span className="num">{fmtPnl(tot.u)}</span>
          </>
        ) : undefined
      }
      actions={
        <>
          <StrategyFilter value={strategy} onChange={setStrategy} options={strategies} />
          <Freshness poll={poll} />
        </>
      }
      flush
    >
      <PollView<Position[]> poll={poll} isEmpty={(d) => d.length === 0} empty={<EmptyState title="No open positions" hint="Positions appear when a strategy's order fills." />}>
        {() => (
          <DataTable
            caption="Open positions"
            rows={rows}
            rowKey={(p) => `${p.ticker}-${p.side}-${p.strategy}`}
            defaultSort={{ key: "u", dir: "asc" }}
            empty={<EmptyState title="No positions for this strategy" />}
            columns={[
              { key: "m", header: "Market", minWidth: 220, sortValue: (p) => p.ticker, render: (p) => <MarketCell ticker={p.ticker} title={p.title} url={p.url} eventTicker={p.event_ticker} /> },
              { key: "s", header: "Strategy", sortValue: (p) => p.strategy, render: (p) => <StrategyTag name={p.strategy} /> },
              { key: "side", header: "Side", sortValue: (p) => p.side, render: (p) => <SideTag side={p.side} /> },
              { key: "n", header: "Qty", align: "right", sortValue: (p) => p.count, render: (p) => <span className="num">{fmtInt(p.count)}</span> },
              { key: "avg", header: "Avg", align: "right", title: "Average entry price for your side", sortValue: (p) => p.avg_price, render: (p) => <Cents value={p.avg_price} /> },
              {
                key: "ba",
                header: "YES bid/ask",
                align: "right",
                title: "Current YES book (NO bid = 100¢ − YES ask)",
                render: (p) => (
                  <span className="num muted">
                    {fmtCents(p.yes_bid)} / {fmtCents(p.yes_ask)}
                  </span>
                ),
              },
              { key: "bid", header: "Bid", align: "right", title: "Best bid for your side (top of book)", sortValue: (p) => p.best_bid, render: (p) => <Cents value={p.best_bid} /> },
              { key: "mark", header: "Exit", align: "right", title: "Average exit price when selling the whole position into the bid ladder (= best bid when the top level covers it)", sortValue: (p) => p.mark_price, render: (p) => p.mark_stale ? (
                <span title="Market closed, result pending: last pre-close value, frozen until the result">
                  <Cents value={p.mark_price} /> <span className="muted">(pre-close)</span>
                </span>
              ) : <Cents value={p.mark_price} /> },
              { key: "fv", header: "Fair", align: "right", title: "Model fair value (P(win)) at entry, in ¢", sortValue: (p) => p.fair_value, render: (p) => <Cents value={p.fair_value} /> },
              { key: "cost", header: "Cost", align: "right", title: "Principal paid, excl. fees (contracts × avg price)", sortValue: (p) => p.cost_basis, render: (p) => <Usd value={p.cost_basis} /> },
              {
                key: "fees",
                header: "Fees",
                align: "right",
                title: "Entry fees of the open contracts. Unrealized P&L = Liq. value − Cost − Fees",
                sortValue: (p) => p.open_fees,
                render: (p) => (p.open_fees === null ? <span className="muted" title="Not reported by the backend">n/r</span> : <Usd value={p.open_fees} />),
              },
              { key: "liq", header: "Liq. value", align: "right", title: "What selling the whole position into the bids now would return (contracts × exit price), before exit fees", sortValue: (p) => p.liquidation_value, render: (p) => <Usd value={p.liquidation_value} /> },
              { key: "u", header: "Unreal. P&L", align: "right", sortValue: (p) => p.unrealized_pnl, render: (p) => <Pnl value={p.unrealized_pnl} /> },
              { key: "edge", header: "Exp. edge", align: "right", title: "Expected $ edge of the opening intents (after fees)", sortValue: (p) => p.expected_edge_total, render: (p) => <Pnl value={p.expected_edge_total} /> },
              { key: "open", header: "Opened", sortValue: (p) => parseTs(p.opened_at), render: (p) => <Time value={p.opened_at} stack /> },
              { key: "close", header: "Closes", sortValue: (p) => parseTs(p.close_time), render: (p) => <Time value={p.close_time} stack /> },
            ]}
            footer={
              <tr className="totals">
                {/* 9 data columns before Cost. */}
                <td colSpan={9}>Total ({rows.length})</td>
                <td className="al-right">
                  <Usd value={tot.cost} />
                </td>
                <td className="al-right">{tot.feesKnown ? <Usd value={tot.fees} /> : <span className="muted">n/r</span>}</td>
                <td className="al-right">
                  <Usd value={tot.liq} />
                </td>
                <td className="al-right">
                  <Pnl value={tot.u} />
                </td>
                <td className="al-right">
                  <Pnl value={tot.edge} />
                </td>
                <td colSpan={2} />
              </tr>
            }
          />
        )}
      </PollView>
    </Card>
  );
}

function OrdersSection() {
  const [status, setStatus] = useStoredState<OrderStatusFilter>("kalshibot.ordersStatus", "open");
  const [strategy, setStrategy] = useState("");
  const poll = usePolling((signal) => api.orders({ status, limit: status === "all" ? 500 : 200 }, { signal }), {
    intervalMs: 5000,
    deps: [status],
    label: "orders",
    refreshOn: ["order", "fill"],
  });
  const { busy, run } = useAction();
  const confirm = useConfirm();
  const toast = useToast();
  const strategies = useMemo(() => uniq((poll.data ?? []).map((o) => o.strategy)), [poll.data]);
  const rows = (poll.data ?? []).filter((o) => !strategy || o.strategy === strategy);

  const cancel = async (o: Order) => {
    const ok = await confirm({
      title: `Cancel Kalshi order #${String(o.id)}?`,
      body: <p>{orderSummary(o)}. The Kalshi cash reserved for the unfilled part is released; contracts already filled stay in your Kalshi position.</p>,
      confirmLabel: "Cancel order",
      cancelLabel: "Keep order",
      danger: true,
    });
    if (!ok) return;
    // The backend answers 409 when the order is no longer open (filled / expired / cancelled
    // a moment earlier): that is information, not a failure.
    const conflict = { detail: "" };
    const r = await run(
      `cancel-${o.id}`,
      () =>
        api.cancelOrder(o.id).catch((e: unknown) => {
          if (e instanceof ApiError && e.status === 409) {
            conflict.detail = e.detail;
            return null;
          }
          throw e;
        }),
      { error: `Couldn't cancel order ${o.id}` },
    );
    if (r === null) {
      toast.info(`Order ${String(o.id)} is no longer open`, { message: conflict.detail || orderSummary(o) });
    } else if (r) {
      if (r.status === "cancelled") toast.success(`Order ${String(o.id)} cancelled`, { message: orderSummary(o) });
      else toast.info(`Order ${String(o.id)} was already ${(r.status === "unknown" ? r.status_raw || "closed" : r.status).replace("_", " ")}`, { message: orderSummary(r) });
      poll.mutate((prev) => prev?.map((x) => (String(x.id) === String(r.id) ? r : x)));
    }
    poll.refresh();
  };

  return (
    <Card
      title="Orders"
      subtitle={status === "open" ? "Resting (GTC) orders waiting for real prints through our price" : "Most recent orders, all states"}
      actions={
        <>
          <Segmented<OrderStatusFilter>
            label="Order status"
            value={status}
            onChange={setStatus}
            options={[
              { value: "open", label: "Open" },
              { value: "all", label: "All" },
            ]}
          />
          <StrategyFilter value={strategy} onChange={setStrategy} options={strategies} />
          <Freshness poll={poll} />
        </>
      }
      flush
    >
      <PollView<Order[]>
        poll={poll}
        isEmpty={(d) => d.length === 0}
        empty={<EmptyState title={status === "open" ? "No resting orders" : "No orders yet"} hint="Taker (IOC) orders fill or cancel immediately, so they rarely appear as open." />}
      >
        {() => (
          <DataTable
            caption="Orders"
            rows={rows}
            rowKey={(o) => String(o.id)}
            defaultSort={{ key: "created", dir: "desc" }}
            empty={<EmptyState title="No orders for this strategy" />}
            columns={[
              { key: "created", header: "Created", sortValue: (o) => parseTs(o.created_at), render: (o) => <Time value={o.created_at} stack seconds /> },
              { key: "m", header: "Market", minWidth: 200, sortValue: (o) => o.ticker, render: (o) => <MarketCell ticker={o.ticker} title={o.title} /> },
              { key: "s", header: "Strategy", sortValue: (o) => o.strategy, render: (o) => <StrategyTag name={o.strategy} /> },
              {
                key: "side",
                header: "Order",
                sortValue: (o) => `${o.action}${o.side}`,
                render: (o) => (
                  <span className="nowrap">
                    <span className="muted">{o.action.toUpperCase()}</span> <SideTag side={o.side} />
                  </span>
                ),
              },
              {
                key: "fill",
                header: "Filled",
                align: "right",
                sortValue: (o) => o.filled_count,
                render: (o) => (
                  <span className="num">
                    {fmtInt(o.filled_count)}/{fmtInt(o.count)}
                  </span>
                ),
              },
              { key: "lim", header: "Limit", align: "right", sortValue: (o) => o.limit_price, render: (o) => <Cents value={o.limit_price} /> },
              { key: "avg", header: "Avg fill", align: "right", sortValue: (o) => o.avg_fill_price, render: (o) => <Cents value={o.avg_fill_price} /> },
              { key: "tif", header: "TIF", render: (o) => <span className="mono">{o.tif.toUpperCase()}</span> },
              { key: "st", header: "Status", sortValue: (o) => o.status, render: (o) => <OrderStatusBadge status={o.status} raw={o.status_raw} /> },
              { key: "q", header: "Queue", align: "right", title: "Contracts ahead of us at our price when placed (maker orders)", sortValue: (o) => o.queue_ahead, render: (o) => <span className="num">{fmtInt(o.queue_ahead)}</span> },
              { key: "fees", header: "Fees", align: "right", sortValue: (o) => o.fees, render: (o) => <Usd value={o.fees} /> },
              { key: "edge", header: "Exp. edge", align: "right", title: "Expected edge per contract after fees", sortValue: (o) => o.expected_edge, render: (o) => <Cents value={o.expected_edge} sign tone /> },
              { key: "exp", header: "Expires", sortValue: (o) => parseTs(o.expires_at), render: (o) => <Time value={o.expires_at} stack /> },
              {
                key: "why",
                header: "Reason",
                minWidth: 220,
                render: (o) => (
                  <div>
                    {o.group_id && (
                      <span className="tag mono" title="All-or-none basket">
                        {o.group_id}
                      </span>
                    )}
                    <ClampText text={o.reason} />
                  </div>
                ),
              },
              {
                key: "act",
                header: <span className="sr-only">Actions</span>,
                render: (o) =>
                  isResting(o) ? (
                    <button
                      className="btn btn-sm"
                      onClick={() => cancel(o)}
                      disabled={busy !== null}
                      aria-busy={busy === `cancel-${o.id}`}
                      aria-label={`Cancel order ${String(o.id)} (${orderSummary(o)})`}
                    >
                      Cancel
                    </button>
                  ) : null,
              },
            ]}
          />
        )}
      </PollView>
    </Card>
  );
}

export function PositionsOrders() {
  return (
    <div className="page">
      <PageHeader title="Positions & Orders" subtitle="Kalshi contracts, valued by selling them into the bid ladder for your side (liquidation value); mid values are on the Kalshi dashboard." />
      <PositionsSection />
      <OrdersSection />
    </div>
  );
}
