import {
  chmodSync, cpSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync,
  rmSync, statSync, writeFileSync, lstatSync,
} from "node:fs";
import { createHash } from "node:crypto";
import { tmpdir, userInfo } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync, type SpawnSyncReturns } from "node:child_process";
import { afterAll, afterEach, beforeAll, expect } from "vitest";

export const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
export const FIX = join(REPO_ROOT, "tests/fixtures/deploy-search-reader");
export const SCRIPT = join(REPO_ROOT, "scripts/deploy-search-reader.sh");
export const T = 180_000;

export const SOURCE_ID = "8f0c3a52-7d31-4c1e-9b7a-2f6d5e4c3b21";
export const SNAPSHOT_ID = "b".repeat(64);
export const EXPECTED_SHA = "a".repeat(64);
export const EXPECTED_PATH = "docs/known.pdf";
export const HIT_QUERY = "known hit query";
export const NOHIT_QUERY = "nothing answers this";
export const SECRET = "SECRETKEYVALUE-0123456789abcdef";
export const UNITS = [
  "mimir-search.service",
  "mimir-search-refresh.service",
  "mimir-search-refresh.timer",
  "mimir-search-semantic-check.service",
  "mimir-search-semantic-check.timer",
];
export const MUTATING_OPS = [
  "lock-acquire", "lock-release", "stage-dir", "stage-commit", "stage-units-dir", "stage-units-hash",
  "marker-remove", "stop-units", "install-units", "refresh-start", "activate", "semantic-run",
  "marker-write", "receipt-write",
];
export const GIT_ENV = {
  GIT_AUTHOR_NAME: "t", GIT_AUTHOR_EMAIL: "t@example.invalid",
  GIT_COMMITTER_NAME: "t", GIT_COMMITTER_EMAIL: "t@example.invalid",
  GIT_CONFIG_GLOBAL: "/dev/null", GIT_CONFIG_NOSYSTEM: "1",
};

export const hasTools = ["python3", "git", "bash"].every((tool) => spawnSync(tool, ["--version"]).status === 0);

const tempDirs: string[] = [];
export function tempDir(prefix = "mimir-deploy-test-"): string {
  const dir = realpathSync(mkdtempSync(join(realpathSync(tmpdir()), prefix)));
  tempDirs.push(dir);
  return dir;
}
afterEach(() => {
  for (const dir of tempDirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

export function run(command: string, args: string[], cwd: string, env: Record<string, string> = {}): SpawnSyncReturns<string> {
  return spawnSync(command, args, { cwd, encoding: "utf8", env: { PATH: process.env.PATH ?? "", ...GIT_ENV, ...env } });
}
export function must(command: string, args: string[], cwd: string, env: Record<string, string> = {}): string {
  const result = run(command, args, cwd, env);
  if (result.status !== 0) throw new Error(`${command} ${args.join(" ")} failed: ${result.stderr}`);
  return result.stdout.trim();
}
export function sha256(data: Buffer | string): string {
  return createHash("sha256").update(data).digest("hex");
}

// A template origin with two commits (previous release, then the release under test).
let templateRoot = "";
let originDir = "";
export let PREV = "";
export let RELEASE = "";
let prevPackage = Buffer.alloc(0);

beforeAll(() => {
  if (!hasTools) return;
  templateRoot = realpathSync(mkdtempSync(join(realpathSync(tmpdir()), "mimir-deploy-template-")));
  originDir = join(templateRoot, "origin.git");
  const work = join(templateRoot, "work");
  must("git", ["init", "--bare", "-q", "-b", "main", originDir], templateRoot);
  must("git", ["init", "-q", "-b", "main", work], templateRoot);
  cpSync(join(REPO_ROOT, "tools"), join(work, "tools"), {
    recursive: true,
    filter: (source) => !source.includes("__pycache__"),
  });
  writeFileSync(join(work, "README.md"), "previous release\n");
  must("git", ["add", "-A"], work);
  must("git", ["commit", "-q", "-m", "previous release"], work);
  PREV = must("git", ["rev-parse", "HEAD"], work);
  writeFileSync(join(work, "notes.txt"), "release under test\n");
  must("git", ["add", "-A"], work);
  must("git", ["commit", "-q", "-m", "release under test"], work);
  RELEASE = must("git", ["rev-parse", "HEAD"], work);
  must("git", ["remote", "add", "origin", originDir], work);
  must("git", ["push", "-q", "origin", "main"], work);
  const prevPath = join(templateRoot, "prev.pyz");
  must("python3", ["-m", "tools.artifact_search.package_runtime", "build", "--release", PREV, "--repo", work, "--package", prevPath], work, { PYTHONDONTWRITEBYTECODE: "1" });
  prevPackage = readFileSync(prevPath);
});

afterAll(() => {
  if (templateRoot) rmSync(templateRoot, { recursive: true, force: true });
});

export interface Fx {
  root: string; repo: string; rhome: string; lhome: string; state: string; log: string; fstate: string;
  pin: string; accept: string; release: string; prev: string; prevPkgSha: string;
  env: Record<string, string>;
  args: (extra?: string[], release?: string) => string[];
  deploy: (extra?: string[], env?: Record<string, string>) => SpawnSyncReturns<string>;
}

export function fixture(): Fx {
  const root = tempDir();
  const repo = join(root, "repo");
  const rhome = join(root, "rhome");
  const lhome = join(root, "lhome");
  const state = join(root, "state");
  const log = join(root, "log");
  const fstate = join(root, "fstate");
  const tmp = join(root, "tmp");
  for (const dir of [log, fstate, tmp, lhome]) mkdirSync(dir);
  must("git", ["clone", "-q", originDir, repo], root);

  // Remote home: the installed previous release.
  const cfg = join(rhome, ".config/mimir-search");
  mkdirSync(cfg, { recursive: true });
  writeFileSync(join(cfg, "runtime.env"), `MIMIR_SEARCH_API_KEY=${SECRET}\n`, { mode: 0o600 });
  writeFileSync(join(cfg, "reader.json"), "{}\n", { mode: 0o600 });
  writeFileSync(join(cfg, "handoff.json"), "{}\n");
  for (const file of ["runtime.env", "reader.json"]) chmodSync(join(cfg, file), 0o600);
  const prevDir = join(rhome, ".local/share/mimir-search/releases", PREV);
  mkdirSync(prevDir, { recursive: true });
  chmodSync(prevDir, 0o700);
  writeFileSync(join(prevDir, "search.pyz"), prevPackage, { mode: 0o600 });
  chmodSync(join(prevDir, "search.pyz"), 0o600);
  writeFileSync(join(rhome, ".local/share/mimir-search/.deployed-commit"), `${PREV}\n`, { mode: 0o600 });
  mkdirSync(join(rhome, ".local/state"), { recursive: true });
  must("python3", ["-m", "tools.artifact_search.service_units", "--role", "reader", "--home", rhome, "--release", PREV,
    "--output-directory", join(rhome, ".config/systemd/user")], repo, { PYTHONDONTWRITEBYTECODE: "1" });
  for (const unit of ["mimir-search.service", "mimir-search-refresh.timer", "mimir-search-semantic-check.timer"]) {
    writeFileSync(join(fstate, `active.${unit}`), "");
  }

  // Local home: client pin and the approved acceptance pins.
  const lcfg = join(lhome, ".config/mimir-search");
  mkdirSync(lcfg, { recursive: true });
  const pin = join(lcfg, "client.json");
  const prevPkgSha = sha256(prevPackage);
  writeFileSync(pin, JSON.stringify({
    schema: "mimir-search-client-v1", ssh_host: "m5", source_id: SOURCE_ID,
    mimir_origin: "https://files.example.invalid", original_prefix: "", release: PREV, package_sha256: prevPkgSha,
  }, null, 2) + "\n", { mode: 0o600 });
  chmodSync(pin, 0o600);
  const accept = join(lcfg, "deploy-acceptance.json");
  writeFileSync(accept, JSON.stringify({
    schema: "mimir-search-deploy-acceptance-v1", source_id: SOURCE_ID, snapshot_id: SNAPSHOT_ID,
    known_hit: { query: HIT_QUERY, expected_path: EXPECTED_PATH, expected_sha256: EXPECTED_SHA },
    no_hit: { query: NOHIT_QUERY },
  }), { mode: 0o600 });
  chmodSync(accept, 0o600);

  const env: Record<string, string> = {
    HOME: lhome, TMPDIR: tmp,
    MIMIR_DEPLOY_SSH: join(FIX, "fake-ssh"), MIMIR_DEPLOY_SCP: join(FIX, "fake-scp"),
    MIMIR_DEPLOY_PYTHON: join(FIX, "fake-python"), MIMIR_DEPLOY_CLIENT: join(FIX, "fake-client"),
    MIMIR_DEPLOY_REMOTE_PYTHON: join(FIX, "fake-rpython"),
    MIMIR_DEPLOY_REMOTE_USER: userInfo().username, MIMIR_DEPLOY_EXPECTED_HOME: rhome,
    MIMIR_DEPLOY_WAIT_SECONDS: "2", MIMIR_DEPLOY_WARN_RETRY_SECONDS: "0",
    FAKE_FIXTURES: FIX, FAKE_REPO: repo, FAKE_LOG: log, FAKE_STATE: fstate, FAKE_RHOME: rhome, FAKE_PIN_FILE: pin,
    FAKE_SOURCE_ID: SOURCE_ID, FAKE_SNAPSHOT_ID: SNAPSHOT_ID, FAKE_EXPECTED_SHA: EXPECTED_SHA,
    FAKE_EXPECTED_PATH: EXPECTED_PATH, FAKE_HIT_QUERY: HIT_QUERY,
  };
  const release = must("git", ["rev-parse", "HEAD"], repo);
  const args = (extra: string[] = [], rel: string = release) => [
    "--release", rel, "--host", "m5", "--client-pin", pin, "--acceptance-config", accept, "--state-dir", state, ...extra,
  ];
  const deploy = (extra: string[] = [], more: Record<string, string> = {}) =>
    run("bash", [SCRIPT, ...args(extra)], repo, { ...env, ...more });
  return { root, repo, rhome, lhome, state, log, fstate, pin, accept, release, prev: PREV, prevPkgSha, env, args, deploy };
}

// ---- observers --------------------------------------------------------------

export function readIf(path: string): string | null {
  return existsSync(path) ? readFileSync(path, "utf8") : null;
}
export const markerOf = (fx: Fx) => (readIf(join(fx.rhome, ".local/share/mimir-search/.deployed-commit")) ?? "absent").trim();
export const opsOf = (fx: Fx) => (readIf(join(fx.log, "ops.log")) ?? "").split("\n").filter(Boolean);
export const unitsOf = (fx: Fx) => Object.fromEntries(UNITS.map((name) => [name, readFileSync(join(fx.rhome, ".config/systemd/user", name), "utf8")]));
export const releaseOfUnits = (fx: Fx) => /^Environment=MIMIR_SEARCH_RELEASE=(.*)$/m.exec(unitsOf(fx)["mimir-search.service"])?.[1];
export const pinOf = (fx: Fx) => JSON.parse(readFileSync(fx.pin, "utf8")) as Record<string, string>;
export const releaseDir = (fx: Fx, release: string) => join(fx.rhome, ".local/share/mimir-search/releases", release);
export const journalDir = (fx: Fx) => join(fx.state, fx.release);
export const journalStatus = (fx: Fx) => /^status=(.*)$/m.exec(readIf(join(journalDir(fx), "previous.env")) ?? "")?.[1];
export const mode = (path: string) => statSync(path).mode & 0o777;

export function snapshotTree(dir: string, base = dir, out: Record<string, string> = {}): Record<string, string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    const rel = full.slice(base.length);
    if (entry.isDirectory()) {
      out[rel] = `dir ${mode(full).toString(8)}`;
      snapshotTree(full, base, out);
    } else if (lstatSync(full).isSymbolicLink()) {
      out[rel] = "symlink";
    } else {
      out[rel] = `file ${mode(full).toString(8)} ${sha256(readFileSync(full))}`;
    }
  }
  return out;
}

export function walkFiles(dir: string): string[] {
  const files: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) files.push(...walkFiles(full));
    else files.push(full);
  }
  return files;
}

export function expectNoMutation(fx: Fx, ops: string[]) {
  for (const op of ops) expect(MUTATING_OPS).not.toContain(op);
  expect(markerOf(fx)).toBe(fx.prev);
  expect(releaseOfUnits(fx)).toBe(fx.prev);
}

export function expectOriginalState(fx: Fx, originalUnits: Record<string, string>, originalPin: string) {
  expect(unitsOf(fx)).toEqual(originalUnits);
  expect(readFileSync(fx.pin, "utf8")).toBe(originalPin);
  expect(markerOf(fx)).toBe(fx.prev);
}

