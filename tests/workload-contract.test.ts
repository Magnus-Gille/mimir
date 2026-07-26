import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  GRIMNIR_FIXTURE_SET_SHA256,
  GRIMNIR_NORMATIVE_VALIDATOR_SHA256,
  GRIMNIR_SCHEMA_SHA256,
  GRIMNIR_SOURCE_REVISION,
  assertVendoredContractArtifactsPinned,
  checkSchemaSupported,
  loadConsumerFixtureSet,
  loadNormativeSchema,
  loadVendoredJson,
  schemaErrors,
  sha256Hex,
  type JsonValue,
} from "../src/node-substrate.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path: string) => readFileSync(resolve(root, path));
const manifest = JSON.parse(read("docs/workload-requirement-v1.json").toString("utf8"));
const provenance = JSON.parse(
  read("docs/workload-requirement-v1.provenance.json").toString("utf8"),
);
const schema = loadNormativeSchema();
const clone = (): Record<string, JsonValue> => structuredClone(manifest);

const canonicalValidator = resolve(
  root,
  "docs/vendor/grimnir/tests/scripts/validate-node-substrate-contract.mjs",
);

describe("vendored Grimnir contract provenance", () => {
  it("records the exact immutable source revision and digests", () => {
    expect(provenance).toEqual({
      source_repository: "Magnus-Gille/grimnir",
      source_revision: GRIMNIR_SOURCE_REVISION,
      schema_path: "docs/node-substrate-contract-v1.schema.json",
      schema_sha256: GRIMNIR_SCHEMA_SHA256,
      fixture_set_path: "tests/fixtures/node-substrate-contract/consumer-fixture-set.json",
      fixture_set_sha256: GRIMNIR_FIXTURE_SET_SHA256,
      vendored_schema_path: "docs/vendor/grimnir/docs/node-substrate-contract-v1.schema.json",
      vendored_fixture_set_path: "docs/vendor/grimnir/tests/fixtures/node-substrate-contract/consumer-fixture-set.json",
      normative_validator_path: "tests/scripts/validate-node-substrate-contract.mjs",
      normative_validator_sha256: GRIMNIR_NORMATIVE_VALIDATOR_SHA256,
      vendored_normative_validator_path:
        "docs/vendor/grimnir/tests/scripts/validate-node-substrate-contract.mjs",
      shared_fixture_sha256: {
        "positive.json": "42f34fe1c576648240cef0f7f427073e9f39c11f8bfe0cf3f2ea74899bfee234",
        "partial-drain.json": "b596e56fb60a0710e1653c1a7935e15a98baf818b7ce6c56421a84cfbdd21d7b",
        "partial-substrate.json": "3a26d123bfcb98adbd8f8f81c2736b38d485a1ac2665deb1770636f219ba6d07",
        "negative.json": "e67d9233a556aa6da9728e9c07ae95ac3b1bc9abe9a4ac8ad817158829b8ead5",
      },
      interpretation: "No consumer-specific overlay is present.",
    });
    expect(GRIMNIR_SOURCE_REVISION).toBe("6d54d49c91612eae7dce5f66286d801900c38c35");
  });

  it("detects drift between the vendored artifacts and the pinned digests", () => {
    expect(() => assertVendoredContractArtifactsPinned()).not.toThrow();
    expect(sha256Hex(read(provenance.vendored_schema_path))).toBe(provenance.schema_sha256);
    expect(sha256Hex(read(provenance.vendored_fixture_set_path))).toBe(
      provenance.fixture_set_sha256,
    );
    expect(sha256Hex(read(provenance.vendored_normative_validator_path))).toBe(
      provenance.normative_validator_sha256,
    );
    for (const [name, digest] of Object.entries(provenance.shared_fixture_sha256)) {
      expect(
        sha256Hex(read(`docs/vendor/grimnir/tests/fixtures/node-substrate-contract/${name}`)),
      ).toBe(digest);
    }
  });

  it("fails closed when a vendored artifact does not match its pin", () => {
    expect(() =>
      loadVendoredJson(
        resolve(root, provenance.vendored_schema_path),
        "0".repeat(64),
      ),
    ).toThrow(/drifted from its pinned revision/);
  });

  it("is named as a consumer of the shared fixture manifest", () => {
    const fixtureSet = loadConsumerFixtureSet();
    expect(fixtureSet.contract).toBe("grimnir.node-substrate/v1");
    expect(fixtureSet.consumers).toContain("mimir");
    expect(fixtureSet.fixtures).toEqual([
      "positive.json",
      "partial-drain.json",
      "partial-substrate.json",
    ]);
  });

  it("uses only the JSON Schema subset the local evaluator implements", () => {
    expect(() => checkSchemaSupported(schema)).not.toThrow();
  });

  it("executes the unchanged canonical positive, partial, and semantic-negative suite", () => {
    const result = spawnSync(process.execPath, [canonicalValidator], {
      cwd: root,
      encoding: "utf8",
    });
    expect(result.status, `${result.stdout}\n${result.stderr}`).toBe(0);
    expect(result.stdout).toContain(
      "Node/substrate v1 normative schema plus 10 hermetic fixture scenarios validated.",
    );
  });
});

describe("Mimir workload requirement manifest", () => {
  it("validates against the vendored normative v1 schema", () => {
    expect(schemaErrors(schema, schema, manifest)).toEqual([]);
  });

  it("rejects an unknown field through the normative schema, not hand asserts", () => {
    const mutated = clone();
    mutated.private_hostname = "nas.internal";
    expect(schemaErrors(schema, schema, mutated)).not.toEqual([]);
  });

  it("rejects a missing required field through the normative schema", () => {
    const mutated = clone();
    delete mutated.backup_restore;
    expect(schemaErrors(schema, schema, mutated)).not.toEqual([]);
  });

  it("rejects an invalid hook declaration through the normative schema", () => {
    const mutated = clone();
    (mutated.hooks as JsonValue[]).push({
      name: "reprovision",
      mode: "mutating",
      contract_versions: ["v1"],
      deadline_seconds: 300,
      idempotency_required: true,
    });
    expect(schemaErrors(schema, schema, mutated)).not.toEqual([]);
  });

  it("rejects a decision-driving extension through the normative schema", () => {
    const mutated = clone();
    mutated.extensions = [
      { id: "mimir-relocation-evidence", version: "v1", decision_effect: "authoritative" },
    ];
    expect(schemaErrors(schema, schema, mutated)).not.toEqual([]);
  });

  it("declares Mimir-owned persistence, recovery, and secrets boundary", () => {
    expect(manifest.workload_id).toBe("mimir");
    expect(manifest.persistent_data).toBe("required");
    expect(manifest.backup_restore).toBe("required");
    expect(manifest.secrets_boundary).toBe("owner_overlay");
  });

  it("declares read-only preflight/verify and compensated mutating drain", () => {
    const byName = new Map(
      (manifest.hooks as Array<Record<string, JsonValue>>).map((hook) => [hook.name, hook]),
    );
    expect(byName.get("preflight")?.mode).toBe("read_only");
    expect(byName.get("verify")?.mode).toBe("read_only");
    const drain = byName.get("drain");
    expect(drain?.mode).toBe("mutating");
    expect(drain?.compensation_hook).toBe("compensate");
    expect(byName.get("compensate")?.mode).toBe("mutating");
    expect(byName.size).toBe((manifest.hooks as JsonValue[]).length);
  });
});

describe("relocation hook wrapper script", () => {
  const wrapper = read("scripts/relocation-verify.sh").toString("utf8");

  it("refuses mutating hooks and contains no mutating commands", () => {
    expect(wrapper).toContain("drain|compensate|rollback)");
    expect(wrapper).toContain("exit 2");
    expect(wrapper).not.toContain("systemctl stop");
    expect(wrapper).not.toContain("rsync ");
    expect(wrapper).not.toContain("rm -");
  });
});
