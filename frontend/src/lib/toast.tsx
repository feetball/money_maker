import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Icon } from "../components/Icon";
import { VenueBadge, type Venue } from "../components/Venue";
import { useVenueScope } from "./venueScope";

export type ToastKind = "error" | "success" | "info";

interface ToastItem {
  id: number;
  key: string | null;
  kind: ToastKind;
  title: string;
  message: string | null;
  count: number;
  ttl: number;
  venue: Venue | null;
}

export interface ToastOptions {
  /** Toasts with the same key replace each other instead of stacking. */
  key?: string;
  message?: string;
  ttlMs?: number;
  /**
   * Which paper account the toast is about (shown as a VenueBadge). Omitted = the
   * enclosing VenueScope of the component that called useToast(); null = none.
   */
  venue?: Venue | null;
}

export interface ToastApi {
  error: (title: string, opts?: ToastOptions) => void;
  success: (title: string, opts?: ToastOptions) => void;
  info: (title: string, opts?: ToastOptions) => void;
  dismiss: (key: string) => void;
}

const noop = () => undefined;
const ToastContext = createContext<ToastApi>({ error: noop, success: noop, info: noop, dismiss: noop });

/**
 * Toast API. Inside a VenueScope, toasts default to that venue (badge + label) unless
 * the call passes `venue` explicitly (null = no venue, e.g. "Backend unreachable").
 */
export function useToast(): ToastApi {
  const api = useContext(ToastContext);
  const scope = useVenueScope();
  return useMemo<ToastApi>(() => {
    if (!scope) return api;
    const withVenue = (o?: ToastOptions): ToastOptions => (o && "venue" in o ? o : { ...o, venue: scope });
    return {
      error: (t, o) => api.error(t, withVenue(o)),
      success: (t, o) => api.success(t, withVenue(o)),
      info: (t, o) => api.info(t, withVenue(o)),
      dismiss: api.dismiss,
    };
  }, [api, scope]);
}

const MAX_TOASTS = 5;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const remove = useCallback((id: number) => setItems((xs) => xs.filter((x) => x.id !== id)), []);

  const push = useCallback((kind: ToastKind, title: string, opts: ToastOptions = {}) => {
    const ttl = opts.ttlMs ?? (kind === "error" ? 10_000 : 4_500);
    setItems((xs) => {
      const key = opts.key ?? null;
      const existing = key ? xs.find((x) => x.key === key) : undefined;
      if (existing) {
        return xs.map((x) =>
          x === existing
            ? { ...x, kind, title, message: opts.message ?? null, count: x.count + 1, ttl, id: nextId.current++, venue: opts.venue ?? null }
            : x,
        );
      }
      const item: ToastItem = { id: nextId.current++, key, kind, title, message: opts.message ?? null, count: 1, ttl, venue: opts.venue ?? null };
      return [...xs, item].slice(-MAX_TOASTS);
    });
  }, []);

  const api = useMemo<ToastApi>(
    () => ({
      error: (t, o) => push("error", t, o),
      success: (t, o) => push("success", t, o),
      info: (t, o) => push("info", t, o),
      dismiss: (key) => setItems((xs) => xs.filter((x) => x.key !== key)),
    }),
    [push],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toasts" role="region" aria-label="Notifications" aria-live="polite">
        {items.map((t) => (
          <ToastView key={t.id} item={t} onClose={remove} />
        ))}
      </div>
    </ToastContext.Provider>
  );
}

/** `onClose` must be stable (it is `remove`), so other toasts coming and going never restart this timer. */
function ToastView({ item, onClose }: { item: ToastItem; onClose: (id: number) => void }) {
  const [paused, setPaused] = useState(false);
  useEffect(() => {
    if (paused) return;
    const t = setTimeout(() => onClose(item.id), item.ttl);
    return () => clearTimeout(t);
  }, [paused, item.ttl, item.id, onClose]);
  const icon = item.kind === "error" ? "alert" : item.kind === "success" ? "check" : "info";
  return (
    <div
      className={`toast toast-${item.kind}`}
      role={item.kind === "error" ? "alert" : "status"}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
    >
      <Icon name={icon} className="toast-icon" />
      <div className="toast-body">
        {item.venue && (
          <div className="toast-venue">
            <VenueBadge venue={item.venue} />
          </div>
        )}
        <div className="toast-title">
          {item.title}
          {item.count > 1 && <span className="toast-count"> ×{item.count}</span>}
        </div>
        {item.message && <div className="toast-msg">{item.message}</div>}
      </div>
      <button className="icon-btn" onClick={() => onClose(item.id)} aria-label="Dismiss notification">
        <Icon name="x" />
      </button>
    </div>
  );
}
