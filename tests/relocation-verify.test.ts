import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { spawnSync } from "node:child_process";
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  EVIDENCE_CHECKS,
  readBinding,
  readReceipt,
  runHook,
} from "../src/relocation-verify.js";
import { definitionErrors, loadNormativeSchema } from "../src/node-substrate.js";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const CLI = join(ROOT, "src/cli/relocation-verify.ts");
const WRAPPER = join(ROOT, "scripts/relocation-verify.sh");
const HOOK_RESULT_DEF = "#/$defs/lifecycle-result/properties/hook_results/items";

const NOW = Date.parse("2026-07-23T12:00:00Z");
const UID = process.getuid?.() ?? 0;

const TUNNEL = { check: "tunnel", status: "tunnel-v1:connected" };

let workDir: string;

function receiptBody(check: string, status: string, overrides: Record<string, unknown> = {}) {
  return {
    kind: "mimir-relocation-evidence",
    schema_version: "v1",
    check,
    status,
    observed_at: "2026-07-23T11:00:00Z",
    valid_until: "2026-07-23T13:00:00Z",
    ...overrides,
  };
}

function writeReceipt(
  name: string,
  body: unknown,
  mode = 0o600,
  serialize: (value: unknown) => string = (value) => JSON.stringify(value, null, 2),
): string {
  const path = join(workDir, name);
  writeFileSync(path, serialize(body), { mode });
  chmodSync(path, mode);
  return path;
}

function binding(overrides: Record<string, string> = {}): Record<string, string> {
  return {
    MIMIR_RELOCATION_ATTEMPT_ID: "attempt-001",
    MIMIR_RELOCATION_PLAN_ID: "plan-001",
    MIMIR_RELOCATION_PLAN_DIGEST: `sha256:${"a".repeat(64)}`,
    MIMIR_RELOCATION_DESIRED_REVISION: `sha256:${"b".repeat(64)}`,
    MIMIR_RELOCATION_OBSERVATION_EVIDENCE_ID: "obs-001",
    MIMIR_RELOCATION_ACTION: "relocate",
    MIMIR_RELOCATION_DEADLINE: "2026-07-23T12:30:00Z",
    MIMIR_RELOCATION_IDEMPOTENCY_KEY: "idem-001",
    ...overrides,
  };
}

function fullEnv(overrides: Record<string, string> = {}): NodeJS.ProcessEnv {
  const env: Record<string, string> = {
    ...binding(),
    MIMIR_RELOCATION_RESULT_DIR: join(workDir, "results"),
  };
  for (const spec of EVIDENCE_CHECKS) {
    env[spec.variable] = writeReceipt(
      `${spec.check}.json`,
      receiptBody(spec.check, spec.status),
    );
  }
  return { ...env, ...overrides };
}

/** Mock systemctl on PATH; behavior driven by MOCK_SYSTEMCTL_* env vars. */
function installSystemctlMock(): string {
  const bin = join(workDir, "mockbin");
  mkdirSync(bin, { recursive: true });
  const callsFile = join(workDir, "systemctl-calls");
  writeFileSync(callsFile, "");
  writeFileSync(
    join(bin, "systemctl"),
    `#!/bin/sh
echo "$@" >> "${callsFile}"
[ -n "\${MOCK_SYSTEMCTL_SLEEP:-}" ] && sleep "\${MOCK_SYSTEMCTL_SLEEP}"
exit "\${MOCK_SYSTEMCTL_RC:-0}"
`,
    { mode: 0o755 },
  );
  return bin;
}

const savedEnv: Record<string, string | undefined> = {};
function setProcessEnv(key: string, value: string): void {
  if (!(key in savedEnv)) savedEnv[key] = process.env[key];
  process.env[key] = value;
}

beforeEach(() => {
  workDir = mkdtempSync(join(tmpdir(), "mimir-relocation-test-"));
  setProcessEnv("PATH", `${installSystemctlMock()}:${process.env.PATH ?? ""}`);
});

afterEach(() => {
  for (const [key, value] of Object.entries(savedEnv)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
    delete savedEnv[key];
  }
  delete process.env.MOCK_SYSTEMCTL_RC;
  delete process.env.MOCK_SYSTEMCTL_SLEEP;
  rmSync(workDir, { recursive: true, force: true });
});

// ---------------------------------------------------------------------------
// Receipt validation
// ---------------------------------------------------------------------------

describe("readReceipt", () => {
  it("accepts a fresh, closed, owner-only receipt", () => {
    const path = writeReceipt("ok.json", receiptBody("tunnel", "tunnel-v1:connected"));
    expect(readReceipt(path, TUNNEL, NOW, UID)).toEqual({ ok: true });
  });

  it("accepts mode 0400", () => {
    const path = writeReceipt("ro.json", receiptBody("tunnel", "tunnel-v1:connected"), 0o400);
    expect(readReceipt(path, TUNNEL, NOW, UID)).toEqual({ ok: true });
  });

  it("rejects a missing file without leaking the path", () => {
    const result = readReceipt(join(workDir, "absent.json"), TUNNEL, NOW, UID);
    expect(result).toEqual({ ok: false, reason: "unreadable" });
  });

  it("rejects a symlink even when its target is valid", () => {
    const target = writeReceipt("target.json", receiptBody("tunnel", "tunnel-v1:connected"));
    const link = join(workDir, "link.json");
    symlinkSync(target, link);
    expect(readReceipt(link, TUNNEL, NOW, UID)).toEqual({ ok: false, reason: "symlink" });
  });

  it("rejects a FIFO without opening it", () => {
    const fifo = join(workDir, "fifo");
    const made = spawnSync("mkfifo", [fifo]);
    expect(made.status).toBe(0);
    chmodSync(fifo, 0o600);
    expect(readReceipt(fifo, TUNNEL, NOW, UID)).toEqual({
      ok: false,
      reason: "not-a-regular-file",
    });
  });

  it("rejects a directory", () => {
    const dir = join(workDir, "dir");
    mkdirSync(dir, { mode: 0o700 });
    expect(readReceipt(dir, TUNNEL, NOW, UID)).toEqual({
      ok: false,
      reason: "not-a-regular-file",
    });
  });

  it("rejects a receipt owned by another user", () => {
    const path = writeReceipt("owner.json", receiptBody("tunnel", "tunnel-v1:connected"));
    expect(readReceipt(path, TUNNEL, NOW, UID + 1)).toEqual({
      ok: false,
      reason: "wrong-owner",
    });
  });

  it.each([0o644, 0o640, 0o604, 0o660])("rejects permissive mode %s", (mode) => {
    const path = writeReceipt("mode.json", receiptBody("tunnel", "tunnel-v1:connected"), mode);
    expect(readReceipt(path, TUNNEL, NOW, UID)).toEqual({
      ok: false,
      reason: "permissions-too-open",
    });
  });

  it("rejects an oversize receipt before parsing", () => {
    const body = receiptBody("tunnel", "tunnel-v1:connected", {
      // Padding lands inside the closed-shape check, but size wins first.
      status: "x".repeat(5000),
    });
    const path = writeReceipt("big.json", body);
    expect(readReceipt(path, TUNNEL, NOW, UID)).toEqual({ ok: false, reason: "oversize" });
  });

  it("rejects malformed JSON without echoing content", () => {
    const path = writeReceipt("bad.json", null, 0o600, () => "CANARY{{not json");
    const result = readReceipt(path, TUNNEL, NOW, UID);
    expect(result).toEqual({ ok: false, reason: "malformed" });
    expect(JSON.stringify(result)).not.toContain("CANARY");
  });

  it("rejects a non-object receipt", () => {
    const path = writeReceipt("array.json", ["tunnel-v1:connected"]);
    expect(readReceipt(path, TUNNEL, NOW, UID)).toEqual({ ok: false, reason: "malformed" });
  });

  it("rejects extra fields — the receipt shape is closed", () => {
    const path = writeReceipt(
      "extra.json",
      receiptBody("tunnel", "tunnel-v1:connected", { note: "extra" }),
    );
    expect(readReceipt(path, TUNNEL, NOW, UID)).toEqual({
      ok: false,
      reason: "not-a-closed-receipt",
    });
  });

  it("rejects missing fields", () => {
    const body = receiptBody("tunnel", "tunnel-v1:connected") as Record<string, unknown>;
    delete body.valid_until;
    const path = writeReceipt("missing.json", body);
    expect(readReceipt(path, TUNNEL, NOW, UID)).toEqual({
      ok: false,
      reason: "not-a-closed-receipt",
    });
  });

  it.each([
    ["wrong-kind", { kind: "static-line" }],
    ["unsupported-version", { schema_version: "v2" }],
    ["wrong-check", { check: "sync" }],
    ["wrong-status", { status: "tunnel-v1:disconnected" }],
  ] as const)("rejects %s", (reason, overrides) => {
    const path = writeReceipt(
      `${reason}.json`,
      receiptBody("tunnel", "tunnel-v1:connected", overrides),
    );
    expect(readReceipt(path, TUNNEL, NOW, UID)).toEqual({ ok: false, reason });
  });

  it.each([
    "2026-07-23T11:00:00.123Z",
    "2026-07-23T11:00:00+00:00",
    "2026-07-23 11:00:00Z",
    "2026-02-30T11:00:00Z",
    "not-a-time",
  ])("rejects non-exact-UTC observed_at %s", (observedAt) => {
    const path = writeReceipt(
      "ts.json",
      receiptBody("tunnel", "tunnel-v1:connected", { observed_at: observedAt }),
    );
    expect(readReceipt(path, TUNNEL, NOW, UID)).toEqual({
      ok: false,
      reason: "invalid-observed-at",
    });
  });

  it("rejects a validity window that ends before it starts", () => {
    const path = writeReceipt(
      "window.json",
      receiptBody("tunnel", "tunnel-v1:connected", {
        observed_at: "2026-07-23T11:00:00Z",
        valid_until: "2026-07-23T10:00:00Z",
      }),
    );
    expect(readReceipt(path, TUNNEL, NOW, UID)).toEqual({
      ok: false,
      reason: "invalid-validity-window",
    });
  });

  it("rejects a receipt observed in the future", () => {
    const path = writeReceipt(
      "future.json",
      receiptBody("tunnel", "tunnel-v1:connected", {
        observed_at: "2026-07-23T12:30:00Z",
        valid_until: "2026-07-23T14:00:00Z",
      }),
    );
    expect(readReceipt(path, TUNNEL, NOW, UID)).toEqual({ ok: false, reason: "future" });
  });

  it("rejects a stale receipt whose validity has expired", () => {
    const path = writeReceipt(
      "stale.json",
      receiptBody("tunnel", "tunnel-v1:connected", {
        observed_at: "2026-07-23T09:00:00Z",
        valid_until: "2026-07-23T10:00:00Z",
      }),
    );
    expect(readReceipt(path, TUNNEL, NOW, UID)).toEqual({ ok: false, reason: "stale" });
  });
});

// ---------------------------------------------------------------------------
// Invocation binding
// ---------------------------------------------------------------------------

describe("readBinding", () => {
  it("parses a complete valid binding", () => {
    const result = readBinding(binding());
    expect(result.problems).toBeUndefined();
    expect(result.binding).toEqual({
      attempt_id: "attempt-001",
      plan_id: "plan-001",
      plan_digest: `sha256:${"a".repeat(64)}`,
      desired_revision: `sha256:${"b".repeat(64)}`,
      observation_evidence_id: "obs-001",
      action: "relocate",
      deadline: "2026-07-23T12:30:00Z",
      idempotency_key: "idem-001",
    });
  });

  it.each([
    ["MIMIR_RELOCATION_ATTEMPT_ID", "Bad_ID"],
    ["MIMIR_RELOCATION_PLAN_DIGEST", "sha256:short"],
    ["MIMIR_RELOCATION_DESIRED_REVISION", "not-a-digest"],
    ["MIMIR_RELOCATION_ACTION", "reprovision"],
    ["MIMIR_RELOCATION_DEADLINE", "2026-07-23T12:30:00+02:00"],
    ["MIMIR_RELOCATION_DEADLINE", "2026-13-01T00:00:00Z"],
    ["MIMIR_RELOCATION_IDEMPOTENCY_KEY", "x"],
  ])("rejects invalid %s=%s", (variable, value) => {
    const result = readBinding(binding({ [variable]: value }));
    expect(result.problems?.join("\n")).toContain(variable);
  });

  it("reports every missing variable by name", () => {
    const result = readBinding({});
    expect(result.problems).toHaveLength(8);
    for (const problem of result.problems ?? []) expect(problem).toContain("MIMIR_RELOCATION_");
  });
});

// ---------------------------------------------------------------------------
// Hook execution (in-process, systemctl mocked via PATH)
// ---------------------------------------------------------------------------

describe("runHook", () => {
  const clock = (ms: number) => () => ms;

  it("succeeds with active units and eight fresh distinct receipts", () => {
    const env = fullEnv();
    const { output, exitCode, diagnostics } = runHook("preflight", env, { nowMs: clock(NOW) });
    expect(exitCode, diagnostics.join("\n")).toBe(0);
    const hookResult = output.hook_result as Record<string, unknown>;
    expect(hookResult.outcome).toBe("success");
    expect(hookResult.hook).toBe("preflight");
    const checks = output.checks as Array<{ check: string; outcome: string }>;
    expect(checks.map((check) => check.check)).toEqual([
      "service", "timer",
      "tunnel", "sync", "t7-copy", "t7-integrity", "offsite", "heimdall",
      "restore", "deployment-marker",
    ]);
    expect(checks.every((check) => check.outcome === "success")).toBe(true);
  });

  it("binds and validates the emitted hook_result against the normative schema", () => {
    const env = fullEnv();
    const { output } = runHook("verify", env, { nowMs: clock(NOW) });
    const hookResult = output.hook_result as Record<string, never>;
    expect(definitionErrors(loadNormativeSchema(), HOOK_RESULT_DEF, hookResult)).toEqual([]);
    expect(hookResult["attempt_id"]).toBe("attempt-001");
    expect(hookResult["plan_id"]).toBe("plan-001");
    expect(hookResult["plan_digest"]).toBe(`sha256:${"a".repeat(64)}`);
    expect(hookResult["desired_revision"]).toBe(`sha256:${"b".repeat(64)}`);
    expect(hookResult["observation_evidence_id"]).toBe("obs-001");
    expect(hookResult["action"]).toBe("relocate");
    expect(hookResult["deadline"]).toBe("2026-07-23T12:30:00Z");
    expect(hookResult["idempotency_key"]).toBe("idem-001");
  });

  it("fails closed when a systemd unit is not active", () => {
    setProcessEnv("MOCK_SYSTEMCTL_RC", "3");
    const env = fullEnv();
    const { output, exitCode } = runHook("preflight", env, { nowMs: clock(NOW) });
    expect(exitCode).toBe(1);
    const hookResult = output.hook_result as Record<string, unknown>;
    expect(hookResult.outcome).toBe("failed");
    const checks = output.checks as Array<{ check: string; outcome: string }>;
    expect(checks.find((check) => check.check === "service")?.outcome).toBe("failed");
  });

  it("records timed_out when a systemd status read exceeds the bounded timeout", () => {
    setProcessEnv("MOCK_SYSTEMCTL_SLEEP", "5");
    const env = fullEnv({ MIMIR_RELOCATION_TIMEOUT_SECONDS: "1" });
    const { output, exitCode } = runHook("preflight", env, { nowMs: clock(NOW) });
    expect(exitCode).toBe(1);
    expect((output.hook_result as Record<string, unknown>).outcome).toBe("timed_out");
  });

  it("refuses to run any check when the deadline has already passed", () => {
    const env = fullEnv();
    const late = Date.parse("2026-07-23T13:00:00Z");
    const { output, exitCode } = runHook("preflight", env, { nowMs: clock(late) });
    expect(exitCode).toBe(1);
    expect((output.hook_result as Record<string, unknown>).outcome).toBe("timed_out");
    expect(output.checks).toEqual([]);
  });

  it("records timed_out when the deadline expires while checks run", () => {
    const env = fullEnv();
    const start = Date.parse("2026-07-23T12:29:59Z");
    let calls = 0;
    const advancing = () => (calls++ === 0 ? start : start + 120_000);
    const { output, exitCode } = runHook("preflight", env, { nowMs: advancing });
    expect(exitCode).toBe(1);
    expect((output.hook_result as Record<string, unknown>).outcome).toBe("timed_out");
  });

  it("fails closed when a receipt variable is not provided", () => {
    const env = fullEnv();
    delete env.MIMIR_RELOCATION_RESTORE_EVIDENCE;
    const { output, exitCode } = runHook("preflight", env, { nowMs: clock(NOW) });
    expect(exitCode).toBe(1);
    const checks = output.checks as Array<{ check: string; outcome: string; reason?: string }>;
    expect(checks.find((check) => check.check === "restore")).toEqual({
      check: "restore",
      outcome: "failed",
      reason: "not-provided",
    });
  });

  it("keeps T7 copy completion and T7 integrity as independent evidence", () => {
    const env = fullEnv();
    env.MIMIR_RELOCATION_T7_INTEGRITY_EVIDENCE = writeReceipt(
      "integrity-stale.json",
      receiptBody("t7-integrity", "local-copy-integrity-v1:verified", {
        observed_at: "2026-07-23T09:00:00Z",
        valid_until: "2026-07-23T10:00:00Z",
      }),
    );
    const { output, exitCode } = runHook("verify", env, { nowMs: clock(NOW) });
    expect(exitCode).toBe(1);
    const checks = output.checks as Array<{ check: string; outcome: string; reason?: string }>;
    expect(checks.find((check) => check.check === "t7-copy")?.outcome).toBe("success");
    expect(checks.find((check) => check.check === "t7-integrity")).toEqual({
      check: "t7-integrity",
      outcome: "failed",
      reason: "stale",
    });
  });

  it("rejects an invalid binding before reading any evidence", () => {
    const env = fullEnv({ MIMIR_RELOCATION_PLAN_DIGEST: "garbage" });
    const { output, exitCode } = runHook("preflight", env, { nowMs: clock(NOW) });
    expect(exitCode).toBe(2);
    expect(output.error).toBe("invalid_binding");
  });

  it("requires the idempotency result directory", () => {
    const env = fullEnv();
    delete env.MIMIR_RELOCATION_RESULT_DIR;
    const { output, exitCode } = runHook("preflight", env, { nowMs: clock(NOW) });
    expect(exitCode).toBe(2);
    expect(output.error).toBe("invalid_binding");
  });

  it("replays the recorded result for the same idempotency key", () => {
    const env = fullEnv();
    const first = runHook("preflight", env, { nowMs: clock(NOW) });
    expect(first.exitCode).toBe(0);
    setProcessEnv("MOCK_SYSTEMCTL_RC", "3"); // live state changed; replay must ignore it
    const second = runHook("preflight", env, { nowMs: clock(NOW + 60_000) });
    expect(second.exitCode).toBe(0);
    expect(second.output).toEqual(first.output);
    expect(second.diagnostics.join("\n")).toContain("REPLAY");
  });

  it("refuses the same idempotency key under a different attempt binding", () => {
    const env = fullEnv();
    expect(runHook("preflight", env, { nowMs: clock(NOW) }).exitCode).toBe(0);
    const conflicting = { ...env, MIMIR_RELOCATION_ATTEMPT_ID: "attempt-002" };
    const { output, exitCode } = runHook("preflight", conflicting, { nowMs: clock(NOW) });
    expect(exitCode).toBe(2);
    expect(output.error).toBe("binding_conflict");
  });

  it("scopes idempotency records per hook", () => {
    const env = fullEnv();
    expect(runHook("preflight", env, { nowMs: clock(NOW) }).exitCode).toBe(0);
    const verify = runHook("verify", env, { nowMs: clock(NOW) });
    expect(verify.exitCode).toBe(0);
    expect(verify.diagnostics.join("\n")).not.toContain("REPLAY");
  });

  it("fails closed on a corrupted idempotency record", () => {
    const env = fullEnv();
    const resultDir = env.MIMIR_RELOCATION_RESULT_DIR as string;
    mkdirSync(resultDir, { recursive: true, mode: 0o700 });
    writeFileSync(join(resultDir, "preflight-idem-001.json"), "corrupt{", { mode: 0o600 });
    const { output, exitCode } = runHook("preflight", env, { nowMs: clock(NOW) });
    expect(exitCode).toBe(2);
    expect(output.error).toBe("result_record_invalid");
  });

  it("fails closed on an idempotency record with permissive mode", () => {
    const env = fullEnv();
    const first = runHook("preflight", env, { nowMs: clock(NOW) });
    expect(first.exitCode).toBe(0);
    const resultDir = env.MIMIR_RELOCATION_RESULT_DIR as string;
    chmodSync(join(resultDir, "preflight-idem-001.json"), 0o644);
    const { output, exitCode } = runHook("preflight", env, { nowMs: clock(NOW) });
    expect(exitCode).toBe(2);
    expect(output.error).toBe("result_record_invalid");
  });

  it("never leaks receipt content or filesystem paths on failure", () => {
    const canaryDir = join(workDir, "canary-path-fragment-9d1");
    mkdirSync(canaryDir, { recursive: true, mode: 0o700 });
    const canaryPath = join(canaryDir, "leaky-receipt.json");
    writeFileSync(canaryPath, "CANARY-CONTENT-7f3 {malformed", { mode: 0o600 });
    const env = fullEnv({ MIMIR_RELOCATION_TUNNEL_EVIDENCE: canaryPath });
    const { output, diagnostics, exitCode } = runHook("preflight", env, { nowMs: clock(NOW) });
    expect(exitCode).toBe(1);
    const everything = `${JSON.stringify(output)}\n${diagnostics.join("\n")}`;
    expect(everything).not.toContain("CANARY-CONTENT-7f3");
    expect(everything).not.toContain("canary-path-fragment-9d1");
    expect(everything).not.toContain(workDir);
    expect(everything).toContain("MIMIR_RELOCATION_TUNNEL_EVIDENCE");
  });
});

// ---------------------------------------------------------------------------
// CLI and wrapper script (end-to-end)
// ---------------------------------------------------------------------------

describe("relocation-verify CLI", () => {
  function runCli(args: string[], env: NodeJS.ProcessEnv = {}) {
    return spawnSync(process.execPath, ["--import", "tsx", CLI, ...args], {
      cwd: ROOT,
      encoding: "utf8",
      env: { ...process.env, ...env },
    });
  }

  function freshCliEnv(): NodeJS.ProcessEnv {
    const nowSeconds = Math.floor(Date.now() / 1000);
    const iso = (deltaSeconds: number) =>
      new Date((nowSeconds + deltaSeconds) * 1000).toISOString().replace(".000Z", "Z");
    const env = fullEnv({ MIMIR_RELOCATION_DEADLINE: iso(600) });
    for (const spec of EVIDENCE_CHECKS) {
      env[spec.variable] = writeReceipt(
        `cli-${spec.check}.json`,
        receiptBody(spec.check, spec.status, {
          observed_at: iso(-60),
          valid_until: iso(3600),
        }),
      );
    }
    return env;
  }

  it("emits schema-bound JSON on stdout and diagnostics on stderr", () => {
    const result = runCli(["preflight"], freshCliEnv());
    expect(result.status, result.stderr).toBe(0);
    const output = JSON.parse(result.stdout);
    expect(output.kind).toBe("mimir-relocation-hook-result");
    expect(
      definitionErrors(loadNormativeSchema(), HOOK_RESULT_DEF, output.hook_result),
    ).toEqual([]);
    expect(output.hook_result.outcome).toBe("success");
    expect(result.stderr).toContain("PASS: Mimir preflight verification evidence is complete");
  });

  it.each(["drain", "compensate", "rollback"])(
    "refuses the mutating %s hook with no side effects",
    (hook) => {
      const env = freshCliEnv();
      const result = runCli([hook], env);
      expect(result.status).toBe(2);
      expect(JSON.parse(result.stdout).error).toBe("mutating_hook_refused");
      expect(result.stderr).toContain("refuses");
      expect(() => readdirSync(env.MIMIR_RELOCATION_RESULT_DIR as string)).toThrow();
    },
  );

  it("prints usage for an unknown hook", () => {
    const result = runCli(["observe"]);
    expect(result.status).toBe(2);
    expect(JSON.parse(result.stdout).error).toBe("usage");
    expect(result.stderr).toContain("Usage");
  });

  it("wrapper script refuses mutating hooks before reaching node", () => {
    const result = spawnSync("bash", [WRAPPER, "drain"], { encoding: "utf8" });
    expect(result.status).toBe(2);
    expect(result.stderr).toContain("read-only hook refuses it");
  });

  it("wrapper script prints usage without arguments", () => {
    const result = spawnSync("bash", [WRAPPER], { encoding: "utf8" });
    expect(result.status).toBe(2);
    expect(result.stderr).toContain("Usage");
  });
});
