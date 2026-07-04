import { readFile, readdir, stat, lstat, rename, mkdir, copyFile, unlink } from "node:fs/promises";
import { join, relative, dirname, resolve } from "node:path";
import { pushPanel } from "./heimdall-report.js";

// --- Detector rules ---
//
// Same detector class as Munin's write-time scanner (known secret-format
// regexes + a generic quoted key=value fallback) — re-implemented locally,
// not imported, since Mimir and Munin are separate repos/deployments.

export interface SecretRule {
  name: string;
  pattern: RegExp;
}

export const SECRET_RULES: SecretRule[] = [
  { name: "aws-access-key-id", pattern: /AKIA[0-9A-Z]{16}/ },
  { name: "aws-secret-access-key", pattern: /aws_secret_access_key\s*[:=]\s*['"]?[A-Za-z0-9/+=]{40}['"]?/i },
  { name: "github-token", pattern: /gh[pousr]_[A-Za-z0-9]{36,255}/ },
  { name: "github-fine-grained-token", pattern: /github_pat_[A-Za-z0-9]{20,}_[A-Za-z0-9]{50,}/ },
  { name: "slack-token", pattern: /xox[baprs]-[0-9A-Za-z-]{10,72}/ },
  { name: "stripe-live-key", pattern: /sk_live_[0-9a-zA-Z]{16,}/ },
  { name: "google-api-key", pattern: /AIza[0-9A-Za-z_-]{35}/ },
  { name: "private-key-block", pattern: /-----BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----/ },
  { name: "jwt", pattern: /eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/ },
  {
    name: "generic-api-key-assignment",
    pattern: /(api[_-]?key|secret|token|password)\s*[:=]\s*['"][A-Za-z0-9_\-/+]{16,}['"]/i,
  },
];

export interface RuleHit {
  rule: string;
  line: number;
}

export interface SecretFinding extends RuleHit {
  file: string; // path relative to the scanned root
}

// --- Text / file scanning ---

const MAX_SCAN_BYTES = 10 * 1024 * 1024; // 10 MB — skip huge files (video, disk images)
const BINARY_SNIFF_BYTES = 8000;

function looksBinary(buf: Buffer): boolean {
  const len = Math.min(buf.length, BINARY_SNIFF_BYTES);
  for (let i = 0; i < len; i++) {
    if (buf[i] === 0) return true;
  }
  return false;
}

export function scanText(text: string): RuleHit[] {
  const hits: RuleHit[] = [];
  const lines = text.split("\n");
  lines.forEach((line, idx) => {
    for (const rule of SECRET_RULES) {
      if (rule.pattern.test(line)) {
        hits.push({ rule: rule.name, line: idx + 1 });
      }
    }
  });
  return hits;
}

export async function scanFile(filePath: string): Promise<RuleHit[]> {
  // Never follow symlinks: the root jail (resolveWithinRoot) only checks the
  // lexical path, so a symlink inside the tree could otherwise point at an
  // arbitrary file outside it and have its target read/scanned.
  const linkStats = await lstat(filePath);
  if (linkStats.isSymbolicLink()) return [];

  const stats = await stat(filePath);
  if (!stats.isFile()) return [];
  if (stats.size > MAX_SCAN_BYTES) {
    console.warn(`[mimir] secret-scan: skipping oversized file (${stats.size} bytes): ${filePath}`);
    return [];
  }
  const buf = await readFile(filePath);
  if (looksBinary(buf)) return [];
  return scanText(buf.toString("utf8"));
}

// Resolve a relative path against rootDir and refuse anything that escapes
// it — same jail pattern as index.ts's resolveFilePath.
function resolveWithinRoot(rootDir: string, relativePath: string): string {
  const full = resolve(rootDir, relativePath);
  if (full !== rootDir && !full.startsWith(rootDir + "/")) {
    throw new Error(`refusing to scan path outside root: ${relativePath}`);
  }
  return full;
}

/** Scan an explicit list of relative paths — used by the ingest hook so only
 * newly-imported files are scanned, not the whole archive on every sync. */
export async function scanFiles(rootDir: string, relativeFiles: string[]): Promise<SecretFinding[]> {
  const findings: SecretFinding[] = [];
  for (const relFile of relativeFiles) {
    const fullPath = resolveWithinRoot(rootDir, relFile);
    const hits = await scanFile(fullPath);
    for (const hit of hits) findings.push({ file: relFile, ...hit });
  }
  return findings;
}

/** Recursively scan every file under rootDir (used for full-tree audits). */
export async function scanDirectory(rootDir: string): Promise<SecretFinding[]> {
  const findings: SecretFinding[] = [];

  async function walk(dir: string): Promise<void> {
    const entries = await readdir(dir, { withFileTypes: true });
    for (const entry of entries) {
      const full = join(dir, entry.name);
      if (entry.isDirectory()) {
        await walk(full);
      } else if (entry.isFile()) {
        const hits = await scanFile(full);
        for (const hit of hits) findings.push({ file: relative(rootDir, full), ...hit });
      }
    }
  }

  await walk(rootDir);
  return findings;
}

// --- Quarantine ---

export async function quarantineFile(
  rootDir: string,
  quarantineDir: string,
  relativeFile: string,
): Promise<string> {
  const resolvedRoot = resolve(rootDir);
  const resolvedQuarantine = resolve(quarantineDir);
  // A quarantine dir inside (or equal to) rootDir would leave the "quarantined"
  // file just as Bearer-servable as before — defeats the whole point.
  if (resolvedQuarantine === resolvedRoot || resolvedQuarantine.startsWith(resolvedRoot + "/")) {
    throw new Error(
      `refusing to quarantine into a directory inside root: quarantineDir=${quarantineDir} rootDir=${rootDir}`,
    );
  }

  const src = resolveWithinRoot(rootDir, relativeFile);
  const dest = join(quarantineDir, relativeFile);
  await mkdir(dirname(dest), { recursive: true });
  try {
    await rename(src, dest);
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "EXDEV") {
      await copyFile(src, dest);
      await unlink(src);
    } else {
      throw err;
    }
  }
  return dest;
}

export interface ScanAndQuarantineResult {
  findings: SecretFinding[];
  quarantined: string[]; // relative paths that were moved to quarantine
}

/**
 * Scan (either the given relative files, or the whole tree if omitted) and
 * quarantine every flagged file out of rootDir. Findings never include the
 * matched text — only rule name, file, and line number.
 */
export async function scanAndQuarantine(
  rootDir: string,
  quarantineDir: string,
  relativeFiles?: string[],
): Promise<ScanAndQuarantineResult> {
  const findings = relativeFiles ? await scanFiles(rootDir, relativeFiles) : await scanDirectory(rootDir);

  const flaggedFiles = [...new Set(findings.map((f) => f.file))];
  const quarantined: string[] = [];
  for (const file of flaggedFiles) {
    await quarantineFile(rootDir, quarantineDir, file);
    quarantined.push(file);
  }

  return { findings, quarantined };
}

// --- Alert ---

const ALERT_PANEL = "secret-scan";

/**
 * Always logs loudly (stderr) so a hit is never silent, regardless of
 * Heimdall config. Additionally pushes a fail-state Heimdall panel when
 * HEIMDALL_HUB_URL/HEIMDALL_FLEET_TOKEN are set — the repo's existing
 * notification mechanism (see heimdall-report.ts, offsite-backup.sh).
 */
export async function alertSecretsFound(findings: SecretFinding[], quarantineDir: string): Promise<void> {
  const rules = [...new Set(findings.map((f) => f.rule))].join(", ");
  const fileCount = new Set(findings.map((f) => f.file)).size;
  const message = `${findings.length} secret(s) detected across ${fileCount} file(s) [${rules}] — quarantined to ${quarantineDir}`;

  console.error(`[mimir] SECRET SCAN ALERT: ${message}`);

  const hubUrl = process.env.HEIMDALL_HUB_URL;
  const token = process.env.HEIMDALL_FLEET_TOKEN;
  if (!hubUrl || !token) return;

  await pushPanel(hubUrl, token, {
    service: "mimir",
    panel: ALERT_PANEL,
    kind: "status",
    label: "Secret scan",
    state: "fail",
    message,
  });
}
