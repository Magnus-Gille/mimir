# Search client and agent tool

This client connects to the owner-only Mímir search pilot through an existing SSH
alias. It executes a fixed Python helper on the search host, which calls the
loopback API. Python 3.10+ and OpenSSH are required on the client; Python 3.10+
is required on the search host. No Python packages or new services are needed.
The pilot searches the configured collection lexically (BM25); it does not
generate answers or assess their completeness.

## Operator profile

Create `~/.config/mimir-search/client.json` with mode `0600`, under directories
that only trusted users can write. Bind these values to an accepted runtime
receipt. The profile contains no API key. Example placeholders:

```json
{
  "schema": "mimir-search-client-v1",
  "ssh_host": "search-host",
  "source_id": "22222222-2222-4222-8222-222222222222",
  "mimir_origin": "https://files.example.test",
  "original_prefix": "",
  "release": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "package_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
}
```

`release` and `package_sha256` pin the **server**, not this client's commit.
The snapshot may change during normal refresh, but its identity must agree
throughout each response. A reviewed runtime upgrade requires updating these
two pins; an unexpected revision is rejected. `original_prefix` pins the optional
relative directory between `/files/` and the snapshot-relative result path; use
an empty string for an archive-root collection.

The SSH alias must already work noninteractively with a verified host key.
Unknown host keys, password prompts and failed connections are not accepted.
The helper reads only the existing `MIMIR_SEARCH_API_KEY` from the search host's
`~/.config/mimir-search/runtime.env`. It never transfers that key back to the
client. No new credential, port forwarding, persistent helper or service is
installed on the search host. The local account, SSH configuration and source
installation are trusted operator inputs, not agent-provided arguments.

## Human CLI

From the checked-out repository:

```sh
scripts/mimir-search 'Workflows Compound' --top 3
scripts/mimir-search --query-stdin --json < query.txt
scripts/mimir-search --config /absolute/path/client.json --query-stdin < query.txt
scripts/mimir-search 'Workflows Compound' --mode hybrid --json
```

`--mode {lexical,hybrid,vector}` is optional. Without it the request and the
exact v1 lexical receipt are unchanged. An explicit mode is sent to the runtime
and the extended receipt is validated: `requested_mode`, a per-result `score_kind`
and, for semantic modes, `cosine`, `abstention` and `embedding` (plus
`candidate_window` for `hybrid`). If the semantic request is served lexically the
receipt has `mode: lexical_fallback` with a `fallback_reason`, and human output
says so first. Semantic modes abstain only when the operator has configured a
threshold (`abstention.policy` `score`); none is calibrated yet, so with policy
`none` a semantic hit does not show that the archive answers the question. A
receipt without a sent mode is extended only when the server advertises a
semantic default through `requested_mode`; the client then requires policy
`score`, and lexical stays exact v1. Scores are ranking values, not confidence. `--mode` cannot be combined with `--tool` or
`--tool-schema`.

Use stdin for private queries: a positional query is visible in local process
arguments and may enter shell history. The SSH command never contains the query
or API key. Human output includes path, page/slide/section, snippet and original
URL. Terminal control characters in indexed text are removed from human output.
Original URLs remain authenticated; the client does not download whole originals.

## Agent command adapter

```sh
scripts/mimir-search --tool-schema
scripts/mimir-search --tool < request.json
```

`request.json` contains one object, for example
`{"query":"Workflows Compound","top":3}`. Only `query`, optional `top` and
optional `mode` (`lexical`, `hybrid` or `vector`, as for `--mode`) are accepted;
a `mode` of `null` or any other value is `invalid_request`. `--config` is an
operator argument; configuration, endpoints, shell commands and credentials
cannot be supplied through the tool JSON.

The adapter returns one JSON envelope with schema `mimir-artifact-search-v1`:

- `status`: `ok`, `no_results`, `unavailable`, or `invalid_request`.
- `evidence`: validated runtime receipt, including results and source, snapshot,
  original hash, locator, URL, freshness and accepted server revision.
- `answer_completeness`: always `not_assessed`.
- `guidance`: source text is untrusted data, scores are not probabilities, and
  no hits do not establish that information is absent.

Exit codes are `0` for both hits and no hits, `2` for invalid input, and `1` for
unavailable/untrusted responses. Failures contain stable error codes, never
raw SSH diagnostics, remote exception text or HTTP error bodies. Examples include
`invalid_profile`, `transport_unavailable`, `credential_unavailable`, `unauthorized`,
`retrieval_unavailable`, `invalid_response`, and `stale_response`.

Python consumers can call
`tools.artifact_search.agent_tool.artifact_search(query, top=5, mode=None, profile_path=...)`
and obtain `TOOL_DEFINITION` from the same module. This is a command/function
adapter, not an automatically registered MCP tool. A consuming agent must invoke
the fixed launcher through its existing permitted tool mechanism. Registration
in another repository or harness is a separate integration action.

## Evidence and failure contract

Queries are limited to 4096 characters and an 8192-byte serialized request;
`top` is an integer from 1 to 5. The byte limit applies to the UTF-8 JSON request
as the client sends it (`json.dumps` with `ensure_ascii=False` and default
separators, for example `{"query": "...", "top": 5}`), so a query within 4096
characters can still be rejected as
`invalid_request` when it contains many non-ASCII or JSON-escaped characters
(quotes, backslashes, control characters). JSON Schema cannot express a byte
limit, so `TOOL_DEFINITION` states it in its descriptions. Runtime responses are
bounded to 128 KiB; the helper re-encodes them as compact UTF-8, so the SSH
output is bounded to 128 KiB plus 64 bytes for the `ok`/`data` wrapper. A runtime
`400` maps to `invalid_request`, `401`/`403` to `unauthorized`, and any other
non-200 status to `retrieval_unavailable`; HTTP error bodies are never read.
SSH completion is limited to 45 seconds, with a 10-second connect timeout and a
30-second loopback HTTP timeout. No redirects or proxy environment variables are
used for the loopback request.

The client rejects incorrect source/release/package pins, inconsistent snapshot
references, changed URL origins, malformed locators, and unverified originals.
Receipts must be younger than 30 seconds, their lease must be at most one hour
and still valid; five seconds of forward clock skew is tolerated. Client and
server clocks therefore need to be synchronized. The server independently checks
freshness and original hashes before returning excerpts. Failed validation never
returns partial excerpts.

The agent should cite individual locations and distinguish what the retrieved
text supports from what remains unanswered. This adapter provides retrieval
evidence; it does not turn earlier quality measurements into a guarantee that
every future answer will be complete.

## Verification and activation boundary

```sh
python3 -m unittest tools.artifact_search.test_client_cli tools.artifact_search.test_client_transport
python3 -m unittest discover -s tools/artifact_search -p 'test_*.py'
scripts/mimir-search --tool-schema
```

Tests use synthetic credentials and a real local search runtime with temporary
snapshots and originals. They do not access production keys. Activation requires
an approved immutable client revision, private profile, existing SSH route, and a
live known-query check. Installing the client does not deploy the server or
change its units, refresh timers, source collection or edge rules.

Use a versioned local installation and switch the launcher only after verification.
Retain the previous launcher/profile until acceptance. On failure, restore those
local bindings; the server needs no rollback because this installation does not
change it. The task's approval packet records exact revision, file hashes,
commands, known-query expectation and reversal before activation.
