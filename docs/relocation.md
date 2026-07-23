# Mimir workload relocation requirements (v1)

This public-safe contract implements the workload-owner boundary in Grimnir
ADR-007. The machine, network realization, and physical relocation are Brokkr
concerns. Mimir owns its archive semantics, service verification, and recovery.
The versioned requirement record is [workload-requirement-v1.json](workload-requirement-v1.json).

## Normative schema and provenance

The exact Grimnir node/substrate v1 schema and the shared consumer fixture
manifest are vendored byte-for-byte under
[`docs/vendor/grimnir/`](vendor/grimnir/) from the immutable source revision in
[workload-requirement-v1.provenance.json](workload-requirement-v1.provenance.json).
`src/node-substrate.ts` refuses to load a vendored artifact whose SHA-256
digest drifts from those pins, and the test suite validates the Mimir
workload manifest against the vendored normative schema rather than
hand-asserting fields. Mimir does not extend or reinterpret decision-driving
shared fields.

## Stable requirements

Mimir can remain configured unchanged through a NAS move when the private owner
overlay continues to bind the same logical identities: the Mimir service and
offsite timer units, loopback API, persistent archive, credential boundary,
tunnel ingress, Mac sync ingress, local T7 copy, encrypted offsite copy,
Heimdall reporting, and accepted deployment marker. Paths, hostnames, tunnel
IDs, endpoints, credentials, and storage locators are intentionally absent.

The archive is persistent data. The deployed code tree, state/log directories,
and artifact archive are separate identities. A relocation must retain the old
archive and previous accepted deployment marker until the new baseline is
verified; path existence is never copy, integrity, or restore proof.

## Read-only hooks and the invocation binding

Run `scripts/relocation-verify.sh preflight` before any mutation and `verify`
after a substrate step. Both are allowlisted and read-only: they read systemd
state and private-overlay evidence receipts only, and produce no deployment,
sync, backup, service, or tunnel change. Machine-readable JSON is emitted on
stdout; concise human diagnostics go to stderr.

Per ADR-007, every invocation is bound to one lifecycle attempt. The private
overlay supplies the binding via environment variables, all mandatory:

| Variable | Field | Format |
| --- | --- | --- |
| `MIMIR_RELOCATION_ATTEMPT_ID` | `attempt_id` | contract id |
| `MIMIR_RELOCATION_PLAN_ID` | `plan_id` | contract id |
| `MIMIR_RELOCATION_PLAN_DIGEST` | `plan_digest` | `sha256:<64 hex>` |
| `MIMIR_RELOCATION_DESIRED_REVISION` | `desired_revision` | `sha256:<64 hex>` |
| `MIMIR_RELOCATION_OBSERVATION_EVIDENCE_ID` | `observation_evidence_id` | contract id |
| `MIMIR_RELOCATION_ACTION` | `action` | `preflight` or `relocate` |
| `MIMIR_RELOCATION_DEADLINE` | `deadline` | exact UTC `YYYY-MM-DDTHH:MM:SSZ` |
| `MIMIR_RELOCATION_IDEMPOTENCY_KEY` | `idempotency_key` | contract id |

The stdout result embeds a `hook_result` that echoes every binding field and is
validated against the vendored normative schema before it is emitted. An
invocation whose deadline has already passed (or that passes while checks run)
records `timed_out` without touching later commands or receipts. The deadline
is checked before every check and caps each systemd status read; the configured
`MIMIR_RELOCATION_TIMEOUT_SECONDS` is an integer from 1 through 300 (default
60). Results are recorded under
`MIMIR_RELOCATION_RESULT_DIR/<hook>-<idempotency_key>.json`: the directory
must be an invoking-user-owned, non-symlink directory with mode `0700`, and
records are mode `0600`. A replay requires the closed output shape, exact
check set/outcomes/reasons, exact UTC `created_at`, a normative `hook_result`,
and a deterministic content digest. Retrying the same idempotency key then
replays the recorded result verbatim; the same key presented with different
bindings is refused as a conflict.

## Evidence receipts

Evidence is consumed as closed typed JSON receipts written by the component
that owns each fact. The private overlay supplies their locations through the
`MIMIR_RELOCATION_*_EVIDENCE` variables; no default path is assumed. A receipt
must be a regular non-symlink file (FIFOs and directories are rejected), owned
by the invoking user, mode `0600`/`0400` (no group/other access), at most 4096
bytes, and exactly this shape:

```json
{
  "kind": "mimir-relocation-evidence",
  "schema_version": "v1",
  "check": "tunnel",
  "status": "tunnel-v1:connected",
  "attempt_id": "attempt-001",
  "plan_id": "plan-001",
  "plan_digest": "sha256:<64 hex>",
  "desired_revision": "sha256:<64 hex>",
  "observation_evidence_id": "obs-001",
  "action": "relocate",
  "observed_at": "2026-07-23T10:00:00Z",
  "valid_until": "2026-07-23T11:00:00Z"
}
```

Freshness is explicit: both timestamps must be exact second-resolution UTC,
`observed_at` must not be in the future, and `valid_until` must still be ahead
of the invocation clock. The six attempt fields shown above must exactly match
the current invocation binding; a mismatch fails closed with a constant
non-leaking reason token. Stale, future, malformed, oversize, wrongly typed,
wrongly owned, or wrongly permissioned receipts also fail closed; receipt
contents and filesystem paths are never echoed to either stream.

Success requires distinct current evidence, not a single green indicator:

| Check | Variable | Required receipt status |
| --- | --- | --- |
| Service | — (systemd read) | `mimir.service` is active. |
| Timer | — (systemd read) | `mimir-offsite.timer` is active. |
| Tunnel | `MIMIR_RELOCATION_TUNNEL_EVIDENCE` | `tunnel-v1:connected` from the tunnel owner. |
| Mac sync | `MIMIR_RELOCATION_SYNC_EVIDENCE` | `sync-v1:complete` after a guarded sync. |
| T7 copy completion | `MIMIR_RELOCATION_T7_COPY_EVIDENCE` | `local-copy-v1:complete` after the copy finishes. |
| T7 integrity | `MIMIR_RELOCATION_T7_INTEGRITY_EVIDENCE` | `local-copy-integrity-v1:verified` from a separate integrity verification. |
| Offsite | `MIMIR_RELOCATION_OFFSITE_EVIDENCE` | `offsite-v1:complete` from encrypted-copy status. |
| Heimdall | `MIMIR_RELOCATION_HEIMDALL_EVIDENCE` | `heimdall-v1:fresh`, a freshness receipt rather than dashboard presence. |
| Restore | `MIMIR_RELOCATION_RESTORE_EVIDENCE` | `restore-v1:representative-ok` from a representative isolated restore. |
| Deployment | `MIMIR_RELOCATION_DEPLOYMENT_EVIDENCE` | `deployment-marker-v1:recoverable`, proving the previous marker/data remain recoverable. |

Copy completion, integrity validation, and representative restore are three
separate receipts from separate procedures. The hook intentionally does not
treat an archive directory, a timer, a finished copy, or a dashboard panel as
a substitute for any of them.

## Mutating lifecycle operations

`drain` and `compensate` are declared mutating hooks with 300-second deadlines
and mandatory idempotency. They are not automated by this repository: invoking
them through the read-only script or CLI is refused. A lifecycle executor may
invoke them only after ADR-007 preflight using one attempt ID, plan/digest,
desired revision, observation evidence, deadline, and idempotency key. A retry
for the same key returns the recorded result; it must not repeat side effects.

Drain must quiesce writes only after a recoverable pre-attempt baseline has been
recorded. A failed, timed-out, or partial drain enters `compensate`; compensation
restores the previous archive/service baseline and is followed by the same
read-only verification. A failed verification after substrate mutation requires
the component rollback/compensation path and stays blocked until old data and
the accepted deployment marker are recoverable. No retry is a new relocation
attempt until that baseline is verified.
