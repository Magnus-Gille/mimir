import express from "express";
import { resolve, join, relative } from "node:path";
import { stat, readdir } from "node:fs/promises";
import { createReadStream } from "node:fs";
import { lookup } from "mime-types";
import { timingSafeEqual } from "node:crypto";

// --- Configuration ---

const PORT = parseInt(process.env.MIMIR_PORT ?? "3031", 10);
const HOST = process.env.MIMIR_HOST ?? "127.0.0.1";
const API_KEY = process.env.MIMIR_API_KEY;
const ROOT_DIR = resolve(process.env.MIMIR_ROOT_DIR ?? "/home/magnus/mimir");
const ALLOWED_HOSTS = (process.env.MIMIR_ALLOWED_HOSTS ?? "")
  .split(",")
  .map((h) => h.trim())
  .filter(Boolean);
const RATE_LIMIT_WINDOW_MS = 60_000;
const RATE_LIMIT_MAX = parseInt(process.env.MIMIR_RATE_LIMIT ?? "60", 10);

// --- Helpers ---

function safeEqual(a: string, b: string): boolean {
  const bufA = Buffer.from(a);
  const bufB = Buffer.from(b);
  if (bufA.length !== bufB.length) return false;
  return timingSafeEqual(bufA, bufB);
}

function resolveFilePath(rootDir: string, requestPath: string): string | null {
  const resolved = resolve(rootDir, requestPath);
  // Prevent path traversal: resolved path must be within rootDir
  if (!resolved.startsWith(rootDir + "/") && resolved !== rootDir) {
    return null;
  }
  return resolved;
}

// --- Rate limiter (in-memory, per-IP) ---

const rateLimitMap = new Map<string, { count: number; resetAt: number }>();

function checkRateLimit(ip: string): boolean {
  const now = Date.now();
  const entry = rateLimitMap.get(ip);
  if (!entry || now > entry.resetAt) {
    rateLimitMap.set(ip, { count: 1, resetAt: now + RATE_LIMIT_WINDOW_MS });
    return true;
  }
  entry.count++;
  return entry.count <= RATE_LIMIT_MAX;
}

// Sweep expired entries periodically
setInterval(() => {
  const now = Date.now();
  for (const [ip, entry] of rateLimitMap) {
    if (now > entry.resetAt) rateLimitMap.delete(ip);
  }
}, 60_000);

// --- Express app ---

export function createApp(config?: { apiKey?: string; rootDir?: string }) {
  const apiKey = config?.apiKey ?? API_KEY;
  const rootDir = resolve(config?.rootDir ?? ROOT_DIR);

  if (!apiKey) {
    throw new Error("MIMIR_API_KEY is required. Set it in .env or pass via config.");
  }

  const app = express();

  // Security headers
  app.use((_req, res, next) => {
    res.setHeader("X-Content-Type-Options", "nosniff");
    res.setHeader("X-Frame-Options", "DENY");
    res.setHeader("Content-Security-Policy", "default-src 'none'");
    res.setHeader("X-Robots-Tag", "noindex, nofollow");
    next();
  });

  // DNS rebinding protection
  if (ALLOWED_HOSTS.length > 0) {
    const defaultHosts = [
      `${HOST}:${PORT}`,
      `localhost:${PORT}`,
      `127.0.0.1:${PORT}`,
    ];
    const allAllowed = new Set([...defaultHosts, ...ALLOWED_HOSTS]);

    app.use((req, res, next) => {
      const host = req.headers.host;
      if (!host || !allAllowed.has(host)) {
        res.status(400).json({ error: "Invalid Host header" });
        return;
      }
      next();
    });
  }

  // Health endpoint (no auth)
  app.get("/health", (_req, res) => {
    res.json({ status: "ok", service: "mimir", root_dir: rootDir });
  });

  // Auth middleware for all other routes
  app.use((req, res, next) => {
    // Rate limiting
    const ip = req.ip ?? req.socket.remoteAddress ?? "unknown";
    if (!checkRateLimit(ip)) {
      res.status(429).json({ error: "Rate limit exceeded. Max 60 requests per minute." });
      return;
    }

    const authHeader = req.headers.authorization;
    if (!authHeader?.startsWith("Bearer ")) {
      res.status(401).json({ error: "Authorization header with Bearer token required." });
      return;
    }
    const token = authHeader.slice(7);
    if (!safeEqual(token, apiKey)) {
      res.status(403).json({ error: "Invalid API key." });
      return;
    }
    next();
  });

  // Directory listing: /list/ and /list/:path
  const listHandler: express.RequestHandler = async (req, res) => {
    const rawPath = (req.params as Record<string, string | string[]>).path;
    const requestPath = Array.isArray(rawPath) ? rawPath.join("/") : (rawPath ?? "");
    const resolved = resolveFilePath(rootDir, requestPath);
    if (!resolved) {
      res.status(400).json({ error: "Invalid path." });
      return;
    }

    try {
      const stats = await stat(resolved);
      if (!stats.isDirectory()) {
        res.status(400).json({ error: "Path is not a directory. Use /files/ to retrieve files." });
        return;
      }

      const dirEntries = await readdir(resolved, { withFileTypes: true });
      const entries = await Promise.all(
        dirEntries
          .filter((e) => !e.name.startsWith("."))
          .map(async (e) => {
            const entryPath = join(resolved, e.name);
            const entryStat = await stat(entryPath).catch(() => null);
            if (!entryStat) return null;
            return {
              name: e.isDirectory() ? e.name + "/" : e.name,
              type: e.isDirectory() ? "directory" as const : "file" as const,
              size: e.isFile() ? entryStat.size : undefined,
              modified: entryStat.mtime.toISOString(),
            };
          })
      );

      const path = "/" + relative(rootDir, resolved);
      res.json({
        path: path.endsWith("/") ? path : path + "/",
        entries: entries.filter(Boolean),
      });
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code === "ENOENT") {
        res.status(404).json({ error: `Directory not found: /${requestPath}` });
        return;
      }
      throw err;
    }
  };
  app.get("/list/", listHandler);
  app.get("/list/{*path}", listHandler);

  // File serving: /files/
  app.get("/files/{*path}", async (req, res) => {
    const rawPath = (req.params as Record<string, string | string[]>).path;
    const requestPath = Array.isArray(rawPath) ? rawPath.join("/") : (rawPath ?? "");
    if (!requestPath) {
      res.status(400).json({ error: "File path required. Use /list/ for directory listings." });
      return;
    }

    const resolved = resolveFilePath(rootDir, requestPath);
    if (!resolved) {
      res.status(400).json({ error: "Invalid path." });
      return;
    }

    try {
      const stats = await stat(resolved);
      if (stats.isDirectory()) {
        res.status(400).json({ error: "Path is a directory. Use /list/ for directory listings, /files/ for individual files." });
        return;
      }

      const mimeType = lookup(resolved) || "application/octet-stream";
      const fileSize = stats.size;

      // Range request support
      const range = req.headers.range;
      if (range) {
        const match = range.match(/bytes=(\d+)-(\d*)/);
        if (match) {
          const start = parseInt(match[1], 10);
          const end = match[2] ? parseInt(match[2], 10) : fileSize - 1;

          if (start >= fileSize || end >= fileSize || start > end) {
            res.status(416).setHeader("Content-Range", `bytes */${fileSize}`).end();
            return;
          }

          res.status(206);
          res.setHeader("Content-Range", `bytes ${start}-${end}/${fileSize}`);
          res.setHeader("Accept-Ranges", "bytes");
          res.setHeader("Content-Length", end - start + 1);
          res.setHeader("Content-Type", mimeType);
          res.setHeader("Last-Modified", stats.mtime.toUTCString());
          createReadStream(resolved, { start, end }).pipe(res);
          return;
        }
      }

      res.setHeader("Content-Type", mimeType);
      res.setHeader("Content-Length", fileSize);
      res.setHeader("Accept-Ranges", "bytes");
      res.setHeader("Last-Modified", stats.mtime.toUTCString());
      createReadStream(resolved).pipe(res);
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code === "ENOENT") {
        res.status(404).json({ error: `File not found: /${requestPath}` });
        return;
      }
      throw err;
    }
  });

  return app;
}

// --- Start server ---

if (process.env.NODE_ENV !== "test" && process.argv[1] && import.meta.url === `file://${process.argv[1]}`) {
  const app = createApp();
  app.listen(PORT, HOST, () => {
    console.log(`Mímir file server listening on ${HOST}:${PORT}`);
    console.log(`Serving files from: ${ROOT_DIR}`);
    if (ALLOWED_HOSTS.length > 0) {
      console.log(`Allowed hosts: ${HOST}:${PORT}, localhost:${PORT}, 127.0.0.1:${PORT}, ${ALLOWED_HOSTS.join(", ")}`);
    }
  });
}
