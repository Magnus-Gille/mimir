import { describe, expect, it, vi } from "vitest";
import { spawn } from "node:child_process";
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";
import { tmpdir } from "node:os";
import type { Server } from "node:http";

const SOURCE_ID = "123e4567-e89b-12d3-a456-426614174000";
const FIXTURE_KEY = "snapshot-origin-fixture-key";

type PythonResult = {
  code: number | null;
  stdout: string;
  stderr: string;
};

type SnapshotArtifact = {
  object: string;
};

type SnapshotManifest = {
  artifacts: SnapshotArtifact[];
};

type CreateApp = (typeof import("../src/index.js"))["createApp"];
type App = ReturnType<CreateApp>;

async function loadCreateApp(): Promise<CreateApp> {
  vi.resetModules();
  const module = await import("../src/index.js");
  return module.createApp;
}

function repositoryRoot(): string {
  return resolve(dirname(fileURLToPath(import.meta.url)), "..");
}

function pythonEnvironment(pythonPath: string, variables: Record<string, string> = {}): NodeJS.ProcessEnv {
  return {
    PATH: process.env.PATH ?? "/usr/bin:/bin",
    LANG: process.env.LANG ?? "C",
    TMPDIR: process.env.TMPDIR ?? tmpdir(),
    PYTHONPATH: pythonPath,
    ...variables,
  };
}

function runPython(
  args: string[],
  pythonPath: string,
  variables: Record<string, string> = {},
): Promise<PythonResult> {
  return new Promise((resolveResult, reject) => {
    const child = spawn("python3", args, {
      cwd: repositoryRoot(),
      env: pythonEnvironment(pythonPath, variables),
      stdio: ["ignore", "pipe", "pipe"],
    });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk));
    child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk));
    child.once("error", reject);
    child.once("close", (code) => {
      resolveResult({
        code,
        stdout: Buffer.concat(stdout).toString("utf8"),
        stderr: Buffer.concat(stderr).toString("utf8"),
      });
    });
  });
}

function listen(app: App): Promise<Server> {
  return new Promise((resolveServer, reject) => {
    const server = app.listen(0, "127.0.0.1");
    server.once("error", reject);
    server.once("listening", () => resolveServer(server));
  });
}

function close(server: Server): Promise<void> {
  return new Promise((resolveClose, reject) => {
    server.close((error) => (error ? reject(error) : resolveClose()));
  });
}

function port(server: Server): number {
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Snapshot origin did not expose a TCP port");
  return address.port;
}

describe("snapshot origin integration", () => {
  it("acquires sixty unique objects through the default sixty-per-minute limit", async () => {
    const root = await mkdtemp(join(tmpdir(), "mimir-snapshot-rate-"));
    const sourceRoot = join(root, "source");
    const snapshotStore = join(root, "snapshots");
    const acquiredStore = join(root, "acquired");
    let server: Server | undefined;
    // Advance both sides' clocks 20x: exercise the actual default 60-second
    // window without making this regression take over a minute in CI.
    const realEpoch = Date.now();
    const realStart = performance.now();
    const now = vi.spyOn(Date, "now").mockImplementation(() => realEpoch + (performance.now() - realStart) * 20);
    try {
      await mkdir(sourceRoot);
      for (let i = 0; i < 60; i++) await writeFile(join(sourceRoot, `${i}.txt`), `unique object ${i}`);
      const created = await runPython([
        "-m", "tools.artifact_search.snapshot_cli", "create", "--root", sourceRoot,
        "--store", snapshotStore, "--source-id", SOURCE_ID,
      ], repositoryRoot());
      expect(created.code, created.stderr).toBe(0);
      const snapshotId = (JSON.parse(created.stdout) as { snapshot_id: string }).snapshot_id;
      const createApp = await loadCreateApp();
      const app = createApp({ apiKey: FIXTURE_KEY, rootDir: snapshotStore });
      server = await listen(app);
      const acquired = await runPython(["-c", `
import os,time
from tools.artifact_search import snapshot_http
from tools.artifact_search.snapshot import acquire_snapshot
real_clock, real_sleep = time.monotonic, time.sleep
snapshot_http.time.monotonic = lambda: real_clock() * 20
snapshot_http.time.sleep = lambda seconds: real_sleep(seconds / 20)
source = snapshot_http.HttpSnapshotSource.from_env(os.environ['SNAPSHOT_ID'], os.environ['SOURCE_ID'])
result = acquire_snapshot(source, os.environ['STORE'])
assert result['artifacts'] == 60
`], repositoryRoot(), {
        MIMIR_SNAPSHOT_URL: `http://127.0.0.1:${port(server)}/files/${snapshotId}`,
        MIMIR_API_KEY: FIXTURE_KEY,
        SNAPSHOT_ID: snapshotId,
        SOURCE_ID,
        STORE: acquiredStore,
      });
      expect(acquired.code, acquired.stderr).toBe(0);
      expect(await readdir(join(acquiredStore, snapshotId, "objects"))).toHaveLength(60);
    } finally {
      now.mockRestore();
      if (server) await close(server);
      await rm(root, { recursive: true, force: true });
    }
  }, 15000);

  it("serves a Python-created immutable bundle to acquire-http over Bearer auth", async () => {
    const root = await mkdtemp(join(tmpdir(), "mimir-snapshot-origin-"));
    const sourceRoot = join(root, "source");
    const snapshotStore = join(root, "snapshots");
    const acquiredStore = join(root, "acquired");
    const rejectedStore = join(root, "rejected");
    const pythonPath = repositoryRoot();
    let server: Server | undefined;

    try {
      await mkdir(join(sourceRoot, "nested"), { recursive: true });
      await mkdir(snapshotStore, { recursive: true, mode: 0o700 });
      await writeFile(join(sourceRoot, "README.md"), "immutable snapshot README\n");
      await writeFile(join(sourceRoot, "nested", "record.txt"), "immutable snapshot object\n");

      const created = await runPython([
        "-m", "tools.artifact_search.snapshot_cli", "create",
        "--root", sourceRoot,
        "--store", snapshotStore,
        "--source-id", SOURCE_ID,
      ], pythonPath);
      expect(created.code, created.stderr).toBe(0);
      const creation = JSON.parse(created.stdout) as { snapshot_id: string };
      expect(creation.snapshot_id).toMatch(/^[0-9a-f]{64}$/);
      const snapshotId = creation.snapshot_id;
      const bundle = join(snapshotStore, snapshotId);

      const manifestBytes = await readFile(join(bundle, "manifest.json"));
      const manifest = JSON.parse(manifestBytes.toString("utf8")) as SnapshotManifest;
      const createApp = await loadCreateApp();
      const app = createApp({ apiKey: FIXTURE_KEY, rootDir: snapshotStore, rateLimitMax: 20 });
      server = await listen(app);
      const baseUrl = `http://127.0.0.1:${port(server)}/files/${snapshotId}`;

      const acquired = await runPython([
        "-m", "tools.artifact_search.snapshot_cli", "acquire-http",
        "--store", acquiredStore,
        "--source-id", SOURCE_ID,
        "--snapshot-id", snapshotId,
      ], pythonPath, {
        MIMIR_SNAPSHOT_URL: baseUrl,
        MIMIR_API_KEY: FIXTURE_KEY,
      });
      expect(acquired.code, acquired.stderr).toBe(0);

      const acquiredBundle = join(acquiredStore, snapshotId);
      await expect(readFile(join(acquiredBundle, "manifest.json"))).resolves.toEqual(manifestBytes);
      const expectedObjects = manifest.artifacts.map((artifact) => artifact.object.slice("objects/".length));
      expect((await readdir(join(acquiredBundle, "objects"))).sort()).toEqual([...expectedObjects].sort());
      for (const objectHash of expectedObjects) {
        await expect(readFile(join(acquiredBundle, "objects", objectHash)))
          .resolves.toEqual(await readFile(join(bundle, "objects", objectHash)));
      }

      const rejected = await runPython([
        "-m", "tools.artifact_search.snapshot_cli", "acquire-http",
        "--store", rejectedStore,
        "--source-id", SOURCE_ID,
        "--snapshot-id", snapshotId,
      ], pythonPath, {
        MIMIR_SNAPSHOT_URL: baseUrl,
        MIMIR_API_KEY: `${FIXTURE_KEY}-wrong`,
      });
      expect(rejected.code).not.toBe(0);
      expect(rejected.stderr).toContain("source_http_403");
      await expect(readdir(rejectedStore)).rejects.toMatchObject({ code: "ENOENT" });
    } finally {
      if (server) await close(server);
      await rm(root, { recursive: true, force: true });
    }
  // Includes three Python startups and real 1.1-second request pacing.
  }, 15000);
});
