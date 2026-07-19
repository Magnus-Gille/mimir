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

const SYNC_SCRIPTS = ["sync-artifacts.sh", "sync-artifacts-daemon.sh"] as const;

function mockSyncCommands(root: string): { bin: string; calls: string } {
  const bin = join(root, "bin");
  const calls = join(root, "sync.calls");
  mkdirSync(bin);

  executable(
    join(bin, "ssh"),
    `printf 'ssh %s\n' "$*" >> "$SYNC_CALLS"
case "$*" in
  *"find '/home/mimir/mimir'"*)
    [ "\${MOCK_REMOTE_COUNT_RC:-0}" -eq 0 ] || exit "$MOCK_REMOTE_COUNT_RC"
    i=0; while [ "$i" -lt "\${MOCK_REMOTE_TOTAL:-100}" ]; do printf .; i=$((i + 1)); done
    ;;
esac
exit 0
`,
  );
  executable(
    join(bin, "rsync"),
    `printf 'rsync %s\n' "$*" >> "$SYNC_CALLS"
case "$*" in
  *"mimir-inbox/"*)
    destination="\${!#}"
    if [ -n "\${MOCK_IMPORTED:-}" ]; then
      while IFS= read -r path; do
        [ -n "$path" ] || continue
        case "$path" in
          */) mkdir -p "$destination$path" ;;
          *)
            mkdir -p "$(dirname "$destination$path")"
            printf 'staged:%s\n' "$path" > "$destination$path"
            ;;
        esac
      done <<< "$MOCK_IMPORTED"
    fi
    exit "\${MOCK_IMPORT_RC:-0}"
    ;;
  *"import-pending/"*)
    previous=""
    for argument in "$@"; do source="$previous"; previous="$argument"; done
    destination="$previous"
    while IFS= read -r staged; do
      relative="\${staged#"$source"}"
      target="$destination$relative"
      if [ ! -e "$target" ] && [ ! -L "$target" ]; then
        mkdir -p "$(dirname "$target")"
        mv "$staged" "$target"
      fi
    done < <(find "$source" ! -type d -print)
    exit "\${MOCK_PROMOTE_RC:-0}"
    ;;
  *"-an --delete"*)
    i=0; while [ "$i" -lt "\${MOCK_DELETES:-0}" ]; do printf 'deleting old-%s\n' "$i"; i=$((i + 1)); done
    i=0; while [ "$i" -lt "\${MOCK_ADDITIONS:-0}" ]; do printf 'new-%s\n' "$i"; i=$((i + 1)); done
    exit "\${MOCK_DRY_RC:-0}"
    ;;
  *"--delete --max-delete="*) exit "\${MOCK_MIRROR_RC:-0}" ;;
esac
exit 0
`,
  );
  executable(
    join(bin, "node"),
    `printf 'node %s\n' "$*" >> "$SYNC_CALLS"
while IFS= read -r _line; do :; done
exit "\${MOCK_SCAN_RC:-0}"
`,
  );
  return { bin, calls };
}

function createSyncHarness(script: (typeof SYNC_SCRIPTS)[number]) {
  const root = tempDir();
  const { bin, calls } = mockSyncCommands(root);
  mkdirSync(join(root, "mimir"));
  const args = script === "sync-artifacts.sh"
    ? [join(REPO_ROOT, "scripts", script), "test-nas"]
    : [join(REPO_ROOT, "scripts", script)];

  return {
    root,
    run(overrides: Record<string, string> = {}) {
      const result = spawnSync("bash", args, {
        cwd: REPO_ROOT,
        encoding: "utf8",
        env: {
          ...process.env,
          HOME: root,
          PATH: `${bin}:${process.env.PATH ?? ""}`,
          MIMIR_NAS: "test-nas",
          SYNC_CALLS: calls,
          ...overrides,
        },
      });
      const invocations = existsSync(calls) ? readFileSync(calls, "utf8") : "";
      return { result, invocations };
    },
  };
}

function runSyncScript(
  script: (typeof SYNC_SCRIPTS)[number],
  overrides: Record<string, string> = {},
) {
  return createSyncHarness(script).run(overrides);
}

describe.each(SYNC_SCRIPTS)("%s fail-closed sync", (script) => {
  it("does not scan or mirror after an inbox import failure", () => {
    const { result, invocations } = runSyncScript(script, {
      MOCK_IMPORTED: "partial-secret.txt",
      MOCK_IMPORT_RC: "23",
    });
    expect(result.status).not.toBe(0);
    expect(invocations).not.toContain("node ");
    expect(invocations).not.toContain("--max-delete=");
  });

  it("rescans a partial import on the next invocation before any mirror", () => {
    const harness = createSyncHarness(script);
    const first = harness.run({
      MOCK_IMPORTED: "partial-secret.txt",
      MOCK_IMPORT_RC: "23",
    });
    expect(first.result.status).not.toBe(0);
    const pending = join(harness.root, ".local", "state", "mimir", "import-pending", "partial-secret.txt");
    expect(readFileSync(pending, "utf8")).toContain("staged:partial-secret.txt");

    const callsBeforeRetry = first.invocations.length;
    const second = harness.run({ MOCK_SCAN_RC: "17" });
    const retryInvocations = second.invocations.slice(callsBeforeRetry);
    expect(second.result.status).not.toBe(0);
    expect(retryInvocations).toContain("node ");
    expect(retryInvocations).not.toContain("-an --delete");
    expect(retryInvocations).not.toContain("--max-delete=");
    expect(readFileSync(pending, "utf8")).toContain("staged:partial-secret.txt");
  });

  it("does not dry-run or mirror after the secret scanner fails", () => {
    const { result, invocations } = runSyncScript(script, {
      MOCK_IMPORTED: "secret.txt",
      MOCK_SCAN_RC: "17",
    });
    expect(result.status).not.toBe(0);
    expect(invocations).toContain("node ");
    expect(invocations).not.toContain("-an --delete");
    expect(invocations).not.toContain("--max-delete=");
  });

  it("retains scanner-failed content across invocations and clears it only after success", () => {
    const harness = createSyncHarness(script);
    const first = harness.run({
      MOCK_IMPORTED: "retry-me.txt",
      MOCK_SCAN_RC: "17",
    });
    expect(first.result.status).not.toBe(0);
    const pendingRoot = join(harness.root, ".local", "state", "mimir", "import-pending");
    expect(existsSync(join(pendingRoot, "retry-me.txt"))).toBe(true);

    const callsBeforeRetry = first.invocations.length;
    const second = harness.run();
    const retryInvocations = second.invocations.slice(callsBeforeRetry);
    expect(second.result.status, second.result.stderr).toBe(0);
    expect(retryInvocations).toContain("node ");
    expect(retryInvocations).toContain("import-pending/");
    expect(retryInvocations).toContain("-an --delete");
    expect(retryInvocations).toContain("--max-delete=");
    const promotionIndex = retryInvocations.lastIndexOf("import-pending/");
    expect(retryInvocations.indexOf("node ")).toBeLessThan(promotionIndex);
    expect(promotionIndex).toBeLessThan(retryInvocations.indexOf("-an --delete"));
    expect(existsSync(pendingRoot)).toBe(false);
    expect(readFileSync(join(harness.root, "mimir", "retry-me.txt"), "utf8")).toContain("staged:retry-me.txt");
  });

  it("preserves both files and refuses to mirror when a pending path collides locally", () => {
    const harness = createSyncHarness(script);
    const local = join(harness.root, "mimir", "collision.txt");
    writeFileSync(local, "existing-local\n");

    const { result, invocations } = harness.run({ MOCK_IMPORTED: "collision.txt" });
    expect(result.status).not.toBe(0);
    expect(`${result.stdout}\n${result.stderr}`).toMatch(/collision|overwriting local paths/i);
    expect(readFileSync(local, "utf8")).toBe("existing-local\n");
    expect(readFileSync(
      join(harness.root, ".local", "state", "mimir", "import-pending", "collision.txt"),
      "utf8",
    )).toContain("staged:collision.txt");
    expect(invocations).not.toContain("-an --delete");
    expect(invocations).not.toContain("--max-delete=");
  });

  it("uses remote population rather than additions to calculate delete percentage", () => {
    const { result, invocations } = runSyncScript(script, {
      MOCK_REMOTE_TOTAL: "100",
      MOCK_DELETES: "21",
      MOCK_ADDITIONS: "500",
    });
    expect(result.status).toBe(1);
    expect(`${result.stdout}\n${result.stderr}`).toContain("21/100 remote entries");
    expect(invocations).not.toContain("--max-delete=");
  });

  it("aborts at the absolute delete backstop", () => {
    const { result, invocations } = runSyncScript(script, {
      MOCK_REMOTE_TOTAL: "100",
      MOCK_DELETES: "5",
      MIMIR_SYNC_MAX_DELETE: "5",
      MIMIR_SYNC_MAX_DELETE_PCT: "100",
    });
    expect(result.status).toBe(1);
    expect(invocations).not.toContain("--max-delete=5");
  });

  it("allows the percentage boundary and carries the absolute backstop into rsync", () => {
    const { result, invocations } = runSyncScript(script, {
      MOCK_REMOTE_TOTAL: "100",
      MOCK_DELETES: "20",
      MIMIR_SYNC_MAX_DELETE: "50",
    });
    expect(result.status, result.stderr).toBe(0);
    expect(invocations).toContain("--delete --max-delete=50");
  });

  it("fails closed when the remote population cannot be measured", () => {
    const { result, invocations } = runSyncScript(script, {
      MOCK_DELETES: "1",
      MOCK_REMOTE_COUNT_RC: "9",
    });
    expect(result.status).not.toBe(0);
    expect(invocations).not.toContain("--max-delete=");
  });
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
      join(bin, "git"),
      `printf 'git %s\\n' "$*" >> "$DEPLOY_CALLS"
case "$*" in
  "status --porcelain --untracked-files=normal") printf '%s' "\${MOCK_GIT_STATUS:-}" ;;
  "rev-parse HEAD") printf '%s\\n' "\${MOCK_DEPLOY_COMMIT:-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}" ;;
esac
`,
    );
    executable(
      join(bin, "ssh"),
      `printf 'ssh %s\\n' "$*" >> "$DEPLOY_CALLS"
case "$*" in
  *"curl -fsS --max-time 3"*) exit "\${MOCK_HEALTH_RC:-0}" ;;
  *"test -f"*) exit "\${MOCK_ENV_FILE_RC:-0}" ;;
  *"head -n 1"*) printf '%s\\n' "\${MOCK_PREVIOUS_COMMIT:-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}" ;;
  *"npm ci --omit=dev"*) exit "\${MOCK_DEPENDENCY_RC:-0}" ;;
  *"sudo install -m 0644"*) exit "\${MOCK_UNIT_RC:-0}" ;;
  *"rm -f '/home/mimir/mimir-server/.deployed-commit'"*) exit "\${MOCK_MARKER_REMOVE_RC:-0}" ;;
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

  it("fails before build or sync when the required API key is absent", () => {
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
    expect(result.stderr).toContain("MIMIR_API_KEY");
    const invocations = readFileSync(calls, "utf8");
    expect(invocations).not.toContain("HEIMDALL_HUB_URL");
    expect(invocations).not.toContain("HEIMDALL_FLEET_TOKEN");
    expect(invocations).not.toContain("npm ");
    expect(invocations).not.toContain("rsync ");
  });

  it("renders systemd units for a configured deployment user", () => {
    const root = tempDir();
    const { bin, calls } = mockCommands(root);
    const result = spawnSync("bash", [join(REPO_ROOT, "scripts/deploy-nas.sh"), "test-nas"], {
      cwd: REPO_ROOT,
      encoding: "utf8",
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH ?? ""}`,
        DEPLOY_CALLS: calls,
        MIMIR_DEPLOY_USER: "archive",
      },
    });

    expect(result.status, result.stderr).toBe(0);
    const invocations = readFileSync(calls, "utf8");
    expect(invocations).toContain("archive@test-nas");
    expect(invocations).toContain("/home/archive/mimir-server");
    expect(invocations).toContain("User=archive");
    expect(invocations).toContain("/home/archive/mimir");
  });

  it("refuses a dirty source before contacting the NAS", () => {
    const root = tempDir();
    const { bin, calls } = mockCommands(root);
    const result = spawnSync("bash", [join(REPO_ROOT, "scripts/deploy-nas.sh"), "test-nas"], {
      cwd: REPO_ROOT,
      encoding: "utf8",
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH ?? ""}`,
        DEPLOY_CALLS: calls,
        MOCK_GIT_STATUS: " M src/index.ts\\n",
      },
    });

    expect(result.status).toBe(1);
    expect(result.stderr).toContain("dirty worktree");
    const invocations = readFileSync(calls, "utf8");
    expect(invocations).not.toContain("ssh ");
    expect(invocations).not.toContain("rsync ");
  });

  it("deploys deterministically and stamps only after loopback health", () => {
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
    expect(invocations).toContain("--exclude=.git");
    expect(invocations).not.toContain("--exclude=.git/");
    expect(invocations).toContain("--exclude=.deployed-commit");
    expect(invocations).toContain("chmod 600");
    expect(invocations).toContain("npm ci --omit=dev");
    expect(invocations).not.toContain("npm install --omit=dev");
    expect(invocations).toContain("mimir-offsite.service");
    expect(invocations).toContain("mimir-offsite.timer");
    expect(invocations).toContain("http://127.0.0.1:");
    expect(invocations).toContain(".deployed-commit.tmp");
    const markerRemovalIndex = invocations.indexOf("rm -f '/home/mimir/mimir-server/.deployed-commit'");
    const gitCleanupIndex = invocations.indexOf("rm -rf '/home/mimir/mimir-server/.git'");
    const rsyncIndex = invocations.indexOf("rsync ");
    expect(markerRemovalIndex).toBeGreaterThan(-1);
    expect(markerRemovalIndex).toBeLessThan(gitCleanupIndex);
    expect(gitCleanupIndex).toBeLessThan(rsyncIndex);
    expect(invocations.indexOf(".deployed-commit.tmp")).toBeGreaterThan(
      invocations.indexOf("http://127.0.0.1:"),
    );
    expect(result.stdout).toContain("Accepted commit: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
    expect(result.stdout).toContain("Rollback: check out bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
  });

  it("excludes a worktree .git file and removes stale remote Git metadata", () => {
    const root = tempDir();
    const source = join(root, "source");
    mkdirSync(source);
    writeFileSync(join(source, ".git"), "gitdir: /tmp/example-worktree-metadata\n");
    const { bin, calls } = mockCommands(root);
    const result = spawnSync("bash", [join(REPO_ROOT, "scripts/deploy-nas.sh"), "test-nas"], {
      cwd: source,
      encoding: "utf8",
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH ?? ""}`,
        DEPLOY_CALLS: calls,
      },
    });

    expect(result.status, result.stderr).toBe(0);
    const invocations = readFileSync(calls, "utf8");
    expect(readFileSync(join(source, ".git"), "utf8")).toContain("gitdir:");
    expect(invocations).toContain("--exclude=.git");
    expect(invocations).not.toContain("--exclude=.git/");
    expect(invocations).toContain("rm -rf '/home/mimir/mimir-server/.git'");
    expect(invocations.indexOf("rm -rf '/home/mimir/mimir-server/.git'")).toBeLessThan(
      invocations.indexOf("rsync "),
    );
  });

  it("reports unknown marker state and performs no code mutation when invalidation transport fails", () => {
    const root = tempDir();
    const { bin, calls } = mockCommands(root);
    const result = spawnSync("bash", [join(REPO_ROOT, "scripts/deploy-nas.sh"), "test-nas"], {
      cwd: REPO_ROOT,
      encoding: "utf8",
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH ?? ""}`,
        DEPLOY_CALLS: calls,
        MOCK_MARKER_REMOVE_RC: "255",
      },
    });

    expect(result.status).toBe(255);
    expect(result.stderr).toContain("acceptance-marker state is unknown");
    expect(result.stderr).toContain("Verify the remote marker before trusting provenance");
    const invocations = readFileSync(calls, "utf8");
    expect(invocations).toContain("rm -f '/home/mimir/mimir-server/.deployed-commit'");
    expect(invocations).not.toContain("rm -rf '/home/mimir/mimir-server/.git'");
    expect(invocations).not.toContain("rsync ");
    expect(invocations).not.toContain("npm ci --omit=dev");
    expect(invocations).not.toContain("sudo install -m 0644");
    expect(invocations).not.toContain(".deployed-commit.tmp");
  });

  it("leaves no accepted marker and prints an exact rollback after failed health", () => {
    const root = tempDir();
    const { bin, calls } = mockCommands(root);
    const result = spawnSync("bash", [join(REPO_ROOT, "scripts/deploy-nas.sh"), "test-nas"], {
      cwd: REPO_ROOT,
      encoding: "utf8",
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH ?? ""}`,
        DEPLOY_CALLS: calls,
        MOCK_HEALTH_RC: "1",
      },
    });

    expect(result.status).toBe(1);
    expect(result.stderr).toContain("acceptance marker was cleared and not recreated");
    expect(result.stderr).toContain("check out bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
    const invocations = readFileSync(calls, "utf8");
    expect(invocations.indexOf("rm -f '/home/mimir/mimir-server/.deployed-commit'")).toBeLessThan(
      invocations.indexOf("rsync "),
    );
    const markerWrites = invocations
      .split("\n")
      .filter((line) => line.includes(".deployed-commit.tmp"));
    expect(markerWrites).toHaveLength(0);
  });

  it.each([
    ["dependency installation", "MOCK_DEPENDENCY_RC"],
    ["unit refresh", "MOCK_UNIT_RC"],
  ])("leaves no accepted marker when %s fails", (_step, failureVariable) => {
    const root = tempDir();
    const { bin, calls } = mockCommands(root);
    const result = spawnSync("bash", [join(REPO_ROOT, "scripts/deploy-nas.sh"), "test-nas"], {
      cwd: REPO_ROOT,
      encoding: "utf8",
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH ?? ""}`,
        DEPLOY_CALLS: calls,
        [failureVariable]: "1",
      },
    });

    expect(result.status).toBe(1);
    expect(result.stderr).toContain("acceptance marker was cleared and not recreated");
    const invocations = readFileSync(calls, "utf8");
    expect(invocations).toContain("rm -f '/home/mimir/mimir-server/.deployed-commit'");
    expect(invocations).not.toContain(".deployed-commit.tmp");
  });
});
