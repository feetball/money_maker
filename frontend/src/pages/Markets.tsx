import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { MarketRow, MarketSort } from "../api/types";
import { DataTable } from "../components/DataTable";
import { Card, EmptyState, Freshness, PageHeader, PollView } from "../components/ui";
import { Cents, MarketCell, Time } from "../components/values";
import { useDebounced, usePolling, useStoredState } from "../lib/hooks";
import { fmtCompact, parseTs } from "../lib/format";

/** Kalshi's top-level categories; merged with whatever the backend returns. */
const KNOWN_CATEGORIES = [
  "Climate and Weather",
  "Companies",
  "Crypto",
  "Economics",
  "Elections",
  "Entertainment",
  "Financials",
  "Health",
  "Politics",
  "Science and Technology",
  "Sports",
  "World",
];

const SORTS: { value: MarketSort; label: string }[] = [
  { value: "volume_24h", label: "24h volume (high → low)" },
  { value: "close_time", label: "Closing soonest" },
  { value: "spread", label: "Tightest spread" },
];

export function Markets() {
  const [search, setSearch] = useState("");
  const q = useDebounced(search.trim(), 350);
  const [category, setCategory] = useStoredState<string>("kalshibot.marketsCategory", "");
  const [sort, setSort] = useStoredState<MarketSort>("kalshibot.marketsSort", "volume_24h");
  const [limit, setLimit] = useStoredState<number>("kalshibot.marketsLimit", 100);
  const key = JSON.stringify([q, category, sort, limit]);
  const poll = usePolling((signal) => api.markets({ search: q, category, sort, limit }, { signal }).then((rows) => ({ key, rows })), {
    intervalMs: 10_000,
    deps: [key],
    label: "markets",
  });
  const [seenCategories, setSeen] = useState<string[]>(KNOWN_CATEGORIES);
  useEffect(() => {
    const cats = (poll.data?.rows ?? []).map((m) => m.category).filter(Boolean);
    if (cats.some((c) => !seenCategories.includes(c))) setSeen((prev) => [...new Set([...prev, ...cats])].sort());
  }, [poll.data, seenCategories]);

  const stale = poll.data !== undefined && poll.data.key !== key;

  return (
    <div className="page">
      <PageHeader title="Markets" subtitle="Scanner over the open (non-MVE) universe the engine is watching. Prices are YES prices in ¢." actions={<Freshness poll={poll} />} />
      <div className="toolbar">
        <input
          className="input search"
          type="search"
          placeholder="Search ticker or title…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="Search markets"
        />
        <select className="input input-sm" value={category} onChange={(e) => setCategory(e.target.value)} aria-label="Category">
          <option value="">All categories</option>
          {seenCategories.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
        <select className="input input-sm" value={sort} onChange={(e) => setSort(e.target.value as MarketSort)} aria-label="Sort by">
          {SORTS.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>
        <select className="input input-sm" value={limit} onChange={(e) => setLimit(Number(e.target.value))} aria-label="Rows">
          {[100, 250, 500].map((n) => (
            <option key={n} value={n}>
              {n} rows
            </option>
          ))}
        </select>
      </div>
      <Card flush>
        <PollView<{ key: string; rows: MarketRow[] }>
          poll={poll}
          isEmpty={(d) => d.rows.length === 0}
          empty={<EmptyState title="No markets match" hint={q || category ? "Try a broader search or another category." : "The engine has not loaded the market universe yet."} />}
        >
          {(d) => (
            <div className={stale ? "stale" : undefined}>
              <DataTable
                key={`${sort}`}
                caption="Markets"
                rows={d.rows}
                rowKey={(m) => m.ticker}
                maxHeight={700}
                columns={[
                  { key: "m", header: "Market", minWidth: 260, sortValue: (m) => m.ticker, render: (m) => <MarketCell ticker={m.ticker} title={m.title} url={m.url} eventTicker={m.event_ticker} /> },
                  { key: "cat", header: "Category", sortValue: (m) => m.category, render: (m) => <span className="muted nowrap">{m.category || "—"}</span> },
                  { key: "bid", header: "Bid", align: "right", sortValue: (m) => m.yes_bid, render: (m) => <Cents value={m.yes_bid} /> },
                  { key: "ask", header: "Ask", align: "right", sortValue: (m) => m.yes_ask, render: (m) => <Cents value={m.yes_ask} /> },
                  { key: "spr", header: "Spread", align: "right", sortValue: (m) => m.spread, render: (m) => <Cents value={m.spread} /> },
                  { key: "last", header: "Last", align: "right", sortValue: (m) => m.last_price, render: (m) => <Cents value={m.last_price} /> },
                  { key: "vol", header: "Vol 24h", align: "right", title: "Contracts traded in the last 24h", sortValue: (m) => m.volume_24h, render: (m) => <span className="num">{fmtCompact(m.volume_24h)}</span> },
                  { key: "oi", header: "Open int.", align: "right", sortValue: (m) => m.open_interest, render: (m) => <span className="num">{fmtCompact(m.open_interest)}</span> },
                  { key: "close", header: "Closes", sortValue: (m) => parseTs(m.close_time), render: (m) => <Time value={m.close_time} stack /> },
                ]}
              />
            </div>
          )}
        </PollView>
      </Card>
    </div>
  );
}
