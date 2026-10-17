"""Contract tests for explicit retrieval modes in the pinned search client."""

from __future__ import annotations

import copy
import json
import os
import time
import unittest
from unittest.mock import patch

from tools.artifact_search import client
from tools.artifact_search import test_runtime_semantic
from tools.artifact_search.test_client_transport import (
    PACKAGE_SHA256,
    RELEASE,
    _profile,
    _receipt,
)


def _signal(units=8, top_cosine=0.75, zscore=2.5):
    return {"units": units, "top_cosine": top_cosine, "zscore": zscore}


def _extended(profile, now, requested, served=None, *, results=True):
    """A receipt in the extended shape the runtime returns for an explicit mode."""
    served = served or requested
    value = _receipt(profile, now, results=results)
    value["requested_mode"] = requested
    for item in value["results"]:
        item["score_kind"] = "bm25"
    if served == "lexical_fallback":
        value.update(mode="lexical_fallback", fallback_reason="semantic_disabled")
    elif served in ("vector", "hybrid"):
        value.update(
            mode=served,
            decision="cosine_max_segment" if served == "vector" else "rrf60_bounded",
            abstention={
                "policy": "none",
                "abstained": not results,
                "reason": None if results else "no_vectors",
                "signal": _signal() if results else _signal(0, None, None),
            },
            embedding={"model": "embeddinggemma-2", "dimensions": 256, "cache": "256d-" + "f" * 16},
        )
        if served == "hybrid":
            value["candidate_window"] = 5
        for item in value["results"]:
            item.update(score_kind="cosine" if served == "vector" else "rrf",
                        score=0.75 if served == "vector" else 2 / 61, cosine=0.75)
    return value


def _with(value, **changes):
    value = copy.deepcopy(value)
    value.update(changes)
    return value


def _with_result(value, **changes):
    value = copy.deepcopy(value)
    value["results"][0].update(changes)
    return value


def _without_result_key(value, key):
    value = copy.deepcopy(value)
    del value["results"][0][key]
    return value


def _with_abstention(value, **changes):
    value = copy.deepcopy(value)
    value["abstention"].update(changes)
    return value


def _with_signal(value, **changes):
    value = copy.deepcopy(value)
    value["abstention"]["signal"].update(changes)
    return value


def _with_embedding(value, **changes):
    value = copy.deepcopy(value)
    value["embedding"].update(changes)
    return value


class ModeRequestTests(unittest.TestCase):
    def setUp(self):
        self.profile = _profile()
        self.now = int(time.time())
        self.payloads = []

    def _search(self, receipt, **kwargs):
        def transport(_profile, payload):
            self.payloads.append(json.loads(payload))
            return receipt
        with patch.object(client, "load_profile", return_value=self.profile), patch.object(
            client, "_transport", side_effect=transport
        ), patch.object(client.time, "time", return_value=self.now):
            return client.search("north star", 5, **kwargs)

    def test_default_request_and_receipt_are_unchanged(self):
        receipt = _receipt(self.profile, self.now)
        self.assertIs(self._search(receipt), receipt)
        self.assertEqual(self.payloads, [{"query": "north star", "top": 5}])

    def test_explicit_mode_is_sent_and_validated_as_extended(self):
        for mode in client.MODES:
            with self.subTest(mode=mode):
                self.payloads.clear()
                receipt = _extended(self.profile, self.now, mode)
                self.assertIs(self._search(receipt, mode=mode), receipt)
                self.assertEqual(self.payloads, [{"query": "north star", "top": 5, "mode": mode}])

    def test_modes_are_a_closed_set(self):
        self.assertEqual(client.MODES, ("lexical", "hybrid", "vector"))
        for mode in ("semantic", "LEXICAL", "lexical_fallback", "", 1, True, ["lexical"]):
            with self.subTest(mode=mode):
                with patch.object(client, "_transport") as transport, patch.object(client, "load_profile") as load:
                    with self.assertRaises(client.ClientError) as raised:
                        client.search("north star", 5, mode=mode)
                self.assertEqual(raised.exception.code, "invalid_request")
                transport.assert_not_called()
                load.assert_not_called()

    def test_v1_receipt_is_rejected_for_an_explicit_mode(self):
        with self.assertRaises(client.ClientError) as raised:
            self._search(_receipt(self.profile, self.now), mode="lexical")
        self.assertEqual(raised.exception.code, "invalid_response")

    def test_extended_receipt_is_rejected_without_a_mode(self):
        with self.assertRaises(client.ClientError) as raised:
            self._search(_extended(self.profile, self.now, "lexical"))
        self.assertEqual(raised.exception.code, "invalid_response")

    def test_advertised_semantic_server_default_is_accepted_without_a_mode(self):
        for default in ("hybrid", "vector"):
            with self.subTest(default=default):
                self.payloads.clear()
                receipt = _with_abstention(_extended(self.profile, self.now, default), policy="score")
                self.assertIs(self._search(receipt), receipt)
                self.assertEqual(self.payloads, [{"query": "north star", "top": 5}])
                for reason in ("vectors_incomplete", "embedding_unavailable", "embedding_route_unavailable"):
                    fallback = _with(_extended(self.profile, self.now, default, "lexical_fallback"),
                                     fallback_reason=reason)
                    self.assertIs(self._search(fallback), fallback)

    def test_server_default_cannot_weaken_lexical_or_abstention(self):
        hybrid = _extended(self.profile, self.now, "hybrid")
        cases = (
            # The runtime refuses a semantic default without an abstention policy.
            ("semantic default without abstention policy", hybrid),
            ("unknown advertised mode", _with(hybrid, requested_mode="semantic", mode="semantic")),
            ("advertised mode not a string", _with(hybrid, requested_mode=["hybrid"])),
            ("lexical is always v1 without a mode", _extended(self.profile, self.now, "lexical")),
            ("lexical fallback must name a semantic request",
             _with(_extended(self.profile, self.now, "hybrid", "lexical_fallback"), requested_mode="lexical")),
            ("served mode differs from advertised", _with_abstention(_with(hybrid, requested_mode="vector"), policy="score")),
        ) + tuple(
            # Any configuration error resets the runtime default to lexical (exact v1).
            ("impossible default fallback " + reason,
             _with(_extended(self.profile, self.now, "hybrid", "lexical_fallback"), fallback_reason=reason))
            for reason in sorted(client.FALLBACK_REASONS - client.DEFAULT_FALLBACK_REASONS)
        )
        for label, receipt in cases:
            with self.subTest(case=label):
                with self.assertRaises(client.ClientError) as raised:
                    self._search(receipt)
                self.assertEqual(raised.exception.code, "invalid_response")


class ExtendedReceiptTests(unittest.TestCase):
    def setUp(self):
        self.profile = _profile()
        self.now = int(time.time())

    def accept(self, value, mode, top=5):
        self.assertIs(client.validate_receipt(value, self.profile, top, mode=mode, now=self.now), value)

    def reject(self, cases, mode, code="invalid_response", top=5):
        for label, value in cases:
            with self.subTest(case=label, mode=mode):
                with self.assertRaises(client.ClientError) as raised:
                    client.validate_receipt(value, self.profile, top, mode=mode, now=self.now)
                self.assertEqual(raised.exception.code, code)

    def test_explicit_lexical(self):
        receipt = _extended(self.profile, self.now, "lexical")
        self.accept(receipt, "lexical")
        self.accept(_extended(self.profile, self.now, "lexical", results=False), "lexical")
        self.reject((
            ("requested mode differs", _with(receipt, requested_mode="hybrid")),
            ("requested mode missing", {k: v for k, v in receipt.items() if k != "requested_mode"}),
            ("lexical never falls back", _with(receipt, mode="lexical_fallback", fallback_reason="semantic_disabled")),
            ("fallback reason without fallback", _with(receipt, fallback_reason="semantic_disabled")),
            ("semantic decision", _with(receipt, decision="rrf60_bounded")),
            ("score kind missing", _without_result_key(receipt, "score_kind")),
            ("score kind cosine", _with_result(receipt, score_kind="cosine")),
            ("cosine on lexical result", _with_result(receipt, cosine=0.5)),
            ("abstention on lexical", _with(receipt, abstention={})),
        ), "lexical")

    def test_lexical_fallback_carries_a_bounded_reason(self):
        for requested in ("hybrid", "vector"):
            receipt = _extended(self.profile, self.now, requested, "lexical_fallback")
            for reason in sorted(client.FALLBACK_REASONS):
                self.accept(_with(receipt, fallback_reason=reason), requested)
            self.accept(_extended(self.profile, self.now, requested, "lexical_fallback", results=False), requested)
            self.reject((
                ("unknown reason", _with(receipt, fallback_reason="model_said_so")),
                ("reason is free text", _with(receipt, fallback_reason="Traceback: secret")),
                ("reason null", _with(receipt, fallback_reason=None)),
                ("reason missing", {k: v for k, v in receipt.items() if k != "fallback_reason"}),
                ("requested mode differs", _with(receipt, requested_mode="lexical")),
                ("fallback with semantic decision", _with(receipt, decision="cosine_max_segment")),
                ("fallback with abstention", _with(receipt, abstention={})),
                ("fallback result with cosine", _with_result(receipt, cosine=0.5)),
                ("fallback result scored as rrf", _with_result(receipt, score_kind="rrf")),
            ), requested)
        self.assertTrue({"semantic_disabled", "vectors_incomplete", "embedding_unavailable",
                         "embedding_route_unavailable", "semantic_default_requires_abstention"} <= client.FALLBACK_REASONS)

    def test_vector(self):
        receipt = _extended(self.profile, self.now, "vector")
        self.accept(receipt, "vector")
        self.accept(_with_result(receipt, score=-0.25, cosine=-0.25), "vector")
        self.reject((
            ("served a different semantic mode", _with(receipt, mode="hybrid")),
            ("requested mode differs", _with(receipt, requested_mode="hybrid")),
            ("wrong decision", _with(receipt, decision="rrf60_bounded")),
            ("candidate window on vector", _with(receipt, candidate_window=5)),
            ("fallback reason on semantic", _with(receipt, fallback_reason="semantic_disabled")),
            ("abstention missing", {k: v for k, v in receipt.items() if k != "abstention"}),
            ("embedding missing", {k: v for k, v in receipt.items() if k != "embedding"}),
            ("score kind bm25", _with_result(receipt, score_kind="bm25")),
            ("score kind rrf", _with_result(receipt, score_kind="rrf")),
            ("cosine missing", _without_result_key(receipt, "cosine")),
            ("score is not the cosine", _with_result(receipt, score=0.5)),
            ("cosine above one", _with_result(receipt, score=1.5, cosine=1.5)),
            ("cosine below minus one", _with_result(receipt, score=-1.5, cosine=-1.5)),
            ("cosine infinite", _with_result(receipt, score=float("inf"), cosine=float("inf"))),
            ("cosine bool", _with_result(receipt, score=True, cosine=True)),
            ("cosine string", _with_result(receipt, cosine="0.75")),
        ), "vector")

    def test_hybrid(self):
        receipt = _extended(self.profile, self.now, "hybrid")
        self.accept(receipt, "hybrid")
        self.accept(_with_result(receipt, score=1 / 65), "hybrid", top=1)
        self.reject((
            ("wrong decision", _with(receipt, decision="cosine_max_segment")),
            ("window missing", {k: v for k, v in receipt.items() if k != "candidate_window"}),
            ("window too small", _with(receipt, candidate_window=4)),
            ("window bool", _with(receipt, candidate_window=True)),
            ("window float", _with(receipt, candidate_window=5.0)),
            ("score kind cosine", _with_result(receipt, score_kind="cosine")),
            ("rrf score zero", _with_result(receipt, score=0)),
            ("rrf score below one contribution", _with_result(receipt, score=1e-300)),
            ("rrf score just below 1/65", _with_result(receipt, score=1 / 66)),
            ("rrf score above two channels", _with_result(receipt, score=0.5)),
            ("cosine missing", _without_result_key(receipt, "cosine")),
        ), "hybrid")
        self.reject((("window must be max(top, 5)", _with(receipt, candidate_window=1)),), "hybrid", top=1)

    def test_abstention_object(self):
        receipt = _extended(self.profile, self.now, "hybrid")
        withheld = _with(_extended(self.profile, self.now, "hybrid", results=False), abstention={
            "policy": "score", "abstained": True, "reason": "below_score_margin", "signal": _signal(12, 0.31, 0.4)})
        self.accept(withheld, "hybrid")
        for reason, signal in (("below_cosine_floor", _signal(3, 0.1, None)),
                               ("too_few_units_for_margin", _signal(7, 0.9, None)),
                               ("no_vectors", _signal(0, None, None))):
            self.accept(_with_abstention(withheld, reason=reason, signal=signal), "hybrid")
        self.accept(_with_abstention(receipt, policy="score"), "hybrid")
        self.accept(_with_signal(receipt, units=7, zscore=None), "hybrid")
        self.reject((
            ("abstention not an object", _with(receipt, abstention=None)),
            ("extra abstention key", _with_abstention(receipt, threshold=0.5)),
            ("unknown policy", _with_abstention(receipt, policy="answer_check")),
            ("abstained not bool", _with_abstention(receipt, abstained=0)),
            ("abstained with results", _with_abstention(receipt, abstained=True, reason="below_score_margin")),
            ("results withheld without abstaining", _with_abstention(withheld, abstained=False, reason=None)),
            ("abstained without reason", _with_abstention(withheld, reason=None)),
            ("reason without abstaining", _with_abstention(receipt, reason="below_score_margin")),
            ("unknown reason", _with_abstention(withheld, reason="model_unsure")),
            ("threshold reason under policy none", _with_abstention(withheld, policy="none")),
            ("signal not an object", _with_abstention(receipt, signal=[8, 0.75, 2.5])),
            ("extra signal key", _with_signal(receipt, mean=0.2)),
            ("units bool", _with_signal(receipt, units=True)),
            ("units negative", _with_signal(receipt, units=-1)),
            ("fewer units than results", _with_signal(_with_signal(receipt, zscore=None), units=0, top_cosine=None)),
            ("top cosine missing with units", _with_signal(receipt, top_cosine=None)),
            ("top cosine out of range", _with_signal(receipt, top_cosine=2.0)),
            ("top cosine without units", _with_signal(withheld, units=0, zscore=None)),
            ("zscore with too few units", _with_signal(receipt, units=7)),
            ("zscore missing with enough units", _with_signal(receipt, zscore=None)),
            ("zscore negative", _with_signal(receipt, zscore=-0.5)),
            ("zscore infinite", _with_signal(receipt, zscore=float("inf"))),
            ("no vectors with units", _with_abstention(withheld, reason="no_vectors")),
            ("too few units with a zscore", _with_abstention(withheld, reason="too_few_units_for_margin",
                                                             signal=_signal(8, 0.75, 2.5))),
            ("margin reason without a zscore", _with_abstention(withheld, reason="below_score_margin",
                                                                signal=_signal(7, 0.75, None))),
        ), "hybrid")

    def test_embedding_identity(self):
        receipt = _extended(self.profile, self.now, "vector")
        self.accept(_with_embedding(receipt, dimensions=768, cache="768d-" + "0" * 16), "vector")
        self.reject((
            ("embedding not an object", _with(receipt, embedding="embeddinggemma-2")),
            ("extra embedding key", _with_embedding(receipt, revision="a" * 40)),
            ("empty model", _with_embedding(receipt, model="")),
            ("model with controls", _with_embedding(receipt, model="gemma\n")),
            ("unsupported dimensions", _with_embedding(receipt, dimensions=512, cache="512d-" + "0" * 16)),
            ("dimensions bool", _with_embedding(receipt, dimensions=True)),
            ("cache for other dimensions", _with_embedding(receipt, cache="768d-" + "0" * 16)),
            ("cache not a tag", _with_embedding(receipt, cache="../vectors.sqlite")),
            ("cache uppercase", _with_embedding(receipt, cache="256d-" + "F" * 16)),
        ), "vector")

    def test_common_v1_checks_still_apply_to_extended_receipts(self):
        receipt = _extended(self.profile, self.now, "vector")
        self.reject((
            ("wrong release", _with(receipt, release="c" * 40)),
            ("foreign source", _with(receipt, source_id="11111111-1111-4111-8111-111111111111")),
            ("extra top-level key", _with(receipt, extra=True)),
            ("bad result url", _with_result(receipt, mimir_url="https://elsewhere.test/files/x")),
            ("abstained flag disagrees", _with(receipt, abstained=True)),
        ), "vector")
        self.reject((("stale", _with(receipt, verified_at=self.now - 31)),), "vector", code="stale_response")
        self.reject((("too many results", _with(receipt, results=receipt["results"] * 2)),), "vector", top=1)

    def test_validate_receipt_rejects_an_unknown_mode(self):
        receipt = _extended(self.profile, self.now, "lexical")
        self.reject((("unknown", _with(receipt, requested_mode="semantic")),), "semantic")


class RuntimeModeContractTests(unittest.TestCase):
    """The client accepts what the real runtime emits for every explicit mode."""

    def setUp(self):
        self.fixture = test_runtime_semantic.SemanticRuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.environment = patch.dict(os.environ, {
            "MIMIR_SEARCH_RELEASE": RELEASE, "MIMIR_SEARCH_PACKAGE_SHA256": PACKAGE_SHA256})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.profile = _profile(source_id=test_runtime_semantic.SOURCE_ID, mimir_origin=test_runtime_semantic.ORIGIN)

    def runtime(self, semantic=None):
        fixture = self.fixture
        if not hasattr(fixture, "state"):
            runtime = fixture._runtime(semantic)
        else:
            runtime = test_runtime_semantic.Runtime(fixture.state, fixture.handoff, clock=fixture.clock,
                                                    semantic=semantic or test_runtime_semantic.SemanticConfig(),
                                                    embedder_factory=fixture.factory)
        fixture._ready(runtime)
        return runtime

    def check(self, runtime, query, mode, top=5):
        response = runtime.search(query, top, mode=mode)
        now = self.fixture.clock.value
        self.assertIs(client.validate_receipt(response, self.profile, top, mode=mode, now=now), response)
        return response

    def test_client_accepts_real_runtime_receipts(self):
        runtime = self.runtime(test_runtime_semantic._config())
        self.assertEqual(self.check(runtime, "north star", "lexical")["results"][0]["score_kind"], "bm25")
        for mode in ("vector", "hybrid"):
            for top in (1, 5):
                response = self.check(runtime, "polaris breeze", mode, top)
                self.assertEqual(response["mode"], mode)
                self.assertEqual(response["abstention"]["policy"], "none")
                self.assertTrue(response["results"])
        with self.assertRaises(client.ClientError):
            client.validate_receipt(runtime.search("north star", 5, mode="vector"), self.profile, 5,
                                    now=self.fixture.clock.value)

    def test_client_accepts_real_runtime_abstention(self):
        runtime = self.runtime(test_runtime_semantic._config(MIMIR_SEARCH_ABSTAIN_MIN_COSINE="0.99"))
        for mode in ("vector", "hybrid"):
            response = self.check(runtime, "volcano", mode)
            self.assertEqual((response["results"], response["abstained"]), ([], True))
            self.assertEqual(response["abstention"]["reason"], "below_cosine_floor")
        runtime = self.runtime(test_runtime_semantic._config(MIMIR_SEARCH_ABSTAIN_MIN_ZSCORE="1"))
        self.assertEqual(self.check(runtime, "polaris", "vector")["abstention"]["reason"], "too_few_units_for_margin")

    def test_client_accepts_real_runtime_fallbacks(self):
        runtime = self.runtime()
        response = self.check(runtime, "north star", "hybrid")
        self.assertEqual((response["mode"], response["fallback_reason"]), ("lexical_fallback", "semantic_disabled"))
        runtime = self.runtime(test_runtime_semantic._config(MIMIR_SEARCH_DEFAULT_MODE="hybrid"))
        self.assertEqual(self.check(runtime, "north star", "vector")["fallback_reason"],
                         "semantic_default_requires_abstention")
        runtime = self.runtime(test_runtime_semantic._config())
        self.fixture.failure = test_runtime_semantic.EmbedderUnavailable("embedding_http_error", 503, None)
        self.assertEqual(self.check(runtime, "north star", "vector")["fallback_reason"], "embedding_unavailable")

    def test_client_accepts_advertised_server_default(self):
        runtime = self.runtime(test_runtime_semantic._config(
            MIMIR_SEARCH_DEFAULT_MODE="hybrid", MIMIR_SEARCH_ABSTAIN_MIN_COSINE="0.1"))
        response = self.check(runtime, "polaris breeze", None)
        self.assertEqual((response["mode"], response["requested_mode"]), ("hybrid", "hybrid"))
        self.assertEqual(response["abstention"]["policy"], "score")
        self.assertTrue(response["results"])
        self.fixture.failure = test_runtime_semantic.EmbedderUnavailable("embedding_http_error", 503, None)
        response = self.check(runtime, "north star", None)
        self.assertEqual((response["mode"], response["requested_mode"]), ("lexical_fallback", "hybrid"))

    def test_every_field_of_real_runtime_receipts_is_required_and_closed(self):
        runtime = self.runtime(test_runtime_semantic._config())
        receipts = [(mode, runtime.search("north star" if mode == "lexical" else "polaris breeze", 5, mode=mode))
                    for mode in client.MODES]
        receipts.append(("hybrid", self.runtime().search("north star", 5, mode="hybrid")))
        self.assertEqual([r["mode"] for _, r in receipts], ["lexical", "hybrid", "vector", "lexical_fallback"])
        now = self.fixture.clock.value

        def rejected(value, mode):
            with self.assertRaises(client.ClientError):
                client.validate_receipt(value, self.profile, 5, mode=mode, now=now)

        for mode, receipt in receipts:
            client.validate_receipt(receipt, self.profile, 5, mode=mode, now=now)
            self.assertTrue(receipt["results"])
            paths = [()] + [("results", 0)] + [(key,) for key in ("abstention", "embedding") if key in receipt]
            if "abstention" in receipt:
                paths.append(("abstention", "signal"))
            for path in paths:
                target = receipt
                for step in path:
                    target = target[step]
                for key in list(target) + ["unexpected"]:
                    with self.subTest(mode=receipt["mode"], path=path, key=key):
                        mutated = copy.deepcopy(receipt)
                        node = mutated
                        for step in path:
                            node = node[step]
                        if key in node:
                            del node[key]
                        else:
                            node[key] = None
                        rejected(mutated, mode)
            for other in (None, *client.MODES):
                # A semantic server default may legitimately answer with a fallback.
                if other != mode and not (other is None and receipt["mode"] == "lexical_fallback"):
                    with self.subTest(mode=receipt["mode"], validated_as=other):
                        rejected(receipt, other)

    def test_config_error_codes_are_known_fallback_reasons(self):
        on = {"MIMIR_SEARCH_SEMANTIC": "on", "MIMIR_SEARCH_EMBEDDING_REVISION": test_runtime_semantic.REVISION}
        environments = (
            {"MIMIR_SEARCH_SEMANTIC": "yes"},
            {"MIMIR_SEARCH_DEFAULT_MODE": "fuzzy"},
            {"MIMIR_SEARCH_DEFAULT_MODE": "hybrid"},
            {"MIMIR_SEARCH_SEMANTIC": "on"},
            {**on, "MIMIR_SEARCH_EMBEDDING_DIMENSIONS": "512"},
            {**on, "MIMIR_SEARCH_ABSTAIN_MIN_COSINE": "2"},
            {**on, "MIMIR_SEARCH_ABSTAIN_MIN_ZSCORE": "-1"},
            {**on, "MIMIR_SEARCH_DEFAULT_MODE": "vector"},
        )
        for environment in environments:
            with self.subTest(environment=environment):
                error = test_runtime_semantic.SemanticConfig.from_env(environment).error
                self.assertIsNotNone(error)
                self.assertIn(error, client.FALLBACK_REASONS)


if __name__ == "__main__":
    unittest.main()
