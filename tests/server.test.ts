import { describe, it, expect, beforeAll, afterAll } from "vitest";
import request from "supertest";
import { createApp } from "../src/index.js";
import { mkdirSync, writeFileSync, rmSync } from "node:fs";
import { join } from "node:path";

const TEST_ROOT = join(import.meta.dirname, "__test_fixtures__");
const TEST_API_KEY = "test-key-for-mimir";

function setup() {
  // Create test fixture directory
  mkdirSync(join(TEST_ROOT, "subdir"), { recursive: true });
  writeFileSync(join(TEST_ROOT, "hello.txt"), "Hello, Mímir!");
  writeFileSync(join(TEST_ROOT, "doc.pdf"), "%PDF-1.4 fake pdf content");
  writeFileSync(join(TEST_ROOT, "subdir", "nested.md"), "# Nested file\n\nSome content.");
  writeFileSync(join(TEST_ROOT, ".hidden"), "should not appear in listings");
}

function teardown() {
  rmSync(TEST_ROOT, { recursive: true, force: true });
}

let app: ReturnType<typeof createApp>;

beforeAll(() => {
  setup();
  app = createApp({ apiKey: TEST_API_KEY, rootDir: TEST_ROOT });
});

afterAll(() => {
  teardown();
});

const auth = { Authorization: `Bearer ${TEST_API_KEY}` };

describe("health", () => {
  it("returns ok without auth", async () => {
    const res = await request(app).get("/health");
    expect(res.status).toBe(200);
    expect(res.body.status).toBe("ok");
    expect(res.body.service).toBe("mimir");
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
