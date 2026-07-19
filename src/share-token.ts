import { createHmac, timingSafeEqual } from "node:crypto";

export const MAX_SHARE_TTL_SECONDS = 7 * 24 * 60 * 60;

/**
 * Stateless HMAC-signed share tokens.
 *
 * Token format (dot-separated, URL-safe):
 *   <exp>.<path-b64url>.<sig-b64url>
 *
 * Where:
 *   exp       = Unix timestamp (seconds) as decimal string
 *   path-b64  = base64url-encoded relative file path
 *   sig-b64   = base64url-encoded HMAC-SHA256(path + ":" + exp, secret)
 */

function toBase64Url(buf: Buffer): string {
  return buf.toString("base64url");
}

function fromBase64Url(s: string): Buffer {
  return Buffer.from(s, "base64url");
}

function sign(path: string, exp: number, secret: string): string {
  return createHmac("sha256", secret)
    .update(`${path}:${exp}`)
    .digest("base64url");
}

export interface ShareToken {
  path: string;
  exp: number;
  sig: string;
}

export function generateToken(
  filePath: string,
  ttlSeconds: number,
  secret: string,
): string {
  const exp = Math.floor(Date.now() / 1000) + ttlSeconds;
  const sig = sign(filePath, exp, secret);
  const pathB64 = toBase64Url(Buffer.from(filePath, "utf-8"));
  return `${exp}.${pathB64}.${sig}`;
}

export function validateToken(
  token: string,
  secret: string,
): { valid: true; path: string; exp: number } | { valid: false; error: string } {
  const parts = token.split(".");
  if (parts.length !== 3) {
    return { valid: false, error: "Invalid token format." };
  }

  const [expStr, pathB64, sig] = parts;
  const exp = parseInt(expStr, 10);
  if (isNaN(exp)) {
    return { valid: false, error: "Invalid token format." };
  }

  const now = Math.floor(Date.now() / 1000);
  if (now > exp) {
    return { valid: false, error: "Share link has expired." };
  }
  if (exp - now > MAX_SHARE_TTL_SECONDS) {
    return { valid: false, error: "Share link expiry exceeds the maximum TTL." };
  }

  let path: string;
  try {
    path = fromBase64Url(pathB64).toString("utf-8");
  } catch {
    return { valid: false, error: "Invalid token format." };
  }

  const expectedSig = sign(path, exp, secret);
  const provided = Buffer.from(sig);
  const expected = Buffer.from(expectedSig);
  if (provided.length !== expected.length || !timingSafeEqual(provided, expected)) {
    return { valid: false, error: "Invalid share link." };
  }

  return { valid: true, path, exp };
}

/** Parse a human-friendly TTL string like "1h", "24h", "7d" into seconds. */
export function parseTTL(ttl: string): number | null {
  const match = ttl.match(/^(\d+)([hd])$/);
  if (!match) return null;
  const value = parseInt(match[1], 10);
  const unit = match[2];
  if (value < 1) return null;
  const seconds = unit === "h" ? value * 3600 : value * 86400;
  return seconds <= MAX_SHARE_TTL_SECONDS ? seconds : null;
}
