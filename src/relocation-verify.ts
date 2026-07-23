/**
 * Read-only Mimir relocation verification hooks (Grimnir ADR-007).
 *
 * Evidence and replay records are deliberately closed, owner-only local
 * artifacts.  They are never trusted merely because a path exists: reads use
 * a no-follow descriptor and re-check the opened inode before parsing.
 */
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  closeSync,
  constants,
  fstatSync,
  lstatSync,
  mkdirSync,
  openSync,
  readSync,
  writeFileSync,
} from "node:fs";
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
export const MAX_TIMEOUT_SECONDS = 300;
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

const RECEIPT_BINDING_FIELDS: ReadonlyArray<keyof Pick<
  Binding,
  | "attempt_id"
  | "plan_id"
  | "plan_digest"
  | "desired_revision"
  | "observation_evidence_id"
  | "action"
>> = [
  "attempt_id",
  "plan_id",
  "plan_digest",
  "desired_revision",
  "observation_evidence_id",
  "action",
];
type ReceiptBinding = Pick<Binding, (typeof RECEIPT_BINDING_FIELDS)[number]>;

export const BINDING_VARS: ReadonlyArray<{
  field: keyof Binding;
  variable: string;
  format: "id" | "digest" | "action" | "utc";
}> = [
  { field: "attempt_id", variable: "MIMIR_RELOCATION_ATTEMPT_ID", format: "id" },
  { field: "plan_id", variable: "MIMIR_RELOCATION_PLAN_ID", format: "id" },
  { field: "plan_digest", variable: "MIMIR_RELOCATION_PLAN_DIGEST", format: "digest" },
  { field: "desired_revision", variable: "MIMIR_RELOCATION_DESIRED_REVISION", format: "digest" },
  { field: "observation_evidence_id", variable: "MIMIR_RELOCATION_OBSERVATION_EVIDENCE_ID", format: "id" },
  { field: "action", variable: "MIMIR_RELOCATION_ACTION", format: "action" },
  { field: "deadline", variable: "MIMIR_RELOCATION_DEADLINE", format: "utc" },
  { field: "idempotency_key", variable: "MIMIR_RELOCATION_IDEMPOTENCY_KEY", format: "id" },
];

function bindingValueValid(format: string, value: string): boolean {
  switch (format) {
    case "id": return ID_PATTERN.test(value);
    case "digest": return DIGEST_PATTERN.test(value);
    case "action": return value === "preflight" || value === "relocate";
    case "utc": return isExactUtc(value);
    default: return false;
  }
}

export function readBinding(
  env: NodeJS.ProcessEnv,
): { binding: Binding; problems?: undefined } | { binding?: undefined; problems: string[] } {
  const problems: string[] = [];
  const partial: Record<string, string> = {};
  for (const { field, variable, format } of BINDING_VARS) {
    const value = env[variable];
    if (value === undefined || value === "") problems.push(`${variable} is required`);
    else if (!bindingValueValid(format, value)) problems.push(`${variable} is not a valid ${format} value`);
    else partial[field] = value;
  }
  return problems.length > 0 ? { problems } : { binding: partial as unknown as Binding };
}

export interface EvidenceSpec { check: string; variable: string; status: string; }
export const EVIDENCE_CHECKS: ReadonlyArray<EvidenceSpec> = [
  { check: "tunnel", variable: "MIMIR_RELOCATION_TUNNEL_EVIDENCE", status: "tunnel-v1:connected" },
  { check: "sync", variable: "MIMIR_RELOCATION_SYNC_EVIDENCE", status: "sync-v1:complete" },
  { check: "t7-copy", variable: "MIMIR_RELOCATION_T7_COPY_EVIDENCE", status: "local-copy-v1:complete" },
  { check: "t7-integrity", variable: "MIMIR_RELOCATION_T7_INTEGRITY_EVIDENCE", status: "local-copy-integrity-v1:verified" },
  { check: "offsite", variable: "MIMIR_RELOCATION_OFFSITE_EVIDENCE", status: "offsite-v1:complete" },
  { check: "heimdall", variable: "MIMIR_RELOCATION_HEIMDALL_EVIDENCE", status: "heimdall-v1:fresh" },
  { check: "restore", variable: "MIMIR_RELOCATION_RESTORE_EVIDENCE", status: "restore-v1:representative-ok" },
  { check: "deployment-marker", variable: "MIMIR_RELOCATION_DEPLOYMENT_EVIDENCE", status: "deployment-marker-v1:recoverable" },
];

const RECEIPT_KEYS = [
  "kind", "schema_version", "check", "status", ...RECEIPT_BINDING_FIELDS, "observed_at", "valid_until",
].sort();
const OUTPUT_KEYS = ["kind", "schema_version", "hook_result", "checks", "created_at", "content_digest"].sort();
const CHECK_NAMES = ["service", "timer", ...EVIDENCE_CHECKS.map((spec) => spec.check)];
const RECEIPT_REASONS = new Set([
  "not-provided", "unreadable", "symlink", "not-a-regular-file", "wrong-owner",
  "permissions-too-open", "oversize", "malformed", "not-a-closed-receipt", "wrong-kind",
  "unsupported-version", "wrong-check", "wrong-status", "invalid-observed-at",
  "invalid-valid-until", "invalid-validity-window", "future", "stale", "binding-mismatch",
  "owner-check-unavailable",
]);

export type ReceiptResult = { ok: true } | { ok: false; reason: string };
type SafeRead = { ok: true; contents: string } | { ok: false; reason: string };

interface PrivateFileStats {
  isSymbolicLink(): boolean;
  isFile(): boolean;
  uid: number;
  mode: number | bigint;
  size: number | bigint;
}

function privateFileReason(stats: PrivateFileStats, expectedUid: number | undefined, maxBytes: number): string | undefined {
  if (stats.isSymbolicLink()) return "symlink";
  if (!stats.isFile()) return "not-a-regular-file";
  if (expectedUid === undefined) return "owner-check-unavailable";
  if (stats.uid !== expectedUid) return "wrong-owner";
  if ((Number(stats.mode) & 0o077) !== 0) return "permissions-too-open";
  if (Number(stats.size) > maxBytes) return "oversize";
  return undefined;
}

/** Read a regular private file through a no-follow FD and verify the opened inode. */
function readPrivateFile(path: string, maxBytes: number, expectedUid: number | undefined): SafeRead {
  let listed;
  try { listed = lstatSync(path); } catch { return { ok: false, reason: "unreadable" }; }
  const listedProblem = privateFileReason(listed, expectedUid, maxBytes);
  if (listedProblem !== undefined) return { ok: false, reason: listedProblem };
  let fd: number | undefined;
  try {
    fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
    const opened = fstatSync(fd);
    const openedProblem = privateFileReason(opened, expectedUid, maxBytes);
    if (openedProblem !== undefined || opened.dev !== listed.dev || opened.ino !== listed.ino) {
      return { ok: false, reason: "unreadable" };
    }
    const size = Number(opened.size);
    const bytes = Buffer.alloc(size + 1);
    const count = readSync(fd, bytes, 0, bytes.length, 0);
    const after = fstatSync(fd);
    if (
      count !== size || after.dev !== opened.dev || after.ino !== opened.ino ||
      after.size !== opened.size || privateFileReason(after, expectedUid, maxBytes) !== undefined
    ) return { ok: false, reason: "unreadable" };
    return { ok: true, contents: bytes.subarray(0, count).toString("utf8") };
  } catch {
    return { ok: false, reason: "unreadable" };
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
}

export function readReceipt(
  path: string,
  spec: Pick<EvidenceSpec, "check" | "status">,
  nowMs: number,
  expectedUid: number | undefined,
  expectedBinding: ReceiptBinding,
): ReceiptResult {
  const file = readPrivateFile(path, RECEIPT_MAX_BYTES, expectedUid);
  if (!file.ok) return file;
  let parsed: JsonValue;
  try { parsed = JSON.parse(file.contents) as JsonValue; } catch { return { ok: false, reason: "malformed" }; }
  if (!isPlainObject(parsed)) return { ok: false, reason: "malformed" };
  if (Object.keys(parsed).sort().join(",") !== RECEIPT_KEYS.join(",")) return { ok: false, reason: "not-a-closed-receipt" };
  if (parsed.kind !== "mimir-relocation-evidence") return { ok: false, reason: "wrong-kind" };
  if (parsed.schema_version !== "v1") return { ok: false, reason: "unsupported-version" };
  if (parsed.check !== spec.check) return { ok: false, reason: "wrong-check" };
  if (parsed.status !== spec.status) return { ok: false, reason: "wrong-status" };
  if (RECEIPT_BINDING_FIELDS.some((field) => typeof parsed[field] !== "string" || parsed[field] !== expectedBinding[field])) {
    return { ok: false, reason: "binding-mismatch" };
  }
  const observedAt = parsed.observed_at;
  const validUntil = parsed.valid_until;
  if (typeof observedAt !== "string" || !isExactUtc(observedAt)) return { ok: false, reason: "invalid-observed-at" };
  if (typeof validUntil !== "string" || !isExactUtc(validUntil)) return { ok: false, reason: "invalid-valid-until" };
  const observedMs = Date.parse(observedAt);
  const validMs = Date.parse(validUntil);
  if (validMs <= observedMs) return { ok: false, reason: "invalid-validity-window" };
  if (observedMs > nowMs) return { ok: false, reason: "future" };
  if (validMs <= nowMs) return { ok: false, reason: "stale" };
  return { ok: true };
}

export type CheckOutcome = "success" | "failed" | "timed_out" | "unavailable";
export interface CheckResult { check: string; outcome: CheckOutcome; reason?: string; }
export interface HookRunResult { output: Record<string, JsonValue>; diagnostics: string[]; exitCode: number; }
const SYSTEMD_CHECKS = [{ check: "service", unit: "mimir.service" }, { check: "timer", unit: "mimir-offsite.timer" }] as const;

function systemdCheck(unit: string, timeoutMs: number): CheckOutcome {
  const result = spawnSync("systemctl", ["is-active", "--quiet", unit], { timeout: timeoutMs, stdio: "ignore" });
  if (result.error !== undefined) return (result.error as NodeJS.ErrnoException).code === "ETIMEDOUT" ? "timed_out" : "unavailable";
  if (result.signal !== null) return "timed_out";
  return result.status === 0 ? "success" : "failed";
}

function toExactUtc(ms: number): string { return new Date(Math.floor(ms / 1000) * 1000).toISOString().replace(/\.000Z$/, "Z"); }
function deterministicResultId(hook: string, binding: Binding): string {
  return `r-${createHash("sha256").update([hook, ...Object.values(binding)].join("\n")).digest("hex").slice(0, 24)}`;
}
function stableJson(value: JsonValue): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (isPlainObject(value)) return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}
export function resultContentDigest(output: Record<string, JsonValue>): string {
  const material = { ...output };
  delete material.content_digest;
  return `sha256:${createHash("sha256").update(stableJson(material)).digest("hex")}`;
}
function errorOutput(error: string, problems?: string[]): Record<string, JsonValue> {
  const output: Record<string, JsonValue> = { kind: "mimir-relocation-hook-error", schema_version: "v1", error };
  if (problems !== undefined) output.problems = problems;
  return output;
}

function ensureResultDirectory(path: string, expectedUid: number | undefined): boolean {
  if (expectedUid === undefined) return false;
  try { mkdirSync(path, { recursive: true, mode: 0o700 }); } catch { return false; }
  try {
    const stats = lstatSync(path);
    return !stats.isSymbolicLink() && stats.isDirectory() && stats.uid === expectedUid && (stats.mode & 0o777) === 0o700;
  } catch { return false; }
}

function bindingMatchesRecord(hook: string, binding: Binding, hookResult: Record<string, JsonValue>): boolean {
  return hookResult.hook === hook && (Object.keys(binding) as Array<keyof Binding>).every((field) => hookResult[field] === binding[field]);
}

function validChecks(checks: JsonValue, outcome: JsonValue): checks is JsonValue[] {
  if (!Array.isArray(checks) || !["success", "failed", "timed_out"].includes(String(outcome))) return false;
  if (checks.length > CHECK_NAMES.length || checks.some((entry, index) => !isPlainObject(entry) || entry.check !== CHECK_NAMES[index])) return false;
  for (let index = 0; index < checks.length; index += 1) {
    const entry = checks[index] as Record<string, JsonValue>;
    const system = index < SYSTEMD_CHECKS.length;
    if (entry.outcome === "success") {
      if (Object.keys(entry).sort().join(",") !== "check,outcome") return false;
    } else if (
      Object.keys(entry).sort().join(",") !== "check,outcome,reason" || typeof entry.reason !== "string" ||
      (system ? !["failed", "timed_out", "unavailable"].includes(String(entry.outcome)) || entry.reason !== `${SYSTEMD_CHECKS[index].unit} is not active` : entry.outcome !== "failed" || !RECEIPT_REASONS.has(entry.reason))
    ) return false;
  }
  const outcomes = checks.map((entry) => (entry as Record<string, JsonValue>).outcome);
  if (outcome === "success") return checks.length === CHECK_NAMES.length && outcomes.every((value) => value === "success");
  if (outcome === "failed") return checks.length === CHECK_NAMES.length && outcomes.some((value) => value !== "success") && outcomes.every((value) => value !== "timed_out");
  return outcomes.some((value) => value === "timed_out") || checks.length < CHECK_NAMES.length || outcomes.every((value) => value === "success");
}

function validReplayRecord(record: JsonValue, hook: string, binding: Binding, schema: Record<string, JsonValue>): record is Record<string, JsonValue> {
  if (!isPlainObject(record) || Object.keys(record).sort().join(",") !== OUTPUT_KEYS.join(",")) return false;
  if (record.kind !== "mimir-relocation-hook-result" || record.schema_version !== "v1" || typeof record.created_at !== "string" || !isExactUtc(record.created_at) || typeof record.content_digest !== "string" || !DIGEST_PATTERN.test(record.content_digest)) return false;
  if (record.content_digest !== resultContentDigest(record)) return false;
  if (!isPlainObject(record.hook_result) || definitionErrors(schema, HOOK_RESULT_DEF, record.hook_result).length > 0) return false;
  if (!bindingMatchesRecord(hook, binding, record.hook_result)) return false;
  if (record.hook_result.result_id !== deterministicResultId(hook, binding)) return false;
  if (
    record.hook_result.outcome === "timed_out" &&
    Array.isArray(record.checks) &&
    record.checks.length === CHECK_NAMES.length &&
    record.checks.every((entry) => isPlainObject(entry) && entry.outcome === "success") &&
    Date.parse(record.created_at) < Date.parse(binding.deadline)
  ) return false;
  return validChecks(record.checks, record.hook_result.outcome);
}

export interface HookOptions { nowMs?: () => number; uid?: number; }

export function runHook(hook: ReadOnlyHook, env: NodeJS.ProcessEnv, options: HookOptions = {}): HookRunResult {
  const nowFn = options.nowMs ?? (() => Date.now());
  const diagnostics: string[] = [];
  const bound = readBinding(env);
  if (bound.problems !== undefined) return { output: errorOutput("invalid_binding", bound.problems), diagnostics: bound.problems.map((problem) => `BLOCKED: ${problem}`), exitCode: 2 };
  const binding = bound.binding;
  const expectedUid = options.uid ?? process.getuid?.();
  const resultDir = env.MIMIR_RELOCATION_RESULT_DIR;
  if (resultDir === undefined || resultDir === "") return { output: errorOutput("invalid_binding", ["MIMIR_RELOCATION_RESULT_DIR is required for idempotent result records"]), diagnostics: ["BLOCKED: MIMIR_RELOCATION_RESULT_DIR is required for idempotent result records"], exitCode: 2 };
  const timeoutRaw = env.MIMIR_RELOCATION_TIMEOUT_SECONDS ?? "60";
  const timeoutSeconds = Number(timeoutRaw);
  if (!/^[1-9][0-9]*$/.test(timeoutRaw) || !Number.isSafeInteger(timeoutSeconds) || timeoutSeconds > MAX_TIMEOUT_SECONDS) {
    return { output: errorOutput("invalid_binding", ["MIMIR_RELOCATION_TIMEOUT_SECONDS is outside the allowed range"]), diagnostics: ["BLOCKED: MIMIR_RELOCATION_TIMEOUT_SECONDS is outside the allowed range"], exitCode: 2 };
  }
  const deadlineMs = Date.parse(binding.deadline);
  if (!Number.isSafeInteger(deadlineMs)) return { output: errorOutput("invalid_binding", ["MIMIR_RELOCATION_DEADLINE is invalid"]), diagnostics: ["BLOCKED: MIMIR_RELOCATION_DEADLINE is invalid"], exitCode: 2 };
  let schema: Record<string, JsonValue>;
  try { schema = loadNormativeSchema(); } catch { return { output: errorOutput("schema_unavailable"), diagnostics: ["BLOCKED: normative schema unavailable"], exitCode: 2 }; }
  if (!ensureResultDirectory(resultDir, expectedUid)) return { output: errorOutput("result_directory_invalid"), diagnostics: ["BLOCKED: result directory is unsafe"], exitCode: 2 };
  const recordPath = join(resultDir, `${hook}-${binding.idempotency_key}.json`);
  const replay = tryReplay(hook, binding, recordPath, diagnostics, expectedUid, schema);
  if (replay !== undefined) return replay;

  const checks: CheckResult[] = [];
  let expired = false;
  const beforeCheck = (): { now: number; remaining: number } | undefined => {
    const now = nowFn();
    const remaining = deadlineMs - now;
    if (remaining <= 0) { expired = true; diagnostics.push("BLOCKED: invocation deadline passed; no later checks were run"); return undefined; }
    return { now, remaining };
  };
  for (const { check, unit } of SYSTEMD_CHECKS) {
    const gate = beforeCheck();
    if (gate === undefined) break;
    const checkOutcome = systemdCheck(unit, Math.min(timeoutSeconds * 1000, gate.remaining));
    checks.push(checkOutcome === "success" ? { check, outcome: checkOutcome } : { check, outcome: checkOutcome, reason: `${unit} is not active` });
    diagnostics.push(checkOutcome === "success" ? `PASS: ${check} (${unit})` : `BLOCKED: ${check} (${unit}) ${checkOutcome}`);
  }
  if (!expired) for (const spec of EVIDENCE_CHECKS) {
    const gate = beforeCheck();
    if (gate === undefined) break;
    const path = env[spec.variable];
    const result = path === undefined || path === "" ? { ok: false as const, reason: "not-provided" } : readReceipt(path, spec, gate.now, expectedUid, binding);
    checks.push(result.ok ? { check: spec.check, outcome: "success" } : { check: spec.check, outcome: "failed", reason: result.reason });
    diagnostics.push(result.ok ? `PASS: ${spec.check} receipt (${spec.variable})` : `BLOCKED: ${spec.check} receipt ${result.reason} (${spec.variable})`);
  }
  if (!expired && nowFn() >= deadlineMs) {
    expired = true;
    diagnostics.push("BLOCKED: invocation deadline passed while checks were running");
  }
  const outcome: "success" | "failed" | "timed_out" = expired || checks.some((check) => check.outcome === "timed_out") ? "timed_out" : checks.length === CHECK_NAMES.length && checks.every((check) => check.outcome === "success") ? "success" : "failed";
  const hookResult: Record<string, JsonValue> = {
    result_id: deterministicResultId(hook, binding), hook, attempt_id: binding.attempt_id, plan_id: binding.plan_id,
    plan_digest: binding.plan_digest, desired_revision: binding.desired_revision, observation_evidence_id: binding.observation_evidence_id,
    action: binding.action, deadline: binding.deadline, idempotency_key: binding.idempotency_key, outcome,
  };
  if (definitionErrors(schema, HOOK_RESULT_DEF, hookResult).length > 0) return { output: errorOutput("internal_schema_violation"), diagnostics: ["BLOCKED: internal error: emitted hook_result violates the normative schema"], exitCode: 2 };
  const output: Record<string, JsonValue> = { kind: "mimir-relocation-hook-result", schema_version: "v1", hook_result: hookResult, checks: checks as unknown as JsonValue, created_at: toExactUtc(nowFn()), content_digest: "" };
  output.content_digest = resultContentDigest(output);
  try {
    writeFileSync(recordPath, `${JSON.stringify(output, null, 2)}\n`, { flag: "wx", mode: 0o600 });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "EEXIST") {
      const raced = tryReplay(hook, binding, recordPath, diagnostics, expectedUid, schema);
      if (raced !== undefined) return raced;
      return { output: errorOutput("idempotency_conflict"), diagnostics: [...diagnostics, "BLOCKED: idempotency record conflict"], exitCode: 2 };
    }
    return { output: errorOutput("result_record_unwritable"), diagnostics: [...diagnostics, "BLOCKED: could not record the hook result for idempotent replay"], exitCode: 2 };
  }
  diagnostics.push(outcome === "success" ? `PASS: Mimir ${hook} verification evidence is complete` : `BLOCKED: Mimir ${hook} is ${outcome}`);
  return { output, diagnostics, exitCode: outcome === "success" ? 0 : 1 };
}

function tryReplay(hook: string, binding: Binding, recordPath: string, diagnostics: string[], uid: number | undefined, schema: Record<string, JsonValue>): HookRunResult | undefined {
  let exists = true;
  try { lstatSync(recordPath); } catch { exists = false; }
  if (!exists) return undefined;
  const file = readPrivateFile(recordPath, RESULT_RECORD_MAX_BYTES, uid);
  if (!file.ok) return { output: errorOutput("result_record_invalid"), diagnostics: [...diagnostics, "BLOCKED: recorded idempotency result is unsafe"], exitCode: 2 };
  let record: JsonValue;
  try { record = JSON.parse(file.contents) as JsonValue; } catch { return { output: errorOutput("result_record_invalid"), diagnostics: [...diagnostics, "BLOCKED: recorded idempotency result is unreadable"], exitCode: 2 }; }
  if (!isPlainObject(record) || !isPlainObject(record.hook_result)) return { output: errorOutput("result_record_invalid"), diagnostics: [...diagnostics, "BLOCKED: recorded idempotency result is invalid"], exitCode: 2 };
  if (!bindingMatchesRecord(hook, binding, record.hook_result)) return { output: errorOutput("binding_conflict"), diagnostics: [...diagnostics, "BLOCKED: idempotency key already used by a different attempt binding; refusing replay"], exitCode: 2 };
  if (!validReplayRecord(record, hook, binding, schema)) return { output: errorOutput("result_record_invalid"), diagnostics: [...diagnostics, "BLOCKED: recorded idempotency result is invalid"], exitCode: 2 };
  diagnostics.push(`REPLAY: returning the recorded ${hook} result for this idempotency key`);
  return { output: record, diagnostics, exitCode: record.hook_result.outcome === "success" ? 0 : 1 };
}
