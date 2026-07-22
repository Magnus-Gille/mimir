import { describe, it, expect } from "vitest";
import { generateToken, validateToken, parseTTL } from "../src/share-token.js";

const SECRET = "test-secret-32-bytes-long-enough";

describe("generateToken / validateToken", () => {
  it("round-trips a valid token", () => {
    const token = generateToken("docs/file.pdf", 3600, SECRET);
    const result = validateToken(token, SECRET);
    expect(result.valid).toBe(true);
    if (result.valid) {
      expect(result.path).toBe("docs/file.pdf");
      expect(result.exp).toBeGreaterThan(Math.floor(Date.now() / 1000));
    }
  });

  it("rejects expired tokens", () => {
    const token = generateToken("file.txt", -1, SECRET);
    const result = validateToken(token, SECRET);
    expect(result.valid).toBe(false);
    if (!result.valid) expect(result.error).toMatch(/expired/i);
  });

  it("rejects otherwise valid tokens beyond the seven-day maximum", () => {
    const token = generateToken("file.txt", 8 * 86400, SECRET);
    const result = validateToken(token, SECRET);
    expect(result.valid).toBe(false);
    if (!result.valid) expect(result.error).toMatch(/maximum ttl/i);
  });

  it("rejects tokens with wrong secret", () => {
    const token = generateToken("file.txt", 3600, SECRET);
    const result = validateToken(token, "wrong-secret");
    expect(result.valid).toBe(false);
    if (!result.valid) expect(result.error).toMatch(/invalid/i);
  });

  it("rejects tampered path", () => {
    const token = generateToken("file.txt", 3600, SECRET);
    const [exp, , sig] = token.split(".");
    const tamperedPath = Buffer.from("other.txt").toString("base64url");
    const tampered = `${exp}.${tamperedPath}.${sig}`;
    const result = validateToken(tampered, SECRET);
    expect(result.valid).toBe(false);
  });

  it("rejects tampered expiry", () => {
    const token = generateToken("file.txt", 3600, SECRET);
    const [, pathB64, sig] = token.split(".");
    const futureExp = Math.floor(Date.now() / 1000) + 999999;
    const tampered = `${futureExp}.${pathB64}.${sig}`;
    const result = validateToken(tampered, SECRET);
    expect(result.valid).toBe(false);
  });

  it("rejects malformed tokens", () => {
    expect(validateToken("", SECRET).valid).toBe(false);
    expect(validateToken("one.two", SECRET).valid).toBe(false);
    expect(validateToken("a.b.c.d", SECRET).valid).toBe(false);
    expect(validateToken("notanumber.path.sig", SECRET).valid).toBe(false);
  });

  it("handles unicode file paths", () => {
    const token = generateToken("docs/résumé.pdf", 3600, SECRET);
    const result = validateToken(token, SECRET);
    expect(result.valid).toBe(true);
    if (result.valid) expect(result.path).toBe("docs/résumé.pdf");
  });
});

describe("parseTTL", () => {
  it("parses hours", () => {
    expect(parseTTL("1h")).toBe(3600);
    expect(parseTTL("24h")).toBe(86400);
  });

  it("parses days", () => {
    expect(parseTTL("1d")).toBe(86400);
    expect(parseTTL("7d")).toBe(604800);
  });

  it("rejects invalid formats", () => {
    expect(parseTTL("")).toBeNull();
    expect(parseTTL("24")).toBeNull();
    expect(parseTTL("abc")).toBeNull();
    expect(parseTTL("1m")).toBeNull();
    expect(parseTTL("-1h")).toBeNull();
    expect(parseTTL("0h")).toBeNull();
    expect(parseTTL("8d")).toBeNull();
    expect(parseTTL("169h")).toBeNull();
  });
});
