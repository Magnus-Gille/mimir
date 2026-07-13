import express from "express";
import { resolve, join, relative } from "node:path";
import { open, realpath, readdir } from "node:fs/promises";
import { constants, realpathSync } from "node:fs";
import { lookup } from "mime-types";
import contentDisposition from "content-disposition";
import { timingSafeEqual } from "node:crypto";
import { validateToken } from "./share-token.js";
import { startHeimdallReporter } from "./heimdall-report.js";

// --- Configuration ---

const PORT = parseInt(process.env.MIMIR_PORT ?? "3031", 10);
const HOST = process.env.MIMIR_HOST ?? "127.0.0.1";
const API_KEY = process.env.MIMIR_API_KEY;
const SHARE_SECRET = process.env.MIMIR_SHARE_SECRET;
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

function isWithinRoot(rootDir: string, candidate: string): boolean {
  return candidate === rootDir || candidate.startsWith(rootDir + "/");
}

/**
 * Resolve an existing request path and verify both its lexical path and real
 * filesystem target stay inside the archive. The realpath check is essential:
 * rsync preserves symlinks, so a lexical startsWith jail alone can follow an
 * in-tree link to arbitrary files or directories outside Mimir's root.
 *
 * Missing paths deliberately throw ENOENT so callers can preserve the public
 * 404 behavior. A containment violation returns null and is a 400.
 */
async function resolveFilePath(
  rootDir: string,
  realRootDir: string,
  requestPath: string,
): Promise<string | null> {
  // Normalize even caller-supplied relative roots before lexical containment.
  // createApp currently does this as well, but keeping the invariant local to
  // the jail prevents a future call site from silently changing its meaning.
  const lexicalRoot = resolve(rootDir);
  const lexicalPath = resolve(lexicalRoot, requestPath);
  if (!isWithinRoot(lexicalRoot, lexicalPath)) {
    return null;
  }

  const realPath = await realpath(lexicalPath);
  return isWithinRoot(realRootDir, realPath) ? realPath : null;
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

// --- Shared file-serving helper ---

const INLINE_TYPES = new Set([
  "application/pdf",
  "text/plain",
  "text/html",
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/svg+xml",
  "image/webp",
  "audio/mpeg",
  "audio/ogg",
  "video/mp4",
  "video/webm",
]);
const SAFE_OPEN_FLAGS = constants.O_RDONLY | constants.O_NOFOLLOW;

async function serveFile(
  req: express.Request,
  res: express.Response,
  rootDir: string,
  realRootDir: string,
  requestPath: string,
  options?: { disposition?: "inline" | "attachment" },
): Promise<void> {
  if (!requestPath) {
    res.status(400).json({ error: "File path required. Use /list/ for directory listings." });
    return;
  }

  try {
    const resolved = await resolveFilePath(rootDir, realRootDir, requestPath);
    if (!resolved) {
      res.status(400).json({ error: "Invalid path." });
      return;
    }

    // Open first, then stat and stream through the same handle. This removes
    // the stat -> createReadStream race where a concurrent rsync deletion could
    // otherwise emit an unhandled ReadStream error and terminate the server.
    const file = await open(resolved, SAFE_OPEN_FLAGS);
    let streamOwnsFile = false;
    try {
      const stats = await file.stat();
      if (stats.isDirectory()) {
        res.status(400).json({ error: "Path is a directory. Use /list/ for directory listings, /files/ for individual files." });
        return;
      }

      const mimeType = lookup(resolved) || "application/octet-stream";
      const contentType = mimeType.startsWith("text/") ? `${mimeType}; charset=utf-8` : mimeType;
      const fileSize = stats.size;

      // Content-Disposition for share links
      if (options?.disposition) {
        const useInline = options.disposition === "inline" && INLINE_TYPES.has(mimeType);
        res.setHeader("Content-Disposition", useInline ? "inline" : contentDisposition(resolved));
      }

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
          res.setHeader("Content-Type", contentType);
          res.setHeader("Last-Modified", stats.mtime.toUTCString());
          const stream = file.createReadStream({ start, end, autoClose: true });
          streamOwnsFile = true;
          stream.on("error", (err) => res.destroy(err));
          stream.pipe(res);
          return;
        }
      }

      res.setHeader("Content-Type", contentType);
      res.setHeader("Content-Length", fileSize);
      res.setHeader("Accept-Ranges", "bytes");
      res.setHeader("Last-Modified", stats.mtime.toUTCString());
      const stream = file.createReadStream({ autoClose: true });
      streamOwnsFile = true;
      stream.on("error", (err) => res.destroy(err));
      stream.pipe(res);
    } finally {
      if (!streamOwnsFile) await file.close();
    }
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") {
      res.status(404).json({ error: `File not found: /${requestPath}` });
      return;
    }
    if ((err as NodeJS.ErrnoException).code === "ELOOP") {
      res.status(400).json({ error: "Invalid path." });
      return;
    }
    throw err;
  }
}

// --- Heimdall self-descriptor ---
// Served at GET /heimdall.json for Tier-1 discovery by the Heimdall dashboard.
// Shape must satisfy Heimdall's validateDescriptor (schema/service/v1).
// Keep version in sync with package.json when bumping.
export const HEIMDALL_DESCRIPTOR = {
  _schema: 'https://heimdall.gille.ai/schema/service/v1',
  service: {
    name: 'mimir',
    label: 'Mímir',
    namespace: 'grimnir',
    instance_id: 'nas',
    criticality: 'normal',
  },
  kind: 'http-service',
  status: 'pass',
  version: '0.1.0',
  deploy: {
    host: 'nas',
    systemd_unit: 'mimir',
    platform: 'bare-metal',
  },
  metrics: [],
  alerts: { rules: [], active_count: 0, firing: [] },
  panels: [],
  links: {
    self: '/heimdall.json',
    health: '/health',
    repo: 'https://github.com/Magnus-Gille/mimir',
  },
  ui: { icon: 'book', category: 'infra' },
} as const;

// --- Express app ---

export function createApp(config?: { apiKey?: string; rootDir?: string; shareSecret?: string }) {
  const apiKey = config?.apiKey ?? API_KEY;
  const configuredRoot = resolve(config?.rootDir ?? ROOT_DIR);
  const shareSecret = config?.shareSecret ?? SHARE_SECRET;

  if (!apiKey) {
    throw new Error("MIMIR_API_KEY is required. Set it in .env or pass via config.");
  }

  // Fail fast if the configured archive root itself is missing. Request paths
  // are compared against this canonical root to prevent symlink escapes.
  const rootDir = realpathSync(configuredRoot);
  const realRootDir = rootDir;

  const app = express();

  // Trust proxy (behind Cloudflare Tunnel)
  app.set("trust proxy", true);

  // Security headers
  app.use((_req, res, next) => {
    res.setHeader("X-Content-Type-Options", "nosniff");
    res.setHeader("X-Frame-Options", "DENY");
    res.setHeader("Content-Security-Policy", "default-src 'none'");
    res.setHeader("X-Robots-Tag", "noindex, nofollow");
    res.setHeader("Cache-Control", "no-store");
    res.setHeader("Referrer-Policy", "no-referrer");
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

  // Rate limiting (standalone — applies to all routes)
  app.use((req, res, next) => {
    const ip = req.ip ?? req.socket.remoteAddress ?? "unknown";
    if (!checkRateLimit(ip)) {
      res.status(429).json({ error: "Rate limit exceeded. Max 60 requests per minute." });
      return;
    }
    next();
  });

  // Health endpoint (no auth)
  app.get("/health", (_req, res) => {
    res.json({ status: "ok", service: "mimir", root_dir: rootDir });
  });

  // Heimdall self-descriptor (no auth)
  app.get("/heimdall.json", (_req, res) => {
    res.json(HEIMDALL_DESCRIPTOR);
  });

  // Share endpoint (no Bearer auth — token in URL provides auth)
  app.get("/share/:token", async (req, res) => {
    if (!shareSecret) {
      res.status(501).json({ error: "Share links are not configured on this server." });
      return;
    }

    const result = validateToken(req.params.token, shareSecret);
    if (!result.valid) {
      res.status(403).json({ error: result.error });
      return;
    }

    // ?dl=1 forces a download (attachment) instead of inline browser playback
    const forceDownload = req.query.dl === "1" || req.query.download === "1";
    await serveFile(req, res, rootDir, realRootDir, result.path, {
      disposition: forceDownload ? "attachment" : "inline",
    });
  });

  // Auth middleware for all remaining routes
  app.use((req, res, next) => {
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
    try {
      const resolved = await resolveFilePath(rootDir, realRootDir, requestPath);
      if (!resolved) {
        res.status(400).json({ error: "Invalid path." });
        return;
      }

      const directory = await open(resolved, SAFE_OPEN_FLAGS);
      const stats = await directory.stat().finally(() => directory.close());
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
            const realEntryPath = await resolveFilePath(
              realRootDir,
              realRootDir,
              entryPath,
            ).catch(() => null);
            // Do not expose even the metadata/name of an external or dangling
            // symlink through a directory listing.
            if (!realEntryPath) return null;
            const entry = await open(realEntryPath, SAFE_OPEN_FLAGS).catch(() => null);
            if (!entry) return null;
            const entryStat = await entry.stat().finally(() => entry.close());
            const isDirectory = entryStat.isDirectory();
            return {
              name: isDirectory ? e.name + "/" : e.name,
              type: isDirectory ? "directory" as const : "file" as const,
              size: entryStat.isFile() ? entryStat.size : undefined,
              modified: entryStat.mtime.toISOString(),
            };
          })
      );

      const path = "/" + relative(rootDir, resolve(rootDir, requestPath));
      res.json({
        path: path.endsWith("/") ? path : path + "/",
        entries: entries.filter(Boolean),
      });
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code === "ENOENT") {
        res.status(404).json({ error: `Directory not found: /${requestPath}` });
        return;
      }
      if ((err as NodeJS.ErrnoException).code === "ELOOP") {
        res.status(400).json({ error: "Invalid path." });
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
    await serveFile(req, res, rootDir, realRootDir, requestPath);
  });

  return app;
}

// --- Start server ---

if (process.env.NODE_ENV !== "test" && process.argv[1] && import.meta.url === `file://${process.argv[1]}`) {
  const app = createApp();
  app.listen(PORT, HOST, () => {
    console.log(`Mímir file server listening on ${HOST}:${PORT}`);
    console.log(`Serving files from: ${ROOT_DIR}`);
    if (SHARE_SECRET) {
      console.log("Share links: enabled");
    } else {
      console.log("Share links: disabled (set MIMIR_SHARE_SECRET to enable)");
    }
    if (ALLOWED_HOSTS.length > 0) {
      console.log(`Allowed hosts: ${HOST}:${PORT}, localhost:${PORT}, 127.0.0.1:${PORT}, ${ALLOWED_HOSTS.join(", ")}`);
    }

    // Start Heimdall self-report (no-op if env vars are absent).
    // The interval is unref()'d, so it never blocks process exit and we
    // deliberately register NO signal handlers here — adding a SIGTERM/SIGINT
    // listener would override Node's default termination and leave the file
    // server running on systemd stop / Ctrl-C. Node's default handling exits
    // the process; the unref'd timer simply stops with it.
    startHeimdallReporter(ROOT_DIR);
  });
}
