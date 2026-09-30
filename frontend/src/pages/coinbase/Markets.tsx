import { useState } from "react";
import { cbApi } from "../../api/coinbase/client";
import type { CbProductRow, CbProductSort } from "../../api/coinbase/types";
import { DataTable } from "../../components/DataTable";
import { Badge, Card, EmptyState, Freshness, PageHeader, PollView } from "../../components/ui";
import { fmtPct, pnlTone } from "../../lib/format";
import { useDebounced, useStoredState } from "../../lib/hooks";
import { fmtUsdCompact } from "./format";
import { Bps, CbPage, Price, ProductCell, useCbPolling } from "./shared";

const SORTS: { value: CbProductSort; label: string }[] = [
  { value: "volume", label: "24h USD volume (high → low)" },
  { value: "spread", label: "Tightest spread" },
  { value: "change", label: "24h change (high → low)" },
];

function MarketsBody() {
  const [search, setSearch] = useState("");
  const q = useDebounced(search.trim(), 350);
  const [sort, setSort] = useStoredState<CbProductSort>("kalshibot.cb.productsSort", "volume");
  const [limit, setLimit] = useStoredState<number>("kalshibot.cb.productsLimit", 100);
  const [tradableOnly, setTradableOnly] = useStoredState<boolean>("kalshibot.cb.tradableOnly", false);
  const key = JSON.stringify([q, sort, limit]);
  const poll = useCbPolling((signal) => cbApi.products({ search: q, sort, limit }, { signal }).then((rows) => ({ key, rows })), {
    intervalMs: 15_000,
    deps: [key],
    label: "Coinbase products",
  });
  const stale = poll.data !== undefined && poll.data.key !== key;

  return (
    <>
      <PageHeader
        title="Coinbase markets"
        subtitle="Scanner over the USD spot products the Coinbase engine loads. Prices in USD per coin; spread in basis points of the mid (1 bp = 0.01 %)."
        actions={<Freshness poll={poll} />}
      />
      <div className="toolbar">
        <input className="input search" type="search" placeholder="Search product (e.g. SOL)…" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Search Coinbase products" />
        <select className="input input-sm" value={sort} onChange={(e) => setSort(e.target.value as CbProductSort)} aria-label="Sort Coinbase products by">
          {SORTS.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>
        <select className="input input-sm" value={limit} onChange={(e) => setLimit(Number(e.target.value))} aria-label="Rows">
          {[50, 100, 250].map((n) => (
            <option key={n} value={n}>
              {n} rows
            </option>
          ))}
        </select>
        <label className="inline-field">
          <input type="checkbox" checked={tradableOnly} onChange={(e) => setTradableOnly(e.target.checked)} /> <span>Tradable only</span>
        </label>
      </div>
      <Card flush>
        <PollView<{ key: string; rows: CbProductRow[] }>
          poll={poll}
          isEmpty={(d) => d.rows.length === 0}
          empty={<EmptyState title="No Coinbase products match" hint={q ? "Try a broader search." : "The Coinbase engine has not loaded products yet (refreshes hourly)."} />}
        >
          {(d) => {
            const rows = tradableOnly ? d.rows.filter((r) => r.tradable) : d.rows;
            return (
              <div className={stale ? "stale" : undefined}>
                <DataTable
                  key={sort}
                  caption="Coinbase products"
                  rows={rows}
                  rowKey={(m) => m.product_id}
                  maxHeight={700}
                  empty={<EmptyState title="No tradable products in this list" />}
                  columns={[
                    { key: "p", header: "Product", sortValue: (m) => m.product_id, render: (m) => <ProductCell pid={m.product_id} url={m.url} /> },
                    { key: "px", header: "Price", align: "right", sortValue: (m) => m.price, render: (m) => <Price value={m.price} /> },
                    { key: "bid", header: "Bid", align: "right", sortValue: (m) => m.bid, render: (m) => <Price value={m.bid} /> },
                    { key: "ask", header: "Ask", align: "right", sortValue: (m) => m.ask, render: (m) => <Price value={m.ask} /> },
                    {
                      key: "spr",
                      header: "Spread",
                      align: "right",
                      title: "(ask − bid) ÷ mid, in basis points. Round-trip taker fees are ~240 bps at the lowest tier.",
                      sortValue: (m) => m.spread_bps,
                      render: (m) => <Bps value={m.spread_bps} />,
                    },
                    {
                      key: "chg",
                      header: "24h change",
                      align: "right",
                      sortValue: (m) => m.change_24h_pct,
                      render: (m) => <span className={`num tone-${pnlTone(m.change_24h_pct, 0.05)}`}>{fmtPct(m.change_24h_pct, { sign: true, dp: 2 })}</span>,
                    },
                    { key: "vol", header: "Volume 24h", align: "right", title: "USD traded in the last 24 hours", sortValue: (m) => m.volume_24h_usd, render: (m) => <span className="num">{fmtUsdCompact(m.volume_24h_usd)}</span> },
                    {
                      key: "tr",
                      header: "Status",
                      sortValue: (m) => (m.tradable ? 1 : 0),
                      render: (m) =>
                        m.tradable ? (
                          <Badge tone="good" icon="check">
                            Tradable
                          </Badge>
                        ) : (
                          <Badge tone="neutral" icon="x" title="Not online, trading disabled or cancel-only: the paper broker rejects orders">
                            Not tradable
                          </Badge>
                        ),
                    },
                  ]}
                />
              </div>
            );
          }}
        </PollView>
      </Card>
    </>
  );
}

export function CoinbaseMarkets() {
  return (
    <CbPage>
      <MarketsBody />
    </CbPage>
  );
}
