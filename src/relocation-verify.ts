/**
 * Read-only Mimir relocation verification hooks (Grimnir ADR-007).
 *
 * `preflight` and `verify` never change service, storage, tunnel, sync,
 * backup, or Heimdall state. Every invocation is bound to one lifecycle
 * attempt (attempt/plan/digest/revision/observation/action/deadline/
 * idempotency key), emits a machine-readable result on stdout whose
 * `hook_result` conforms to the vendored normative v1 schema, and records
 * that result under its idempotency key so a retry replays the recorded
 * result instead of re-observing.
 *
 * Evidence is consumed as closed typed JSON receipts supplied by the private
 * owner overlay. A receipt must be a regular non-symlink file owned by the
 * invoking user with restrictive permissions, size-bounded, well-formed, and
 * explicitly fresh via exact UTC `observed_at`/`valid_until`. Failures are
 * reported as short reason tokens against the logical evidence variable name;
 * receipt contents and filesystem paths are never echoed.
 */
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { lstatSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import {
  definitionErrors,
  isExactUtc,
  isPlainObject,
  loadNormativeSchema,
  type JsonValue,
} from "./node-substrate.js";

export const READ_ONLY_HOOKS = ["preflight", "verify"] as const;
export const MUTATING_HOOKS = ["drain", "compensate", "rollback"] as const;
export type ReadOnlyHook = (typeof READ_ONLY_HOOKS)[number];

export const ID_PATTERN = /^[a-z][a-z0-9-]{2,62}$/;
export const DIGEST_PATTERN = /^sha256:[a-f0-9]{64}$/;
export const RECEIPT_MAX_BYTES = 4096;
export const RESULT_RECORD_MAX_BYTES = 16384;
const HOOK_RESULT_DEF = "#/$defs/lifecycle-result/properties/hook_results/items";

export interface Binding {
  attempt_id: string;
  plan_id: string;
  plan_digest: string;
  desired_revision: string;
  observation_evidence_id: string;
  action: "preflight" | "relocate";
  deadline: string;
  idempotency_key: string;
}

export const BINDING_VARS: ReadonlyArray<{
  field: keyof Binding;
  variable: string;
  format: "id" | "digest" | "action" | "utc";
}> = [
  { field: "attempt_id", variable: "MIMIR_RELOCATION_ATTEMPT_ID", format: "id" },
  { field: "plan_id", variable: "MIMIR_RELOCATION_PLAN_ID", format: "id" },
  { field: "plan_digest", variable: "MIMIR_RELOCATION_PLAN_DIGEST", format: "digest" },
  { field: "desired_revision", variable: "MIMIR_RELOCATION_DESIRED_REVISION", format: "digest" },
  {
    field: "observation_evidence_id",
    variable: "MIMIR_RELOCATION_OBSERVATION_EVIDENCE_ID",
    format: "id",
  },
  { field: "action", variable: "MIMIR_RELOCATION_ACTION", format: "action" },
  { field: "deadline", variable: "MIMIR_RELOCATION_DEADLINE", format: "utc" },
  { field: "idempotency_key", variable: "MIMIR_RELOCATION_IDEMPOTENCY_KEY", format: "id" },
];

function bindingValueValid(format: string, value: string): boolean {
  switch (format) {
    case "id":
      return ID_PATTERN.test(value);
    case "digest":
      return DIGEST_PATTERN.test(value);
    case "action":
      return value === "preflight" || value === "relocate";
    case "utc":
      return isExactUtc(value);
    default:
      return false;
  }
}

/** Parse the ADR-007 invocation binding from the environment. */
export function readBinding(
  env: NodeJS.ProcessEnv,
): { binding: Binding; problems?: undefined } | { binding?: undefined; problems: string[] } {
  const problems: string[] = [];
  const partial: Record<string, string> = {};
  for (const { field, variable, format } of BINDING_VARS) {
    const value = env[variable];
    if (value === undefined || value === "") {
      problems.push(`${variable} is required`);
      continue;
    }
    if (!bindingValueValid(format, value)) {
      problems.push(`${variable} is not a valid ${format} value`);
      continue;
    }
    partial[field] = value;
  }
  if (problems.length > 0) return { problems };
  return { binding: partial as unknown as Binding };
}

// ---------------------------------------------------------------------------
// Typed evidence receipts
// ---------------------------------------------------------------------------

export interface EvidenceSpec {
  check: string;
  variable: string;
  status: string;
}

/**
 * Distinct evidence classes. T7 copy completion and T7 integrity are
 * deliberately separate receipts: a finished copy is not integrity proof.
 */
export const EVIDENCE_CHECKS: ReadonlyArray<EvidenceSpec> = [
  { check: "tunnel", variable: "MIMIR_RELOCATION_TUNNEL_EVIDENCE", status: "tunnel-v1:connected" },
  { check: "sync", variable: "MIMIR_RELOCATION_SYNC_EVIDENCE", status: "sync-v1:complete" },
  { check: "t7-copy", variable: "MIMIR_RELOCATION_T7_COPY_EVIDENCE", status: "local-copy-v1:complete" },
  {
    check: "t7-integrity",
    variable: "MIMIR_RELOCATION_T7_INTEGRITY_EVIDENCE",
    status: "local-copy-integrity-v1:verified",
  },
  { check: "offsite", variable: "MIMIR_RELOCATION_OFFSITE_EVIDENCE", status: "offsite-v1:complete" },
  { check: "heimdall", variable: "MIMIR_RELOCATION_HEIMDALL_EVIDENCE", status: "heimdall-v1:fresh" },
  {
    check: "restore",
    variable: "MIMIR_RELOCATION_RESTORE_EVIDENCE",
    status: "restore-v1:representative-ok",
  },
  {
    check: "deployment-marker",
    variable: "MIMIR_RELOCATION_DEPLOYMENT_EVIDENCE",
    status: "deployment-marker-v1:recoverable",
  },
];

const RECEIPT_KEYS = ["kind", "schema_version", "check", "status", "observed_at", "valid_until"];

export type ReceiptResult = { ok: true } | { ok: false; reason: string };

/**
 * Safely read one evidence receipt. Reason tokens are constant strings —
 * they never include the path, the content, or parser detail.
 */
export function readReceipt(
  path: string,
  spec: Pick<EvidenceSpec, "check" | "status">,
  nowMs: number,
  expectedUid: number | undefined = process.getuid?.(),
): ReceiptResult {
  if (expectedUid === undefined) return { ok: false, reason: "owner-check-unavailable" };
  let stats;
  try {
    stats = lstatSync(path);
  } catch {
    return { ok: false, reason: "unreadable" };
  }
  if (stats.isSymbolicLink()) return { ok: false, reason: "symlink" };
  if (!stats.isFile()) return { ok: false, reason: "not-a-regular-file" };
  if (stats.uid !== expectedUid) return { ok: false, reason: "wrong-owner" };
  if ((stats.mode & 0o077) !== 0) return { ok: false, reason: "permissions-too-open" };
  if (stats.size > RECEIPT_MAX_BYTES) return { ok: false, reason: "oversize" };
  let parsed: JsonValue;
  try {
    parsed = JSON.parse(readFileSync(path, "utf8")) as JsonValue;
  } catch {
    return { ok: false, reason: "malformed" };
  }
  if (!isPlainObject(parsed)) return { ok: false, reason: "malformed" };
  const keys = Object.keys(parsed).sort();
  if (keys.join(",") !== [...RECEIPT_KEYS].sort().join(",")) {
    return { ok: false, reason: "not-a-closed-receipt" };
  }
  if (parsed.kind !== "mimir-relocation-evidence") return { ok: false, reason: "wrong-kind" };
  if (parsed.schema_version !== "v1") return { ok: false, reason: "unsupported-version" };
  if (parsed.check !== spec.check) return { ok: false, reason: "wrong-check" };
  if (parsed.status !== spec.status) return { ok: false, reason: "wrong-status" };
  const observedAt = parsed.observed_at;
  const validUntil = parsed.valid_until;
  if (typeof observedAt !== "string" || !isExactUtc(observedAt)) {
    return { ok: false, reason: "invalid-observed-at" };
  }
  if (typeof validUntil !== "string" || !isExactUtc(validUntil)) {
    return { ok: false, reason: "invalid-valid-until" };
  }
  const observedMs = Date.parse(observedAt);
  const validMs = Date.parse(validUntil);
  if (validMs <= observedMs) return { ok: false, reason: "invalid-validity-window" };
  if (observedMs > nowMs) return { ok: false, reason: "future" };
  if (validMs <= nowMs) return { ok: false, reason: "stale" };
  return { ok: true };
}

// ---------------------------------------------------------------------------
// Hook execution
// ---------------------------------------------------------------------------

export type CheckOutcome = "success" | "failed" | "timed_out" | "unavailable";

export interface CheckResult {
  check: string;
  outcome: CheckOutcome;
  reason?: string;
}

export interface HookRunResult {
  output: Record<string, JsonValue>;
  diagnostics: string[];
  exitCode: number;
}

const SYSTEMD_CHECKS: ReadonlyArray<{ check: string; unit: string }> = [
  { check: "service", unit: "mimir.service" },
  { check: "timer", unit: "mimir-offsite.timer" },
];

function systemdCheck(unit: string, timeoutMs: number): CheckOutcome {
  const result = spawnSync("systemctl", ["is-active", "--quiet", unit], {
    timeout: timeoutMs,
    stdio: "ignore",
  });
  if (result.error !== undefined) {
    return (result.error as NodeJS.ErrnoException).code === "ETIMEDOUT" ? "timed_out" : "unavailable";
  }
  if (result.signal !== null) return "timed_out";
  return result.status === 0 ? "success" : "failed";
}

function toExactUtc(ms: number): string {
  return new Date(Math.floor(ms / 1000) * 1000).toISOString().replace(/\.000Z$/, "Z");
}

function deterministicResultId(hook: string, binding: Binding): string {
  const material = [
    hook,
    binding.attempt_id,
    binding.plan_id,
    binding.plan_digest,
    binding.desired_revision,
    binding.observation_evidence_id,
    binding.action,
    binding.deadline,
    binding.idempotency_key,
  ].join("\n");
  return `r-${createHash("sha256").update(material).digest("hex").slice(0, 24)}`;
}

function errorOutput(error: string, problems?: string[]): Record<string, JsonValue> {
  const output: Record<string, JsonValue> = {
    kind: "mimir-relocation-hook-error",
    schema_version: "v1",
    error,
  };
  if (problems !== undefined) output.problems = problems;
  return output;
}

function bindingMatchesRecord(
  hook: string,
  binding: Binding,
  record: JsonValue,
): record is Record<string, JsonValue> {
  if (!isPlainObject(record)) return false;
  const hookResult = record.hook_result;
  if (!isPlainObject(hookResult)) return false;
  if (hookResult.hook !== hook) return false;
  return (Object.keys(binding) as Array<keyof Binding>).every(
    (field) => hookResult[field] === binding[field],
  );
}

export interface HookOptions {
  nowMs?: () => number;
  uid?: number;
}

/** Run a read-only hook. Never throws; failures become fail-closed results. */
export function runHook(
  hook: ReadOnlyHook,
  env: NodeJS.ProcessEnv,
  options: HookOptions = {},
): HookRunResult {
  const nowFn = options.nowMs ?? (() => Date.now());
  const diagnostics: string[] = [];

  const bound = readBinding(env);
  if (bound.problems !== undefined) {
    for (const problem of bound.problems) diagnostics.push(`BLOCKED: ${problem}`);
    return { output: errorOutput("invalid_binding", bound.problems), diagnostics, exitCode: 2 };
  }
  const binding = bound.binding;

  const resultDir = env.MIMIR_RELOCATION_RESULT_DIR;
  if (resultDir === undefined || resultDir === "") {
    const problem = "MIMIR_RELOCATION_RESULT_DIR is required for idempotent result records";
    diagnostics.push(`BLOCKED: ${problem}`);
    return { output: errorOutput("invalid_binding", [problem]), diagnostics, exitCode: 2 };
  }

  const timeoutRaw = env.MIMIR_RELOCATION_TIMEOUT_SECONDS ?? "60";
  if (!/^[1-9][0-9]*$/.test(timeoutRaw)) {
    const problem = "MIMIR_RELOCATION_TIMEOUT_SECONDS must be a positive integer";
    diagnostics.push(`BLOCKED: ${problem}`);
    return { output: errorOutput("invalid_binding", [problem]), diagnostics, exitCode: 2 };
  }
  const timeoutMs = Number(timeoutRaw) * 1000;

  let schema;
  try {
    schema = loadNormativeSchema();
  } catch (error) {
    const message = error instanceof Error ? error.message : "schema load failed";
    diagnostics.push(`BLOCKED: normative schema unavailable: ${message}`);
    return { output: errorOutput("schema_unavailable"), diagnostics, exitCode: 2 };
  }

  const recordPath = join(resultDir, `${hook}-${binding.idempotency_key}.json`);
  const replay = tryReplay(hook, binding, recordPath, diagnostics, options.uid);
  if (replay !== undefined) return replay;

  const startMs = nowFn();
  const deadlineMs = Date.parse(binding.deadline);
  const checks: CheckResult[] = [];
  let outcome: "success" | "failed" | "timed_out";

  if (startMs >= deadlineMs) {
    outcome = "timed_out";
    diagnostics.push("BLOCKED: invocation deadline has already passed; no checks were run");
  } else {
    for (const { check, unit } of SYSTEMD_CHECKS) {
      const checkOutcome = systemdCheck(unit, timeoutMs);
      checks.push(
        checkOutcome === "success" ? { check, outcome: checkOutcome } : { check, outcome: checkOutcome, reason: `${unit} is not active` },
      );
      diagnostics.push(
        checkOutcome === "success" ? `PASS: ${check} (${unit})` : `BLOCKED: ${check} (${unit}) ${checkOutcome}`,
      );
    }
    for (const spec of EVIDENCE_CHECKS) {
      const path = env[spec.variable];
      let result: ReceiptResult;
      if (path === undefined || path === "") {
        result = { ok: false, reason: "not-provided" };
      } else {
        result = readReceipt(path, spec, nowFn(), options.uid ?? process.getuid?.());
      }
      if (result.ok) {
        checks.push({ check: spec.check, outcome: "success" });
        diagnostics.push(`PASS: ${spec.check} receipt (${spec.variable})`);
      } else {
        checks.push({ check: spec.check, outcome: "failed", reason: result.reason });
        diagnostics.push(`BLOCKED: ${spec.check} receipt ${result.reason} (${spec.variable})`);
      }
    }
    const endMs = nowFn();
    if (endMs >= deadlineMs) {
      outcome = "timed_out";
      diagnostics.push("BLOCKED: invocation deadline passed while checks were running");
    } else if (checks.some((check) => check.outcome === "timed_out")) {
      outcome = "timed_out";
    } else if (checks.every((check) => check.outcome === "success")) {
      outcome = "success";
    } else {
      outcome = "failed";
    }
  }

  const hookResult: Record<string, JsonValue> = {
    result_id: deterministicResultId(hook, binding),
    hook,
    attempt_id: binding.attempt_id,
    plan_id: binding.plan_id,
    plan_digest: binding.plan_digest,
    desired_revision: binding.desired_revision,
    observation_evidence_id: binding.observation_evidence_id,
    action: binding.action,
    deadline: binding.deadline,
    idempotency_key: binding.idempotency_key,
    outcome,
  };
  const schemaProblems = definitionErrors(schema, HOOK_RESULT_DEF, hookResult);
  if (schemaProblems.length > 0) {
    diagnostics.push("BLOCKED: internal error: emitted hook_result violates the normative schema");
    return { output: errorOutput("internal_schema_violation"), diagnostics, exitCode: 2 };
  }

  const output: Record<string, JsonValue> = {
    kind: "mimir-relocation-hook-result",
    schema_version: "v1",
    hook_result: hookResult,
    checks: checks as unknown as JsonValue,
    created_at: toExactUtc(nowFn()),
  };

  try {
    mkdirSync(resultDir, { recursive: true, mode: 0o700 });
    writeFileSync(recordPath, `${JSON.stringify(output, null, 2)}\n`, {
      flag: "wx",
      mode: 0o600,
    });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "EEXIST") {
      const replayAfterRace = tryReplay(hook, binding, recordPath, diagnostics, options.uid);
      if (replayAfterRace !== undefined) return replayAfterRace;
      diagnostics.push("BLOCKED: idempotency record conflict");
      return { output: errorOutput("idempotency_conflict"), diagnostics, exitCode: 2 };
    }
    diagnostics.push("BLOCKED: could not record the hook result for idempotent replay");
    return { output: errorOutput("result_record_unwritable"), diagnostics, exitCode: 2 };
  }

  diagnostics.push(
    outcome === "success"
      ? `PASS: Mimir ${hook} verification evidence is complete`
      : `BLOCKED: Mimir ${hook} is ${outcome}`,
  );
  return { output, diagnostics, exitCode: outcome === "success" ? 0 : 1 };
}

/**
 * Return the recorded result for this hook + idempotency key if one exists.
 * A record bound to different attempt/plan bindings is a fail-closed
 * conflict, not a replay.
 */
function tryReplay(
  hook: string,
  binding: Binding,
  recordPath: string,
  diagnostics: string[],
  uid: number | undefined,
): HookRunResult | undefined {
  let stats;
  try {
    stats = lstatSync(recordPath);
  } catch {
    return undefined;
  }
  const expectedUid = uid ?? process.getuid?.();
  if (
    stats.isSymbolicLink() ||
    !stats.isFile() ||
    expectedUid === undefined ||
    stats.uid !== expectedUid ||
    (stats.mode & 0o077) !== 0 ||
    stats.size > RESULT_RECORD_MAX_BYTES
  ) {
    diagnostics.push("BLOCKED: recorded idempotency result is not a safe regular file");
    return { output: errorOutput("result_record_invalid"), diagnostics, exitCode: 2 };
  }
  let record: JsonValue;
  try {
    record = JSON.parse(readFileSync(recordPath, "utf8")) as JsonValue;
  } catch {
    diagnostics.push("BLOCKED: recorded idempotency result is unreadable");
    return { output: errorOutput("result_record_invalid"), diagnostics, exitCode: 2 };
  }
  if (!bindingMatchesRecord(hook, binding, record)) {
    diagnostics.push(
      "BLOCKED: idempotency key already used by a different attempt binding; refusing replay",
    );
    return { output: errorOutput("binding_conflict"), diagnostics, exitCode: 2 };
  }
  const hookResult = record.hook_result as Record<string, JsonValue>;
  diagnostics.push(`REPLAY: returning the recorded ${hook} result for this idempotency key`);
  return {
    output: record,
    diagnostics,
    exitCode: hookResult.outcome === "success" ? 0 : 1,
  };
}
