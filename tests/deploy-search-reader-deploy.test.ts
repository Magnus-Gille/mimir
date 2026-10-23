import { existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import {
  SECRET,
  T,
  UNITS,
  fixture,
  hasTools,
  journalDir,
  journalStatus,
  markerOf,
  mode,
  opsOf,
  pinOf,
  readIf,
  releaseDir,
  releaseOfUnits,
  sha256,
  snapshotTree,
  unitsOf,
  walkFiles,
} from "./helpers/deploy-search-reader-fixture";

describe.skipIf(!hasTools)("deploy-search-reader.sh deploy", () => {
  describe("happy path", () => {
    it("deploys, accepts and writes the marker last", () => {
      const fx = fixture();
      const originalUnits = unitsOf(fx);
      const originalPin = readFileSync(fx.pin, "utf8");
      const result = fx.deploy();
      expect(result.status, result.stdout + result.stderr).toBe(0);
      expect(result.stdout).toContain("Deploy complete.");
      expect(result.stdout).toContain("Rollback recipe");

      // Package staged immutably with the expected modes and the built hash.
      const dir = releaseDir(fx, fx.release);
      expect(mode(dir)).toBe(0o700);
      expect(mode(join(dir, "search.pyz"))).toBe(0o600);
      const built = sha256(readFileSync(join(dir, "search.pyz")));
      expect(readdirSync(dir)).toEqual(["search.pyz"]);

      // Units rewritten for the new release and the other release kept.
      expect(releaseOfUnits(fx)).toBe(fx.release);
      for (const unit of UNITS.filter((name) => name.endsWith(".service"))) expect(unitsOf(fx)[unit]).toContain(fx.release);
      expect(existsSync(releaseDir(fx, fx.prev))).toBe(true);

      // Pin: only release and package hash change.
      const pin = pinOf(fx);
      const old = JSON.parse(originalPin) as Record<string, string>;
      expect(pin.release).toBe(fx.release);
      expect(pin.package_sha256).toBe(built);
      for (const key of Object.keys(old).filter((k) => k !== "release" && k !== "package_sha256")) expect(pin[key]).toBe(old[key]);
      expect(mode(fx.pin)).toBe(0o600);

      // Marker is the SHA plus a newline, last in the mutation sequence.
      expect(readFileSync(join(fx.rhome, ".local/share/mimir-search/.deployed-commit"), "utf8")).toBe(`${fx.release}\n`);
      expect(mode(join(fx.rhome, ".local/share/mimir-search/.deployed-commit"))).toBe(0o600);
      const ops = opsOf(fx);
      expect(ops.indexOf("marker-remove")).toBeGreaterThan(ops.indexOf("stage-units-hash"));
      expect(ops.indexOf("marker-remove")).toBeLessThan(ops.indexOf("stop-units"));
      expect(ops.lastIndexOf("marker-write")).toBeGreaterThan(ops.lastIndexOf("probe-listeners"));
      const mutatingAfterMarker = ops.slice(ops.lastIndexOf("marker-write") + 1).filter((op) => !["receipt-write", "lock-release"].includes(op));
      expect(mutatingAfterMarker).toEqual([]);
      expect((readIf(join(fx.log, "client.log")) ?? "").trim().split("\n")).toHaveLength(2);

      // Switch order: timers, then the service; refresh before the service starts.
      const systemctl = (readIf(join(fx.log, "systemctl.log")) ?? "").split("\n");
      const at = (needle: string) => systemctl.findIndex((line) => line.includes(needle));
      expect(at("stop mimir-search-refresh.timer")).toBeLessThan(at("stop mimir-search-semantic-check.timer"));
      expect(at("stop mimir-search-semantic-check.timer")).toBeLessThan(at("stop mimir-search.service"));
      expect(at("daemon-reload")).toBeLessThan(at("start mimir-search-refresh.service"));
      expect(at("start mimir-search-refresh.service")).toBeLessThan(at("restart mimir-search.service"));

      // Journal holds the previous state; the audit receipt and lock cleanup happened.
      const journal = journalDir(fx);
      expect(mode(journal)).toBe(0o700);
      for (const unit of UNITS) expect(readFileSync(join(journal, "units-prev", unit), "utf8")).toBe(originalUnits[unit]);
      expect(readFileSync(join(journal, "client.json.prev"), "utf8")).toBe(originalPin);
      const env = readFileSync(join(journal, "previous.env"), "utf8");
      expect(env).toContain(`prev_release=${fx.prev}`);
      expect(env).toContain(`prev_package_sha256=${fx.prevPkgSha}`);
      expect(env).toContain(`prev_marker=${fx.prev}`);
      expect(journalStatus(fx)).toBe("deployed");
      for (const file of walkFiles(journal)) expect(mode(file)).toBe(0o600);
      expect(existsSync(join(fx.rhome, ".local/state/mimir-search-deploy.lock"))).toBe(false);
      expect(existsSync(join(fx.state, "deploy.lock"))).toBe(false);
      const receipt = JSON.parse(readFileSync(join(fx.rhome, ".local/share/mimir-search/.deployed-receipt.json"), "utf8"));
      expect(receipt.release).toBe(fx.release);
      expect(receipt.previous_release).toBe(fx.prev);
      expect(Object.keys(receipt.unit_sha256_set).sort()).toEqual([...UNITS].sort());
      expect(readdirSync(join(fx.root, "tmp"))).toEqual([]);
    }, T);

    it("never leaks the API key into output, argv, scripts, logs or the journal", () => {
      const fx = fixture();
      const result = fx.deploy();
      expect(result.status).toBe(0);
      const haystacks = [result.stdout, result.stderr];
      for (const file of walkFiles(fx.log)) haystacks.push(readFileSync(file, "utf8"));
      for (const file of walkFiles(fx.state)) haystacks.push(readFileSync(file, "utf8"));
      for (const text of haystacks) {
        expect(text).not.toContain(SECRET);
        expect(text).not.toContain("MIMIR_SEARCH_API_KEY=");
      }
      expect((readIf(join(fx.log, "ssh-scripts.log")) ?? "")).not.toContain("xtrace");
    }, T);

    it("is a no-op when re-run for the same release", () => {
      const fx = fixture();
      expect(fx.deploy().status).toBe(0);
      const before = snapshotTree(fx.rhome);
      const pinBefore = readFileSync(fx.pin, "utf8");
      writeFileSync(join(fx.log, "ops.log"), "");
      const second = fx.deploy();
      expect(second.status, second.stderr).toBe(0);
      expect(second.stdout).toContain("already deployed and verified");
      const ops = opsOf(fx);
      for (const forbidden of ["marker-remove", "stop-units", "install-units", "stage-commit", "marker-write", "activate"]) {
        expect(ops).not.toContain(forbidden);
      }
      expect(snapshotTree(fx.rhome)).toEqual(before);
      expect(readFileSync(fx.pin, "utf8")).toBe(pinBefore);
    }, T);

    it("redeploys when the marker is current but live state drifted", () => {
      const fx = fixture();
      expect(fx.deploy().status).toBe(0);
      writeFileSync(join(fx.log, "ops.log"), "");
      writeFileSync(join(fx.rhome, ".config/systemd/user/mimir-search-refresh.timer"), "[Timer]\nOnBootSec=1\n");
      const second = fx.deploy();
      expect(second.status, second.stdout + second.stderr).toBe(0);
      expect(second.stdout).not.toContain("already deployed and verified");
      expect(opsOf(fx)).toContain("install-units");
      expect(markerOf(fx)).toBe(fx.release);
      expect(unitsOf(fx)["mimir-search-refresh.timer"]).toContain("OnUnitActiveSec=60s");
    }, T);

    it("retries the semantic check once after a warn", () => {
      const fx = fixture();
      const result = fx.deploy([], { FAKE_SEMANTIC_SEQ: "warn,pass" });
      expect(result.status).toBe(0);
      expect(result.stdout).toContain("retrying once");
    }, T);
  });

});
