import { chmodSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { describe, expect, it } from "vitest";
import {
  MUTATING_OPS,
  SCRIPT,
  T,
  expectNoMutation,
  fixture,
  hasTools,
  markerOf,
  must,
  opsOf,
  readIf,
  releaseDir,
  releaseOfUnits,
  run,
  snapshotTree,
  type Fx,
} from "./helpers/deploy-search-reader-fixture";

describe.skipIf(!hasTools)("deploy-search-reader.sh preflight", () => {
  describe("usage errors", () => {
    const cases: [string, (fx: Fx) => string[]][] = [
      ["a missing --release", (fx) => fx.args().slice(2)],
      ["an abbreviated SHA", (fx) => fx.args([], fx.release.slice(0, 12))],
      ["a 64-character value", (fx) => fx.args([], "a".repeat(64))],
      ["an uppercase SHA", (fx) => fx.args([], fx.release.toUpperCase())],
      ["a ref name", (fx) => fx.args([], "main")],
      ["an unknown argument", (fx) => fx.args(["--force"])],
      ["--dry-run with --rollback", (fx) => fx.args(["--dry-run", "--rollback"])],
      ["a host that is not an alias", (fx) => [...fx.args(), "--host", "m5; touch /tmp/x"]],
      ["a relative state directory", (fx) => [...fx.args(), "--state-dir", "relative/state"]],
      ["a non-canonical pin path", (fx) => [...fx.args(), "--client-pin", "/tmp/a/../b.json"]],
    ];
    for (const [name, build] of cases) {
      it(`exits 2 and touches nothing for ${name}`, () => {
        const fx = fixture();
        const result = run("bash", [SCRIPT, ...build(fx)], fx.repo, fx.env);
        expect(result.status).toBe(2);
        expect(opsOf(fx)).toEqual([]);
        expect(existsSync(fx.state)).toBe(false);
      }, T);
    }
  });


  describe("source identity (A1)", () => {
    it("refuses a dirty tracked file", () => {
      const fx = fixture();
      writeFileSync(join(fx.repo, "README.md"), "edited\n");
      const result = fx.deploy();
      expect(result.status).toBe(3);
      expect(result.stderr).toContain("not clean");
      expect(opsOf(fx)).toEqual([]);
    }, T);

    it("refuses an untracked file", () => {
      const fx = fixture();
      writeFileSync(join(fx.repo, "stray.txt"), "x\n");
      const result = fx.deploy();
      expect(result.status).toBe(3);
      expect(result.stderr).toContain("not clean");
      expect(opsOf(fx)).toEqual([]);
    }, T);

    it("refuses when HEAD is not --release", () => {
      const fx = fixture();
      const result = run("bash", [SCRIPT, ...fx.args([], fx.prev)], fx.repo, fx.env);
      expect(result.status).toBe(3);
      expect(result.stderr).toContain("HEAD does not equal --release");
      expect(opsOf(fx)).toEqual([]);
    }, T);

    it("refuses a release that is not an ancestor of origin/main", () => {
      const fx = fixture();
      must("git", ["checkout", "-q", "-b", "unmerged"], fx.repo);
      writeFileSync(join(fx.repo, "unmerged.txt"), "x\n");
      must("git", ["add", "-A"], fx.repo);
      must("git", ["commit", "-q", "-m", "unmerged"], fx.repo);
      const unmerged = must("git", ["rev-parse", "HEAD"], fx.repo);
      const result = run("bash", [SCRIPT, ...fx.args([], unmerged)], fx.repo, fx.env);
      expect(result.status).toBe(3);
      expect(result.stderr).toContain("not reachable from origin/main");
      expect(opsOf(fx)).toEqual([]);
    }, T);

    it("fails closed when origin is unreachable", () => {
      const fx = fixture();
      must("git", ["remote", "set-url", "origin", join(fx.root, "missing.git")], fx.repo);
      const result = fx.deploy();
      expect(result.status).toBe(3);
      expect(result.stderr).toContain("unreachable");
    }, T);

    it("prints the expected and actual source lines", () => {
      const fx = fixture();
      const result = fx.deploy(["--dry-run"]);
      expect(result.stdout).toContain(`Expected source: ${fx.repo} @ ${fx.release}`);
      expect(result.stdout).toContain(`Actual source: ${fx.repo} @ ${fx.release}`);
    }, T);
  });


  describe("preflight refusals on the target", () => {
    it("refuses when the remote deploy lock is held", () => {
      const fx = fixture();
      writeFileSync(join(fx.rhome, ".local/state/mimir-search-deploy.lock"), "someone-else\n");
      const result = fx.deploy();
      expect(result.status).toBe(3);
      expect(result.stderr).toContain("lock is held");
      expect(readFileSync(join(fx.rhome, ".local/state/mimir-search-deploy.lock"), "utf8")).toBe("someone-else\n");
      expectNoMutation(fx, opsOf(fx).filter((op) => op !== "lock-acquire"));
    }, T);

    const hasPerl = spawnSync("perl", ["-e", "1"]).status === 0;
    const HOLDER = 'use Fcntl ":flock"; open(F, ">>", $ARGV[0]) or exit 2; flock(F, LOCK_EX|LOCK_NB) or exit 1; $| = 1; print "held\\n"; sleep 120;';

    it.skipIf(!hasPerl)("refuses while another process holds the local deploy lock, even with an empty lock file", async () => {
      const fx = fixture();
      mkdirSync(fx.state, { mode: 0o700 });
      const lock = join(fx.state, "deploy.lock");
      writeFileSync(lock, "");
      const holder = spawn("perl", ["-e", HOLDER, lock], { stdio: ["ignore", "pipe", "ignore"] });
      try {
        await new Promise<void>((done) => holder.stdout.once("data", () => done()));
        const result = fx.deploy();
        expect(result.status, result.stdout + result.stderr).toBe(3);
        expect(result.stderr).toContain("another deploy holds");
        expect(opsOf(fx)).toEqual([]);
        expect(existsSync(lock)).toBe(true);
      } finally {
        holder.kill("SIGKILL");
      }
    }, T);

    it.skipIf(!hasPerl)("takes over a lock file left behind by a process that is gone", () => {
      const fx = fixture();
      mkdirSync(fx.state, { mode: 0o700 });
      writeFileSync(join(fx.state, "deploy.lock"), "999999\n");
      expect(fx.deploy(["--no-auto-rollback"]).status).toBe(0);
    }, T);

    it.skipIf(!hasPerl)("lets exactly one of several concurrent deploys proceed", async () => {
      const fixtures = [fixture(), fixture(), fixture(), fixture()];
      const shared = fixtures[0].state;
      const runs = fixtures.map((fx) => {
        const args = fx.args();
        args[args.indexOf("--state-dir") + 1] = shared;
        const child = spawn("bash", [SCRIPT, ...args], {
          cwd: fx.repo, env: { PATH: process.env.PATH ?? "", ...fx.env, FAKE_SLEEP_OP: "identity", FAKE_SLEEP_SECONDS: "4" }, stdio: ["ignore", "ignore", "pipe"],
        });
        let stderr = "";
        child.stderr.on("data", (chunk) => { stderr += String(chunk); });
        return new Promise<{ code: number | null; stderr: string }>((done) => child.on("exit", (code) => done({ code, stderr })));
      });
      const results = await Promise.all(runs);
      expect(results.map((r) => r.code).sort()).toEqual([0, 3, 3, 3]);
      for (const r of results.filter((x) => x.code === 3)) expect(r.stderr).toContain("another deploy holds");
    }, T);

    it("refuses when a unit is not installed (first install is out of scope)", () => {
      const fx = fixture();
      rmSync(join(fx.rhome, ".config/systemd/user/mimir-search-semantic-check.timer"));
      const result = fx.deploy();
      expect(result.status).toBe(3);
      expect(result.stderr).toContain("only upgrades");
    }, T);

    it("refuses a client pin with the wrong mode", () => {
      const fx = fixture();
      chmodSync(fx.pin, 0o644);
      expect(fx.deploy().status).toBe(3);
    }, T);

    it("refuses an acceptance config with an extra field", () => {
      const fx = fixture();
      const config = JSON.parse(readFileSync(fx.accept, "utf8"));
      config.extra = "x";
      writeFileSync(fx.accept, JSON.stringify(config));
      chmodSync(fx.accept, 0o600);
      const result = fx.deploy();
      expect(result.status).toBe(3);
      expect(result.stderr).toContain("acceptance config is invalid");
    }, T);

    it("refuses a missing acceptance config", () => {
      const fx = fixture();
      rmSync(fx.accept);
      expect(fx.deploy().status).toBe(3);
    }, T);

    it("refuses when runtime.env is not mode 0600 without reading it", () => {
      const fx = fixture();
      chmodSync(join(fx.rhome, ".config/mimir-search/runtime.env"), 0o644);
      const result = fx.deploy();
      expect(result.status).toBe(3);
      expect(result.stderr).toContain("runtime.env");
    }, T);

    it("refuses when lingering is disabled", () => {
      const fx = fixture();
      const result = fx.deploy([], { FAKE_LINGER: "no" });
      expect(result.status).toBe(3);
      expect(result.stderr).toContain("lingering");
    }, T);

    it("retries a transport error on a read-only operation once", () => {
      const fx = fixture();
      const result = fx.deploy(["--dry-run"], { FAKE_TRANSPORT_FAIL_OPS: "identity:1" });
      expect(result.status).toBe(0);
      expect(opsOf(fx).filter((op) => op === "identity")).toHaveLength(2);
    }, T);

    it("refuses non-reproducible builds before touching the target", () => {
      const fx = fixture();
      const result = fx.deploy([], { FAKE_NONREPRO: "1" });
      expect(result.status).toBe(3);
      expect(result.stderr).toContain("not byte-identical");
      expectNoMutation(fx, opsOf(fx).filter((op) => op !== "lock-acquire" && op !== "lock-release"));
      expect(existsSync(releaseDir(fx, fx.release))).toBe(false);
    }, T);

    it("refuses a renderer file-set mismatch (extra file)", () => {
      const fx = fixture();
      const result = fx.deploy([], { FAKE_EXTRA_UNIT: "1" });
      expect(result.status).toBe(3);
      expect(result.stderr).toContain("unexpected file set");
      expect(existsSync(releaseDir(fx, fx.release))).toBe(false);
    }, T);

    it("refuses a renderer file-set mismatch (missing file)", () => {
      const fx = fixture();
      const result = fx.deploy([], { FAKE_MISSING_UNIT: "1" });
      expect(result.status).toBe(3);
      expect(result.stderr).toContain("unexpected file set");
    }, T);

    it("treats an existing release directory with different content as immutable", () => {
      const fx = fixture();
      const dir = releaseDir(fx, fx.release);
      mkdirSync(dir, { mode: 0o700 });
      chmodSync(dir, 0o700);
      writeFileSync(join(dir, "search.pyz"), "different bytes", { mode: 0o600 });
      chmodSync(join(dir, "search.pyz"), 0o600);
      const result = fx.deploy();
      expect(result.status).toBe(3);
      expect(result.stderr).toContain("immutable");
      expect(readFileSync(join(dir, "search.pyz"), "utf8")).toBe("different bytes");
      expect(markerOf(fx)).toBe(fx.prev);
      expect(releaseOfUnits(fx)).toBe(fx.prev);
    }, T);

    it("refuses a release directory with unexpected entries", () => {
      const fx = fixture();
      const dir = releaseDir(fx, fx.release);
      mkdirSync(dir, { mode: 0o700 });
      chmodSync(dir, 0o700);
      writeFileSync(join(dir, "surprise.txt"), "x");
      expect(fx.deploy().status).toBe(3);
    }, T);

    it("refuses when the service is not an upgrade target (unparsable release)", () => {
      const fx = fixture();
      const path = join(fx.rhome, ".config/systemd/user/mimir-search.service");
      writeFileSync(path, readFileSync(path, "utf8").replace(/^Environment=MIMIR_SEARCH_RELEASE=.*$/m, "Environment=OTHER=1"));
      expect(fx.deploy().status).toBe(3);
    }, T);
  });


  describe("dry run", () => {
    it("prints the plan, performs only read-only operations and mutates nothing", () => {
      const fx = fixture();
      const remoteBefore = snapshotTree(fx.rhome);
      const pinBefore = readFileSync(fx.pin, "utf8");
      const result = fx.deploy(["--dry-run"]);
      expect(result.status).toBe(0);
      expect(result.stdout).toContain("== Plan ==");
      expect(result.stdout).toContain(`Release:            ${fx.release}`);
      expect(result.stdout).toContain("Rollback recipe");
      expect(result.stdout).toContain("== Unit diff (installed -> new) ==");
      expect(result.stdout).toContain(`+Environment=MIMIR_SEARCH_RELEASE=${fx.release}`);
      expect(result.stdout).toContain("Dry run complete");
      const ops = opsOf(fx);
      expect(ops.length).toBeGreaterThan(0);
      for (const op of ops) expect(MUTATING_OPS).not.toContain(op);
      // Only downloads from the target: every scp destination is local.
      for (const line of (readIf(join(fx.log, "scp.log")) ?? "").split("\n").filter(Boolean)) {
        expect(line.trim().split(" ").pop()).not.toMatch(/^m5:/);
      }
      expect(snapshotTree(fx.rhome)).toEqual(remoteBefore);
      expect(readFileSync(fx.pin, "utf8")).toBe(pinBefore);
      expect(existsSync(fx.state)).toBe(false);
      expect(readdirSync(join(fx.root, "tmp"))).toEqual([]);
      expect(markerOf(fx)).toBe(fx.prev);
    }, T);

    it("leaves the local .git untouched: no FETCH_HEAD, refs or objects are written", () => {
      const fx = fixture();
      const before = snapshotTree(join(fx.repo, ".git"));
      const result = fx.deploy(["--dry-run"]);
      expect(result.status, result.stdout + result.stderr).toBe(0);
      expect(existsSync(join(fx.repo, ".git/FETCH_HEAD"))).toBe(false);
      expect(snapshotTree(join(fx.repo, ".git"))).toEqual(before);
    }, T);

    it("also refuses a dirty tree", () => {
      const fx = fixture();
      writeFileSync(join(fx.repo, "stray.txt"), "x\n");
      expect(fx.deploy(["--dry-run"]).status).toBe(3);
    }, T);

    it("on a deployed marker only reads: the semantic result is read, not started", () => {
      const fx = fixture();
      expect(fx.deploy().status).toBe(0);
      writeFileSync(join(fx.log, "ops.log"), "");
      const before = snapshotTree(fx.rhome);
      const result = fx.deploy(["--dry-run"]);
      expect(result.status).toBe(0);
      expect(result.stdout).toContain("already deployed and verified");
      for (const op of opsOf(fx)) expect(MUTATING_OPS).not.toContain(op);
      expect(snapshotTree(fx.rhome)).toEqual(before);
    }, T);
  });

});
