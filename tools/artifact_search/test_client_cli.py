"""Tests for the bounded user-facing client and agent tool."""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import unittest
from unittest.mock import patch

from tools.artifact_search.client import ClientError


RECEIPT = {
    "mode": "lexical",
    "decision": "fts5_bm25",
    "results": [{
        "path": "docs/guide.pdf",
        "artifact_id": "artifact-1",
        "sha256": "a" * 64,
        "locator": {"kind": "page", "number": 3, "anchor": None},
        "title": "Guide",
        "snippet": "A useful result",
        "score": -1.25,
        "mimir_url": "https://mimir.example/files/docs/guide.pdf#page=3",
        "source_id": "source-1",
        "snapshot_id": "snapshot-1",
        "verification": "verified",
    }],
    "release": "release-1",
    "package_sha256": "b" * 64,
    "source_id": "source-1",
    "snapshot_id": "snapshot-1",
    "observed_at": "2026-10-11T10:00:00Z",
    "expires_at": "2026-10-11T10:05:00Z",
    "verified_at": "2026-10-11T10:00:00Z",
    "abstained": False,
}


class NoisyClientError(ClientError):
    def __init__(self, code: str, detail: str):
        super().__init__(code)
        self.detail = detail

    def __str__(self):
        return self.detail


def load_entrypoints():
    agent = importlib.reload(importlib.import_module("tools.artifact_search.agent_tool"))
    cli = importlib.reload(importlib.import_module("tools.artifact_search.client_cli"))
    return cli, agent


class ClientCliTests(unittest.TestCase):
    def test_query_cli_json_and_profile_are_forwarded(self):
        cli, agent = load_entrypoints()
        output = io.StringIO()
        with patch.object(cli, "search", return_value=RECEIPT) as search, contextlib.redirect_stdout(output):
            status = cli.main(["searchable", "--top", "2", "--config", "/tmp/profile.json", "--json"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue()), RECEIPT)
        search.assert_called_once_with("searchable", 2, profile_path="/tmp/profile.json")

    def test_query_stdin_is_bounded_and_human_output_sanitizes_controls(self):
        cli, _ = load_entrypoints()
        output = io.StringIO()
        unsafe = {**RECEIPT, "results": [{**RECEIPT["results"][0], "snippet": "line\x1b[31mred\x00"}]}
        with patch.object(cli, "search", return_value=unsafe), patch("sys.stdin", io.StringIO("needle\n")), contextlib.redirect_stdout(output):
            status = cli.main(["--query-stdin"])
        self.assertEqual(status, 0)
        self.assertNotIn("\x1b", output.getvalue())
        self.assertNotIn("\x00", output.getvalue())
        self.assertIn("docs/guide.pdf", output.getvalue())
        self.assertIn("page 3", output.getvalue())

    def test_cli_rejects_oversized_stdin_and_non_numeric_top_as_json(self):
        cli, _ = load_entrypoints()
        for argv, stdin in ((["--query-stdin"], "q" * 4097), (["needle", "--top", "many"], "")):
            output = io.StringIO()
            with patch.object(cli, "search") as search, patch("sys.stdin", io.StringIO(stdin)), contextlib.redirect_stdout(output):
                status = cli.main(argv)
            self.assertEqual(status, 2)
            self.assertEqual(json.loads(output.getvalue())["status"], "invalid_request")
            search.assert_not_called()

    def test_tool_mode_success_no_results_and_unavailable_are_machine_json(self):
        _, agent = load_entrypoints()
        with patch.object(agent, "search", return_value=RECEIPT):
            success = agent.artifact_search("needle")
        self.assertEqual(success["schema"], "mimir-artifact-search-v1")
        self.assertEqual(success["status"], "ok")
        self.assertEqual(success["evidence"], RECEIPT)
        self.assertEqual(success["answer_completeness"], "not_assessed")
        self.assertIn("untrusted", success["guidance"].lower())
        self.assertIn("not proof", success["guidance"].lower())
        self.assertIn("not confidence", success["guidance"].lower())
        with patch.object(agent, "search", return_value={**RECEIPT, "results": []}):
            empty = agent.artifact_search("needle")
        self.assertEqual(empty["status"], "no_results")
        with patch.object(agent, "search", side_effect=NoisyClientError("retrieval_unavailable", "token=do-not-show")):
            failed = agent.artifact_search("needle")
        self.assertEqual(failed["status"], "unavailable")
        self.assertNotIn("do-not-show", str(failed))

    def test_tool_rejects_extra_keys_and_invalid_top_without_calling_client(self):
        _, agent = load_entrypoints()
        with patch.object(agent, "search") as search:
            extra = agent.handle_tool_input({"query": "needle", "debug": True})
            invalid = agent.handle_tool_input({"query": "needle", "top": 6})
        self.assertEqual(extra["status"], "invalid_request")
        self.assertEqual(invalid["status"], "invalid_request")
        search.assert_not_called()

    def test_cli_tool_protocol_unknown_keys_invalid_top_and_client_error_are_json(self):
        cli, agent = load_entrypoints()
        for request in ('{"query":"needle","unknown":1}', '{"query":"needle","top":0}', '{"query":"needle","query":"other"}'):
            output = io.StringIO()
            with patch("sys.stdin", io.StringIO(request)), contextlib.redirect_stdout(output):
                status = cli.main(["--tool"])
            self.assertEqual(status, 2)
            self.assertEqual(json.loads(output.getvalue())["status"], "invalid_request")
        output = io.StringIO()
        with patch.object(agent, "search", side_effect=NoisyClientError("unauthorized", "secret detail")), patch("sys.stdin", io.StringIO('{"query":"needle"}')), contextlib.redirect_stdout(output):
            status = cli.main(["--tool"])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "unavailable")
        self.assertNotIn("secret detail", output.getvalue())

    def test_tool_mode_accepts_operator_config_and_preserves_json_byte_bound(self):
        cli, agent = load_entrypoints()
        output = io.StringIO()
        with patch.object(agent, "search", return_value=RECEIPT) as search, patch("sys.stdin", io.StringIO('{"query":"needle"}')), contextlib.redirect_stdout(output):
            status = cli.main(["--tool", "--config", "/tmp/profile.json"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "ok")
        search.assert_called_once_with("needle", 5, profile_path="/tmp/profile.json")

        multibyte = '{"query":"' + ("é" * 4091) + '"}'
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(multibyte)), contextlib.redirect_stdout(output):
            status = cli.main(["--tool"])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "invalid_request")

    def test_tool_mode_returns_safe_error_for_escaped_lone_surrogate(self):
        cli, _ = load_entrypoints()
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO('{"query":"\\ud800"}')), contextlib.redirect_stdout(output):
            status = cli.main(["--tool"])
        self.assertEqual(status, 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "invalid_request")
        self.assertNotIn("\\ud800", output.getvalue())

    def test_tool_schema_does_not_call_client(self):
        cli, _ = load_entrypoints()
        output = io.StringIO()
        with patch.object(cli, "search") as search, contextlib.redirect_stdout(output):
            status = cli.main(["--tool-schema"])
        schema = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(schema["name"], "artifact_search")
        self.assertFalse(schema["inputSchema"]["additionalProperties"])
        search.assert_not_called()


    def test_cli_mode_is_forwarded_and_omitted_by_default(self):
        cli, _ = load_entrypoints()
        with patch.object(cli, "search", return_value=RECEIPT) as search, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["needle", "--mode", "hybrid"]), 0)
        search.assert_called_once_with("needle", 5, mode="hybrid", profile_path=None)
        with patch.object(cli, "search", return_value=RECEIPT) as search, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["needle"]), 0)
        self.assertNotIn("mode", search.call_args.kwargs)

    def test_cli_rejects_bad_or_misplaced_mode_without_calling_client(self):
        cli, _ = load_entrypoints()
        for argv in (["needle", "--mode", "fuzzy"], ["--tool", "--mode", "hybrid"], ["--tool-schema", "--mode", "vector"]):
            output = io.StringIO()
            with patch.object(cli, "search") as search, patch("sys.stdin", io.StringIO('{"query":"needle"}')), contextlib.redirect_stdout(output):
                status = cli.main(argv)
            self.assertEqual(status, 2, argv)
            self.assertEqual(json.loads(output.getvalue())["status"], "invalid_request")
            search.assert_not_called()

    def test_tool_json_mode_is_forwarded_validated_and_optional(self):
        cli, agent = load_entrypoints()
        output = io.StringIO()
        with patch.object(agent, "search", return_value=RECEIPT) as search, patch("sys.stdin", io.StringIO('{"query":"needle","mode":"hybrid"}')), contextlib.redirect_stdout(output):
            status = cli.main(["--tool"])
        self.assertEqual(status, 0)
        search.assert_called_once_with("needle", 5, mode="hybrid", profile_path=None)
        with patch.object(agent, "search", return_value=RECEIPT) as search:
            agent.handle_tool_input({"query": "needle"})
        self.assertNotIn("mode", search.call_args.kwargs)
        for bad in ("null", "1", "true", '"semantic"'):
            output = io.StringIO()
            with patch.object(agent, "search") as search, patch("sys.stdin", io.StringIO('{"query":"needle","mode":%s}' % bad)), contextlib.redirect_stdout(output):
                status = cli.main(["--tool"])
            self.assertEqual(status, 2, bad)
            self.assertEqual(json.loads(output.getvalue())["status"], "invalid_request")
            search.assert_not_called()

    def test_artifact_search_rejects_invalid_mode_with_code(self):
        _, agent = load_entrypoints()
        with patch.object(agent, "search") as search:
            result = agent.artifact_search("needle", mode="semantic")
        self.assertEqual((result["status"], result["error"]["code"]), ("invalid_request", "invalid_mode"))
        search.assert_not_called()

    def test_tool_definition_exposes_mode_enum_and_guidance(self):
        _, agent = load_entrypoints()
        mode = agent.TOOL_DEFINITION["inputSchema"]["properties"]["mode"]
        self.assertEqual(mode, {"type": "string", "enum": ["lexical", "hybrid", "vector"]})
        self.assertNotIn("mode", agent.TOOL_DEFINITION["inputSchema"]["required"])
        self.assertIn("lexical_fallback", agent.GUIDANCE)

    def test_human_output_announces_lexical_fallback(self):
        cli, _ = load_entrypoints()
        fallback = {**RECEIPT, "mode": "lexical_fallback", "fallback_reason": "embedding\x1b[31m_unavailable\n"}
        output = io.StringIO()
        with patch.object(cli, "search", return_value=fallback), contextlib.redirect_stdout(output):
            cli.main(["needle", "--mode", "hybrid"])
        first = output.getvalue().splitlines()[0]
        self.assertEqual(first, "Semantisk sökning ej tillgänglig (embedding [31m_unavailable); visar lexikala träffar.")
        self.assertNotIn("\x1b", output.getvalue())
        self.assertIn("docs/guide.pdf", output.getvalue())


if __name__ == "__main__":
    unittest.main()
