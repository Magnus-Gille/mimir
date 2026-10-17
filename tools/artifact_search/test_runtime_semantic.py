"""Opt-in semantic retrieval in the snapshot runtime, on synthetic fixtures only."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.artifact_search import runtime as runtime_module
from tools.artifact_search.embedding_client import EmbedderUnavailable
from tools.artifact_search.runtime import Runtime, RuntimeErrorCode, handler
from tools.artifact_search.runtime_vectors import SemanticConfig
from tools.artifact_search.snapshot import canonical, create_snapshot
from tools.artifact_search.test_runtime import _http_request, _running_http
from tools.artifact_search.vectors import EmbeddingIdentity

SOURCE_ID = "33333333-3333-4333-8333-333333333333"
REVISION = "a" * 40 + ":" + "b" * 64
ORIGIN = "https://files.example.test"
# Synthetic concepts: words in one group share a dimension, so a paraphrase with
# no lexical overlap ("breeze") still lands on the "wind" document.
CONCEPTS = (("north", "star", "stellar", "polaris"), ("south", "wind", "breeze", "gale"),
            ("archive", "evidence", "record"), ("retrieval", "lookup"))


class _Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class FakeEmbedder:
    def __init__(self, identity, log, failure=None):
        self.identity = identity
        self.log = log
        self.failure = failure

    def embed(self, inputs):
        self.log.append(list(inputs))
        if self.failure is not None:
            raise self.failure
        vectors = []
        for text in inputs:
            words = text.lower().replace(".", " ").replace("|", " ").split()
            vector = [0.0] * self.identity.dimensions
            for index, group in enumerate(CONCEPTS):
                vector[index] = float(sum(word in group for word in words))
            vector[-1] = 0.05  # never a zero vector
            vectors.append(vector)
        return vectors


def _config(**overrides):
    env = {"MIMIR_SEARCH_SEMANTIC": "on", "MIMIR_SEARCH_EMBEDDING_REVISION": REVISION}
    env.update(overrides)
    return SemanticConfig.from_env(env)


class SemanticRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tempdir.name)
        self.calls = []
        self.failure = None
        self.factory_calls = []

    def tearDown(self):
        self.tempdir.cleanup()

    def factory(self, identity, timeout):
        self.factory_calls.append(timeout)
        return FakeEmbedder(identity, self.calls, self.failure)

    def _source(self, name, texts):
        root = self.workspace / (name + "-original")
        store = self.workspace / (name + "-snapshots")
        (root / "docs").mkdir(mode=0o700, parents=True)
        store.mkdir(mode=0o700)
        for filename, text in texts.items():
            (root / "docs" / filename).write_text(text, encoding="utf-8")
        result = create_snapshot(root, store, SOURCE_ID)
        return root, result, Path(result["manifest_path"]).parent

    def _write_handoff(self, root, result, bundle, observed_at=100):
        value = {
            "schema": "mimir-search-handoff-v1", "source_id": SOURCE_ID,
            "snapshot_id": result["snapshot_id"], "observed_at": observed_at,
            "expires_at": observed_at + 100, "kind": "local", "bundle": str(bundle),
            "original": str(root), "mimir_origin": ORIGIN, "original_prefix": "",
        }
        self.handoff.write_bytes(canonical(value))
        os.chmod(self.handoff, 0o600)

    def _runtime(self, semantic=None, texts=None):
        self.root, result, bundle = self._source("first", texts or {
            "alpha.txt": "North star retrieval evidence.\n",
            "beta.txt": "South wind archive evidence.\n",
        })
        self.state = self.workspace / "state"
        self.state.mkdir(mode=0o700)
        self.handoff = self.workspace / "handoff.json"
        self._write_handoff(self.root, result, bundle)
        self.clock = _Clock(150)
        return Runtime(self.state, self.handoff, clock=self.clock,
                       semantic=semantic if semantic is not None else SemanticConfig(),
                       embedder_factory=self.factory)

    def _ready(self, runtime, steps=12):
        for _ in range(steps):
            status = runtime.step()
            vectors = runtime.job().get("vectors", {}).get("status")
            if status == "ready" and (not runtime.semantic.enabled or vectors == "complete"):
                return
        self.fail("runtime did not become ready")

    # -- lexical default --------------------------------------------------

    def test_default_configuration_never_contacts_the_embedding_model(self):
        runtime = self._runtime()
        self._ready(runtime)
        response = runtime.search("north star")
        self.assertEqual(response["mode"], "lexical")
        self.assertEqual(response["decision"], "fts5_bm25")
        self.assertEqual(self.factory_calls, [])
        self.assertNotIn("vectors", runtime.job())
        self.assertFalse(runtime.health()["semantic"]["enabled"])

    def test_semantic_request_while_disabled_is_an_explicit_lexical_fallback(self):
        runtime = self._runtime()
        self._ready(runtime)
        response = runtime.search("north star", mode="hybrid")
        self.assertEqual(response["mode"], "lexical_fallback")
        self.assertEqual(response["requested_mode"], "hybrid")
        self.assertEqual(response["fallback_reason"], "semantic_disabled")
        self.assertEqual(response["results"][0]["path"], "docs/alpha.txt")
        self.assertEqual(self.factory_calls, [])

    def test_invalid_semantic_configuration_reports_error_and_stays_lexical(self):
        config = _config(MIMIR_SEARCH_DEFAULT_MODE="hybrid")  # no abstention policy
        self.assertEqual(config.error, "semantic_default_requires_abstention")
        runtime = self._runtime(config)
        self._ready(runtime)
        self.assertEqual(runtime.health()["semantic"]["error"], "semantic_default_requires_abstention")
        self.assertEqual(runtime.search("north star")["mode"], "lexical")
        fallback = runtime.search("north star", mode="vector")
        self.assertEqual(fallback["fallback_reason"], "semantic_default_requires_abstention")
        self.assertEqual(self.factory_calls, [])

    def test_unknown_mode_is_rejected(self):
        runtime = self._runtime()
        self._ready(runtime)
        with self.assertRaises(RuntimeErrorCode):
            runtime.search("north star", mode="semantic")

    # -- vector build -----------------------------------------------------

    def test_vectors_build_after_lexical_ready_into_private_identity_cache(self):
        runtime = self._runtime(_config())
        statuses = []
        while not statuses or statuses[-1] != "ready":
            statuses.append(runtime.step())
        self.assertNotIn("vectors", runtime.job(), "vectors never delay lexical readiness")
        self.assertTrue(runtime.health()["ready"])
        self.assertEqual(runtime.step(), "ready")
        vectors = runtime.job()["vectors"]
        self.assertEqual(vectors["status"], "complete")
        self.assertEqual((vectors["units"], vectors["covered"]), (2, 2))
        caches = list(self.state.glob("vectors-256d-*.sqlite"))
        self.assertEqual(len(caches), 1)
        self.assertEqual(caches[0].stat().st_mode & 0o777, 0o600)
        self.assertTrue(all(text.startswith("title: ") for batch in self.calls for text in batch))
        health = runtime.health()["semantic"]
        self.assertEqual((health["enabled"], health["vectors"]["status"]), (True, "complete"))
        self.assertNotIn("M5_API_KEY", json.dumps(health))

    def test_step_delay_keeps_building_until_vectors_are_complete(self):
        runtime = self._runtime(_config())
        while runtime.step() != "ready":
            pass
        self.assertEqual(runtime.step_delay(runtime.job()), 1.1)
        runtime.step()
        self.assertEqual(runtime.step_delay(runtime.job()), 60)
        plain = Runtime(self.state, self.handoff, clock=self.clock, semantic=SemanticConfig(),
                        embedder_factory=self.factory)
        self.assertEqual(plain.step_delay({"status": "ready"}), 60)

    def test_transient_gateway_failure_backs_off_without_affecting_lexical(self):
        runtime = self._runtime(_config())
        while runtime.step() != "ready":
            pass
        self.failure = EmbedderUnavailable("embedding_http_error", 503, None)
        self.assertEqual(runtime.step(), "ready")
        vectors = runtime.job()["vectors"]
        self.assertEqual((vectors["status"], vectors["error"], vectors["failures"]),
                         ("unavailable", "embedding_http_error", 1))
        self.assertEqual(vectors["retry_after"], 155)
        self.assertTrue(runtime.health()["ready"])
        self.assertEqual(runtime.search("north star")["mode"], "lexical")
        self.assertGreaterEqual(runtime.step_delay(runtime.job()), 1.1)
        calls = len(self.calls)
        runtime.step()  # still inside the backoff window: no model call
        self.assertEqual(len(self.calls), calls)
        self.failure = None
        self.clock.value = 156
        runtime.step()
        self.assertEqual(runtime.job()["vectors"]["status"], "complete")

    def test_terminal_gateway_failure_needs_explicit_retry(self):
        runtime = self._runtime(_config())
        while runtime.step() != "ready":
            pass
        self.failure = EmbedderUnavailable("embedding_gateway_revision_mismatch")
        runtime.step()
        self.assertEqual(runtime.job()["vectors"], {"status": "failed", "identity": _config().tag,
                                                    "error": "embedding_gateway_revision_mismatch"})
        self.failure = None
        calls = len(self.calls)
        runtime.step()
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(runtime.job()["status"], "ready")
        runtime.control("retry")
        self.assertNotIn("vectors", runtime.job())
        self._ready(runtime)
        self.assertEqual(runtime.job()["vectors"]["status"], "complete")

    def test_missing_embedding_route_is_a_failed_vector_state_not_a_failed_job(self):
        runtime = self._runtime(_config())
        runtime.embedder_factory = mock.Mock(side_effect=ValueError("M5_API_KEY is required"))
        while runtime.step() != "ready":
            pass
        runtime.step()
        self.assertEqual(runtime.job()["vectors"]["error"], "embedding_route_unavailable")
        self.assertTrue(runtime.health()["ready"])

    def test_new_snapshot_rebinds_coverage_and_reuses_unchanged_vectors(self):
        runtime = self._runtime(_config())
        self._ready(runtime)
        first_inputs = sum(len(batch) for batch in self.calls)
        (self.root / "docs" / "gamma.txt").write_text("Polaris lookup record.\n", encoding="utf-8")
        store = self.workspace / "second-snapshots"
        store.mkdir(mode=0o700)
        result = create_snapshot(self.root, store, SOURCE_ID)
        self._write_handoff(self.root, result, Path(result["manifest_path"]).parent, observed_at=120)
        self.calls.clear()
        while runtime.step() != "ready" or runtime.job()["snapshot_id"] != result["snapshot_id"]:
            pass
        self.assertEqual(runtime.search("breeze", mode="vector")["fallback_reason"], "vectors_incomplete")
        self._ready(runtime)
        self.assertEqual(sum(len(batch) for batch in self.calls), 1, "only the new unit is embedded")
        self.assertEqual(first_inputs, 2)
        self.assertEqual(runtime.job()["vectors"]["units"], 3)

    def test_ready_job_that_backs_off_or_fails_keeps_a_readable_job_state(self):
        runtime = self._runtime(_config())
        self._ready(runtime)
        self.clock.value = 250  # lease expired: transient back-off from a ready job
        self.assertEqual(runtime.step(), "pending")
        job = runtime.job()
        self.assertNotIn("vectors", job)
        self.assertEqual(job["error"], "source_stale")
        self.assertFalse(runtime.health()["ready"])
        self.clock.value = 150
        self._write_handoff(self.root, {"snapshot_id": job["snapshot_id"]}, Path(job["handoff"]["bundle"]),
                            observed_at=140)
        runtime.control("retry")
        self._ready(runtime)
        self.assertEqual(runtime.search("breeze", mode="vector")["mode"], "vector")

    # -- retrieval ---------------------------------------------------------

    def test_vector_mode_finds_a_paraphrase_without_lexical_overlap(self):
        runtime = self._runtime(_config())
        self._ready(runtime)
        self.assertEqual(runtime.search("breeze")["results"], [], "lexical has no overlap")
        response = runtime.search("breeze", mode="vector")
        self.assertEqual((response["mode"], response["decision"]), ("vector", "cosine_max_segment"))
        top = response["results"][0]
        self.assertEqual(top["path"], "docs/beta.txt")
        self.assertEqual(top["verification"], "original_sha256_matched")
        self.assertEqual(top["score_kind"], "cosine")
        self.assertEqual(response["abstention"]["policy"], "none")
        self.assertFalse(response["abstained"])
        self.assertEqual(self.calls[-1], ["task: search result | query: breeze"])

    def test_hybrid_mode_fuses_channels_and_verifies_originals(self):
        runtime = self._runtime(_config())
        self._ready(runtime)
        response = runtime.search("north star", mode="hybrid")
        self.assertEqual((response["mode"], response["decision"]), ("hybrid", "rrf60_bounded"))
        self.assertEqual(response["candidate_window"], 5)
        self.assertEqual(response["results"][0]["path"], "docs/alpha.txt")
        self.assertEqual(response["results"][0]["score_kind"], "rrf")
        self.assertTrue(all(item["verification"] == "original_sha256_matched" for item in response["results"]))
        self.assertTrue(all(item["snapshot_id"] == runtime.job()["snapshot_id"] for item in response["results"]))

    def test_semantic_default_with_abstention_policy_withholds_absent_topics(self):
        texts = {"doc%02d.txt" % index: "Archive record number %d.\n" % index for index in range(9)}
        texts["star.txt"] = "North star polaris stellar.\n"
        runtime = self._runtime(_config(MIMIR_SEARCH_DEFAULT_MODE="hybrid",
                                        MIMIR_SEARCH_ABSTAIN_MIN_ZSCORE="2.5"), texts)
        self._ready(runtime)
        present = runtime.search("polaris")
        self.assertEqual(present["mode"], "hybrid")
        self.assertFalse(present["abstained"])
        self.assertEqual(present["results"][0]["path"], "docs/star.txt")
        absent = runtime.search("gale")  # unrelated to every document
        self.assertEqual(absent["mode"], "hybrid")
        self.assertEqual(absent["results"], [])
        self.assertTrue(absent["abstained"])
        self.assertEqual(absent["abstention"]["reason"], "below_score_margin")
        self.assertEqual(absent["abstention"]["policy"], "score")

    def test_vectors_incomplete_falls_back_to_lexical(self):
        runtime = self._runtime(_config())
        while runtime.step() != "ready":
            pass
        response = runtime.search("north star", mode="hybrid")
        self.assertEqual((response["mode"], response["fallback_reason"]), ("lexical_fallback", "vectors_incomplete"))
        self.assertEqual(response["results"][0]["path"], "docs/alpha.txt")

    def test_query_embedding_outage_falls_back_and_opens_a_short_breaker(self):
        runtime = self._runtime(_config())
        self._ready(runtime)
        self.failure = EmbedderUnavailable("embedding_transport_error")
        response = runtime.search("north star", mode="hybrid")
        self.assertEqual((response["mode"], response["fallback_reason"]), ("lexical_fallback", "embedding_unavailable"))
        self.assertEqual(response["results"][0]["path"], "docs/alpha.txt")
        self.assertEqual(self.factory_calls[-1], 3, "interactive query uses the short timeout")
        calls = len(self.calls)
        self.failure = None
        again = runtime.search("north star", mode="hybrid")
        self.assertEqual(again["fallback_reason"], "embedding_unavailable")
        self.assertEqual(len(self.calls), calls, "breaker skips the model while open")
        with mock.patch.object(runtime_module.time, "monotonic", return_value=runtime._breaker_until + 1):
            self.assertEqual(runtime.search("north star", mode="hybrid")["mode"], "hybrid")

    def test_changed_original_suppresses_semantic_results(self):
        runtime = self._runtime(_config())
        self._ready(runtime)
        (self.root / "docs" / "beta.txt").write_text("South wind archive evidence, edited.\n", encoding="utf-8")
        with self.assertRaises(RuntimeErrorCode):
            runtime.search("breeze", mode="vector")

    # -- HTTP contract -----------------------------------------------------

    def test_http_accepts_a_mode_and_rejects_unknown_modes(self):
        runtime = self._runtime(_config())
        self._ready(runtime)
        with _running_http(runtime, api_key="runtime-test-owner-key") as server:
            status, body = _http_request(server, "POST", "/search",
                                         json.dumps({"query": "breeze", "mode": "vector"}).encode())
            self.assertEqual(status, 200)
            self.assertEqual(body["results"][0]["path"], "docs/beta.txt")
            status, body = _http_request(server, "POST", "/search",
                                         json.dumps({"query": "breeze", "mode": "magic"}).encode())
            self.assertEqual((status, body), (400, {"error": "invalid_request"}))
            status, body = _http_request(server, "POST", "/search", json.dumps({"query": "breeze"}).encode())
            self.assertEqual((status, body["mode"]), (200, "lexical"))


if __name__ == "__main__":
    unittest.main()
