import { useMemo, useState, type ReactNode } from "react";
import type { OrderStatus, SignalDecision } from "../api/types";
import { useServerNow } from "../lib/hooks";
import {
  centsTone,
  DASH,
  fmtAbsolute,
  fmtCents,
  fmtPnl,
  fmtRelative,
  fmtTooltipTime,
  fmtUsd,
  kalshiUrl,
  pnlTone,
  sideLabel,
} from "../lib/format";
import { Icon } from "./Icon";
import { Badge, type Tone } from "./ui";

/** Signed dollars with tone class; the sign carries meaning, colour only reinforces. */
export function Pnl({ value, className }: { value: number | null | undefined; className?: string }) {
  return <span className={`num tone-${pnlTone(value)}${className ? ` ${className}` : ""}`}>{fmtPnl(value)}</span>;
}

export function Usd({ value }: { value: number | null | undefined }) {
  return <span className="num">{fmtUsd(value)}</span>;
}

/** Price / per-contract value in ¢. The tone follows the value AS DISPLAYED (centsTone). */
export function Cents({ value, sign, tone, dp }: { value: number | null | undefined; sign?: boolean; tone?: boolean; dp?: number }) {
  return <span className={`num${tone ? ` tone-${centsTone(value, dp)}` : ""}`}>{fmtCents(value, { sign, dp })}</span>;
}

export function SideTag({ side }: { side: string }) {
  const s = side.toLowerCase();
  if (s !== "yes" && s !== "no") {
    return (
      <span className="side side-unknown" title={side ? `Unrecognised side "${side}"` : "Side not reported"}>
        {sideLabel(s)}
      </span>
    );
  }
  return <span className={`side side-${s}`}>{sideLabel(s)}</span>;
}

/**
 * Relative + absolute timestamp; full local/UTC time on hover. Values are server
 * timestamps, so "5m ago" is measured on the server's clock (browser skew removed).
 */
export function Time({ value, stack, seconds }: { value: string | null | undefined; stack?: boolean; seconds?: boolean }) {
  const now = useServerNow();
  // The tooltip does not depend on `now`; don't rebuild it on every 5 s clock tick.
  const title = useMemo(() => fmtTooltipTime(value), [value]);
  if (!value) return <span className="muted">{DASH}</span>;
  return (
    <time dateTime={value} title={title} className={stack ? "ts ts-stack" : "ts"}>
      <span className="ts-rel">{fmtRelative(value, now)}</span>
      <span className="ts-abs">{fmtAbsolute(value, { seconds })}</span>
    </time>
  );
}

export function KalshiLink({
  url,
  ticker,
  eventTicker,
  children,
}: {
  url?: string | null;
  ticker?: string | null;
  eventTicker?: string | null;
  children?: ReactNode;
}) {
  const href = kalshiUrl(url, ticker, eventTicker);
  if (!href) return <>{children}</>;
  return (
    <a className="ext-link" href={href} target="_blank" rel="noopener noreferrer" title="Open on kalshi.com (new tab)">
      {children}
      <Icon name="external" />
      <span className="sr-only"> (opens kalshi.com in a new tab)</span>
    </a>
  );
}

/** Ticker (mono, linked to Kalshi) with the market title underneath. */
export function MarketCell({
  ticker,
  title,
  url,
  eventTicker,
}: {
  ticker: string;
  title?: string | null;
  url?: string | null;
  eventTicker?: string | null;
}) {
  return (
    <div className="market-cell">
      <KalshiLink url={url} ticker={ticker} eventTicker={eventTicker}>
        <span className="ticker">{ticker || DASH}</span>
      </KalshiLink>
      {title && (
        <span className="market-title" title={title}>
          {title}
        </span>
      )}
    </div>
  );
}

export function StrategyTag({ name }: { name: string }) {
  return <span className="strategy-tag">{name || DASH}</span>;
}

const DECISION: Record<SignalDecision, { tone: Tone; label: string; icon: "check" | "alert" | "x" | "dot" | "clock" }> = {
  executed: { tone: "good", label: "Executed", icon: "check" },
  partial: { tone: "warn", label: "Partial", icon: "dot" },
  rejected: { tone: "bad", label: "Rejected", icon: "x" },
  unfilled: { tone: "neutral", label: "Unfilled", icon: "dot" },
  unknown: { tone: "neutral", label: "Pending", icon: "clock" },
};

/** Label for a decision; an unknown one shows the raw backend value ("Pending" when empty). */
export function decisionLabel(decision: SignalDecision, raw?: string): string {
  if (decision === "unknown" && raw) return raw;
  return DECISION[decision].label;
}

export function DecisionBadge({ decision, raw }: { decision: SignalDecision; raw?: string }) {
  const d = DECISION[decision];
  return (
    <Badge tone={d.tone} icon={d.icon} title={decision === "unknown" ? (raw ? `Unrecognised decision "${raw}"` : "No decision reported yet") : undefined}>
      {decisionLabel(decision, raw)}
    </Badge>
  );
}

const ORDER_STATUS: Record<OrderStatus, { tone: Tone; label: string }> = {
  open: { tone: "info", label: "Open" },
  partially_filled: { tone: "warn", label: "Partial" },
  filled: { tone: "good", label: "Filled" },
  cancelled: { tone: "neutral", label: "Cancelled" },
  expired: { tone: "neutral", label: "Expired" },
  rejected: { tone: "bad", label: "Rejected" },
  unknown: { tone: "neutral", label: "Unknown" },
};

export function OrderStatusBadge({ status, raw }: { status: OrderStatus; raw?: string }) {
  const s = ORDER_STATUS[status];
  return (
    <Badge tone={s.tone} title={status === "unknown" ? `Unrecognised status "${raw ?? ""}"` : undefined}>
      {status === "unknown" && raw ? raw : s.label}
    </Badge>
  );
}

/** Settlement / backtest result relative to the side held ("won" = result is the held side). */
export function ResultTag({ result, side, kind }: { result: string | null; side?: string; kind?: "settlement" | "close" }) {
  const r = (result ?? "").toLowerCase();
  if (kind === "close") {
    return (
      <Badge tone="neutral" title="Position netted out (or sold) before the market resolved; not a settlement">
        Closed early
      </Badge>
    );
  }
  if (!r) return <span className="muted">{DASH}</span>;
  if (r !== "yes" && r !== "no") return <Badge tone="neutral">{r.toUpperCase()}</Badge>;
  const held = side?.toLowerCase();
  const won = held === "yes" || held === "no" ? r === held : null;
  return (
    <span className="result">
      <span className={`side side-${r}`}>{r.toUpperCase()}</span>
      {won !== null && <span className={won ? "tone-pos" : "tone-neg"}>{won ? " won" : " lost"}</span>}
    </span>
  );
}

/**
 * Long free text (rejection / strategy reasons) clamped to two lines, with a real
 * button to expand it — a hover title is unreachable by keyboard and touch. Screen
 * readers always get the full text (the clamp is visual only).
 */
export function ClampText({ text, className, threshold = 80 }: { text: string; className?: string; threshold?: number }) {
  const [open, setOpen] = useState(false);
  if (!text) return <span className="muted">{DASH}</span>;
  const long = text.length > threshold;
  return (
    <span className="clamp-wrap">
      <span className={`${open ? "clamp-open" : "clamp-2"}${className ? ` ${className}` : ""}`}>{text}</span>
      {long && (
        <button type="button" className="link-btn" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
          {open ? "Show less" : "Show all"}
        </button>
      )}
    </span>
  );
}
