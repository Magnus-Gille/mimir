import { cpSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { spawn, type SpawnSyncReturns } from "node:child_process";
import {
  SCRIPT,
  T,
  expectOriginalState,
  fixture,
  hasTools,
  journalDir,
  journalStatus,
  markerOf,
  opsOf,
  pinOf,
  releaseDir,
  releaseOfUnits,
  unitsOf,
  type Fx,
} from "./helpers/deploy-search-reader-fixture";

describe.skipIf(!hasTools)("deploy-search-reader.sh failure handling", () => {
  describe("failure handling and automatic rollback", () => {
    function expectRolledBack(fx: Fx, result: SpawnSyncReturns<string>, originalUnits: Record<string, string>, originalPin: string) {
      expect(result.status, result.stdout + result.stderr).toBe(10);
      expectOriginalState(fx, originalUnits, originalPin);
      expect(journalStatus(fx)).toBe("rolled-back");
      expect(existsSync(releaseDir(fx, fx.release))).toBe(true);
      expect(existsSync(releaseDir(fx, fx.prev))).toBe(true);
      expect(existsSync(join(fx.rhome, ".local/state/mimir-search-deploy.lock"))).toBe(false);
    }

    const rollbackCases: [string, Record<string, string>][] = [
      ["D2 stopping the units fails", { FAKE_FAIL_OPS: "stop-units:1" }],
      ["D3 installing the units fails after replacing them", { FAKE_INDETERMINATE_OPS: "install-units:1" }],
      ["D4 the initial refresh fails", { FAKE_FAIL_OPS: "refresh-start:1" }],
      ["D5 the service does not start", { FAKE_FAIL_OPS: "activate:1" }],
      ["E1 the receipts are stale", { FAKE_RECEIPT_SEQ: "stale,ok" }],
      ["E1 the receipts name another package", { FAKE_RECEIPT_SEQ: "wrongpkg,ok" }],
      ["E3 unauthenticated search is not denied", { FAKE_UNAUTH_SEQ: "200,401" }],
      // The first listener probe is the informational A10 baseline.
      ["E4 a wildcard listener exists", { FAKE_LISTENERS_SEQ: "ok,wildcard,ok" }],
      ["E4 an extra loopback port is listening", { FAKE_LISTENERS_SEQ: "ok,extra,ok" }],
      ["E5 the semantic check fails", { FAKE_SEMANTIC_SEQ: "fail,pass" }],
      ["E5 the semantic check warns twice", { FAKE_SEMANTIC_SEQ: "warn,warn,pass" }],
      ["F3 the client cannot read the new pin", { FAKE_CLIENT_FAIL_FIRST: "1" }],
      ["F3 the client known hit has the wrong path", { FAKE_CLIENT_WRONG_PATH_FIRST: "1" }],
      ["F3 the client no-hit query returns a result", { FAKE_CLIENT_RESULTS_ON_NOHIT_FIRST: "2" }],
    ];
    for (const [name, env] of rollbackCases) {
      it(`${name}: restores units, pin and marker and exits 10`, () => {
        const fx = fixture();
        const originalUnits = unitsOf(fx);
        const originalPin = readFileSync(fx.pin, "utf8");
        const result = fx.deploy([], env);
        expectRolledBack(fx, result, originalUnits, originalPin);
        // The previous service is accepted again, with the restored pin byte-identical.
        expect(result.stdout).toContain("Accepting the previous state");
        expect(opsOf(fx).filter((op) => op === "probe-receipts").length).toBeGreaterThanOrEqual(1);
      }, T);
    }

    it("a failure while staging (before D1) exits 3 and leaves the target untouched", () => {
      const fx = fixture();
      const originalUnits = unitsOf(fx);
      const originalPin = readFileSync(fx.pin, "utf8");
      const result = fx.deploy([], { FAKE_SCP_FAIL_MATCH: "search.pyz" });
      expect(result.status).toBe(3);
      expectOriginalState(fx, originalUnits, originalPin);
      expect(opsOf(fx)).not.toContain("marker-remove");
      expect(journalStatus(fx)).not.toBe("deployed");
    }, T);

    it("a failure staging the units exits 3 with the release package inert", () => {
      const fx = fixture();
      const originalUnits = unitsOf(fx);
      const result = fx.deploy([], { FAKE_FAIL_OPS: "stage-units-hash" });
      expect(result.status).toBe(3);
      expect(unitsOf(fx)).toEqual(originalUnits);
      expect(markerOf(fx)).toBe(fx.prev);
      expect(opsOf(fx)).not.toContain("marker-remove");
    }, T);

    it("an indeterminate marker removal that cannot be read back stops with exit 11 and no further action", () => {
      const fx = fixture();
      const originalUnits = unitsOf(fx);
      // marker-state works for the two preflight reads, then fails for every read-back.
      const result = fx.deploy([], { FAKE_INDETERMINATE_OPS: "marker-remove", FAKE_FAIL_OPS: "marker-state@2" });
      expect(result.status, result.stdout + result.stderr).toBe(11);
      expect(result.stderr).toContain("indeterminate");
      expect(result.stderr).toContain("Marker: unknown");
      expect(opsOf(fx)).not.toContain("stop-units");
      expect(opsOf(fx)).not.toContain("install-units");
      expect(unitsOf(fx)).toEqual(originalUnits);
      expect(journalStatus(fx)).toBe("marker-unknown");
    }, T);

    it("an indeterminate marker removal that is confirmed by a read-back continues normally", () => {
      const fx = fixture();
      const result = fx.deploy([], { FAKE_INDETERMINATE_OPS: "marker-remove:1" });
      expect(result.status, result.stdout + result.stderr).toBe(0);
      expect(markerOf(fx)).toBe(fx.release);
    }, T);

    it("a marker removal that fails while the marker is unchanged exits 3", () => {
      const fx = fixture();
      const originalUnits = unitsOf(fx);
      const result = fx.deploy([], { FAKE_FAIL_OPS: "marker-remove" });
      expect(result.status).toBe(3);
      expect(result.stderr).toContain("nothing was modified");
      expect(markerOf(fx)).toBe(fx.prev);
      expect(unitsOf(fx)).toEqual(originalUnits);
      expect(opsOf(fx)).not.toContain("stop-units");
    }, T);

    it("a pin edited mid-run is never clobbered and the unproven rollback exits 11", () => {
      const fx = fixture();
      const result = fx.deploy([], { FAKE_EDIT_PIN_AT_OP: "probe-401" });
      expect(result.status, result.stdout + result.stderr).toBe(11);
      expect(result.stderr).toContain("client pin changed during the run");
      expect(pinOf(fx).release).toBe("c".repeat(40));
      expect(markerOf(fx)).toBe("absent");
      expect(journalStatus(fx)).toBe("rollback-failed");
      expect(result.stderr).toContain("Journal:");
    }, T);

    it("a rollback that cannot be proved exits 11 with the marker absent", () => {
      const fx = fixture();
      const result = fx.deploy([], { FAKE_FAIL_OPS: "activate" });
      expect(result.status).toBe(11);
      expect(markerOf(fx)).toBe("absent");
      expect(journalStatus(fx)).toBe("rollback-failed");
      expect(result.stderr).toContain(`Journal: ${journalDir(fx)}`);
    }, T);

    it("--no-auto-rollback leaves the system as-is with the marker absent", () => {
      const fx = fixture();
      const result = fx.deploy(["--no-auto-rollback"], { FAKE_FAIL_OPS: "activate" });
      expect(result.status).toBe(11);
      expect(markerOf(fx)).toBe("absent");
      expect(releaseOfUnits(fx)).toBe(fx.release);
      expect(journalStatus(fx)).toBe("failed-not-rolled-back");
      expect(opsOf(fx).filter((op) => op === "install-units")).toHaveLength(1);
    }, T);

    it("--no-auto-rollback after acceptance failure keeps the new pin untouched until accepted", () => {
      const fx = fixture();
      const originalPin = readFileSync(fx.pin, "utf8");
      const result = fx.deploy(["--no-auto-rollback"], { FAKE_UNAUTH_SEQ: "200" });
      expect(result.status).toBe(11);
      expect(readFileSync(fx.pin, "utf8")).toBe(originalPin);
      expect(markerOf(fx)).toBe("absent");
    }, T);

    it("the marker is written only on full acceptance, never when any check failed", () => {
      for (const env of [{ FAKE_UNAUTH_SEQ: "200" }, { FAKE_LISTENERS_SEQ: "wildcard" }, { FAKE_SEMANTIC_SEQ: "fail" }, { FAKE_RECEIPT_SEQ: "stale" }, { FAKE_CLIENT_FAIL_FIRST: "99" }]) {
        const fx = fixture();
        const result = fx.deploy(["--no-auto-rollback"], env);
        expect(result.status).toBe(11);
        expect(markerOf(fx)).toBe("absent");
        expect(opsOf(fx)).not.toContain("marker-write");
      }
    }, T);

    it("accepted but unmarked: a failing marker write is not rolled back and a re-run finishes it", () => {
      const fx = fixture();
      const result = fx.deploy([], { FAKE_FAIL_OPS: "marker-write" });
      expect(result.status).toBe(11);
      expect(result.stderr).toContain("accepted but unmarked");
      expect(markerOf(fx)).toBe("absent");
      expect(releaseOfUnits(fx)).toBe(fx.release);
      expect(pinOf(fx).release).toBe(fx.release);
      expect(opsOf(fx).filter((op) => op === "marker-write")).toHaveLength(2);
      expect(journalStatus(fx)).toBe("accepted-unmarked");
      const rerun = fx.deploy();
      expect(rerun.status, rerun.stdout + rerun.stderr).toBe(0);
      expect(markerOf(fx)).toBe(fx.release);
      // The previous state recorded by the first run is preserved for rollback.
      expect(readFileSync(join(journalDir(fx), "previous.env"), "utf8")).toContain(`prev_release=${fx.prev}`);
    }, T);

    it("SIGTERM after the switch began triggers the automatic rollback and exits 10", async () => {
      const fx = fixture();
      const originalUnits = unitsOf(fx);
      const originalPin = readFileSync(fx.pin, "utf8");
      const child = spawn("bash", [SCRIPT, ...fx.args()], {
        cwd: fx.repo, env: { PATH: process.env.PATH ?? "", ...fx.env, FAKE_SLEEP_OP: "activate", FAKE_SLEEP_SECONDS: "4" }, stdio: "ignore",
      });
      const exited = new Promise<number | null>((done) => child.on("exit", (code) => done(code)));
      const deadline = Date.now() + 60_000;
      while (!opsOf(fx).includes("activate") && Date.now() < deadline) await new Promise((r) => setTimeout(r, 100));
      child.kill("SIGTERM");
      expect(await exited).toBe(10);
      expectOriginalState(fx, originalUnits, originalPin);
      expect(journalStatus(fx)).toBe("rolled-back");
      expect(readFileSync(join(journalDir(fx), "phase.log"), "utf8")).toContain("interrupted by SIGTERM");
    }, T);

    it("SIGTERM before the switch exits 3 without touching the target", async () => {
      const fx = fixture();
      const originalUnits = unitsOf(fx);
      const child = spawn("bash", [SCRIPT, ...fx.args()], {
        cwd: fx.repo, env: { PATH: process.env.PATH ?? "", ...fx.env, FAKE_SLEEP_OP: "stage-dir", FAKE_SLEEP_SECONDS: "3" }, stdio: "ignore",
      });
      const exited = new Promise<number | null>((done) => child.on("exit", (code) => done(code)));
      const deadline = Date.now() + 60_000;
      while (!opsOf(fx).includes("stage-dir") && Date.now() < deadline) await new Promise((r) => setTimeout(r, 100));
      child.kill("SIGTERM");
      expect(await exited).toBe(3);
      expect(unitsOf(fx)).toEqual(originalUnits);
      expect(markerOf(fx)).toBe(fx.prev);
      expect(existsSync(join(fx.rhome, ".local/state/mimir-search-deploy.lock"))).toBe(false);
    }, T);

    it("retries the marker write once", () => {
      const fx = fixture();
      const result = fx.deploy([], { FAKE_FAIL_OPS: "marker-write:1" });
      expect(result.status).toBe(0);
      expect(markerOf(fx)).toBe(fx.release);
    }, T);

    it("a rerun after a --no-auto-rollback failure completes and keeps the original rollback target", () => {
      const fx = fixture();
      expect(fx.deploy(["--no-auto-rollback"], { FAKE_FAIL_OPS: "activate:1" }).status).toBe(11);
      const rerun = fx.deploy();
      expect(rerun.status, rerun.stdout + rerun.stderr).toBe(0);
      expect(markerOf(fx)).toBe(fx.release);
      expect(readFileSync(join(journalDir(fx), "previous.env"), "utf8")).toContain(`prev_release=${fx.prev}`);
    }, T);

    it("refuses to guess when live state matches neither the release nor the journal", () => {
      const fx = fixture();
      expect(fx.deploy(["--no-auto-rollback"], { FAKE_FAIL_OPS: "activate:1" }).status).toBe(11);
      const service = join(fx.rhome, ".config/systemd/user/mimir-search.service");
      const other = "d".repeat(40);
      cpSync(releaseDir(fx, fx.release), releaseDir(fx, other), { recursive: true });
      writeFileSync(service, readFileSync(service, "utf8").replaceAll(fx.release, other));
      const rerun = fx.deploy();
      expect(rerun.status).toBe(3);
      expect(rerun.stderr).toContain("reconcile");
    }, T);
  });

});
