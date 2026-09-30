import { useMemo, useState } from "react";
import { cbApi } from "../../api/coinbase/client";
import type { CbOrder, CbOrderStatusFilter, CbPosition } from "../../api/coinbase/types";
import { useConfirm } from "../../components/ConfirmDialog";
import { DataTable } from "../../components/DataTable";
import { Card, EmptyState, Freshness, PageHeader, PollView, Segmented } from "../../components/ui";
import { ClampText, OrderStatusBadge, Pnl, StrategyTag, Time, Usd } from "../../components/values";
import { fmtPct, fmtPnl, fmtUsd, parseTs, pnlTone } from "../../lib/format";
import { useAction, useStoredState } from "../../lib/hooks";
import { useToast } from "../../lib/toast";
import { baseOf, fmtPrice, fmtQty, fmtWeight } from "./format";
import { CbPage, CbSideTag, CbStrategyFilter as StrategyFilter, Fee, Price, ProductCell, Qty, sizeText, tolerate409, uniqSorted, useCbPolling, Weight } from "./shared";

const isResting = (o: CbOrder) => o.tif === "gtc" && (o.status === "open" || o.status === "partially_filled");

/** "BUY $25.00 of SOL @ $182.10 limit, post-only (momentum_rotation)" — names the order in dialogs. */
function orderSummary(o: CbOrder): string {
  const px = o.limit_price !== null ? ` @ ${fmtPrice(o.limit_price)} limit` : " at market";
  return `${o.side.toUpperCase()} ${sizeText(o)}${px}${o.post_only ? ", post-only" : ""} (${o.strategy || "no strategy"})`;
}

function PositionsSection() {
  const poll = useCbPolling((signal) => cbApi.positions({ signal }), { intervalMs: 5000, label: "Coinbase positions", refreshOn: ["fill"] });
  const [strategy, setStrategy] = useState("");
  const strategies = useMemo(() => uniqSorted((poll.data ?? []).map((p) => p.strategy)), [poll.data]);
  const rows = (poll.data ?? []).filter((p) => !strategy || p.strategy === strategy);
  const tot = rows.reduce(
    (a, p) => ({ cost: a.cost + p.cost_basis, liq: a.liq + p.liquidation_value, mid: a.mid + (p.mid_value ?? 0), u: a.u + p.unrealized_pnl, r: a.r + p.realized_pnl, fees: a.fees + p.fees_paid }),
    { cost: 0, liq: 0, mid: 0, u: 0, r: 0, fees: 0 },
  );
  return (
    <Card
      title="Coinbase holdings"
      subtitle={
        poll.data ? (
          <>
            {rows.length} positions · cost {fmtUsd(tot.cost)} (incl. buy fees) · liquidation value {fmtUsd(tot.liq)} · mid {fmtUsd(tot.mid)} · unrealized{" "}
            <span className="num">{fmtPnl(tot.u)}</span>
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
      <PollView<CbPosition[]>
        poll={poll}
        isEmpty={(d) => d.length === 0}
        empty={<EmptyState title="No open Coinbase positions" hint="Holdings appear when a strategy's buy fills. Spot only: no shorting, so every position is a long." />}
      >
        {() => (
          <DataTable
            caption="Coinbase holdings"
            rows={rows}
            rowKey={(p) => `${p.product_id}-${p.strategy}`}
            defaultSort={{ key: "liq", dir: "desc" }}
            empty={<EmptyState title="No positions for this strategy" />}
            columns={[
              { key: "p", header: "Product", minWidth: 120, sortValue: (p) => p.product_id, render: (p) => <ProductCell pid={p.product_id} url={p.url} /> },
              { key: "s", header: "Strategy", sortValue: (p) => p.strategy, render: (p) => <StrategyTag name={p.strategy} /> },
              { key: "q", header: "Quantity", align: "right", sortValue: (p) => p.quantity, render: (p) => <Qty value={p.quantity} base={p.base_currency} /> },
              { key: "avg", header: "Avg cost", align: "right", title: "USD per unit, including buy fees", sortValue: (p) => p.avg_cost, render: (p) => <Price value={p.avg_cost} /> },
              {
                key: "mark",
                header: "Liq. price",
                align: "right",
                title: "Average price selling the whole quantity into the bid ladder now (= best bid when the top level covers it); below it the best bid and the mid price",
                sortValue: (p) => p.mark_price,
                render: (p) => {
                  const midPx = p.mid_value !== null && p.quantity > 0 ? p.mid_value / p.quantity : null;
                  return (
                    <span className="cb-stack">
                      <Price value={p.mark_price} />
                      <span className="cb-stack-sub">
                        bid {fmtPrice(p.best_bid)} · mid {fmtPrice(midPx)}
                      </span>
                    </span>
                  );
                },
              },
              { key: "cost", header: "Cost basis", align: "right", title: "What the open quantity cost, including buy fees", sortValue: (p) => p.cost_basis, render: (p) => <Usd value={p.cost_basis} /> },
              {
                key: "liq",
                header: "Liq. value",
                align: "right",
                title: "Selling the whole quantity into the bids now, before the sell fee",
                sortValue: (p) => p.liquidation_value,
                render: (p) => <Usd value={p.liquidation_value} />,
              },
              { key: "u", header: "Unreal. $", align: "right", title: "Liquidation value − cost basis", sortValue: (p) => p.unrealized_pnl, render: (p) => <Pnl value={p.unrealized_pnl} /> },
              {
                key: "upct",
                header: "Unreal. %",
                align: "right",
                title: "Unrealized P&L ÷ cost basis",
                sortValue: (p) => p.unrealized_pnl_pct,
                render: (p) => <span className={`num tone-${pnlTone(p.unrealized_pnl_pct, 0.05)}`}>{fmtPct(p.unrealized_pnl_pct, { sign: true, dp: 2 })}</span>,
              },
              {
                key: "mid",
                header: "Mid value",
                align: "right",
                title: "Quantity × mid price",
                sortValue: (p) => p.mid_value,
                render: (p) => (
                  <span className="num muted">
                    {fmtUsd(p.mid_value)}
                    {p.mid_value !== null && p.mid_value - p.liquidation_value > 0.005 && (
                      <span title="Mid value minus liquidation value (the cost of crossing the spread and walking the ladder)"> (−{fmtUsd(p.mid_value - p.liquidation_value)})</span>
                    )}
                  </span>
                ),
              },
              { key: "r", header: "Realized", align: "right", title: "Realized P&L from earlier sells of this product by this strategy", sortValue: (p) => p.realized_pnl, render: (p) => <Pnl value={p.realized_pnl} /> },
              { key: "fees", header: "Fees", align: "right", sortValue: (p) => p.fees_paid, render: (p) => <Usd value={p.fees_paid} /> },
              {
                key: "w",
                header: "Weight",
                align: "right",
                title: "Share of the strategy's allocation this holding represents",
                sortValue: (p) => p.weight_of_strategy,
                render: (p) => <Weight value={p.weight_of_strategy} />,
              },
              { key: "open", header: "Opened", sortValue: (p) => parseTs(p.opened_at), render: (p) => <Time value={p.opened_at} stack /> },
            ]}
            footer={
              <tr className="totals">
                {/* 5 leading columns + the Venue column DataTable adds inside the Coinbase scope. */}
                <td colSpan={6}>Total ({rows.length})</td>
                <td className="al-right">
                  <Usd value={tot.cost} />
                </td>
                <td className="al-right">
                  <Usd value={tot.liq} />
                </td>
                <td className="al-right">
                  <Pnl value={tot.u} />
                </td>
                <td className="al-right">
                  <span className={`num tone-${pnlTone(tot.cost ? (tot.u / tot.cost) * 100 : null, 0.05)}`}>{fmtPct(tot.cost ? (tot.u / tot.cost) * 100 : null, { sign: true, dp: 2 })}</span>
                </td>
                <td className="al-right">
                  <Usd value={tot.mid} />
                </td>
                <td className="al-right">
                  <Pnl value={tot.r} />
                </td>
                <td className="al-right">
                  <Usd value={tot.fees} />
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
  const [status, setStatus] = useStoredState<CbOrderStatusFilter>("kalshibot.cb.ordersStatus", "open");
  const [strategy, setStrategy] = useState("");
  const poll = useCbPolling((signal) => cbApi.orders({ status, limit: status === "all" ? 500 : 200 }, { signal }), {
    intervalMs: 5000,
    deps: [status],
    label: "Coinbase orders",
    refreshOn: ["order", "fill"],
  });
  const { busy, run } = useAction();
  const confirm = useConfirm();
  const toast = useToast();
  const strategies = useMemo(() => uniqSorted((poll.data ?? []).map((o) => o.strategy)), [poll.data]);
  const rows = (poll.data ?? []).filter((o) => !strategy || o.strategy === strategy);

  const cancel = async (o: CbOrder) => {
    const ok = await confirm({
      title: `Cancel Coinbase order #${String(o.id)}?`,
      body: (
        <p>
          {orderSummary(o)}. The USD reserved for the unfilled part returns to Coinbase cash; anything already filled ({fmtQty(o.filled_base, baseOf(o.product_id))}) stays in
          the position.
        </p>
      ),
      confirmLabel: "Cancel Coinbase order",
      cancelLabel: "Keep order",
      danger: true,
    });
    if (!ok) return;
    const conflict = { detail: "" };
    const r = await run(`cancel-${o.id}`, () => tolerate409(cbApi.cancelOrder(o.id), (d) => (conflict.detail = d)), {
      error: `Couldn't cancel Coinbase order ${String(o.id)}`,
    });
    if (r === null) toast.info(`Coinbase order ${String(o.id)} is no longer open`, { message: conflict.detail || orderSummary(o) });
    else if (r) {
      if (r.status === "cancelled") toast.success(`Coinbase order ${String(o.id)} cancelled`, { message: orderSummary(o) });
      else toast.info(`Coinbase order ${String(o.id)} was already ${(r.status === "unknown" ? r.status_raw || "closed" : r.status).replace("_", " ")}`, { message: orderSummary(r) });
      poll.mutate((prev) => prev?.map((x) => (String(x.id) === String(r.id) ? r : x)));
    }
    poll.refresh();
  };

  return (
    <Card
      title="Coinbase orders"
      subtitle={status === "open" ? "Resting (GTC) orders; they fill only from later public trades through their price" : "Most recent Coinbase orders, all states"}
      actions={
        <>
          <Segmented<CbOrderStatusFilter>
            label="Coinbase order status"
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
      <PollView<CbOrder[]>
        poll={poll}
        isEmpty={(d) => d.length === 0}
        empty={
          <EmptyState
            title={status === "open" ? "No resting Coinbase orders" : "No Coinbase orders yet"}
            hint="Market / IOC orders fill or cancel immediately, so only post-only or GTC limit orders appear as open."
          />
        }
      >
        {() => (
          <DataTable
            caption="Coinbase orders"
            rows={rows}
            rowKey={(o) => String(o.id)}
            defaultSort={{ key: "created", dir: "desc" }}
            empty={<EmptyState title="No orders for this strategy" />}
            columns={[
              { key: "created", header: "Created", sortValue: (o) => parseTs(o.created_at), render: (o) => <Time value={o.created_at} stack seconds /> },
              { key: "p", header: "Product", sortValue: (o) => o.product_id, render: (o) => <ProductCell pid={o.product_id} /> },
              { key: "s", header: "Strategy", sortValue: (o) => o.strategy, render: (o) => <StrategyTag name={o.strategy} /> },
              { key: "side", header: "Side", sortValue: (o) => o.side, render: (o) => <CbSideTag side={o.side} /> },
              {
                key: "type",
                header: "Type",
                render: (o) => (
                  <span className="mono nowrap">
                    {o.order_type.toUpperCase()} {o.tif.toUpperCase()}
                    {o.post_only ? " · post-only" : ""}
                  </span>
                ),
              },
              { key: "size", header: "Size", align: "right", title: "Buys by USD amount (incl. fee); sells by quantity", render: (o) => <span className="num nowrap">{sizeText(o)}</span> },
              { key: "lim", header: "Limit", align: "right", sortValue: (o) => o.limit_price, render: (o) => <Price value={o.limit_price} /> },
              {
                key: "filled",
                header: "Filled",
                align: "right",
                sortValue: (o) => o.filled_base,
                render: (o) => (
                  <span className="num nowrap" title={`${fmtUsd(o.filled_quote)} traded`}>
                    {fmtQty(o.filled_base, baseOf(o.product_id))}
                  </span>
                ),
              },
              { key: "st", header: "Status", sortValue: (o) => o.status, render: (o) => <OrderStatusBadge status={o.status} raw={o.status_raw} /> },
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
                      aria-label={`Cancel Coinbase order ${String(o.id)} (${orderSummary(o)})`}
                    >
                      Cancel
                    </button>
                  ) : null,
              },
              { key: "avg", header: "Avg fill", align: "right", sortValue: (o) => o.avg_fill_price, render: (o) => <Price value={o.avg_fill_price} /> },
              { key: "fees", header: "Fees", align: "right", sortValue: (o) => o.fees, render: (o) => <Fee fee={o.fees} notional={o.filled_quote - (o.side === "buy" ? o.fees : 0)} /> },
              { key: "exp", header: "Expires", sortValue: (o) => parseTs(o.expires_at), render: (o) => <Time value={o.expires_at} stack /> },
              { key: "why", header: "Reason", minWidth: 220, render: (o) => <ClampText text={o.reason} /> },
            ]}
          />
        )}
      </PollView>
    </Card>
  );
}

export function CoinbasePositions() {
  return (
    <CbPage>
      <PageHeader
        title="Coinbase positions & orders"
        subtitle={
          <>
            Quantities in coin units, prices in USD per coin. Holdings are valued by selling into the Coinbase bid ladder (liquidation); the mid value shows what
            crossing the spread costs. Weight = share of the strategy's allocation ({fmtWeight(1)} = fully invested).
          </>
        }
      />
      <PositionsSection />
      <OrdersSection />
    </CbPage>
  );
}
