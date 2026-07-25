import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const REPO_ROOT = join(fileURLToPath(new URL("..", import.meta.url)));

function unitProperty(unit: string, property: string): string | undefined {
  const line = readFileSync(join(REPO_ROOT, unit), "utf8")
    .split("\n")
    .find((candidate) => candidate.startsWith(`${property}=`));
  return line?.slice(property.length + 1);
}

describe("checked-in Mímir systemd unit", () => {
  it("matches the authoritative deployed service identity and sandbox paths", () => {
    expect(unitProperty("mimir.service", "User")).toBe("magnus");
    expect(unitProperty("mimir.service", "WorkingDirectory")).toBe("/home/magnus/mimir-server");
    expect(unitProperty("mimir.service", "EnvironmentFile")).toBe("/home/magnus/mimir-server/.env");
    expect(unitProperty("mimir.service", "ReadWritePaths")).toBe("/home/magnus/mimir-server");
    expect(unitProperty("mimir.service", "ReadOnlyPaths")).toBe("/home/magnus/mimir");
  });

  it.each(["magnus", "archive"])("renders the HTTP service paths for deployment user %s", (user) => {
    const result = renderUnit(user, "mimir.service");

    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout).toContain(`User=${user}`);
    expect(result.stdout).toContain(`WorkingDirectory=/home/${user}/mimir-server`);
    expect(result.stdout).toContain(`EnvironmentFile=/home/${user}/mimir-server/.env`);
    expect(result.stdout).toContain(`ReadWritePaths=/home/${user}/mimir-server`);
    expect(result.stdout).toContain(`ReadOnlyPaths=/home/${user}/mimir`);
  });

  it("renders every deployed unit for a custom user without stale home paths", () => {
    for (const unit of ["mimir.service", "mimir-offsite.service", "mimir-offsite.timer"]) {
      const result = renderUnit("archive", unit);
      expect(result.status, `${unit}: ${result.stderr}`).toBe(0);
      expect(result.stdout, unit).not.toContain("/home/mimir");
      expect(result.stdout, unit).not.toContain("/home/magnus");
    }

    const offsite = renderUnit("archive", "mimir-offsite.service");
    expect(offsite.stdout).toContain("User=archive");
    expect(offsite.stdout).toContain("ReadWritePaths=/home/archive/.config/rclone");
    expect(offsite.stdout).toContain("ReadOnlyPaths=/home/archive/mimir");
  });
});

function renderUnit(user: string, unit: string) {
  return spawnSync("bash", [join(REPO_ROOT, "scripts/render-systemd-unit.sh"), user, join(REPO_ROOT, unit)], {
    encoding: "utf8",
  });
}
