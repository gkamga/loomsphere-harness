"""Regression tests for output truncation detection (harness issue #3).

A hosted model that hits its output ceiling returns a plausible-looking prefix of
an answer. Reporting that as an invalid contract hid the real cause, so the runtime
now names truncation explicitly and keeps the evidence.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from loomsphere_harness.pydantic_harness import (
    REASON_RESPONSE_TRUNCATED,
    finish_reason,
    looks_incomplete,
    truncation_detail,
    truncation_evidence,
)

COMPLETE_PAYLOAD = '```json\n{"summary": "done"}\n```'


@dataclass
class _Response:
    finish_reason: str | None


@dataclass
class _Result:
    response: _Response | None = None
    messages: tuple[object, ...] = ()

    def all_messages(self) -> tuple[object, ...]:
        return self.messages


class FinishReason(unittest.TestCase):
    def test_reads_the_final_model_response(self) -> None:
        result = _Result(response=_Response("length"))
        self.assertEqual(finish_reason(result), "length")

    def test_falls_back_to_the_message_history(self) -> None:
        @dataclass
        class _Message:
            finish_reason: str | None

        result = _Result(response=None, messages=(_Message("stop"), _Message("length")))
        self.assertEqual(finish_reason(result), "length")

    def test_unknown_when_the_provider_reports_nothing(self) -> None:
        @dataclass
        class _Message:
            finish_reason: str | None = None

        self.assertIsNone(finish_reason(_Result(messages=(_Message(),))))
        self.assertIsNone(finish_reason(object()))


class Completeness(unittest.TestCase):
    def test_complete_fenced_payload_is_complete(self) -> None:
        self.assertFalse(looks_incomplete(COMPLETE_PAYLOAD))

    def test_unterminated_fence_is_incomplete(self) -> None:
        self.assertTrue(looks_incomplete('```json\n{"summary": "cut off'))

    def test_unbalanced_object_is_incomplete(self) -> None:
        self.assertTrue(looks_incomplete('{"summary": "cut off'))

    def test_braces_inside_strings_do_not_count(self) -> None:
        self.assertFalse(looks_incomplete('{"summary": "a { b } c"}'))

    def test_ordinary_prose_is_complete(self) -> None:
        self.assertFalse(looks_incomplete("No JSON here, just prose."))


class Evidence(unittest.TestCase):
    def test_length_finish_reason_is_truncation(self) -> None:
        evidence = truncation_evidence(
            finish_reason="length",
            output_tokens=4096,
            max_output_tokens=4096,
            response='{"summary": "cut',
        )
        assert evidence is not None
        self.assertIn("finish-reason-length", evidence["signals"])
        self.assertIn("incomplete-payload", evidence["signals"])
        self.assertEqual(evidence["finish_reason"], "length")

    def test_length_finish_reason_counts_even_when_the_text_looks_complete(self) -> None:
        evidence = truncation_evidence(
            finish_reason="length",
            output_tokens=10,
            max_output_tokens=4096,
            response=COMPLETE_PAYLOAD,
        )
        assert evidence is not None
        self.assertEqual(evidence["signals"], ["finish-reason-length"])

    def test_clean_finish_at_the_ceiling_is_not_truncation(self) -> None:
        self.assertIsNone(
            truncation_evidence(
                finish_reason="stop",
                output_tokens=4096,
                max_output_tokens=4096,
                response=COMPLETE_PAYLOAD,
            )
        )

    def test_incomplete_text_alone_is_not_truncation(self) -> None:
        # A model may simply answer badly; that stays a contract failure.
        self.assertIsNone(
            truncation_evidence(
                finish_reason="stop",
                output_tokens=12,
                max_output_tokens=4096,
                response='{"summary": "cut',
            )
        )

    def test_unreported_finish_reason_at_the_ceiling_with_incomplete_text(self) -> None:
        evidence = truncation_evidence(
            finish_reason=None,
            output_tokens=4096,
            max_output_tokens=4096,
            response='```json\n{"summary": "cut',
        )
        assert evidence is not None
        self.assertIn("output-token-ceiling", evidence["signals"])

    def test_ordinary_response_is_not_truncation(self) -> None:
        self.assertIsNone(
            truncation_evidence(
                finish_reason="stop",
                output_tokens=200,
                max_output_tokens=4096,
                response=COMPLETE_PAYLOAD,
            )
        )

    def test_detail_names_the_reason_and_the_remedy(self) -> None:
        evidence = truncation_evidence(
            finish_reason="length",
            output_tokens=4096,
            max_output_tokens=4096,
            response='{"summary": "cut',
        )
        assert evidence is not None
        detail = truncation_detail(evidence, response='{"summary": "cut')
        self.assertIn(REASON_RESPONSE_TRUNCATED.split("-")[1], detail)
        self.assertIn("model.max_output_tokens", detail)
        self.assertIn("length", detail)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
