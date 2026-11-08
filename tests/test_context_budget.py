"""Bounded model context: retained tool-result text, stub fidelity, refusal.

A PydanticAI run re-sends the whole message history on every request, so a tool
result is paid for once per remaining request. The M00-02 pilot showed the cost:
seven capped reads exhausted a 150,000-token QA budget with no verdict. These
tests pin the correction down without a network, a provider, or wall-clock
dependence: they assert a bound exists, that it names what it removed, that it
never touches evidence the harness must keep verbatim, and that it stops before a
request that provably cannot fit rather than overshooting.

They also assert the bound cannot be used to weaken QA: elision keeps the tool, the
target, the true size, and a digest, the newest evidence stays verbatim, and the
recorded audit trail still reports what each tool actually returned.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from uuid import uuid4

from pydantic_ai.messages import (
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime_support import RuntimeCase, model_config  # noqa: E402

from loomsphere_harness.agent_runner import (  # noqa: E402
    AgentExecutionError,
    AgentInvocation,
)
from loomsphere_harness.implementation_prompt import (  # noqa: E402
    QA_RULES,
    compose_qa_prompt,
)
from loomsphere_harness.pydantic_harness import (  # noqa: E402
    MAX_READ_CHARS,
    ContextPolicy,
    PydanticHarnessRunner,
)

#: A marker that appears only in file content, so its absence from a request or
#: from recorded evidence proves the content itself was not copied somewhere.
CONTENT_MARKER = "UNIQUE-BODY-MARKER"

#: Elision disabled. This is the pre-fix behaviour, and it is what the "before"
#: column of every measurement is computed against.
UNBOUNDED = ContextPolicy(max_retained_chars=10**9, keep_recent=10**6, elidable_tools=())


class ContextCase(RuntimeCase):
    """A repository with files large enough to reach the read cap."""

    def setUp(self) -> None:
        super().setUp()
        body = "".join(
            f"def helper_{index:04d}(value):\n"
            f"    return value  # {CONTENT_MARKER} padding padding padding padding\n"
            for index in range(1400)
        )
        for name in ("big.py", "other.py"):
            (self.repo / name).write_text(body, encoding="utf-8")
        (self.repo / "small.txt").write_text("tiny\n", encoding="utf-8")

    def reads(self, sequence: list[str]):
        """A model that performs the reads in order, then answers."""

        state = {"step": 0}
        requests: list[dict[str, object]] = []

        def respond(messages, info):
            returns = [
                part
                for message in messages
                for part in getattr(message, "parts", ())
                if isinstance(part, ToolReturnPart)
            ]
            requests.append(
                {
                    "returns": [str(part.content) for part in returns],
                    "prompt_chars": sum(
                        len(str(getattr(part, "content", "")))
                        for message in messages
                        for part in getattr(message, "parts", ())
                    ),
                }
            )
            step = state["step"]
            state["step"] += 1
            usage = RequestUsage(input_tokens=10, output_tokens=1)
            if step < len(sequence):
                return ModelResponse(
                    parts=[ToolCallPart("read_file", {"path": sequence[step]})],
                    usage=usage,
                )
            return ModelResponse(parts=[TextPart("answered")], usage=usage)

        return FunctionModel(respond), requests

    def run_reads(self, sequence: list[str], policy: ContextPolicy):
        model, requests = self.reads(sequence)
        runner = PydanticHarnessRunner(model_config(), model=model, context_policy=policy)
        invocation = AgentInvocation(
            uuid4(), self.repo, "read the implementation", 30, role="qa", read_only=True
        )
        return runner.run(invocation), requests


class DuplicateAndRetention(ContextCase):
    def test_repeated_identical_reads_are_elided_from_later_requests(self) -> None:
        sequence = ["big.py", "big.py", "small.txt", "big.py", "small.txt"]
        bounded, bounded_requests = self.run_reads(sequence, ContextPolicy())
        unbounded, unbounded_requests = self.run_reads(sequence, UNBOUNDED)

        # The same reads, so the comparison isolates the bound and nothing else.
        self.assertEqual(len(bounded_requests), len(unbounded_requests))
        self.assertEqual(bounded_requests[0], unbounded_requests[0])

        context = bounded.events["context"]
        self.assertGreaterEqual(context["elided_tool_results"], 2)
        self.assertEqual(context["policy"]["keep_recent_tool_results"], 2)

        # Every request from the third on carries a prior result, so every one of
        # them is materially smaller than it would have been.
        for index in range(2, len(bounded_requests)):
            self.assertLess(
                bounded_requests[index]["prompt_chars"],
                unbounded_requests[index]["prompt_chars"],
                f"request {index + 1} was not reduced by the bound",
            )
        self.assertLess(
            bounded_requests[-1]["prompt_chars"], MAX_READ_CHARS * 2,
            "the final request still carried a copy of every earlier read",
        )
        # The peak of the bounded run is below the peak of the unbounded run: the
        # bound reduces the worst request, not only the final total.
        self.assertLess(context["peak_prompt_chars"], unbounded.events["context"]["peak_prompt_chars"])
        self.assertEqual(
            context["peak_prompt_chars"],
            max(entry["prompt_chars"] for entry in context["measurements"]),
        )

    def test_an_elided_result_names_what_it_removed_and_keeps_no_content(self) -> None:
        sequence = ["big.py", "big.py", "small.txt", "big.py"]
        bounded, requests = self.run_reads(sequence, ContextPolicy(max_retained_chars=1_000))

        stubs = [
            entry
            for request in requests
            for entry in request["returns"]
            if "Result elided from this request" in entry
        ]
        self.assertTrue(stubs, "no elided result was ever shown to the model")
        stub = stubs[0]
        self.assertIn("tool: read_file", stub)
        self.assertIn("target: big.py", stub)
        self.assertIn("elided_chars: ", stub)
        self.assertIn("sha256: ", stub)
        # The content itself is gone, and the model is told how to get it back.
        self.assertNotIn(CONTENT_MARKER, stub)
        self.assertIn("call read_file again", stub)

        # Elision is never silent, never inferred as an empty result.
        self.assertIn("Nothing was hidden from you selectively", stub)
        self.assertNotIn("0 bytes", stub)

    def test_the_most_recent_results_stay_verbatim(self) -> None:
        sequence = ["small.txt", "other.py", "big.py"]
        bounded, requests = self.run_reads(
            sequence, ContextPolicy(max_retained_chars=1_000, keep_recent=1)
        )

        # The newest read is intact even though the retention bound is far smaller
        # than it, so the answer under construction keeps its own evidence.
        newest = requests[-1]["returns"][-1]
        self.assertIn(CONTENT_MARKER, newest)
        self.assertNotIn("Result elided", newest)
        self.assertGreater(bounded.events["context"]["elided_tool_results"], 0)

    def test_a_file_that_never_repeats_is_only_elided_by_the_retention_bound(self) -> None:
        sequence = ["big.py", "other.py", "small.txt"]
        bounded, requests = self.run_reads(sequence, ContextPolicy(max_retained_chars=1_000))
        context = bounded.events["context"]

        elided = bounded.events["tool_activity"]
        reasons = [entry.get("elision_reason") for entry in elided if entry.get("elided")]
        self.assertIn("retention-bound", reasons)
        self.assertNotIn("duplicate", reasons)

        # The true size survives in the audit trail; only the request was reduced.
        big = [entry for entry in elided if entry.get("target") == "big.py"][-1]
        self.assertTrue(big["elided"])
        self.assertEqual(big["elided_chars"], big["result_chars"])
        self.assertGreater(big["result_chars"], 1_000)
        self.assertGreaterEqual(
            context["elided_chars"], big["result_chars"]
        )


class EvidenceBoundary(ContextCase):
    def test_recorded_evidence_contains_no_content_and_no_arguments(self) -> None:
        bounded, _ = self.run_reads(["big.py", "big.py", "small.txt"], ContextPolicy())
        recorded = json.dumps(bounded.events, default=str)

        self.assertNotIn(CONTENT_MARKER, recorded)
        self.assertNotIn("def helper_", recorded)
        self.assertNotIn("Result elided from this request", recorded)

        # What is recorded is counts, sizes, paths, and digests.
        context = bounded.events["context"]
        self.assertEqual(
            sorted(context["measurements"][0]),
            sorted(
                [
                    "request", "prompt_chars", "messages", "tool_results",
                    "retained_tool_chars", "elided_tool_results", "projected_tokens",
                ]
            ),
        )
        for measurement in context["measurements"]:
            self.assertIsInstance(measurement["prompt_chars"], int)
            self.assertIsInstance(measurement["retained_tool_chars"], int)
            self.assertLessEqual(
                measurement["retained_tool_chars"], measurement["prompt_chars"]
            )

    def test_a_non_elidable_tool_result_is_never_replaced(self) -> None:
        model, _ = self.reads(["small.txt", "small.txt", "small.txt"])
        runner = PydanticHarnessRunner(
            model_config(),
            model=model,
            context_policy=ContextPolicy(
                max_retained_chars=0, keep_recent=0, elidable_tools=()
            ),
        )
        invocation = AgentInvocation(
            uuid4(), self.repo, "read", 30, role="qa", read_only=True
        )
        result = runner.run(invocation)
        # An empty elidable set means the bound must be inert, not aggressive.
        self.assertEqual(result.events["context"]["elided_tool_results"], 0)


class PreRequestRefusal(ContextCase):
    def test_a_request_that_cannot_fit_is_refused_before_it_is_sent(self) -> None:
        model, requests = self.reads(["big.py", "big.py", "small.txt"])
        runner = PydanticHarnessRunner(
            model_config(total_token_limit=4_000), model=model, context_policy=UNBOUNDED
        )
        invocation = AgentInvocation(
            uuid4(), self.repo, "read", 30, role="qa", read_only=True
        )

        with self.assertRaises(AgentExecutionError) as raised:
            runner.run(invocation)

        error = raised.exception
        self.assertEqual(error.reason, "resource-limit-exceeded")
        evidence = error.events["context_limit"]
        self.assertEqual(evidence["kind"], "projected-request-cannot-fit")
        self.assertGreaterEqual(evidence["projected_tokens"], evidence["remaining_tokens"])
        self.assertEqual(evidence["total_token_limit"], 4_000)
        self.assertTrue(error.events["context"]["measurements"][-1]["refused"])

        # Refused, not overshot: the provider was never asked for that request.
        self.assertLess(len(requests), 3)
        self.assertIn("was never sent", error.detail)
        self.assertNotIn(CONTENT_MARKER, json.dumps(error.events, default=str))

    def test_a_run_that_fits_is_never_refused(self) -> None:
        bounded, requests = self.run_reads(["big.py", "small.txt"], ContextPolicy())
        self.assertEqual(bounded.exit_code, 0)
        self.assertFalse(any(r.get("refused") for r in bounded.events["context"]["measurements"]))
        self.assertEqual(len(requests), 3)


class QAGateIsUnweakened(ContextCase):
    def test_the_qa_prompt_keeps_every_criterion_and_its_judgement_rules(self) -> None:
        criteria = [f"AC-{index}: observable behaviour {index}" for index in range(1, 6)]
        composed = compose_qa_prompt(
            persona="# Role: QA persona",
            task_title="Bounded context",
            requirements="Verify the change.",
            acceptance_criteria="\n".join(criteria),
            task_identifier="T-0001",
            implementation_summary="Added the endpoint.",
            implementation_files=["big.py"],
            diff_excerpt="+added line",
            validation_command=["pytest", "-q"],
            validation_exit_code=0,
            validation_output="1 passed",
            approved_criteria=[f"{item} (approved)" for item in criteria],
        )
        text = " ".join(composed.text.split())

        for item in criteria:
            self.assertIn(item, text)
        # The pre-existing obligations survive verbatim.
        self.assertIn("Do not modify any file", text)
        self.assertIn(
            "A passing validation command is necessary but not sufficient", text
        )
        self.assertIn("Report what you observed, never what you assume was run", text)
        self.assertIn("Do not rerun the command there", text)
        # A verdict is still mandatory, and gaps must be stated rather than hidden.
        self.assertIn("an absent verdict is not", text)
        self.assertIn('"verdict"', text)

    def test_read_economy_rules_do_not_authorise_skipping_evidence(self) -> None:
        lowered = QA_RULES.lower()
        for forbidden in ("skip the criteria", "omit findings", "assume it passed"):
            self.assertNotIn(forbidden, lowered)
        self.assertIn("read each file once", lowered)
        self.assertIn("still yours to cite", lowered)
        # The bound is a budget for reading, never a licence to suppress a finding.
        self.assertIn("narrow what you read, not what you judge", lowered)


if __name__ == "__main__":
    unittest.main()
