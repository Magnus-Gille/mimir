import { constants, type BigIntStats, type Dirent } from "node:fs";
import { open, readdir, stat, lstat, realpath, mkdir, link, unlink } from "node:fs/promises";
import { spawn } from "node:child_process";
import { join, relative, dirname, resolve, sep } from "node:path";
import { TextDecoder } from "node:util";
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

export const SCAN_INCOMPLETE_REASONS = [
  "binary",
  "oversized",
  "symlink",
  "nonregular",
  "io-failure",
  "unsupported-encoding",
] as const;

export type ScanIncompleteReason = typeof SCAN_INCOMPLETE_REASONS[number];

export type ScanFileVerdict =
  | { status: "clean" }
  | { status: "findings"; findings: RuleHit[] }
  | { status: "incomplete"; reason: ScanIncompleteReason };

export type ScanEntryResult =
  | { file: string; status: "clean" }
  | { file: string; status: "findings"; findings: SecretFinding[] }
  | { file: string; status: "incomplete"; reason: ScanIncompleteReason };

export interface ScanIncomplete {
  file: string;
  reason: ScanIncompleteReason;
}

export interface ScanBatchResult {
  entries: ScanEntryResult[];
  findings: SecretFinding[];
  incomplete: ScanIncomplete[];
}

// --- Text / file scanning ---

const MAX_SCAN_BYTES = 10 * 1024 * 1024; // 10 MB — incomplete for huge files (video, disk images)

function looksBinary(buf: Buffer): boolean {
  return buf.includes(0);
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

export async function scanFile(filePath: string): Promise<ScanFileVerdict> {
  let pathStats: BigIntStats;
  try {
    pathStats = await lstat(filePath, { bigint: true });
  } catch {
    return { status: "incomplete", reason: "io-failure" };
  }

  if (pathStats.isSymbolicLink()) return { status: "incomplete", reason: "symlink" };
  if (!pathStats.isFile()) return { status: "incomplete", reason: "nonregular" };
  if (pathStats.size > BigInt(MAX_SCAN_BYTES)) {
    console.warn(`[mimir] secret-scan: skipping oversized file (${pathStats.size} bytes): ${filePath}`);
    return { status: "incomplete", reason: "oversized" };
  }

  let handle: Awaited<ReturnType<typeof open>> | undefined;
  let verdict: ScanFileVerdict = { status: "incomplete", reason: "io-failure" };
  try {
    // O_NONBLOCK prevents a path swapped to a FIFO between lstat and open
    // from hanging the scanner; O_NOFOLLOW prevents following a final symlink.
    handle = await open(
      filePath,
      constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0) | (constants.O_NONBLOCK ?? 0),
    );
    const openedStats = await handle.stat({ bigint: true });
    if (!openedStats.isFile()) {
      verdict = { status: "incomplete", reason: "nonregular" };
    } else if (openedStats.dev !== pathStats.dev || openedStats.ino !== pathStats.ino) {
      verdict = { status: "incomplete", reason: "io-failure" };
    } else if (openedStats.size > BigInt(MAX_SCAN_BYTES)) {
      verdict = { status: "incomplete", reason: "oversized" };
    } else {
      // Read at most the cap plus one byte so a file that grows after lstat
      // cannot make the scanner allocate without bound or be treated as clean.
      const buffer = Buffer.allocUnsafe(Math.min(Number(openedStats.size) + 1, MAX_SCAN_BYTES + 1));
      let length = 0;
      while (length < buffer.length) {
        const { bytesRead } = await handle.read(buffer, length, buffer.length - length, null);
        if (bytesRead === 0) break;
        length += bytesRead;
      }

      if (length > MAX_SCAN_BYTES) {
        verdict = { status: "incomplete", reason: "oversized" };
      } else {
        const finalStats = await handle.stat({ bigint: true });
        if (finalStats.size > BigInt(MAX_SCAN_BYTES)) {
          verdict = { status: "incomplete", reason: "oversized" };
        } else if (
          finalStats.size !== openedStats.size
          || finalStats.mtimeNs !== openedStats.mtimeNs
          || finalStats.ctimeNs !== openedStats.ctimeNs
          || BigInt(length) !== finalStats.size
        ) {
          verdict = { status: "incomplete", reason: "io-failure" };
        } else {
          const contents = buffer.subarray(0, length);
          if (looksBinary(contents)) {
            verdict = { status: "incomplete", reason: "binary" };
          } else {
            let text: string | undefined;
            try {
              text = new TextDecoder("utf-8", { fatal: true }).decode(contents);
            } catch {
              verdict = { status: "incomplete", reason: "unsupported-encoding" };
            }
            if (text !== undefined) {
              const findings = scanText(text);
              verdict = findings.length > 0 ? { status: "findings", findings } : { status: "clean" };
            }
          }
        }
      }
    }
  } catch (error) {
    const code = errnoCode(error);
    if (code === "ELOOP") {
      verdict = { status: "incomplete", reason: "symlink" };
    } else if (code === "EISDIR") {
      verdict = { status: "incomplete", reason: "nonregular" };
    } else {
      verdict = { status: "incomplete", reason: "io-failure" };
    }
  }

  if (handle) {
    try {
      await handle.close();
    } catch {
      return { status: "incomplete", reason: "io-failure" };
    }
  }
  return verdict;
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
function recordVerdict(result: ScanBatchResult, file: string, verdict: ScanFileVerdict): void {
  if (verdict.status === "clean") {
    result.entries.push({ file, status: "clean" });
    return;
  }
  if (verdict.status === "incomplete") {
    const incomplete = { file, reason: verdict.reason };
    result.entries.push({ file, status: "incomplete", reason: verdict.reason });
    result.incomplete.push(incomplete);
    return;
  }

  const findings = verdict.findings.map((finding) => ({ file, ...finding }));
  result.entries.push({ file, status: "findings", findings });
  result.findings.push(...findings);
}

function emptyScanResult(): ScanBatchResult {
  return { entries: [], findings: [], incomplete: [] };
}

async function scanRelativeFile(rootDir: string, relativeFile: string): Promise<ScanFileVerdict> {
  const root = resolve(rootDir);
  let fullPath: string;
  try {
    fullPath = resolveWithinRoot(root, relativeFile);
  } catch {
    // Keep the lexical root check fail-closed while retaining this input in the
    // batch inventory instead of aborting the remaining requested paths.
    return { status: "incomplete", reason: "io-failure" };
  }
  let rootStats: BigIntStats;
  try {
    rootStats = await lstat(root, { bigint: true });
  } catch {
    return { status: "incomplete", reason: "io-failure" };
  }
  if (rootStats.isSymbolicLink()) return { status: "incomplete", reason: "symlink" };
  if (!rootStats.isDirectory()) return { status: "incomplete", reason: "nonregular" };

  const parentParts = relative(root, dirname(fullPath)).split(sep).filter(Boolean);
  let current = root;

  for (const part of parentParts) {
    current = join(current, part);
    let parentStats: BigIntStats;
    try {
      parentStats = await lstat(current, { bigint: true });
    } catch {
      return { status: "incomplete", reason: "io-failure" };
    }
    if (parentStats.isSymbolicLink()) return { status: "incomplete", reason: "symlink" };
    if (!parentStats.isDirectory()) return { status: "incomplete", reason: "nonregular" };
  }

  return scanFile(fullPath);
}

export async function scanFiles(rootDir: string, relativeFiles: string[]): Promise<ScanBatchResult> {
  const result = emptyScanResult();
  for (const relFile of relativeFiles) {
    const verdict = await scanRelativeFile(rootDir, relFile);
    recordVerdict(result, relFile, verdict);
  }
  return result;
}

/** Recursively scan every file under rootDir (used for full-tree audits). */
export async function scanDirectory(rootDir: string): Promise<ScanBatchResult> {
  const result = emptyScanResult();
  const resolvedRoot = resolve(rootDir);

  async function walk(dir: string, relativeDir: string): Promise<void> {
    let directoryStats: BigIntStats;
    try {
      directoryStats = await lstat(dir, { bigint: true });
    } catch {
      recordVerdict(result, relativeDir || ".", { status: "incomplete", reason: "io-failure" });
      return;
    }
    if (directoryStats.isSymbolicLink()) {
      recordVerdict(result, relativeDir || ".", { status: "incomplete", reason: "symlink" });
      return;
    }
    if (!directoryStats.isDirectory()) {
      recordVerdict(result, relativeDir || ".", { status: "incomplete", reason: "nonregular" });
      return;
    }

    let entries: Dirent<Buffer>[];
    try {
      entries = await readdir(dir, { withFileTypes: true, encoding: "buffer" });
    } catch {
      recordVerdict(result, relativeDir || ".", { status: "incomplete", reason: "io-failure" });
      return;
    }
    for (const entry of entries) {
      let entryName: string;
      try {
        entryName = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(entry.name);
        if (!Buffer.from(entryName, "utf8").equals(entry.name)) throw new Error("filename did not round-trip");
      } catch {
        const displayName = `[filename-bytes:${entry.name.toString("hex")}]`;
        recordVerdict(result, join(relativeDir, displayName), {
          status: "incomplete",
          reason: "unsupported-encoding",
        });
        continue;
      }

      const full = join(dir, entryName);
      const relativeFile = relative(resolvedRoot, full);
      let entryStats: BigIntStats;
      try {
        entryStats = await lstat(full, { bigint: true });
      } catch {
        recordVerdict(result, relativeFile, { status: "incomplete", reason: "io-failure" });
        continue;
      }

      if (entryStats.isSymbolicLink()) {
        recordVerdict(result, relativeFile, { status: "incomplete", reason: "symlink" });
      } else if (entryStats.isDirectory()) {
        await walk(full, relativeFile);
      } else if (entryStats.isFile()) {
        recordVerdict(result, relativeFile, await scanFile(full));
      } else {
        recordVerdict(result, relativeFile, { status: "incomplete", reason: "nonregular" });
      }
    }
  }

  await walk(resolvedRoot, "");
  return result;
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

export interface ScanAndQuarantineResult extends ScanBatchResult {
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
  const scanResult = relativeFiles ? await scanFiles(rootDir, relativeFiles) : await scanDirectory(rootDir);

  const flaggedFiles = [...new Set(scanResult.findings.map((f) => f.file))];
  const quarantined: string[] = [];
  for (const file of flaggedFiles) {
    await quarantineFile(rootDir, quarantineDir, file);
    quarantined.push(file);
  }

  return { ...scanResult, quarantined };
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
