# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Do not
open a public issue containing credentials, private URLs, file paths, or exploit
details. Include the affected version or commit, reproduction steps, impact, and
any suggested remediation.

## Supported versions

Security fixes target the latest release and the default branch. This project is
maintained on a best-effort basis; there is no guaranteed response or embargo SLA.

## Deployment boundary

Mímir is not an internet edge proxy. Keep its listener on loopback or a private
network, place an authenticated proxy or tunnel in front of it, use long random
Bearer and share secrets, and restrict filesystem permissions on the served root.
Only configure `MIMIR_TRUST_PROXY` when direct origin access is prevented and the
value precisely identifies the trusted proxy path.

Share URLs are credentials. They can appear in browser history, chat transcripts,
proxy logs, and referrer data. Use the shortest practical TTL, avoid sharing
sensitive files, and rotate `MIMIR_SHARE_SECRET` if a link must be revoked early.

Secret scanning is defense in depth, not proof that an archive is safe to publish.
Review content and backup restores independently.
