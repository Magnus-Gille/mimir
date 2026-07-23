/**
 * Hermetic consumer of the Grimnir node/substrate contract v1.
 *
 * The normative schema and shared consumer fixture manifest are vendored
 * byte-exact under docs/vendor/grimnir/ from the immutable source revision
 * below. Loading verifies the SHA-256 pins so silent drift (local edits or a
 * re-vendor from another revision) fails closed. The JSON Schema subset
 * evaluator mirrors Grimnir's normative validator semantics
 * (tests/scripts/validate-node-substrate-contract.mjs at the same revision);
 * it is re-implemented locally, not imported across repos, per the same
 * convention as src/secret-scan.ts.
 */
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

export const GRIMNIR_SOURCE_REPOSITORY = "Magnus-Gille/grimnir";
export const GRIMNIR_SOURCE_REVISION = "6d54d49c91612eae7dce5f66286d801900c38c35";
export const GRIMNIR_SCHEMA_SOURCE_PATH = "docs/node-substrate-contract-v1.schema.json";
export const GRIMNIR_SCHEMA_SHA256 =
  "9a69f1b23499cd6e70fdaa80ee57bf983e7e5b288882e0cf2b0f01f10824fbbe";
export const GRIMNIR_FIXTURE_SET_SOURCE_PATH =
  "tests/fixtures/node-substrate-contract/consumer-fixture-set.json";
export const GRIMNIR_FIXTURE_SET_SHA256 =
  "355481f2b3866840795ba18033077d6f36487d1a447b36c323384cf7837c5fcb";

export const VENDORED_SCHEMA_PATH = fileURLToPath(
  new URL("../docs/vendor/grimnir/node-substrate-contract-v1.schema.json", import.meta.url),
);
export const VENDORED_FIXTURE_SET_PATH = fileURLToPath(
  new URL("../docs/vendor/grimnir/consumer-fixture-set.json", import.meta.url),
);

export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

export function sha256Hex(data: string | Buffer): string {
  return createHash("sha256").update(data).digest("hex");
}

/** Read a vendored contract artifact and fail closed on digest drift. */
export function loadVendoredJson(path: string, expectedSha256: string): JsonValue {
  const bytes = readFileSync(path);
  const actual = sha256Hex(bytes);
  if (actual !== expectedSha256) {
    throw new Error(
      `vendored contract artifact drifted from its pinned revision ` +
        `(expected sha256:${expectedSha256}, got sha256:${actual})`,
    );
  }
  return JSON.parse(bytes.toString("utf8")) as JsonValue;
}

export function loadNormativeSchema(): Record<string, JsonValue> {
  const schema = loadVendoredJson(VENDORED_SCHEMA_PATH, GRIMNIR_SCHEMA_SHA256);
  if (!isPlainObject(schema)) throw new Error("vendored schema is not an object");
  return schema;
}

export function loadConsumerFixtureSet(): Record<string, JsonValue> {
  const manifest = loadVendoredJson(VENDORED_FIXTURE_SET_PATH, GRIMNIR_FIXTURE_SET_SHA256);
  if (!isPlainObject(manifest)) throw new Error("vendored fixture manifest is not an object");
  return manifest;
}

// ---------------------------------------------------------------------------
// Normative JSON Schema subset evaluator (mirrors Grimnir's validator).
// ---------------------------------------------------------------------------

const EXACT_UTC = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

/** Exact second-resolution UTC timestamp on a real calendar date. */
export function isExactUtc(value: string): boolean {
  const match = EXACT_UTC.exec(value);
  if (!match) return false;
  const instant = new Date(value);
  if (Number.isNaN(instant.getTime())) return false;
  const [year, month, day] = value.slice(0, 10).split("-").map(Number);
  const [hour, minute, second] = value.slice(11, 19).split(":").map(Number);
  return (
    instant.getUTCFullYear() === year &&
    instant.getUTCMonth() + 1 === month &&
    instant.getUTCDate() === day &&
    instant.getUTCHours() === hour &&
    instant.getUTCMinutes() === minute &&
    instant.getUTCSeconds() === second
  );
}

export function isPlainObject(value: unknown): value is Record<string, JsonValue> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function canonical(value: JsonValue | undefined): string {
  if (!isPlainObject(value)) return JSON.stringify(value);
  return JSON.stringify(value, Object.keys(value).sort());
}

function typeMatches(type: string, value: JsonValue | undefined): boolean {
  switch (type) {
    case "object":
      return isPlainObject(value);
    case "array":
      return Array.isArray(value);
    case "string":
      return typeof value === "string";
    case "integer":
      return Number.isInteger(value);
    case "boolean":
      return typeof value === "boolean";
    case "null":
      return value === null;
    default:
      return false;
  }
}

function resolveRef(root: Record<string, JsonValue>, ref: string): JsonValue {
  if (!ref.startsWith("#/")) throw new Error(`unsupported external schema ref ${ref}`);
  let node: JsonValue | undefined = root;
  for (const raw of ref.slice(2).split("/")) {
    const key = raw.replaceAll("~1", "/").replaceAll("~0", "~");
    if (!isPlainObject(node)) throw new Error(`unresolved ref ${ref}`);
    node = node[key];
  }
  if (node === undefined) throw new Error(`unresolved ref ${ref}`);
  return node;
}

const SUPPORTED_KEYWORDS = new Set([
  "$schema", "$id", "$defs", "$ref", "title", "description", "oneOf", "const",
  "enum", "type", "minLength", "pattern", "format", "minimum", "minItems",
  "uniqueItems", "items", "required", "properties", "additionalProperties",
]);

/**
 * Assert the vendored schema uses only the keyword subset this evaluator
 * implements, so a schema revision cannot silently weaken validation.
 */
export function checkSchemaSupported(node: JsonValue, at = "$"): void {
  if (typeof node === "boolean") return;
  if (!isPlainObject(node)) throw new Error(`schema node must be an object at ${at}`);
  for (const key of Object.keys(node)) {
    if (!SUPPORTED_KEYWORDS.has(key)) {
      throw new Error(`unsupported JSON Schema keyword ${key} at ${at}`);
    }
  }
  if (typeof node.$ref === "string") {
    const siblings = Object.keys(node).filter(
      (key) => key !== "$ref" && key !== "title" && key !== "description",
    );
    if (siblings.length > 0) throw new Error(`$ref siblings unsupported at ${at}`);
  }
  if (node.type !== undefined) {
    if (
      typeof node.type !== "string" ||
      !["object", "array", "string", "integer", "boolean", "null"].includes(node.type)
    ) {
      throw new Error(`unsupported type at ${at}`);
    }
  }
  if (node.format !== undefined && node.format !== "date-time") {
    throw new Error(`unsupported format at ${at}`);
  }
  if (node.additionalProperties !== undefined && typeof node.additionalProperties !== "boolean") {
    throw new Error(`additionalProperties must be boolean at ${at}`);
  }
  if (isPlainObject(node.properties)) {
    for (const [key, child] of Object.entries(node.properties)) {
      checkSchemaSupported(child, `${at}.properties.${key}`);
    }
  }
  if (isPlainObject(node.$defs)) {
    for (const [key, child] of Object.entries(node.$defs)) {
      checkSchemaSupported(child, `${at}.$defs.${key}`);
    }
  }
  if (node.items !== undefined) checkSchemaSupported(node.items, `${at}.items`);
  if (Array.isArray(node.oneOf)) {
    node.oneOf.forEach((child, index) => checkSchemaSupported(child, `${at}.oneOf[${index}]`));
  }
}

/**
 * Validate a value against a node of the normative schema. Returns an array
 * of error strings; empty means valid. `root` is the whole vendored schema
 * document so local `$ref`s resolve.
 */
export function schemaErrors(
  root: Record<string, JsonValue>,
  node: JsonValue,
  value: JsonValue | undefined,
  at = "$",
): string[] {
  if (node === true) return [];
  if (node === false) return [`${at}: forbidden`];
  if (!isPlainObject(node)) return [`${at}: invalid schema node`];
  if (typeof node.$ref === "string") return schemaErrors(root, resolveRef(root, node.$ref), value, at);
  if (Array.isArray(node.oneOf)) {
    const attempts = node.oneOf.map((child) => schemaErrors(root, child, value, at));
    const passing = attempts.filter((errors) => errors.length === 0).length;
    return passing === 1
      ? []
      : [`${at}: expected exactly one branch (${attempts.flat().join("; ")})`];
  }
  const errors: string[] = [];
  if (Object.hasOwn(node, "const") && canonical(value) !== canonical(node.const)) {
    errors.push(`${at}: const mismatch`);
  }
  if (Array.isArray(node.enum) && !node.enum.some((candidate) => canonical(candidate) === canonical(value))) {
    errors.push(`${at}: enum mismatch`);
  }
  if (typeof node.type === "string" && !typeMatches(node.type, value)) {
    return [...errors, `${at}: expected ${node.type}`];
  }
  if (typeof value === "string") {
    if (typeof node.minLength === "number" && value.length < node.minLength) {
      errors.push(`${at}: minLength`);
    }
    if (typeof node.pattern === "string" && !new RegExp(node.pattern).test(value)) {
      errors.push(`${at}: pattern`);
    }
    if (node.format === "date-time" && !EXACT_UTC.test(value)) errors.push(`${at}: date-time`);
  }
  if (typeof value === "number" && typeof node.minimum === "number" && value < node.minimum) {
    errors.push(`${at}: minimum`);
  }
  if (Array.isArray(value)) {
    if (typeof node.minItems === "number" && value.length < node.minItems) {
      errors.push(`${at}: minItems`);
    }
    if (node.uniqueItems && new Set(value.map(canonical)).size !== value.length) {
      errors.push(`${at}: duplicate items`);
    }
    if (node.items !== undefined) {
      value.forEach((item, index) =>
        errors.push(...schemaErrors(root, node.items as JsonValue, item, `${at}[${index}]`)),
      );
    }
  }
  if (isPlainObject(value)) {
    if (Array.isArray(node.required)) {
      for (const field of node.required) {
        if (typeof field === "string" && !Object.hasOwn(value, field)) {
          errors.push(`${at}.${field}: required`);
        }
      }
    }
    const properties = isPlainObject(node.properties) ? node.properties : {};
    for (const [field, child] of Object.entries(properties)) {
      if (Object.hasOwn(value, field)) {
        errors.push(...schemaErrors(root, child, value[field], `${at}.${field}`));
      }
    }
    if (node.additionalProperties === false) {
      for (const field of Object.keys(value)) {
        if (!Object.hasOwn(properties, field)) errors.push(`${at}.${field}: additional property`);
      }
    }
  }
  return errors;
}

/** Validate a record against a named `$defs` entry of the normative schema. */
export function definitionErrors(
  root: Record<string, JsonValue>,
  ref: string,
  value: JsonValue,
): string[] {
  return schemaErrors(root, resolveRef(root, ref), value);
}
