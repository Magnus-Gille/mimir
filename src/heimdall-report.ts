// --- Heimdall panel push types ---

interface StatusPanel {
  service: string;
  panel: string;
  kind: "status";
  label?: string;
  state: "pass" | "warn" | "fail";
  message?: string;
}

interface StatPanel {
  service: string;
  panel: string;
  kind: "stat";
  label?: string;
  value: number;
  unit?: string;
}

export type Panel = StatusPanel | StatPanel;

// --- Pure panel builder (unit-testable) ---

/**
 * Build the two mimir status panels for a given rootDir and uptime.
 * Pure function: no I/O, no side effects.
 */
export function buildReportPanels({
  rootDir,
  uptimeS,
}: {
  rootDir: string;
  uptimeS: number;
}): Panel[] {
  const uptimeH = Math.round((uptimeS / 3600) * 10) / 10;
  return [
    {
      service: "mimir",
      panel: "health",
      kind: "status",
      label: "Mímir",
      state: "pass",
      message: `serving ${rootDir}`,
    },
    {
      service: "mimir",
      panel: "uptime",
      kind: "stat",
      label: "Uptime",
      value: uptimeH,
      unit: "h",
    },
  ];
}

// --- Push helpers ---

const REPORT_INTERVAL_MS = 60_000;
const PUSH_TIMEOUT_MS = 4_000;

/**
 * POST a single panel to the Heimdall hub. Exported so one-shot callers
 * (e.g. the secret-scan alert) can reuse the same push semantics — fail-soft,
 * 4 s timeout, never throws — without duplicating them.
 */
export async function pushPanel(
  hubUrl: string,
  token: string,
  panel: Panel,
): Promise<void> {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), PUSH_TIMEOUT_MS);
  try {
    const response = await fetch(hubUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(panel),
      signal: ac.signal,
    });
    // Treat non-2xx as a (logged) failure — fetch resolves for 4xx/5xx.
    // Log only the panel id + status code; never the token, headers, or body.
    if (!response.ok) {
      console.warn(
        `[mimir] Heimdall push rejected (panel=${panel.panel}, status=${response.status}).`,
      );
    }
  } catch (err) {
    console.warn(
      `[mimir] Heimdall push failed (panel=${panel.panel}):`,
      err instanceof Error ? err.message : String(err),
    );
  } finally {
    clearTimeout(timer);
  }
}

async function runReport(
  hubUrl: string,
  token: string,
  rootDir: string,
): Promise<void> {
  const panels = buildReportPanels({ rootDir, uptimeS: process.uptime() });
  for (const panel of panels) {
    await pushPanel(hubUrl, token, panel);
  }
}

// --- Periodic reporter ---

/**
 * Start the Heimdall self-report loop.
 *
 * Env-gated: requires both HEIMDALL_HUB_URL and HEIMDALL_FLEET_TOKEN.
 * Returns a cleanup function on success, or null when env vars are absent
 * (safe to call: logs a single debug line and does nothing).
 *
 * Fail-soft: every push is wrapped in try/catch with a 4 s abort timeout;
 * errors are logged as warnings and never propagate.
 *
 * The interval is unref()'d so it does not prevent process exit.
 */
export function startHeimdallReporter(rootDir: string): (() => void) | null {
  const hubUrl = process.env.HEIMDALL_HUB_URL;
  const token = process.env.HEIMDALL_FLEET_TOKEN;

  if (!hubUrl || !token) {
    console.debug(
      "[mimir] Heimdall reporter disabled (HEIMDALL_HUB_URL/HEIMDALL_FLEET_TOKEN not set).",
    );
    return null;
  }

  const fire = (): void => {
    // Terminal catch: runReport is fail-soft internally, but guard the
    // fire-and-forget call so any unexpected rejection (e.g. outside the
    // inner fetch try/catch) can never become an unhandled rejection / crash.
    runReport(hubUrl, token, rootDir).catch((err) => {
      console.warn(
        "[mimir] Heimdall report cycle errored:",
        err instanceof Error ? err.message : String(err),
      );
    });
  };

  // Immediate report on startup, then every 60 s
  fire();
  const interval = setInterval(fire, REPORT_INTERVAL_MS);
  interval.unref(); // don't block process exit

  return () => clearInterval(interval);
}
