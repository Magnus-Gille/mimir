#!/usr/bin/env node
/**
 * Read-only Mimir relocation hook CLI (Grimnir ADR-007).
 *
 * Usage:
 *   node dist/cli/relocation-verify.js preflight|verify
 *
 * The ADR-007 invocation binding, the result-record directory, and the
 * evidence receipt locations are supplied via MIMIR_RELOCATION_* environment
 * variables (see docs/relocation.md). Machine-readable JSON is emitted on
 * stdout; concise human diagnostics go to stderr.
 *
 * Exit 0: hook ran and every check passed.
 * Exit 1: hook ran and is fail-closed blocked (or timed out).
 * Exit 2: refused — mutating hook, invalid binding, or unsafe local state.
 */
import {
  MUTATING_HOOKS,
  READ_ONLY_HOOKS,
  runHook,
  type ReadOnlyHook,
} from "../relocation-verify.js";

function emit(output: unknown, diagnostics: string[], exitCode: number): never {
  process.stdout.write(`${JSON.stringify(output, null, 2)}\n`);
  for (const line of diagnostics) process.stderr.write(`${line}\n`);
  process.exit(exitCode);
}

const hook = process.argv[2] ?? "";
if ((MUTATING_HOOKS as readonly string[]).includes(hook)) {
  emit(
    { kind: "mimir-relocation-hook-error", schema_version: "v1", error: "mutating_hook_refused", hook },
    [
      `ERROR: ${hook} is a mutating component-owned operation; this read-only hook refuses it.`,
      "Use the documented, attempt-bound operator procedure and its compensation recipe.",
    ],
    2,
  );
}
if (!(READ_ONLY_HOOKS as readonly string[]).includes(hook)) {
  emit(
    { kind: "mimir-relocation-hook-error", schema_version: "v1", error: "usage" },
    ["Usage: relocation-verify preflight|verify"],
    2,
  );
}

const { output, diagnostics, exitCode } = runHook(hook as ReadOnlyHook, process.env);
emit(output, diagnostics, exitCode);
