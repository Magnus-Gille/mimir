#!/usr/bin/env node
/**
 * Pi-side CLI for generating share URLs.
 *
 * Usage: node dist/cli/share.js <relative-path> [ttl]
 *
 * Reads MIMIR_SHARE_SECRET and MIMIR_ROOT_DIR from environment (same .env as server).
 * Verifies the file exists before generating a token.
 */

import { resolve } from "node:path";
import { statSync } from "node:fs";
import { generateToken, parseTTL } from "../share-token.js";

const SHARE_SECRET = process.env.MIMIR_SHARE_SECRET;
const ROOT_DIR = resolve(process.env.MIMIR_ROOT_DIR ?? "/home/magnus/mimir");
const BASE_URL = process.env.MIMIR_BASE_URL ?? "https://mimir.gille.ai";

function die(msg: string): never {
  process.stderr.write(`Error: ${msg}\n`);
  process.exit(1);
}

const [, , filePath, ttlStr = "24h"] = process.argv;

if (!filePath) {
  die("Usage: share <relative-path> [ttl]\n  Example: share presentations/deck.pdf 24h\n  TTL formats: 1h, 6h, 12h, 24h, 3d, 7d");
}

if (!SHARE_SECRET) {
  die("MIMIR_SHARE_SECRET is not set. Add it to .env.");
}

const ttlSeconds = parseTTL(ttlStr);
if (!ttlSeconds) {
  die(`Invalid TTL: ${ttlStr}. Use formats like 1h, 24h, 7d.`);
}

// Verify file exists at the resolved path
const fullPath = resolve(ROOT_DIR, filePath);
if (!fullPath.startsWith(ROOT_DIR + "/")) {
  die(`Path traversal detected: ${filePath}`);
}

try {
  const stats = statSync(fullPath);
  if (!stats.isFile()) {
    die(`Not a file: ${filePath}`);
  }
} catch {
  die(`File not found: ${fullPath}`);
}

const token = generateToken(filePath, ttlSeconds, SHARE_SECRET);
const url = `${BASE_URL}/share/${token}`;

// Output just the URL — the shell wrapper captures this
process.stdout.write(url + "\n");
