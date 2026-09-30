import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { cbApi } from "../../api/coinbase/client";
import type { CbStrategy, ParamValue } from "../../api/coinbase/types";
import { hasErrors, ParamEditor, type ParamErrors } from "../../components/ParamEditor";
import { Badge, Card, EmptyState, Freshness, PageHeader, PollView, Switch } from "../../components/ui";
import { Time } from "../../components/values";
import { fmtInt, fmtNum, fmtPnl, fmtUsd, pnlTone } from "../../lib/format";
import { useAction } from "../../lib/hooks";
import { granularityLabel } from "./format";
import { CB_BASE, CbPage, useCbPolling } from "./shared";

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

const SOURCE_TEXT: Record<string, string> = {
  dashboard: "On/off as last set with this switch.",
  config: "On/off as set in config.yaml (coinbase.strategies).",
  default: "On/off by the strategy's built-in default.",
};

function Stat({ label, value, tone, title }: { label: string; value: string; tone?: "pos" | "neg" | "zero"; title?: string }) {
  return (
    <div className="stat" title={title}>
      <div className="stat-label">{label}</div>
      <div className={`stat-value num${tone ? ` tone-${tone}` : ""}`}>{value}</div>
    </div>
  );
}

function StrategyCard({ s, onUpdated }: { s: CbStrategy; onUpdated: (s: CbStrategy) => void }) {
  const { busy, run } = useAction();
  const [draft, setDraft] = useState<Record<string, ParamValue>>(s.params);
  const [errors, setErrors] = useState<ParamErrors>({});
  const [editorKey, setEditorKey] = useState(0);
  const base = useRef(s.params);
  const [serverChanged, setServerChanged] = useState(false);

  const serverKey = JSON.stringify(s.params);
  useEffect(() => {
    if (same(s.params, base.current)) return;
    if (same(draft, base.current)) {
      base.current = s.params;
      setDraft(s.params);
      setErrors({});
      setEditorKey((k) => k + 1);
      setServerChanged(false);
    } else setServerChanged(true);
  }, [serverKey]);

  const dirty = !same(draft, base.current);
  const invalid = hasErrors(errors);

  const toggle = async (enabled: boolean) => {
    const r = await run("toggle", () => cbApi.patchStrategy(s.name, { enabled }), {
      success: `Coinbase strategy ${s.name} ${enabled ? "enabled" : "disabled"}`,
      error: `Couldn't ${enabled ? "enable" : "disable"} Coinbase strategy ${s.name}`,
    });
    if (r) onUpdated(r);
  };

  const save = async () => {
    // Only the edited keys: the backend stores what it receives as overrides.
    const changed = Object.fromEntries(Object.entries(draft).filter(([k, v]) => !same(v, base.current[k])));
    const n = Object.keys(changed).length;
    if (n === 0) return;
    const r = await run("save", () => cbApi.patchStrategy(s.name, { params: changed }), {
      success: `Coinbase ${s.name}: ${n} parameter${n === 1 ? "" : "s"} saved`,
      error: `Couldn't save Coinbase ${s.name} parameters`,
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
            <Badge tone="warn" title="Not validated out of sample against fees: a forward paper test at small size, not an expected profit.">
              Experimental
            </Badge>
          )}
          <Badge tone="neutral" icon="clock" title="Bar size: the strategy decides once per closed bar">
            {granularityLabel(s.bar_granularity_s)}
          </Badge>
          {s.backtestable && <Badge tone="info">Backtestable</Badge>}
        </span>
      }
      actions={
        <span className="inline-toggle">
          <Switch checked={s.enabled} onChange={toggle} label={`Coinbase ${s.name}`} showLabel disabled={busy !== null} />
        </span>
      }
    >
      {s.description && <p className="strategy-desc">{s.description}</p>}
      <div className="cb-meta">
        <span>
          Universe ({fmtInt(s.universe.length)}):
          {s.universe.length === 0 && <span className="muted"> not reported</span>}
        </span>
      </div>
      {s.universe.length > 0 && (
        <ul className="cb-chips" aria-label={`${s.name} universe`}>
          {s.universe.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
      <p className="note muted">
        {s.enabled_source && <>{SOURCE_TEXT[s.enabled_source] ?? `Switch state from ${s.enabled_source}.`} </>}
        {st.allocation_pct !== null && <>May use up to {fmtNum(st.allocation_pct, 0)}% of Coinbase equity. </>}
        Last bar evaluated: <Time value={st.last_bar_at} />.
      </p>
      {st.last_error && <p className="note warn">Last error: {st.last_error}</p>}
      <div className="stat-grid">
        <Stat label="Total P&L" value={fmtPnl(total)} tone={pnlTone(total)} title="Realized + unrealized (liquidation), after fees" />
        <Stat label="Realized" value={fmtPnl(st.realized_pnl)} tone={pnlTone(st.realized_pnl)} />
        <Stat label="Unrealized" value={fmtPnl(st.unrealized_pnl)} tone={pnlTone(st.unrealized_pnl)} />
        <Stat label="Fees" value={fmtUsd(st.fees)} />
        <Stat label="Held (liq.)" value={fmtUsd(st.exposure)} title="Liquidation value of this strategy's holdings" />
        <Stat label="Allocation" value={st.allocation_pct === null ? "—" : `${fmtNum(st.allocation_pct, 0)}%`} title="Share of Coinbase equity this strategy may use" />
        <Stat label="Open pos." value={fmtInt(st.open_positions)} />
        <Stat label="Orders / fills" value={`${fmtInt(st.orders)} / ${fmtInt(st.fills)}`} />
      </div>
      <details className="params" open={dirty || undefined}>
        <summary>
          Parameters <span className="muted">({Object.keys(s.param_schema).length})</span>
          {dirty && <Badge tone="warn">Unsaved changes</Badge>}
        </summary>
        {serverChanged && (
          <p className="note warn">These parameters were changed elsewhere while you were editing. Saving sends only the fields you edited; Reset loads the current values.</p>
        )}
        <ParamEditor
          key={editorKey}
          idPrefix={`cbp-${s.name}`}
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
            <Link className="btn btn-ghost" to={`${CB_BASE}/backtests?strategy=${encodeURIComponent(s.name)}`}>
              Backtest…
            </Link>
          )}
          {invalid && <span className="field-error">Fix the highlighted fields first.</span>}
        </div>
      </details>
    </Card>
  );
}

function StrategiesBody() {
  const poll = useCbPolling((signal) => cbApi.strategies({ signal }), { intervalMs: 10_000, label: "Coinbase strategies", refreshOn: ["fill", "bar"] });
  const onUpdated = (s: CbStrategy) => poll.mutate((prev) => prev?.map((x) => (x.name === s.name ? s : x)));
  return (
    <>
      <PageHeader
        title="Coinbase strategies"
        subtitle="Bar-based target-weight strategies. Each decides once per closed bar (hourly or daily); changes apply from its next bar."
        actions={<Freshness poll={poll} />}
      />
      <PollView<CbStrategy[]> poll={poll} isEmpty={(d) => d.length === 0} empty={<EmptyState title="No Coinbase strategies registered" hint="kalshibot/coinbase/strategies/ has no strategy modules yet." />}>
        {(list) => (
          <div className="strategy-list">
            {list.map((s) => (
              <StrategyCard key={s.name} s={s} onUpdated={onUpdated} />
            ))}
          </div>
        )}
      </PollView>
    </>
  );
}

export function CoinbaseStrategies() {
  return (
    <CbPage>
      <StrategiesBody />
    </CbPage>
  );
}
