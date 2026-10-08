import { useEffect, useRef, useState } from "react";
import { api, API_BASE, IS_MOCK } from "../api/client";
import type {
  CredentialsResponse,
  ExposureRow,
  KalshiEnv,
  ModeResponse,
  RiskLimits,
  RiskResponse,
  StoredKeyInfo,
  TradingMode,
} from "../api/types";
import { useConfirm } from "../components/ConfirmDialog";
import { DataTable } from "../components/DataTable";
import { Icon } from "../components/Icon";
import { Badge, Card, EmptyState, Field, fieldAria, Freshness, Meter, PageHeader, PollView, Switch } from "../components/ui";
import { StrategyTag, Usd } from "../components/values";
import { useAction, usePolling } from "../lib/hooks";
import { useToast } from "../lib/toast";
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
            the equity history. Kalshi analytics start again from zero.
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

const MODE_INFO: Record<TradingMode, { title: string; body: string }> = {
  paper: { title: "Paper", body: "Simulated fills against live Kalshi books. No orders, no money." },
  demo: { title: "Demo", body: "Real orders on Kalshi's demo exchange with fake money. Needs a demo key." },
  prod: { title: "Real money", body: "Real orders on Kalshi with your money. Needs a production key." },
};

function TradingModeSwitch() {
  const poll = usePolling((signal) => api.mode({ signal }), { intervalMs: 10_000, label: "trading mode" });
  const confirm = useConfirm();
  const { busy, run } = useAction();

  const choose = async (m: ModeResponse, target: TradingMode) => {
    if (target === m.mode || busy !== null) return;
    const leavingLive = m.mode !== "paper";
    const ok = await confirm({
      title: target === "prod" ? "Switch to REAL MONEY trading?" : `Switch to ${MODE_INFO[target].title.toLowerCase()} trading?`,
      danger: target === "prod",
      requireText: target === "prod" ? "REAL MONEY" : undefined,
      confirmLabel: `Switch to ${MODE_INFO[target].title}`,
      body: (
        <>
          <p>
            The engine <strong>stops</strong>{m.engine_running ? " (it is running now)" : ""}. The {MODE_INFO[target].title.toLowerCase()} ledger takes over:
            positions, history and analytics are kept separately for each mode and come back when you switch back.
          </p>
          {target !== "paper" && (
            <p>
              A new {target} ledger starts at your Kalshi balance and copies this mode's strategy switches and risk limits. Check the Strategies page and
              the risk limits, then press Start.
            </p>
          )}
          {leavingLive && <p>Open {m.mode} positions stay on Kalshi and settle there; their P&amp;L is booked when you switch back.</p>}
          {target === "prod" && (
            <p>
              <strong>Orders will spend real money.</strong> Type REAL MONEY to confirm.
            </p>
          )}
        </>
      ),
    });
    if (!ok) return;
    const r = await run("mode", () => api.switchMode(target, target === "prod" ? "REAL MONEY" : undefined), {
      error: `Couldn't switch to ${MODE_INFO[target].title}`,
    });
    // every page, stream and cached number belongs to the old ledger: start the app over
    if (r) window.location.reload();
  };

  return (
    <PollView<ModeResponse> poll={poll} loadingLabel="Loading mode…">
      {(m) => (
        <div>
          <div className="mode-options" role="radiogroup" aria-label="Trading mode">
            {(["paper", "demo", "prod"] as const).map((opt) => {
              const needsKey = opt !== "paper" && !m.keys[opt];
              const blockedByOrders = m.mode !== "paper" && opt !== m.mode && m.open_live_orders > 0;
              const on = opt === m.mode;
              const disabled = !on && (needsKey || blockedByOrders || busy !== null);
              return (
                <button
                  key={opt}
                  role="radio"
                  aria-checked={on}
                  className={`mode-option mode-${opt}${on ? " on" : ""}`}
                  disabled={disabled}
                  aria-busy={busy === "mode"}
                  onClick={() => void choose(m, opt)}
                >
                  <span className="mode-title">
                    {opt === "prod" && <Icon name="alert" />}
                    {MODE_INFO[opt].title}
                    {on && <span className="mode-current">current</span>}
                  </span>
                  <span className="mode-body">{MODE_INFO[opt].body}</span>
                  {!on && needsKey && <span className="mode-note">Add a {opt} key below first</span>}
                  {on && opt !== "paper" && !m.ready && <span className="mode-note">Locked: {m.blocked_reason}</span>}
                </button>
              );
            })}
          </div>
          <p className="muted small">
            {m.open_live_orders > 0 && (
              <>
                {m.open_live_orders} order(s) are resting on Kalshi: cancel them (Positions &amp; Orders) before leaving {m.mode} mode.{" "}
              </>
            )}
            {m.source === "dashboard"
              ? "Chosen here; it is remembered across restarts and wins over live.enabled in config.yaml."
              : "Set by config.yaml (live.enabled / live.environment). Choosing here overrides it."}
          </p>
        </div>
      )}
    </PollView>
  );
}

const ENV_LABEL: Record<KalshiEnv, string> = { demo: "Demo (fake money)", prod: "Production (real money)" };

function KeyRow({ env, info, onRemove, busy }: { env: KalshiEnv; info: StoredKeyInfo; onRemove: () => void; busy: boolean }) {
  return (
    <>
      <dt>{ENV_LABEL[env]}</dt>
      <dd>
        {info.source === null && <span className="muted">No key</span>}
        {info.source === "config" && (
          <>
            <span className="mono">{info.api_key_id}</span> <span className="muted">· set in config.yaml or KALSHIBOT_LIVE__* env vars (change it there)</span>
          </>
        )}
        {info.source === "dashboard" && (
          <>
            <span className="mono">{info.api_key_id}</span>
            {info.fingerprint && (
              <span className="muted" title="SHA-256 of the public key: compare with the key you created on Kalshi">
                {" "}
                · fingerprint <span className="mono">{info.fingerprint}</span>
              </span>
            )}
            {info.saved_at && <span className="muted"> · saved {info.saved_at.slice(0, 16).replace("T", " ")} UTC</span>}{" "}
            <button className="btn btn-sm" onClick={onRemove} disabled={busy}>
              Remove
            </button>
          </>
        )}
      </dd>
    </>
  );
}

function ApiKeyForm({ creds, onSaved }: { creds: CredentialsResponse; onSaved: (c: CredentialsResponse) => void }) {
  const { refresh } = useStatus();
  const confirm = useConfirm();
  const { busy, run } = useAction();
  const [env, setEnv] = useState<KalshiEnv>(creds.environment);
  const [keyId, setKeyId] = useState("");
  const [pem, setPem] = useState("");
  const [fileName, setFileName] = useState<string | null>(null);
  const locked = creds.keys[env].source === "config";
  const pemLooksOk = pem.includes("PRIVATE KEY-----");
  const canSave = !locked && keyId.trim() !== "" && pemLooksOk && busy === null;

  const clear = () => {
    setKeyId("");
    setPem("");
    setFileName(null);
  };

  const loadFile = async (f: File | undefined) => {
    if (!f) return;
    if (f.size > 16_000) {
      setFileName(`${f.name}: too large to be a key file`);
      setPem("");
      return;
    }
    setPem(await f.text());
    setFileName(f.name);
  };

  const save = async () => {
    if (!canSave) return;
    if (env === "prod") {
      const ok = await confirm({
        title: "Save a production (real-money) key?",
        danger: true,
        confirmLabel: "Save production key",
        body: (
          <p>
            With live trading on in production, this key lets the bot place <strong>real orders with your money</strong>. Anyone who can open this
            dashboard can start the engine, so keep it on localhost.
          </p>
        ),
      });
      if (!ok) return;
    }
    const r = await run("save", () => api.putCredentials(env, keyId.trim(), pem), { error: "The key was not saved" });
    clear(); // the key leaves browser memory either way
    if (r) {
      onSaved(r);
      refresh();
    }
  };

  return (
    <div className="api-key-form">
      <div className="form-row">
        <Field label="Exchange" htmlFor="key-env">
          <select id="key-env" className="input" value={env} onChange={(e) => setEnv(e.target.value as KalshiEnv)}>
            <option value="demo">{ENV_LABEL.demo}</option>
            <option value="prod">{ENV_LABEL.prod}</option>
          </select>
        </Field>
        <Field label="API key ID" htmlFor="key-id" hint="Shown next to the key on Kalshi (Account → API keys).">
          <input
            id="key-id"
            className="input mono"
            autoComplete="off"
            spellCheck={false}
            value={keyId}
            disabled={locked}
            onChange={(e) => setKeyId(e.target.value)}
          />
        </Field>
      </div>
      <Field label="Private key" htmlFor="key-file" hint="The .pem file Kalshi downloaded when you created the key, or paste its text below.">
        <input id="key-file" className="input" type="file" accept=".pem,.key,.txt" disabled={locked} onChange={(e) => void loadFile(e.target.files?.[0])} />
      </Field>
      <textarea
        className="input mono secret-input"
        aria-label="Private key (PEM text)"
        rows={3}
        autoComplete="off"
        spellCheck={false}
        placeholder="…or paste -----BEGIN PRIVATE KEY----- …"
        value={fileName ? "" : pem}
        disabled={locked || fileName !== null}
        onChange={(e) => setPem(e.target.value)}
      />
      {fileName && (
        <p className="muted">
          Loaded <span className="mono">{fileName}</span>
          {pemLooksOk ? "" : " — this does not look like a PEM private key"}{" "}
          <button className="btn btn-sm" onClick={clear}>
            Clear
          </button>
        </p>
      )}
      {locked && <p className="muted">The {env} key comes from config.yaml / env vars, which win over the dashboard.</p>}
      <button className="btn btn-primary" onClick={save} disabled={!canSave} aria-busy={busy === "save"}>
        <Icon name="check" /> Verify with Kalshi &amp; save
      </button>
    </div>
  );
}

function ApiKeys() {
  const poll = usePolling((signal) => api.credentials({ signal }), { intervalMs: 30_000, label: "API keys" });
  const { refresh } = useStatus();
  const confirm = useConfirm();
  const { busy, run } = useAction();
  const toast = useToast();
  const remove = async (env: KalshiEnv) => {
    const ok = await confirm({
      title: `Remove the ${env} key from this bot?`,
      danger: true,
      confirmLabel: "Remove key",
      body: <p>The key file entry is deleted. If live trading uses this key it locks until you add another. The key stays valid on Kalshi: revoke it there if it may have leaked.</p>,
    });
    if (!ok) return;
    const r = await run("remove", () => api.deleteCredentials(env), { success: `${env} key removed`, error: "Couldn't remove the key" });
    if (r) {
      poll.mutate(() => r);
      refresh();
    }
  };
  return (
    <PollView<CredentialsResponse> poll={poll} loadingLabel="Loading keys…">
      {(c) => (
        <div className="reset">
          <dl className="kv">
            <KeyRow env="demo" info={c.keys.demo} busy={busy !== null} onRemove={() => void remove("demo")} />
            <KeyRow env="prod" info={c.keys.prod} busy={busy !== null} onRemove={() => void remove("prod")} />
            <dt>Live trading</dt>
            <dd>
              {c.live_enabled ? (
                c.ready ? (
                  <Badge tone="bad" icon="alert">
                    On · {c.environment} · ready
                  </Badge>
                ) : (
                  <>
                    <Badge tone="warn" icon="alert">
                      On · {c.environment} · locked
                    </Badge>{" "}
                    <span className="muted">{c.blocked_reason}</span>
                  </>
                )
              ) : (
                <span className="muted">Off (paper). Keys can be added and verified now; switch the trading mode above to use them.</span>
              )}
            </dd>
          </dl>
          <ApiKeyForm
            creds={c}
            onSaved={(r) => {
              poll.mutate(() => r);
              toast.success(
                r.verified_balance !== null ? `Key verified with Kalshi (balance ${fmtUsd(r.verified_balance)}) and saved` : "Key saved",
                r.activated ? { message: "Live trading is unlocked. Start the engine when you're ready." } : undefined,
              );
            }}
          />
          <p className="muted small">
            How keys are kept: they are sent once over this connection, checked against Kalshi, then written to{" "}
            <span className="mono">{c.secrets_path ?? "the key file"}</span>, which only the bot's user can read. They are never shown again, logged, or sent
            back to any browser, and that file stays out of git and the Docker image. The dashboard has no login, so anyone who can open it can trade with
            these keys: keep it bound to localhost.
          </p>
        </div>
      )}
    </PollView>
  );
}

function LiveTrading() {
  const { status, refresh } = useStatus();
  const { busy, run } = useAction();
  const confirm = useConfirm();
  const live = status?.live;
  if (!live) return null;
  const x = live.exchange;
  const reconcile = async () => {
    if (await run("reconcile", () => api.liveReconcile(), { success: "Compared the ledger with Kalshi", error: "Couldn't reach Kalshi" })) refresh();
  };
  const sync = async () => {
    const ok = await confirm({
      title: "Book the balance difference as a transfer?",
      confirmLabel: "Sync cash",
      body: (
        <p>
          The difference between the Kalshi balance ({fmtUsd(x.balance ?? undefined)}) and the bot's books ({fmtPnl(x.cash_drift ?? 0)}) is booked as a
          deposit or withdrawal, so P&amp;L is unchanged. A withdrawal comes out of the reserved (swept) profit first, then the trading cash. Use this
          after you move money in or out of Kalshi.
        </p>
      ),
    });
    if (!ok) return;
    const r = await run("sync", () => api.liveSyncCash(), { error: "Couldn't sync cash" });
    if (r) refresh();
  };
  return (
    <div className="reset">
      <dl className="kv">
        <dt>Environment</dt>
        <dd>
          <Badge tone={live.environment === "prod" ? "bad" : "warn"} icon="alert">
            {live.environment === "prod" ? "Production: real money" : "Demo exchange: fake money"}
          </Badge>
        </dd>
        <dt>Per-order caps</dt>
        <dd className="num">
          {fmtInt(live.max_order_contracts)} contracts · {fmtUsd(live.max_order_cost)} · taker orders sent as {humanize(live.taker_time_in_force)}
        </dd>
        <dt>Kalshi balance</dt>
        <dd className="num">{fmtUsd(x.balance ?? undefined)}</dd>
        <dt>Ledger cash</dt>
        <dd className="num">{fmtUsd(x.ledger_cash ?? undefined)}</dd>
        <dt>Difference</dt>
        <dd className="num">{x.cash_drift === null ? "—" : fmtPnl(x.cash_drift)}</dd>
        <dt>Positions</dt>
        <dd>
          {x.position_mismatches.length === 0
            ? "Match Kalshi"
            : x.position_mismatches.map((m) => `${m.ticker}: ledger ${m.ledger}, Kalshi ${m.exchange}`).join("; ")}
        </dd>
        <dt>Last checked</dt>
        <dd>{x.checked_at ?? "never"}{x.error ? ` · error: ${x.error}` : ""}</dd>
      </dl>
      <div className="form-row">
        <button className="btn" onClick={reconcile} disabled={busy !== null} aria-busy={busy === "reconcile"}>
          <Icon name="check" /> Compare now
        </button>
        <button className="btn" onClick={sync} disabled={busy !== null || x.balance === null} aria-busy={busy === "sync"}>
          <Icon name="alert" /> Sync cash to Kalshi…
        </button>
      </div>
    </div>
  );
}

function Preferences() {
  const stream = useStreamInfo();
  const { status } = useStatus();
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
        {status?.mode === "live" ? (
          <>
            <Badge tone="bad" icon="alert">
              Live trading
            </Badge>{" "}
            <span className="muted">Orders go to Kalshi ({status.live?.environment === "prod" ? "real money" : "demo exchange"}).</span>
          </>
        ) : (
          <>
            <Badge tone="warn" icon="shield">
              Paper trading
            </Badge>{" "}
            <span className="muted">No real orders. Set live.enabled in config.yaml to trade for real.</span>
          </>
        )}
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
  const keysCard = (
    <Card title="Kalshi API keys" subtitle="Needed for live trading. Write-only: a saved key is never shown again">
      <ApiKeys />
    </Card>
  );
  return (
    <div className="page">
      <PageHeader title="Settings" subtitle={`Risk limits are enforced on every order intent before it reaches the ${status?.mode === "live" ? "live Kalshi" : "paper"} broker.`} actions={<Freshness poll={poll} />} />
      <Card title="Trading mode" subtitle="Paper, Kalshi demo, or real money. Switching stops the engine">
        <TradingModeSwitch />
      </Card>
      {keysCard}
      <Card title="Risk limits" subtitle="Changes apply to the next intent the engine evaluates">
        <PollView<RiskResponse> poll={poll} loadingLabel="Loading risk limits…">
          {(r) => <RiskLimitsForm risk={r} onSaved={(n) => poll.mutate(() => n)} />}
        </PollView>
      </Card>
      <Card title="Current utilization">
        <PollView<RiskResponse> poll={poll}>{(r) => <Utilization risk={r} />}</PollView>
      </Card>
      {status?.mode === "live" && (
        <Card title="Live trading" subtitle="The bot's ledger compared with your Kalshi account (checked every snapshot)">
          <LiveTrading />
        </Card>
      )}
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
