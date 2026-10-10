from __future__ import annotations

import json
import unittest

from tools.artifact_search.answerability import (
    AnswerabilityError,
    _has_calendar_date,
    _requests_calendar_date,
    _segment_candidates,
    check_answerability,
)


SOURCE = "The launch happened on 14 March 2025 in Stockholm, Sweden."
CANDIDATES = [{"candidate_id": "C1", "text": SOURCE}]


def response(*requirements: dict[str, object]) -> str:
    return json.dumps({"requirements": list(requirements)})


def supported(requirement: str = "the launch date", span_id: str = "S1") -> dict[str, object]:
    return {
        "id": "R1",
        "requirement": requirement,
        "evidence": [span_id],
        "answer": "The launch occurred on 14 March 2025.",
        "justification": "The selected source span states the date explicitly.",
        "status": "supported",
    }


class AnswerabilityTests(unittest.TestCase):
    def test_status_before_answer_is_rejected(self) -> None:
        item = supported()
        wrong_order = {"status": item.pop("status"), **item}
        with self.assertRaisesRegex(AnswerabilityError, "invalid_requirement"):
            check_answerability("What is the launch date?", CANDIDATES,
                                lambda _prompt, _payload: response(wrong_order))

    def test_empty_candidates_are_structured_missing_without_callback(self) -> None:
        calls: list[object] = []

        def complete(*args: object) -> str:
            calls.append(args)
            return "{}"

        result = check_answerability("What happened?", [], complete)
        self.assertEqual(result, {"answerable": False, "status": "missing", "evidence": [], "checks": []})
        self.assertEqual(calls, [])

    def test_spans_are_exact_overlapping_bounded_and_cover_sources(self) -> None:
        text = "".join(f"token-{index:04d} " for index in range(500))
        candidates = [{"candidate_id": "C1", "text": text}]
        spans = _segment_candidates(candidates)
        self.assertGreater(len(spans), 1)
        self.assertEqual([span.span_id for span in spans], [f"S{index}" for index in range(1, len(spans) + 1)])
        self.assertEqual(spans[0].start, 0)
        self.assertEqual(spans[-1].end, len(text))
        self.assertTrue(all(0 < span.end - span.start <= 1200 for span in spans))
        self.assertTrue(all(text[span.start : span.end] == span.text for span in spans))
        self.assertTrue(all(current.start <= previous.end for previous, current in zip(spans, spans[1:])))
        many = _segment_candidates([{"candidate_id": str(index), "text": "x" * 10000} for index in range(5)])
        self.assertLessEqual(len(many), 55)

    def test_source_metadata_preserves_provenance_without_changing_quotes_or_offsets(self) -> None:
        plain = _segment_candidates([{"candidate_id": "C1", "text": SOURCE}])
        labeled = _segment_candidates(
            [{"candidate_id": "C1", "text": SOURCE, "source": "deck-a/page-1"}]
        )
        self.assertEqual([(span.start, span.end, span.text) for span in plain], [(span.start, span.end, span.text) for span in labeled])
        self.assertIsNone(plain[0].source)
        self.assertEqual(labeled[0].source, "deck-a/page-1")

        candidates = [
            {"candidate_id": "C1", "text": SOURCE, "source": "deck-a/page-1"},
            {"candidate_id": "C2", "text": SOURCE, "source": "deck-b/page-9"},
        ]
        calls: list[dict[str, object]] = []

        def complete(_prompt: str, payload: dict[str, object]) -> str:
            calls.append(payload)
            return response(
                {
                    "id": "R1",
                    "requirement": "the launch date in both documents",
                    "evidence": ["S1", "S2"],
                    "answer": "Both documents state the same launch date.",
                    "justification": "Each selected span states the date explicitly.",
                    "status": "supported",
                }
            )

        result = check_answerability("What is the exact launch date?", candidates, complete)
        expected_spans = [
            {"span_id": "S1", "candidate_id": "C1", "text": SOURCE, "source": "deck-a/page-1"},
            {"span_id": "S2", "candidate_id": "C2", "text": SOURCE, "source": "deck-b/page-9"},
        ]
        self.assertEqual(calls[0]["spans"], expected_spans)
        self.assertEqual(calls[1]["spans"], expected_spans)
        self.assertEqual(
            result["evidence"],
            [
                {"candidate_id": "C1", "quote": SOURCE},
                {"candidate_id": "C2", "quote": SOURCE},
            ],
        )
        self.assertEqual(set(result["evidence"][0]), {"candidate_id", "quote"})

    def test_source_metadata_is_optional_and_validated(self) -> None:
        calls: list[dict[str, object]] = []

        def complete(_prompt: str, payload: dict[str, object]) -> str:
            calls.append(payload)
            return response(supported())

        check_answerability("What is the launch date?", CANDIDATES, complete)
        self.assertNotIn("source", calls[0]["spans"][0])
        for source in ("", "   ", None, 42, "x" * 1001):
            with self.subTest(source=source):
                candidate = {"candidate_id": "C1", "text": SOURCE, "source": source}
                with self.assertRaisesRegex(AnswerabilityError, "invalid_candidate"):
                    check_answerability("What is the launch date?", [candidate], lambda *_: "{}")

    def test_calendar_date_parser_is_conservative_and_validates_real_dates(self) -> None:
        positives = [
            "Release date — 3 February 2026",
            "Date: 3rd February 2026",
            "Date: February 3, 2026",
            "Datum: 3:e februari 2026",
            "Date: 2026-02-03",
            "Date: 03/02/2026",
            "Date: 29 February 2024",
        ]
        for text in positives:
            with self.subTest(text=text):
                self.assertTrue(_has_calendar_date(text))
        negatives = [
            "03 — Overview, Feb 2026",
            "Date: 29 February 2025",
            "Date: 3 February",
            "Date: third of February, two thousand twenty-six",
            "Date: 3 February — 2026",
        ]
        for text in negatives:
            with self.subTest(text=text):
                self.assertFalse(_has_calendar_date(text))
        self.assertFalse(_has_calendar_date("3 February"))
        self.assertFalse(_has_calendar_date("2026"))

    def test_calendar_request_detection_excludes_year_month_meta_and_source_queries(self) -> None:
        self.assertTrue(_requests_calendar_date("What is the exact date?"))
        self.assertTrue(_requests_calendar_date("What is the exact launch date?"))
        self.assertTrue(_requests_calendar_date("Give the launch date."))
        self.assertTrue(_requests_calendar_date("On what date did it happen?"))
        self.assertTrue(_requests_calendar_date("Vilket exakt datum anges?"))
        self.assertTrue(_requests_calendar_date("Vilket exakt kalenderdatum anges?"))
        self.assertTrue(_requests_calendar_date("Ange kalenderdatumet."))
        self.assertTrue(_requests_calendar_date("Ange det exakta datumet."))
        self.assertTrue(_requests_calendar_date("What is the year and what is the exact date?"))
        for question in (
            "What is the year?",
            "What is the month?",
            "Why is an exact date missing?",
            "Where is the source for the exact date?",
            "What is the date format?",
            "What is the date field called?",
            "What is the date label?",
            "What is the date syntax?",
            "What is the exact launch date formatting?",
            "Vilket datumformat används?",
        ):
            with self.subTest(question=question):
                self.assertFalse(_requests_calendar_date(question))

    def test_date_metadata_does_not_require_a_calendar_value(self) -> None:
        candidate = [{"candidate_id": "C1", "text": "The date field is called launch_date."}]
        def complete(_prompt: str, _payload: dict[str, object]) -> str:
            return response(supported("the field name"))
        result = check_answerability("What is the date field called?", candidate, complete)
        self.assertTrue(result["answerable"])
        self.assertEqual(len(result["checks"]), 2)
        self.assertTrue(_requests_calendar_date("What is the date field called and what is the launch date?"))

    def test_calendar_date_suppresses_labeled_heading_numbers_only(self) -> None:
        for text in (
            "Page 3, February 2026",
            "Section 3. February 2026",
            "Slide 3 Feb 2026",
            "Sida 3 februari 2026",
            "Avsnitt 3. februari 2026",
            "Bild 3 februari 2026",
        ):
            with self.subTest(text=text):
                self.assertFalse(_has_calendar_date(text))
        for text in (
            "Slide 3 — Date: 3 February 2026",
            "Launch date: 3 February 2026",
        ):
            with self.subTest(text=text):
                self.assertTrue(_has_calendar_date(text))

    def test_full_date_guard_abstains_for_heading_number_and_month_year(self) -> None:
        candidate = [{"candidate_id": "C1", "text": "03 — Overview, Feb 2026"}]

        def complete(_prompt: str, _payload: dict[str, object]) -> str:
            return response(supported("the exact date"))

        result = check_answerability("What is the exact date?", candidate, complete)
        self.assertFalse(result["answerable"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["evidence"], [])
        self.assertEqual(result["checks"][-1]["stage"], "precision")
        self.assertEqual(result["checks"][-1]["requirements"][0]["status"], "partial")

        multipart = check_answerability("What is the year and what is the exact date?", candidate, complete)
        self.assertFalse(multipart["answerable"])
        self.assertEqual(multipart["status"], "partial")

        for question in ("What is the exact launch date?", "Give the launch date.", "Vilket exakt kalenderdatum hände det?"):
            with self.subTest(question=question):
                guarded = check_answerability(question, candidate, complete)
                self.assertFalse(guarded["answerable"])
                self.assertEqual(guarded["status"], "partial")

        year_only = check_answerability("What is the launch year?", candidate, complete)
        self.assertTrue(year_only["answerable"])
        self.assertEqual(len(year_only["checks"]), 2)
        meta = check_answerability("Why is an exact date missing?", candidate, complete)
        self.assertTrue(meta["answerable"])
        self.assertEqual(len(meta["checks"]), 2)

    def test_partial_multipart_request_stops_after_first_check(self) -> None:
        calls = 0

        def complete(_prompt: str, _payload: dict[str, object]) -> str:
            nonlocal calls
            calls += 1
            return response(
                {
                    "id": "R1",
                    "requirement": "the date and location",
                    "evidence": [],
                    "answer": "The date is present but the location is incomplete.",
                    "justification": "The source does not establish every requested part.",
                    "status": "partial",
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
                    "evidence": ["S1"],
                    "answer": "March 2025 is given, but the exact day is absent.",
                    "justification": "The source supplies only month and year.",
                    "status": "partial",
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
                    "evidence": ["S1"],
                    "answer": "The date is stated, but the requested location is not established.",
                    "justification": "The independent check identifies an omitted requested clause.",
                    "status": "partial",
                }
            )

        result = check_answerability("Give the date and location.", CANDIDATES, complete)
        self.assertFalse(result["answerable"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1]["question"], "Give the date and location.")
        self.assertEqual(set(calls[0][1]), {"question", "spans"})
        self.assertEqual(set(calls[1][1]), {"question", "spans"})
        self.assertEqual(calls[1][1]["spans"], [{"span_id": "S1", "candidate_id": "C1", "text": SOURCE}])

    def test_verifier_cannot_cite_unsupplied_span(self) -> None:
        calls = 0
        candidate = [{"candidate_id": "C1", "text": "A" * 1300}]

        def complete(_prompt: str, _payload: dict[str, object]) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                return response(supported("the launch date", "S1"))
            return response(supported("the launch date", "S2"))

        with self.assertRaisesRegex(AnswerabilityError, "invalid_evidence"):
            check_answerability("What is the launch date?", candidate, complete)

    def test_false_premise_is_contradicted(self) -> None:
        calls = 0

        def complete(_prompt: str, _payload: dict[str, object]) -> str:
            nonlocal calls
            calls += 1
            return response(
                {
                    "id": "R1",
                    "requirement": "the claimed launch date",
                    "evidence": ["S1"],
                    "answer": "The claimed launch premise is contradicted by the source.",
                    "justification": "The source conflicts with the requested premise.",
                    "status": "contradicted",
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
        self.assertEqual(len(result["checks"]), 3)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]["spans"], calls[1][1]["spans"])
        self.assertEqual(result["checks"][-1]["stage"], "precision")
        self.assertEqual(result["checks"][-1]["requirements"][0]["status"], "supported")
        requirement_record = result["checks"][0]["requirements"][0]
        self.assertEqual(
            list(requirement_record),
            ["id", "requirement", "evidence", "answer", "justification", "status"],
        )
        self.assertTrue(requirement_record["answer"])
        self.assertTrue(requirement_record["justification"])

    def test_tiny_source_span_is_resolved_verbatim(self) -> None:
        candidate = [{"candidate_id": "C1", "text": "tiny"}]

        def complete(_prompt: str, _payload: dict[str, object]) -> str:
            return response(supported("the tiny fact", "S1"))

        result = check_answerability("What is the tiny fact?", candidate, complete)
        self.assertTrue(result["answerable"])
        self.assertEqual(result["evidence"], [{"candidate_id": "C1", "quote": "tiny"}])

    def test_attempted_quote_fabrication_is_rejected(self) -> None:
        def complete(_prompt: str, _payload: dict[str, object]) -> str:
            return response(
                {
                    "id": "R1",
                    "requirement": "the date",
                    "evidence": [{"candidate_id": "C1", "quote": SOURCE}],
                    "answer": "The date is supported.",
                    "justification": "The source quote is presented as evidence.",
                    "status": "supported",
                }
            )

        with self.assertRaisesRegex(AnswerabilityError, "invalid_evidence"):
            check_answerability("What is the date?", CANDIDATES, complete)

    def test_unknown_candidate_id_is_rejected(self) -> None:
        def complete(_prompt: str, _payload: dict[str, object]) -> str:
            return response(
                {
                    "id": "R1",
                    "requirement": "the date",
                    "evidence": ["S9"],
                    "answer": "",
                    "justification": "The cited span ID is not supplied.",
                    "status": "missing",
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
                "evidence": [],
                "answer": "",
                "justification": "The status type is invalid.",
                "status": True,
            }
        )
        with self.assertRaisesRegex(AnswerabilityError, "invalid_requirement"):
            check_answerability("What is the date?", CANDIDATES, lambda *_: raw)

    def test_answer_and_justification_are_required_and_bounded(self) -> None:
        base = {
            "id": "R1",
            "requirement": "the date",
            "evidence": [],
            "answer": "",
            "justification": "The date is absent.",
            "status": "missing",
        }
        supported_without_answer = dict(base, evidence=["S1"], answer="", status="supported")
        with self.assertRaisesRegex(AnswerabilityError, "invalid_requirement"):
            check_answerability("What is the date?", CANDIDATES, lambda *_: response(supported_without_answer))
        without_justification = dict(base, justification="")
        with self.assertRaisesRegex(AnswerabilityError, "invalid_requirement"):
            check_answerability("What is the date?", CANDIDATES, lambda *_: response(without_justification))
        oversized_answer = dict(base, answer="x" * 2001)
        with self.assertRaisesRegex(AnswerabilityError, "invalid_requirement"):
            check_answerability("What is the date?", CANDIDATES, lambda *_: response(oversized_answer))
        oversized_justification = dict(base, justification="x" * 2001)
        with self.assertRaisesRegex(AnswerabilityError, "invalid_requirement"):
            check_answerability("What is the date?", CANDIDATES, lambda *_: response(oversized_justification))

    def test_unknown_requirement_fields_are_rejected(self) -> None:
        item = supported()
        item["unexpected"] = "value"
        with self.assertRaisesRegex(AnswerabilityError, "invalid_requirement"):
            check_answerability("What is the date?", CANDIDATES, lambda *_: response(item))

    def test_duplicate_keys_and_duplicate_requirement_ids_are_rejected(self) -> None:
        duplicate_key = '{"requirements":[{"id":"R1","requirement":"date","status":"missing","status":"partial","evidence":[]}]}'
        with self.assertRaisesRegex(AnswerabilityError, "duplicate_json_key"):
            check_answerability("What is the date?", CANDIDATES, lambda *_: duplicate_key)
        duplicate_ids = response(
            {
                "id": "R1",
                "requirement": "date",
                "evidence": [],
                "answer": "",
                "justification": "The date is absent.",
                "status": "missing",
            },
            {
                "id": "R1",
                "requirement": "city",
                "evidence": [],
                "answer": "",
                "justification": "The city is absent.",
                "status": "missing",
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
