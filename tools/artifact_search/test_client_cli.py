"""Tests for the bounded user-facing client and agent tool."""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import sys
import types
import unittest
from unittest.mock import patch


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


class ClientError(Exception):
    def __init__(self, code: str, detail: str = "sensitive detail"):
        super().__init__(detail)
        self.code = code


@contextlib.contextmanager
def load_entrypoints():
    stub = types.ModuleType("tools.artifact_search.client")
    stub.ClientError = ClientError
    stub.search = lambda *args, **kwargs: RECEIPT
    with patch.dict(sys.modules, {"tools.artifact_search.client": stub}):
        sys.modules.pop("tools.artifact_search.client_cli", None)
        sys.modules.pop("tools.artifact_search.agent_tool", None)
        cli = importlib.import_module("tools.artifact_search.client_cli")
        agent = importlib.import_module("tools.artifact_search.agent_tool")
        yield cli, agent, stub


class ClientCliTests(unittest.TestCase):
    def test_query_cli_json_and_profile_are_forwarded(self):
        with load_entrypoints() as (cli, agent, client):
            output = io.StringIO()
            with patch.object(cli, "search", return_value=RECEIPT) as search, contextlib.redirect_stdout(output):
                status = cli.main(["searchable", "--top", "2", "--config", "/tmp/profile.json", "--json"])
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(output.getvalue()), RECEIPT)
            search.assert_called_once_with("searchable", 2, profile_path="/tmp/profile.json")

    def test_query_stdin_is_bounded_and_human_output_sanitizes_controls(self):
        with load_entrypoints() as (cli, _, client):
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
        with load_entrypoints() as (cli, _, client):
            for argv, stdin in ((["--query-stdin"], "q" * 4097), (["needle", "--top", "many"], "")):
                output = io.StringIO()
                with patch.object(cli, "search") as search, patch("sys.stdin", io.StringIO(stdin)), contextlib.redirect_stdout(output):
                    status = cli.main(argv)
                self.assertEqual(status, 2)
                self.assertEqual(json.loads(output.getvalue())["status"], "invalid_request")
                search.assert_not_called()

    def test_tool_mode_success_no_results_and_unavailable_are_machine_json(self):
        with load_entrypoints() as (_, agent, client):
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
            with patch.object(agent, "search", side_effect=ClientError("transport_unavailable", "token=do-not-show")):
                failed = agent.artifact_search("needle")
            self.assertEqual(failed["status"], "unavailable")
            self.assertNotIn("do-not-show", str(failed))

    def test_tool_rejects_extra_keys_and_invalid_top_without_calling_client(self):
        with load_entrypoints() as (_, agent, client):
            with patch.object(agent, "search") as search:
                extra = agent.handle_tool_input({"query": "needle", "debug": True})
                invalid = agent.handle_tool_input({"query": "needle", "top": 6})
            self.assertEqual(extra["status"], "invalid_request")
            self.assertEqual(invalid["status"], "invalid_request")
            search.assert_not_called()

    def test_cli_tool_protocol_unknown_keys_invalid_top_and_client_error_are_json(self):
        with load_entrypoints() as (cli, agent, client):
            for request in ('{"query":"needle","unknown":1}', '{"query":"needle","top":0}'):
                output = io.StringIO()
                with patch("sys.stdin", io.StringIO(request)), contextlib.redirect_stdout(output):
                    status = cli.main(["--tool"])
                self.assertEqual(status, 2)
                self.assertEqual(json.loads(output.getvalue())["status"], "invalid_request")
            output = io.StringIO()
            with patch.object(agent, "search", side_effect=ClientError("auth_failed", "secret detail")), patch("sys.stdin", io.StringIO('{"query":"needle"}')), contextlib.redirect_stdout(output):
                status = cli.main(["--tool"])
            self.assertEqual(status, 1)
            self.assertEqual(json.loads(output.getvalue())["status"], "unavailable")
            self.assertNotIn("secret detail", output.getvalue())

    def test_tool_schema_does_not_call_client(self):
        with load_entrypoints() as (cli, _, client):
            output = io.StringIO()
            with patch.object(cli, "search") as search, contextlib.redirect_stdout(output):
                status = cli.main(["--tool-schema"])
            schema = json.loads(output.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(schema["name"], "artifact_search")
            self.assertFalse(schema["inputSchema"]["additionalProperties"])
            search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
