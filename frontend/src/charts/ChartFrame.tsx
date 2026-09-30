import { useState, type ReactNode } from "react";
import { Icon } from "../components/Icon";

export interface LegendItem {
  label: string;
  color: string;
  /** Mirror the mark: line for lines, rect for bars/areas, dot for points. */
  shape: "line" | "rect" | "dot";
  value?: ReactNode;
}

export function Legend({ items }: { items: LegendItem[] }) {
  return (
    <ul className="legend" aria-label="Legend">
      {items.map((i) => (
        <li key={i.label}>
          <svg width="14" height="10" aria-hidden="true" className="legend-key">
            {i.shape === "line" ? (
              <line x1="0" y1="5" x2="14" y2="5" stroke={i.color} strokeWidth="2" strokeLinecap="round" />
            ) : i.shape === "dot" ? (
              <circle cx="7" cy="5" r="4" fill={i.color} />
            ) : (
              <rect x="1" y="1" width="12" height="8" rx="2" fill={i.color} />
            )}
          </svg>
          <span className="legend-label">{i.label}</span>
          {i.value !== undefined && <span className="legend-value">{i.value}</span>}
        </li>
      ))}
    </ul>
  );
}

/**
 * Chart container: legend row, the plot, and a "Table" toggle that swaps in the
 * accessible table twin of the same data. Height includes the axis band.
 */
export function ChartFrame({
  legend,
  table,
  children,
  height,
  label,
  dim,
  toolbar,
}: {
  legend?: LegendItem[];
  table: ReactNode;
  children: ReactNode;
  height: number;
  label: string;
  /** Dim while refetching (hold the previous render, no skeleton flash). */
  dim?: boolean;
  toolbar?: ReactNode;
}) {
  const [asTable, setAsTable] = useState(false);
  return (
    <figure className="chart-frame" aria-label={label}>
      <div className="chart-top">
        {legend && legend.length > 0 ? <Legend items={legend} /> : <span />}
        <div className="chart-tools">
          {toolbar}
          {/* Fixed name + aria-pressed (a label that also flipped would announce backwards). */}
          <button
            type="button"
            className={asTable ? "btn btn-sm btn-ghost is-pressed" : "btn btn-sm btn-ghost"}
            onClick={() => setAsTable((v) => !v)}
            aria-pressed={asTable}
            title={asTable ? "Showing the table — press to go back to the chart" : "Show the data as a table"}
          >
            <Icon name="table" />
            <span>Table</span>
          </button>
        </div>
      </div>
      {asTable ? (
        <div className="chart-table">{table}</div>
      ) : (
        <div className={dim ? "chart-plot dim" : "chart-plot"} style={{ height }} role="img" aria-label={label}>
          {children}
        </div>
      )}
    </figure>
  );
}

export interface TooltipRow {
  label: string;
  value: string;
  color?: string;
  shape?: "line" | "rect" | "dot";
}

/** Tooltip body: value leads (strong), series label follows, keyed with a short line. */
export function TooltipCard({ title, rows, note }: { title?: string; rows: TooltipRow[]; note?: string }) {
  return (
    <div className="chart-tooltip">
      {title && <div className="tt-title">{title}</div>}
      {rows.map((r) => (
        <div className="tt-row" key={r.label}>
          {r.color ? (
            <svg width="12" height="8" aria-hidden="true">
              {r.shape === "dot" ? (
                <circle cx="6" cy="4" r="3.5" fill={r.color} />
              ) : r.shape === "rect" ? (
                <rect x="1" y="0.5" width="10" height="7" rx="1.5" fill={r.color} />
              ) : (
                <line x1="0" y1="4" x2="12" y2="4" stroke={r.color} strokeWidth="2" strokeLinecap="round" />
              )}
            </svg>
          ) : (
            <span className="tt-nokey" />
          )}
          <strong className="tt-value">{r.value}</strong>
          <span className="tt-label">{r.label}</span>
        </div>
      ))}
      {note && <div className="tt-note">{note}</div>}
    </div>
  );
}
