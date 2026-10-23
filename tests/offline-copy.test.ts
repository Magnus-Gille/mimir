import { chmodSync, existsSync, lstatSync, mkdtempSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { afterEach, describe, expect, it, vi } from "vitest";

// Each case spawns the scripts (and real rclone) several times; a loaded host
// can take longer than the default 5 s.
vi.setConfig({ testTimeout: 30_000 });

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SCRIPT = join(REPO_ROOT, "scripts/offline-copy-no-cloud.sh");
const tempDirs: string[] = [];

function tempDir(): string {
  const dir = mkdtempSync(join(tmpdir(), "mimir-offline-test-"));
  tempDirs.push(dir);
  return dir;
}

function executable(path: string, body: string): void {
  writeFileSync(path, `#!/bin/bash\nset -euo pipefail\n${body}`);
  chmodSync(path, 0o755);
}

function sleepMs(ms: number): void {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

afterEach(() => {
  for (const dir of tempDirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

const MARKED = "mgc/customers/secret-client/";
const GHOST = "mgc/customers/ghost-client/";
const CLASSIFICATION = `default: client-confidential
paths:
  mgc/customers/: client-confidential
  ${MARKED}: {level: client-restricted, no-cloud: true}
  mgc/internal/: internal
`;
const TWO_MARKERS = `${CLASSIFICATION}  ${GHOST}: {level: client-restricted, no-cloud: true}
`;
const NO_MARKERS = `default: client-confidential
paths:
  mgc/customers/: client-confidential
  mgc/internal/: internal
`;
const REMOTE = "offline-test";
const FILTER_PREFIX = "--filter - **/.git/**";
const FILTER_SUFFIX = "--filter - **";

function markerFilters(...folders: string[]): string {
  return [
    FILTER_PREFIX,
    ...folders.flatMap((f) => [`--filter + /${f.replace(/\/$/, "")}`, `--filter + /${f.replace(/\/$/, "")}/**`]),
    FILTER_SUFFIX,
  ].join(" ");
}

function createHarness(classification: string | null = CLASSIFICATION) {
  const root = tempDir();
  const source = join(root, "mimir");
  const mount = join(root, "medium");
  const state = join(root, "state");
  const bin = join(root, "bin");
  const calls = join(root, "rclone.calls");
  const unmountCalls = join(root, "unmount.calls");
  mkdirSync(join(source, MARKED), { recursive: true });
  mkdirSync(join(source, "mgc", "internal"), { recursive: true });
  mkdirSync(mount);
  mkdirSync(bin);
  writeFileSync(join(source, MARKED, "contract.pdf"), "restricted\n");
  writeFileSync(join(source, "mgc", "internal", "notes.md"), "internal\n");
  if (classification !== null) writeFileSync(join(source, "classification.yaml"), classification);

  executable(
    join(bin, "rclone"),
    `printf '%s\\n' "$*" >> "$MOCK_RCLONE_CALLS"
case "\${1:-}" in
  config)
    if [ -n "\${MOCK_CONFIG:-}" ]; then
      printf '%b' "$MOCK_CONFIG"
    else
      printf '[%s]\\ntype = crypt\\nfilename_encryption = standard\\nremote = %s/vault\\npassword = CANARY_PASSWORD\\n' "$2" "$MIMIR_OFFLINE_MOUNT"
    fi
    ;;
  lsf)
    printf 'CANARY_RCLONE_ERR\\n' >&2
    [ "\${MOCK_LSF_RC:-0}" -eq 0 ] || exit "$MOCK_LSF_RC"
    IFS=, read -r -a tokens <<< "\${MOCK_PRESENT:-secret-client}"
    for token in "\${tokens[@]}"; do
      if [[ "$*" == *"$token"* ]]; then printf 'a/one\\na/two\\n'; fi
    done
    ;;
  sync)
    printf 'CANARY_RCLONE_OUT\\n'; printf 'CANARY_RCLONE_ERR\\n' >&2
    exit "\${MOCK_SYNC_RC:-0}"
    ;;
  cryptcheck)
    printf 'CANARY_RCLONE_OUT\\n'; printf 'CANARY_RCLONE_ERR\\n' >&2
    exit "\${MOCK_CHECK_RC:-0}"
    ;;
  copyto)
    printf 'CANARY_RCLONE_OUT\\n'; printf 'CANARY_RCLONE_ERR\\n' >&2
    cp "$2" "$MOCK_RCLONE_CALLS.receipt-copy"
    exit "\${MOCK_COPYTO_RC:-0}"
    ;;
  *) exit 0 ;;
esac
`,
  );
  executable(join(bin, "mountpoint"), `printf 'mountpoint %s\\n' "$*" >> "$MOCK_RCLONE_CALLS"\nexit "\${MOCK_MOUNTPOINT_RC:-0}"\n`);
  executable(join(bin, "umount"), `printf 'umount %s\\n' "$*" >> "$UNMOUNT_CALLS"\nprintf 'CANARY_UNMOUNT\\n'\nexit "\${MOCK_UNMOUNT_RC:-0}"\n`);
  executable(join(bin, "diskutil"), `printf 'diskutil %s\\n' "$*" >> "$UNMOUNT_CALLS"\nprintf 'CANARY_UNMOUNT\\n'\nexit "\${MOCK_UNMOUNT_RC:-0}"\n`);

  const readCalls = (file = calls): string[] =>
    existsSync(file) ? readFileSync(file, "utf8").split("\n").filter(Boolean) : [];

  return {
    root,
    source,
    mount,
    state,
    bin,
    calls: () => readCalls(calls),
    rcloneCalls: (verb: string) => readCalls(calls).filter((c) => c.startsWith(`${verb} `)),
    unmounts: () => readCalls(unmountCalls),
    receiptDir: join(state, "offline-private", "receipts"),
    receipts: () => {
      const dir = join(state, "offline-private", "receipts");
      return existsSync(dir) ? readdirSync(dir).filter((n) => n.endsWith(".receipt")) : [];
    },
    run(args: string[] = [], overrides: Record<string, string | undefined> = {}) {
      return spawnSync("bash", [SCRIPT, ...args], {
        cwd: REPO_ROOT,
        encoding: "utf8",
        env: {
          ...process.env,
          HOME: root,
          PATH: `${bin}:${process.env.PATH ?? ""}`,
          MIMIR_OFFLINE_MOUNT: mount,
          MIMIR_OFFLINE_REMOTE: REMOTE,
          MIMIR_OFFLINE_ROOT: source,
          MIMIR_OFFLINE_STATE_DIR: state,
          RCLONE_BIN: join(bin, "rclone"),
          MOCK_RCLONE_CALLS: calls,
          UNMOUNT_CALLS: unmountCalls,
          ...overrides,
        },
      });
    },
  };
}

function expectBlind(stderr: string, h: { root: string; mount: string; source: string }) {
  for (const secret of ["secret-client", "ghost-client", "contract", "CANARY", h.mount, h.source, h.root, REMOTE]) {
    expect(stderr).not.toContain(secret);
  }
  for (const line of stderr.split("\n").filter(Boolean)) {
    expect(line).toMatch(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ stage=[a-z_]+ state=(pass|fail|warning) exit=\d+( [a-z_]+=[A-Za-z0-9_]+)*$/);
  }
}

describe("offline copy of no-cloud folders: usage and classification", () => {
  it("rejects unknown arguments with exit 64 before touching anything", () => {
    const h = createHarness();
    const result = h.run(["--bogus"]);
    expect(result.status).toBe(64);
    expect(result.stderr).toContain("usage:");
    expect(h.calls()).toEqual([]);
  });

  it("requires MIMIR_OFFLINE_MOUNT", () => {
    const h = createHarness();
    const result = h.run([], { MIMIR_OFFLINE_MOUNT: "" });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("stage=preflight state=fail exit=1");
    expect(h.rcloneCalls("sync")).toHaveLength(0);
  });

  it.each([["has:colon"], ["has/slash"], [""]])("rejects remote name %j", (name) => {
    const h = createHarness();
    const result = h.run([], { MIMIR_OFFLINE_REMOTE: name === "" ? ":" : name });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("stage=preflight state=fail exit=1");
    expect(h.calls()).toEqual([]);
  });

  it("fails when the classification file is absent", () => {
    const h = createHarness(null);
    const result = h.run();
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("stage=classification state=fail exit=1 reason=absent");
    expect(h.rcloneCalls("sync")).toHaveLength(0);
    expectBlind(result.stderr, h);
  });

  it("fails with a line number when the classification is invalid", () => {
    const h = createHarness(`paths:\n  ${MARKED}: {level: client-restricted, no_cloud: true}\n`);
    const result = h.run();
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("stage=classification state=fail exit=1 reason=invalid line=2");
    expect(h.calls()).toEqual([]);
    expectBlind(result.stderr, h);
  });

  it("fails when a non-ASCII marker cannot be normalized", () => {
    const h = createHarness(`paths:\n  mgc/Åkesson/: {level: client-restricted, no-cloud: true}\n`);
    const result = h.run([], { NO_CLOUD_PYTHON: join(h.root, "no-such-python") });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("stage=classification state=fail exit=1 reason=unicode");
    expect(h.rcloneCalls("sync")).toHaveLength(0);
  });

  it("with zero markers exits 0 without copying, receipt or unmount", () => {
    const h = createHarness(NO_MARKERS);
    const result = h.run();
    expect(result.status, result.stderr).toBe(0);
    expect(result.stderr).toContain("stage=complete state=pass exit=0 no_cloud_markers=0");
    expect(h.calls()).toEqual([]);
    expect(h.unmounts()).toEqual([]);
    expect(existsSync(join(h.state, "offline-private"))).toBe(false);
  });
});

describe("offline copy of no-cloud folders: preflight", () => {
  it("fails before any sync when the medium is not mounted (mountpoint)", () => {
    const h = createHarness();
    const result = h.run([], { MOCK_MOUNTPOINT_RC: "1" });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("stage=medium state=fail exit=1");
    expect(h.rcloneCalls("sync")).toHaveLength(0);
    expect(h.rcloneCalls("lsf")).toHaveLength(0);
    expect(h.receipts()).toEqual([]);
    expectBlind(result.stderr, h);
  });

  it("falls back to comparing device ids when mountpoint is unavailable", () => {
    const h = createHarness();
    // PATH holding only the tools the script needs, so `mountpoint` is absent.
    const slim = join(h.root, "slim");
    mkdirSync(slim);
    for (const tool of ["awk", "date", "stat", "mkdir", "chmod", "mv", "rm", "cat", "dirname", "uname", "grep", "mktemp", "id", "sort", "tr", "wc", "head", "python3", "sleep", "ls", "cp", "sed", "readlink", "basename", "env"]) {
      const found = spawnSync("bash", ["-c", `command -v ${tool}`], { encoding: "utf8" }).stdout.trim();
      if (found) symlinkSync(found, join(slim, tool));
    }
    symlinkSync(join(h.bin, "rclone"), join(slim, "rclone"));
    symlinkSync(join(h.bin, "umount"), join(slim, "umount"));
    symlinkSync(join(h.bin, "diskutil"), join(slim, "diskutil"));
    const bash = spawnSync("bash", ["-c", "command -v bash"], { encoding: "utf8" }).stdout.trim();
    const same = spawnSync(bash, [SCRIPT], {
      cwd: REPO_ROOT,
      encoding: "utf8",
      env: {
        PATH: slim,
        HOME: h.root,
        MIMIR_OFFLINE_MOUNT: h.mount,
        MIMIR_OFFLINE_REMOTE: REMOTE,
        MIMIR_OFFLINE_ROOT: h.source,
        MIMIR_OFFLINE_STATE_DIR: h.state,
        RCLONE_BIN: join(h.bin, "rclone"),
        MOCK_RCLONE_CALLS: join(h.root, "rclone.calls"),
      },
    });
    // The mount directory shares its parent's device: not a mount point.
    expect(same.status, same.stderr).toBe(1);
    expect(same.stderr).toContain("stage=medium state=fail exit=1");
    expect(h.rcloneCalls("sync")).toHaveLength(0);

    // /dev is its own filesystem on macOS and Linux.
    const mounted = spawnSync(bash, [SCRIPT, "--keep-mounted"], {
      cwd: REPO_ROOT,
      encoding: "utf8",
      env: {
        PATH: slim,
        HOME: h.root,
        MIMIR_OFFLINE_MOUNT: "/dev",
        MIMIR_OFFLINE_REMOTE: REMOTE,
        MIMIR_OFFLINE_ROOT: h.source,
        MIMIR_OFFLINE_STATE_DIR: h.state,
        RCLONE_BIN: join(h.bin, "rclone"),
        MOCK_RCLONE_CALLS: join(h.root, "rclone.calls"),
        MOCK_CONFIG: "type = crypt\nfilename_encryption = standard\nremote = /dev/vault\n",
      },
    });
    expect(mounted.status, mounted.stderr).toBe(0);
    expect(h.rcloneCalls("sync")).toHaveLength(1);
  });

  it.each([
    ["relative mount", () => ({ MIMIR_OFFLINE_MOUNT: "relative/medium" })],
    ["missing mount directory", (h: ReturnType<typeof createHarness>) => ({ MIMIR_OFFLINE_MOUNT: join(h.root, "absent") })],
    ["filesystem root", () => ({ MIMIR_OFFLINE_MOUNT: "/" })],
  ])("fails on %s", (_name, overrides) => {
    const h = createHarness();
    const result = h.run([], overrides(h));
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("stage=medium state=fail exit=1");
    expect(h.rcloneCalls("sync")).toHaveLength(0);
  });

  it("fails when the crypt remote path is outside the mount (never the cloud)", () => {
    for (const remote of ["/somewhere/else", "gdrive:backup", "relative/vault", "MOUNT-sibling", "MOUNT/../escape", "MOUNT/./vault"]) {
      const h = createHarness();
      const value = remote === "MOUNT-sibling" ? `${h.mount}-sibling` : remote.replace("MOUNT", h.mount);
      const result = h.run([], {
        MOCK_CONFIG: `type = crypt\nfilename_encryption = standard\nremote = ${value}\n`,
      });
      expect(result.status, remote).toBe(1);
      expect(result.stderr, remote).toContain("stage=medium state=fail exit=1 reason=remote_outside_medium");
      expect(h.rcloneCalls("sync"), remote).toHaveLength(0);
      expectBlind(result.stderr, h);
    }
  });

  it("fails when the crypt remote resolves outside the mount through a symlink", () => {
    const h = createHarness();
    const elsewhere = join(h.root, "elsewhere");
    mkdirSync(elsewhere);
    symlinkSync(elsewhere, join(h.mount, "vault"));
    const result = h.run();
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("reason=remote_outside_medium");
    expect(h.rcloneCalls("sync")).toHaveLength(0);
  });

  it("accepts a remote equal to the mount and a trailing slash", () => {
    for (const suffix of ["", "/", "/vault/"]) {
      const h = createHarness();
      const result = h.run(["--keep-mounted"], {
        MOCK_CONFIG: `type = crypt\nfilename_encryption = standard\nremote = ${h.mount}${suffix}\n`,
      });
      expect(result.status, `${suffix}: ${result.stderr}`).toBe(0);
    }
  });

  it("fails when the remote is not a crypt or has filename encryption off", () => {
    for (const config of [
      "type = local\nremote = MOUNT/vault\n",
      "type = crypt\nfilename_encryption = off\nremote = MOUNT/vault\n",
      "[empty]\n",
    ]) {
      const h = createHarness();
      const result = h.run([], { MOCK_CONFIG: config.replace("MOUNT", h.mount) });
      expect(result.status, config).toBe(1);
      expect(result.stderr, config).toContain("stage=preflight state=fail exit=1");
      expect(h.rcloneCalls("sync"), config).toHaveLength(0);
      expectBlind(result.stderr, h);
    }
  });

  it("requires encrypted contents, standard file names and encrypted directory names", () => {
    for (const [setting, reason] of [
      ["no_data_encryption = true", "no_data_encryption"],
      ["directory_name_encryption = false", "directory_name_encryption_off"],
      ["filename_encryption = obfuscate", "filename_encryption_not_standard"],
    ]) {
      const h = createHarness();
      const base = setting.startsWith("filename_encryption") ? "" : "filename_encryption = standard\n";
      const result = h.run([], { MOCK_CONFIG: `type = crypt\n${base}${setting}\nremote = ${h.mount}/vault\n` });
      expect(result.status, setting).toBe(1);
      expect(result.stderr, setting).toContain(`stage=preflight state=fail exit=1 reason=${reason}`);
      expect(h.rcloneCalls("sync"), setting).toHaveLength(0);
    }
  });

  it("refuses rclone environment overrides that could redirect the copy", () => {
    for (const name of ["RCLONE_CRYPT_REMOTE", "RCLONE_CONFIG_OFFLINE_TEST_REMOTE", "RCLONE_TRANSFERS"]) {
      const h = createHarness();
      const result = h.run([], { [name]: "elsewhere:backup" });
      expect(result.status, name).toBe(1);
      expect(result.stderr, name).toContain("stage=preflight state=fail exit=1 reason=rclone_env");
      expect(result.stderr, name).not.toContain("elsewhere");
      expect(h.calls().filter((c) => !c.startsWith("lsf ")), name).toEqual([]);
    }
  });

  it("refuses a vault that contains a symlink", () => {
    const h = createHarness();
    const elsewhere = join(h.root, "elsewhere");
    mkdirSync(elsewhere);
    mkdirSync(join(h.mount, "vault"));
    symlinkSync(elsewhere, join(h.mount, "vault", "encrypted-current"));
    const result = h.run();
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("stage=medium state=fail exit=1 reason=symlink_in_vault");
    expect(h.rcloneCalls("sync")).toHaveLength(0);
  });

  it("fails when rclone or the archive root is missing", () => {
    const h = createHarness();
    const noRclone = h.run([], { RCLONE_BIN: join(h.root, "no-such-rclone") });
    expect(noRclone.status).toBe(1);
    expect(noRclone.stderr).toContain("stage=preflight state=fail exit=1");
    const noRoot = h.run([], { MIMIR_OFFLINE_ROOT: join(h.root, "no-such-root") });
    expect(noRoot.status).toBe(1);
    expect(noRoot.stderr).toContain("stage=preflight state=fail exit=1");
    expect(h.rcloneCalls("sync")).toHaveLength(0);
  });

  it("refuses a symlinked or foreign private state directory before copying", () => {
    const h = createHarness();
    const elsewhere = join(h.root, "elsewhere");
    mkdirSync(elsewhere);
    mkdirSync(h.state);
    symlinkSync(elsewhere, join(h.state, "offline-private"));
    const result = h.run();
    expect(result.status).toBe(1);
    expect(h.rcloneCalls("sync")).toHaveLength(0);
    expect(readdirSync(elsewhere)).toEqual([]);
  });
});

describe("offline copy of no-cloud folders: markers and filters", () => {
  it("fails on an unmatched marker unless --allow-unmatched", () => {
    const h = createHarness(TWO_MARKERS);
    const strict = h.run();
    expect(strict.status).toBe(1);
    expect(strict.stderr).toContain("stage=classification state=fail exit=1 reason=unmatched no_cloud_unmatched=1");
    expect(h.rcloneCalls("sync")).toHaveLength(0);
    expectBlind(strict.stderr, h);

    const lenient = h.run(["--allow-unmatched", "--keep-mounted"]);
    expect(lenient.status, lenient.stderr).toBe(0);
    expect(lenient.stderr).toContain("stage=classification state=pass exit=0 no_cloud_markers=2 no_cloud_unmatched=1 files=2");
    expect(h.rcloneCalls("sync")).toHaveLength(1);
    expectBlind(lenient.stderr, h);
  });

  it("counts per-marker presence with the same filter shape and fails on lsf errors", () => {
    const h = createHarness(TWO_MARKERS);
    h.run(["--allow-unmatched", "--keep-mounted"]);
    const lsf = h.rcloneCalls("lsf");
    expect(lsf).toContain(`lsf -R --files-only ${h.source} ${markerFilters(MARKED)}`);
    expect(lsf).toContain(`lsf -R --files-only ${h.source} ${markerFilters(GHOST)}`);

    const broken = createHarness();
    const result = broken.run([], { MOCK_LSF_RC: "5" });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("stage=classification state=fail");
    expect(broken.rcloneCalls("sync")).toHaveLength(0);
  });

  it("passes the exact filter sequence and a backup-dir to sync and cryptcheck", () => {
    const h = createHarness(TWO_MARKERS);
    const result = h.run(["--allow-unmatched", "--keep-mounted"]);
    expect(result.status, result.stderr).toBe(0);
    // The classification file names the marked folders, so it travels with them.
    const filters = markerFilters(MARKED, GHOST, "classification.yaml");
    const [sync] = h.rcloneCalls("sync");
    expect(sync).toMatch(new RegExp(
      `^sync ${h.source}/ ${REMOTE}:current --backup-dir ${REMOTE}:history/\\d{8}T\\d{6}Z ${filters.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`,
    ));
    const [check] = h.rcloneCalls("cryptcheck");
    expect(check).toBe(`cryptcheck ${h.source}/ ${REMOTE}:current --one-way ${filters}`);
    // Sync happens before verify, and nothing is deleted or pruned.
    const verbs = h.calls().map((c) => c.split(" ")[0]);
    expect(verbs.indexOf("sync")).toBeLessThan(verbs.indexOf("cryptcheck"));
    expect(verbs).not.toContain("purge");
    expect(verbs).not.toContain("delete");
    expect(sync).not.toContain("--delete");
    expect(sync).not.toMatch(/--(include|exclude) /);
  });
});

describe("offline copy of no-cloud folders: copy, verify, receipt, unmount", () => {
  it("copies, verifies, writes a private receipt, copies it to the medium and unmounts", () => {
    const h = createHarness();
    const result = h.run();
    expect(result.status, result.stderr).toBe(0);
    expect(result.stderr).toContain("stage=classification state=pass exit=0 no_cloud_markers=1 no_cloud_unmatched=0 files=2");
    expect(result.stderr).toContain("stage=copy state=pass exit=0");
    expect(result.stderr).toContain("stage=verify state=pass exit=0");
    expect(result.stderr).toContain("stage=receipt state=pass exit=0");
    expect(result.stderr).toContain("stage=complete state=pass exit=0 files=2 no_cloud_markers=1");
    expectBlind(result.stderr, h);

    const unmounts = h.unmounts();
    expect(unmounts).toHaveLength(1);
    expect(unmounts[0]).toMatch(new RegExp(`^(diskutil unmount|umount) ${h.mount.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`));
    expect(h.calls().at(-1)).toMatch(/^copyto /);

    const receipts = h.receipts();
    expect(receipts).toHaveLength(1);
    const id = receipts[0].match(/^offline-copy-(\d{8}T\d{6}Z)\.receipt$/)?.[1];
    expect(id).toBeTruthy();
    const body = readFileSync(join(h.receiptDir, receipts[0]), "utf8");
    expect(body).toMatch(/^receipt=mimir-offline-copy\nversion=1\ncompleted_at=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\nno_cloud_markers=1\nno_cloud_unmatched=0\nfiles=2\ncopy=pass\nverify=cryptcheck-one-way-pass\nhistory=\d{8}T\d{6}Z\n$/);
    expect(body).toContain(`history=${id}`);
    for (const secret of ["secret-client", "contract", h.mount, h.source, REMOTE]) expect(body).not.toContain(secret);
    expect(readFileSync(join(h.root, "rclone.calls.receipt-copy"), "utf8")).toBe(body);
    expect(h.rcloneCalls("copyto")[0]).toBe(`copyto ${join(h.receiptDir, receipts[0])} ${REMOTE}:receipts/offline-copy-${id}.receipt`);

    expect(statSync(join(h.receiptDir, receipts[0])).mode & 0o777).toBe(0o600);
    expect(statSync(h.receiptDir).mode & 0o777).toBe(0o700);
    expect(statSync(join(h.state, "offline-private")).mode & 0o777).toBe(0o700);
    expect(lstatSync(join(h.state, "offline-private")).isSymbolicLink()).toBe(false);
    expect(readdirSync(h.receiptDir).filter((n) => n.startsWith("."))).toEqual([]);
  });

  it("skips the unmount with --keep-mounted", () => {
    const h = createHarness();
    const result = h.run(["--keep-mounted"]);
    expect(result.status, result.stderr).toBe(0);
    expect(h.unmounts()).toEqual([]);
    expect(result.stderr).toContain("stage=complete state=pass exit=0 files=2 no_cloud_markers=1");
    expect(h.receipts()).toHaveLength(1);
  });

  it("reports an unmount failure as a warning with exit 3 after the receipt exists", () => {
    const h = createHarness();
    const result = h.run([], { MOCK_UNMOUNT_RC: "1" });
    expect(result.status).toBe(3);
    expect(result.stderr).toContain("stage=unmount state=warning exit=1");
    expect(result.stderr).not.toContain("stage=complete");
    expect(h.receipts()).toHaveLength(1);
    expect(h.rcloneCalls("copyto")).toHaveLength(1);
    expectBlind(result.stderr, h);
  });

  it("fails without a receipt or unmount when the copy fails", () => {
    const h = createHarness();
    const result = h.run([], { MOCK_SYNC_RC: "7" });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("stage=copy state=fail exit=7");
    expect(h.rcloneCalls("cryptcheck")).toHaveLength(0);
    expect(h.receipts()).toEqual([]);
    expect(h.unmounts()).toEqual([]);
    expectBlind(result.stderr, h);
  });

  it("fails without a receipt or unmount when verification fails", () => {
    const h = createHarness();
    const result = h.run([], { MOCK_CHECK_RC: "1" });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("stage=verify state=fail exit=1");
    expect(h.receipts()).toEqual([]);
    expect(h.rcloneCalls("copyto")).toHaveLength(0);
    expect(h.unmounts()).toEqual([]);
    expectBlind(result.stderr, h);
  });

  it("fails at the receipt stage, without unmounting, when the receipt cannot reach the medium", () => {
    const h = createHarness();
    const result = h.run([], { MOCK_COPYTO_RC: "1" });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("stage=receipt state=fail exit=1");
    expect(h.unmounts()).toEqual([]);
    expectBlind(result.stderr, h);
  });

  it("never prints marker names, mount paths or rclone output, even when everything fails", () => {
    const outputs = [
      createHarness().run([], { MOCK_SYNC_RC: "1" }),
      createHarness().run([], { MOCK_UNMOUNT_RC: "1" }),
    ];
    for (const result of outputs) {
      expect(result.stdout).toBe("");
      expect(result.stderr).not.toMatch(/CANARY|secret-client|medium/);
    }
  });
});

describe("offline copy of no-cloud folders: real rclone crypt", () => {
  const hasRclone = spawnSync("rclone", ["version"], { encoding: "utf8" }).status === 0;

  function walk(dir: string): string[] {
    return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
      const full = join(dir, entry.name);
      return entry.isDirectory() ? [full, ...walk(full)] : [full];
    });
  }

  it.skipIf(!hasRclone)("copies only marked folders into an encrypted vault with history and a receipt", () => {
    const root = tempDir();
    const source = join(root, "mimir");
    const mount = join(root, "medium");
    const bin = join(root, "bin");
    const state = join(root, "state");
    const config = join(root, "rclone.conf");
    const decomposed = "mgc/customers/A\u030Akesson/";
    const precomposed = "mgc/customers/\u00C5kesson/";
    const secretText = "TOPSECRET-CONTENT-ALPHA";
    mkdirSync(join(source, MARKED, ".git"), { recursive: true });
    mkdirSync(join(source, decomposed), { recursive: true });
    mkdirSync(join(source, "mgc", "internal"), { recursive: true });
    mkdirSync(mount);
    mkdirSync(bin);
    writeFileSync(join(source, MARKED, "contract.pdf"), `${secretText}-1\n`);
    writeFileSync(join(source, MARKED, ".git", "HEAD"), "ref: refs/heads/main\n");
    writeFileSync(join(source, decomposed, "nda.pdf"), `${secretText}-2\n`);
    writeFileSync(join(source, "mgc", "internal", "notes.md"), "unmarked internal notes\n");
    writeFileSync(join(source, "classification.yaml"), `default: client-confidential
paths:
  mgc/internal/: internal
  ${MARKED}: {level: client-restricted, no-cloud: true}
  ${precomposed}: {level: client-restricted, no-cloud: true}
`);
    const obscure = (value: string) => spawnSync("rclone", ["obscure", value], { encoding: "utf8" }).stdout.trim();
    writeFileSync(config, `[${REMOTE}]
type = crypt
remote = ${mount}/vault
password = ${obscure("synthetic-password")}
password2 = ${obscure("synthetic-salt")}
filename_encryption = standard
filename_encoding = base32
`);
    chmodSync(config, 0o600);
    executable(join(bin, "mountpoint"), "exit 0\n");

    const run = () => spawnSync("bash", [SCRIPT, "--keep-mounted"], {
      cwd: REPO_ROOT,
      encoding: "utf8",
      env: {
        ...process.env,
        HOME: root,
        PATH: `${bin}:${process.env.PATH ?? ""}`,
        RCLONE_CONFIG: config,
        MIMIR_OFFLINE_MOUNT: mount,
        MIMIR_OFFLINE_REMOTE: REMOTE,
        MIMIR_OFFLINE_ROOT: source,
        MIMIR_OFFLINE_STATE_DIR: state,
      },
    });
    const rclone = (...args: string[]) =>
      spawnSync("rclone", args, { encoding: "utf8", env: { ...process.env, RCLONE_CONFIG: config } });
    const remoteFiles = (path: string) =>
      rclone("lsf", "-R", "--files-only", `${REMOTE}:${path}`).stdout.normalize("NFC").split("\n").filter(Boolean).sort();

    const first = run();
    expect(first.status, first.stderr).toBe(0);
    expect(first.stderr).toContain("no_cloud_markers=2 no_cloud_unmatched=0 files=3");
    expectBlind(first.stderr, { root, mount, source });

    expect(remoteFiles("current")).toEqual([
      "classification.yaml",
      "mgc/customers/secret-client/contract.pdf",
      "mgc/customers/Åkesson/nda.pdf",
    ].sort());

    // The medium holds neither plaintext names nor plaintext content.
    const raw = walk(join(mount, "vault"));
    expect(raw.length).toBeGreaterThan(0);
    for (const path of raw) {
      const relative = path.slice(mount.length);
      // Encrypted base32 names can contain short words by chance (for example
      // "pdf"), so only tokens long enough, or with characters outside base32, count.
      for (const plain of ["customers", "secret-client", "contract", "kesson", "notes.md", "internal", ".git", "classification", ".pdf"]) {
        expect(relative.toLowerCase(), relative).not.toContain(plain.toLowerCase());
      }
      if (statSync(path).isFile()) {
        expect(readFileSync(path).includes(Buffer.from("TOPSECRET")), relative).toBe(false);
        expect(readFileSync(path).includes(Buffer.from("refs/heads"))).toBe(false);
      }
    }

    const receipts = readdirSync(join(state, "offline-private", "receipts"));
    expect(receipts).toHaveLength(1);
    const receiptName = receipts[0];
    expect(remoteFiles("receipts")).toEqual([receiptName]);
    expect(rclone("cat", `${REMOTE}:receipts/${receiptName}`).stdout)
      .toBe(readFileSync(join(state, "offline-private", "receipts", receiptName), "utf8"));
    expect(statSync(join(state, "offline-private", "receipts", receiptName)).mode & 0o777).toBe(0o600);

    // A changed marked file moves its prior version into history.
    sleepMs(1100);
    writeFileSync(join(source, MARKED, "contract.pdf"), `${secretText}-CHANGED\n`);
    const second = run();
    expect(second.status, second.stderr).toBe(0);
    const secondReceipt = readdirSync(join(state, "offline-private", "receipts")).filter((n) => n !== receiptName);
    expect(secondReceipt).toHaveLength(1);
    const historyId = readFileSync(join(state, "offline-private", "receipts", secondReceipt[0]), "utf8")
      .match(/^history=(\d{8}T\d{6}Z)$/m)?.[1];
    expect(historyId).toBeTruthy();
    expect(remoteFiles(`history/${historyId}`)).toEqual(["mgc/customers/secret-client/contract.pdf"]);
    expect(rclone("cat", `${REMOTE}:history/${historyId}/mgc/customers/secret-client/contract.pdf`).stdout).toBe(`${secretText}-1\n`);
    expect(rclone("cat", `${REMOTE}:current/mgc/customers/secret-client/contract.pdf`).stdout).toBe(`${secretText}-CHANGED\n`);
    expect(remoteFiles("receipts")).toHaveLength(2);
  }, 90_000);
});
