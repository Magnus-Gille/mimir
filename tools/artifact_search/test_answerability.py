from __future__ import annotations

import json
import unittest

from tools.artifact_search.answerability import AnswerabilityError, check_answerability


SOURCE = "The launch happened on 14 March 2025 in Stockholm, Sweden."
CANDIDATES = [{"candidate_id": "C1", "text": SOURCE}]


def response(*requirements: dict[str, object]) -> str:
    return json.dumps({"requirements": list(requirements)})


def supported(requirement: str = "the launch date", quote: str = SOURCE) -> dict[str, object]:
    return {
        "id": "R1",
        "requirement": requirement,
        "status": "supported",
        "evidence": [{"candidate_id": "C1", "quote": quote}],
    }


class AnswerabilityTests(unittest.TestCase):
    def test_empty_candidates_are_structured_missing_without_callback(self) -> None:
        calls: list[object] = []

        def complete(*args: object) -> str:
            calls.append(args)
            return "{}"

        result = check_answerability("What happened?", [], complete)
        self.assertEqual(result, {"answerable": False, "status": "missing", "evidence": [], "checks": []})
        self.assertEqual(calls, [])

    def test_partial_multipart_request_stops_after_first_check(self) -> None:
        calls = 0

        def complete(_prompt: str, _payload: dict[str, object]) -> str:
            nonlocal calls
            calls += 1
            return response(
                {
                    "id": "R1",
                    "requirement": "the date and location",
                    "status": "partial",
                    "evidence": [],
                }
            )

        result = check_answerability("Give the date and location.", CANDIDATES, complete)
        self.assertFalse(result["answerable"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(calls, 1)

    def test_month_does_not_satisfy_exact_day_precision(self) -> None:
        candidate = [{"candidate_id": "C1", "text": "The launch occurred in March 2025."}]

        def complete(_prompt: str, _payload: dict[str, object]) -> str:
            return response(
                {
                    "id": "R1",
                    "requirement": "the exact launch date",
                    "status": "partial",
                    "evidence": [{"candidate_id": "C1", "quote": "launch occurred in March 2025"}],
                }
            )

        result = check_answerability("What exact day was the launch?", candidate, complete)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["answerable"])

    def test_independent_second_check_rejects_omitted_clause(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def complete(prompt: str, payload: dict[str, object]) -> str:
            calls.append((prompt, payload))
            if len(calls) == 1:
                return response(supported("date and location"))
            return response(
                {
                    "id": "R1",
                    "requirement": "the date and location",
                    "status": "partial",
                    "evidence": [{"candidate_id": "C1", "quote": SOURCE}],
                }
            )

        result = check_answerability("Give the date and location.", CANDIDATES, complete)
        self.assertFalse(result["answerable"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1]["question"], "Give the date and location.")
        self.assertEqual(set(calls[1][1]), {"question", "sources"})
        self.assertEqual(calls[1][1]["sources"], [{"candidate_id": "C1", "quote": SOURCE}])

    def test_verifier_cannot_cite_unsupplied_original_text(self) -> None:
        calls = 0
        first_quote = "launch happened on 14 March 2025"

        def complete(_prompt: str, _payload: dict[str, object]) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                return response(supported("the launch date", first_quote))
            return response(supported("the launch date", "Stockholm, Sweden"))

        with self.assertRaisesRegex(AnswerabilityError, "invalid_evidence"):
            check_answerability("What is the launch date?", CANDIDATES, complete)

    def test_false_premise_is_contradicted(self) -> None:
        calls = 0

        def complete(_prompt: str, _payload: dict[str, object]) -> str:
            nonlocal calls
            calls += 1
            return response(
                {
                    "id": "R1",
                    "requirement": "the claimed launch date",
                    "status": "contradicted",
                    "evidence": [{"candidate_id": "C1", "quote": SOURCE}],
                }
            )

        result = check_answerability("When did the nonexistent launch happen?", CANDIDATES, complete)
        self.assertEqual(result["status"], "contradicted")
        self.assertFalse(result["answerable"])
        self.assertEqual(result["evidence"], [])
        self.assertEqual(calls, 1)

    def test_precise_date_and_multipart_happy_path(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def complete(prompt: str, payload: dict[str, object]) -> str:
            calls.append((prompt, payload))
            return response(supported("the exact date and city"))

        result = check_answerability("Give the exact date and city.", CANDIDATES, complete)
        self.assertTrue(result["answerable"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["evidence"], [{"candidate_id": "C1", "quote": SOURCE}])
        self.assertEqual(len(result["checks"]), 2)
        self.assertEqual(len(calls), 2)

    def test_fabricated_quote_is_rejected(self) -> None:
        def complete(_prompt: str, _payload: dict[str, object]) -> str:
            return response(supported(quote="This quote is not in the source."))

        with self.assertRaisesRegex(AnswerabilityError, "invalid_evidence"):
            check_answerability("What is the date?", CANDIDATES, complete)

    def test_unknown_candidate_id_is_rejected(self) -> None:
        def complete(_prompt: str, _payload: dict[str, object]) -> str:
            return response(
                {
                    "id": "R1",
                    "requirement": "the date",
                    "status": "missing",
                    "evidence": [{"candidate_id": "C9", "quote": SOURCE}],
                }
            )

        with self.assertRaisesRegex(AnswerabilityError, "invalid_evidence"):
            check_answerability("What is the date?", CANDIDATES, complete)

    def test_malformed_or_truncated_json_is_rejected(self) -> None:
        for raw in ["{\"requirements\":[", "not json"]:
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(AnswerabilityError, "malformed_json"):
                    check_answerability("What is the date?", CANDIDATES, lambda *_: raw)

    def test_boolean_status_is_not_accepted(self) -> None:
        raw = response(
            {
                "id": "R1",
                "requirement": "the date",
                "status": True,
                "evidence": [],
            }
        )
        with self.assertRaisesRegex(AnswerabilityError, "invalid_requirement"):
            check_answerability("What is the date?", CANDIDATES, lambda *_: raw)

    def test_duplicate_keys_and_duplicate_requirement_ids_are_rejected(self) -> None:
        duplicate_key = '{"requirements":[{"id":"R1","requirement":"date","status":"missing","status":"partial","evidence":[]}]}'
        with self.assertRaisesRegex(AnswerabilityError, "duplicate_json_key"):
            check_answerability("What is the date?", CANDIDATES, lambda *_: duplicate_key)
        duplicate_ids = response(
            {
                "id": "R1",
                "requirement": "date",
                "status": "missing",
                "evidence": [],
            },
            {
                "id": "R1",
                "requirement": "city",
                "status": "missing",
                "evidence": [],
            },
        )
        with self.assertRaisesRegex(AnswerabilityError, "invalid_requirement"):
            check_answerability("What is the date and city?", CANDIDATES, lambda *_: duplicate_ids)

    def test_bounds_are_enforced(self) -> None:
        with self.assertRaisesRegex(AnswerabilityError, "invalid_question"):
            check_answerability("x" * 2001, CANDIDATES, lambda *_: "{}")
        too_many = [{"candidate_id": str(i), "text": "source text"} for i in range(6)]
        with self.assertRaisesRegex(AnswerabilityError, "invalid_candidates"):
            check_answerability("question", too_many, lambda *_: "{}")
        oversized = [{"candidate_id": "C1", "text": "x" * 10001}]
        with self.assertRaisesRegex(AnswerabilityError, "invalid_candidate"):
            check_answerability("question", oversized, lambda *_: "{}")
        huge = "{" + "x" * (64 * 1024) + "}"
        with self.assertRaisesRegex(AnswerabilityError, "json_too_large"):
            check_answerability("question", CANDIDATES, lambda *_: huge)

    def test_callback_failure_propagates(self) -> None:
        error = RuntimeError("backend stopped")

        def complete(_prompt: str, _payload: dict[str, object]) -> str:
            raise error

        with self.assertRaises(RuntimeError) as raised:
            check_answerability("What is the date?", CANDIDATES, complete)
        self.assertIs(raised.exception, error)


if __name__ == "__main__":
    unittest.main()
