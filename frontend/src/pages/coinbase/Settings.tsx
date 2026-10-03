import { useEffect, useRef, useState } from "react";
import { CB_API_BASE, cbApi, IS_MOCK } from "../../api/coinbase/client";
import { useCbStatus } from "../../api/coinbase/status";
import { cbStreamStore, useCbStreamInfo } from "../../api/coinbase/stream";
import type { CbExposureRow, CbRisk, CbRiskLimits } from "../../api/coinbase/types";
import { useConfirm } from "../../components/ConfirmDialog";
import { DataTable } from "../../components/DataTable";
import { Icon } from "../../components/Icon";
import { ThemeControl } from "../../components/ThemeControl";
import { Badge, Card, EmptyState, Field, fieldAria, Freshness, Meter, PageHeader, PollView, Switch } from "../../components/ui";
import { StrategyTag, Usd } from "../../components/values";
import { fmtInt, fmtPct, fmtPnl, fmtUsd, humanize, MINUS } from "../../lib/format";
import { useAction } from "../../lib/hooks";
import { useOverview } from "../../lib/overview";
import { fmtRate } from "./format";
import { CbPage, ProductCell, useCbLiveAccount, useCbPolling } from "./shared";

interface LimitMeta {
  label: string;
  unit: string;
  help: string;
  step?: number;
  max?: number;
  integer?: boolean;
}

/** Contract §11 limits; unknown keys are shown generically. */
const LIMIT_META: Record<string, LimitMeta> = {
  max_position_pct_per_product: { label: "Max per product", unit: "% of equity", help: "Cap on the value held in any one coin, as a share of Coinbase equity.", step: 1, max: 100 },
  max_total_exposure_pct: { label: "Max total exposure", unit: "% of equity", help: "All coin holdings + cash reserved by resting buys, as a share of Coinbase equity.", step: 1, max: 100 },
  max_strategy_allocation_pct: { label: "Max per strategy", unit: "% of equity", help: "Share of Coinbase equity any one strategy may use (a strategy can set a lower one).", step: 1, max: 100 },
  min_cash_reserve: { label: "Min cash reserve", unit: "$", help: "Buys are rejected if they would leave less than this much USD cash.", step: 5 },
  max_orders_per_minute: { label: "Max orders / minute", unit: "orders", help: "Throttle across all Coinbase strategies. 0 = no throttle.", step: 1, integer: true },
  daily_loss_limit: {
    label: "Daily loss limit",
    unit: "$",
    help: "Losing this much in a UTC day trips the Coinbase kill switch (blocks buys; sells to reduce risk are still allowed). 0 = disabled.",
    step: 5,
  },
  max_spread_bps: { label: "Max spread", unit: "bps", help: "Skip products whose bid/ask spread is wider than this (1 bp = 0.01 %).", step: 5, integer: false },
  min_trade_usd: { label: "Min trade size", unit: "$", help: "Rebalance orders smaller than this are skipped (Coinbase itself requires about $1).", step: 1 },
};

const ZERO_DISABLES: Record<string, string> = {
  daily_loss_limit: "disables the automatic Coinbase kill switch — no daily loss will stop new buys",
  max_orders_per_minute: "turns off the Coinbase order-rate throttle",
};

function riskyChanges(changed: Record<string, number | boolean | string>, limits: CbRiskLimits): string[] {
  const out: string[] = [];
  for (const [k, what] of Object.entries(ZERO_DISABLES)) {
    if (changed[k] === 0 && limits[k] !== 0) out.push(`${LIMIT_META[k]?.label ?? humanize(k)} = 0 ${what}.`);
  }
  const dll = changed.daily_loss_limit;
  const before = limits.daily_loss_limit;
  if (typeof dll === "number" && dll > 0 && typeof before === "number" && before > 0 && dll >= before * 3) {
    out.push(`Coinbase daily loss limit rises ${(dll / before).toFixed(1)}× (${fmtUsd(before)} → ${fmtUsd(dll)}) before the kill switch trips.`);
  }
  for (const k of ["max_position_pct_per_product", "max_total_exposure_pct", "max_strategy_allocation_pct"]) {
    const v = changed[k];
    if (typeof v === "number" && v >= 100 && (limits[k] as number) < 100) out.push(`${LIMIT_META[k]?.label ?? k} = 100 % removes that cap.`);
  }
  return out;
}

function RiskLimitsForm({ risk, onSaved }: { risk: CbRisk; onSaved: (r: CbRisk) => void }) {
  const { busy, run } = useAction();
  const confirm = useConfirm();
  const keys = Object.keys(risk.limits);
  const toDraft = (l: CbRiskLimits) => Object.fromEntries(Object.entries(l).map(([k, v]) => [k, v === null || v === undefined ? "" : String(v)]));
  const [draft, setDraft] = useState<Record<string, string>>(() => toDraft(risk.limits));
  const base = useRef(risk.limits);

  const serverKey = JSON.stringify(risk.limits);
  useEffect(() => {
    const prev = base.current;
    base.current = risk.limits;
    setDraft((d) => {
      const next = { ...d };
      for (const [k, v] of Object.entries(risk.limits)) {
        const prevStr = prev[k] === null || prev[k] === undefined ? "" : String(prev[k]);
        if (d[k] === undefined || d[k] === prevStr) next[k] = v === null || v === undefined ? "" : String(v);
      }
      return next;
    });
  }, [serverKey]);

  const errors: Record<string, string | null> = {};
  const changed: Record<string, number | boolean | string> = {};
  for (const k of keys) {
    const orig = risk.limits[k];
    const raw = draft[k] ?? "";
    if (typeof orig === "boolean") {
      const b = raw === "true";
      if (b !== orig) changed[k] = b;
      continue;
    }
    if (typeof orig === "number" || orig === null) {
      const meta = LIMIT_META[k];
      const blank = raw.trim() === "";
      if (orig === null && blank) continue;
      const n = Number(raw);
      if (blank || !Number.isFinite(n)) errors[k] = "Enter a number";
      else if (n < 0) errors[k] = "Must be ≥ 0";
      else if (meta?.max !== undefined && n > meta.max) errors[k] = `Must be ≤ ${meta.max}`;
      else if (meta?.integer && !Number.isInteger(n)) errors[k] = "Whole number";
      else if (n !== orig) changed[k] = n;
      continue;
    }
    if (raw !== String(orig ?? "")) changed[k] = raw;
  }
  const invalid = Object.values(errors).some(Boolean);
  const dirty = Object.keys(changed).length > 0;
  const pristine = toDraft(risk.limits);
  const edited = keys.some((k) => (draft[k] ?? "") !== (pristine[k] ?? ""));

  const save = async () => {
    const risky = riskyChanges(changed, risk.limits);
    if (risky.length) {
      const ok = await confirm({
        title: "Loosen a Coinbase safety limit?",
        danger: true,
        confirmLabel: "Save Coinbase limits anyway",
        body: (
          <ul>
            {risky.map((x) => (
              <li key={x}>{x}</li>
            ))}
          </ul>
        ),
      });
      if (!ok) return;
    }
    const r = await run("save", () => cbApi.patchRisk(changed), { success: "Coinbase risk limits saved", error: "Couldn't save the Coinbase risk limits" });
    if (r) {
      base.current = r.limits;
      setDraft(toDraft(r.limits));
      onSaved(r);
    }
  };

  if (keys.length === 0) return <EmptyState title="The backend reported no Coinbase risk limits" />;

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (dirty && !invalid) void save();
      }}
      noValidate
    >
      <div className="param-grid">
        {keys.map((k) => {
          const meta = LIMIT_META[k];
          const orig = risk.limits[k];
          const id = `cbrisk-${k}`;
          const raw = draft[k] ?? "";
          const n = Number(raw);
          const zeroOff = raw.trim() !== "" && n === 0 && ZERO_DISABLES[k] ? `0 ${ZERO_DISABLES[k]}` : null;
          const hint = orig === null && raw.trim() === "" ? "No limit set (leave empty to keep it that way)" : (zeroOff ?? meta?.help);
          return (
            <Field
              key={k}
              htmlFor={id}
              label={
                <>
                  {meta?.label ?? humanize(k)} <span className="field-key">{k}</span>
                </>
              }
              error={errors[k]}
              hint={hint}
            >
              {typeof orig === "boolean" ? (
                <div className="field-inline">
                  <Switch checked={raw === "true"} onChange={(b) => setDraft((d) => ({ ...d, [k]: String(b) }))} label={meta?.label ?? humanize(k)} showLabel />
                </div>
              ) : (
                <div className="input-affix">
                  {meta?.unit === "$" && <span className="affix">$</span>}
                  <input
                    id={id}
                    className="input num-input"
                    type={typeof orig === "string" ? "text" : "number"}
                    inputMode="decimal"
                    step={meta?.step ?? "any"}
                    min={0}
                    max={meta?.max}
                    value={raw}
                    placeholder={orig === null ? "no limit" : undefined}
                    {...fieldAria(id, errors[k])}
                    onChange={(e) => setDraft((d) => ({ ...d, [k]: e.target.value }))}
                  />
                  {meta && meta.unit !== "$" && <span className="affix">{meta.unit}</span>}
                </div>
              )}
            </Field>
          );
        })}
      </div>
      <div className="form-actions">
        <button type="submit" className="btn btn-primary" disabled={!dirty || invalid || busy !== null} aria-busy={busy === "save"}>
          Save Coinbase limits{dirty ? ` (${Object.keys(changed).length})` : ""}
        </button>
        <button type="button" className="btn" disabled={(!dirty && !edited) || busy !== null} onClick={() => setDraft(toDraft(risk.limits))}>
          Discard changes
        </button>
        {invalid && <span className="field-error">Fix the highlighted fields first.</span>}
      </div>
    </form>
  );
}

function exposureTable(rows: CbExposureRow[], kind: "product" | "strategy") {
  return (
    <DataTable
      caption={`Coinbase exposure by ${kind}`}
      rows={rows}
      rowKey={(r) => r.key}
      maxHeight={280}
      defaultSort={{ key: "exp", dir: "desc" }}
      empty={<EmptyState title={`No ${kind} exposure`} />}
      columns={[
        { key: "k", header: kind === "product" ? "Product" : "Strategy", sortValue: (r) => r.key, render: (r) => (kind === "product" ? <ProductCell pid={r.key} /> : <StrategyTag name={r.key} />) },
        { key: "exp", header: "Held", align: "right", sortValue: (r) => r.exposure, render: (r) => <Usd value={r.exposure} /> },
        { key: "pct", header: "% of equity", align: "right", sortValue: (r) => r.pct, render: (r) => <span className="num">{fmtPct(r.pct, { dp: 1 })}</span> },
        {
          key: "lim",
          header: "Limit",
          align: "right",
          sortValue: (r) => r.limit_pct,
          render: (r) => {
            const near = r.pct !== null && r.limit_pct !== null && r.limit_pct > 0 && r.pct / r.limit_pct >= 0.9;
            return (
              <span className={`num${near ? " tone-neg" : ""}`}>
                {near && <Icon name="alert" title="At or near the limit" />} {fmtPct(r.limit_pct, { dp: 0 })}
              </span>
            );
          },
        },
      ]}
    />
  );
}

function Utilization({ risk }: { risk: CbRisk }) {
  const u = risk.utilization;
  const l = risk.limits;
  const { status } = useCbStatus();
  const killOn = status ? status.engine.kill_switch : risk.kill_switch;
  const reason = killOn ? (status?.engine.kill_switch_reason ?? risk.kill_switch_reason) : null;
  const num = (v: unknown) => (typeof v === "number" ? v : null);
  const maxExp = num(l.max_total_exposure_pct);
  const maxOrders = num(l.max_orders_per_minute);
  const lossLimit = num(l.daily_loss_limit);
  const loss = Math.max(0, -u.daily_pnl);
  return (
    <>
      <div className="meters">
        <Meter
          label="Total exposure"
          value={u.total_exposure_pct}
          max={maxExp}
          valueLabel={
            <>
              {fmtPct(u.total_exposure_pct)} of {fmtPct(maxExp, { dp: 0 })} · {fmtUsd(u.total_exposure)}
            </>
          }
        />
        <Meter
          label="Orders, last minute"
          value={u.orders_last_minute}
          max={maxOrders === 0 ? null : maxOrders}
          valueLabel={
            <>
              {fmtInt(u.orders_last_minute)} of {maxOrders === 0 ? "no throttle" : fmtInt(maxOrders)}
            </>
          }
        />
        <Meter
          label={lossLimit === 0 ? "Today's loss (no limit)" : "Today's loss"}
          value={loss}
          max={lossLimit === 0 ? null : lossLimit}
          valueLabel={
            <>
              {fmtPnl(u.daily_pnl)} · limit {lossLimit === null ? "—" : lossLimit === 0 ? <strong className="tone-neg">disabled</strong> : `${MINUS}${fmtUsd(lossLimit)}`}
            </>
          }
        />
        <div className="kill-state">
          {killOn ? (
            <Badge tone="bad" icon="shield" title={reason ?? undefined}>
              Coinbase kill switch ON — buys blocked
            </Badge>
          ) : (
            <Badge tone="good" icon="check">
              Coinbase kill switch off
            </Badge>
          )}
          {killOn && (
            <div className="kill-reason">
              {reason ? (
                <>
                  Reason: <span className="mono wrap">{reason}</span>
                </>
              ) : (
                <span className="muted">No reason reported (usually engaged manually).</span>
              )}
            </div>
          )}
        </div>
      </div>
      <div className="grid grid-2 tight">
        <div>
          <h3 className="sub-title">By product</h3>
          {exposureTable(u.by_product, "product")}
        </div>
        <div>
          <h3 className="sub-title">By strategy</h3>
          {exposureTable(u.by_strategy, "strategy")}
        </div>
      </div>
    </>
  );
}

function AccountReset() {
  const { data: account, poll: accountPoll } = useCbLiveAccount();
  const { refresh } = useCbStatus();
  const overview = useOverview();
  const confirm = useConfirm();
  const { busy, run } = useAction();
  const [balance, setBalance] = useState("");
  const current = account?.starting_balance;
  const value = balance.trim() === "" ? current : Number(balance);
  const invalid = value === undefined || !Number.isFinite(value) || value <= 0;

  const reset = async () => {
    if (invalid || value === undefined) return;
    const ok = await confirm({
      title: "Reset the Coinbase paper account?",
      danger: true,
      confirmLabel: "Reset Coinbase account",
      body: (
        <>
          <p>
            This <strong>stops the Coinbase engine</strong> and permanently wipes all Coinbase paper state: holdings, orders, fills, signals and the Coinbase
            equity history. Coinbase analytics start again from zero.
          </p>
          <p>
            New Coinbase starting balance: <strong className="num">{fmtUsd(value)}</strong> USD. Coinbase strategy settings and risk limits are kept.
          </p>
          <p>
            <strong>The Kalshi paper account is not touched.</strong>
          </p>
        </>
      ),
    });
    if (!ok) return;
    const r = await run("reset", () => cbApi.resetAccount(balance.trim() === "" ? undefined : value), {
      success: `Coinbase paper account reset to ${fmtUsd(value)}`,
      error: "Couldn't reset the Coinbase account",
    });
    if (r) {
      setBalance("");
      cbStreamStore.clear();
      accountPoll.mutate(() => r);
      accountPoll.refresh();
      refresh();
      overview.refresh();
    }
  };

  return (
    <div className="reset">
      <p>
        Current Coinbase starting balance: <strong className="num">{fmtUsd(current)}</strong>
        {account && (
          <>
            {" "}
            · equity (liquidation) now <span className="num">{fmtUsd(account.equity)}</span> ({fmtPnl(account.total_pnl)})
          </>
        )}
      </p>
      <div className="form-row">
        <Field
          label="New Coinbase starting balance (USD)"
          htmlFor="cb-reset-balance"
          hint="Leave empty to keep the current starting balance."
          error={balance && invalid ? "Enter a positive amount" : null}
        >
          <input
            id="cb-reset-balance"
            className="input num-input"
            type="number"
            min={1}
            step={100}
            inputMode="decimal"
            placeholder={current !== undefined ? String(current) : "1000"}
            value={balance}
            {...fieldAria("cb-reset-balance", balance && invalid ? "Enter a positive amount" : null)}
            onChange={(e) => setBalance(e.target.value)}
          />
        </Field>
      </div>
      <button className="btn btn-danger" onClick={reset} disabled={invalid || busy !== null} aria-busy={busy === "reset"}>
        <Icon name="alert" /> Reset Coinbase paper account…
      </button>
    </div>
  );
}

function Connection() {
  const stream = useCbStreamInfo();
  const { status } = useCbStatus();
  return (
    <dl className="kv">
      <dt>Theme</dt>
      <dd>
        <ThemeControl />
      </dd>
      <dt>Data source</dt>
      <dd>{IS_MOCK ? "Mock Coinbase data generated in the browser (VITE_MOCK=1)" : <span className="mono">{CB_API_BASE} (same origin)</span>}</dd>
      <dt>Event stream</dt>
      <dd>
        <span className="mono">{CB_API_BASE}/stream</span> · {stream.state}
        {stream.attempts > 0 && ` · ${stream.attempts} failed attempt(s)`}
      </dd>
      <dt>Fee tier</dt>
      <dd>
        {status ? (
          <>
            <span className="mono">{status.fee_tier.name}</span> ({status.fee_tier.label}) · maker {fmtRate(status.fee_tier.maker_rate)} · taker {fmtRate(status.fee_tier.taker_rate)}
            <div className="muted">Set with coinbase.fee_tier (or coinbase.fee_rates) in config.yaml.</div>
          </>
        ) : (
          "—"
        )}
      </dd>
      <dt>Mode</dt>
      <dd>
        <Badge tone="warn" icon="shield">
          Paper trading only
        </Badge>{" "}
        <span className="muted">Public Coinbase market data only — no real orders, no API keys.</span>
      </dd>
    </dl>
  );
}

function SettingsBody() {
  const poll = useCbPolling((signal) => cbApi.risk({ signal }), { intervalMs: 7500, label: "Coinbase risk", refreshOn: ["fill"] });
  const { status } = useCbStatus();
  const kill = status?.engine.kill_switch;
  const { refresh } = poll;
  const seenKill = useRef(kill);
  useEffect(() => {
    if (seenKill.current !== undefined && kill !== undefined && kill !== seenKill.current) refresh();
    seenKill.current = kill;
  }, [kill, refresh]);
  return (
    <>
      <PageHeader
        title="Coinbase settings"
        subtitle="Coinbase risk limits are checked on every Coinbase order intent before it reaches the paper broker. Kalshi has its own, separate limits."
        actions={<Freshness poll={poll} />}
      />
      <Card title="Coinbase risk limits" subtitle="Changes apply to the next intent the Coinbase engine evaluates">
        <PollView<CbRisk> poll={poll} loadingLabel="Loading Coinbase risk limits…">
          {(r) => <RiskLimitsForm risk={r} onSaved={(n) => poll.mutate(() => n)} />}
        </PollView>
      </Card>
      <Card title="Current Coinbase utilization">
        <PollView<CbRisk> poll={poll}>{(r) => <Utilization risk={r} />}</PollView>
      </Card>
      <div className="grid grid-2">
        <Card title="Reset Coinbase paper account" className="danger-zone">
          <AccountReset />
        </Card>
        <Card title="Display & Coinbase connection">
          <Connection />
        </Card>
      </div>
    </>
  );
}

export function CoinbaseSettings() {
  return (
    <CbPage>
      <SettingsBody />
    </CbPage>
  );
}
