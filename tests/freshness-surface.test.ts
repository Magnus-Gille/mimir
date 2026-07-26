import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
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

function executable(path: string, body: string): void {
  writeFileSync(path, `#!/bin/bash\nprintf '%s %s\\n' "$(basename "$0")" "$*" >> "$INSTALLER_CALLS"\n${body}`);
  chmodSync(path, 0o755);
}

function installerHarness() {
  const root = tempDir();
  const bin = join(root, "bin");
  const calls = join(root, "calls");
  mkdirSync(bin);
  executable(join(bin, "id"), 'printf "0\\n"');
  executable(join(bin, "getent"), "exit 1");
  for (const command of ["groupadd", "install", "chown", "chmod", "rm", "rmdir"]) {
    executable(join(bin, command), "exit 0");
  }
  return {
    root,
    run(args: string[] = [], env: Record<string, string> = {}) {
      const result = spawnSync("bash", [join(REPO_ROOT, "scripts", "install-heimdall-freshness-surface.sh"), ...args], {
        cwd: REPO_ROOT,
        encoding: "utf8",
        env: { ...process.env, PATH: `${bin}:${process.env.PATH ?? ""}`, INSTALLER_CALLS: calls, ...env },
      });
      return { result, calls: existsSync(calls) ? readFileSync(calls, "utf8") : "" };
    },
  };
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

  it("refuses a symlinked surface before remove can mutate its target", () => {
    const harness = installerHarness();
    const target = join(harness.root, "target");
    const surface = join(harness.root, "surface");
    mkdirSync(target);
    symlinkSync(target, surface);

    const { result, calls } = harness.run(["--remove"], { MIMIR_FRESHNESS_DIR: surface });
    expect(result.status).not.toBe(0);
    expect(calls).not.toMatch(/^(rm|rmdir|install|chown|chmod|groupadd) /m);
  });

  it("refuses a symlinked fixed record before apply mutates the surface", () => {
    const harness = installerHarness();
    const surface = join(harness.root, "surface");
    const target = join(harness.root, "target");
    mkdirSync(surface);
    writeFileSync(target, "not a freshness record");
    symlinkSync(target, join(surface, "backup.json"));

    const { result, calls } = harness.run([], { MIMIR_FRESHNESS_DIR: surface });
    expect(result.status).not.toBe(0);
    expect(calls).not.toMatch(/^(install|chown|chmod|groupadd) /m);
  });

  it("refuses an unexpected fixed record type before remove mutates the surface", () => {
    const harness = installerHarness();
    const surface = join(harness.root, "surface");
    mkdirSync(join(surface, "sync.json"), { recursive: true });

    const { result, calls } = harness.run(["--remove"], { MIMIR_FRESHNESS_DIR: surface });
    expect(result.status).not.toBe(0);
    expect(calls).not.toMatch(/^(rm|rmdir|install|chown|chmod|groupadd) /m);
  });

  it("rejects root paths, unexpected record types, and option-like identities before mutation", () => {
    const rootPath = installerHarness();
    expect(rootPath.run([], { MIMIR_FRESHNESS_DIR: "/" }).result.status).not.toBe(0);
    expect(rootPath.run([], { MIMIR_FRESHNESS_DIR: "/" }).calls).not.toMatch(/^(install|chown|chmod|groupadd) /m);

    const typePath = installerHarness();
    const surface = join(typePath.root, "surface");
    mkdirSync(join(surface, "sync.json"), { recursive: true });
    const typed = typePath.run([], { MIMIR_FRESHNESS_DIR: surface });
    expect(typed.result.status).not.toBe(0);
    expect(typed.calls).not.toMatch(/^(install|chown|chmod|groupadd) /m);

    const identity = installerHarness();
    const invalid = identity.run([], { MIMIR_FRESHNESS_PUBLISHER_USER: "--bad" });
    expect(invalid.result.status).not.toBe(0);
    expect(invalid.calls).not.toMatch(/^(install|chown|chmod|groupadd) /m);

    const group = installerHarness();
    const invalidGroup = group.run([], { MIMIR_FRESHNESS_PROBE_GROUP: "--bad" });
    expect(invalidGroup.result.status).not.toBe(0);
    expect(invalidGroup.calls).not.toMatch(/^(install|chown|chmod|groupadd) /m);
  });
});
