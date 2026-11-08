"""Bounded model context: QA-only retention, stub fidelity, honest projection.

A PydanticAI run re-sends the whole message history on every request, so a tool
result is paid for once per remaining request. A reference QA run recorded 69,624
tokens against a 60,000-token limit in four requests without answering, and an
offline replay of seven representative capped reads estimates 183,573 input
tokens at a 150,000-token ceiling by characters/4. These tests pin the correction
down without a network, a provider, or wall-clock dependence: they assert a bound
exists for read-only QA, that it names what it removed, that it never touches
evidence the harness must keep verbatim, that no other role is affected, and that
the token projection is recorded rather than enforced.

They also assert the bound cannot be used to weaken QA: elision keeps the tool, the
target, the true size, and a digest, the newest evidence stays verbatim, an elided
section can be reread narrowly, and the recorded audit trail still reports what
each tool actually returned.
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
from loomsphere_harness.reports import parse_qa_report  # noqa: E402

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

    def run_reads(
        self,
        sequence: list[str],
        policy: ContextPolicy,
        *,
        role: str = "qa",
        read_only: bool = True,
        config=None,
    ):
        model, requests = self.reads(sequence)
        runner = PydanticHarnessRunner(
            config or model_config(), model=model, context_policy=policy
        )
        invocation = AgentInvocation(
            uuid4(), self.repo, "read the implementation", 30,
            role=role, read_only=read_only,
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
                    "remaining_tokens", "projection_exceeds_remaining",
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


class BudgetProjectionIsDiagnostic(ContextCase):
    """The next-request projection is recorded, never enforced.

    A character heuristic cannot prove that a request will not fit any provider's
    tokenizer, so refusing on it could spend a QA task's one-time authorization on a
    false positive. ``UsageLimits`` remains the only enforcement, and it is checked
    after a response, so an overshoot inside one response is still possible.
    """

    def test_a_conservative_projection_does_not_refuse_a_request(self) -> None:
        model, requests = self.reads(["big.py", "big.py", "small.txt"])
        runner = PydanticHarnessRunner(
            model_config(total_token_limit=4_000), model=model, context_policy=UNBOUNDED
        )
        invocation = AgentInvocation(
            uuid4(), self.repo, "read", 30, role="qa", read_only=True
        )

        result = runner.run(invocation)

        # Every request was sent, including the ones the estimate flags.
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(requests), 4)
        context = result.events["context"]
        measurements = context["measurements"]
        self.assertTrue(
            any(entry["projection_exceeds_remaining"] for entry in measurements),
            "the fixture did not exercise a projection above the remaining budget",
        )
        self.assertFalse(any("refused" in entry for entry in measurements))
        self.assertEqual(context["policy"]["projection_enforcement"], "diagnostic-only")

    def test_highly_compressible_input_is_not_refused_by_a_character_estimate(
        self,
    ) -> None:
        # 20,000 spaces: 20,000 characters, but a real tokenizer compresses long
        # runs to a few hundred tokens. A character-based refusal would trip here
        # while the run had spent almost nothing.
        (self.repo / "spaces.txt").write_text(" " * 20_000, encoding="utf-8")
        model, requests = self.reads(["spaces.txt", "spaces.txt"])
        runner = PydanticHarnessRunner(
            model_config(total_token_limit=500), model=model, context_policy=UNBOUNDED
        )
        invocation = AgentInvocation(
            uuid4(), self.repo, "read", 30, role="qa", read_only=True
        )

        result = runner.run(invocation)

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(requests), 3)
        flagged = [
            entry
            for entry in result.events["context"]["measurements"]
            if entry["projection_exceeds_remaining"]
        ]
        self.assertTrue(flagged, "the estimate did not record the projected overrun")
        # The projection is far above what the run actually recorded. That gap is
        # exactly why it may be reported but may not refuse a request.
        self.assertGreater(
            flagged[-1]["projected_tokens"], result.events["usage"]["total_tokens"]
        )

    def test_the_official_usage_limit_remains_the_only_enforcement(self) -> None:
        def respond(messages, info):
            returns = [
                part
                for message in messages
                for part in getattr(message, "parts", ())
                if isinstance(part, ToolReturnPart)
            ]
            usage = RequestUsage(input_tokens=500, output_tokens=1)
            if returns:
                return ModelResponse(parts=[TextPart("answered")], usage=usage)
            return ModelResponse(
                parts=[ToolCallPart("read_file", {"path": "small.txt"})], usage=usage
            )

        runner = PydanticHarnessRunner(
            model_config(total_token_limit=100),
            model=FunctionModel(respond),
            context_policy=ContextPolicy(),
        )
        invocation = AgentInvocation(
            uuid4(), self.repo, "read", 30, role="qa", read_only=True
        )

        with self.assertRaises(AgentExecutionError) as raised:
            runner.run(invocation)

        error = raised.exception
        self.assertEqual(error.reason, "resource-limit-exceeded")
        self.assertIn("A run limit was reached", error.detail)
        # The ceiling is checked after the response, so the run can be stopped only
        # once it has already spent past it. Nothing here claims overspend is gone.
        self.assertGreater(error.events["usage"]["total_tokens"], 100)


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
        # The economy rules are about which evidence to fetch, never about what may
        # be judged: supplied evidence comes first, duplicate reads add nothing, and
        # a stub means the text is no longer in context.
        self.assertIn("evidence already supplied in this prompt", lowered)
        self.assertIn("avoid duplicate reads", lowered)
        self.assertIn("no longer present in your active context", lowered)
        self.assertIn("never claim to know, quote, or assess content", lowered)
        # A targeted reread is allowed when a finding needs it.
        self.assertIn("reread only the narrowly targeted section", lowered)
        # Every criterion still needs a justified assessment, and a verdict is
        # mandatory: stating a gap is acceptable, dropping the judgement is not.
        self.assertIn("every approved acceptance criterion must still receive", lowered)
        self.assertIn("if you cannot inspect its evidence, state that gap", lowered)
        self.assertIn("an absent verdict is not", lowered)


class RoleScoping(ContextCase):
    """The bound is a read-only QA measure and changes no other role's history.

    A Builder, Reviewer, Architect, or Intake run must never have its evidence
    elided, its prompt shrunk, or its request judged by the QA context policy.
    """

    NON_QA_ROLES = ("builder", "reviewer", "architect", "intake")

    def test_non_qa_roles_send_exactly_what_they_sent_before(self) -> None:
        sequence = ["big.py", "big.py", "small.txt", "big.py"]
        reference, reference_requests = self.run_reads(sequence, UNBOUNDED, role="builder")
        aggressive = ContextPolicy(max_retained_chars=1_000, keep_recent=1)

        for role in self.NON_QA_ROLES:
            with self.subTest(role=role):
                result, requests = self.run_reads(sequence, aggressive, role=role)
                self.assertEqual(result.exit_code, 0)
                # Byte-for-byte the same requests as the policy-free baseline.
                self.assertEqual(requests, reference_requests)
                self.assertIsNone(result.events.get("context"))
                # The recorded limits are identical apart from the role name.
                self.assertEqual(
                    {key: value for key, value in result.events["limits"].items()
                     if key != "role"},
                    {key: value for key, value in reference.events["limits"].items()
                     if key != "role"},
                )

    def test_a_writable_invocation_is_never_bounded(self) -> None:
        sequence = ["big.py", "big.py", "small.txt"]
        result, requests = self.run_reads(
            sequence, ContextPolicy(max_retained_chars=1_000), role="qa", read_only=False
        )

        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(result.events.get("context"))
        for request in requests:
            for entry in request["returns"]:
                self.assertNotIn("Result elided from this request", entry)

    def test_the_same_sequence_is_bounded_for_qa_only(self) -> None:
        sequence = ["big.py", "big.py", "small.txt", "big.py"]
        aggressive = ContextPolicy(max_retained_chars=1_000, keep_recent=1)

        bounded, bounded_requests = self.run_reads(sequence, aggressive, role="qa")
        unbounded, unbounded_requests = self.run_reads(
            sequence, aggressive, role="reviewer"
        )

        self.assertGreaterEqual(bounded.events["context"]["elided_tool_results"], 1)
        self.assertIsNone(unbounded.events.get("context"))
        self.assertLess(
            bounded_requests[-1]["prompt_chars"],
            unbounded_requests[-1]["prompt_chars"],
        )


class ElisionRecovery(ContextCase):
    """A QA model that meets a stub, rereads narrowly, and only then judges."""

    def test_qa_rereads_elided_evidence_before_reaching_a_verdict(self) -> None:
        criteria = [f"AC-{index}" for index in range(1, 6)]
        calls: list[dict[str, object]] = []
        requests: list[list[str]] = []
        state = {"step": 0}

        def read(path: str, **extra) -> ModelResponse:
            calls.append({"path": path, **extra})
            args: dict[str, object] = {"path": path}
            args.update(extra)
            return ModelResponse(
                parts=[ToolCallPart("read_file", args)],
                usage=RequestUsage(input_tokens=10, output_tokens=1),
            )

        def respond(messages, info):
            returns = [
                part
                for message in messages
                for part in getattr(message, "parts", ())
                if isinstance(part, ToolReturnPart)
            ]
            requests.append([str(part.content) for part in returns])
            step = state["step"]
            state["step"] += 1
            if step == 0:
                return read("big.py")
            if step == 1:
                return read("other.py")
            if step == 2:
                return read("small.txt")
            if step == 3:
                # The stub for the first read is in the history now. The model does
                # what the rules require: a narrow reread, not a memory claim.
                return read("big.py", offset=0, limit=5)
            report = {
                "verdict": "failed",
                "summary": "The first criterion is unmet.",
                "findings": [
                    {
                        "severity": "blocking",
                        "description": "The required branch is missing.",
                        "evidence": "big.py lines 1-5",
                    }
                ],
                "acceptance_criteria": [
                    {
                        "criterion_id": criterion,
                        "criterion": f"criterion {criterion}",
                        "status": "unmet" if criterion == "AC-1" else "unknown",
                        "evidence": "big.py lines 1-5",
                    }
                    for criterion in criteria
                ],
                "residual_risks": [],
            }
            return ModelResponse(
                parts=[TextPart(f"```json\n{json.dumps(report)}\n```")],
                usage=RequestUsage(input_tokens=10, output_tokens=1),
            )

        runner = PydanticHarnessRunner(
            model_config(),
            model=FunctionModel(respond),
            context_policy=ContextPolicy(max_retained_chars=1_000, keep_recent=1),
        )
        invocation = AgentInvocation(
            uuid4(), self.repo, "Assess " + ", ".join(criteria), 30,
            role="qa", read_only=True,
        )

        result = runner.run(invocation)

        stubbed = [
            index
            for index, returns in enumerate(requests)
            if any("Result elided from this request" in entry for entry in returns)
        ]
        self.assertTrue(stubbed, "the model never encountered an elision stub")
        self.assertGreaterEqual(len(requests), 5, "the run ended before the reread")
        # The stub was visible before the reread was requested, so the recovery was
        # a response to it and not a coincidence of ordering.
        self.assertLessEqual(stubbed[0], len(requests) - 2)

        # The reread happened last, and it was narrow, not a full re-read.
        self.assertEqual(calls[-1]["path"], "big.py")
        self.assertEqual(calls[-1]["limit"], 5)
        # The verdict request carries both the stub and the restored section, so
        # the model judged with the content in view and the history honest.
        final = requests[-1]
        self.assertTrue(
            any(CONTENT_MARKER in entry for entry in final),
            "the targeted reread did not restore the elided content",
        )
        self.assertTrue(
            any("Result elided from this request" in entry for entry in final),
            "the stub was hidden rather than kept as the historical marker",
        )

        # The verdict arrived, and every criterion still carries an assessment.
        report_parsed = parse_qa_report(result.response_text)
        self.assertEqual(report_parsed.verdict, "failed")
        self.assertEqual(
            [claim.criterion_id for claim in report_parsed.acceptance_criteria],
            criteria,
        )
        for claim in report_parsed.acceptance_criteria:
            self.assertTrue(
                claim.evidence, f"{claim.criterion_id} was assessed without justification"
            )
        self.assertGreaterEqual(result.events["context"]["elided_tool_results"], 1)


if __name__ == "__main__":
    unittest.main()
