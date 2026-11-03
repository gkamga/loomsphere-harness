"""Regression tests for structured-payload extraction (harness issue #4).

A verbose model narrates before it answers, and its prose often quotes a JSON
*example*. Extraction must take the payload the response contract asked for — the
declared fenced block — rather than whichever object happens to appear last in the
text. Validation stays strict: nothing parseable means the run fails.
"""

from __future__ import annotations

import json
import unittest

from loomsphere_harness.schemas.architect_proposal import (
    ProposalValidationError,
    extract_payload,
    payload_candidates,
)

INLINE_EXAMPLE = '{"summary": "example only", "components": []}'
REAL_PAYLOAD = {"summary": "the real proposal", "components": [{"path": "a.py"}]}


def fenced(payload: dict) -> str:
    return "```json\n" + json.dumps(payload) + "\n```"


class FencedBlockWins(unittest.TestCase):
    def test_quoted_example_before_the_fenced_payload(self) -> None:
        text = (
            "Here is the shape I will use:\n\n"
            f"{INLINE_EXAMPLE}\n\n"
            "Now the proposal itself:\n\n"
            f"{fenced(REAL_PAYLOAD)}\n"
        )
        self.assertEqual(extract_payload(text), REAL_PAYLOAD)

    def test_quoted_example_after_the_fenced_payload(self) -> None:
        text = (
            f"{fenced(REAL_PAYLOAD)}\n\n"
            "For reference, an unrelated object looks like:\n\n"
            f"{INLINE_EXAMPLE}\n"
        )
        self.assertEqual(extract_payload(text), REAL_PAYLOAD)

    def test_two_fenced_blocks_use_the_declared_last_block(self) -> None:
        text = f"{fenced({'summary': 'draft'})}\n\nCorrected:\n\n{fenced(REAL_PAYLOAD)}\n"
        self.assertEqual(extract_payload(text), REAL_PAYLOAD)

    def test_example_fence_that_is_not_json_does_not_hide_the_payload(self) -> None:
        text = "```json\nnot json at all\n```\n\n" f"{fenced(REAL_PAYLOAD)}\n"
        self.assertEqual(extract_payload(text), REAL_PAYLOAD)

    def test_fenced_list_falls_through_to_the_object(self) -> None:
        text = f"```json\n[1, 2, 3]\n```\n\n{fenced(REAL_PAYLOAD)}\n"
        self.assertEqual(extract_payload(text), REAL_PAYLOAD)


class Fallbacks(unittest.TestCase):
    def test_bare_object_without_fence_still_parses(self) -> None:
        text = json.dumps(REAL_PAYLOAD)
        self.assertEqual(extract_payload(text), REAL_PAYLOAD)

    def test_prose_wrapped_object_still_parses(self) -> None:
        text = f"Proposal follows. {json.dumps(REAL_PAYLOAD)} That is all."
        self.assertEqual(extract_payload(text), REAL_PAYLOAD)

    def test_last_object_in_prose_wins_over_an_earlier_example(self) -> None:
        text = f"An example: {INLINE_EXAMPLE}\n\nThe proposal: {json.dumps(REAL_PAYLOAD)}"
        self.assertEqual(extract_payload(text), REAL_PAYLOAD)

    def test_candidate_order_is_declared_first(self) -> None:
        text = f"{INLINE_EXAMPLE}\n\n{fenced(REAL_PAYLOAD)}\n"
        candidates = payload_candidates(text)
        self.assertEqual(json.loads(candidates[0]), REAL_PAYLOAD)
        self.assertEqual(candidates[-1], text)
        self.assertIn(INLINE_EXAMPLE, candidates)


class FailClosed(unittest.TestCase):
    def test_no_json_object_fails(self) -> None:
        with self.assertRaises(ProposalValidationError):
            extract_payload("I could not produce a proposal.")

    def test_truncated_object_fails(self) -> None:
        text = '```json\n{"summary": "cut off here'
        with self.assertRaises(ProposalValidationError):
            extract_payload(text)

    def test_empty_response_fails(self) -> None:
        with self.assertRaises(ProposalValidationError):
            extract_payload("   ")

    def test_non_object_json_fails(self) -> None:
        with self.assertRaises(ProposalValidationError):
            extract_payload("[1, 2, 3]")

    def test_nested_unclosed_object_fails(self) -> None:
        with self.assertRaises(ProposalValidationError):
            extract_payload('{"summary": {"nested": "value"}')


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
