/** Minimal inline stroke icon set (16px, currentColor). */
const PATHS = {
  alert: "M8 1.8 15 14.2H1L8 1.8Zm0 4.4v3.6m0 2.2v.1",
  check: "M2.5 8.5 6 12l7.5-8",
  info: "M8 1.5a6.5 6.5 0 1 1 0 13 6.5 6.5 0 0 1 0-13Zm0 5.3v4.5m0-6.9v.1",
  x: "M3.5 3.5l9 9m0-9-9 9",
  play: "M4.5 2.8v10.4L13 8 4.5 2.8Z",
  stop: "M3.5 3.5h9v9h-9z",
  external: "M9 2.5h4.5V7M13.5 2.5 7.5 8.5M11.5 9.5v4h-9v-9h4",
  refresh: "M13.5 3v3.5H10M2.5 13V9.5H6M3.3 6.5A5 5 0 0 1 12.8 6M12.7 9.5A5 5 0 0 1 3.2 10",
  sortAsc: "M8 3.5 4.5 8h7L8 3.5Z",
  sortDesc: "M8 12.5 4.5 8h7L8 12.5Z",
  sortNone: "M8 2.5 5 6h6L8 2.5Zm0 11L5 10h6l-3 3.5Z",
  shield: "M8 1.5 13.5 3.5v4c0 3.3-2.3 5.8-5.5 7-3.2-1.2-5.5-3.7-5.5-7v-4L8 1.5Z",
  clock: "M8 1.5a6.5 6.5 0 1 1 0 13 6.5 6.5 0 0 1 0-13ZM8 4.5V8l2.5 1.5",
  search: "M7 2a5 5 0 1 1 0 10A5 5 0 0 1 7 2Zm3.6 8.6L14 14",
  dot: "M8 5.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5Z",
  plug: "M5.5 1.5v3m5-3v3M3.5 4.5h9v3a4.5 4.5 0 0 1-9 0v-3ZM8 12v2.5",
  table: "M2 3h12v10H2zM2 6.5h12M2 10h12M6.5 3v10",
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, className, title }: { name: IconName; className?: string; title?: string }) {
  return (
    <svg
      className={`icon${className ? ` ${className}` : ""}`}
      viewBox="0 0 16 16"
      width="16"
      height="16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden={title ? undefined : true}
      role={title ? "img" : undefined}
    >
      {title && <title>{title}</title>}
      <path d={PATHS[name]} />
    </svg>
  );
}
