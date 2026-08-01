import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

type Probe = {
  id: string;
  prompt: string;
  control?: boolean;
  expected_doc?: string;
  expected_secondary_doc?: string;
  expected_rule?: string;
};

const REPO_ROOT = join(fileURLToPath(new URL("..", import.meta.url)));
const DOCS_ROOT = join(REPO_ROOT, "docs");

function listIndexedDocs(root: string, base = root): string[] {
  const paths: string[] = [];

  for (const entry of readdirSync(root)) {
    if (entry === "vendor") {
      continue;
    }

    const absolute = join(root, entry);
    const relativePath = relative(base, absolute).replaceAll("\\", "/");
    const stat = statSync(absolute);

    if (stat.isDirectory()) {
      paths.push(...listIndexedDocs(absolute, base));
      continue;
    }

    if (absolute.endsWith(".md") || absolute.endsWith(".json")) {
      paths.push(`docs/${relativePath}`);
    }
  }

  return paths.sort();
}

const agentsGuidance = readFileSync(join(REPO_ROOT, "AGENTS.md"), "utf8");
const probes = JSON.parse(
  readFileSync(join(REPO_ROOT, "tests/ab-instructions-probes.json"), "utf8"),
) as Probe[];

describe("agent guidance index", () => {
  it("indexes every non-vendored repo doc from AGENTS.md", () => {
    expect(agentsGuidance).toContain("## Reference docs");

    const missing = listIndexedDocs(DOCS_ROOT).filter((path) => !agentsGuidance.includes(path));
    expect(missing).toEqual([]);
  });

  it("freezes a mixed probe set that points at real docs and keeps an inline control", () => {
    expect(probes.length).toBeGreaterThanOrEqual(6);
    expect(new Set(probes.map((probe) => probe.id)).size).toBe(probes.length);
    expect(probes.some((probe) => probe.control)).toBe(true);

    for (const probe of probes) {
      expect(probe.prompt.length).toBeGreaterThan(20);

      if (probe.expected_doc) {
        expect(statSync(join(REPO_ROOT, probe.expected_doc)).isFile()).toBe(true);
      }

      if (probe.expected_secondary_doc) {
        expect(statSync(join(REPO_ROOT, probe.expected_secondary_doc)).isFile()).toBe(true);
      }
    }
  });
});
