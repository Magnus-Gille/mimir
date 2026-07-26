import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const fixtures = path.join(root, "tests/fixtures/node-substrate-contract");
const read = (name) => JSON.parse(fs.readFileSync(path.join(fixtures, name), "utf8"));
const schema = JSON.parse(fs.readFileSync(path.join(root, "docs/node-substrate-contract-v1.schema.json"), "utf8"));
const positive = read("positive.json");
const partialDrain = read("partial-drain.json");
const partialSubstrate = read("partial-substrate.json");
const negative = read("negative.json");
const consumers = read("consumer-fixture-set.json");
const id = /^[a-z][a-z0-9-]{2,62}$/;
const digest = /^sha256:[a-f0-9]{64}$/;
const utc = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
const fail = (message) => { throw new Error(message); };
const canonical = (value) => JSON.stringify(value, Object.keys(value ?? {}).sort());
const plain = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const typeMatches = (type, value) => ({ object: plain(value), array: Array.isArray(value), string: typeof value === "string", integer: Number.isInteger(value), boolean: typeof value === "boolean", null: value === null })[type];
const dateTime = (value) => {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$/.exec(value);
  if (!match) return false;
  const instant = new Date(value);
  if (Number.isNaN(instant.getTime())) return false;
  const [, year, month, day, hour, minute, second] = match;
  return instant.getUTCFullYear() === Number(year) && instant.getUTCMonth() + 1 === Number(month) && instant.getUTCDate() === Number(day) && instant.getUTCHours() === Number(hour) && instant.getUTCMinutes() === Number(minute) && instant.getUTCSeconds() === Number(second);
};
const resolve = (ref) => {
  if (!ref.startsWith("#/")) fail(`unsupported external schema ref ${ref}`);
  return ref.slice(2).split("/").reduce((value, raw) => value?.[raw.replaceAll("~1", "/").replaceAll("~0", "~")], schema);
};
const keywordSet = new Set(["$schema", "$id", "$defs", "$ref", "title", "description", "oneOf", "const", "enum", "type", "minLength", "pattern", "format", "minimum", "minItems", "uniqueItems", "items", "required", "properties", "additionalProperties"]);
function checkSchema(node, at = "$") {
  if (typeof node === "boolean") return;
  assert.ok(plain(node), `schema node must be an object at ${at}`);
  for (const key of Object.keys(node)) assert.ok(keywordSet.has(key), `unsupported JSON Schema keyword ${key} at ${at}`);
  if (node.$ref) { assert.ok(resolve(node.$ref), `unresolved ref ${node.$ref}`); assert.deepEqual(Object.keys(node).filter((key) => key !== "$ref" && !["title", "description"].includes(key)), [], `$ref siblings unsupported at ${at}`); }
  if (node.type) assert.ok(["object", "array", "string", "integer", "boolean", "null"].includes(node.type), `unsupported type at ${at}`);
  if (node.format) assert.equal(node.format, "date-time", `unsupported format at ${at}`);
  if (node.additionalProperties !== undefined) assert.equal(typeof node.additionalProperties, "boolean", `additionalProperties must be boolean at ${at}`);
  for (const [key, child] of Object.entries(node.properties ?? {})) checkSchema(child, `${at}.properties.${key}`);
  for (const [key, child] of Object.entries(node.$defs ?? {})) checkSchema(child, `${at}.$defs.${key}`);
  if (node.items) checkSchema(node.items, `${at}.items`);
  for (const [index, child] of (node.oneOf ?? []).entries()) checkSchema(child, `${at}.oneOf[${index}]`);
}
function schemaErrors(node, value, at = "$") {
  if (node === true) return [];
  if (node === false) return [`${at}: forbidden`];
  if (node.$ref) return schemaErrors(resolve(node.$ref), value, at);
  if (node.oneOf) {
    const attempts = node.oneOf.map((child) => schemaErrors(child, value, at));
    return attempts.filter((errors) => errors.length === 0).length === 1 ? [] : [`${at}: expected exactly one branch (${attempts.flat().join("; ")})`];
  }
  const errors = [];
  if (Object.hasOwn(node, "const") && canonical(value) !== canonical(node.const)) errors.push(`${at}: const mismatch`);
  if (node.enum && !node.enum.some((candidate) => canonical(candidate) === canonical(value))) errors.push(`${at}: enum mismatch`);
  if (node.type && !typeMatches(node.type, value)) return [...errors, `${at}: expected ${node.type}`];
  if (typeof value === "string") { if (node.minLength !== undefined && value.length < node.minLength) errors.push(`${at}: minLength`); if (node.pattern && !new RegExp(node.pattern).test(value)) errors.push(`${at}: pattern`); if (node.format === "date-time" && !utc.test(value)) errors.push(`${at}: date-time`); }
  if (typeof value === "number" && node.minimum !== undefined && value < node.minimum) errors.push(`${at}: minimum`);
  if (Array.isArray(value)) { if (node.minItems !== undefined && value.length < node.minItems) errors.push(`${at}: minItems`); if (node.uniqueItems && new Set(value.map(canonical)).size !== value.length) errors.push(`${at}: duplicate items`); if (node.items) value.forEach((item, index) => errors.push(...schemaErrors(node.items, item, `${at}[${index}]`))); }
  if (plain(value)) {
    for (const field of node.required ?? []) if (!Object.hasOwn(value, field)) errors.push(`${at}.${field}: required`);
    for (const [field, child] of Object.entries(node.properties ?? {})) if (Object.hasOwn(value, field)) errors.push(...schemaErrors(child, value[field], `${at}.${field}`));
    if (node.additionalProperties === false) for (const field of Object.keys(value)) if (!Object.hasOwn(node.properties ?? {}, field)) errors.push(`${at}.${field}: additional property`);
  }
  return errors;
}
const schemaValid = (record, label) => assert.deepEqual(schemaErrors(schema, record), [], `${label} violates the normative v1 schema`);
const schemaInvalid = (record, label) => assert.notDeepEqual(schemaErrors(schema, record), [], `${label} must be rejected by the normative schema`);
const fields = (value, names, label) => { if (!plain(value)) fail(`${label} must be object`); for (const name of names) if (!Object.hasOwn(value, name)) fail(`${label}.${name} missing`); };
const exact = (value, names, label) => { fields(value, names, label); for (const key of Object.keys(value)) if (!names.includes(key)) fail(`${label}.${key} is not a v1 field`); };
const strings = (value, label) => { if (!Array.isArray(value) || !value.every((item) => typeof item === "string")) fail(`${label} must be string array`); };
function rejectPrivate(value, label = "$") {
  if (typeof value === "string") { if (/(?:\b(?:10|127|192\.168)\.|\b172\.(?:1[6-9]|2\d|3[01])\.)|\/Users\/|\.ssh\/|password=|token=/i.test(value)) fail(`${label} contains private locator or credential-like data`); return; }
  if (Array.isArray(value)) return value.forEach((item, index) => rejectPrivate(item, `${label}[${index}]`));
  if (plain(value)) for (const [key, item] of Object.entries(value)) { if (/^(?:wifi_?ssid|ssid|wifi_?name|credential|token|password)$/i.test(key)) fail(`${label}.${key} is private identity material`); rejectPrivate(item, `${label}.${key}`); }
}
function extensions(value, label) { if (!Array.isArray(value)) fail(`${label}.extensions must be array`); const seen = new Set(); for (const item of value) { exact(item, ["id", "version", "decision_effect"], `${label}.extension`); if (!id.test(item.id) || !/^v[1-9][0-9]*$/.test(item.version) || item.decision_effect !== "informational" || seen.has(item.id)) fail(`${label}.extension invalid or decision-driving`); seen.add(item.id); } }
function node(record, now) {
  exact(record, ["kind", "schema_version", "node_id", "observed_at", "valid_until", "evidence", "capability_status", "architecture", "resources", "uptime_class", "network_capabilities", "logical_storage", "service_manager", "deployment_mechanisms", "health_reporting", "extensions"], "node");
  if (!id.test(record.node_id) || !dateTime(record.observed_at) || !dateTime(record.valid_until) || Date.parse(record.valid_until) <= Date.parse(record.observed_at) || Date.parse(record.valid_until) <= now) fail("node stale/invalid identity or timestamp");
  exact(record.evidence, ["evidence_id", "producer", "observed_at", "digest"], "node.evidence"); if (!id.test(record.evidence.evidence_id) || record.evidence.producer !== "brokkr" || record.evidence.observed_at !== record.observed_at || !dateTime(record.evidence.observed_at) || !digest.test(record.evidence.digest)) fail("node evidence must be Brokkr-bound to observation");
  exact(record.resources, ["cpu_cores", "memory_mib"], "node.resources"); if (![record.resources.cpu_cores, record.resources.memory_mib].every((value) => Number.isInteger(value) && value > 0) || record.capability_status !== "known" || !["arm64", "x86_64"].includes(record.architecture)) fail("node cannot drive placement");
  strings(record.network_capabilities, "node.network_capabilities"); for (const item of record.logical_storage) { exact(item, ["class", "available_mib", "status"], "node.logical_storage"); if (!Number.isInteger(item.available_mib) || item.available_mib < 0) fail("invalid storage"); } extensions(record.extensions, "node");
}
function workload(record) {
  exact(record, ["kind", "schema_version", "workload_id", "supported_architectures", "persistent_data", "ports", "units", "timers", "secrets_boundary", "dependencies", "backup_restore", "health", "hooks", "extensions"], "workload");
  if (!id.test(record.workload_id) || !Array.isArray(record.supported_architectures) || !record.supported_architectures.length || [record.persistent_data, record.secrets_boundary, record.backup_restore].some((value) => ["unknown", "not_applicable"].includes(value))) fail("unknown workload requirement fails closed");
  const hookNames = new Set(); for (const hook of record.hooks) { exact(hook, ["name", "mode", "contract_versions", "deadline_seconds", "idempotency_required", ...(hook.compensation_hook ? ["compensation_hook"] : [])], "workload.hook"); if (hookNames.has(hook.name) || !Array.isArray(hook.contract_versions) || !hook.contract_versions.includes("v1") || !Number.isInteger(hook.deadline_seconds) || hook.deadline_seconds < 1 || hook.idempotency_required !== true) fail("duplicate or invalid workload hook"); hookNames.add(hook.name); }
  if (!["preflight", "verify", "drain"].every((name) => hookNames.has(name)) || record.hooks.find((hook) => hook.name === "preflight")?.mode !== "read_only" || record.hooks.find((hook) => hook.name === "verify")?.mode !== "read_only") fail("required read-only or drain hook missing");
  const drain = record.hooks.find((hook) => hook.name === "drain"); if (drain.mode !== "mutating" || !["rollback", "compensate"].includes(drain.compensation_hook) || !hookNames.has(drain.compensation_hook)) fail("drain requires compensation"); extensions(record.extensions, "workload");
}
function placement(record) { exact(record, ["kind", "schema_version", "placement_id", "workload_id", "target_node_id", "desired_revision", "created_at", "planned_drift", "extensions"], "placement"); if (![record.placement_id, record.workload_id, record.target_node_id].every((value) => id.test(value)) || !digest.test(record.desired_revision) || !dateTime(record.created_at)) fail("invalid placement binding"); extensions(record.extensions, "placement"); }
function lifecycle(record) {
  exact(record, ["kind", "schema_version", "result_id", "attempt_id", "plan_id", "plan_digest", "desired_revision", "observation_evidence_id", "action", "deadline", "idempotency_key", "phase", "outcome", "drift", "hook_results", "substrate", "created_at", "extensions"], "lifecycle");
  if (!["result_id", "attempt_id", "plan_id", "observation_evidence_id", "idempotency_key"].every((key) => id.test(record[key])) || !["plan_digest", "desired_revision"].every((key) => digest.test(record[key])) || !dateTime(record.deadline) || !dateTime(record.created_at) || Date.parse(record.created_at) > Date.parse(record.deadline)) fail("invalid lifecycle binding");
  const resultIds = new Set(); for (const hook of record.hook_results) { exact(hook, ["result_id", "hook", "attempt_id", "plan_id", "plan_digest", "desired_revision", "observation_evidence_id", "action", "deadline", "idempotency_key", "outcome"], "hook_result"); if (!id.test(hook.result_id) || resultIds.has(hook.result_id) || !dateTime(hook.deadline) || !["attempt_id", "plan_id", "plan_digest", "desired_revision", "observation_evidence_id", "action", "deadline", "idempotency_key"].every((key) => hook[key] === record[key])) fail("hook replay/idempotency binding mismatch"); resultIds.add(hook.result_id); }
  exact(record.substrate, ["outcome", "rollback", "pre_state_evidence_id"], "substrate"); if (!id.test(record.substrate.pre_state_evidence_id)) fail("invalid substrate pre-state evidence");
  const partial = record.hook_results.some((hook) => hook.outcome === "partial"); if (partial && (record.outcome !== "blocked" || record.phase !== "compensate" || !record.hook_results.some((hook) => ["compensate", "rollback"].includes(hook.hook) && hook.outcome === "success") || !record.hook_results.some((hook) => hook.hook === "verify" && hook.outcome === "success"))) fail("partial drain must compensate and verify baseline");
  if (record.substrate.outcome === "partial" && (record.outcome !== "blocked" || record.phase !== "substrate_rollback" || record.substrate.rollback !== "verified")) fail("partial substrate must roll back");
  if (record.outcome === "promoted" && (!["planned", "none"].includes(record.drift) || record.phase !== "verify" || record.substrate.outcome !== "success" || !record.hook_results.some((hook) => hook.hook === "verify" && hook.outcome === "success"))) fail("promotion lacks verified plan"); extensions(record.extensions, "lifecycle");
}
function semantic(record, now = Date.parse("2026-07-23T10:15:00Z")) { rejectPrivate(record); if (record.schema_version !== "v1") fail("unsupported version"); if (record.kind === "node-capability") return node(record, now); if (record.kind === "workload-requirement") return workload(record); if (record.kind === "placement-intent") return placement(record); if (record.kind === "lifecycle-result") return lifecycle(record); fail("unknown record kind"); }
const reject = (fn, label) => assert.throws(fn, undefined, label);

checkSchema(schema);
assert.deepEqual(consumers.consumers.sort(), ["brokkr", "hugin", "mimir"]);
for (const record of [...positive.records, ...partialDrain.records, ...partialSubstrate.records]) { schemaValid(record, record.kind); semantic(record, Date.parse(record.created_at ?? record.observed_at)); }
schemaInvalid(negative.schema_unsupported_version, "unsupported-version fixture");
for (const record of [negative.duplicate_hook_workload, negative.replayed_lifecycle]) schemaValid(record, "schema-valid semantic-negative fixture");
reject(() => semantic(negative.duplicate_hook_workload), "duplicate hook names");
reject(() => semantic(negative.replayed_lifecycle), "attempt-bound replay/idempotency mismatch");
schemaValid(negative.impossible_date_node, "schema-valid impossible calendar date");
reject(() => semantic(negative.impossible_date_node), "impossible calendar date");
schemaValid(negative.unknown_drift_promotion, "schema-valid unknown-drift promotion");
reject(() => semantic(negative.unknown_drift_promotion), "unknown drift promotion");
reject(() => rejectPrivate(negative.privacy_adversarial), "privacy adversarial fixture");
const incompatibleNode = positive.records[0]; const incompatibleWorkload = structuredClone(positive.records[1]); incompatibleWorkload.supported_architectures = ["x86_64"]; schemaValid(incompatibleNode, "incompatible node"); schemaValid(incompatibleWorkload, "incompatible workload"); reject(() => { if (!incompatibleWorkload.supported_architectures.includes(incompatibleNode.architecture)) fail("incompatible placement"); }, "incompatible placement");
const stale = structuredClone(positive.records[0]); stale.observed_at = "2026-07-23T08:00:00Z"; stale.valid_until = "2026-07-23T09:00:00Z"; stale.evidence.observed_at = stale.observed_at; schemaValid(stale, "stale schema-valid node"); reject(() => semantic(stale, Date.parse("2026-07-23T10:15:00Z")), "stale evidence");
console.log("Node/substrate v1 normative schema plus 10 hermetic fixture scenarios validated.");
