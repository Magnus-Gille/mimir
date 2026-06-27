import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { buildReportPanels, startHeimdallReporter } from "../src/heimdall-report.js";

// ---------------------------------------------------------------------------
// buildReportPanels — pure function tests
// ---------------------------------------------------------------------------

describe("buildReportPanels", () => {
  it("returns exactly 2 panels", () => {
    const panels = buildReportPanels({ rootDir: "/srv/mimir", uptimeS: 3600 });
    expect(panels).toHaveLength(2);
  });

  it("health panel has correct service, kind, state, label, and message", () => {
    const panels = buildReportPanels({ rootDir: "/srv/mimir", uptimeS: 3600 });
    const health = panels.find((p) => p.panel === "health");
    expect(health).toMatchObject({
      service: "mimir",
      panel: "health",
      kind: "status",
      label: "Mímir",
      state: "pass",
      message: "serving /srv/mimir",
    });
  });

  it("health panel message reflects the given rootDir", () => {
    const panels = buildReportPanels({ rootDir: "/custom/root", uptimeS: 0 });
    const health = panels.find((p) => p.panel === "health");
    expect(health).toHaveProperty("message", "serving /custom/root");
  });

  it("uptime panel has correct service, kind, label, and unit", () => {
    const panels = buildReportPanels({ rootDir: "/srv/mimir", uptimeS: 0 });
    const uptime = panels.find((p) => p.panel === "uptime");
    expect(uptime).toMatchObject({
      service: "mimir",
      panel: "uptime",
      kind: "stat",
      label: "Uptime",
      unit: "h",
    });
  });

  it("uptime value is rounded to 1 decimal hour (1.5 h)", () => {
    const panels = buildReportPanels({ rootDir: "/srv/mimir", uptimeS: 5400 });
    const uptime = panels.find((p) => p.panel === "uptime");
    expect(uptime).toHaveProperty("value", 1.5);
  });

  it("uptime value is 0.0 for a freshly started process", () => {
    const panels = buildReportPanels({ rootDir: "/srv/mimir", uptimeS: 0 });
    const uptime = panels.find((p) => p.panel === "uptime");
    expect(uptime).toHaveProperty("value", 0);
  });

  it("uptime value rounds to nearest tenth (e.g. 3666 s ≈ 1.0 h)", () => {
    const panels = buildReportPanels({ rootDir: "/srv/mimir", uptimeS: 3666 });
    const uptime = panels.find((p) => p.panel === "uptime");
    expect(uptime).toHaveProperty("value", 1.0);
  });
});

// ---------------------------------------------------------------------------
// startHeimdallReporter — env-gating and fetch calls
// ---------------------------------------------------------------------------

describe("startHeimdallReporter", () => {
  const HUB_URL = "http://hub.local/api/panels";
  const FLEET_TOKEN = "test-fleet-token";

  beforeEach(() => {
    delete process.env.HEIMDALL_HUB_URL;
    delete process.env.HEIMDALL_FLEET_TOKEN;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns null and never calls fetch when env vars are absent", () => {
    const mockFetch = vi.fn();
    vi.stubGlobal("fetch", mockFetch);

    const cleanup = startHeimdallReporter("/srv/mimir");

    expect(cleanup).toBeNull();
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("returns null when only HEIMDALL_HUB_URL is set", () => {
    process.env.HEIMDALL_HUB_URL = HUB_URL;
    const mockFetch = vi.fn();
    vi.stubGlobal("fetch", mockFetch);

    const cleanup = startHeimdallReporter("/srv/mimir");

    expect(cleanup).toBeNull();
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("returns null when only HEIMDALL_FLEET_TOKEN is set", () => {
    process.env.HEIMDALL_FLEET_TOKEN = FLEET_TOKEN;
    const mockFetch = vi.fn();
    vi.stubGlobal("fetch", mockFetch);

    const cleanup = startHeimdallReporter("/srv/mimir");

    expect(cleanup).toBeNull();
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("returns a cleanup function when both env vars are present", async () => {
    process.env.HEIMDALL_HUB_URL = HUB_URL;
    process.env.HEIMDALL_FLEET_TOKEN = FLEET_TOKEN;

    const mockFetch = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal("fetch", mockFetch);

    const cleanup = startHeimdallReporter("/srv/mimir");

    // Flush microtasks so the initial async report settles
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(typeof cleanup).toBe("function");

    cleanup!();
  });

  it("POSTs exactly 2 panels on startup", async () => {
    process.env.HEIMDALL_HUB_URL = HUB_URL;
    process.env.HEIMDALL_FLEET_TOKEN = FLEET_TOKEN;

    const mockFetch = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal("fetch", mockFetch);

    const cleanup = startHeimdallReporter("/srv/mimir");

    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(mockFetch).toHaveBeenCalledTimes(2);

    cleanup!();
  });

  it("sends POSTs to the configured hub URL", async () => {
    process.env.HEIMDALL_HUB_URL = HUB_URL;
    process.env.HEIMDALL_FLEET_TOKEN = FLEET_TOKEN;

    const mockFetch = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal("fetch", mockFetch);

    const cleanup = startHeimdallReporter("/srv/mimir");

    await new Promise((resolve) => setTimeout(resolve, 0));

    for (const call of mockFetch.mock.calls) {
      expect(call[0]).toBe(HUB_URL);
    }

    cleanup!();
  });

  it("includes Authorization: Bearer header on every POST", async () => {
    process.env.HEIMDALL_HUB_URL = HUB_URL;
    process.env.HEIMDALL_FLEET_TOKEN = FLEET_TOKEN;

    const mockFetch = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal("fetch", mockFetch);

    const cleanup = startHeimdallReporter("/srv/mimir");

    await new Promise((resolve) => setTimeout(resolve, 0));

    for (const [, init] of mockFetch.mock.calls as [string, RequestInit][]) {
      const headers = init.headers as Record<string, string>;
      expect(headers["Authorization"]).toBe(`Bearer ${FLEET_TOKEN}`);
      expect(headers["Content-Type"]).toBe("application/json");
    }

    cleanup!();
  });

  it("POSTs health panel and uptime panel by kind", async () => {
    process.env.HEIMDALL_HUB_URL = HUB_URL;
    process.env.HEIMDALL_FLEET_TOKEN = FLEET_TOKEN;

    const mockFetch = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal("fetch", mockFetch);

    const cleanup = startHeimdallReporter("/srv/mimir");

    await new Promise((resolve) => setTimeout(resolve, 0));

    const bodies = mockFetch.mock.calls.map(([, init]: [string, RequestInit]) =>
      JSON.parse(init.body as string),
    );

    const healthBody = bodies.find((b: { panel: string }) => b.panel === "health");
    expect(healthBody).toMatchObject({ service: "mimir", kind: "status", state: "pass" });

    const uptimeBody = bodies.find((b: { panel: string }) => b.panel === "uptime");
    expect(uptimeBody).toMatchObject({ service: "mimir", kind: "stat", unit: "h" });

    cleanup!();
  });
});
