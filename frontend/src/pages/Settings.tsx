import { useEffect, useRef, useState } from "react";
import { api, API_BASE, IS_MOCK } from "../api/client";
import type { ExposureRow, RiskLimits, RiskResponse } from "../api/types";
import { useConfirm } from "../components/ConfirmDialog";
import { DataTable } from "../components/DataTable";
import { Icon } from "../components/Icon";
import { Badge, Card, EmptyState, Field, fieldAria, Freshness, Meter, PageHeader, PollView, Switch } from "../components/ui";
import { StrategyTag, Usd } from "../components/values";
import { useAction, usePolling } from "../lib/hooks";
import { fmtCents, fmtInt, fmtPct, fmtPnl, fmtUsd, humanize, MINUS } from "../lib/format";
import { useStatus } from "../lib/status";
import { streamStore, useStreamInfo } from "../lib/stream";
import { ThemeControl } from "../components/ThemeControl";
import { useLiveAccount } from "./Dashboard";

interface LimitMeta {
  label: string;
  unit: string;
  help: string;
  step?: number;
  max?: number;
  integer?: boolean;
  extra?: (v: number) => string;
}

/** Labels/units for the limits of ARCHITECTURE §8 / §14; unknown keys are shown generically. */
const LIMIT_META: Record<string, LimitMeta> = {
  max_position_cost_per_market: { label: "Max cost per market", unit: "$", help: "Cap on total cost basis held in any single market.", step: 5 },
  max_exposure_per_event: { label: "Max exposure per event", unit: "$", help: "Across all markets of one event (e.g. every bracket of a weather event).", step: 5 },
  max_total_exposure_pct: { label: "Max total exposure", unit: "% of equity", help: "All open positions + reserved cash, as a share of equity.", step: 1, max: 100 },
  max_strategy_allocation_pct: { label: "Max per strategy", unit: "% of equity", help: "Exposure any one strategy may hold.", step: 1, max: 100 },
  min_cash_reserve: { label: "Min cash reserve", unit: "$", help: "Orders are rejected if they would dip below this much free cash.", step: 5 },
  max_orders_per_minute: { label: "Max orders / minute", unit: "orders", help: "Throttle across all strategies. 0 = no throttle.", step: 1, integer: true },
  daily_loss_limit: {
    label: "Daily loss limit",
    unit: "$",
    help: "Losing this much in a UTC day trips the kill switch (existing positions still settle). 0 = disabled.",
    step: 5,
  },
  min_seconds_to_close: { label: "No entries within", unit: "s of close", help: "Blocks new entries this close to a market's close time. 0 = off.", step: 30, integer: true, extra: (v) => `${(v / 60).toFixed(1)} min` },
  max_spread: { label: "Max spread", unit: "$", help: "Skip markets whose bid/ask spread is wider than this.", step: 0.01, max: 1, extra: (v) => `= ${fmtCents(v)}` },
  kelly_fraction: { label: "Kelly fraction", unit: "× Kelly", help: "Fraction of full-Kelly used by the sizing helper.", step: 0.05, max: 1 },
};

/** Limits where the backend treats 0 as "off" (risk.py only enforces them when > 0). */
const ZERO_DISABLES: Record<string, string> = {
  daily_loss_limit: "disables the automatic kill switch — no daily loss will stop new entries",
  max_orders_per_minute: "turns off the order-rate throttle",
  min_seconds_to_close: "allows new entries right up to a market's close",
};

/** Changes that loosen a safety control and need an explicit confirmation. */
function riskyChanges(changed: Record<string, number | boolean | string>, limits: RiskLimits): string[] {
  const out: string[] = [];
  for (const [k, what] of Object.entries(ZERO_DISABLES)) {
    if (changed[k] === 0 && limits[k] !== 0) out.push(`${LIMIT_META[k]?.label ?? humanize(k)} = 0 ${what}.`);
  }
  const dll = changed.daily_loss_limit;
  const before = limits.daily_loss_limit;
  if (typeof dll === "number" && dll > 0 && typeof before === "number" && before > 0 && dll >= before * 3) {
    out.push(`Daily loss limit rises ${(dll / before).toFixed(1)}× (${fmtUsd(before)} → ${fmtUsd(dll)}) before the kill switch trips.`);
  }
  return out;
}

function RiskLimitsForm({ risk, onSaved }: { risk: RiskResponse; onSaved: (r: RiskResponse) => void }) {
  const { busy, run } = useAction();
  const confirm = useConfirm();
  const keys = Object.keys(risk.limits);
  const toDraft = (l: RiskLimits) => Object.fromEntries(Object.entries(l).map(([k, v]) => [k, v === null || v === undefined ? "" : String(v)]));
  const [draft, setDraft] = useState<Record<string, string>>(() => toDraft(risk.limits));
  const base = useRef(risk.limits);

  // Adopt server changes for fields the user hasn't touched.
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
      // A limit the backend reports as null ("no limit") stays untouched while empty;
      // it is only validated and sent once the user types a value.
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
  // Any edit at all, including an invalid one (a cleared field is not in `changed`):
  // Discard must still be able to bring the saved values back.
  const pristine = toDraft(risk.limits);
  const edited = keys.some((k) => (draft[k] ?? "") !== (pristine[k] ?? ""));

  const save = async () => {
    const risky = riskyChanges(changed, risk.limits);
    if (risky.length) {
      const ok = await confirm({
        title: "Loosen a Kalshi safety limit?",
        danger: true,
        confirmLabel: "Save anyway",
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
    const r = await run("save", () => api.patchRisk(changed), { success: "Kalshi risk limits saved", error: "Couldn't save Kalshi risk limits" });
    if (r) {
      base.current = r.limits;
      setDraft(toDraft(r.limits));
      onSaved(r);
    }
  };

  if (keys.length === 0) return <EmptyState title="The backend reported no risk limits" />;

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
          const id = `risk-${k}`;
          const raw = draft[k] ?? "";
          const n = Number(raw);
          const zeroOff = raw.trim() !== "" && n === 0 && ZERO_DISABLES[k] ? `0 ${ZERO_DISABLES[k]}` : null;
          const extra = zeroOff ?? (meta?.extra && raw !== "" && Number.isFinite(n) ? meta.extra(n) : null);
          const hint = orig === null && raw.trim() === "" ? "No limit set (leave empty to keep it that way)" : [meta?.help, extra].filter(Boolean).join(" · ") || undefined;
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
          Save limits{dirty ? ` (${Object.keys(changed).length})` : ""}
        </button>
        <button type="button" className="btn" disabled={(!dirty && !edited) || busy !== null} onClick={() => setDraft(toDraft(risk.limits))}>
          Discard changes
        </button>
        {invalid && <span className="field-error">Fix the highlighted fields first.</span>}
      </div>
    </form>
  );
}

function exposureTable(rows: ExposureRow[], kind: "event" | "strategy") {
  return (
    <DataTable
      caption={`Exposure by ${kind}`}
      rows={rows}
      rowKey={(r) => r.key}
      maxHeight={280}
      defaultSort={{ key: "exp", dir: "desc" }}
      empty={<EmptyState title={`No ${kind} exposure`} />}
      columns={[
        { key: "k", header: kind === "event" ? "Event" : "Strategy", sortValue: (r) => r.key, render: (r) => (kind === "event" ? <span className="ticker">{r.key}</span> : <StrategyTag name={r.key} />) },
        { key: "exp", header: "Exposure", align: "right", sortValue: (r) => r.exposure, render: (r) => <Usd value={r.exposure} /> },
        { key: "lim", header: "Limit", align: "right", sortValue: (r) => r.limit, render: (r) => <Usd value={r.limit} /> },
        {
          key: "pct",
          header: "Used",
          align: "right",
          sortValue: (r) => r.pct ?? (r.limit ? (r.exposure / r.limit) * 100 : null),
          render: (r) => {
            const pct = r.pct ?? (r.limit ? (r.exposure / r.limit) * 100 : null);
            return <span className={`num${pct !== null && pct >= 90 ? " tone-neg" : ""}`}>{fmtPct(pct, { dp: 0 })}</span>;
          },
        },
      ]}
    />
  );
}

function Utilization({ risk }: { risk: RiskResponse }) {
  const u = risk.utilization;
  const l = risk.limits;
  // On/off comes from /api/status (updated the moment the header toggles it); the
  // reason comes from /api/risk (refreshed when the status flips, see Settings).
  const { status } = useStatus();
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
        <Meter label="Total exposure (% of equity)" value={u.total_exposure_pct} max={maxExp} valueLabel={<>{fmtPct(u.total_exposure_pct)} of {fmtPct(maxExp, { dp: 0 })} · {fmtUsd(u.total_exposure)}</>} />
        <Meter
          label="Orders in the last minute"
          value={u.orders_last_minute}
          max={maxOrders === 0 ? null : maxOrders}
          valueLabel={<>{fmtInt(u.orders_last_minute)} of {maxOrders === 0 ? "no throttle" : fmtInt(maxOrders)}</>}
        />
        <Meter
          label={lossLimit === 0 ? "Today's loss (daily limit disabled)" : "Today's loss vs daily limit"}
          value={loss}
          max={lossLimit === 0 ? null : lossLimit}
          valueLabel={
            <>
              today {fmtPnl(u.daily_pnl)} · limit{" "}
              {lossLimit === null ? "—" : lossLimit === 0 ? <strong className="tone-neg">disabled</strong> : `${MINUS}${fmtUsd(lossLimit)}`}
            </>
          }
        />
        <div className="kill-state">
          {killOn ? (
            <Badge tone="bad" icon="shield" title={reason ?? undefined}>
              Kill switch ON — new entries blocked
            </Badge>
          ) : (
            <Badge tone="good" icon="check">
              Kill switch off
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
          <h3 className="sub-title">By event</h3>
          {exposureTable(u.by_event, "event")}
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
  const { data: account, poll: accountPoll } = useLiveAccount();
  const { refresh } = useStatus();
  const confirm = useConfirm();
  const { busy, run } = useAction();
  const [balance, setBalance] = useState("");
  const current = account?.starting_balance;
  const value = balance.trim() === "" ? current : Number(balance);
  const invalid = value === undefined || !Number.isFinite(value) || value <= 0;

  const reset = async () => {
    if (invalid || value === undefined) return;
    const ok = await confirm({
      title: "Reset the Kalshi paper account?",
      danger: true,
      requireText: "RESET",
      confirmLabel: "Reset Kalshi account",
      body: (
        <>
          <p>
            This <strong>stops the Kalshi engine</strong> and permanently wipes all Kalshi paper state: positions, orders, fills, settlements, signals and
            the equity history. Kalshi analytics start again from zero. The Coinbase paper account is separate and is not touched.
          </p>
          <p>
            New starting balance: <strong className="num">{fmtUsd(value)}</strong>. Strategy settings and risk limits are kept.
          </p>
        </>
      ),
    });
    if (!ok) return;
    const r = await run("reset", () => api.resetAccount(balance.trim() === "" ? undefined : value), {
      success: `Kalshi paper account reset to ${fmtUsd(value)}`,
      error: "Couldn't reset the Kalshi account",
    });
    if (r) {
      setBalance("");
      // Drop pre-reset SSE events and the cached SSE account (the Dashboard would
      // otherwise flash the old balance and list fills of the wiped account), and
      // show the new account right away instead of after the next poll.
      streamStore.clear();
      accountPoll.mutate(() => r);
      accountPoll.refresh();
      refresh();
    }
  };

  return (
    <div className="reset">
      <p>
        Current starting balance: <strong className="num">{fmtUsd(current)}</strong>
        {account && (
          <>
            {" "}
            · equity (liquidation) now <span className="num">{fmtUsd(account.equity)}</span> ({fmtPnl(account.total_pnl)})
          </>
        )}
      </p>
      <div className="form-row">
        <Field label="New starting balance ($)" htmlFor="reset-balance" hint="Leave empty to keep the current starting balance." error={balance && invalid ? "Enter a positive amount" : null}>
          <input
            id="reset-balance"
            className="input num-input"
            type="number"
            min={1}
            step={100}
            inputMode="decimal"
            placeholder={current !== undefined ? String(current) : "1000"}
            value={balance}
            {...fieldAria("reset-balance", balance && invalid ? "Enter a positive amount" : null)}
            onChange={(e) => setBalance(e.target.value)}
          />
        </Field>
      </div>
      <button className="btn btn-danger" onClick={reset} disabled={invalid || busy !== null} aria-busy={busy === "reset"}>
        <Icon name="alert" /> Reset paper account…
      </button>
    </div>
  );
}

function ProfitSweep() {
  const { data: account, poll: accountPoll } = useLiveAccount();
  const { busy, run } = useAction();
  const [pct, setPct] = useState("");
  const enabled = account?.profit_sweep_enabled ?? true;
  const currentPct = account?.profit_sweep_pct;
  const pctValue = pct.trim() === "" ? currentPct : Number(pct);
  const pctInvalid = pctValue === undefined || !Number.isFinite(pctValue) || pctValue < 0 || pctValue > 100;
  const pctDirty = pct.trim() !== "" && Number(pct) !== currentPct;

  const toggle = async (next: boolean) => {
    const r = await run("toggle", () => api.patchAccount({ profit_sweep_enabled: next }), {
      success: `Profit sweep ${next ? "enabled" : "disabled"}`,
      error: "Couldn't update the profit sweep",
    });
    if (r) accountPoll.mutate(() => r);
  };

  const savePct = async () => {
    if (pctInvalid || pctValue === undefined) return;
    const r = await run("pct", () => api.patchAccount({ profit_sweep_pct: pctValue }), {
      success: `Profit sweep set to ${pctValue}%`,
      error: "Couldn't update the profit sweep",
    });
    if (r) {
      accountPoll.mutate(() => r);
      setPct("");
    }
  };

  const [withdraw, setWithdraw] = useState("");
  const reserved = account?.reserved_profit ?? 0;
  const withdrawValue = withdraw.trim() === "" ? reserved : Number(withdraw);
  const withdrawInvalid = !Number.isFinite(withdrawValue) || withdrawValue < 0 || withdrawValue > reserved;

  const doWithdraw = async () => {
    if (withdrawInvalid || reserved <= 0) return;
    const r = await run(
      "withdraw",
      () => api.withdrawProfit(withdraw.trim() === "" ? {} : { amount: withdrawValue }),
      { success: `${fmtUsd(withdrawValue)} moved from reserved profit to cash`, error: "Couldn't withdraw reserved profit" },
    );
    if (r) {
      accountPoll.mutate(() => r);
      setWithdraw("");
    }
  };

  return (
    <div className="reset">
      <p>
        Winning trades set aside <strong className="num">{fmtUsd(reserved)}</strong> reserved profit, kept out of the tradeable pool
        {account && (
          <>
            {" "}
            · net worth <span className="num">{fmtUsd(account.net_worth)}</span>
          </>
        )}
        .
      </p>
      <div className="form-row">
        <Switch checked={enabled} onChange={toggle} label="Sweep profit out of cash" showLabel disabled={busy !== null} />
      </div>
      <div className="form-row">
        <Field label="Sweep %" htmlFor="sweep-pct" hint="% of each winning trade's profit moved to reserved profit." error={pct && pctInvalid ? "Enter 0-100" : null}>
          <input
            id="sweep-pct"
            className="input num-input"
            type="number"
            min={0}
            max={100}
            step={5}
            inputMode="decimal"
            placeholder={currentPct !== undefined ? String(currentPct) : "100"}
            value={pct}
            {...fieldAria("sweep-pct", pct && pctInvalid ? "Enter 0-100" : null)}
            onChange={(e) => setPct(e.target.value)}
          />
        </Field>
        <button className="btn" onClick={savePct} disabled={!pctDirty || pctInvalid || busy !== null} aria-busy={busy === "pct"}>
          Save %
        </button>
      </div>
      <div className="form-row">
        <Field
          label="Move back to cash ($)"
          htmlFor="withdraw-amount"
          hint={`Leave empty to withdraw all ${fmtUsd(reserved)}.`}
          error={withdraw && withdrawInvalid ? `Enter 0-${reserved}` : null}
        >
          <input
            id="withdraw-amount"
            className="input num-input"
            type="number"
            min={0}
            max={reserved}
            step={1}
            inputMode="decimal"
            placeholder={fmtUsd(reserved)}
            value={withdraw}
            {...fieldAria("withdraw-amount", withdraw && withdrawInvalid ? `Enter 0-${reserved}` : null)}
            onChange={(e) => setWithdraw(e.target.value)}
          />
        </Field>
        <button className="btn" onClick={doWithdraw} disabled={reserved <= 0 || withdrawInvalid || busy !== null} aria-busy={busy === "withdraw"}>
          <Icon name="check" /> Withdraw to cash
        </button>
      </div>
    </div>
  );
}

function Preferences() {
  const stream = useStreamInfo();
  return (
    <dl className="kv">
      <dt>Theme</dt>
      <dd>
        <ThemeControl />
      </dd>
      <dt>Data source</dt>
      <dd>{IS_MOCK ? "Mock data generated in the browser (VITE_MOCK=1)" : <span className="mono">{API_BASE} (same origin)</span>}</dd>
      <dt>Event stream</dt>
      <dd>
        <span className="mono">{API_BASE}/stream</span> · {stream.state}
        {stream.attempts > 0 && ` · ${stream.attempts} failed attempt(s)`}
      </dd>
      <dt>Mode</dt>
      <dd>
        <Badge tone="warn" icon="shield">
          Paper trading only
        </Badge>{" "}
        <span className="muted">No real orders, no Kalshi credentials.</span>
      </dd>
    </dl>
  );
}

export function Settings() {
  const poll = usePolling((signal) => api.risk({ signal }), { intervalMs: 7500, label: "risk", refreshOn: ["fill", "settlement"] });
  // Refetch risk (utilization + kill_switch_reason) as soon as the kill switch flips.
  const { status } = useStatus();
  const kill = status?.engine.kill_switch;
  const { refresh } = poll;
  const seenKill = useRef(kill);
  useEffect(() => {
    if (seenKill.current !== undefined && kill !== undefined && kill !== seenKill.current) refresh();
    seenKill.current = kill;
  }, [kill, refresh]);
  return (
    <div className="page">
      <PageHeader title="Settings" subtitle="Kalshi risk limits are enforced on every Kalshi order intent before it reaches the Kalshi paper broker." actions={<Freshness poll={poll} />} />
      <Card title="Risk limits" subtitle="Changes apply to the next intent the engine evaluates">
        <PollView<RiskResponse> poll={poll} loadingLabel="Loading risk limits…">
          {(r) => <RiskLimitsForm risk={r} onSaved={(n) => poll.mutate(() => n)} />}
        </PollView>
      </Card>
      <Card title="Current utilization">
        <PollView<RiskResponse> poll={poll}>{(r) => <Utilization risk={r} />}</PollView>
      </Card>
      <Card title="Profit sweep" subtitle="Keep winning trades' profit out of the tradeable pool, or bring some back in">
        <ProfitSweep />
      </Card>
      <div className="grid grid-2">
        <Card title="Reset paper account" className="danger-zone">
          <AccountReset />
        </Card>
        <Card title="Display & connection">
          <Preferences />
        </Card>
      </div>
    </div>
  );
}
