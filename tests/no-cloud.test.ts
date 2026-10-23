import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { afterEach, describe, expect, it, vi } from "vitest";

// Each case spawns the scripts (and real rclone) several times; a loaded host
// can take longer than the default 5 s.
vi.setConfig({ testTimeout: 30_000 });

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const FILTER_SCRIPT = join(REPO_ROOT, "scripts/no-cloud-filter.sh");
const tempDirs: string[] = [];

function executable(path: string, body: string): void {
  writeFileSync(path, `#!/bin/bash\nset -euo pipefail\n${body}`);
  chmodSync(path, 0o755);
}

function tempDir(): string {
  const dir = mkdtempSync(join(tmpdir(), "mimir-no-cloud-test-"));
  tempDirs.push(dir);
  return dir;
}

afterEach(() => {
  for (const dir of tempDirs.splice(0)) {
    try {
      chmodSync(dir, 0o700);
    } catch {
      // already removed
    }
    rmSync(dir, { recursive: true, force: true });
  }
});

// Mirrors the structure of the owner's classification.yaml (policy v1.1).
const OWNER_SHAPE = `# ~/mimir/classification.yaml
# Folder defaults for data-classification-policy v1.1.
default: client-confidential          # L2: unknown means L2

paths:
  # MGC
  mgc/customers/:          client-confidential   # L2; raise per client
  # Per-client contract marker (decision D), for example:
  # mgc/customers/<client>/: {level: client-restricted, no-cloud: true}
  mgc/bokföring/:          client-restricted     # L3: financial records
  mgc/internal/:           internal              # L1
  transcripts/voice-memos/: client-confidential  # L2
  artifact-snapshots/:     inherit               # derived
`;

// While any folder is marked, the classification file names it, so the rules
// cover the file too (the CLI takes it to sit at the archive root).
const MANIFEST_RULES = "- /classification.yaml\n- /classification.yaml/**\n";

function filterRules(content: string | null, mode = "--exclude") {
  const root = tempDir();
  const file = join(root, "classification.yaml");
  if (content !== null) writeFileSync(file, content);
  return spawnSync("bash", [FILTER_SCRIPT, mode, file], { encoding: "utf8" });
}

describe("no-cloud classification parser", () => {
  it("accepts the owner's current file with no markers", () => {
    const result = filterRules(OWNER_SHAPE);
    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout).toBe("");
    expect(result.stderr).toContain("no_cloud_markers=0 source=file");
  });

  it("turns flow-mapping markers into rooted exclude rules and ignores no-cloud: false", () => {
    const result = filterRules(`${OWNER_SHAPE}  mgc/customers/acme/: {level: client-restricted, no-cloud: true}  # NDA
  mgc/customers/beta/: { no-cloud: false , level: client-restricted }
  mgc/customers/gamma ab/:{level: client-restricted,no-cloud: true}
`);
    // The third entry has no space after the key colon, which YAML reads as a
    // plain scalar key without a value: it must be rejected, not guessed at.
    expect(result.status).toBe(2);
    expect(result.stderr).toContain("line=16");

    const ok = filterRules(`${OWNER_SHAPE}  mgc/customers/acme/: {level: client-restricted, no-cloud: true}  # NDA
  mgc/customers/beta/: { no-cloud: false , level: client-restricted }
  mgc/customers/gamma ab/: {level: client-restricted,no-cloud: true}
`);
    expect(ok.status, ok.stderr).toBe(0);
    expect(ok.stdout).toBe([
      "- /mgc/customers/acme",
      "- /mgc/customers/acme/**",
      "- /mgc/customers/gamma ab",
      "- /mgc/customers/gamma ab/**",
      "",
    ].join("\n") + MANIFEST_RULES);
    expect(ok.stderr).toContain("no_cloud_markers=2 source=file");
    expect(ok.stderr).not.toContain("acme");
  });

  it("emits include rules that end with an exclude-everything rule", () => {
    const result = filterRules(`${OWNER_SHAPE}  mgc/bokföring/kund/: {level: client-restricted, no-cloud: true}
`, "--include");
    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout).toBe(
      "+ /mgc/kund\n+ /mgc/kund/**\n+ /classification.yaml\n+ /classification.yaml/**\n- **\n"
        .replace(/kund/g, "bokf{ö,ö}ring/kund"),
    );
  });

  it("matches precomposed and decomposed spellings of a non-ASCII marker", () => {
    // The owner's archive stores some folders decomposed (NFD) while the YAML
    // is typically precomposed (NFC); a byte-exact rule would miss them.
    for (const [spelling, forms] of [
      ["Åkesson AB", "{Å,Å}"],
      // The literal spelling always stays first and is always matched.
      ["Åkesson AB", "{Å,Å}"],
    ]) {
      const result = filterRules(`paths:\n  mgc/customers/${spelling}/: {level: client-restricted, no-cloud: true}\n`);
      expect(result.status, result.stderr).toBe(0);
      expect(result.stdout).toBe(`- /mgc/customers/${forms}kesson AB\n- /mgc/customers/${forms}kesson AB/**\n${MANIFEST_RULES}`);
    }
  });

  it("keeps the literal spelling of canonical singletons and composing jamo", () => {
    // NFC maps the Kelvin sign to K and the Angstrom sign to Å, so a rule built
    // only from normal forms would miss a folder that uses the literal sign.
    for (const [name, forms] of [
      ["\u212a", "{\u212a,K}"],
      ["\u212b", "{\u212b,\u00c5,A\u030a}"],
      ["\u1100\u1161", "{\u1100\u1161,\uac00}"],
    ]) {
      const result = filterRules(`paths:\n  clients/${name}/: {level: client-restricted, no-cloud: true}\n`);
      expect(result.status, result.stderr).toBe(0);
      expect(result.stdout).toBe(`- /clients/${forms}\n- /clients/${forms}/**\n${MANIFEST_RULES}`);
    }
  });

  it("fails closed when a non-ASCII marker cannot be normalized", () => {
    const root = tempDir();
    const file = join(root, "classification.yaml");
    writeFileSync(file, "paths:\n  mgc/customers/Åkesson/: {level: client-restricted, no-cloud: true}\n");
    const missing = spawnSync("bash", [FILTER_SCRIPT, "--exclude", file], {
      encoding: "utf8",
      env: { ...process.env, NO_CLOUD_PYTHON: join(root, "no-python3") },
    });
    expect(missing.status).toBe(4);
    expect(missing.stdout).toBe("");
    expect(missing.stderr).toContain("classification=unicode");

    writeFileSync(file, Buffer.concat([
      Buffer.from("paths:\n  mgc/customers/"),
      Buffer.from([0xff, 0xfe]),
      Buffer.from("/: {level: client-restricted, no-cloud: true}\n"),
    ]));
    const malformed = spawnSync("bash", [FILTER_SCRIPT, "--exclude", file], { encoding: "utf8" });
    expect(malformed.status).toBe(4);
    expect(malformed.stdout).toBe("");

    // ASCII-only markers never need python3.
    writeFileSync(file, "paths:\n  mgc/customers/acme/: {level: client-restricted, no-cloud: true}\n");
    const ascii = spawnSync("bash", [FILTER_SCRIPT, "--exclude", file], {
      encoding: "utf8",
      env: { ...process.env, NO_CLOUD_PYTHON: join(root, "no-python3") },
    });
    expect(ascii.status, ascii.stderr).toBe(0);
    expect(ascii.stdout).toBe(`- /mgc/customers/acme\n- /mgc/customers/acme/**\n${MANIFEST_RULES}`);
  });

  it("escapes rclone glob metacharacters in marked paths", () => {
    const result = filterRules(`paths:
  odd/a*b?[c]{d}\\e/: {level: client-restricted, no-cloud: true}
`);
    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout).toBe(`- /odd/a\\*b\\?\\[c\\]\\{d\\}\\\\e\n- /odd/a\\*b\\?\\[c\\]\\{d\\}\\\\e/**\n${MANIFEST_RULES}`);
  });

  it("treats an absent file as zero markers", () => {
    const result = filterRules(null);
    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout).toBe("");
    expect(result.stderr).toContain("no_cloud_markers=0 source=absent");
  });

  const INVALID: Array<[string, string, number]> = [
    ["misspelled marker key", "  secret-client/: {level: client-restricted, no_cloud: true}\n", 2],
    ["non-boolean marker", "  secret-client/: {level: client-restricted, no-cloud: yes}\n", 2],
    ["marker without level", "  secret-client/: {no-cloud: true}\n", 2],
    ["duplicate mapping key", "  secret-client/: {level: internal, level: internal, no-cloud: true}\n", 2],
    ["unterminated flow mapping", "  secret-client/: {level: client-restricted, no-cloud: true\n", 2],
    ["trailing text after flow mapping", "  secret-client/: {level: client-restricted, no-cloud: true} x\n", 2],
    ["empty flow element", "  secret-client/: {level: client-restricted, no-cloud: true,}\n", 2],
    ["nested block mapping", "  secret-client/:\n    level: client-restricted\n", 2],
    ["quoted key", "  \"secret-client/\": {level: client-restricted, no-cloud: true}\n", 2],
    ["absolute path", "  /secret-client/: {level: client-restricted, no-cloud: true}\n", 2],
    ["parent segment", "  a/../secret-client/: {level: client-restricted, no-cloud: true}\n", 2],
    ["dot segment", "  ./secret-client/: {level: client-restricted, no-cloud: true}\n", 2],
    ["empty segment", "  a//secret-client/: {level: client-restricted, no-cloud: true}\n", 2],
    ["marker on a non-folder path", "  secret-client: {level: client-restricted, no-cloud: true}\n", 2],
    ["duplicate path", "  mgc/internal/: internal\n", 2],
    ["inconsistent indentation", "    secret-client/: internal\n", 2],
    ["tab character", "  secret-client/:\tinternal\n", 2],
    ["non-token level", "  secret-client/: Client Restricted\n", 2],
    ["missing value", "  secret-client/: # nothing\n", 2],
    ["list item", "  - secret-client/\n", 2],
  ];

  it.each(INVALID)("fails closed on %s without echoing the line", (_name, line, code) => {
    const result = filterRules(`${OWNER_SHAPE}${line}`);
    expect(result.status).toBe(code);
    expect(result.stdout).toBe("");
    expect(result.stderr).toMatch(/classification=invalid line=1[45]\b/);
    expect(result.stderr).not.toContain("secret-client");
  });

  it.each([
    ["an empty file", ""],
    ["a file without a paths section", "default: internal\n"],
    ["an unknown top-level key", `${OWNER_SHAPE}version: 1\n`],
    ["a duplicate paths section", `${OWNER_SHAPE}paths:\n`],
    ["CRLF line endings", OWNER_SHAPE.replace(/\n/g, "\r\n")],
    ["a byte-order mark", `\uFEFF${OWNER_SHAPE}`],
    ["an indented line before paths", "  secret-client/: internal\npaths:\n"],
  ])("fails closed on %s", (_name, content) => {
    const result = filterRules(content);
    expect(result.status).toBe(2);
    expect(result.stdout).toBe("");
    expect(result.stderr).toMatch(/classification=invalid line=\d+/);
    expect(result.stderr).not.toContain("secret-client");
  });

  it("fails closed on unreadable, non-regular and symlinked files", () => {
    const root = tempDir();
    const unreadable = join(root, "unreadable.yaml");
    writeFileSync(unreadable, OWNER_SHAPE);
    chmodSync(unreadable, 0o000);
    const directory = join(root, "directory.yaml");
    mkdirSync(directory);
    const target = join(root, "target.yaml");
    writeFileSync(target, OWNER_SHAPE);
    const link = join(root, "link.yaml");
    symlinkSync(target, link);
    const dangling = join(root, "dangling.yaml");
    symlinkSync(join(root, "missing.yaml"), dangling);

    const cases = process.getuid?.() === 0 ? [directory, link, dangling] : [unreadable, directory, link, dangling];
    for (const file of cases) {
      const result = spawnSync("bash", [FILTER_SCRIPT, "--exclude", file], { encoding: "utf8" });
      expect(result.status, file).toBe(3);
      expect(result.stdout).toBe("");
      expect(result.stderr).toContain("classification=unreadable");
    }
  });

  it("rejects unknown command-line usage", () => {
    const result = spawnSync("bash", [FILTER_SCRIPT, "--bogus"], { encoding: "utf8" });
    expect(result.status).toBe(64);
  });
});

describe("offsite no-cloud purge", () => {
  const PURGE_SCRIPT = join(REPO_ROOT, "scripts/offsite-purge-no-cloud.sh");
  const MARKED = "mgc/customers/secret-client/";
  const CLASSIFICATION = `paths:\n  ${MARKED}: {level: client-restricted, no-cloud: true}\n  mgc/internal/: internal\n`;

  // A fake rclone whose remote is a directory of per-target object counts:
  // "current", "a0000AA" and the legacy "archive/2026-01-01T000000Z".
  function createPurgeHarness(counts: Record<string, number>, classification: string | null = CLASSIFICATION) {
    const root = tempDir();
    const source = join(root, "mimir");
    const remote = join(root, "remote");
    const bin = join(root, "bin");
    const calls = join(root, "rclone.calls");
    const state = join(root, "state");
    mkdirSync(source);
    mkdirSync(remote);
    mkdirSync(bin);
    if (classification !== null) writeFileSync(join(source, "classification.yaml"), classification);
    const setCounts = (next: Record<string, number>) => {
      for (const [target, count] of Object.entries(next)) {
        writeFileSync(join(remote, target.replace(/\//g, "__")), `${count}\n`);
      }
    };
    setCounts(counts);
    executable(
      join(bin, "rclone"),
      `printf '%s\\n' "$*" >> "$RCLONE_CALLS"
[ "\${1:-}" = lsf ] || printf '%s\\n' 'rclone-output-canary'
printf '%s\\n' 'rclone-stderr-canary' >&2
target_of() { local t="\${1#mimir-crypt:}"; printf '%s' "\${t//\\//__}"; }
case "\${1:-}" in
  config)
    if [ "$3" = storage ]; then
      printf 'type = onedrive\\ndrive_id = %s\\ntoken = %s\\n' "\${MOCK_DRIVE:-drive-1}" "\${MOCK_TOKEN:-token-1}"
    else
      printf 'type = crypt\\nremote = storage:encrypted/mimir\\nfilename_encryption = standard\\n%b' "\${MOCK_CONF_EXTRA:-}"
    fi
    ;;
  lsf)
    if [ "$2" = --dirs-only ]; then
      case "$3" in
        mimir-crypt:) for f in "$MOCK_REMOTE"/*; do n="\${f##*/}"; case "$n" in archive__*) ;; *) printf '%s/\\n' "$n" ;; esac; done
          for f in "$MOCK_REMOTE"/archive__*; do [ ! -e "$f" ] || { printf 'archive/\\n'; break; }; done; printf 'other/\\n' ;;
        mimir-crypt:archive) for f in "$MOCK_REMOTE"/archive__*; do printf '%s/\\n' "\${f##*/archive__}"; done ;;
      esac
      exit "\${MOCK_LSF_RC:-0}"
    fi
    exit "\${MOCK_LSF_RC:-0}"
    ;;
  lsjson)
    t=$(target_of "$4"); n=$(cat "$MOCK_REMOTE/$t" 2>/dev/null || printf 0)
    printf '[\\n'
    for ((i = 0; i < n; i++)); do
      sep=,; [ "$i" -lt $((n - 1)) ] || sep=
      printf '{"Path":"mgc/customers/secret-client/file-%s%s","Size":1,"ModTime":"2026-10-20T03:30:00%s+00:00","IsDir":false}%s\\n' \\
        "$i" "\${MOCK_OBJECT_TAG:-}" "\${MOCK_MTIME_FRACTION:-.100}" "$sep"
    done
    printf ']\\n'
    exit "\${MOCK_LSF_RC:-0}"
    ;;
  delete)
    t=$(target_of "$2")
    [ "\${MOCK_DELETE_RC:-0}" = 0 ] || exit "$MOCK_DELETE_RC"
    printf '%s\\n' "\${MOCK_REMAINING:-0}" > "$MOCK_REMOTE/$t"
    ;;
  rmdirs) exit "\${MOCK_RMDIRS_RC:-0}" ;;
  *) exit 0 ;;
esac
`,
    );
    const run = (args: string[] = [], overrides: Record<string, string> = {}) => {
      const result = spawnSync("bash", [PURGE_SCRIPT, ...args], {
        cwd: REPO_ROOT,
        encoding: "utf8",
        env: {
          ...process.env,
          HOME: root,
          MIMIR_OFFSITE_ROOT: source,
          MIMIR_OFFSITE_STATE_DIR: state,
          RCLONE_BIN: join(bin, "rclone"),
          RCLONE_CALLS: calls,
          MOCK_REMOTE: remote,
          ...overrides,
        },
      });
      return { result, calls: existsSync(calls) ? readFileSync(calls, "utf8") : "" };
    };
    const purgeDir = join(state, "offsite-private", "no-cloud-purge");
    const files = () => (existsSync(purgeDir) ? readdirSync(purgeDir).sort() : []);
    const planId = (stderr: string) => /plan=([0-9a-f]{32})/.exec(stderr)?.[1] ?? "";
    return { root, source, run, setCounts, purgeDir, files, planId };
  }

  const deletes = (calls: string) => calls.split("\n").filter((line) => /^(delete|purge|deletefile|rmdir|moveto|sync|move) /.test(line));

  it("plans with counts only and deletes nothing", () => {
    const harness = createPurgeHarness({ current: 3, a0000AA: 2, "archive/2026-01-01T000000Z": 1, a0000AB: 0 });
    const { result, calls } = harness.run();

    expect(result.status, result.stderr).toBe(0);
    expect(result.stderr).toMatch(
      /stage=plan state=pass exit=0 no_cloud_markers=1 current_objects=3 archive_objects=3 archive_dirs=2 plan=[0-9a-f]{32} expires=\S+Z/,
    );
    expect(deletes(calls)).toEqual([]);
    expect(calls).not.toMatch(/^rmdirs /m);
    const lists = calls.split("\n").filter((line) => line.startsWith("lsjson -R"));
    expect(lists.map((line) => line.split(" ")[3]).sort()).toEqual([
      "mimir-crypt:a0000AA", "mimir-crypt:a0000AB", "mimir-crypt:archive/2026-01-01T000000Z", "mimir-crypt:current",
    ]);
    for (const line of lists) {
      expect(line).toContain("--filter + /mgc/customers/secret-client --filter + /mgc/customers/secret-client/** --filter + /classification.yaml --filter + /classification.yaml/** --filter - **");
    }
    const output = `${result.stdout}\n${result.stderr}`;
    expect(output).not.toMatch(/secret-client|canary|a0000AA|archive\//);
    const id = harness.planId(result.stderr);
    expect(harness.files()).toEqual([`plan-${id}`]);
    expect(statSync(harness.purgeDir).mode & 0o777).toBe(0o700);
    expect(statSync(join(harness.purgeDir, `plan-${id}`)).mode & 0o777).toBe(0o600);
    expect(readFileSync(join(harness.purgeDir, `plan-${id}`), "utf8")).not.toContain("secret-client");
    expect(result.stdout).toContain(`--execute ${id}`);
  });

  it("executes an approved plan with capped, filtered deletes and writes a receipt", () => {
    const harness = createPurgeHarness({ current: 3, a0000AA: 2, a0000AB: 0 });
    const plan = harness.run();
    const id = harness.planId(plan.result.stderr);
    const { result, calls } = harness.run(["--execute", id]);

    expect(result.status, result.stderr).toBe(0);
    const executed = deletes(calls);
    expect(executed).toHaveLength(2);
    expect(executed).toContain(
      "delete mimir-crypt:current --filter + /mgc/customers/secret-client --filter + /mgc/customers/secret-client/** --filter + /classification.yaml --filter + /classification.yaml/** --filter - ** --max-delete 3",
    );
    expect(executed).toContain(
      "delete mimir-crypt:a0000AA --filter + /mgc/customers/secret-client --filter + /mgc/customers/secret-client/** --filter + /classification.yaml --filter + /classification.yaml/** --filter - ** --max-delete 2",
    );
    expect(calls).toMatch(/^rmdirs mimir-crypt:current --leave-root --filter \+ /m);
    expect(result.stderr).toMatch(/stage=execute state=pass exit=0 planned_objects=5 remaining_objects=0 receipt=[0-9a-f]{32}/);
    expect(harness.files()).toEqual([`receipt-${id}`]);
    const receipt = readFileSync(join(harness.purgeDir, `receipt-${id}`), "utf8");
    expect(receipt).toContain(`plan=${id}\n`);
    expect(receipt).toContain("planned_objects=5\n");
    expect(receipt).toContain("delete_failures=0\n");
    expect(receipt).toContain("remaining_objects=0\n");
    expect(receipt).toContain("outcome=pass\n");
    expect(receipt).toContain("provider_trash=owner_action_required\n");
    expect(receipt).not.toMatch(/secret-client|a0000AA|current/);
    expect(statSync(join(harness.purgeDir, `receipt-${id}`)).mode & 0o777).toBe(0o600);
    expect(`${result.stdout}\n${result.stderr}`).not.toMatch(/secret-client|canary/);

    // A plan is single-use.
    const again = harness.run(["--execute", id]);
    expect(again.result.status).toBe(1);
    expect(again.result.stderr).toContain("reason=no_plan");
  });

  it.each([
    ["the remote changed", (h: ReturnType<typeof createPurgeHarness>) => h.setCounts({ current: 4 })],
    ["a new archive dir holds marked objects", (h: ReturnType<typeof createPurgeHarness>) => h.setCounts({ a0000AC: 1 })],
    ["the markers changed", (h: ReturnType<typeof createPurgeHarness>) => writeFileSync(
      join(h.source, "classification.yaml"),
      `${CLASSIFICATION}  mgc/customers/other/: {level: client-restricted, no-cloud: true}\n`,
    )],
  ])("refuses to execute when %s since the plan", (_name, change) => {
    const harness = createPurgeHarness({ current: 3 });
    const id = harness.planId(harness.run().result.stderr);
    change(harness);
    const { result, calls } = harness.run(["--execute", id]);

    expect(result.status).toBe(1);
    expect(result.stderr).toContain("stage=execute state=fail exit=1 reason=plan_mismatch");
    expect(deletes(calls)).toEqual([]);
    expect(harness.files()).toEqual([]);
  });

  it.each([
    ["the remote now points elsewhere under the same name", { MOCK_CONF_EXTRA: "remote = other:place\\n" }],
    ["an object was replaced without changing the count", { MOCK_OBJECT_TAG: "-replaced" }],
    ["an object was replaced within the same second", { MOCK_MTIME_FRACTION: ".900" }],
    ["the underlying remote now points at another drive", { MOCK_DRIVE: "drive-2" }],
    ["an environment setting redirects the backend", { RCLONE_ONEDRIVE_DRIVE_ID: "drive-2" }],
  ])("refuses to execute when %s", (_name, overrides) => {
    const harness = createPurgeHarness({ current: 3 });
    const id = harness.planId(harness.run().result.stderr);
    const { result, calls } = harness.run(["--execute", id], overrides);

    expect(result.status).toBe(1);
    expect(result.stderr).toContain("stage=execute state=fail exit=1 reason=plan_mismatch");
    expect(deletes(calls)).toEqual([]);
  });

  it("still executes after the provider token was refreshed", () => {
    const harness = createPurgeHarness({ current: 3 });
    const id = harness.planId(harness.run().result.stderr);
    const { result } = harness.run(["--execute", id], { MOCK_TOKEN: "token-2" });
    expect(result.status, result.stderr).toBe(0);
  });

  it("purges an equivalent spelling of the in-archive classification path", () => {
    const harness = createPurgeHarness({ current: 3 });
    mkdirSync(join(harness.source, "sub"));
    const { result, calls } = harness.run([], {
      MIMIR_OFFSITE_CLASSIFICATION: `${harness.source}/./sub/../classification.yaml`,
    });
    expect(result.status, result.stderr).toBe(0);
    expect(calls).toContain("--filter + /classification.yaml --filter + /classification.yaml/** --filter - **");
  });

  it("refuses a remote that does not encrypt contents or that the environment overrides", () => {
    const plaintext = createPurgeHarness({ current: 3 });
    const noData = plaintext.run([], { MOCK_CONF_EXTRA: "no_data_encryption = true\\n" });
    expect(noData.result.status).toBe(1);
    expect(noData.result.stderr).toContain("stage=preflight state=fail exit=1 reason=remote");
    expect(plaintext.files()).toEqual([]);

    for (const name of ["RCLONE_CRYPT_REMOTE", "RCLONE_CONFIG_MIMIR_CRYPT_REMOTE", "RCLONE_FILTER_FROM", "RCLONE_IGNORE_CASE"]) {
      const harness = createPurgeHarness({ current: 3 });
      const { result, calls } = harness.run([], { [name]: "x" });
      expect(result.status, name).toBe(1);
      expect(result.stderr, name).toContain("stage=preflight state=fail exit=1 reason=rclone_env");
      expect(calls, name).toBe("");
    }
  });

  it("never prints the invoked script path", () => {
    const harness = createPurgeHarness({ current: 3 });
    const { result } = harness.run();
    expect(result.status, result.stderr).toBe(0);
    expect(`${result.stdout}\n${result.stderr}`).not.toContain(REPO_ROOT);
    expect(result.stdout).toMatch(/^ {2}offsite-purge-no-cloud\.sh --execute [0-9a-f]{32}$/m);
  });

  it("refuses an expired, malformed or unknown plan", () => {
    const harness = createPurgeHarness({ current: 3 });
    const id = harness.planId(harness.run().result.stderr);
    const planFile = join(harness.purgeDir, `plan-${id}`);
    writeFileSync(planFile, readFileSync(planFile, "utf8").replace(/^created=\d+$/m, "created=1000"));
    const expired = harness.run(["--execute", id]);
    expect(expired.result.status).toBe(1);
    expect(expired.result.stderr).toContain("reason=plan_expired");

    for (const bad of ["../../etc/passwd", "ABC", ""]) {
      const { result } = harness.run(["--execute", bad]);
      expect(result.status).not.toBe(0);
    }
    const unknown = harness.run(["--execute", "0".repeat(32)]);
    expect(unknown.result.stderr).toContain("reason=no_plan");
    expect(deletes(`${expired.calls}\n${unknown.calls}`)).toEqual([]);
  });

  it("records a failed delete in the receipt and exits non-zero", () => {
    const harness = createPurgeHarness({ current: 3 });
    const id = harness.planId(harness.run().result.stderr);
    const { result } = harness.run(["--execute", id], { MOCK_DELETE_RC: "7" });

    expect(result.status).toBe(1);
    expect(result.stderr).toMatch(/stage=execute state=fail exit=1 planned_objects=3 remaining_objects=3 receipt=/);
    const receipt = readFileSync(join(harness.purgeDir, `receipt-${id}`), "utf8");
    expect(receipt).toContain("delete_failures=1\n");
    expect(receipt).toContain("outcome=fail\n");
  });

  it("reports leftover objects after the deletes as a failure", () => {
    const harness = createPurgeHarness({ current: 3 });
    const id = harness.planId(harness.run().result.stderr);
    const { result } = harness.run(["--execute", id], { MOCK_REMAINING: "1" });
    expect(result.status).toBe(1);
    expect(readFileSync(join(harness.purgeDir, `receipt-${id}`), "utf8")).toContain("remaining_objects=1\n");
  });

  it("has nothing to plan without markers or matching objects", () => {
    const empty = createPurgeHarness({ current: 0 });
    const none = empty.run();
    expect(none.result.status, none.result.stderr).toBe(0);
    expect(none.result.stderr).toContain("stage=plan state=pass exit=0 no_cloud_markers=1 current_objects=0 archive_objects=0 archive_dirs=0");
    expect(empty.files()).toEqual([]);

    const unmarked = createPurgeHarness({ current: 3 }, "paths:\n  mgc/internal/: internal\n");
    const zero = unmarked.run();
    expect(zero.result.status).toBe(0);
    expect(zero.result.stderr).toContain("no_cloud_markers=0");
    expect(zero.calls).not.toMatch(/^lsjson -R/m);
  });

  it.each([
    ["absent", null, "reason=absent"],
    ["invalid", "paths:\n  x/: {no-cloud: true}\n", "reason=invalid line=2"],
  ])("fails closed on an %s classification file", (_name, content, detail) => {
    const harness = createPurgeHarness({ current: 3 }, content);
    const { result, calls } = harness.run();
    expect(result.status).toBe(1);
    expect(result.stderr).toContain(`stage=classification state=fail exit=1 ${detail}`);
    expect(calls).toBe("");
  });

  it("fails closed when the remote cannot be listed or is not a crypt", () => {
    const listing = createPurgeHarness({ current: 3 });
    const failed = listing.run([], { MOCK_LSF_RC: "3" });
    expect(failed.result.status).toBe(1);
    expect(listing.files()).toEqual([]);

    const plain = createPurgeHarness({ current: 3 });
    const notCrypt = plain.run([], { MIMIR_OFFSITE_REMOTE: "bad:remote" });
    expect(notCrypt.result.status).toBe(1);
    expect(notCrypt.calls).toBe("");
  });

  const hasRclone = spawnSync("rclone", ["version"], { encoding: "utf8" }).status === 0;

  it.skipIf(!hasRclone)("removes earlier copies from current/ and archive run-dirs of a real crypt remote", () => {
    const root = tempDir();
    const source = join(root, "mimir");
    const cloud = join(root, "cloud");
    const config = join(root, "rclone.conf");
    const state = join(root, "state");
    mkdirSync(join(source, MARKED, "deep"), { recursive: true });
    mkdirSync(join(source, "mgc", "internal"), { recursive: true });
    mkdirSync(cloud);
    writeFileSync(join(source, MARKED, "deep", "contract.pdf"), "v1\n");
    writeFileSync(join(source, MARKED, "nda.pdf"), "restricted\n");
    writeFileSync(join(source, "mgc", "internal", "notes.md"), "internal\n");
    const obscure = (value: string) => spawnSync("rclone", ["obscure", value], { encoding: "utf8" }).stdout.trim();
    writeFileSync(config, `[offsite-test]\ntype = crypt\nremote = ${cloud}\npassword = ${obscure("pw")}\npassword2 = ${obscure("salt")}\nfilename_encryption = standard\n`);
    chmodSync(config, 0o600);
    const env = {
      ...process.env,
      HOME: root,
      RCLONE_CONFIG: config,
      MIMIR_OFFSITE_ROOT: source,
      MIMIR_OFFSITE_REMOTE: "offsite-test",
      MIMIR_OFFSITE_STATE_DIR: state,
      HEIMDALL_HUB_URL: "",
    };
    const backup = () => spawnSync("bash", [join(REPO_ROOT, "scripts/offsite-backup.sh")], { encoding: "utf8", env });
    const purge = (args: string[] = []) => spawnSync("bash", [PURGE_SCRIPT, ...args], { encoding: "utf8", env });
    const remote = (path: string) => spawnSync("rclone", ["lsf", "-R", "--files-only", `offsite-test:${path}`], {
      encoding: "utf8", env,
    }).stdout.split("\n").filter(Boolean).sort();
    const archiveDirs = () => spawnSync("rclone", ["lsf", "--dirs-only", "offsite-test:"], { encoding: "utf8", env })
      .stdout.split("\n").filter((d) => /^a[0-9A-Za-z]{6}\/$/.test(d));

    expect(backup().status).toBe(0);
    // A second run moves the replaced contract into an archive run-dir.
    writeFileSync(join(source, MARKED, "deep", "contract.pdf"), "v2 longer\n");
    spawnSync("sleep", ["1"]);
    expect(backup().status).toBe(0);
    expect(archiveDirs()).toHaveLength(1);
    const archive = archiveDirs()[0].replace(/\/$/, "");
    expect(remote(archive)).toEqual(["mgc/customers/secret-client/deep/contract.pdf"]);

    writeFileSync(join(source, "classification.yaml"), CLASSIFICATION);
    expect(backup().status).toBe(0);
    expect(remote("current")).toContain("mgc/customers/secret-client/nda.pdf");

    const plan = purge();
    expect(plan.status, plan.stderr).toBe(0);
    expect(plan.stderr).toContain("current_objects=2 archive_objects=1 archive_dirs=1");
    expect(remote("current")).toContain("mgc/customers/secret-client/nda.pdf");
    const id = /plan=([0-9a-f]{32})/.exec(plan.stderr)?.[1] ?? "";
    const executed = purge(["--execute", id]);
    expect(executed.status, executed.stderr).toBe(0);
    expect(remote("current")).toEqual(["mgc/internal/notes.md"]);
    expect(remote(archive)).toEqual([]);
    const dirs = spawnSync("rclone", ["lsf", "-R", "--dirs-only", "offsite-test:current"], { encoding: "utf8", env }).stdout;
    expect(dirs).not.toContain("secret-client");

    // The nightly run afterwards neither re-uploads nor trips the delete gate.
    expect(backup().status).toBe(0);
    expect(remote("current")).toEqual(["mgc/internal/notes.md"]);
  }, 30_000);
});
