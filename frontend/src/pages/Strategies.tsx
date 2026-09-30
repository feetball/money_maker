import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { api } from "../api/client";
import type { ParamValue, Strategy } from "../api/types";
import { hasErrors, ParamEditor, type ParamErrors } from "../components/ParamEditor";
import { Badge, Card, EmptyState, Freshness, PageHeader, PollView, Switch } from "../components/ui";
import { useAction, usePolling } from "../lib/hooks";
import { fmtFrac, fmtInt, fmtNum, fmtPnl, fmtUsd, pnlTone } from "../lib/format";

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

const SOURCE_TEXT: Record<string, string> = {
  dashboard: "On/off as last set with this switch.",
  config: "On/off as set in config.yaml.",
  default: "On/off by the strategy's built-in default (not set in config.yaml or here).",
};

function Stat({ label, value, tone, title }: { label: string; value: string; tone?: "pos" | "neg" | "zero"; title?: string }) {
  return (
    <div className="stat" title={title}>
      <div className="stat-label">{label}</div>
      <div className={`stat-value num${tone ? ` tone-${tone}` : ""}`}>{value}</div>
    </div>
  );
}

function StrategyCard({ s, onUpdated }: { s: Strategy; onUpdated: (s: Strategy) => void }) {
  const { busy, run } = useAction();
  const [draft, setDraft] = useState<Record<string, ParamValue>>(s.params);
  const [errors, setErrors] = useState<ParamErrors>({});
  const [editorKey, setEditorKey] = useState(0);
  const base = useRef(s.params);
  const [serverChanged, setServerChanged] = useState(false);

  // Adopt server-side param changes unless the user has unsaved edits.
  const serverKey = JSON.stringify(s.params);
  useEffect(() => {
    if (same(s.params, base.current)) return;
    if (same(draft, base.current)) {
      base.current = s.params;
      setDraft(s.params);
      setErrors({});
      setEditorKey((k) => k + 1);
      setServerChanged(false);
    } else {
      setServerChanged(true);
    }
  }, [serverKey]);

  const dirty = !same(draft, base.current);
  const invalid = hasErrors(errors);

  const toggle = async (enabled: boolean) => {
    const r = await run("toggle", () => api.patchStrategy(s.name, { enabled }), {
      success: `${s.name} ${enabled ? "enabled" : "disabled"}`,
      error: `Couldn't ${enabled ? "enable" : "disable"} ${s.name}`,
    });
    if (r) onUpdated(r);
  };

  const save = async () => {
    // Send ONLY the edited keys. The backend stores whatever it receives as runtime
    // overrides that beat config.yaml and code defaults, so sending the full object
    // would freeze every other parameter at today's value.
    const changed = Object.fromEntries(Object.entries(draft).filter(([k, v]) => !same(v, base.current[k])));
    if (Object.keys(changed).length === 0) return;
    const n = Object.keys(changed).length;
    const r = await run("save", () => api.patchStrategy(s.name, { params: changed }), {
      success: `${s.name}: ${n} parameter${n === 1 ? "" : "s"} saved`,
      error: `Couldn't save ${s.name} parameters`,
    });
    if (r) {
      base.current = r.params;
      setDraft(r.params);
      setErrors({});
      setEditorKey((k) => k + 1);
      setServerChanged(false);
      onUpdated(r);
    }
  };

  const reset = () => {
    base.current = s.params;
    setDraft(s.params);
    setErrors({});
    setEditorKey((k) => k + 1);
    setServerChanged(false);
  };

  const st = s.stats;
  const total = st.realized_pnl + st.unrealized_pnl;
  return (
    <Card
      className={s.enabled ? "strategy-card" : "strategy-card is-disabled"}
      title={
        <span className="strategy-name">
          <span className="mono">{s.name}</span>
          {s.enabled ? (
            <Badge tone="good" icon="check">
              Enabled
            </Badge>
          ) : (
            <Badge tone="neutral">Disabled</Badge>
          )}
          {s.experimental && (
            <Badge tone="warn" title="Not validated on data it was not tuned on: a forward paper-test at small size, not an expected profit.">
              Experimental
            </Badge>
          )}
          {s.backtestable && <Badge tone="info">Backtestable</Badge>}
          {s.risk_limits?.paused && (
            <Badge tone="bad" title={s.risk_limits.paused}>
              Paused today
            </Badge>
          )}
        </span>
      }
      actions={
        // A <span>, not a <label>: Switch renders its own label. The accessible name is
        // the strategy (the switch state already says on/off).
        <span className="inline-toggle">
          <Switch checked={s.enabled} onChange={toggle} label={s.name} showLabel disabled={busy !== null} />
        </span>
      }
    >
      {s.description && <p className="strategy-desc">{s.description}</p>}
      {(s.enabled_source || s.risk_limits) && (
        <p className="note muted">
          {s.enabled_source && <>{SOURCE_TEXT[s.enabled_source] ?? `Switch state from ${s.enabled_source}.`} </>}
          {s.risk_limits && s.risk_limits.max_allocation_pct !== null && (
            <>
              Own risk limits: at most {fmtNum(s.risk_limits.max_allocation_pct, 0)}% of equity at risk;{" "}
              {s.risk_limits.daily_loss_limit ? `entries pause for the day after a ${fmtUsd(s.risk_limits.daily_loss_limit)} loss.` : "no daily loss pause."}
            </>
          )}
        </p>
      )}
      {s.last_error && <p className="note warn">Last error: {s.last_error}</p>}
      <div className="stat-grid">
        <Stat label="Total P&L" value={fmtPnl(total)} tone={pnlTone(total)} title="Realized + unrealized (liquidation)" />
        <Stat label="Realized" value={fmtPnl(st.realized_pnl)} tone={pnlTone(st.realized_pnl)} />
        <Stat label="Unrealized" value={fmtPnl(st.unrealized_pnl)} tone={pnlTone(st.unrealized_pnl)} />
        <Stat label="Fees" value={fmtUsd(st.fees)} />
        <Stat label="Win rate" value={fmtFrac(st.win_rate)} title="Share of settled trades with positive P&L" />
        <Stat label="Exposure" value={fmtUsd(st.exposure)} title="Capital currently at risk" />
        <Stat label="Open pos." value={fmtInt(st.open_positions)} />
        <Stat label="Settled" value={fmtInt(st.settled)} />
        <Stat label="Orders / fills" value={`${fmtInt(st.orders)} / ${fmtInt(st.fills)}`} />
      </div>
      <details className="params" open={dirty || undefined}>
        <summary>
          Parameters <span className="muted">({Object.keys(s.param_schema).length})</span>
          {dirty && <Badge tone="warn">Unsaved changes</Badge>}
        </summary>
        {serverChanged && (
          <p className="note warn">
            These parameters were changed elsewhere while you were editing. Saving sends only the fields you edited (overwriting those); Reset loads the
            current values.
          </p>
        )}
        <ParamEditor
          key={editorKey}
          idPrefix={`p-${s.name}`}
          schema={s.param_schema}
          values={draft}
          disabled={busy !== null}
          onChange={(v, e) => {
            setDraft(v);
            setErrors(e);
          }}
        />
        <div className="form-actions">
          <button className="btn btn-primary" onClick={save} disabled={!dirty || invalid || busy !== null} aria-busy={busy === "save"}>
            Save parameters
          </button>
          <button className="btn" onClick={reset} disabled={(!dirty && !serverChanged) || busy !== null}>
            Reset
          </button>
          {s.backtestable && (
            <Link className="btn btn-ghost" to={`/kalshi/backtests?strategy=${encodeURIComponent(s.name)}`}>
              Backtest…
            </Link>
          )}
          {invalid && <span className="field-error">Fix the highlighted fields first.</span>}
        </div>
      </details>
    </Card>
  );
}

export function Strategies() {
  const poll = usePolling((signal) => api.strategies({ signal }), { intervalMs: 10_000, label: "strategies", refreshOn: ["settlement"] });
  const onUpdated = (s: Strategy) => poll.mutate((prev) => prev?.map((x) => (x.name === s.name ? s : x)));
  return (
    <div className="page">
      <PageHeader
        title="Strategies"
        subtitle="Enable or disable strategies and tune their parameters. Changes apply from the next engine tick."
        actions={<Freshness poll={poll} />}
      />
      <PollView<Strategy[]> poll={poll} isEmpty={(d) => d.length === 0} empty={<EmptyState title="No strategies registered" hint="The backend REGISTRY is empty." />}>
        {(list) => (
          <div className="strategy-list">
            {list.map((s) => (
              <StrategyCard key={s.name} s={s} onUpdated={onUpdated} />
            ))}
          </div>
        )}
      </PollView>
    </div>
  );
}
