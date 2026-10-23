import { chmodSync, cpSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import {
  T,
  expectOriginalState,
  fixture,
  hasTools,
  journalStatus,
  markerOf,
  opsOf,
  pinOf,
  readIf,
  releaseDir,
  unitsOf,
} from "./helpers/deploy-search-reader-fixture";

describe.skipIf(!hasTools)("deploy-search-reader.sh --rollback", () => {
  describe("--rollback", () => {
    it("restores the previous release, pin and marker after a successful deploy", () => {
      const fx = fixture();
      const originalUnits = unitsOf(fx);
      const originalPin = readFileSync(fx.pin, "utf8");
      expect(fx.deploy().status).toBe(0);
      expect(markerOf(fx)).toBe(fx.release);
      const result = fx.deploy(["--rollback"]);
      expect(result.status, result.stdout + result.stderr).toBe(0);
      expect(result.stdout).toContain("Rollback accepted");
      expectOriginalState(fx, originalUnits, originalPin);
      expect(journalStatus(fx)).toBe("rolled-back");
      expect(existsSync(releaseDir(fx, fx.release))).toBe(true);
      const systemctl = readIf(join(fx.log, "systemctl.log")) ?? "";
      expect(systemctl).toContain("daemon-reload");
    }, T);

    it("refuses without a journal", () => {
      const fx = fixture();
      const result = fx.deploy(["--rollback"]);
      expect(result.status).toBe(3);
      expect(result.stderr).toContain("no journal");
      expect(opsOf(fx)).toEqual([]);
    }, T);

    it("refuses when the live release is neither the deployed nor the previous one", () => {
      const fx = fixture();
      expect(fx.deploy().status).toBe(0);
      const service = join(fx.rhome, ".config/systemd/user/mimir-search.service");
      cpSync(releaseDir(fx, fx.release), releaseDir(fx, "e".repeat(40)), { recursive: true });
      writeFileSync(service, readFileSync(service, "utf8").replaceAll(fx.release, "e".repeat(40)));
      const result = fx.deploy(["--rollback"]);
      expect(result.status).toBe(3);
      expect(result.stderr).toContain("neither");
    }, T);

    it("refuses when the previous package hash differs from the journal", () => {
      const fx = fixture();
      expect(fx.deploy().status).toBe(0);
      const file = join(releaseDir(fx, fx.prev), "search.pyz");
      chmodSync(file, 0o600);
      writeFileSync(file, "tampered");
      chmodSync(file, 0o600);
      const result = fx.deploy(["--rollback"]);
      expect(result.status).toBe(3);
      expect(result.stderr).toContain("differs from the journal");
      expect(markerOf(fx)).toBe(fx.release);
    }, T);

    it("refuses to restore a pin someone else changed after the deploy wrote it", () => {
      const fx = fixture();
      expect(fx.deploy().status).toBe(0);
      const pin = pinOf(fx);
      pin.ssh_host = "elsewhere";
      writeFileSync(fx.pin, JSON.stringify(pin));
      chmodSync(fx.pin, 0o600);
      const result = fx.deploy(["--rollback"]);
      expect(result.status).toBe(11);
      expect(result.stderr).toContain("conflict");
      expect(pinOf(fx).ssh_host).toBe("elsewhere");
      expect(markerOf(fx)).toBe("absent");
    }, T);
  });
});
