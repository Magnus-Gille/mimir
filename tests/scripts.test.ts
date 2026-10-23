import { chmodSync, copyFileSync, existsSync, linkSync, lstatSync, mkdtempSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
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

const REAL_RSYNC_PATH = spawnSync("which", ["rsync"], { encoding: "utf8" }).stdout.trim();

const SYNC_SCRIPTS = ["sync-artifacts.sh", "sync-artifacts-daemon.sh"] as const;

function mockSyncCommands(root: string): { bin: string; calls: string } {
  const bin = join(root, "bin");
  const calls = join(root, "sync.calls");
  mkdirSync(bin);

  executable(
    join(bin, "ssh"),
    `printf 'ssh %s\n' "$*" >> "$SYNC_CALLS"
exit 0
`,
  );
  executable(
    join(bin, "rsync"),
    `printf 'rsync %s\n' "$*" >> "$SYNC_CALLS"
if [ "\${MOCK_REAL_RSYNC:-0}" = 1 ]; then
  case "$*" in
    *"--delete"*)
      # Run the real rsync against local directories standing in for the NAS.
      real_args=()
      for argument in "$@"; do real_args+=("\${argument#test-nas:}"); done
      exec "$REAL_RSYNC" "\${real_args[@]}"
      ;;
  esac
fi
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
    while IFS= read -r -d '' staged; do
      relative="\${staged#"$source"}"
      target="$destination$relative"
      if [ ! -e "$target" ] && [ ! -L "$target" ]; then
        mkdir -p "$(dirname "$target")"
        mv "$staged" "$target"
      fi
    done < <(find "$source" ! -type d -print0)
    exit "\${MOCK_PROMOTE_RC:-0}"
    ;;
  *"-ani --delete --out-format=%i"*"mimir-sync-empty."*)
    # Delete-gate denominator: dry run from an empty source lists every entry
    # rsync could delete under the mirror filters.
    if [ "\${MOCK_EMPTY_DRY_RC:-0}" -ne 0 ]; then printf 'mock empty-source failure\n' >&2; exit "$MOCK_EMPTY_DRY_RC"; fi
    if [ -n "\${MOCK_EMPTY_EXTRA_OUTPUT:-}" ]; then printf '%s\n' "$MOCK_EMPTY_EXTRA_OUTPUT"; fi
    i=0; while [ "$i" -lt "\${MOCK_REMOTE_TOTAL:-100}" ]; do printf '*deleting nas-%s\n' "$i"; i=$((i + 1)); done
    exit 0
    ;;
  *"-ani --delete --out-format=%i"*)
    i=0; while [ "$i" -lt "\${MOCK_DELETES:-0}" ]; do
      case "\${MOCK_DELETE_FORMAT:-filename}" in
        bare) printf '*deleting\n' ;;
        padded) printf '*deleting   \n' ;;
        filename) printf '*deleting old-%s\n' "$i" ;;
      esac
      i=$((i + 1))
    done
    i=0; while [ "$i" -lt "\${MOCK_ADDITIONS:-0}" ]; do printf '>f+++++++\n'; i=$((i + 1)); done
    if [ "\${MOCK_ADDITIONS:-0}" -gt 0 ]; then printf '>f+++++++++\n>f       \n'; fi
    if [ -n "\${MOCK_EXTRA_OUTPUT:-}" ]; then printf '%s\n' "$MOCK_EXTRA_OUTPUT"; fi
    if [ "\${MOCK_DRY_RC:-0}" -ne 0 ]; then printf 'mock dry-run failure\n' >&2; fi
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
if [ "\${MOCK_REAL_SCAN:-0}" = 1 ]; then
  shift
  exec "$REAL_NODE" --import tsx "$REAL_SCAN_CLI" "$@"
fi
cat >/dev/null
exit "\${MOCK_SCAN_RC:-0}"
`,
  );
  return { bin, calls };
}

function createSyncHarness(script: (typeof SYNC_SCRIPTS)[number]) {
  const root = tempDir();
  const { bin, calls } = mockSyncCommands(root);
  mkdirSync(join(root, "mimir"));
  const scanCli = join(root, "secret-scan.js");
  writeFileSync(scanCli, "// placeholder scanner build output; the mock node never executes it\n");
  const args = script === "sync-artifacts.sh"
    ? [join(REPO_ROOT, "scripts", script), "test-nas"]
    : [join(REPO_ROOT, "scripts", script)];

  return {
    root,
    bin,
    run(overrides: Record<string, string> = {}, options: { cwd?: string } = {}) {
      const result = spawnSync("bash", args, {
        cwd: options.cwd ?? REPO_ROOT,
        encoding: "utf8",
        env: {
          ...process.env,
          HOME: root,
          PATH: `${bin}:${process.env.PATH ?? ""}`,
          MIMIR_NAS: "test-nas",
          MIMIR_SECRET_SCAN_CLI: scanCli,
          SYNC_CALLS: calls,
          REAL_NODE: process.execPath,
          REAL_RSYNC: REAL_RSYNC_PATH,
          REAL_SCAN_CLI: join(REPO_ROOT, "src", "cli", "secret-scan.ts"),
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

function mirrorLines(invocations: string): string[] {
  return invocations.split("\n").filter(line => line.startsWith("rsync ") && line.includes("--delete"));
}

/** Filters passed to the real mirror, as logged by the mock rsync. */
function mirrorFilters(script: (typeof SYNC_SCRIPTS)[number]): string[] {
  const { result, invocations } = runSyncScript(script);
  expect(result.status, result.stderr).toBe(0);
  const real = mirrorLines(invocations).find(line => line.includes("--max-delete="))!;
  return [...real.matchAll(/--filter=P \S+|--exclude=\S+/g)].map(match => match[0]);
}

const DEFAULT_CACHE_EXCLUDES = [
  "node_modules/", "__pycache__/", ".venv/", "venv/", ".next/",
  ".pytest_cache/", ".mypy_cache/", ".DS_Store",
];

describe.each(SYNC_SCRIPTS)("%s fail-closed sync", (script) => {
  it.skipIf(spawnSync("rsync", ["--version"]).status !== 0)("keeps NAS snapshots when the sender collides with any filesystem type", () => {
    const filters = mirrorFilters(script).filter(filter => /artifact-snapshots/.test(filter));
    expect(filters).toHaveLength(2);
    for (const kind of ["file", "directory", "symlink"]) {
      const root = tempDir();
      const sender = join(root, "sender");
      const receiver = join(root, "receiver");
      mkdirSync(sender);
      mkdirSync(join(receiver, "artifact-snapshots", "pilot"), { recursive: true });
      const protectedFile = join(receiver, "artifact-snapshots", "pilot", "manifest.json");
      writeFileSync(protectedFile, "NAS-owned");
      writeFileSync(join(sender, "original.html"), "current original");
      const collision = join(sender, "artifact-snapshots");
      if (kind === "file") writeFileSync(collision, "sender file");
      else if (kind === "symlink") symlinkSync(join(root, "elsewhere"), collision);
      else {
        mkdirSync(join(collision, "pilot"), { recursive: true });
        writeFileSync(join(collision, "pilot", "manifest.json"), "sender replacement");
      }
      const result = spawnSync("rsync", ["-a", "--delete", ...filters, `${sender}/`, `${receiver}/`], { encoding: "utf8" });
      expect(result.status, `${kind}: ${result.stderr}`).toBe(0);
      expect(readFileSync(protectedFile, "utf8")).toBe("NAS-owned");
      expect(readFileSync(join(receiver, "original.html"), "utf8")).toBe("current original");
    }
  });
  it("protects NAS-owned snapshots in both delete preview and actual mirror", () => {
    const { result, invocations } = runSyncScript(script);
    expect(result.status, result.stderr).toBe(0);
    const mirrors = mirrorLines(invocations);
    expect(mirrors).toHaveLength(2);
    expect(mirrors[0]).toContain("-ani --delete --out-format=%i");
    expect(mirrors[0]).not.toContain("2>/dev/null");
    for (const invocation of mirrors) {
      expect(invocation).toContain("--filter=P /artifact-snapshots/***");
      expect(invocation).toContain("--exclude=/artifact-snapshots");
    }
  });
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
    expect(retryInvocations).not.toContain("-ani --delete --out-format=%i");
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
    expect(invocations).not.toContain("-ani --delete --out-format=%i");
    expect(invocations).not.toContain("--max-delete=");
  });

  it("keeps binary and oversized imports staged and blocks promotion and mirroring", () => {
    const harness = createSyncHarness(script);
    const pendingRoot = join(harness.root, ".local", "state", "mimir", "import-pending");
    mkdirSync(pendingRoot, { recursive: true });
    writeFileSync(join(pendingRoot, "binary.bin"), Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x00, 0xff]));
    writeFileSync(join(pendingRoot, "oversized.txt"), Buffer.alloc(10 * 1024 * 1024 + 1, "x"));

    const { result, invocations } = harness.run({ MOCK_REAL_SCAN: "1" });
    const output = `${result.stdout ?? ""}\n${result.stderr ?? ""}`;

    expect(result.status).not.toBe(0);
    expect(output).toContain("incomplete 2 item(s)");
    expect(output).toContain("binary=1");
    expect(output).toContain("oversized=1");
    expect(existsSync(join(pendingRoot, "binary.bin"))).toBe(true);
    expect(existsSync(join(pendingRoot, "oversized.txt"))).toBe(true);
    expect(existsSync(join(harness.root, "mimir", "binary.bin"))).toBe(false);
    expect(existsSync(join(harness.root, "mimir", "oversized.txt"))).toBe(false);
    expect(invocations).not.toContain("-ani --delete --out-format=%i");
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
    expect(retryInvocations).toContain("-ani --delete --out-format=%i");
    expect(retryInvocations).toContain("--max-delete=");
    const promotionIndex = retryInvocations.lastIndexOf("import-pending/");
    expect(retryInvocations.indexOf("node ")).toBeLessThan(promotionIndex);
    expect(promotionIndex).toBeLessThan(retryInvocations.indexOf("-ani --delete --out-format=%i"));
    expect(existsSync(pendingRoot)).toBe(false);
    expect(readFileSync(join(harness.root, "mimir", "retry-me.txt"), "utf8")).toContain("staged:retry-me.txt");
  });

  it("scans exact staged paths with newline, whitespace, and CR characters before promotion", () => {
    const harness = createSyncHarness(script);
    const pending = join(harness.root, ".local", "state", "mimir", "import-pending");
    const quarantine = join(harness.root, "mimir-quarantine");
    mkdirSync(pending, { recursive: true });

    const riskyPaths = [
      join(pending, "nested", "line\nbreak.txt"),
      join(pending, " leading-and-trailing "),
      join(pending, "carriage\rreturn"),
    ];
    for (const path of riskyPaths) {
      mkdirSync(dirname(path), { recursive: true });
      writeFileSync(path, "AKIAABCDEFGHIJKLMNOP\n");
    }

    const cleanPath = join(pending, "ordinary\n nested ", " clean\r file ");
    mkdirSync(dirname(cleanPath), { recursive: true });
    writeFileSync(cleanPath, "ordinary content\n");
    const nestedControl = join(pending, "ordinary", "nested", "control.txt");
    mkdirSync(dirname(nestedControl), { recursive: true });
    writeFileSync(nestedControl, "nested control\n");

    const { result, invocations } = harness.run({ MOCK_REAL_SCAN: "1" });

    expect(result.status, result.stderr).toBe(0);
    expect(invocations).toContain("secret-scan.js ");
    expect(invocations).not.toContain("--stdin");
    for (const path of riskyPaths) {
      const relative = path.slice(pending.length + 1);
      expect(existsSync(path)).toBe(false);
      expect(existsSync(join(quarantine, relative))).toBe(true);
    }
    expect(readFileSync(join(harness.root, "mimir", "ordinary\n nested ", " clean\r file "), "utf8"))
      .toBe("ordinary content\n");
    expect(readFileSync(join(harness.root, "mimir", "ordinary", "nested", "control.txt"), "utf8"))
      .toBe("nested control\n");
    expect(existsSync(pending)).toBe(false);
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
    expect(invocations).not.toContain("-ani --delete --out-format=%i");
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

  it.each(["bare", "padded", "filename"])("counts %s rsync deletion records", (deleteFormat) => {
    const { result, invocations } = runSyncScript(script, {
      MOCK_REMOTE_TOTAL: "100",
      MOCK_DELETES: "21",
      MOCK_DELETE_FORMAT: deleteFormat,
    });
    expect(result.status).toBe(1);
    expect(`${result.stdout}\n${result.stderr}`).toContain("21/100 remote entries");
    expect(invocations).not.toContain("--max-delete=");
  });

  it("aborts when the exact delete percentage exceeds the threshold by a fraction", () => {
    const { result, invocations } = runSyncScript(script, {
      MOCK_REMOTE_TOTAL: "101",
      MOCK_DELETES: "21",
    });
    expect(result.status).toBe(1);
    expect(`${result.stdout}\n${result.stderr}`).toContain("21/101 remote entries (21%; max 1000 entries or 20%)");
    expect(invocations).not.toContain("--max-delete=");
  });

  it.each(["010", "09", "08"])("treats percentage %s as decimal", (threshold) => {
    const { result, invocations } = runSyncScript(script, {
      MOCK_REMOTE_TOTAL: "100",
      MOCK_DELETES: threshold === "08" ? "8" : "9",
      MIMIR_SYNC_MAX_DELETE_PCT: threshold,
    });
    expect(result.status, result.stderr).toBe(0);
    expect(result.stderr).not.toContain("value too great for base");
    expect(invocations).toContain("--max-delete=");
  });

  it("enforces a zero-padded decimal percentage limit", () => {
    const { result, invocations } = runSyncScript(script, {
      MOCK_REMOTE_TOTAL: "100",
      MOCK_DELETES: "9",
      MIMIR_SYNC_MAX_DELETE_PCT: "08",
    });
    expect(result.status).toBe(1);
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
    // The denominator is rsync's own view: an empty-source dry run with the
    // identical filters, and no find over ssh.
    const lines = mirrorLines(invocations);
    expect(lines).toHaveLength(3);
    const empty = lines.find(line => line.includes("mimir-sync-empty."))!;
    const filtersOf = (line: string) => [...line.matchAll(/--filter=P \S+|--exclude=\S+/g)].map(match => match[0]);
    expect(empty).toContain("-ani --delete --out-format=%i");
    expect(filtersOf(empty)).toEqual(filtersOf(lines[0]));
    expect(invocations).not.toMatch(/^ssh .*find /m);
  });

  it("stops when the dry-run fails and leaves its diagnostic visible", () => {
    const { result, invocations } = runSyncScript(script, { MOCK_DRY_RC: "23" });
    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain("mock dry-run failure");
    expect(invocations).not.toContain("--max-delete=");
  });

  it("fails closed on unexpected nonempty dry-run output", () => {
    const { result, invocations } = runSyncScript(script, { MOCK_EXTRA_OUTPUT: "deleting old.txt" });
    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain("unrecognized rsync dry-run output");
    expect(invocations).not.toContain("--max-delete=");
  });

  it("fails closed when the deletable population cannot be measured", () => {
    const { result, invocations } = runSyncScript(script, {
      MOCK_DELETES: "1",
      MOCK_EMPTY_DRY_RC: "9",
    });
    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain("mock empty-source failure");
    expect(invocations).not.toContain("--max-delete=");
  });

  it("fails closed on unrecognised empty-source dry-run output", () => {
    const { result, invocations } = runSyncScript(script, {
      MOCK_DELETES: "1",
      MOCK_EMPTY_EXTRA_OUTPUT: "surprise output",
    });
    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain("unrecognized rsync dry-run output");
    expect(invocations).not.toContain("--max-delete=");
  });

  it("fails closed when deletions are planned but nothing is deletable", () => {
    const { result, invocations } = runSyncScript(script, {
      MOCK_DELETES: "1",
      MOCK_REMOTE_TOTAL: "0",
    });
    expect(result.status).toBe(1);
    expect(`${result.stdout}\n${result.stderr}`).toContain("1/0 remote entries");
    expect(invocations).not.toContain("--max-delete=");
  });

  it("skips the empty-source measurement entirely when no deletion is planned", () => {
    const { result, invocations } = runSyncScript(script);
    expect(result.status, result.stderr).toBe(0);
    expect(invocations).not.toContain("mimir-sync-empty.");
  });

  it("removes the empty comparison directory afterwards, even when the gate aborts", () => {
    for (const deletes of ["1", "50"]) {
      const harness = createSyncHarness(script);
      harness.run({ MOCK_DELETES: deletes, MOCK_REMOTE_TOTAL: "100" });
      const state = join(harness.root, ".local", "state", "mimir");
      expect(readdirSync(state).filter(name => name.startsWith("mimir-sync-empty."))).toEqual([]);
    }
  });

  it("quotes freshness publisher configuration before handing it to the remote shell", () => {
    const { result, invocations } = runSyncScript(script, {
      MIMIR_REMOTE_FRESHNESS_DIR: "/state'; touch should-not-run; #",
      MIMIR_REMOTE_FRESHNESS_PUBLISHER: "/usr/local/libexec/mimir-publish-freshness",
    });
    expect(result.status, result.stderr).toBe(0);
    if (script === "sync-artifacts-daemon.sh") {
      expect(invocations).toContain("[ -d '/state'\\''; touch should-not-run; #' ]");
    } else {
      expect(invocations).not.toContain("publish-freshness");
    }
  });
});

it.skipIf(spawnSync("rsync", ["--version"]).status !== 0)("rsync itemized dry-run format marks deletions", () => {
  const root = tempDir();
  const sender = join(root, "sender");
  const receiver = join(root, "receiver");
  mkdirSync(sender);
  mkdirSync(receiver);
  writeFileSync(join(receiver, "old.txt"), "old");

  const result = spawnSync("rsync", ["-ani", "--delete", "--out-format=%i", `${sender}/`, `${receiver}/`], {
    encoding: "utf8",
  });
  expect(result.status, result.stderr).toBe(0);
  expect(result.stdout.split("\n").filter(line => /^\*deleting([\s]|$)/.test(line))).toHaveLength(1);
  expect(existsSync(join(receiver, "old.txt"))).toBe(true);
});

describe("sync freshness compatibility", () => {
  it("keeps the legacy heartbeat until the new publisher is explicitly configured", () => {
    const legacy = runSyncScript("sync-artifacts-daemon.sh");
    expect(legacy.result.status, legacy.result.stderr).toBe(0);
    expect(legacy.invocations).toContain("date +%s > '/home/mimir/mimir-sync.stamp'");

    const freshness = runSyncScript("sync-artifacts-daemon.sh", {
      MIMIR_REMOTE_FRESHNESS_PUBLISHER: "/usr/local/libexec/mimir-publish-freshness",
    });
    expect(freshness.result.status, freshness.result.stderr).toBe(0);
    expect(freshness.invocations).toContain("'/usr/local/libexec/mimir-publish-freshness' sync 'success'");
    expect(freshness.invocations).not.toContain("date +%s >");
  });
});

describe.each(SYNC_SCRIPTS)("%s scanner resolution", (script) => {
  /** Move the mock node out of PATH so only an explicit location can supply it. */
  function relocateNode(harness: ReturnType<typeof createSyncHarness>, name = "fake-homebrew-bin") {
    const dir = join(harness.root, name);
    mkdirSync(dir);
    copyFileSync(join(harness.bin, "node"), join(dir, "node"));
    chmodSync(join(dir, "node"), 0o755);
    rmSync(join(harness.bin, "node"));
    return dir;
  }
  const minimalPath = (harness: ReturnType<typeof createSyncHarness>) => `${harness.bin}:/usr/bin:/bin`;

  it("finds node in the augmented search path when the scheduler PATH lacks it", () => {
    const harness = createSyncHarness(script);
    const fakeHomebrew = relocateNode(harness);
    const { result, invocations } = harness.run({
      PATH: minimalPath(harness),
      MIMIR_NODE_SEARCH_PATH: `/nonexistent/first:${fakeHomebrew}`,
    });
    expect(result.status, result.stderr).toBe(0);
    expect(invocations).toMatch(/^node .*secret-scan\.js /m);
    expect(invocations).toContain("--max-delete=");
  });

  it("honours MIMIR_NODE_BIN ahead of any PATH node", () => {
    const harness = createSyncHarness(script);
    const custom = join(harness.root, "custom-node");
    executable(custom, `printf 'custom-node %s\\n' "$*" >> "$SYNC_CALLS"\ncat >/dev/null\nexit 0\n`);
    const { result, invocations } = harness.run({ MIMIR_NODE_BIN: custom });
    expect(result.status, result.stderr).toBe(0);
    expect(invocations).toContain("custom-node ");
    expect(invocations).not.toMatch(/^node /m);
  });

  it.each(["relative-node", "/nonexistent/node", "DIRECTORY"])(
    "treats an unusable MIMIR_NODE_BIN (%s) as scanner unavailable without falling back to PATH",
    (value) => {
      const harness = createSyncHarness(script);
      const bin = value === "DIRECTORY" ? harness.root : value;
      const { result, invocations } = harness.run({ MIMIR_NODE_BIN: bin });
      expect(result.status).toBe(69);
      expect(`${result.stdout}\n${result.stderr}`).toContain("class=scanner_unavailable");
      expect(invocations).not.toMatch(/^node /m);
      expect(invocations).not.toContain("-ani --delete");
      expect(invocations).not.toContain("--max-delete=");
    },
  );

  it("fails as scanner unavailable, never skipping the scan, when node exists nowhere", () => {
    const harness = createSyncHarness(script);
    relocateNode(harness);
    const { result, invocations } = harness.run({
      PATH: minimalPath(harness),
      MIMIR_NODE_SEARCH_PATH: "/nonexistent/one:/nonexistent/two",
      MOCK_IMPORTED: "kept-staged.txt",
    });
    const output = `${result.stdout}\n${result.stderr}`;
    expect(result.status).toBe(69);
    expect(output).toContain("class=scanner_unavailable");
    expect(output).toContain("node was not found");
    expect(output).not.toContain("class=scan_failed");
    expect(invocations).not.toContain("-ani --delete");
    expect(invocations).not.toContain("--max-delete=");
    expect(existsSync(join(harness.root, ".local", "state", "mimir", "import-pending", "kept-staged.txt"))).toBe(true);
    expect(existsSync(join(harness.root, "mimir", "kept-staged.txt"))).toBe(false);
  });

  it("fails as scanner unavailable when the scanner build output is missing", () => {
    const { result, invocations } = runSyncScript(script, {
      MIMIR_SECRET_SCAN_CLI: "/nonexistent/dist/cli/secret-scan.js",
    });
    const output = `${result.stdout}\n${result.stderr}`;
    expect(result.status).toBe(69);
    expect(output).toContain("class=scanner_unavailable");
    expect(output).toContain("run npm run build");
    expect(invocations).not.toMatch(/^node /m);
    expect(invocations).not.toContain("--max-delete=");
  });

  it("keeps a scanner failure a distinct class from scanner unavailable", () => {
    const { result, invocations } = runSyncScript(script, { MOCK_SCAN_RC: "17" });
    const output = `${result.stdout}\n${result.stderr}`;
    expect(result.status).toBe(1);
    expect(output).toContain("class=scan_failed");
    expect(output).not.toContain("class=scanner_unavailable");
    expect(invocations).not.toContain("--max-delete=");
  });

  it("still quarantines a secret and mirrors the rest (existing behaviour)", () => {
    const harness = createSyncHarness(script);
    const pending = join(harness.root, ".local", "state", "mimir", "import-pending");
    mkdirSync(pending, { recursive: true });
    writeFileSync(join(pending, "leaky.txt"), "AKIAABCDEFGHIJKLMNOP\n");
    const { result, invocations } = harness.run({ MOCK_REAL_SCAN: "1" });
    expect(result.status, result.stderr).toBe(0);
    expect(existsSync(join(harness.root, "mimir-quarantine", "leaky.txt"))).toBe(true);
    expect(existsSync(join(harness.root, "mimir", "leaky.txt"))).toBe(false);
    expect(invocations).toContain("--max-delete=");
  });
});

describe.each(SYNC_SCRIPTS)("%s stalled-sync visibility", (script) => {
  const TOKEN = "fleet-token-SENTINEL-1234";

  function withCurl(harness: ReturnType<typeof createSyncHarness>) {
    const args = join(harness.root, "curl.args");
    const stdin = join(harness.root, "curl.stdin");
    executable(
      join(harness.bin, "curl"),
      `printf '%s\\n' "$*" >> "$CURL_ARGS"\ncat >> "$CURL_STDIN"\nexit 0\n`,
    );
    const env = {
      HEIMDALL_HUB_URL: "http://hub.invalid/api/panels",
      HEIMDALL_FLEET_TOKEN: TOKEN,
      CURL_ARGS: args,
      CURL_STDIN: stdin,
    };
    const read = (path: string) => (existsSync(path) ? readFileSync(path, "utf8") : "");
    return { env, args: () => read(args), stdin: () => read(stdin) };
  }

  it("pushes a fail panel with only the class when the scanner is unavailable", () => {
    const harness = createSyncHarness(script);
    const curl = withCurl(harness);
    const { result } = harness.run({
      ...curl.env,
      MOCK_IMPORTED: "private-client-name.txt",
      MIMIR_SECRET_SCAN_CLI: "/nonexistent/secret-scan.js",
    });
    expect(result.status).toBe(69);
    const body = curl.args();
    expect(body).toContain("http://hub.invalid/api/panels");
    expect(body).toContain('"panel":"sync"');
    expect(body).toContain('"state":"fail"');
    expect(body).toContain("class=scanner_unavailable exit=69");
    expect(body).not.toContain("private-client-name");
    expect(curl.stdin()).toContain(`Authorization: Bearer ${TOKEN}`);
    expect(body).not.toContain(TOKEN);
    expect(`${result.stdout}\n${result.stderr}`).not.toContain(TOKEN);
  });

  it.each([
    ["import_failed", { MOCK_IMPORTED: "x.txt", MOCK_IMPORT_RC: "23" }],
    ["scan_failed", { MOCK_SCAN_RC: "17" }],
    ["dry_run_failed", { MOCK_DRY_RC: "23" }],
    ["delete_gate", { MOCK_DELETES: "50", MOCK_REMOTE_TOTAL: "100" }],
  ] as const)("pushes a fail panel for %s", (cls, overrides) => {
    const harness = createSyncHarness(script);
    const curl = withCurl(harness);
    const { result } = harness.run({ ...curl.env, ...overrides });
    expect(result.status).not.toBe(0);
    expect(curl.args()).toContain(`class=${cls} exit=`);
    expect(curl.args()).toContain('"state":"fail"');
  });

  it("pushes a pass panel after a successful mirror, and no fail panel", () => {
    const harness = createSyncHarness(script);
    const curl = withCurl(harness);
    const { result } = harness.run(curl.env);
    expect(result.status, result.stderr).toBe(0);
    expect(curl.args()).toContain('"state":"pass"');
    expect(curl.args()).not.toContain('"state":"fail"');
  });

  it("does not invoke curl without both Heimdall variables", () => {
    for (const missing of ["HEIMDALL_HUB_URL", "HEIMDALL_FLEET_TOKEN"]) {
      const harness = createSyncHarness(script);
      const curl = withCurl(harness);
      const { result } = harness.run({ ...curl.env, [missing]: "", MOCK_SCAN_RC: "17" });
      expect(result.status).not.toBe(0);
      expect(curl.args()).toBe("");
    }
  });

  it("refuses a fleet token containing a line break and keeps the sync failure", () => {
    const harness = createSyncHarness(script);
    const curl = withCurl(harness);
    const { result } = harness.run({ ...curl.env, HEIMDALL_FLEET_TOKEN: "bad\ntoken", MOCK_SCAN_RC: "17" });
    expect(result.status).toBe(1);
    expect(curl.args()).toBe("");
  });

  it("keeps the original exit status when the panel push itself fails", () => {
    const harness = createSyncHarness(script);
    const curl = withCurl(harness);
    executable(join(harness.bin, "curl"), "cat >/dev/null\nexit 22\n");
    const { result } = harness.run({ ...curl.env, MOCK_SCAN_RC: "17" });
    expect(result.status).toBe(1);
  });

  it("moves the NAS freshness signal to error on refusal (daemon only; the manual script has no publisher)", () => {
    const { result, invocations } = runSyncScript(script, {
      MIMIR_REMOTE_FRESHNESS_PUBLISHER: "/usr/local/libexec/mimir-publish-freshness",
      MOCK_SCAN_RC: "17",
    });
    expect(result.status).toBe(1);
    if (script === "sync-artifacts-daemon.sh") {
      expect(invocations).toContain("'/usr/local/libexec/mimir-publish-freshness' sync 'error'");
      expect(invocations).not.toContain("sync 'success'");
    } else {
      expect(invocations).not.toContain("mimir-publish-freshness");
    }
  });
});

describe("daemon freshness on refusal", () => {
  it("never writes the legacy heartbeat on a refusal, so the signal goes stale", () => {
    const { result, invocations } = runSyncScript("sync-artifacts-daemon.sh", { MOCK_SCAN_RC: "17" });
    expect(result.status).toBe(1);
    expect(invocations).not.toContain("mimir-sync.stamp");
  });

  it("publishes error exactly once when the push itself fails", () => {
    const { result, invocations } = runSyncScript("sync-artifacts-daemon.sh", {
      MIMIR_REMOTE_FRESHNESS_PUBLISHER: "/usr/local/libexec/mimir-publish-freshness",
      MOCK_MIRROR_RC: "12",
    });
    expect(result.status).toBe(12);
    expect(invocations.split("sync 'error'").length - 1).toBe(1);
  });
});

describe.each(SYNC_SCRIPTS)("%s cache excludes", (script) => {
  const rsyncAvailable = spawnSync("rsync", ["--version"]).status === 0;
  const filtersOf = (line: string) => [...line.matchAll(/--filter=P \S+|--exclude=\S+/g)].map(match => match[0]);

  it("applies one identical filter list to the delete gate and the real mirror", () => {
    const { result, invocations } = runSyncScript(script);
    expect(result.status, result.stderr).toBe(0);
    const [dry, real] = mirrorLines(invocations);
    expect(filtersOf(dry)).toEqual(filtersOf(real));
    for (const pattern of DEFAULT_CACHE_EXCLUDES) expect(filtersOf(real)).toContain(`--exclude=${pattern}`);
    for (const forbidden of [".git", ".git/", "dist", "dist/", "build", "build/", "worktrees", "worktrees/"]) {
      expect(filtersOf(real)).not.toContain(`--exclude=${forbidden}`);
    }
  });

  it("only adds to the defaults through MIMIR_SYNC_EXTRA_EXCLUDES", () => {
    const { result, invocations } = runSyncScript(script, {
      MIMIR_SYNC_EXTRA_EXCLUDES: "scratch/ *.tmp",
    });
    expect(result.status, result.stderr).toBe(0);
    const [dry, real] = mirrorLines(invocations);
    for (const line of [dry, real]) {
      expect(line).toContain("--exclude=scratch/");
      expect(line).toContain("--exclude=*.tmp");
      for (const pattern of DEFAULT_CACHE_EXCLUDES) expect(line).toContain(`--exclude=${pattern}`);
    }
  });

  it.each(["-x", "+ keep", "!reset", "!"])("rejects the filter-rule-looking extra exclude %j before touching the network", (value) => {
    const { result, invocations } = runSyncScript(script, { MIMIR_SYNC_EXTRA_EXCLUDES: value });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("MIMIR_SYNC_EXTRA_EXCLUDES");
    expect(invocations).toBe("");
  });

  it.skipIf(!rsyncAvailable)("never transfers or deletes caches but still mirrors the version-control dir, dist and build", () => {
    const harness = createSyncHarness(script);
    const { result: probe, invocations } = harness.run();
    expect(probe.status, probe.stderr).toBe(0);
    const filters = filtersOf(mirrorLines(invocations).find(line => line.includes("--max-delete="))!);
    const root = tempDir();
    const sender = join(root, "sender");
    const receiver = join(root, "receiver");
    const cacheLike = /node_modules|__pycache__|\.venv|venv|\.next|_cache|\.DS_Store/;
    const files = [
      "doc.md",
      "app/node_modules/pkg/index.js",
      "app/__pycache__/m.pyc",
      "app/.venv/bin/python",
      "app/venv/bin/python",
      "web/.next/build.json",
      "t/.pytest_cache/v",
      "t/.mypy_cache/v",
      "notes/.DS_Store",
      "repo/.git/HEAD",
      "repo/dist/out.js",
      "repo/build/out.js",
      "repo/.claude/worktrees/w1/file",
    ];
    for (const relative of files) {
      mkdirSync(dirname(join(sender, relative)), { recursive: true });
      writeFileSync(join(sender, relative), "content");
    }
    // Caches already mirrored by an earlier sync, plus one stale real file.
    for (const relative of ["old/node_modules/x.js", "old/.venv/y", "old/.DS_Store", "gone.md"]) {
      mkdirSync(dirname(join(receiver, relative)), { recursive: true });
      writeFileSync(join(receiver, relative), "nas");
    }

    const dry = spawnSync("rsync", ["-ani", "--delete", "--out-format=%i %n", ...filters, `${sender}/`, `${receiver}/`], { encoding: "utf8" });
    expect(dry.status, dry.stderr).toBe(0);
    const deletions = dry.stdout.split("\n").filter(line => line.startsWith("*deleting"));
    // Only the stale real file counts toward the delete gate; the cache
    // entries already on the NAS are protected, not deleted.
    expect(deletions).toEqual(["*deleting gone.md"]);
    expect(dry.stdout).not.toMatch(cacheLike);

    const real = spawnSync("rsync", ["-a", "--delete", ...filters, `${sender}/`, `${receiver}/`], { encoding: "utf8" });
    expect(real.status, real.stderr).toBe(0);
    for (const relative of ["old/node_modules/x.js", "old/.venv/y", "old/.DS_Store"]) {
      expect(existsSync(join(receiver, relative)), relative).toBe(true);
    }
    expect(existsSync(join(receiver, "gone.md"))).toBe(false);
    for (const relative of files) {
      expect(existsSync(join(receiver, relative)), relative).toBe(!cacheLike.test(relative));
    }
  }, 30_000);

  it("does not expand exclude patterns against the working directory", () => {
    const cwd = tempDir();
    for (const name of ["alpha.md", "beta.md", "gamma.md"]) writeFileSync(join(cwd, name), "x");
    const { result, invocations } = createSyncHarness(script).run(
      { MIMIR_SYNC_EXTRA_EXCLUDES: "*.md", MOCK_DELETES: "1" },
      { cwd },
    );
    expect(result.status, result.stderr).toBe(0);
    const lines = mirrorLines(invocations);
    expect(lines).toHaveLength(3);
    for (const line of lines) {
      expect(line).toContain("--exclude=*.md");
      expect(line).not.toMatch(/--exclude=(alpha|beta|gamma)\.md/);
    }
  });

  it("keeps every extra exclude when TMPDIR is unusable, since splitting needs no temp file", () => {
    const { result, invocations } = runSyncScript(script, {
      TMPDIR: "/nonexistent/tmpdir",
      MIMIR_SYNC_EXTRA_EXCLUDES: "first/ second.tmp\nthird-*",
    });
    expect(result.status, result.stderr).toBe(0);
    for (const line of mirrorLines(invocations)) {
      for (const pattern of ["first/", "second.tmp", "third-*"]) expect(line).toContain(`--exclude=${pattern}`);
    }
  });

  it("adds the extras after all defaults and in order", () => {
    const { invocations } = runSyncScript(script, { MIMIR_SYNC_EXTRA_EXCLUDES: "  one   two  " });
    const real = mirrorLines(invocations).find(line => line.includes("--max-delete="))!;
    const excludes = [...real.matchAll(/--exclude=(\S+)/g)].map(match => match[1]);
    expect(excludes).toEqual(["/artifact-snapshots", ...DEFAULT_CACHE_EXCLUDES, "one", "two"]);
  });

  describe.skipIf(!rsyncAvailable)("real rsync output through the production parser", () => {
    function realMirror(extra: Record<string, string> = {}, populate?: (sender: string, receiver: string) => void) {
      const harness = createSyncHarness(script);
      const sender = join(harness.root, "sender");
      const receiver = join(harness.root, "receiver");
      mkdirSync(sender);
      mkdirSync(receiver);
      writeFileSync(join(sender, "doc.md"), "doc");
      populate?.(sender, receiver);
      const run = harness.run({
        MIMIR_LOCAL_ROOT: sender,
        MIMIR_REMOTE_ROOT: receiver,
        MOCK_REAL_RSYNC: "1",
        MIMIR_SYNC_MAX_DELETE_PCT: "100",
        ...extra,
      });
      return { ...run, root: harness.root, sender, receiver };
    }

    it("does not stall when a removed source directory still holds protected caches on the NAS", () => {
      const { result, receiver } = realMirror({}, (_sender, rx) => {
        mkdirSync(join(rx, "gone", "node_modules"), { recursive: true });
        writeFileSync(join(rx, "gone", "node_modules", "f"), "cache");
        writeFileSync(join(rx, "gone", "stale.txt"), "stale");
        mkdirSync(join(rx, "only-cache", ".venv"), { recursive: true });
        writeFileSync(join(rx, "only-cache", ".venv", "y"), "cache");
      });
      const output = `${result.stdout}\n${result.stderr}`;
      expect(output).not.toContain("unrecognized rsync dry-run output");
      expect(result.status, output).toBe(0);
      expect(existsSync(join(receiver, "gone", "node_modules", "f"))).toBe(true);
      expect(existsSync(join(receiver, "only-cache", ".venv", "y"))).toBe(true);
      expect(existsSync(join(receiver, "gone", "stale.txt"))).toBe(false);
      expect(existsSync(join(receiver, "doc.md"))).toBe(true);
    });

    it("still counts real deletions alongside the skipped non-empty directories", () => {
      const { result, invocations } = realMirror({ MIMIR_SYNC_MAX_DELETE: "1" }, (_sender, rx) => {
        mkdirSync(join(rx, "gone", "node_modules"), { recursive: true });
        writeFileSync(join(rx, "gone", "node_modules", "f"), "cache");
        writeFileSync(join(rx, "gone", "stale.txt"), "stale");
      });
      // stale.txt is the one counted deletion; MAX_DELETE=1 trips the backstop.
      expect(result.status).toBe(1);
      expect(`${result.stdout}\n${result.stderr}`).toContain("1/");
      expect(invocations).not.toContain("--max-delete=");
    });

    const output = (run: { result: { stdout: string | null; stderr: string | null } }) => `${run.result.stdout}\n${run.result.stderr}`;

    it("derives the denominator from an empty-source rsync dry run, ignoring retained ancestors", () => {
      // Four directories survive only because they hold protected node_modules.
      // The single deletable file is 100% of what rsync could delete.
      const run = realMirror({ MIMIR_SYNC_MAX_DELETE_PCT: "20" }, (_sender, rx) => {
        for (const name of ["p1", "p2", "p3", "p4"]) {
          mkdirSync(join(rx, name, "node_modules"), { recursive: true });
          writeFileSync(join(rx, name, "node_modules", "f"), "cache");
        }
        writeFileSync(join(rx, "stale.md"), "stale");
      });
      expect(run.result.status).toBe(1);
      expect(output(run)).toContain("1/1 remote entries (100%");
      expect(existsSync(join(run.receiver, "stale.md"))).toBe(true);
      expect(existsSync(join(run.receiver, "p1", "node_modules", "f"))).toBe(true);
    });

    it("honours a cross-directory ** pattern exactly as rsync does", () => {
      // a**b/*** protects a/x/b and everything below it; a and a/x are retained.
      const run = realMirror({ MIMIR_SYNC_EXTRA_EXCLUDES: "a**b/***", MIMIR_SYNC_MAX_DELETE_PCT: "20" }, (sender, rx) => {
        writeFileSync(join(sender, "plain.md"), "p");
        writeFileSync(join(rx, "plain.md"), "p");
        writeFileSync(join(rx, "stale.md"), "stale");
        mkdirSync(join(rx, "a", "x", "b", "deep"), { recursive: true });
        writeFileSync(join(rx, "a", "x", "b", "f"), "protected");
        writeFileSync(join(rx, "a", "x", "b", "deep", "g"), "protected");
      });
      // From an empty source rsync could delete plain.md and stale.md (a and a/x
      // are retained ancestors); the real dry run deletes only stale.md.
      expect(run.result.status).toBe(1);
      expect(output(run)).toContain("1/2 remote entries (50%");
      expect(existsSync(join(run.receiver, "a", "x", "b", "deep", "g"))).toBe(true);
    });

    it("counts protected /keep*/*** directories out of the denominator", () => {
      const run = realMirror({ MIMIR_SYNC_EXTRA_EXCLUDES: "/keep*/***", MIMIR_SYNC_MAX_DELETE_PCT: "20" }, (_sender, rx) => {
        for (let i = 0; i < 99; i++) mkdirSync(join(rx, `keep-${i}`));
        writeFileSync(join(rx, "eligible.md"), "x");
      });
      expect(run.result.status).toBe(1);
      expect(output(run)).toContain("1/1 remote entries (100%");
      expect(existsSync(join(run.receiver, "eligible.md"))).toBe(true);
    });

    // 65 files are in both trees; n stale files are NAS-only. Retained
    // ancestors, a 119-file node_modules, __pycache__ and .DS_Store never count.
    const arithmetic = (stale: number) =>
      realMirror({ MIMIR_SYNC_MAX_DELETE_PCT: "20" }, (sender, rx) => {
        for (let i = 0; i < 65; i++) {
          writeFileSync(join(sender, `keep-${i}.md`), "x");
          writeFileSync(join(rx, `keep-${i}.md`), "x");
        }
        for (let i = 0; i < stale; i++) writeFileSync(join(rx, `stale-${i}.md`), "x");
        mkdirSync(join(rx, "proj", "node_modules"), { recursive: true });
        for (let i = 0; i < 119; i++) writeFileSync(join(rx, "proj", "node_modules", `f-${i}`), "x");
        mkdirSync(join(rx, "proj", "__pycache__"));
        writeFileSync(join(rx, ".DS_Store"), "x");
        mkdirSync(join(rx, "artifact-snapshots", "pilot"), { recursive: true });
        writeFileSync(join(rx, "artifact-snapshots", "pilot", "m.json"), "x");
      });

    it("aborts just above the percentage threshold of the rsync-derived total (17/82)", () => {
      const run = arithmetic(17);
      expect(run.result.status).toBe(1);
      expect(output(run)).toContain("17/82 remote entries (21%; max 1000 entries or 20%)");
    });

    it("passes just below the percentage threshold of the rsync-derived total (16/81)", () => {
      const run = arithmetic(16);
      expect(run.result.status, output(run)).toBe(0);
      expect(run.invocations).toContain("--max-delete=");
      expect(existsSync(join(run.receiver, "proj", "node_modules", "f-0"))).toBe(true);
      expect(existsSync(join(run.receiver, "artifact-snapshots", "pilot", "m.json"))).toBe(true);
      expect(existsSync(join(run.receiver, "stale-0.md"))).toBe(false);
    });

    it("leaves no empty comparison directory behind after a real run", () => {
      const run = arithmetic(16);
      const state = join(run.root, ".local", "state", "mimir");
      expect(readdirSync(state).filter(name => name.startsWith("mimir-sync-empty."))).toEqual([]);
    });
  });

  it.each([
    "cannot delete non-empty directory: some/dir",
    "rsync(12345): warning: some/dir: not empty, cannot delete",
  ])("accepts the protected-directory diagnostic %j without counting it as a deletion", (line) => {
    const { result, invocations } = runSyncScript(script, { MOCK_EXTRA_OUTPUT: line });
    expect(result.status, result.stderr).toBe(0);
    expect(invocations).toContain("--max-delete=");
  });

  it.each([
    "cannot delete: some/dir",
    "rsync: error: not empty, cannot delete",
    "warning: not empty, cannot delete extra",
  ])("still rejects the lookalike dry-run line %j", (line) => {
    const { result, invocations } = runSyncScript(script, { MOCK_EXTRA_OUTPUT: line });
    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain("unrecognized rsync dry-run output");
    expect(invocations).not.toContain("--max-delete=");
  });

  describe("configuration refusals are reported", () => {
    const withCurl = (harness: ReturnType<typeof createSyncHarness>) => {
      const args = join(harness.root, "curl.args");
      executable(join(harness.bin, "curl"), `printf '%s\\n' "$*" >> "$CURL_ARGS"\ncat >/dev/null\nexit 0\n`);
      return {
        env: { HEIMDALL_HUB_URL: "http://hub.invalid/api/panels", HEIMDALL_FLEET_TOKEN: "tok-SENTINEL", CURL_ARGS: args },
        args: () => (existsSync(args) ? readFileSync(args, "utf8") : ""),
      };
    };

    it.each([
      ["invalid extra exclude", { MIMIR_SYNC_EXTRA_EXCLUDES: "-x" }],
      ["invalid delete limit", { MIMIR_SYNC_MAX_DELETE: "abc" }],
      ["invalid delete percentage", { MIMIR_SYNC_MAX_DELETE_PCT: "101" }],
    ] as const)("pushes class=config_invalid for an %s", (_label, overrides) => {
      const harness = createSyncHarness(script);
      const curl = withCurl(harness);
      const { result } = harness.run({ ...curl.env, ...overrides });
      expect(result.status).toBe(1);
      expect(curl.args()).toContain('"state":"fail"');
      expect(curl.args()).toContain("class=config_invalid exit=1");
      expect(curl.args()).not.toContain("tok-SENTINEL");
    });

    it("moves daemon freshness to error on a configuration refusal", () => {
      const { result, invocations } = runSyncScript(script, {
        MIMIR_REMOTE_FRESHNESS_PUBLISHER: "/usr/local/libexec/mimir-publish-freshness",
        MIMIR_SYNC_EXTRA_EXCLUDES: "-x",
      });
      expect(result.status).toBe(1);
      if (script === "sync-artifacts-daemon.sh") {
        expect(invocations).toContain("'/usr/local/libexec/mimir-publish-freshness' sync 'error'");
      } else {
        expect(invocations).not.toContain("mimir-publish-freshness");
      }
    });
  });
});

describe("local backup script", () => {
  it("keeps the default log outside the deployed code tree", () => {
    const root = tempDir();
    const source = join(root, "mimir");
    const destination = join(root, "backup");
    const mount = join(root, "mount");
    const bin = join(root, "bin");
    const calls = join(root, "backup.calls");
    mkdirSync(source);
    mkdirSync(mount);
    mkdirSync(bin);
    executable(join(bin, "mountpoint"), "exit 0\n");
    executable(join(bin, "rsync"), `printf '%s\\n' "$*" >> "$BACKUP_CALLS"\n`);

    const result = spawnSync("bash", [join(REPO_ROOT, "scripts/backup-artifacts.sh")], {
      cwd: REPO_ROOT,
      encoding: "utf8",
      env: {
        ...process.env,
        HOME: root,
        PATH: `${bin}:${process.env.PATH ?? ""}`,
        BACKUP_CALLS: calls,
        MIMIR_BACKUP_DEST: destination,
        MIMIR_BACKUP_MOUNT: mount,
      },
    });

    expect(result.status, result.stderr).toBe(0);
    expect(readFileSync(calls, "utf8")).toContain(`${source}/ ${destination}`);
    expect(readFileSync(join(root, ".local/state/mimir/backup.log"), "utf8")).toContain(
      "Backup complete",
    );
  });
});

describe("offsite backup script", () => {
  it.skipIf(process.platform !== "darwin")("removes access ACLs from an existing private custom log parent", () => {
    const root = tempDir();
    const parent = join(root, "custom-private");
    const fakeRclone = join(root, "rclone");
    mkdirSync(parent, { mode: 0o700 });
    const acl = spawnSync("/bin/chmod", ["+a", "everyone allow list,search,add_file", parent], { encoding: "utf8" });
    expect(acl.status, acl.stderr).toBe(0);
    expect(spawnSync("/bin/ls", ["-lde", parent], { encoding: "utf8" }).stdout).toMatch(/\d+: .* allow /);
    executable(fakeRclone, `case "$1" in config) printf 'type = crypt\\nfilename_encryption = standard\\n';; esac\n`);
    const result = spawnSync("bash", [join(REPO_ROOT, "scripts/offsite-backup.sh"), "--dry-run"], {
      encoding: "utf8",
      env: {
        ...process.env, HOME: root, MIMIR_OFFSITE_ROOT: root,
        MIMIR_OFFSITE_STATE_DIR: join(root, "state"),
        MIMIR_OFFSITE_LOG: join(parent, "backup.log"), RCLONE_BIN: fakeRclone,
      },
    });
    expect(result.status, result.stderr).toBe(0);
    expect(spawnSync("/bin/ls", ["-lde", parent], { encoding: "utf8" }).stdout).not.toMatch(/\d+: .* allow /);
  });

  it("keeps runtime state outside the deploy tree and bounds encrypted archive depth", () => {
    const root = tempDir();
    const source = join(root, "mimir");
    const state = join(root, "state");
    const calls = join(root, "rclone.calls");
    const fakeRclone = join(root, "rclone");
    const fakeDate = join(root, "date");
    mkdirSync(source);
    writeFileSync(join(source, "artifact.txt"), "artifact\n");
    mkdirSync(state);
    chmodSync(state, 0o755);
    writeFileSync(join(state, "offsite-backup.log"), "prior\n");
    writeFileSync(join(state, "offsite.stamp"), "prior\n");
    chmodSync(join(state, "offsite-backup.log"), 0o644);
    chmodSync(join(state, "offsite.stamp"), 0o644);
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

    const result = spawnSync("bash", ["-c", "umask 0022; exec bash \"$1\"", "offsite-test", join(REPO_ROOT, "scripts/offsite-backup.sh")], {
      cwd: REPO_ROOT,
      encoding: "utf8",
      env: {
        ...process.env,
        HOME: root,
        PATH: `${root}:${process.env.PATH ?? ""}`,
        MIMIR_OFFSITE_STATE_DIR: state,
        RCLONE_BIN: fakeRclone,
        RCLONE_CALLS: calls,
      },
    });

    expect(result.status, result.stderr).toBe(0);
    const privateState = join(state, "offsite-private");
    expect(statSync(state).mode & 0o777).toBe(0o755);
    expect(readFileSync(join(state, "offsite-backup.log"), "utf8")).toBe("prior\n");
    expect(readFileSync(join(state, "offsite.stamp"), "utf8")).toBe("prior\n");
    expect(statSync(privateState).mode & 0o777).toBe(0o700);
    expect(existsSync(join(privateState, "offsite.stamp"))).toBe(true);
    expect(existsSync(join(privateState, "offsite-backup.log"))).toBe(true);
    expect(readFileSync(join(privateState, "offsite-backup.log"), "utf8")).toContain(
      "stage=complete state=pass exit=0 files=1",
    );
    expect(statSync(join(privateState, "offsite.stamp")).mode & 0o777).toBe(0o600);
    expect(statSync(join(privateState, "offsite-backup.log")).mode & 0o777).toBe(0o600);
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
    expect(result.stderr).toMatch(/stage=delete_gate state=fail exit=1/);
    expect(readFileSync(calls, "utf8")).not.toMatch(/^sync /m);
    expect(existsSync(join(state, "offsite.stamp"))).toBe(false);
  });

  it("rejects symlinked, non-regular, and multiply-linked log files before target writes", () => {
    for (const kind of ["symlink", "directory", "hardlink"]) {
      const root = tempDir();
      const source = join(root, "source");
      const state = join(root, "state");
      const fakeRclone = join(root, "rclone");
      const log = join(state, "offsite-backup.log");
      const target = join(root, "target");
      mkdirSync(source);
      mkdirSync(state);
      chmodSync(state, 0o700);
      if (kind === "symlink") {
        writeFileSync(target, "sentinel\n");
        symlinkSync(target, log);
      } else if (kind === "directory") {
        writeFileSync(target, "sentinel\n");
        mkdirSync(log);
      } else {
        writeFileSync(log, "sentinel\n");
        linkSync(log, target);
      }
      executable(fakeRclone, "printf 'type = crypt\\nfilename_encryption = standard\\n'");

      const result = spawnSync("bash", [join(REPO_ROOT, "scripts/offsite-backup.sh")], {
        cwd: REPO_ROOT,
        encoding: "utf8",
        env: {
          ...process.env,
          HOME: root,
          PATH: `${root}:${process.env.PATH ?? ""}`,
          MIMIR_OFFSITE_ROOT: source,
          MIMIR_OFFSITE_STATE_DIR: state,
          MIMIR_OFFSITE_LOG: log,
          MIMIR_OFFSITE_STAMP: join(state, "offsite.stamp"),
          RCLONE_BIN: fakeRclone,
        },
      });

      expect(result.status).toBe(1);
      expect(result.stderr).toBe("ERROR: offsite backup private state unavailable\n");
      expect(readFileSync(target, "utf8")).toBe("sentinel\n");
    }
  });

  it("reports a fixed bootstrap panel without a log file or path details", () => {
    const root = tempDir();
    const source = join(root, "source");
    const state = join(root, "state");
    const log = join(state, "offsite-backup.log");
    const target = join(root, "bootstrap-path-canary");
    const panel = join(root, "panel.calls");
    const fakeCurl = join(root, "curl");
    const fakeRclone = join(root, "rclone");
    mkdirSync(source);
    mkdirSync(state);
    chmodSync(state, 0o700);
    writeFileSync(target, "sentinel\n");
    symlinkSync(target, log);
    executable(fakeCurl, `printf '%s\\n' "$*" > "$PANEL_CALLS"
exit 17
`);
    executable(fakeRclone, "printf 'type = crypt\\nfilename_encryption = standard\\n'");

    const result = spawnSync("bash", [join(REPO_ROOT, "scripts/offsite-backup.sh")], {
      cwd: REPO_ROOT,
      encoding: "utf8",
      env: {
        ...process.env,
        HOME: root,
        PATH: `${root}:${process.env.PATH ?? ""}`,
        MIMIR_OFFSITE_ROOT: source,
        MIMIR_OFFSITE_STATE_DIR: state,
        MIMIR_OFFSITE_LOG: log,
        MIMIR_OFFSITE_STAMP: join(state, "offsite.stamp"),
        RCLONE_BIN: fakeRclone,
        PANEL_CALLS: panel,
        HEIMDALL_HUB_URL: "http://panel.invalid",
        HEIMDALL_FLEET_TOKEN: "fleet-token",
      },
    });

    expect(result.status).toBe(1);
    expect(result.stderr).toBe("ERROR: offsite backup private state unavailable\n");
    expect(readFileSync(panel, "utf8")).toContain("stage=bootstrap state=fail exit=1");
    expect(readFileSync(panel, "utf8")).not.toContain("bootstrap-path-canary");
    expect(readFileSync(target, "utf8")).toBe("sentinel\n");
    expect(lstatSync(log).isSymbolicLink()).toBe(true);
    expect(existsSync(join(state, "offsite.stamp"))).toBe(false);
  });

  it("keeps rclone output and configured paths out of normal channels", () => {
    const root = tempDir();
    const source = join(root, "source-path-canary");
    const state = join(root, "state");
    const calls = join(root, "rclone.calls");
    const panel = join(root, "panel.calls");
    const fakeRclone = join(root, "rclone");
    const fakeCurl = join(root, "curl");
    const fakeDate = join(root, "date");
    mkdirSync(source);
    writeFileSync(join(source, "artifact.txt"), "artifact\n");
    executable(
      fakeRclone,
      `printf '%s\\n' "$*" >> "$RCLONE_CALLS"
printf '%s\\n' 'rclone-stdout-artifact-name-canary'
printf '%s\\n' 'rclone-stderr-artifact-name-canary' >&2
case "\${1:-}" in
  config) printf 'type = crypt\\nfilename_encryption = standard\\n' ;;
  lsf)
    if [[ "$*" == *"--dirs-only"* ]]; then printf 'a000000/\\n'; else printf 'artifact.txt\\n'; fi
    ;;
  purge) exit "\${MOCK_PURGE_RC:-0}" ;;
  sync) exit "\${MOCK_SYNC_RC:-0}" ;;
  *) exit 0 ;;
esac
`,
    );
    executable(fakeCurl, `printf '%s\\n' "$*" > "$PANEL_CALLS"`);
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

    const run = (overrides: Record<string, string> = {}) => spawnSync(
      "bash",
      [join(REPO_ROOT, "scripts/offsite-backup.sh")],
      {
        cwd: REPO_ROOT,
        encoding: "utf8",
        env: {
          ...process.env,
          HOME: root,
          PATH: `${root}:${process.env.PATH ?? ""}`,
          MIMIR_OFFSITE_ROOT: source,
          MIMIR_OFFSITE_STATE_DIR: state,
          MIMIR_OFFSITE_REMOTE: "remote-canary",
          RCLONE_BIN: fakeRclone,
          RCLONE_CALLS: calls,
          PANEL_CALLS: panel,
          HEIMDALL_HUB_URL: "http://panel.invalid",
          HEIMDALL_FLEET_TOKEN: "fleet-token",
          ...overrides,
        },
      },
    );
    const channels = () => `${readFileSync(join(state, "offsite-private", "offsite-backup.log"), "utf8")}\n${readFileSync(panel, "utf8")}`;

    const success = run();
    expect(success.status, success.stderr).toBe(0);
    expect(`${success.stdout}\n${success.stderr}\n${channels()}`).not.toContain("rclone-stdout-artifact-name-canary");
    expect(`${success.stdout}\n${success.stderr}\n${channels()}`).not.toContain("source-path-canary");
    expect(readFileSync(calls, "utf8")).not.toContain("--log-file");
    expect(readFileSync(calls, "utf8")).not.toContain("--log-level");
    expect(readFileSync(panel, "utf8")).toContain('"state":"pass"');

    const failure = run({ MOCK_SYNC_RC: "23" });
    expect(failure.status).toBe(23);
    expect(failure.stderr).toMatch(/stage=sync state=fail exit=23/);
    expect(`${failure.stdout}\n${failure.stderr}\n${channels()}`).not.toContain("rclone-stderr-artifact-name-canary");
    expect(readFileSync(panel, "utf8")).toContain('"state":"fail"');

    const prune = run({ MOCK_PURGE_RC: "17" });
    expect(prune.status).toBe(0);
    expect(readFileSync(join(state, "offsite-private", "offsite-backup.log"), "utf8")).toMatch(
      /stage=prune state=warning exit=1/,
    );
    expect(readFileSync(panel, "utf8")).toContain('"state":"pass"');
    expect(channels()).not.toContain("rclone-stderr-artifact-name-canary");
  });
  function createOffsitePanelHarness() {
    const root = tempDir();
    const source = join(root, "mimir");
    const state = join(root, "state");
    const bin = join(root, "bin");
    const rcloneCalls = join(root, "rclone.calls");
    const curlArgs = join(root, "curl.args");
    const curlStdin = join(root, "curl.stdin");
    const token = "synthetic-fleet-token-0123456789";
    mkdirSync(source);
    mkdirSync(bin);
    writeFileSync(join(source, "artifact.txt"), "artifact\n");

    executable(
      join(bin, "rclone"),
      `printf '%s\\n' "$*" >> "$RCLONE_CALLS"
case "\${1:-}" in
  config) printf 'type = crypt\\nfilename_encryption = standard\\n' ;;
  lsf) [[ "$*" != *"--dirs-only mimir-crypt:"* ]] || printf 'current/\\n' ;;
  sync)
    [ -z "\${MOCK_SYNC_STDERR:-}" ] || cat "$MOCK_SYNC_STDERR" >&2
    [ -z "\${MOCK_SYNC_STDOUT:-}" ] || cat "$MOCK_SYNC_STDOUT"
    exit "\${MOCK_SYNC_RC:-0}"
    ;;
  *) exit 0 ;;
esac
`,
    );
    executable(
      join(bin, "curl"),
      `set -euo pipefail
printf '%s\\n' "$*" >> "$CURL_ARGS"
cat > "$CURL_STDIN"
if [[ "$*" == *"$EXPECTED_TOKEN"* ]]; then
  printf 'token-in-argv' > "$CURL_LEAK"
fi
exit "\${MOCK_CURL_RC:-0}"
`,
    );

    return {
      token,
      root,
      run(overrides: Record<string, string> = {}) {
        const result = spawnSync("bash", [join(REPO_ROOT, "scripts/offsite-backup.sh")], {
          cwd: REPO_ROOT,
          encoding: "utf8",
          env: {
            ...process.env,
            HOME: root,
            PATH: `${bin}:${process.env.PATH ?? ""}`,
            MIMIR_OFFSITE_ROOT: source,
            MIMIR_OFFSITE_STATE_DIR: state,
            RCLONE_BIN: join(bin, "rclone"),
            RCLONE_CALLS: rcloneCalls,
            HEIMDALL_HUB_URL: "http://hub.invalid/panels",
            HEIMDALL_FLEET_TOKEN: token,
            EXPECTED_TOKEN: token,
            CURL_ARGS: curlArgs,
            CURL_STDIN: curlStdin,
            CURL_LEAK: join(root, "curl.leak"),
            ...overrides,
          },
        });
        const stateLog = join(state, "offsite-private", "offsite-backup.log");
        return {
          result,
          privateFiles: existsSync(join(state, "offsite-private"))
            ? readdirSync(join(state, "offsite-private")).sort()
            : [],
          args: existsSync(curlArgs) ? readFileSync(curlArgs, "utf8") : "",
          stdin: existsSync(curlStdin) ? readFileSync(curlStdin, "utf8") : "",
          log: existsSync(stateLog) ? readFileSync(stateLog, "utf8") : "",
          leakedArgv: existsSync(join(root, "curl.leak")),
        };
      },
    };
  }

  it("pipes the exact auth header without exposing the token in curl argv or logs", () => {
    const harness = createOffsitePanelHarness();
    const { result, args, stdin, log, leakedArgv } = harness.run();

    expect(result.status, result.stderr).toBe(0);
    expect(args).toContain("-H @-");
    expect(args).toContain('"state":"pass"');
    expect(stdin).toBe(`Authorization: Bearer ${harness.token}\n`);
    expect(args).not.toContain(harness.token);
    expect(`${result.stdout}\n${result.stderr}\n${log}`).not.toContain(harness.token);
    expect(leakedArgv).toBe(false);
  });

  it("keeps backup failure status when a fail-panel curl also fails", () => {
    const harness = createOffsitePanelHarness();
    const { result, args, stdin, log, leakedArgv } = harness.run({
      MOCK_SYNC_RC: "23",
      MOCK_CURL_RC: "22",
    });

    expect(result.status).toBe(23);
    expect(args).toContain('"state":"fail"');
    expect(stdin).toBe(`Authorization: Bearer ${harness.token}\n`);
    expect(args).not.toContain(harness.token);
    expect(`${result.stdout}\n${result.stderr}\n${log}`).not.toContain(harness.token);
    expect(leakedArgv).toBe(false);
  });

  it("does not invoke curl when the fleet token is unset", () => {
    const harness = createOffsitePanelHarness();
    const { result, args, stdin } = harness.run({ HEIMDALL_FLEET_TOKEN: "" });

    expect(result.status, result.stderr).toBe(0);
    expect(args).toBe("");
    expect(stdin).toBe("");
  });

  it("rejects CR/LF-containing tokens without sending or logging the token", () => {
    const harness = createOffsitePanelHarness();
    const invalidToken = `${harness.token}\nsecond-line`;
    const { result, args, stdin, log } = harness.run({ HEIMDALL_FLEET_TOKEN: invalidToken });

    expect(result.status, result.stderr).toBe(0);
    expect(args).toBe("");
    expect(stdin).toBe("");
    expect(`${result.stdout}\n${result.stderr}\n${log}`).not.toContain(invalidToken);
    expect(`${result.stdout}\n${result.stderr}\n${log}`).toContain(
      "fleet token contains CR/LF",
    );
  });

  it("uses the same private bearer transport for bootstrap failures", () => {
    const harness = createOffsitePanelHarness();
    const target = join(harness.root, "bootstrap-target");
    const unsafeLog = join(harness.root, "unsafe-log");
    writeFileSync(target, "unchanged");
    symlinkSync(target, unsafeLog);
    const { result, args, stdin, leakedArgv } = harness.run({ MIMIR_OFFSITE_LOG: unsafeLog });
    expect(result.status).toBe(1);
    expect(args).toContain("stage=bootstrap state=fail exit=1");
    expect(args).toContain("-H @-");
    expect(args).not.toContain(harness.token);
    expect(stdin).toBe(`Authorization: Bearer ${harness.token}\n`);
    expect(leakedArgv).toBe(false);
    expect(readFileSync(target, "utf8")).toBe("unchanged");
  });

  it("rejects a multiline bearer before the bootstrap failure report", () => {
    const harness = createOffsitePanelHarness();
    const target = join(harness.root, "bootstrap-target");
    const unsafeLog = join(harness.root, "unsafe-log");
    writeFileSync(target, "unchanged");
    symlinkSync(target, unsafeLog);
    const invalidToken = `${harness.token}\rsecond-line`;
    const { result, args, stdin } = harness.run({
      MIMIR_OFFSITE_LOG: unsafeLog,
      HEIMDALL_FLEET_TOKEN: invalidToken,
    });
    expect(result.status).toBe(1);
    expect(args).toBe("");
    expect(stdin).toBe("");
    expect(`${result.stdout}\n${result.stderr}`).not.toContain(invalidToken);
  });

  const PATHLEN_DETAIL = "doc=offsite-backup.md#path-length-budget";
  // One rclone --use-json-log record, in the field order rclone 1.7x emits.
  const rcloneRecord = (level: string, msg: string, object?: string) => JSON.stringify({
    time: "2026-10-18T03:30:02.000000+00:00",
    level,
    msg,
    ...(object === undefined ? {} : { object, objectType: "*local.Object" }),
    source: "operations/copy.go:349",
  });
  const ONEDRIVE_PATHLEN = "Failed to copy: invalidRequest: pathIsTooLong: The specified file or folder name is too long. The URL path for all files and folders must be 400 characters or less";

  it("reports only a count of provider path-length rejections, never their names", () => {
    const harness = createOffsitePanelHarness();
    const stderr = join(harness.root, "sync.stderr");
    const stdout = join(harness.root, "sync.stdout");
    writeFileSync(stderr, [
      rcloneRecord("notice", "Can't follow symlink without -L/--copy-links", "deep/symlink-name-canary"),
      // Two distinct objects that share a prefix up to ": Failed to ".
      rcloneRecord("error", ONEDRIVE_PATHLEN, "deep/a: Failed to one-name-canary"),
      rcloneRecord("error", ONEDRIVE_PATHLEN, "deep/a: Failed to two-name-canary"),
      // Local ENAMETOOLONG, as rclone reports it for the crypt-wrapped path.
      rcloneRecord("error", "Failed to copy: open /x/cryptdest/k3fh/v9e9: file name too long", "deep/three-name-canary"),
      // A filename that imitates a summary line is still a per-object record.
      rcloneRecord("error", ONEDRIVE_PATHLEN, "Attempt 1/3 failed-name-canary.txt"),
      rcloneRecord("error", "not deleting files as there were IO errors", "Encrypted drive 'remote-name-canary:current'"),
      // Summary records carry no object; retries repeat the same objects.
      rcloneRecord("error", `Attempt 1/3 failed with 4 errors and: ${ONEDRIVE_PATHLEN}`),
      rcloneRecord("error", ONEDRIVE_PATHLEN, "deep/a: Failed to one-name-canary"),
      rcloneRecord("error", "Failed to copy: open /x/cryptdest/k3fh/v9e9: file name too long", "deep/three-name-canary"),
      rcloneRecord("notice", `Failed to sync with 4 errors: last error was: ${ONEDRIVE_PATHLEN}`),
    ].join("\n"));
    writeFileSync(stdout, "stdout-name-canary\n");

    const { result, args, log, privateFiles } = harness.run({
      MOCK_SYNC_RC: "6",
      MOCK_SYNC_STDERR: stderr,
      MOCK_SYNC_STDOUT: stdout,
    });

    expect(result.status).toBe(6);
    const event = `stage=sync state=fail exit=6 pathlen_rejections=4 ${PATHLEN_DETAIL}`;
    expect(result.stderr).toContain(event);
    expect(log).toContain(event);
    expect(args).toContain(`"message":"${event}"`);
    expect(`${result.stdout}\n${result.stderr}\n${log}\n${args}`).not.toMatch(/name-canary|pathIsTooLong|Failed to/);
    expect(privateFiles).toEqual(["offsite-backup.log"]);
    const calls = readFileSync(join(harness.root, "rclone.calls"), "utf8");
    expect(calls).toMatch(/^sync .*--use-json-log/m);
    // A failed sync publishes no heartbeat and prunes nothing.
    expect(calls).not.toMatch(/^(purge|lsf --dirs-only)/m);
  });

  it("does not let filenames decide the rejection class", () => {
    const harness = createOffsitePanelHarness();
    const stderr = join(harness.root, "sync.stderr");
    writeFileSync(stderr, [
      rcloneRecord("error", "Failed to copy: unauthenticated: token expired", "report-path too long-name-canary.txt"),
      rcloneRecord("error", "Failed to copy: unauthenticated: token expired", "x: pathIsTooLong: file name too long-name-canary"),
      rcloneRecord("error", "Failed to copy: open /src/file name too long-name-canary.txt: permission denied", "y-name-canary"),
      rcloneRecord("error", "Failed to copy: open /src/report pathIsTooLong: name-canary.txt: permission denied", "report pathIsTooLong: name-canary.txt"),
      rcloneRecord("error", "Failed to copy: open /src/a: file name too long: b-name-canary: permission denied", "a: file name too long: b-name-canary"),
      rcloneRecord("error", "Failed to copy: wrapped: invalidRequest: pathIsTooLong: name-canary", "wrapped-name-canary"),
      rcloneRecord("notice", ONEDRIVE_PATHLEN, "notice-level-name-canary"),
      // A string value cannot smuggle a structural field.
      `{"level":"error","msg":"Failed to copy: denied","object":"z\\",\\"msg\\":\\"pathIsTooLong: name-canary"}`,
      "plain-text ERROR : legacy-name-canary: Failed to copy: pathIsTooLong:",
      "",
    ].join("\n"));

    const { result, args, log, privateFiles } = harness.run({ MOCK_SYNC_RC: "7", MOCK_SYNC_STDERR: stderr });

    expect(result.status).toBe(7);
    expect(result.stderr).toMatch(/stage=sync state=fail exit=7$/m);
    expect(log).toMatch(/stage=sync state=fail exit=7$/m);
    expect(args).toContain('"message":"stage=sync state=fail exit=7"');
    expect(`${result.stdout}\n${result.stderr}\n${log}\n${args}`).not.toMatch(/pathlen_rejections|name-canary/);
    expect(privateFiles).toEqual(["offsite-backup.log"]);
  });

  it("does not let rejection records turn a successful sync into a failure or a report", () => {
    const harness = createOffsitePanelHarness();
    const stderr = join(harness.root, "sync.stderr");
    writeFileSync(stderr, `${rcloneRecord("error", ONEDRIVE_PATHLEN, "a-name-canary")}\n`);

    const { result, args, log } = harness.run({ MOCK_SYNC_STDERR: stderr });

    expect(result.status, result.stderr).toBe(0);
    expect(log).toContain("stage=complete state=pass exit=0 files=1");
    expect(args).toContain('"state":"pass"');
    expect(`${result.stdout}\n${result.stderr}\n${log}\n${args}`).not.toMatch(/pathlen_rejections|name-canary/);
  });

  it("keeps the sync exit status out of reach of rclone's output", () => {
    const harness = createOffsitePanelHarness();
    const stderr = join(harness.root, "sync.stderr");
    const stdout = join(harness.root, "sync.stdout");
    // Status-shaped lines on both streams must not make a failed sync pass,
    // and a final line without a newline must not merge with any record.
    const forged = "0\n0 0\n\n0 0\nrc=0\n";
    writeFileSync(stderr, `${forged}${rcloneRecord("error", ONEDRIVE_PATHLEN, "x-name-canary")}\n${forged}0 0`);
    writeFileSync(stdout, `${forged}0 0`);

    const { result, log } = harness.run({ MOCK_SYNC_RC: "6", MOCK_SYNC_STDERR: stderr, MOCK_SYNC_STDOUT: stdout });

    expect(result.status).toBe(6);
    expect(log).toContain(`stage=sync state=fail exit=6 pathlen_rejections=1 ${PATHLEN_DETAIL}`);
    expect(log).not.toContain("stage=complete");
  });

  it("fails closed and reports no count when the counter cannot run", () => {
    const harness = createOffsitePanelHarness();
    const stderr = join(harness.root, "sync.stderr");
    const bin = join(harness.root, "bin");
    writeFileSync(stderr, `${rcloneRecord("error", ONEDRIVE_PATHLEN, "x-name-canary")}\n`);
    // An awk that prints a plausible count and then fails: the count must be
    // discarded, and the sync failure must still be reported.
    executable(join(bin, "awk"), `case "$*" in
  *pathIsTooLong*) cat >/dev/null; printf '99\\n'; exit 2 ;;
  *) exec /usr/bin/awk "$@" ;;
esac
`);

    const failed = harness.run({ MOCK_SYNC_RC: "6", MOCK_SYNC_STDERR: stderr });
    expect(failed.result.status).toBe(6);
    expect(failed.log).toMatch(/stage=sync state=fail exit=6$/m);
    expect(failed.log).not.toMatch(/pathlen_rejections|name-canary/);

    // rclone's status is authoritative; the counter is diagnostic only.
    const succeeded = harness.run({ MOCK_SYNC_STDERR: stderr });
    expect(succeeded.result.status, succeeded.result.stderr).toBe(0);
    expect(succeeded.log).toContain("stage=complete state=pass exit=0 files=1");
    expect(succeeded.log).not.toMatch(/pathlen_rejections|name-canary/);
  });

  it("treats a counter that stops reading as a failed sync", () => {
    const harness = createOffsitePanelHarness();
    const stderr = join(harness.root, "sync.stderr");
    const bin = join(harness.root, "bin");
    writeFileSync(stderr, `${rcloneRecord("error", ONEDRIVE_PATHLEN, "x-name-canary")}\n`.repeat(4000));
    // The counter exits at once, so rclone writing its log hits a closed pipe.
    executable(join(bin, "awk"), `case "$*" in
  *pathIsTooLong*) exit 0 ;;
  *) exec /usr/bin/awk "$@" ;;
esac
`);

    const { result, log } = harness.run({ MOCK_SYNC_STDERR: stderr });

    expect(result.status).not.toBe(0);
    expect(log).toMatch(/stage=sync state=fail exit=\d+$/m);
    expect(log).not.toMatch(/stage=complete|pathlen_rejections|name-canary/);
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
  *"printf configured"*) printf '%s' "\${MOCK_HEIMDALL_STATE:-configured}" ;;
  *"head -n 1"*) printf '%s\\n' "\${MOCK_PREVIOUS_COMMIT:-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}" ;;
  *"npm ci --omit=dev"*) exit "\${MOCK_DEPENDENCY_RC:-0}" ;;
  *"sudo install -m 0644"*) exit "\${MOCK_UNIT_RC:-0}" ;;
  *"rm -f '/home/magnus/mimir-server/.deployed-commit'"*) exit "\${MOCK_MARKER_REMOVE_RC:-0}" ;;
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

  it("fails before build or sync when required deployment values are absent", () => {
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
    expect(result.stderr).toContain("MIMIR_ROOT_DIR");
    const invocations = readFileSync(calls, "utf8");
    expect(invocations).toContain("MIMIR_ROOT_DIR");
    expect(invocations).toContain("MIMIR_BASE_URL");
    expect(invocations).not.toContain("HEIMDALL_HUB_URL");
    expect(invocations).not.toContain("HEIMDALL_FLEET_TOKEN");
    expect(invocations).not.toContain("npm ");
    expect(invocations).not.toContain("rsync ");
  });

  it("warns before deployment when Heimdall reporting is disabled", () => {
    const root = tempDir();
    const { bin, calls } = mockCommands(root);
    const result = spawnSync("bash", [join(REPO_ROOT, "scripts/deploy-nas.sh"), "test-nas"], {
      cwd: REPO_ROOT,
      encoding: "utf8",
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH ?? ""}`,
        DEPLOY_CALLS: calls,
        MOCK_HEIMDALL_STATE: "disabled",
      },
    });

    expect(result.status, result.stderr).toBe(0);
    expect(result.stderr).toContain("WARNING");
    expect(result.stderr).toContain("Heimdall reporting is disabled");
    const invocations = readFileSync(calls, "utf8");
    expect(invocations).toContain("HEIMDALL_HUB_URL");
    expect(invocations).toContain("HEIMDALL_FLEET_TOKEN");
  });

  it("fails before build or sync when only one Heimdall variable is configured", () => {
    const root = tempDir();
    const { bin, calls } = mockCommands(root);
    const result = spawnSync("bash", [join(REPO_ROOT, "scripts/deploy-nas.sh"), "test-nas"], {
      cwd: REPO_ROOT,
      encoding: "utf8",
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH ?? ""}`,
        DEPLOY_CALLS: calls,
        MOCK_HEIMDALL_STATE: "partial",
      },
    });

    expect(result.status).toBe(1);
    expect(result.stderr).toContain("both HEIMDALL_HUB_URL and HEIMDALL_FLEET_TOKEN");
    const invocations = readFileSync(calls, "utf8");
    expect(invocations).not.toContain("chmod 600");
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
    expect(invocations).toContain("render-systemd-unit.sh' 'archive'");
    expect(invocations).toContain("/home/archive/mimir");
    expect(result.stdout).toContain("MIMIR_DEPLOY_USER=archive");
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
    expect(invocations).toContain("--exclude=STATUS.md");
    expect(invocations).toContain("--exclude=backup.log");
    expect(invocations).toContain("chmod 600");
    expect(invocations).toContain("npm ci --omit=dev");
    expect(invocations).not.toContain("npm install --omit=dev");
    expect(invocations).toContain("mimir-offsite.service");
    expect(invocations).toContain("mimir-offsite.timer");
    expect(invocations).toContain("http://127.0.0.1:");
    expect(invocations).toContain(".deployed-commit.tmp");
    const markerRemovalIndex = invocations.indexOf("rm -f '/home/magnus/mimir-server/.deployed-commit'");
    const gitCleanupIndex = invocations.indexOf("rm -rf '/home/magnus/mimir-server/.git'");
    const rsyncIndex = invocations.indexOf("rsync ");
    expect(markerRemovalIndex).toBeGreaterThan(-1);
    expect(markerRemovalIndex).toBeLessThan(gitCleanupIndex);
    expect(gitCleanupIndex).toBeLessThan(rsyncIndex);
    expect(invocations.indexOf(".deployed-commit.tmp")).toBeGreaterThan(
      invocations.indexOf("http://127.0.0.1:"),
    );
    expect(result.stdout).toContain("Accepted commit: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
    expect(result.stdout).toContain("Rollback: check out bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
    expect(result.stdout).toContain("MIMIR_DEPLOY_USER=magnus");
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
    expect(invocations).toContain("rm -rf '/home/magnus/mimir-server/.git'");
    expect(invocations.indexOf("rm -rf '/home/magnus/mimir-server/.git'")).toBeLessThan(
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
    expect(invocations).toContain("rm -f '/home/magnus/mimir-server/.deployed-commit'");
    expect(invocations).not.toContain("rm -rf '/home/magnus/mimir-server/.git'");
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
    expect(invocations.indexOf("rm -f '/home/magnus/mimir-server/.deployed-commit'")).toBeLessThan(
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
    expect(invocations).toContain("rm -f '/home/magnus/mimir-server/.deployed-commit'");
    expect(invocations).not.toContain(".deployed-commit.tmp");
  });
});
