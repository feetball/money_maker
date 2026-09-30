import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useVenueScope } from "../lib/venueScope";
import { Icon } from "./Icon";
import { VenueBadge, type Venue } from "./Venue";

export interface ConfirmOptions {
  title: string;
  body?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  /** User must type this exact text to enable the confirm button. */
  requireText?: string;
  /**
   * Which paper account the action affects (VenueBadge above the title). Omitted = the
   * enclosing VenueScope of the caller; null = none. Titles should still name the
   * venue in words ("Stop the Kalshi engine?").
   */
  venue?: Venue | null;
}

type ConfirmFn = (o: ConfirmOptions) => Promise<boolean>;

const ConfirmContext = createContext<ConfirmFn>(async () => false);

/** Confirm dialog; inside a VenueScope the dialog defaults to that venue's badge. */
export function useConfirm(): ConfirmFn {
  const confirm = useContext(ConfirmContext);
  const scope = useVenueScope();
  return useCallback<ConfirmFn>((o) => confirm(o.venue === undefined && scope ? { ...o, venue: scope } : o), [confirm, scope]);
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [req, setReq] = useState<(ConfirmOptions & { resolve: (v: boolean) => void }) | null>(null);

  const confirm = useCallback<ConfirmFn>(
    (o) =>
      new Promise<boolean>((resolve) => {
        setReq((prev) => {
          prev?.resolve(false);
          return { ...o, resolve };
        });
      }),
    [],
  );

  const close = (v: boolean) => {
    req?.resolve(v);
    setReq(null);
  };

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {req && <ConfirmModal opts={req} onClose={close} />}
    </ConfirmContext.Provider>
  );
}

function ConfirmModal({ opts, onClose }: { opts: ConfirmOptions; onClose: (v: boolean) => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [typed, setTyped] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  // The element that opened the dialog, captured before showModal() moves focus.
  const opener = useRef<HTMLElement | null>(null);
  if (opener.current === null && typeof document !== "undefined" && document.activeElement instanceof HTMLElement) {
    opener.current = document.activeElement;
  }

  // Unmounting an open modal <dialog> does not restore focus (it falls to <body>), so
  // close it explicitly and hand focus back to the opener (e.g. the Kill switch button).
  useEffect(() => {
    const d = ref.current;
    return () => {
      if (d?.open) {
        try {
          d.close();
        } catch {
          d.removeAttribute("open");
        }
      }
      const el = opener.current;
      if (el && el.isConnected && el !== document.body) el.focus();
    };
  }, []);

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (!d.open) {
      try {
        d.showModal();
      } catch {
        d.setAttribute("open", "");
      }
    }
    (opts.requireText ? inputRef.current : cancelRef.current)?.focus();
  }, [opts.requireText]);

  const canConfirm = !opts.requireText || typed.trim() === opts.requireText;

  return (
    <dialog
      ref={ref}
      className="dialog"
      aria-labelledby="confirm-title"
      onCancel={(e) => {
        e.preventDefault();
        onClose(false);
      }}
      onClick={(e) => {
        if (e.target === ref.current) onClose(false);
      }}
    >
      <form
        method="dialog"
        className={opts.venue ? `dialog-inner venue-${opts.venue}` : "dialog-inner"}
        onSubmit={(e) => {
          e.preventDefault();
          if (canConfirm) onClose(true);
        }}
      >
        {opts.venue && (
          <div className="dialog-venue">
            <VenueBadge venue={opts.venue} long size="md" />
          </div>
        )}
        <h2 id="confirm-title" className="dialog-title">
          {opts.danger && <Icon name="alert" className="tone-neg" />}
          {opts.title}
        </h2>
        {opts.body && <div className="dialog-body">{opts.body}</div>}
        {opts.requireText && (
          <label className="field">
            <span className="field-label">
              Type <kbd>{opts.requireText}</kbd> to confirm
            </span>
            <input
              ref={inputRef}
              className="input mono"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              autoComplete="off"
              spellCheck={false}
            />
          </label>
        )}
        <div className="dialog-actions">
          <button type="button" ref={cancelRef} className="btn" onClick={() => onClose(false)}>
            {opts.cancelLabel ?? "Cancel"}
          </button>
          <button type="submit" className={opts.danger ? "btn btn-danger" : "btn btn-primary"} disabled={!canConfirm}>
            {opts.confirmLabel ?? "Confirm"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
