import { chmodSync, mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { afterEach, describe, expect, it } from "vitest";

const REPO_ROOT = resolve(import.meta.dirname, "..");
const tempDirs: string[] = [];

function tempDir(): string {
  const dir = mkdtempSync(join(tmpdir(), "mimir-freshness-test-"));
  tempDirs.push(dir);
  return dir;
}

function publish(dir: string, subject: string, state: string) {
  return spawnSync("bash", [join(REPO_ROOT, "scripts", "publish-freshness.sh"), subject, state], {
    cwd: REPO_ROOT,
    encoding: "utf8",
    env: { ...process.env, MIMIR_FRESHNESS_DIR: dir },
  });
}

afterEach(() => {
  for (const dir of tempDirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

describe("Heimdall freshness surface", () => {
  it("atomically publishes fixed metadata-only backup and sync records", () => {
    const dir = tempDir();
    chmodSync(dir, 0o750);

    for (const subject of ["backup", "sync"]) {
      const result = publish(dir, subject, "success");
      expect(result.status, result.stderr).toBe(0);
      const record = JSON.parse(readFileSync(join(dir, `${subject}.json`), "utf8"));
      expect(record).toEqual({ schema_version: 1, state: "fresh", observed_at: expect.any(String) });
      expect(record.observed_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
      expect(statSync(join(dir, `${subject}.json`)).mode & 0o777).toBe(0o640);
    }
  });

  it("publishes an explicit error without a path, filename, or artifact content", () => {
    const dir = tempDir();
    const result = publish(dir, "backup", "error");
    expect(result.status, result.stderr).toBe(0);
    expect(JSON.parse(readFileSync(join(dir, "backup.json"), "utf8"))).toEqual({
      schema_version: 1,
      state: "error",
      observed_at: expect.any(String),
    });
  });

  it("rejects unknown subjects and states without creating a record", () => {
    const dir = tempDir();
    expect(publish(dir, "offsite", "success").status).not.toBe(0);
    expect(publish(dir, "backup", "maybe").status).not.toBe(0);
  });

  it("ships a reversible root installer with a fixed group/mode contract", () => {
    const installer = readFileSync(join(REPO_ROOT, "scripts", "install-heimdall-freshness-surface.sh"), "utf8");
    expect(installer).toContain("heimdall-storage-probe");
    expect(installer).toContain("2750");
    expect(installer).toContain("0640");
    expect(installer).toContain("--remove");
  });
});
