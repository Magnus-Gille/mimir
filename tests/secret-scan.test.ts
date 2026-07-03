import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mkdirSync, writeFileSync, rmSync, existsSync } from "node:fs";
import { join } from "node:path";
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

function cleanup() {
  rmSync(TEST_ROOT, { recursive: true, force: true });
  rmSync(QUARANTINE_ROOT, { recursive: true, force: true });
}

beforeEach(() => {
  cleanup();
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
    const hits = await scanFile(filePath);
    expect(hits.length).toBeGreaterThan(0);
  });

  it("returns an empty array for a clean file", async () => {
    const filePath = join(TEST_ROOT, "clean.txt");
    writeFileSync(filePath, "Just a regular note about the project.\n");
    const hits = await scanFile(filePath);
    expect(hits).toHaveLength(0);
  });

  it("skips binary files without throwing", async () => {
    const filePath = join(TEST_ROOT, "image.png");
    writeFileSync(filePath, Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x00, 0x01, 0x02, 0x00, 0xff]));
    await expect(scanFile(filePath)).resolves.toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// scanFiles — explicit relative-path list (used by the ingest hook)
// ---------------------------------------------------------------------------

describe("scanFiles", () => {
  it("scans only the given relative files, ignoring others in the tree", async () => {
    writeFileSync(join(TEST_ROOT, "flagged.txt"), "AKIAABCDEFGHIJKLMNOP\n");
    writeFileSync(join(TEST_ROOT, "ignored.txt"), "AKIAZZZZZZZZZZZZZZZZ\n");

    const hits = await scanFiles(TEST_ROOT, ["flagged.txt"]);

    expect(hits).toHaveLength(1);
    expect(hits[0].file).toBe("flagged.txt");
  });

  it("refuses to scan a path that escapes the root", async () => {
    await expect(scanFiles(TEST_ROOT, ["../outside.txt"])).rejects.toThrow();
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

    const findings = await scanDirectory(TEST_ROOT);

    expect(findings).toHaveLength(1);
    expect(findings[0].file).toBe(join("sub", "creds.txt"));
    expect(findings[0].rule).toBe("private-key-block");
  });

  it("returns an empty array for a fully clean tree", async () => {
    writeFileSync(join(TEST_ROOT, "a.txt"), "hello\n");
    mkdirSync(join(TEST_ROOT, "b"), { recursive: true });
    writeFileSync(join(TEST_ROOT, "b", "c.txt"), "world\n");

    const findings = await scanDirectory(TEST_ROOT);
    expect(findings).toEqual([]);
  });

  it("scans dotfiles (not just what directory listing shows)", async () => {
    writeFileSync(join(TEST_ROOT, ".env"), "AKIAABCDEFGHIJKLMNOP\n");
    const findings = await scanDirectory(TEST_ROOT);
    expect(findings.some((f) => f.file === ".env")).toBe(true);
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

  it("refuses to quarantine a path that escapes the root", async () => {
    await expect(quarantineFile(TEST_ROOT, QUARANTINE_ROOT, "../escape.txt")).rejects.toThrow();
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
