import { useRef, type KeyboardEvent, type ReactNode } from "react";
import { errorMessage } from "../api/client";
import type { PollResult } from "../lib/hooks";
import { useNow } from "../lib/hooks";
import { fmtAbsolute, fmtRelative } from "../lib/format";
import { Icon, type IconName } from "./Icon";

export function Card({
  title,
  subtitle,
  actions,
  children,
  className,
  flush,
  id,
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  /** No body padding (tables that run edge-to-edge). */
  flush?: boolean;
  id?: string;
}) {
  return (
    <section className={`card${className ? ` ${className}` : ""}`} id={id}>
      {(title || actions) && (
        <header className="card-head">
          <div className="card-titles">
            {title && <h2 className="card-title">{title}</h2>}
            {subtitle && <div className="card-sub">{subtitle}</div>}
          </div>
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      <div className={flush ? "card-body flush" : "card-body"}>{children}</div>
    </section>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="page-head">
      <div>
        <h1 className="page-title">{title}</h1>
        {subtitle && <p className="page-sub">{subtitle}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}

export type Tone = "neutral" | "good" | "warn" | "bad" | "info" | "accent" | "serious";

export function Badge({ tone = "neutral", icon, children, title }: { tone?: Tone; icon?: IconName; children: ReactNode; title?: string }) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {icon && <Icon name={icon} />}
      {children}
    </span>
  );
}

export function KpiTile({
  label,
  value,
  sub,
  tone,
  hero,
  title,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "pos" | "neg" | "zero";
  hero?: boolean;
  title?: string;
}) {
  return (
    <div className={`kpi${hero ? " kpi-hero" : ""}`} title={title}>
      <div className="kpi-label">{label}</div>
      <div className={`kpi-value${tone ? ` tone-${tone}` : ""}`}>{value}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
    </div>
  );
}

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  label,
  size = "sm",
}: {
  options: readonly { value: T; label: ReactNode; title?: string }[];
  value: T;
  onChange: (v: T) => void;
  label: string;
  size?: "sm" | "md";
}) {
  // ARIA radio-group pattern: one tab stop (the checked option), arrows/Home/End move
  // and select.
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const current = Math.max(0, options.findIndex((o) => o.value === value));
  const move = (to: number) => {
    const n = options.length;
    if (n === 0) return;
    const i = ((to % n) + n) % n;
    const o = options[i];
    if (!o) return;
    if (o.value !== value) onChange(o.value);
    refs.current[i]?.focus();
  };
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "ArrowRight" || e.key === "ArrowDown") move(current + 1);
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") move(current - 1);
    else if (e.key === "Home") move(0);
    else if (e.key === "End") move(options.length - 1);
    else return;
    e.preventDefault();
  };
  return (
    <div className={`segmented seg-${size}`} role="radiogroup" aria-label={label} onKeyDown={onKeyDown}>
      {options.map((o, i) => (
        <button
          key={o.value}
          ref={(el) => {
            refs.current[i] = el;
          }}
          type="button"
          role="radio"
          aria-checked={o.value === value}
          tabIndex={i === current ? 0 : -1}
          className={o.value === value ? "seg on" : "seg"}
          onClick={() => onChange(o.value)}
          title={o.title}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/**
 * role=switch checkbox. `label` is the accessible name and must NOT describe the
 * state (the checked state already says on/off): "favorite_longshot", not "Disable
 * favorite_longshot". `text` is visible text inside the same <label> (never wrap a
 * Switch in another <label>); `showLabel` shows "On"/"Off".
 */
export function Switch({
  checked,
  onChange,
  label,
  disabled,
  showLabel = false,
  text,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  disabled?: boolean;
  showLabel?: boolean;
  text?: ReactNode;
}) {
  return (
    <label className={`switch${disabled ? " disabled" : ""}`} title={label}>
      <input type="checkbox" role="switch" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} aria-label={label} />
      <span className="switch-track" aria-hidden="true">
        <span className="switch-thumb" />
      </span>
      {text !== undefined ? (
        <span className="switch-text" aria-hidden="true">
          {text}
        </span>
      ) : (
        showLabel && (
          <span className="switch-text" aria-hidden="true">
            {checked ? "On" : "Off"}
          </span>
        )
      )}
    </label>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return <span className="spinner" role="status" aria-label={label} />;
}

export function LoadingBlock({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="state-block">
      <Spinner />
      <span>{label}</span>
    </div>
  );
}

export function EmptyState({ title, hint, icon = "info" }: { title: string; hint?: ReactNode; icon?: IconName }) {
  return (
    <div className="state-block empty">
      <Icon name={icon} />
      <div>
        <div className="state-title">{title}</div>
        {hint && <div className="state-hint">{hint}</div>}
      </div>
    </div>
  );
}

export function ErrorBlock({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  return (
    <div className="state-block error" role="alert">
      <Icon name="alert" />
      <div>
        <div className="state-title">Couldn't load this data</div>
        <div className="state-hint">{errorMessage(error)}</div>
      </div>
      {onRetry && (
        <button className="btn btn-sm" onClick={onRetry}>
          <Icon name="refresh" /> Retry
        </button>
      )}
    </div>
  );
}

/**
 * Standard loading / error / empty handling for a polled resource. Renders children
 * with the data once available; after a failed refresh it keeps the last data at full
 * contrast, marked stale by a warning stripe and a one-line note.
 */
export function PollView<T>({
  poll,
  children,
  isEmpty,
  empty,
  loadingLabel,
}: {
  poll: PollResult<T>;
  children: (data: T) => ReactNode;
  isEmpty?: (data: T) => boolean;
  empty?: ReactNode;
  loadingLabel?: string;
}) {
  if (poll.data === undefined) {
    if (poll.loading) return <LoadingBlock label={loadingLabel} />;
    return <ErrorBlock error={poll.error} onRetry={poll.refresh} />;
  }
  if (isEmpty?.(poll.data)) return <>{empty ?? <EmptyState title="Nothing here yet" />}</>;
  if (!poll.error) return <div>{children(poll.data)}</div>;
  // Absolute time: this text must not change every few seconds.
  const at = poll.updatedAt ? fmtAbsolute(new Date(poll.updatedAt).toISOString(), { seconds: true }) : null;
  return (
    <div className="stale">
      <div className="stale-note">
        <Icon name="alert" /> Refresh failed — showing the last loaded data{at ? ` (from ${at})` : ""}.
      </div>
      {children(poll.data)}
    </div>
  );
}

/** "Updated 4s ago" / "Stale — last update 2m ago" + manual refresh. */
export function Freshness({ poll }: { poll: Pick<PollResult<unknown>, "updatedAt" | "error" | "refreshing" | "refresh"> }) {
  const now = useNow();
  const at = poll.updatedAt ? new Date(poll.updatedAt).toISOString() : null;
  return (
    <span className={`freshness${poll.error ? " is-stale" : ""}`}>
      {poll.error ? (
        <>
          <Icon name="alert" /> {at ? `Stale · updated ${fmtRelative(at, now)}` : "Not loaded"}
        </>
      ) : at ? (
        `Updated ${fmtRelative(at, now)}`
      ) : (
        "Loading…"
      )}
      <button className="icon-btn" onClick={poll.refresh} aria-label="Refresh now" title="Refresh now" disabled={poll.refreshing}>
        <Icon name="refresh" className={poll.refreshing ? "spin" : undefined} />
      </button>
    </span>
  );
}

export function Meter({
  value,
  max,
  label,
  valueLabel,
  warnAt = 0.75,
  badAt = 0.95,
}: {
  value: number;
  max: number | null | undefined;
  label: string;
  valueLabel: ReactNode;
  warnAt?: number;
  badAt?: number;
}) {
  const frac = max && max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
  const level = frac >= badAt ? "bad" : frac >= warnAt ? "warn" : "ok";
  return (
    <div className="meter">
      <div className="meter-head">
        <span className="meter-label">{label}</span>
        <span className="meter-value">
          {level !== "ok" && <Icon name="alert" title={level === "bad" ? "At limit" : "Near limit"} />}
          {valueLabel}
        </span>
      </div>
      <div
        className={`meter-track meter-${level}`}
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={max ?? 0}
        aria-valuenow={value}
      >
        <div className="meter-fill" style={{ width: `${(frac * 100).toFixed(1)}%` }} />
      </div>
    </div>
  );
}

/** id of a Field's hint/error text for the control whose id is `id`. */
export const fieldMsgId = (id: string) => `${id}-msg`;

/** Spread onto the control inside a <Field htmlFor={id}>: links its hint/error and flags errors. */
export function fieldAria(id: string, error?: string | null) {
  return { "aria-describedby": fieldMsgId(id), "aria-invalid": error ? (true as const) : undefined };
}

/** Label + control + hint/error. Give the control `{...fieldAria(htmlFor, error)}`. */
export function Field({
  label,
  hint,
  error,
  children,
  htmlFor,
}: {
  label: ReactNode;
  hint?: ReactNode;
  error?: string | null;
  children: ReactNode;
  htmlFor?: string;
}) {
  return (
    <div className={`field${error ? " has-error" : ""}`}>
      <label className="field-label" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {error ? (
        <div className="field-error" id={htmlFor ? fieldMsgId(htmlFor) : undefined}>
          {error}
        </div>
      ) : hint ? (
        <div className="field-hint" id={htmlFor ? fieldMsgId(htmlFor) : undefined}>
          {hint}
        </div>
      ) : null}
    </div>
  );
}
