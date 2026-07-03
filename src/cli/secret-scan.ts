#!/usr/bin/env node
/**
 * Ingest-time secret scan. Run after rsync imports new files into ~/mimir/
 * and before those files are mirrored to the NAS's Bearer-servable tree —
 * see scripts/sync-artifacts.sh / sync-artifacts-daemon.sh and mimir#13.
 *
 * Usage:
 *   node dist/cli/secret-scan.js <target-dir> [quarantine-dir]
 *   node dist/cli/secret-scan.js --stdin <target-dir> [quarantine-dir]
 *
 * --stdin reads newline-separated relative paths (as produced by
 * `rsync --out-format='%n'`) and scans only those files, rather than
 * walking the whole tree on every sync.
 *
 * Quarantine dir defaults to MIMIR_QUARANTINE_DIR, or a `-quarantine`
 * sibling of the target dir.
 *
 * Exit 0: scan ran (any hits were quarantined and alerted).
 * Exit 1: the scan itself failed (I/O error) — caller must not assume the
 *         tree is clean.
 */
import { resolve } from "node:path";
import { scanAndQuarantine, alertSecretsFound } from "../secret-scan.js";

async function readStdin(): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) chunks.push(chunk as Buffer);
  return Buffer.concat(chunks).toString("utf8");
}

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  let useStdin = false;
  if (args[0] === "--stdin") {
    useStdin = true;
    args.shift();
  }
  const [targetDirArg, quarantineDirArg] = args;

  if (!targetDirArg) {
    process.stderr.write("Usage: secret-scan [--stdin] <target-dir> [quarantine-dir]\n");
    process.exit(1);
  }

  const targetDir = resolve(targetDirArg);
  const quarantineDir = resolve(
    quarantineDirArg ?? process.env.MIMIR_QUARANTINE_DIR ?? `${targetDir}-quarantine`,
  );

  let relativeFiles: string[] | undefined;
  if (useStdin) {
    relativeFiles = (await readStdin())
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean);
    if (relativeFiles.length === 0) {
      console.log("[mimir] secret-scan: no files on stdin — nothing to scan.");
      return;
    }
  }

  const { findings, quarantined } = await scanAndQuarantine(targetDir, quarantineDir, relativeFiles);

  if (quarantined.length > 0) {
    await alertSecretsFound(findings, quarantineDir);
    console.log(`[mimir] secret-scan: quarantined ${quarantined.length} file(s) — see log above.`);
  } else {
    console.log(`[mimir] secret-scan: clean (${relativeFiles ? relativeFiles.length + " file(s)" : targetDir}).`);
  }
}

main().catch((err) => {
  console.error("[mimir] secret-scan: scan failed:", err instanceof Error ? err.message : String(err));
  process.exit(1);
});
