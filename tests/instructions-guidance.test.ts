import { spawnSync } from "node:child_process";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

type Probe = {
  id: string;
  kind: "retrieval" | "control";
  prompt: string;
  target: string;
  assert_regex?: string;
  expected_secondary_doc?: string;
  expected_related_docs?: string[];
};

type ProbeFixture = {
  probes: Probe[];
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

    if (relativePath === "index.md") {
      continue;
    }

    if (absolute.endsWith(".md") || absolute.endsWith(".json")) {
      paths.push(`docs/${relativePath}`);
    }
  }

  return paths.sort();
}

const agentsGuidance = readFileSync(join(REPO_ROOT, "AGENTS.md"), "utf8");
const docsIndex = readFileSync(join(DOCS_ROOT, "index.md"), "utf8");
const probeFixture = JSON.parse(
  readFileSync(join(REPO_ROOT, "tests/ab-instructions-probes.json"), "utf8"),
) as ProbeFixture;
const probes = probeFixture.probes;
const singleLineAgentsGuidance = agentsGuidance.replace(/\s+/g, " ").trim();
const historicalControlExamples = new Map([
  [
    "secret-scan-quarantine-control",
    "The staged file is quarantined outside the servable tree and mirroring stays blocked.",
  ],
]);

function assertPosixEreMatch(pattern: string, input: string, label: string): void {
  const result = spawnSync("grep", ["-E", pattern], {
    encoding: "utf8",
    input: `${input}\n`,
  });

  if (result.error) {
    throw result.error;
  }

  if (result.status !== 0) {
    throw new Error(
      `grep -E failed for ${label} with status ${result.status}: ${result.stderr.trim() || "no stderr"}`,
    );
  }
}

describe("agent guidance index", () => {
  it("keeps AGENTS lean and indexes every non-vendored repo doc from docs/index.md", () => {
    expect(agentsGuidance).toContain("## Reference docs");
    expect(agentsGuidance).toContain("docs/index.md");
    expect(agentsGuidance).toContain("docs/relocation.md");
    expect(agentsGuidance).not.toContain("docs/workload-requirement-v1.json");
    expect(agentsGuidance).not.toContain("docs/workload-requirement-v1.provenance.json");

    const missing = listIndexedDocs(DOCS_ROOT).filter((path) => !docsIndex.includes(path));
    expect(missing).toEqual([]);
  });

  it("freezes a harness-compatible mixed probe set with doc coverage and an inline control", () => {
    expect(Array.isArray(probeFixture.probes)).toBe(true);
    expect(probes.length).toBeGreaterThanOrEqual(6);
    expect(new Set(probes.map((probe) => probe.id)).size).toBe(probes.length);

    const indexedDocs = listIndexedDocs(DOCS_ROOT);
    const coveredDocs = new Set<string>();
    const controls = probes.filter((probe) => probe.kind === "control");

    expect(controls).toHaveLength(1);

    for (const probe of probes) {
      expect(probe.prompt.length).toBeGreaterThan(20);
      expect(statSync(join(REPO_ROOT, probe.target)).isFile()).toBe(true);

      if (probe.kind === "retrieval") {
        expect(indexedDocs).toContain(probe.target);
        coveredDocs.add(probe.target);
      } else {
        expect(probe.target).toBe("AGENTS.md");
        expect(probe.assert_regex).toBeTypeOf("string");
        const regex = new RegExp(probe.assert_regex!);
        expect(regex.test(singleLineAgentsGuidance)).toBe(true);
        assertPosixEreMatch(probe.assert_regex!, singleLineAgentsGuidance, `${probe.id} AGENTS guidance`);

        const historicalExample = historicalControlExamples.get(probe.id);
        if (historicalExample) {
          expect(regex.test(historicalExample)).toBe(true);
          assertPosixEreMatch(probe.assert_regex!, historicalExample, `${probe.id} historical example`);
        }
      }

      if (probe.expected_secondary_doc) {
        expect(statSync(join(REPO_ROOT, probe.expected_secondary_doc)).isFile()).toBe(true);
        expect(indexedDocs).toContain(probe.expected_secondary_doc);
        coveredDocs.add(probe.expected_secondary_doc);
      }

      for (const relatedDoc of probe.expected_related_docs ?? []) {
        expect(statSync(join(REPO_ROOT, relatedDoc)).isFile()).toBe(true);
        expect(indexedDocs).toContain(relatedDoc);
        coveredDocs.add(relatedDoc);
      }
    }

    expect([...coveredDocs].sort()).toEqual(indexedDocs);
  });
});
