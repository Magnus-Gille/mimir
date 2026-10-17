import { constants } from "node:fs";
import { open, readFile, readdir, stat, lstat, realpath, mkdir, link, unlink } from "node:fs/promises";
import { spawn } from "node:child_process";
import { join, relative, dirname, resolve, sep } from "node:path";
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
  const resolvedRoot = resolve(rootDir);
  const full = resolve(resolvedRoot, relativePath);
  if (full !== resolvedRoot && !full.startsWith(resolvedRoot + sep)) {
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

const PRIVATE_DIRECTORY_MODE = 0o700;
const PRIVATE_FILE_MODE = 0o600;

function isWithin(parent: string, candidate: string): boolean {
  return candidate === parent || candidate.startsWith(parent + sep);
}

function errnoCode(error: unknown): string | undefined {
  return (error as NodeJS.ErrnoException).code;
}

async function getCurrentUid(): Promise<number> {
  if (typeof process.getuid !== "function") {
    throw new Error("cannot validate quarantine ownership on this platform");
  }
  return process.getuid();
}

async function stripAcl(handle: Awaited<ReturnType<typeof open>>): Promise<void> {
  if (process.platform !== "darwin") return;

  await new Promise<void>((resolvePromise, rejectPromise) => {
    const child = spawn("/bin/chmod", ["-N", "/dev/fd/3"], {
      stdio: ["ignore", "ignore", "pipe", handle.fd],
    });
    let stderr = "";
    child.stderr?.setEncoding("utf8");
    child.stderr?.on("data", (chunk: string) => {
      if (stderr.length < 1000) stderr += chunk.slice(0, 1000 - stderr.length);
    });
    child.once("error", (error) => {
      rejectPromise(new Error("failed to remove ACL from quarantine handle", { cause: error }));
    });
    child.once("close", (code, signal) => {
      if (code === 0) {
        resolvePromise();
      } else {
        rejectPromise(new Error(`failed to remove ACL from quarantine handle (exit=${code}, signal=${signal}): ${stderr.trim()}`));
      }
    });
  });
}

async function secureQuarantineRoot(directory: string, uid: number, sourceRoot: string): Promise<string> {
  const missing: string[] = [];
  let existing = directory;
  while (true) {
    try {
      const entry = await lstat(existing);
      if (existing === directory && entry.isSymbolicLink()) {
        throw new Error(`refusing symlink in quarantine path: ${directory}`);
      }
      break;
    } catch (error) {
      if (errnoCode(error) !== "ENOENT") throw error;
      missing.push(existing);
      const parent = dirname(existing);
      if (parent === existing) {
        throw new Error(`no existing ancestor for quarantine directory: ${directory}`, { cause: error });
      }
      existing = parent;
    }
  }

  const existingCanonical = await realpath(existing);
  const existingStats = await stat(existing);
  if (!existingStats.isDirectory()) throw new Error(`quarantine ancestor is not a directory: ${existing}`);
  const prospectiveRoot = join(existingCanonical, relative(existing, directory));
  if (isWithin(sourceRoot, prospectiveRoot)) {
    throw new Error(`refusing to quarantine into a directory inside root: quarantineDir=${directory}`);
  }

  if (missing.length === 0) {
    const canonical = await realpath(directory);
    if (isWithin(sourceRoot, canonical)) {
      throw new Error(`refusing quarantine alias inside servable root: ${directory}`);
    }
    await secureDirectory(directory, uid, false);
    return canonical;
  }

  for (const missingDirectory of missing.reverse()) {
    await secureDirectory(missingDirectory, uid, true);
  }
  const canonical = await realpath(directory);
  if (isWithin(sourceRoot, canonical)) {
    throw new Error(`refusing quarantine alias inside servable root: ${directory}`);
  }
  return canonical;
}

async function secureDirectory(directory: string, uid: number, create: boolean): Promise<void> {
  let entry;
  try {
    entry = await lstat(directory);
  } catch (error) {
    if (errnoCode(error) !== "ENOENT" || !create) throw error;
    await mkdir(directory, { mode: PRIVATE_DIRECTORY_MODE });
    entry = await lstat(directory);
  }

  if (entry.isSymbolicLink()) throw new Error(`refusing symlink in quarantine path: ${directory}`);
  if (!entry.isDirectory()) throw new Error(`quarantine path is not a directory: ${directory}`);
  if (entry.uid !== uid) throw new Error(`quarantine directory is not owned by current user: ${directory}`);

  const directoryHandle = await open(directory, constants.O_RDONLY | (constants.O_DIRECTORY ?? 0) | (constants.O_NOFOLLOW ?? 0));
  try {
    const opened = await directoryHandle.stat();
    if (!opened.isDirectory() || opened.uid !== uid || opened.dev !== entry.dev || opened.ino !== entry.ino) {
      throw new Error(`quarantine directory changed while being secured: ${directory}`);
    }
    await stripAcl(directoryHandle);
    await directoryHandle.chmod(PRIVATE_DIRECTORY_MODE);
  } finally {
    await directoryHandle.close();
  }
}

async function copyPrivately(sourceHandle: Awaited<ReturnType<typeof open>>, destination: string): Promise<void> {
  const destinationHandle = await open(
    destination,
    constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | (constants.O_NOFOLLOW ?? 0),
    PRIVATE_FILE_MODE,
  );
  let failure: unknown;
  try {
    await stripAcl(destinationHandle);
    await destinationHandle.chmod(PRIVATE_FILE_MODE);
    const buffer = Buffer.allocUnsafe(64 * 1024);
    let position = 0;
    while (true) {
      const { bytesRead } = await sourceHandle.read(buffer, 0, buffer.length, position);
      if (bytesRead === 0) break;
      let written = 0;
      while (written < bytesRead) {
        const result = await destinationHandle.write(buffer, written, bytesRead - written, position + written);
        if (result.bytesWritten === 0) throw new Error(`short write while copying into quarantine: ${destination}`);
        written += result.bytesWritten;
      }
      position += bytesRead;
    }
    await destinationHandle.sync();
  } catch (error) {
    failure = error;
  }
  try {
    await destinationHandle.close();
  } catch (closeError) {
    failure = failure === undefined ? closeError : new AggregateError([failure, closeError], "quarantine copy and close failed");
  }
  if (failure !== undefined) {
    try {
      await unlink(destination);
    } catch (cleanupError) {
      if (errnoCode(cleanupError) !== "ENOENT") {
        throw new AggregateError(
          [failure, cleanupError],
          `quarantine copy failed and partial destination could not be removed: ${destination}`,
          { cause: cleanupError },
        );
      }
    }
    throw failure;
  }
}

export async function quarantineFile(
  rootDir: string,
  quarantineDir: string,
  relativeFile: string,
): Promise<string> {
  const resolvedRoot = resolve(rootDir);
  const resolvedQuarantine = resolve(quarantineDir);
  const sourceRoot = await realpath(resolvedRoot);
  const src = resolveWithinRoot(rootDir, relativeFile);
  const sourceEntry = await lstat(src);
  if (sourceEntry.isSymbolicLink()) throw new Error(`refusing to quarantine a symbolic link: ${relativeFile}`);
  if (!sourceEntry.isFile()) throw new Error(`refusing to quarantine a non-regular file: ${relativeFile}`);
  const canonicalSource = await realpath(src);
  if (!isWithin(sourceRoot, canonicalSource) || canonicalSource === sourceRoot) {
    throw new Error(`refusing to quarantine a source outside root: ${relativeFile}`);
  }
  const uid = await getCurrentUid();
  if (sourceEntry.uid !== uid) throw new Error(`source file is not owned by current user: ${relativeFile}`);
  const canonicalQuarantine = await secureQuarantineRoot(resolvedQuarantine, uid, sourceRoot);

  const relativeDestination = relative(resolve(rootDir), src);
  const dest = join(resolvedQuarantine, relativeDestination);
  const destinationParent = dirname(dest);
  const directoryParts = relative(resolvedQuarantine, destinationParent).split(sep).filter(Boolean);
  let currentDirectory = resolvedQuarantine;
  for (const part of directoryParts) {
    currentDirectory = join(currentDirectory, part);
    await secureDirectory(currentDirectory, uid, true);
    const canonicalDirectory = await realpath(currentDirectory);
    if (!isWithin(canonicalQuarantine, canonicalDirectory) || isWithin(sourceRoot, canonicalDirectory)) {
      throw new Error(`quarantine directory resolves outside the private quarantine root: ${currentDirectory}`);
    }
  }

  const sourceHandle = await open(src, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0) | (constants.O_NONBLOCK ?? 0));
  try {
    const sourceStats = await sourceHandle.stat();
    if (!sourceStats.isFile()) throw new Error(`refusing to quarantine a non-regular file: ${relativeFile}`);
    if (sourceStats.uid !== uid) throw new Error(`source file is not owned by current user: ${relativeFile}`);
    if (sourceStats.dev !== sourceEntry.dev || sourceStats.ino !== sourceEntry.ino) {
      throw new Error(`source file changed while being opened: ${relativeFile}`);
    }
    await stripAcl(sourceHandle);
    await sourceHandle.chmod(PRIVATE_FILE_MODE);

    try {
      // link() is an exclusive same-filesystem move primitive: it cannot
      // replace an existing quarantine entry. Remove the source only after
      // confirming the new name refers to the protected source inode.
      await link(src, dest);
      const destinationStats = await lstat(dest);
      if (!destinationStats.isFile() || destinationStats.dev !== sourceStats.dev || destinationStats.ino !== sourceStats.ino) {
        throw new Error(`quarantine destination does not match protected source: ${dest}`);
      }
      await unlink(src);
    } catch (error) {
      if (errnoCode(error) !== "EXDEV") throw error;
      await copyPrivately(sourceHandle, dest);
      await unlink(src);
    }
  } finally {
    await sourceHandle.close();
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
