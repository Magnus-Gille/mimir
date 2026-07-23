# Mimir workload relocation requirements (v1)

This public-safe contract implements the workload-owner boundary in Grimnir
ADR-007. The machine, network realization, and physical relocation are Brokkr
concerns. Mimir owns its archive semantics, service verification, and recovery.
The versioned requirement record is [workload-requirement-v1.json](workload-requirement-v1.json).
It pins the exact shared schema and fixture-set revision/digests used by every
consumer; Mimir does not extend or reinterpret decision-driving shared fields.

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

## Evidence and read-only hook

Run `scripts/relocation-verify.sh preflight` before any mutation and `verify`
after a substrate step. It is allowlisted and read-only: it reads systemd state
and private-overlay evidence files only. Each invocation has a bounded timeout
(default 60 seconds), produces no deployment, sync, backup, service, or tunnel
change, and is idempotent. The private overlay supplies evidence locations using
the `MIMIR_RELOCATION_*_EVIDENCE` variables; no default path is assumed.

Success requires distinct current evidence, not a single green indicator:

| Evidence | Required success criterion |
| --- | --- |
| Service | `mimir.service` is active. |
| Timer | `mimir-offsite.timer` is active. |
| Tunnel | `tunnel-v1:connected` from the tunnel owner. |
| Mac sync | `sync-v1:complete` after a guarded sync. |
| T7 local copy | `local-copy-v1:complete` after copy and integrity verification. |
| Offsite | `offsite-v1:complete` from encrypted-copy status. |
| Heimdall | `heimdall-v1:fresh`, a freshness receipt rather than dashboard presence. |
| Restore | `restore-v1:representative-ok` from a representative isolated restore. |
| Deployment | `deployment-marker-v1:recoverable`, proving the previous marker/data remain recoverable. |

Copy completion, integrity validation, and representative restore are separate
records. The hook intentionally does not treat an archive directory, a timer,
or a dashboard panel as a substitute for any of them.

## Mutating lifecycle operations

`drain` and `compensate` are declared mutating hooks with 300-second deadlines
and mandatory idempotency. They are not automated by this repository: invoking
them through the read-only script is refused. A lifecycle executor may invoke
them only after ADR-007 preflight using one attempt ID, plan/digest, desired
revision, observation evidence, deadline, and idempotency key. A retry for the
same key returns the recorded result; it must not repeat side effects.

Drain must quiesce writes only after a recoverable pre-attempt baseline has been
recorded. A failed, timed-out, or partial drain enters `compensate`; compensation
restores the previous archive/service baseline and is followed by the same
read-only verification. A failed verification after substrate mutation requires
the component rollback/compensation path and stays blocked until old data and
the accepted deployment marker are recoverable. No retry is a new relocation
attempt until that baseline is verified.
