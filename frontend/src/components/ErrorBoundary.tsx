import { Component, type ErrorInfo, type ReactNode } from "react";
import { Icon } from "./Icon";

/**
 * Catches render errors of the routed page so a crash (a chart library error, an
 * unexpected payload that slipped past normalize.ts) replaces only the page body.
 * The top bar and nav stay mounted; on Kalshi pages KalshiSection has its own
 * boundary below the Kalshi banner, so its Start/Stop and kill-switch buttons survive
 * a page crash too. Keyed by pathname, so navigating elsewhere resets it.
 */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  override state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: unknown): { error: Error } {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  override componentDidCatch(error: unknown, info: ErrorInfo) {
    console.error("Page crashed:", error, info.componentStack);
  }

  override render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="state-block error page-crash" role="alert">
        <Icon name="alert" />
        <div>
          <div className="state-title">This page hit an error and could not be shown</div>
          <div className="state-hint mono wrap">{error.message || String(error)}</div>
          <div className="state-hint">The engine controls in the top bar still work. Other pages are unaffected.</div>
        </div>
        <div className="crash-actions">
          <button className="btn btn-sm" onClick={() => this.setState({ error: null })}>
            <Icon name="refresh" /> Try again
          </button>
          <button className="btn btn-sm btn-ghost" onClick={() => window.location.reload()}>
            Reload app
          </button>
        </div>
      </div>
    );
  }
}
