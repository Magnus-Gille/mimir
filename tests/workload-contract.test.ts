import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const contract = JSON.parse(readFileSync(resolve(root, "docs/workload-requirement-v1.json"), "utf8"));
const provenance = JSON.parse(readFileSync(resolve(root, "docs/workload-requirement-v1.provenance.json"), "utf8"));
const hooks = readFileSync(resolve(root, "scripts/relocation-verify.sh"), "utf8");

describe("Mimir node/substrate workload contract", () => {
  it("pins the exact shared v1 schema and fixture semantics", () => {
    expect(contract.kind).toBe("workload-requirement");
    expect(contract.schema_version).toBe("v1");
    expect(provenance).toEqual({
      source_repository: "Magnus-Gille/grimnir",
      source_revision: "6d54d49c91612eae7dce5f66286d801900c38c35",
      schema_path: "docs/node-substrate-contract-v1.schema.json",
      schema_sha256: "9a69f1b23499cd6e70fdaa80ee57bf983e7e5b288882e0cf2b0f01f10824fbbe",
      fixture_set_path: "tests/fixtures/node-substrate-contract/consumer-fixture-set.json",
      fixture_set_sha256: "355481f2b3866840795ba18033077d6f36487d1a447b36c323384cf7837c5fcb",
      interpretation: "No consumer-specific overlay is present.",
    });
    expect(Object.keys(contract).sort()).toEqual([
      "backup_restore", "dependencies", "extensions", "health", "hooks", "kind",
      "persistent_data", "ports", "schema_version", "secrets_boundary",
      "supported_architectures", "timers", "units", "workload_id",
    ]);
  });

  it("declares persistent storage, recovery and bound hook semantics", () => {
    expect(contract.persistent_data).toBe("required");
    expect(contract.backup_restore).toBe("required");
    expect(contract.secrets_boundary).toBe("owner_overlay");
    expect(contract.hooks).toEqual(expect.arrayContaining([
      expect.objectContaining({ name: "preflight", mode: "read_only", idempotency_required: true }),
      expect.objectContaining({ name: "drain", mode: "mutating", compensation_hook: "compensate" }),
      expect.objectContaining({ name: "verify", mode: "read_only", idempotency_required: true }),
      expect.objectContaining({ name: "compensate", mode: "mutating", idempotency_required: true }),
    ]));
  });

  it("ships only allowlisted read-only verification commands", () => {
    expect(hooks).toContain('case "$HOOK" in');
    expect(hooks).toContain('preflight|verify)');
    expect(hooks).toContain('drain|compensate|rollback)');
    expect(hooks).toContain('timeout "$TIMEOUT_SECONDS"');
    expect(hooks).not.toContain("systemctl stop");
    expect(hooks).not.toContain("rsync ");
  });
});
