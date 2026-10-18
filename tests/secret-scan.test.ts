import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mkdirSync, writeFileSync, rmSync, existsSync, symlinkSync, chmodSync, statSync, readFileSync } from "node:fs";
import type { Dirent } from "node:fs";
import { execFileSync } from "node:child_process";
import { join } from "node:path";

const fsMockControl = vi.hoisted(() => ({
  forceNextLinkExdev: false,
  failOpenPath: null as string | null,
  failReadDirPath: null as string | null,
  rawReadDirPath: null as string | null,
  rawReadDirEntries: null as Array<Pick<Dirent<Buffer>, "name" | "isDirectory" | "isFile" | "isSymbolicLink">> | null,
}));
const processMockControl = vi.hoisted(() => ({ failAfterAclStrips: null as number | null }));
vi.mock("node:fs/promises", async (importOriginal) => {
  const actual = await importOriginal<typeof import("node:fs/promises")>();
  return {
    ...actual,
    link: (...args: Parameters<typeof actual.link>) => {
      if (fsMockControl.forceNextLinkExdev) {
        fsMockControl.forceNextLinkExdev = false;
        return Promise.reject(Object.assign(new Error("synthetic cross-device error"), { code: "EXDEV" }));
      }
      return actual.link(...args);
    },
    open: (...args: Parameters<typeof actual.open>) => {
      if (fsMockControl.failOpenPath === String(args[0])) {
        fsMockControl.failOpenPath = null;
        return Promise.reject(Object.assign(new Error("synthetic copy failure"), { code: "EIO" }));
      }
      return actual.open(...args);
    },
    readdir: (...args: Parameters<typeof actual.readdir>) => {
      if (fsMockControl.failReadDirPath === String(args[0])) {
        fsMockControl.failReadDirPath = null;
        return Promise.reject(Object.assign(new Error("synthetic listing failure"), { code: "EIO" }));
      }
      if (fsMockControl.rawReadDirPath === String(args[0]) && fsMockControl.rawReadDirEntries) {
        const entries = fsMockControl.rawReadDirEntries;
        fsMockControl.rawReadDirPath = null;
        fsMockControl.rawReadDirEntries = null;
        return Promise.resolve(entries) as ReturnType<typeof actual.readdir>;
      }
      return actual.readdir(...args);
    },
  };
});
vi.mock("node:child_process", async (importOriginal) => {
  const actual = await importOriginal<typeof import("node:child_process")>();
  return {
    ...actual,
    spawn: (...args: unknown[]) => {
      const [command, childArgs, options] = args;
      if (processMockControl.failAfterAclStrips !== null) {
        if (processMockControl.failAfterAclStrips > 0) {
          processMockControl.failAfterAclStrips -= 1;
        } else {
          processMockControl.failAfterAclStrips = null;
          return (actual.spawn as (...values: unknown[]) => unknown)("/bin/false", childArgs, options);
        }
      }
      return (actual.spawn as (...values: unknown[]) => unknown)(command, childArgs, options);
    },
  };
});
import {
  SECRET_RULES,
  scanText,
  scanFile,
  scanFiles,
  scanDirectory,
  quarantineFile,
  scanAndQuarantine,
  alertSecretsFound,
} from "../src/secret-scan.js";

const TEST_ROOT = join(import.meta.dirname, "__test_fixtures_secret_scan__");
const QUARANTINE_ROOT = join(import.meta.dirname, "__test_fixtures_secret_scan_quarantine__");
const OUTSIDE_ROOT = join(import.meta.dirname, "__test_fixtures_secret_scan_outside__");

function cleanup() {
  rmSync(TEST_ROOT, { recursive: true, force: true });
  rmSync(QUARANTINE_ROOT, { recursive: true, force: true });
  rmSync(OUTSIDE_ROOT, { recursive: true, force: true });
}

beforeEach(() => {
  cleanup();
  fsMockControl.failOpenPath = null;
  fsMockControl.failReadDirPath = null;
  fsMockControl.rawReadDirPath = null;
  fsMockControl.rawReadDirEntries = null;
  mkdirSync(TEST_ROOT, { recursive: true });
});

afterEach(() => {
  cleanup();
});

// ---------------------------------------------------------------------------
// scanText — pure regex rules
// ---------------------------------------------------------------------------

describe("scanText", () => {
  it("flags an AWS access key id", () => {
    const hits = scanText("aws_key = AKIAABCDEFGHIJKLMNOP\n");
    expect(hits.some((h) => h.rule === "aws-access-key-id")).toBe(true);
  });

  it("flags a private key block", () => {
    const hits = scanText("-----BEGIN RSA PRIVATE KEY-----\nMIIBogIBAAJ...\n");
    expect(hits.some((h) => h.rule === "private-key-block")).toBe(true);
  });

  it("flags a GitHub personal access token", () => {
    const hits = scanText(`token: ghp_${"a".repeat(36)}\n`);
    expect(hits.some((h) => h.rule === "github-token")).toBe(true);
  });

  it("flags a GitHub fine-grained personal access token", () => {
    const hits = scanText(`token: github_pat_${"a".repeat(22)}_${"b".repeat(59)}\n`);
    expect(hits.some((h) => h.rule === "github-fine-grained-token")).toBe(true);
  });

  it("flags a Slack token", () => {
    const hits = scanText(`SLACK_TOKEN=xoxb-${"FAKE".repeat(6)}\n`);
    expect(hits.some((h) => h.rule === "slack-token")).toBe(true);
  });

  it("flags a Stripe live secret key", () => {
    const hits = scanText(`sk_live_${"a1B2c3D4e5F6g7H8".repeat(2)}\n`);
    expect(hits.some((h) => h.rule === "stripe-live-key")).toBe(true);
  });

  it("flags a generic quoted api key assignment", () => {
    const hits = scanText(`api_key = "sk_test_reallyLongSecretValue1234567890"\n`);
    expect(hits.some((h) => h.rule === "generic-api-key-assignment")).toBe(true);
  });

  it("reports the correct 1-indexed line number", () => {
    const hits = scanText(`line one\nline two\nAKIAABCDEFGHIJKLMNOP\nline four\n`);
    const hit = hits.find((h) => h.rule === "aws-access-key-id");
    expect(hit?.line).toBe(3);
  });

  it("does not flag ordinary prose", () => {
    const hits = scanText(
      "# Meeting notes\n\nWe discussed the roadmap and agreed on next steps.\nAction: token the design with the team next week.\n",
    );
    expect(hits).toHaveLength(0);
  });

  it("does not flag markdown with short, unquoted example values", () => {
    const hits = scanText("Set MIMIR_API_KEY=dev-key in your .env file.\n");
    expect(hits).toHaveLength(0);
  });

  it("every rule has a unique name", () => {
    const names = SECRET_RULES.map((r) => r.name);
    expect(new Set(names).size).toBe(names.length);
  });
});

// ---------------------------------------------------------------------------
// scanFile — single file on disk
// ---------------------------------------------------------------------------

describe("scanFile", () => {
  it("returns findings for a file containing a secret", async () => {
    const filePath = join(TEST_ROOT, "leaky.env");
    writeFileSync(filePath, "AWS_KEY=AKIAABCDEFGHIJKLMNOP\n");
    const result = await scanFile(filePath);
    expect(result.status).toBe("findings");
    if (result.status === "findings") expect(result.findings.length).toBeGreaterThan(0);
  });

  it("returns a complete clean verdict for a clean file", async () => {
    const filePath = join(TEST_ROOT, "clean.txt");
    writeFileSync(filePath, "Just a regular note about the project.\n");
    await expect(scanFile(filePath)).resolves.toEqual({ status: "clean" });
  });

  it("returns an incomplete binary verdict without throwing", async () => {
    const filePath = join(TEST_ROOT, "image.png");
    writeFileSync(filePath, Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x00, 0x01, 0x02, 0x00, 0xff]));
    await expect(scanFile(filePath)).resolves.toEqual({ status: "incomplete", reason: "binary" });
  });

  it("detects a NUL byte anywhere in the bounded file contents", async () => {
    const filePath = join(TEST_ROOT, "late-nul.bin");
    writeFileSync(filePath, Buffer.concat([Buffer.alloc(8000, 0x61), Buffer.from([0x00])]));
    await expect(scanFile(filePath)).resolves.toEqual({ status: "incomplete", reason: "binary" });
  });

  it("does not follow a symlink to read/scan a file outside the tree", async () => {
    const outsidePath = join(import.meta.dirname, "__secret_scan_outside_target__.txt");
    writeFileSync(outsidePath, "AKIAABCDEFGHIJKLMNOP\n");
    const linkPath = join(TEST_ROOT, "link-to-outside.txt");
    symlinkSync(outsidePath, linkPath);
    try {
      await expect(scanFile(linkPath)).resolves.toEqual({ status: "incomplete", reason: "symlink" });
    } finally {
      rmSync(outsidePath, { force: true });
    }
  });

  it("logs a warning and treats an oversized file as unscanned (not scanned-clean)", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const filePath = join(TEST_ROOT, "huge.bin");
    writeFileSync(filePath, Buffer.alloc(10 * 1024 * 1024 + 1, "a"));

    const result = await scanFile(filePath);

    expect(result).toEqual({ status: "incomplete", reason: "oversized" });
    expect(warnSpy).toHaveBeenCalled();
    warnSpy.mockRestore();
  });

  it("reports unsupported UTF-8 instead of scanning replacement characters", async () => {
    const filePath = join(TEST_ROOT, "invalid-utf8.txt");
    writeFileSync(filePath, Buffer.from([0x61, 0xff, 0x62]));
    await expect(scanFile(filePath)).resolves.toEqual({ status: "incomplete", reason: "unsupported-encoding" });
  });

  it("reports non-regular inputs and I/O failures as incomplete", async () => {
    await expect(scanFile(TEST_ROOT)).resolves.toEqual({ status: "incomplete", reason: "nonregular" });
    await expect(scanFile(join(TEST_ROOT, "missing.txt"))).resolves.toEqual({
      status: "incomplete",
      reason: "io-failure",
    });
  });

  it("reports a read-open failure as incomplete", async () => {
    const filePath = join(TEST_ROOT, "unreadable.txt");
    writeFileSync(filePath, "clean\n");
    fsMockControl.failOpenPath = filePath;
    await expect(scanFile(filePath)).resolves.toEqual({ status: "incomplete", reason: "io-failure" });
  });
});

// ---------------------------------------------------------------------------
// scanFiles — explicit relative-path list (used by the ingest hook)
// ---------------------------------------------------------------------------

describe("scanFiles", () => {
  it("scans only the given relative files, ignoring others in the tree", async () => {
    writeFileSync(join(TEST_ROOT, "flagged.txt"), "AKIAABCDEFGHIJKLMNOP\n");
    writeFileSync(join(TEST_ROOT, "ignored.txt"), "AKIAZZZZZZZZZZZZZZZZ\n");

    const result = await scanFiles(TEST_ROOT, ["flagged.txt", "missing.txt"]);

    expect(result.entries).toEqual([
      expect.objectContaining({ file: "flagged.txt", status: "findings" }),
      { file: "missing.txt", status: "incomplete", reason: "io-failure" },
    ]);
    expect(result.findings).toHaveLength(1);
    expect(result.findings[0].file).toBe("flagged.txt");
    expect(result.incomplete).toEqual([{ file: "missing.txt", reason: "io-failure" }]);
  });

  it("refuses paths outside the root, records them, and continues scanning the batch", async () => {
    writeFileSync(join(TEST_ROOT, "clean.txt"), "clean\n");

    const result = await scanFiles(TEST_ROOT, ["../outside.txt", "clean.txt"]);

    expect(result.entries).toEqual([
      { file: "../outside.txt", status: "incomplete", reason: "io-failure" },
      { file: "clean.txt", status: "clean" },
    ]);
    expect(result.incomplete).toEqual([{ file: "../outside.txt", reason: "io-failure" }]);
    expect(result.findings).toEqual([]);
  });

  it("does not follow a symlinked parent directory", async () => {
    mkdirSync(OUTSIDE_ROOT);
    writeFileSync(join(OUTSIDE_ROOT, "leaky.txt"), "AKIAABCDEFGHIJKLMNOP\n");
    symlinkSync(OUTSIDE_ROOT, join(TEST_ROOT, "external-directory"));

    const result = await scanFiles(TEST_ROOT, ["external-directory/leaky.txt"]);

    expect(result.entries).toEqual([{ file: "external-directory/leaky.txt", status: "incomplete", reason: "symlink" }]);
    expect(result.findings).toEqual([]);
    expect(existsSync(join(OUTSIDE_ROOT, "leaky.txt"))).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// scanDirectory — recursive walk
// ---------------------------------------------------------------------------

describe("scanDirectory", () => {
  it("finds secrets in nested subdirectories", async () => {
    mkdirSync(join(TEST_ROOT, "sub"), { recursive: true });
    writeFileSync(join(TEST_ROOT, "sub", "creds.txt"), "-----BEGIN RSA PRIVATE KEY-----\n");
    writeFileSync(join(TEST_ROOT, "top.txt"), "nothing to see here\n");

    const result = await scanDirectory(TEST_ROOT);

    expect(result.findings).toHaveLength(1);
    expect(result.findings[0].file).toBe(join("sub", "creds.txt"));
    expect(result.findings[0].rule).toBe("private-key-block");
    expect(result.entries.map(({ file }) => file).sort()).toEqual(["sub/creds.txt", "top.txt"]);
  });

  it("returns an empty array for a fully clean tree", async () => {
    writeFileSync(join(TEST_ROOT, "a.txt"), "hello\n");
    mkdirSync(join(TEST_ROOT, "b"), { recursive: true });
    writeFileSync(join(TEST_ROOT, "b", "c.txt"), "world\n");

    const result = await scanDirectory(TEST_ROOT);
    expect(result.findings).toEqual([]);
    expect(result.incomplete).toEqual([]);
    expect(result.entries.map(({ file }) => file).sort()).toEqual(["a.txt", join("b", "c.txt")]);
  });

  it("scans dotfiles (not just what directory listing shows)", async () => {
    writeFileSync(join(TEST_ROOT, ".env"), "AKIAABCDEFGHIJKLMNOP\n");
    const result = await scanDirectory(TEST_ROOT);
    expect(result.findings.some((f) => f.file === ".env")).toBe(true);
  });

  it("includes skipped symlinks and non-regular entries in the recursive inventory", async () => {
    writeFileSync(join(TEST_ROOT, "clean.txt"), "clean\n");
    writeFileSync(join(TEST_ROOT, "binary.bin"), Buffer.from([0x61, 0x00, 0x62]));
    symlinkSync(join(TEST_ROOT, "clean.txt"), join(TEST_ROOT, "linked.txt"));
    execFileSync("mkfifo", [join(TEST_ROOT, "pipe.fifo")]);
    mkdirSync(OUTSIDE_ROOT);
    writeFileSync(join(OUTSIDE_ROOT, "external-secret.txt"), "AKIAABCDEFGHIJKLMNOP\n");
    symlinkSync(OUTSIDE_ROOT, join(TEST_ROOT, "linked-directory"));

    const result = await scanDirectory(TEST_ROOT);

    expect(result.entries).toEqual(expect.arrayContaining([
      { file: "clean.txt", status: "clean" },
      { file: "binary.bin", status: "incomplete", reason: "binary" },
      { file: "linked.txt", status: "incomplete", reason: "symlink" },
      { file: "linked-directory", status: "incomplete", reason: "symlink" },
      { file: "pipe.fifo", status: "incomplete", reason: "nonregular" },
    ]));
    expect(result.incomplete).toEqual(expect.arrayContaining([
      { file: "binary.bin", reason: "binary" },
      { file: "linked.txt", reason: "symlink" },
      { file: "linked-directory", reason: "symlink" },
      { file: "pipe.fifo", reason: "nonregular" },
    ]));
    expect(result.findings).toEqual([]);
  });

  it("records an unreadable directory listing as incomplete and continues other entries", async () => {
    const inaccessible = join(TEST_ROOT, "cannot-list");
    mkdirSync(inaccessible);
    writeFileSync(join(inaccessible, "hidden-from-walk.txt"), "clean\n");
    writeFileSync(join(TEST_ROOT, "visible.txt"), "clean\n");
    fsMockControl.failReadDirPath = inaccessible;

    const result = await scanDirectory(TEST_ROOT);

    expect(result.entries).toEqual(expect.arrayContaining([
      { file: "cannot-list", status: "incomplete", reason: "io-failure" },
      { file: "visible.txt", status: "clean" },
    ]));
    expect(result.incomplete).toContainEqual({ file: "cannot-list", reason: "io-failure" });
  });

  it("returns an incomplete verdict when the root cannot be listed", async () => {
    const missingRoot = join(TEST_ROOT, "missing-root");
    const result = await scanDirectory(missingRoot);
    expect(result.incomplete).toEqual([{ file: ".", reason: "io-failure" }]);
  });

  it("keeps invalid UTF-8 names incomplete without aliasing a valid replacement-character name", async () => {
    const replacementName = "\uFFFD";
    writeFileSync(join(TEST_ROOT, replacementName), "clean\n");
    const regularDirent = (name: Buffer) => ({
      name,
      isDirectory: () => false,
      isFile: () => true,
      isSymbolicLink: () => false,
    });
    fsMockControl.rawReadDirPath = TEST_ROOT;
    fsMockControl.rawReadDirEntries = [
      regularDirent(Buffer.from(replacementName, "utf8")),
      regularDirent(Buffer.from([0xff])),
    ];

    const result = await scanAndQuarantine(TEST_ROOT, QUARANTINE_ROOT);

    expect(result.entries).toEqual(expect.arrayContaining([
      { file: replacementName, status: "clean" },
      { file: "[filename-bytes:ff]", status: "incomplete", reason: "unsupported-encoding" },
    ]));
    expect(result.incomplete).toContainEqual({ file: "[filename-bytes:ff]", reason: "unsupported-encoding" });
    expect(result.quarantined).toEqual([]);
    expect(existsSync(join(TEST_ROOT, replacementName))).toBe(true);
    expect(existsSync(join(QUARANTINE_ROOT, replacementName))).toBe(false);
  });

  it("preserves a BOM in filenames instead of aliasing the clean name", async () => {
    const bomName = "\uFEFFsecret.txt";
    writeFileSync(join(TEST_ROOT, bomName), "AWS_KEY=AKIAABCDEFGHIJKLMNOP\n");
    writeFileSync(join(TEST_ROOT, "secret.txt"), "clean\n");

    const result = await scanAndQuarantine(TEST_ROOT, QUARANTINE_ROOT);

    expect(result.quarantined).toContain(bomName);
    expect(result.findings).toContainEqual(expect.objectContaining({ file: bomName }));
    expect(existsSync(join(QUARANTINE_ROOT, bomName))).toBe(true);
    expect(existsSync(join(TEST_ROOT, "secret.txt"))).toBe(true);
    expect(existsSync(join(QUARANTINE_ROOT, "secret.txt"))).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// quarantineFile — move out of the servable tree
// ---------------------------------------------------------------------------

describe("quarantineFile", () => {
  it("moves the file out of the root into the quarantine dir, preserving relative path", async () => {
    mkdirSync(join(TEST_ROOT, "sub"), { recursive: true });
    const src = join(TEST_ROOT, "sub", "secret.txt");
    writeFileSync(src, "AKIAABCDEFGHIJKLMNOP\n");

    const dest = await quarantineFile(TEST_ROOT, QUARANTINE_ROOT, join("sub", "secret.txt"));

    expect(existsSync(src)).toBe(false);
    expect(existsSync(dest)).toBe(true);
    expect(dest).toBe(join(QUARANTINE_ROOT, "sub", "secret.txt"));
  });

  it("makes quarantine directories private and protects a permissive source before moving it", async () => {
    const nested = join(TEST_ROOT, "nested", "secret.txt");
    mkdirSync(join(TEST_ROOT, "nested"), { recursive: true });
    writeFileSync(nested, "synthetic confidential bytes\n", { mode: 0o644 });
    chmodSync(nested, 0o644);

    const dest = await quarantineFile(TEST_ROOT, QUARANTINE_ROOT, "nested/secret.txt");

    expect(readFileSync(dest, "utf8")).toBe("synthetic confidential bytes\n");
    expect(statSync(dest).mode & 0o777).toBe(0o600);
    expect(statSync(QUARANTINE_ROOT).mode & 0o777).toBe(0o700);
    expect(statSync(join(QUARANTINE_ROOT, "nested")).mode & 0o777).toBe(0o700);
  });

  it("secures existing owned quarantine directories before using them", async () => {
    mkdirSync(join(QUARANTINE_ROOT, "nested"), { recursive: true, mode: 0o755 });
    chmodSync(QUARANTINE_ROOT, 0o755);
    chmodSync(join(QUARANTINE_ROOT, "nested"), 0o755);
    mkdirSync(join(TEST_ROOT, "nested"));
    writeFileSync(join(TEST_ROOT, "nested", "secret.txt"), "synthetic payload\n", { mode: 0o644 });

    await quarantineFile(TEST_ROOT, QUARANTINE_ROOT, "nested/secret.txt");

    expect(statSync(QUARANTINE_ROOT).mode & 0o777).toBe(0o700);
    expect(statSync(join(QUARANTINE_ROOT, "nested")).mode & 0o777).toBe(0o700);
  });

  it("rejects a symlink quarantine root without changing its target", async () => {
    const target = join(TEST_ROOT, "target");
    mkdirSync(target, { mode: 0o755 });
    symlinkSync(target, QUARANTINE_ROOT);
    writeFileSync(join(TEST_ROOT, "secret.txt"), "synthetic payload\n");

    await expect(quarantineFile(TEST_ROOT, QUARANTINE_ROOT, "secret.txt")).rejects.toThrow(/symlink/);

    expect(statSync(target).mode & 0o777).toBe(0o755);
    expect(existsSync(join(TEST_ROOT, "secret.txt"))).toBe(true);
  });

  it("rejects a symlink in the quarantine directory hierarchy without changing its target", async () => {
    mkdirSync(QUARANTINE_ROOT);
    const target = join(TEST_ROOT, "target");
    mkdirSync(target, { mode: 0o755 });
    symlinkSync(target, join(QUARANTINE_ROOT, "nested"));
    mkdirSync(join(TEST_ROOT, "nested"));
    writeFileSync(join(TEST_ROOT, "nested", "secret.txt"), "synthetic payload\n");

    await expect(quarantineFile(TEST_ROOT, QUARANTINE_ROOT, "nested/secret.txt")).rejects.toThrow(/symlink/);

    expect(statSync(target).mode & 0o777).toBe(0o755);
    expect(existsSync(join(TEST_ROOT, "nested", "secret.txt"))).toBe(true);
  });

  it("rejects a quarantine parent alias that resolves into the servable root", async () => {
    const alias = join(TEST_ROOT, "outside-alias");
    symlinkSync(TEST_ROOT, alias);
    const aliasedQuarantine = join(alias, "quarantine");
    writeFileSync(join(TEST_ROOT, "secret.txt"), "synthetic payload\n");

    await expect(quarantineFile(TEST_ROOT, aliasedQuarantine, "secret.txt")).rejects.toThrow(/inside root/);

    expect(existsSync(join(TEST_ROOT, "secret.txt"))).toBe(true);
    expect(existsSync(join(TEST_ROOT, "quarantine"))).toBe(false);
  });

  it("keeps the source and existing quarantine data when a destination collision occurs", async () => {
    const src = join(TEST_ROOT, "secret.txt");
    const dest = join(QUARANTINE_ROOT, "secret.txt");
    writeFileSync(src, "source payload\n", { mode: 0o644 });
    mkdirSync(QUARANTINE_ROOT);
    writeFileSync(dest, "existing payload\n", { mode: 0o600 });

    await expect(quarantineFile(TEST_ROOT, QUARANTINE_ROOT, "secret.txt")).rejects.toMatchObject({ code: "EEXIST" });

    expect(existsSync(src)).toBe(true);
    expect(readFileSync(dest, "utf8")).toBe("existing payload\n");
  });

  it("uses a private exclusive copy on EXDEV before removing the source", async () => {
    const src = join(TEST_ROOT, "secret.txt");
    const dest = join(QUARANTINE_ROOT, "secret.txt");
    writeFileSync(src, "synthetic cross-device payload\n", { mode: 0o644 });
    fsMockControl.forceNextLinkExdev = true;

    try {
      await quarantineFile(TEST_ROOT, QUARANTINE_ROOT, "secret.txt");
    } finally {
      fsMockControl.forceNextLinkExdev = false;
    }

    expect(existsSync(src)).toBe(false);
    expect(readFileSync(dest, "utf8")).toBe("synthetic cross-device payload\n");
    expect(statSync(dest).mode & 0o777).toBe(0o600);
  });

  it("preserves an existing destination and source when the EXDEV copy collides", async () => {
    const src = join(TEST_ROOT, "secret.txt");
    const dest = join(QUARANTINE_ROOT, "secret.txt");
    writeFileSync(src, "source payload\n", { mode: 0o644 });
    mkdirSync(QUARANTINE_ROOT);
    writeFileSync(dest, "existing payload\n", { mode: 0o600 });
    fsMockControl.forceNextLinkExdev = true;

    try {
      await expect(quarantineFile(TEST_ROOT, QUARANTINE_ROOT, "secret.txt")).rejects.toMatchObject({ code: "EEXIST" });
    } finally {
      fsMockControl.forceNextLinkExdev = false;
    }

    expect(existsSync(src)).toBe(true);
    expect(readFileSync(dest, "utf8")).toBe("existing payload\n");
  });

  it("retains the source when the EXDEV copy cannot create a destination", async () => {
    const src = join(TEST_ROOT, "secret.txt");
    const dest = join(QUARANTINE_ROOT, "secret.txt");
    writeFileSync(src, "synthetic payload\n", { mode: 0o644 });
    fsMockControl.forceNextLinkExdev = true;
    fsMockControl.failOpenPath = dest;

    try {
      await expect(quarantineFile(TEST_ROOT, QUARANTINE_ROOT, "secret.txt")).rejects.toMatchObject({ code: "EIO" });
    } finally {
      fsMockControl.forceNextLinkExdev = false;
      fsMockControl.failOpenPath = null;
    }

    expect(existsSync(src)).toBe(true);
    expect(existsSync(dest)).toBe(false);
  });

  it("rejects symbolic-link and non-regular source files", async () => {
    const target = join(TEST_ROOT, "target.txt");
    writeFileSync(target, "synthetic payload\n");
    symlinkSync(target, join(TEST_ROOT, "link.txt"));
    mkdirSync(join(TEST_ROOT, "directory"));

    await expect(quarantineFile(TEST_ROOT, QUARANTINE_ROOT, "link.txt")).rejects.toThrow();
    await expect(quarantineFile(TEST_ROOT, QUARANTINE_ROOT, "directory")).rejects.toThrow(/non-regular/);
    expect(existsSync(target)).toBe(true);
  });

  it("rejects a source parent alias outside the root before changing source permissions", async () => {
    mkdirSync(OUTSIDE_ROOT);
    const externalFile = join(OUTSIDE_ROOT, "secret.txt");
    writeFileSync(externalFile, "synthetic outside payload\n", { mode: 0o644 });
    chmodSync(externalFile, 0o644);
    symlinkSync(OUTSIDE_ROOT, join(TEST_ROOT, "external"));

    await expect(quarantineFile(TEST_ROOT, QUARANTINE_ROOT, "external/secret.txt")).rejects.toThrow(/outside root/);

    expect(existsSync(externalFile)).toBe(true);
    expect(statSync(externalFile).mode & 0o777).toBe(0o644);
  });

  it("creates a missing quarantine parent chain without weakening created directory modes", async () => {
    mkdirSync(OUTSIDE_ROOT);
    const customParent = join(OUTSIDE_ROOT, "missing", "intermediate");
    const customQuarantine = join(customParent, "quarantine");
    writeFileSync(join(TEST_ROOT, "secret.txt"), "synthetic payload\n");

    const dest = await quarantineFile(TEST_ROOT, customQuarantine, "secret.txt");

    expect(existsSync(dest)).toBe(true);
    expect(existsSync(join(TEST_ROOT, "secret.txt"))).toBe(false);
    expect(statSync(OUTSIDE_ROOT).mode & 0o777).toBe(0o755);
    expect(statSync(OUTSIDE_ROOT + "/missing").mode & 0o777).toBe(0o700);
    expect(statSync(customParent).mode & 0o777).toBe(0o700);
    expect(statSync(customQuarantine).mode & 0o777).toBe(0o700);
  });

  it.skipIf(process.platform !== "darwin")("removes inherited everyone ACLs from the quarantine tree and file", async () => {
    mkdirSync(OUTSIDE_ROOT);
    const source = join(TEST_ROOT, "nested", "secret.txt");
    mkdirSync(join(TEST_ROOT, "nested"));
    writeFileSync(source, "synthetic ACL payload\n", { mode: 0o644 });
    const acl = "everyone allow read,search,file_inherit,directory_inherit";
    execFileSync("/bin/chmod", ["+a", acl, OUTSIDE_ROOT]);
    execFileSync("/bin/chmod", ["+a", "everyone allow read", source]);
    const customQuarantine = join(OUTSIDE_ROOT, "quarantine");

    await quarantineFile(TEST_ROOT, customQuarantine, "nested/secret.txt");

    const listing = execFileSync("/bin/ls", ["-lde", customQuarantine, join(customQuarantine, "nested"), join(customQuarantine, "nested", "secret.txt")], { encoding: "utf8" });
    expect(listing).not.toMatch(/everyone/i);
  });

  it.skipIf(process.platform !== "darwin")("fails closed and retains the source if ACL removal fails", async () => {
    const source = join(TEST_ROOT, "secret.txt");
    writeFileSync(source, "synthetic ACL failure payload\n", { mode: 0o644 });
    processMockControl.failAfterAclStrips = 1;

    try {
      await expect(quarantineFile(TEST_ROOT, QUARANTINE_ROOT, "secret.txt")).rejects.toThrow(/ACL/);
    } finally {
      processMockControl.failAfterAclStrips = null;
    }

    expect(existsSync(source)).toBe(true);
    expect(statSync(source).mode & 0o777).toBe(0o644);
    expect(existsSync(join(QUARANTINE_ROOT, "secret.txt"))).toBe(false);
  });

  it("refuses to quarantine a path that escapes the root", async () => {
    await expect(quarantineFile(TEST_ROOT, QUARANTINE_ROOT, "../escape.txt")).rejects.toThrow();
  });

  it("refuses to quarantine into a directory nested inside the root (would stay servable)", async () => {
    const src = join(TEST_ROOT, "secret.txt");
    writeFileSync(src, "AKIAABCDEFGHIJKLMNOP\n");
    const insideQuarantine = join(TEST_ROOT, "quarantine");

    await expect(quarantineFile(TEST_ROOT, insideQuarantine, "secret.txt")).rejects.toThrow();
    // Must not have moved the file — it should still be servable-tree-safe on failure.
    expect(existsSync(src)).toBe(true);
  });

  it("refuses to quarantine into the root itself", async () => {
    writeFileSync(join(TEST_ROOT, "secret.txt"), "AKIAABCDEFGHIJKLMNOP\n");
    await expect(quarantineFile(TEST_ROOT, TEST_ROOT, "secret.txt")).rejects.toThrow();
  });
});

// ---------------------------------------------------------------------------
// scanAndQuarantine — orchestrator
// ---------------------------------------------------------------------------

describe("scanAndQuarantine", () => {
  it("quarantines only the flagged file, leaves clean files in place", async () => {
    writeFileSync(join(TEST_ROOT, "clean.txt"), "hello world\n");
    writeFileSync(join(TEST_ROOT, "leaky.txt"), "AKIAABCDEFGHIJKLMNOP\n");

    const result = await scanAndQuarantine(TEST_ROOT, QUARANTINE_ROOT);

    expect(result.quarantined).toEqual(["leaky.txt"]);
    expect(existsSync(join(TEST_ROOT, "leaky.txt"))).toBe(false);
    expect(existsSync(join(TEST_ROOT, "clean.txt"))).toBe(true);
    expect(existsSync(join(QUARANTINE_ROOT, "leaky.txt"))).toBe(true);
  });

  it("scans only the given relative files when a file list is provided", async () => {
    writeFileSync(join(TEST_ROOT, "leaky.txt"), "AKIAABCDEFGHIJKLMNOP\n");
    writeFileSync(join(TEST_ROOT, "unscanned-leaky.txt"), "AKIAZZZZZZZZZZZZZZZZ\n");

    const result = await scanAndQuarantine(TEST_ROOT, QUARANTINE_ROOT, ["leaky.txt"]);

    expect(result.quarantined).toEqual(["leaky.txt"]);
    // Not in the scan list — must be left alone even though it also contains a secret.
    expect(existsSync(join(TEST_ROOT, "unscanned-leaky.txt"))).toBe(true);
  });

  it("quarantines nothing and reports no findings for a clean tree", async () => {
    writeFileSync(join(TEST_ROOT, "clean.txt"), "hello world\n");
    const result = await scanAndQuarantine(TEST_ROOT, QUARANTINE_ROOT);
    expect(result.findings).toEqual([]);
    expect(result.quarantined).toEqual([]);
  });

  it("quarantines complete findings but leaves incomplete files in place", async () => {
    writeFileSync(join(TEST_ROOT, "leaky.txt"), "AKIAABCDEFGHIJKLMNOP\n");
    writeFileSync(join(TEST_ROOT, "binary.bin"), Buffer.from([0x00, 0xff]));

    const result = await scanAndQuarantine(TEST_ROOT, QUARANTINE_ROOT);

    expect(result.quarantined).toEqual(["leaky.txt"]);
    expect(existsSync(join(QUARANTINE_ROOT, "leaky.txt"))).toBe(true);
    expect(existsSync(join(TEST_ROOT, "binary.bin"))).toBe(true);
    expect(result.incomplete).toEqual([{ file: "binary.bin", reason: "binary" }]);
  });
});

// ---------------------------------------------------------------------------
// alertSecretsFound — logs loudly always, pushes Heimdall panel when configured
// ---------------------------------------------------------------------------

describe("alertSecretsFound", () => {
  beforeEach(() => {
    delete process.env.HEIMDALL_HUB_URL;
    delete process.env.HEIMDALL_FLEET_TOKEN;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("logs loudly even when Heimdall is not configured", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const mockFetch = vi.fn();
    vi.stubGlobal("fetch", mockFetch);

    await alertSecretsFound([{ file: "leaky.txt", rule: "aws-access-key-id", line: 1 }], "/quarantine");

    expect(errorSpy).toHaveBeenCalled();
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("never includes the matched secret value in the log message", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const secretValue = "AKIAABCDEFGHIJKLMNOP";

    await alertSecretsFound([{ file: "leaky.txt", rule: "aws-access-key-id", line: 1 }], "/quarantine");

    const logged = errorSpy.mock.calls.map((c) => c.join(" ")).join("\n");
    expect(logged).not.toContain(secretValue);
  });

  it("pushes a fail-state Heimdall panel when hub env vars are configured", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    process.env.HEIMDALL_HUB_URL = "http://hub.local/api/panels";
    process.env.HEIMDALL_FLEET_TOKEN = "test-fleet-token";
    const mockFetch = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal("fetch", mockFetch);

    await alertSecretsFound([{ file: "leaky.txt", rule: "aws-access-key-id", line: 1 }], "/quarantine");

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://hub.local/api/panels");
    const body = JSON.parse(init.body as string);
    expect(body).toMatchObject({ service: "mimir", kind: "status", state: "fail" });
  });
});
