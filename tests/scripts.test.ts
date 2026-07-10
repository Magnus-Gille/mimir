import { chmodSync, existsSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { afterEach, describe, expect, it } from "vitest";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const tempDirs: string[] = [];

function tempDir(): string {
  const dir = mkdtempSync(join(tmpdir(), "mimir-scripts-test-"));
  tempDirs.push(dir);
  return dir;
}

function executable(path: string, body: string): void {
  writeFileSync(path, `#!/bin/bash\nset -euo pipefail\n${body}`);
  chmodSync(path, 0o755);
}

afterEach(() => {
  for (const dir of tempDirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

describe("offsite backup script", () => {
  it("keeps runtime state outside the deploy tree and bounds encrypted archive depth", () => {
    const root = tempDir();
    const source = join(root, "source");
    const state = join(root, "state");
    const calls = join(root, "rclone.calls");
    const fakeRclone = join(root, "rclone");
    const fakeDate = join(root, "date");
    mkdirSync(source);
    writeFileSync(join(source, "artifact.txt"), "artifact\n");
    mkdirSync(join(source, "project", ".git", "refs"), { recursive: true });
    writeFileSync(join(source, "project", ".git", "refs", "checkpoint"), "transient\n");
    executable(
      fakeRclone,
      `printf '%s\\n' "$*" >> "$RCLONE_CALLS"
case "\${1:-}" in
  config) printf 'type = crypt\\nfilename_encryption = standard\\n' ;;
  lsf) [[ "$*" != *"--dirs-only mimir-crypt:"* ]] || printf 'current/\\n0000000/\\na000000/\\n' ;;
  *) exit 0 ;;
esac
`,
    );
    executable(
      fakeDate,
      `case "$*" in
  "-u +%s") printf '1780000000\\n' ;;
  "-u -d 30 days ago +%s") printf '1700000000\\n' ;;
  "-u -d @1700000000 +%Y-%m-%dT%H%M%SZ") printf '2023-11-14T221320Z\\n' ;;
  "+%s") printf '1780000000\\n' ;;
  *) printf '2026-07-10T10:00:00Z\\n' ;;
esac
`,
    );

    const result = spawnSync("bash", [join(REPO_ROOT, "scripts/offsite-backup.sh")], {
      cwd: REPO_ROOT,
      encoding: "utf8",
      env: {
        ...process.env,
        HOME: root,
        PATH: `${root}:${process.env.PATH ?? ""}`,
        MIMIR_OFFSITE_ROOT: source,
        MIMIR_OFFSITE_STATE_DIR: state,
        RCLONE_BIN: fakeRclone,
        RCLONE_CALLS: calls,
      },
    });

    expect(result.status, result.stderr).toBe(0);
    expect(existsSync(join(state, "offsite.stamp"))).toBe(true);
    expect(existsSync(join(state, "offsite-backup.log"))).toBe(true);
    expect(readFileSync(join(state, "offsite-backup.log"), "utf8")).toContain(
      "offsite backup complete: 1 files mirrored",
    );
    const invocations = readFileSync(calls, "utf8");
    expect(invocations).toContain("--exclude **/.git/**");
    expect(invocations).toMatch(/--backup-dir mimir-crypt:a[0-9A-Za-z]{6}(?:\s|$)/);
    expect(invocations).not.toContain("--backup-dir mimir-crypt:archive/");
    expect(invocations).toContain("purge mimir-crypt:a000000");
    expect(invocations).not.toContain("purge mimir-crypt:0000000");
    expect(invocations).not.toContain("purge mimir-crypt:current");
  });

  it("fails closed when the remote cannot be listed for the delete gate", () => {
    const root = tempDir();
    const source = join(root, "source");
    const state = join(root, "state");
    const calls = join(root, "rclone.calls");
    const fakeRclone = join(root, "rclone");
    mkdirSync(source);
    writeFileSync(join(source, "artifact.txt"), "artifact\n");
    executable(
      fakeRclone,
      `printf '%s\\n' "$*" >> "$RCLONE_CALLS"
case "\${1:-}" in
  config) printf 'type = crypt\\nfilename_encryption = standard\\n' ;;
  lsf) [[ "$*" != *"mimir-crypt:current"* ]] || exit 17 ;;
  *) exit 0 ;;
esac
`,
    );

    const result = spawnSync("bash", [join(REPO_ROOT, "scripts/offsite-backup.sh")], {
      cwd: REPO_ROOT,
      encoding: "utf8",
      env: {
        ...process.env,
        HOME: root,
        MIMIR_OFFSITE_ROOT: source,
        MIMIR_OFFSITE_STATE_DIR: state,
        RCLONE_BIN: fakeRclone,
        RCLONE_CALLS: calls,
      },
    });

    expect(result.status).toBe(1);
    expect(result.stderr).toContain("cannot list mimir-crypt:current");
    expect(readFileSync(calls, "utf8")).not.toMatch(/^sync /m);
    expect(existsSync(join(state, "offsite.stamp"))).toBe(false);
  });
});

describe("NAS deploy script", () => {
  function mockCommands(root: string): { bin: string; calls: string } {
    const bin = join(root, "bin");
    const calls = join(root, "calls");
    mkdirSync(bin);
    executable(
      join(bin, "ssh"),
      `printf 'ssh %s\\n' "$*" >> "$DEPLOY_CALLS"
case "$*" in
  *"test -f"*) exit "\${MOCK_ENV_FILE_RC:-0}" ;;
  *"set -a;"*) exit "\${MOCK_ENV_VALUES_RC:-0}" ;;
esac
exit 0
`,
    );
    for (const command of ["npm", "rsync"]) {
      executable(join(bin, command), `printf '${command} %s\\n' "$*" >> "$DEPLOY_CALLS"\n`);
    }
    return { bin, calls };
  }

  it("fails before build or sync when required environment values are absent", () => {
    const root = tempDir();
    const { bin, calls } = mockCommands(root);
    const result = spawnSync("bash", [join(REPO_ROOT, "scripts/deploy-nas.sh"), "test-nas"], {
      cwd: REPO_ROOT,
      encoding: "utf8",
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH ?? ""}`,
        DEPLOY_CALLS: calls,
        MOCK_ENV_VALUES_RC: "1",
      },
    });

    expect(result.status).toBe(1);
    expect(result.stderr).toContain("HEIMDALL_HUB_URL");
    const invocations = readFileSync(calls, "utf8");
    expect(invocations).not.toContain("npm ");
    expect(invocations).not.toContain("rsync ");
  });

  it("continues without displaying values when all required environment values exist", () => {
    const root = tempDir();
    const { bin, calls } = mockCommands(root);
    const result = spawnSync("bash", [join(REPO_ROOT, "scripts/deploy-nas.sh"), "test-nas"], {
      cwd: REPO_ROOT,
      encoding: "utf8",
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH ?? ""}`,
        DEPLOY_CALLS: calls,
      },
    });

    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout).toContain("values not displayed");
    const invocations = readFileSync(calls, "utf8");
    expect(invocations).toContain("npm run build");
    expect(invocations).toContain("rsync ");
  });
});
