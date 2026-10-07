import { useMemo, useState, type ReactNode } from "react";
import { Icon } from "./Icon";
import { EmptyState } from "./ui";

export interface Column<T> {
  key: string;
  header: ReactNode;
  render: (row: T) => ReactNode;
  /** Enables sorting on this column. */
  sortValue?: (row: T) => number | string | null | undefined;
  align?: "left" | "right" | "center";
  /** Header tooltip (explain units / definitions). */
  title?: string;
  className?: string;
  /** CSS min-width for the column. */
  minWidth?: number;
}

export type SortState = { key: string; dir: "asc" | "desc" } | null;

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  defaultSort = null,
  empty,
  maxHeight = 560,
  footer,
  rowClassName,
  caption,
  pageSize = 200,
}: {
  columns: Column<T>[];
  rows: readonly T[];
  rowKey: (row: T, index: number) => string;
  defaultSort?: SortState;
  empty?: ReactNode;
  /** Scroll container max height (px) — header stays sticky inside it. */
  maxHeight?: number | "none";
  footer?: ReactNode;
  rowClassName?: (row: T) => string | undefined;
  caption?: string;
  /** Rows rendered before a "show more" button. */
  pageSize?: number;
}) {
  const [sort, setSort] = useState<SortState>(defaultSort);
  const [shown, setShown] = useState(pageSize);

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const col = columns.find((c) => c.key === sort.key);
    if (!col?.sortValue) return rows;
    const get = col.sortValue;
    const mult = sort.dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      const va = get(a);
      const vb = get(b);
      const na = va === null || va === undefined || (typeof va === "number" && !Number.isFinite(va));
      const nb = vb === null || vb === undefined || (typeof vb === "number" && !Number.isFinite(vb));
      if (na && nb) return 0;
      if (na) return 1; // nulls last regardless of direction
      if (nb) return -1;
      if (typeof va === "number" && typeof vb === "number") return (va - vb) * mult;
      return String(va).localeCompare(String(vb)) * mult;
    });
  }, [rows, sort, columns]);

  const toggle = (c: Column<T>) => {
    if (!c.sortValue) return;
    setSort((s) => {
      if (!s || s.key !== c.key) return { key: c.key, dir: c.align === "right" ? "desc" : "asc" };
      return { key: c.key, dir: s.dir === "asc" ? "desc" : "asc" };
    });
  };

  if (rows.length === 0) return <>{empty ?? <EmptyState title="No rows" />}</>;

  const visible = sorted.slice(0, shown);
  const label = caption ?? "Table";
  return (
    <div
      className="table-wrap"
      style={{ maxHeight: maxHeight === "none" ? undefined : maxHeight }}
      tabIndex={0}
      role="region"
      aria-label={label}
    >
      <table className="table">
        {caption && <caption className="sr-only">{label}</caption>}
        <thead>
          <tr>
            {columns.map((c) => {
              const active = sort?.key === c.key;
              const ariaSort = active ? (sort?.dir === "asc" ? "ascending" : "descending") : undefined;
              return (
                <th
                  key={c.key}
                  scope="col"
                  className={`${c.align ? `al-${c.align}` : ""}${c.className ? ` ${c.className}` : ""}`}
                  style={c.minWidth ? { minWidth: c.minWidth } : undefined}
                  aria-sort={c.sortValue ? (ariaSort ?? "none") : undefined}
                  title={c.title}
                >
                  {c.sortValue ? (
                    <button type="button" className={`th-sort${active ? " active" : ""}`} onClick={() => toggle(c)}>
                      <span>{c.header}</span>
                      <Icon name={active ? (sort?.dir === "asc" ? "sortAsc" : "sortDesc") : "sortNone"} />
                    </button>
                  ) : (
                    c.header
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {visible.map((r, i) => (
            <tr key={rowKey(r, i)} className={rowClassName?.(r)}>
              {columns.map((c) => (
                <td key={c.key} className={`${c.align ? `al-${c.align}` : ""}${c.className ? ` ${c.className}` : ""}`}>
                  {c.render(r)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
        {footer && <tfoot>{footer}</tfoot>}
      </table>
      {sorted.length > shown && (
        <div className="table-more">
          <span>
            Showing {shown.toLocaleString()} of {sorted.length.toLocaleString()}
          </span>
          <button className="btn btn-sm" onClick={() => setShown((n) => n + pageSize * 2)}>
            Show more
          </button>
          <button className="btn btn-sm btn-ghost" onClick={() => setShown(sorted.length)}>
            Show all
          </button>
        </div>
      )}
    </div>
  );
}
