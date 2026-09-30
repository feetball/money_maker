/**
 * Form generated from a strategy's `param_schema` ({name: {type, min, max, help}}).
 * Keeps the raw text of each input so partial numbers ("0.") can be typed, and
 * reports parsed values + per-field errors to the parent on every change.
 */
import { useState } from "react";
import type { ParamSpec, ParamValue } from "../api/types";
import { humanize } from "../lib/format";
import { Switch } from "./ui";

type Kind = "int" | "float" | "bool" | "enum" | "str" | "json";

export function paramKind(spec: ParamSpec): Kind {
  if (spec.enum && spec.enum.length) return "enum";
  const t = spec.type.toLowerCase();
  if (t === "int" || t === "integer") return "int";
  if (t === "float" || t === "number" || t === "decimal") return "float";
  if (t === "bool" || t === "boolean") return "bool";
  if (t === "str" || t === "string" || t === "text") return "str";
  return "json";
}

function toRaw(spec: ParamSpec, v: ParamValue | undefined): string {
  const k = paramKind(spec);
  if (v === undefined || v === null) return "";
  if (k === "json") return JSON.stringify(v);
  if (k === "enum") return JSON.stringify(v);
  return String(v);
}

export function validateParam(spec: ParamSpec, v: ParamValue): string | null {
  const k = paramKind(spec);
  if (k === "int" || k === "float") {
    if (typeof v !== "number" || !Number.isFinite(v)) return "Enter a number";
    if (k === "int" && !Number.isInteger(v)) return "Must be a whole number";
    if (spec.min !== null && spec.min !== undefined && v < spec.min) return `Must be ≥ ${spec.min}`;
    if (spec.max !== null && spec.max !== undefined && v > spec.max) return `Must be ≤ ${spec.max}`;
  }
  return null;
}

function parseRaw(spec: ParamSpec, raw: string): { value: ParamValue; error: string | null } {
  const k = paramKind(spec);
  if (k === "int" || k === "float") {
    if (raw.trim() === "") return { value: null, error: "Required" };
    const n = Number(raw);
    const value = Number.isFinite(n) ? n : null;
    return { value, error: value === null ? "Enter a number" : validateParam(spec, value) };
  }
  if (k === "json" || k === "enum") {
    try {
      return { value: JSON.parse(raw) as ParamValue, error: null };
    } catch {
      return { value: null, error: "Invalid JSON" };
    }
  }
  return { value: raw, error: null };
}

function rangeHint(spec: ParamSpec): string | null {
  const hasMin = spec.min !== null && spec.min !== undefined;
  const hasMax = spec.max !== null && spec.max !== undefined;
  if (hasMin && hasMax) return `${spec.min} – ${spec.max}`;
  if (hasMin) return `≥ ${spec.min}`;
  if (hasMax) return `≤ ${spec.max}`;
  return null;
}

export type ParamErrors = Record<string, string | null>;

export function ParamEditor({
  schema,
  values,
  onChange,
  disabled,
  idPrefix,
}: {
  schema: Record<string, ParamSpec>;
  values: Record<string, ParamValue>;
  onChange: (values: Record<string, ParamValue>, errors: ParamErrors) => void;
  disabled?: boolean;
  idPrefix: string;
}) {
  const keys = Object.keys(schema);
  const [raw, setRaw] = useState<Record<string, string>>(() => Object.fromEntries(keys.map((k) => [k, toRaw(schema[k]!, values[k])])));
  const [errors, setErrors] = useState<ParamErrors>({});

  const update = (key: string, next: { raw?: string; value?: ParamValue }) => {
    const spec = schema[key];
    if (!spec) return;
    let value: ParamValue;
    let error: string | null = null;
    if (next.raw !== undefined) {
      setRaw((r) => ({ ...r, [key]: next.raw ?? "" }));
      const parsed = parseRaw(spec, next.raw);
      value = parsed.value;
      error = parsed.error;
    } else {
      value = next.value ?? null;
    }
    const nextErrors = { ...errors, [key]: error };
    setErrors(nextErrors);
    onChange({ ...values, [key]: value }, nextErrors);
  };

  if (keys.length === 0) return <p className="muted">This strategy has no tunable parameters.</p>;

  return (
    <div className="param-grid">
      {keys.map((key) => {
        const spec = schema[key]!;
        const kind = paramKind(spec);
        const id = `${idPrefix}-${key}`;
        const err = errors[key];
        const hint = [spec.help, rangeHint(spec)].filter(Boolean).join(" · ");
        const v = values[key];
        return (
          <div className={`field${err ? " has-error" : ""}`} key={key}>
            <label className="field-label" htmlFor={id}>
              {spec.title ?? humanize(key)} <span className="field-key">{key}</span>
            </label>
            {kind === "bool" ? (
              <div className="field-inline">
                <Switch checked={v === true} onChange={(b) => update(key, { value: b })} label={spec.title ?? humanize(key)} disabled={disabled} showLabel />
              </div>
            ) : kind === "enum" ? (
              <select
                id={id}
                className="input"
                value={raw[key] ?? ""}
                disabled={disabled}
                aria-invalid={err ? true : undefined}
                aria-describedby={`${id}-hint`}
                onChange={(e) => update(key, { raw: e.target.value })}
              >
                {!(spec.enum ?? []).some((o) => JSON.stringify(o) === raw[key]) && <option value={raw[key] ?? ""}>{String(v ?? "—")}</option>}
                {(spec.enum ?? []).map((o) => (
                  <option key={JSON.stringify(o)} value={JSON.stringify(o)}>
                    {String(o)}
                  </option>
                ))}
              </select>
            ) : kind === "json" ? (
              <textarea
                id={id}
                className="input mono"
                rows={2}
                value={raw[key] ?? ""}
                disabled={disabled}
                spellCheck={false}
                aria-invalid={err ? true : undefined}
                aria-describedby={`${id}-hint`}
                onChange={(e) => update(key, { raw: e.target.value })}
              />
            ) : (
              <input
                id={id}
                className="input num-input"
                type={kind === "str" ? "text" : "number"}
                inputMode={kind === "int" ? "numeric" : kind === "float" ? "decimal" : undefined}
                step={kind === "int" ? (spec.step ?? 1) : (spec.step ?? "any")}
                min={spec.min ?? undefined}
                max={spec.max ?? undefined}
                value={raw[key] ?? ""}
                disabled={disabled}
                aria-invalid={err ? true : undefined}
                aria-describedby={`${id}-hint`}
                onChange={(e) => update(key, { raw: e.target.value })}
              />
            )}
            <div id={`${id}-hint`} className={err ? "field-error" : "field-hint"}>
              {err ?? (hint || " ")}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export const hasErrors = (e: ParamErrors) => Object.values(e).some(Boolean);
