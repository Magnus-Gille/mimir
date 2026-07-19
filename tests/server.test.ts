import { describe, it, expect, beforeAll, afterAll } from "vitest";
import request from "supertest";
import { createApp, HEIMDALL_DESCRIPTOR } from "../src/index.js";
import { generateToken } from "../src/share-token.js";
import { mkdirSync, writeFileSync, rmSync, symlinkSync } from "node:fs";
import { join, relative } from "node:path";

const TEST_ROOT = join(import.meta.dirname, "__test_fixtures__");
const TEST_OUTSIDE_ROOT = join(import.meta.dirname, "__test_fixtures_outside__");
const TEST_API_KEY = "test-key-for-mimir";
const TEST_SHARE_SECRET = "test-share-secret-for-mimir-32bytes!";

function setup() {
  // Create test fixture directory
  mkdirSync(join(TEST_ROOT, "subdir"), { recursive: true });
  writeFileSync(join(TEST_ROOT, "hello.txt"), "Hello, Mímir!");
  writeFileSync(join(TEST_ROOT, "doc.pdf"), "%PDF-1.4 fake pdf content");
  writeFileSync(join(TEST_ROOT, "subdir", "nested.md"), "# Nested file\n\nSome content.");
  writeFileSync(join(TEST_ROOT, ".hidden"), "should not appear in listings");
  writeFileSync(join(TEST_ROOT, "data.csv"), "id,namn\n1,Åsa");
  writeFileSync(join(TEST_ROOT, 'quo"te.md'), "quoted filename");
  writeFileSync(join(TEST_ROOT, "åäö.md"), "svenska tecken");
  writeFileSync(join(TEST_ROOT, "line\nbreak.md"), "control char filename");
  mkdirSync(join(TEST_OUTSIDE_ROOT, "private-dir"), { recursive: true });
  writeFileSync(join(TEST_OUTSIDE_ROOT, "private.txt"), "must stay outside");
  writeFileSync(join(TEST_OUTSIDE_ROOT, "private-dir", "secret.txt"), "must stay outside");
  symlinkSync(join(TEST_OUTSIDE_ROOT, "private.txt"), join(TEST_ROOT, "outside-file-link"));
  symlinkSync(join(TEST_OUTSIDE_ROOT, "private-dir"), join(TEST_ROOT, "outside-dir-link"));
  symlinkSync("hello.txt", join(TEST_ROOT, "inside-file-link"));
  symlinkSync("subdir", join(TEST_ROOT, "inside-dir-link"));
}

function teardown() {
  rmSync(TEST_ROOT, { recursive: true, force: true });
  rmSync(TEST_OUTSIDE_ROOT, { recursive: true, force: true });
}

let app: ReturnType<typeof createApp>;

beforeAll(() => {
  setup();
  app = createApp({
    apiKey: TEST_API_KEY,
    rootDir: TEST_ROOT,
    shareSecret: TEST_SHARE_SECRET,
    rateLimitMax: 1000,
  });
});

afterAll(() => {
  teardown();
});

const auth = { Authorization: `Bearer ${TEST_API_KEY}` };
const hardeningClient = { ...auth, "X-Forwarded-For": "198.51.100.77" };
const hardeningPublicClient = { "X-Forwarded-For": "198.51.100.77" };

describe("health", () => {
  it("returns ok without auth", async () => {
    const res = await request(app).get("/health");
    expect(res.status).toBe(200);
    expect(res.body.status).toBe("ok");
    expect(res.body.service).toBe("mimir");
    expect(res.body).not.toHaveProperty("root_dir");
  });
});

describe("proxy trust", () => {
  it("does not trust forwarded client addresses by default", () => {
    const defaultApp = createApp({ apiKey: TEST_API_KEY, rootDir: TEST_ROOT });
    expect(defaultApp.get("trust proxy")).toBe(false);
  });

  it("accepts an explicit trusted-proxy configuration", () => {
    const proxyApp = createApp({
      apiKey: TEST_API_KEY,
      rootDir: TEST_ROOT,
      trustProxy: "loopback",
    });
    expect(proxyApp.get("trust proxy")).toBe("loopback");
  });
});

describe("heimdall.json", () => {
  it("returns 200 without auth", async () => {
    const res = await request(app).get("/heimdall.json");
    expect(res.status).toBe(200);
  });

  it("returns the descriptor object", async () => {
    const res = await request(app).get("/heimdall.json");
    expect(res.body).toMatchObject({
      _schema: "https://grimnir.example/schema/service/v1",
      service: { name: "mimir", label: "Mímir", namespace: "grimnir", instance_id: "default" },
      kind: "http-service",
      status: "pass",
    });
  });

  it("matches the exported HEIMDALL_DESCRIPTOR const", async () => {
    const res = await request(app).get("/heimdall.json");
    expect(res.body).toMatchObject(HEIMDALL_DESCRIPTOR as unknown as Record<string, unknown>);
  });

  it("deploy block uses generic public defaults", async () => {
    const res = await request(app).get("/heimdall.json");
    expect(res.body.deploy).toMatchObject({ host: "localhost", systemd_unit: "mimir", platform: "bare-metal" });
  });

  it("uses configured deployment identity instead of collapsing every instance", async () => {
    const configuredApp = createApp({
      apiKey: TEST_API_KEY,
      rootDir: TEST_ROOT,
      instanceId: "archive-primary",
      deployHost: "files-01",
      rateLimitMax: 1000,
    });
    const res = await request(configuredApp).get("/heimdall.json");

    expect(res.body.service.instance_id).toBe("archive-primary");
    expect(res.body.deploy.host).toBe("files-01");
  });

  it("rejects malformed deployment identity", () => {
    expect(() => createApp({
      apiKey: TEST_API_KEY,
      rootDir: TEST_ROOT,
      instanceId: "not valid",
    })).toThrow(/instance id/i);
  });

  it("links are root-relative or https (no protocol-relative)", async () => {
    const res = await request(app).get("/heimdall.json");
    const links: Record<string, string> = res.body.links;
    for (const [, href] of Object.entries(links)) {
      expect(href.startsWith("//")).toBe(false);
      const safe = href.startsWith("/") || /^https?:\/\//i.test(href);
      expect(safe).toBe(true);
    }
  });
});

describe("auth", () => {
  it("rejects requests without auth header", async () => {
    const res = await request(app).get("/files/hello.txt");
    expect(res.status).toBe(401);
  });

  it("rejects requests with wrong token", async () => {
    const res = await request(app)
      .get("/files/hello.txt")
      .set("Authorization", "Bearer wrong-key");
    expect(res.status).toBe(403);
  });

  it("rejects requests with non-Bearer auth", async () => {
    const res = await request(app)
      .get("/files/hello.txt")
      .set("Authorization", "Basic dXNlcjpwYXNz");
    expect(res.status).toBe(401);
  });

  it("accepts valid Bearer token", async () => {
    const res = await request(app)
      .get("/files/hello.txt")
      .set(auth);
    expect(res.status).toBe(200);
  });
});

describe("file serving: /files/*", () => {
  it("serves a text file with correct content-type", async () => {
    const res = await request(app)
      .get("/files/hello.txt")
      .set(auth);
    expect(res.status).toBe(200);
    expect(res.headers["content-type"]).toMatch(/text\/plain/);
    expect(res.text).toBe("Hello, Mímir!");
  });

  it("serves a PDF with correct content-type", async () => {
    const res = await request(app)
      .get("/files/doc.pdf")
      .set(auth);
    expect(res.status).toBe(200);
    expect(res.headers["content-type"]).toBe("application/pdf");
  });

  it("serves nested files", async () => {
    const res = await request(app)
      .get("/files/subdir/nested.md")
      .set(auth);
    expect(res.status).toBe(200);
    expect(res.text).toContain("# Nested file");
  });

  it("returns 404 for missing files", async () => {
    const res = await request(app)
      .get("/files/nonexistent.txt")
      .set(auth);
    expect(res.status).toBe(404);
  });

  it("stays healthy when a sync removes a file as it is being opened", async () => {
    const volatilePath = join(TEST_ROOT, "volatile.txt");
    writeFileSync(volatilePath, "short-lived artifact");
    const pending = request(app).get("/files/volatile.txt").set(hardeningClient);
    setImmediate(() => rmSync(volatilePath, { force: true }));

    const res = await pending;
    // The stable open either wins (200) or realpath/open observes removal
    // (404); neither path may crash the process with an unhandled stream error.
    expect([200, 404]).toContain(res.status);
    expect((await request(app).get("/health").set(hardeningPublicClient)).status).toBe(200);
  });

  it("returns 400 for empty file path", async () => {
    const res = await request(app)
      .get("/files/")
      .set(auth);
    expect(res.status).toBe(400);
  });

  it("returns error when path is a directory", async () => {
    const res = await request(app)
      .get("/files/subdir")
      .set(auth);
    expect(res.status).toBe(400);
    expect(res.body.error).toContain("directory");
  });

  it("sets security headers", async () => {
    const res = await request(app)
      .get("/files/hello.txt")
      .set(auth);
    expect(res.headers["x-content-type-options"]).toBe("nosniff");
    expect(res.headers["x-frame-options"]).toBe("DENY");
    expect(res.headers["cache-control"]).toBe("no-store");
    expect(res.headers["referrer-policy"]).toBe("no-referrer");
  });

  it("includes content-length and accept-ranges", async () => {
    const res = await request(app)
      .get("/files/hello.txt")
      .set(auth);
    expect(res.headers["content-length"]).toBeDefined();
    expect(res.headers["accept-ranges"]).toBe("bytes");
  });

  it("supports range requests", async () => {
    const res = await request(app)
      .get("/files/hello.txt")
      .set(auth)
      .set("Range", "bytes=0-4");
    expect(res.status).toBe(206);
    expect(res.text).toBe("Hello");
    expect(res.headers["content-range"]).toMatch(/bytes 0-4\/\d+/);
  });

  it("returns 416 for out-of-range requests", async () => {
    const res = await request(app)
      .get("/files/hello.txt")
      .set(auth)
      .set("Range", "bytes=9999-");
    expect(res.status).toBe(416);
  });
});

describe("path traversal protection", () => {
  it("canonicalizes a relative configured root before containment checks", async () => {
    const relativeRoot = relative(process.cwd(), TEST_ROOT);
    const relativeRootApp = createApp({
      apiKey: TEST_API_KEY,
      rootDir: relativeRoot,
      shareSecret: TEST_SHARE_SECRET,
    });

    const health = await request(relativeRootApp).get("/health");
    expect(health.status).toBe(200);
    expect(health.body).not.toHaveProperty("root_dir");

    const file = await request(relativeRootApp)
      .get("/files/hello.txt")
      .set({ ...auth, "X-Forwarded-For": "198.51.100.78" });
    expect(file.status).toBe(200);
    expect(file.text).toBe("Hello, Mímir!");
  });

  it("blocks ../ traversal in /files/", async () => {
    const res = await request(app)
      .get("/files/../../../etc/passwd")
      .set(auth);
    // Express normalizes the path, but our resolver catches it
    expect([400, 404]).toContain(res.status);
  });

  it("blocks ../ traversal in /list/", async () => {
    const res = await request(app)
      .get("/list/../../../etc/")
      .set(auth);
    expect([400, 404]).toContain(res.status);
  });

  it("blocks encoded traversal", async () => {
    const res = await request(app)
      .get("/files/..%2F..%2Fetc%2Fpasswd")
      .set(auth);
    expect([400, 404]).toContain(res.status);
  });

  it("blocks a file symlink whose real target escapes the root", async () => {
    const res = await request(app).get("/files/outside-file-link").set(hardeningClient);
    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/invalid path/i);
  });

  it("blocks an external file symlink through a signed share", async () => {
    const token = generateToken("outside-file-link", 3600, TEST_SHARE_SECRET);
    const res = await request(app).get(`/share/${token}`).set(hardeningPublicClient);
    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/invalid path/i);
  });

  it("blocks a directory symlink whose real target escapes the root", async () => {
    const res = await request(app).get("/list/outside-dir-link").set(hardeningClient);
    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/invalid path/i);
  });

  it("still serves valid symlinks whose targets remain inside the root", async () => {
    const file = await request(app).get("/files/inside-file-link").set(hardeningClient);
    expect(file.status).toBe(200);
    expect(file.text).toBe("Hello, Mímir!");

    const dir = await request(app).get("/list/inside-dir-link").set(hardeningClient);
    expect(dir.status).toBe(200);
    expect(dir.body.entries.map((e: { name: string }) => e.name)).toContain("nested.md");

    const token = generateToken("inside-file-link", 3600, TEST_SHARE_SECRET);
    const share = await request(app).get(`/share/${token}`).set(hardeningPublicClient);
    expect(share.status).toBe(200);
    expect(share.text).toBe("Hello, Mímir!");
  });

  it("keeps external symlinks out of parent directory listings", async () => {
    const res = await request(app).get("/list/").set(hardeningClient);
    const names = res.body.entries.map((e: { name: string }) => e.name);
    expect(names).not.toContain("outside-file-link");
    expect(names).not.toContain("outside-dir-link/");
    expect(names).toContain("inside-file-link");
    expect(names).toContain("inside-dir-link/");
  });
});

describe("directory listing: /list/*", () => {
  it("lists root directory", async () => {
    const res = await request(app)
      .get("/list/")
      .set(auth);
    expect(res.status).toBe(200);
    expect(res.body.path).toBe("/");
    const names = res.body.entries.map((e: { name: string }) => e.name);
    expect(names).toContain("hello.txt");
    expect(names).toContain("doc.pdf");
    expect(names).toContain("subdir/");
  });

  it("hides dotfiles", async () => {
    const res = await request(app)
      .get("/list/")
      .set(auth);
    const names = res.body.entries.map((e: { name: string }) => e.name);
    expect(names).not.toContain(".hidden");
  });

  it("lists subdirectories", async () => {
    const res = await request(app)
      .get("/list/subdir")
      .set(auth);
    expect(res.status).toBe(200);
    const names = res.body.entries.map((e: { name: string }) => e.name);
    expect(names).toContain("nested.md");
  });

  it("returns 404 for missing directory", async () => {
    const res = await request(app)
      .get("/list/nonexistent")
      .set(auth);
    expect(res.status).toBe(404);
  });

  it("returns 400 when listing a file", async () => {
    const res = await request(app)
      .get("/list/hello.txt")
      .set(auth);
    expect(res.status).toBe(400);
  });

  it("includes file sizes and types", async () => {
    const res = await request(app)
      .get("/list/")
      .set(auth);
    const txt = res.body.entries.find((e: { name: string }) => e.name === "hello.txt");
    expect(txt.type).toBe("file");
    expect(txt.size).toBeGreaterThan(0);
    expect(txt.modified).toBeDefined();

    const dir = res.body.entries.find((e: { name: string }) => e.name === "subdir/");
    expect(dir.type).toBe("directory");
    expect(dir.size).toBeUndefined();
  });
});

describe("share links: /share/:token", () => {
  it("serves a file with a valid share token", async () => {
    const token = generateToken("hello.txt", 3600, TEST_SHARE_SECRET);
    const res = await request(app).get(`/share/${token}`);
    expect(res.status).toBe(200);
    expect(res.text).toBe("Hello, Mímir!");
    expect(res.headers["content-type"]).toMatch(/text\/plain/);
    expect(res.headers["content-disposition"]).toBe("inline");
  });

  it("serves nested files via share token", async () => {
    const token = generateToken("subdir/nested.md", 3600, TEST_SHARE_SECRET);
    const res = await request(app).get(`/share/${token}`);
    expect(res.status).toBe(200);
    expect(res.text).toContain("# Nested file");
  });

  it("serves markdown as attachment (download), not inline", async () => {
    const token = generateToken("subdir/nested.md", 3600, TEST_SHARE_SECRET);
    const res = await request(app).get(`/share/${token}`);
    expect(res.status).toBe(200);
    expect(res.headers["content-disposition"]).toBe('attachment; filename="nested.md"');
  });

  it("declares utf-8 charset on inline text types", async () => {
    const token = generateToken("hello.txt", 3600, TEST_SHARE_SECRET);
    const res = await request(app).get(`/share/${token}`);
    expect(res.headers["content-type"]).toBe("text/plain; charset=utf-8");
  });

  it("serves CSV as attachment with utf-8 charset", async () => {
    const token = generateToken("data.csv", 3600, TEST_SHARE_SECRET);
    const res = await request(app).get(`/share/${token}`);
    expect(res.status).toBe(200);
    expect(res.headers["content-type"]).toBe("text/csv; charset=utf-8");
    expect(res.headers["content-disposition"]).toBe('attachment; filename="data.csv"');
  });

  it("escapes double quotes in attachment filenames", async () => {
    const token = generateToken('quo"te.md', 3600, TEST_SHARE_SECRET);
    const res = await request(app).get(`/share/${token}`);
    expect(res.status).toBe(200);
    expect(res.headers["content-disposition"]).toBe('attachment; filename="quo\\"te.md"');
  });

  it("serves non-ASCII attachment filenames without error", async () => {
    const token = generateToken("åäö.md", 3600, TEST_SHARE_SECRET);
    const res = await request(app).get(`/share/${token}`);
    expect(res.status).toBe(200);
    // latin1-representable names stay as a plain quoted filename per RFC 6266
    expect(res.headers["content-disposition"]).toBe('attachment; filename="åäö.md"');
  });

  it("encodes control characters in filenames instead of crashing", async () => {
    const token = generateToken("line\nbreak.md", 3600, TEST_SHARE_SECRET);
    const res = await request(app).get(`/share/${token}`);
    expect(res.status).toBe(200);
    expect(res.headers["content-disposition"]).toContain(
      "filename*=UTF-8''line%0Abreak.md",
    );
  });

  it("serves PDFs inline", async () => {
    const token = generateToken("doc.pdf", 3600, TEST_SHARE_SECRET);
    const res = await request(app).get(`/share/${token}`);
    expect(res.status).toBe(200);
    expect(res.headers["content-type"]).toBe("application/pdf");
    expect(res.headers["content-disposition"]).toBe("inline");
  });

  it("rejects expired tokens", async () => {
    // Generate a token that expired 1 hour ago
    const token = generateToken("hello.txt", -3600, TEST_SHARE_SECRET);
    const res = await request(app).get(`/share/${token}`);
    expect(res.status).toBe(403);
    expect(res.body.error).toMatch(/expired/i);
  });

  it("rejects tampered tokens", async () => {
    const token = generateToken("hello.txt", 3600, TEST_SHARE_SECRET);
    // Flip last character of signature
    const tampered = token.slice(0, -1) + (token.slice(-1) === "a" ? "b" : "a");
    const res = await request(app).get(`/share/${tampered}`);
    expect(res.status).toBe(403);
    expect(res.body.error).toMatch(/invalid/i);
  });

  it("rejects tokens signed with wrong secret", async () => {
    const token = generateToken("hello.txt", 3600, "wrong-secret");
    const res = await request(app).get(`/share/${token}`);
    expect(res.status).toBe(403);
  });

  it("rejects malformed tokens", async () => {
    const res = await request(app).get("/share/not-a-valid-token");
    expect(res.status).toBe(403);
  });

  it("blocks path traversal in share tokens", async () => {
    const token = generateToken("../../../etc/passwd", 3600, TEST_SHARE_SECRET);
    const res = await request(app).get(`/share/${token}`);
    expect([400, 404]).toContain(res.status);
  });

  it("returns 404 for missing files", async () => {
    const token = generateToken("nonexistent.txt", 3600, TEST_SHARE_SECRET);
    const res = await request(app).get(`/share/${token}`);
    expect(res.status).toBe(404);
  });

  it("does not require Bearer auth", async () => {
    const token = generateToken("hello.txt", 3600, TEST_SHARE_SECRET);
    // No auth header — should still work
    const res = await request(app).get(`/share/${token}`);
    expect(res.status).toBe(200);
  });

  it("includes security headers on share responses", async () => {
    const token = generateToken("hello.txt", 3600, TEST_SHARE_SECRET);
    const res = await request(app).get(`/share/${token}`);
    expect(res.headers["x-content-type-options"]).toBe("nosniff");
    expect(res.headers["cache-control"]).toBe("no-store");
    expect(res.headers["referrer-policy"]).toBe("no-referrer");
  });

  it("supports range requests on share links", async () => {
    const token = generateToken("hello.txt", 3600, TEST_SHARE_SECRET);
    const res = await request(app)
      .get(`/share/${token}`)
      .set("Range", "bytes=0-4");
    expect(res.status).toBe(206);
    expect(res.text).toBe("Hello");
    expect(res.headers["content-type"]).toBe("text/plain; charset=utf-8");
  });
});

describe("share links disabled", () => {
  it("returns 501 when share secret is not configured", async () => {
    const appNoShare = createApp({ apiKey: TEST_API_KEY, rootDir: TEST_ROOT });
    const res = await request(appNoShare).get("/share/some-token");
    expect(res.status).toBe(501);
  });
});

describe("share links: ?dl=1 force-download", () => {
  it("serves inline without dl param (baseline)", async () => {
    const token = generateToken("hello.txt", 3600, TEST_SHARE_SECRET);
    const res = await request(app).get(`/share/${token}`);
    expect(res.status).toBe(200);
    expect(res.headers["content-disposition"]).toBe("inline");
  });

  it("serves attachment with ?dl=1", async () => {
    const token = generateToken("hello.txt", 3600, TEST_SHARE_SECRET);
    const res = await request(app).get(`/share/${token}?dl=1`);
    expect(res.status).toBe(200);
    expect(res.headers["content-disposition"]).toMatch(/^attachment; filename="hello\.txt"$/);
  });

  it("serves attachment with ?download=1", async () => {
    const token = generateToken("hello.txt", 3600, TEST_SHARE_SECRET);
    const res = await request(app).get(`/share/${token}?download=1`);
    expect(res.status).toBe(200);
    expect(res.headers["content-disposition"]).toMatch(/^attachment; filename="hello\.txt"$/);
  });

  it("forces attachment on PDF with ?dl=1 (overrides inline-PDF default)", async () => {
    const token = generateToken("doc.pdf", 3600, TEST_SHARE_SECRET);
    const res = await request(app).get(`/share/${token}?dl=1`);
    expect(res.status).toBe(200);
    expect(res.headers["content-disposition"]).toMatch(/^attachment; filename="doc\.pdf"$/);
  });
});
