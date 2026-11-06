"""Structured-output reliability: classification, bounded correction, no guessing.

Every agent answer is a contract. These tests pin the three things that made the
real model's formatting mistakes expensive: an answer that cannot be read is
classified instead of guessed at, a *formatting* mistake gets exactly one
corrective attempt, and a decision the agent actually made — an adverse verdict, a
truncated turn, a Builder that already wrote files — is never re-asked.

The model is scripted and the container runtime is a fake, so nothing leaves the
machine; every parser, status transition, and audit record is the production one.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime_support import (  # noqa: E402
    INTAKE_ASSESSMENT,
    PROPOSAL,
    RuntimeCase,
    fenced,
)

from test_implementation_loop import (  # noqa: E402
    BUILD_REPORT,
    QA_REPORT,
    REVIEW_VERDICT,
    LoopCase,
)

from loomsphere_harness.architect_prompt import (  # noqa: E402
    ComposedPrompt,
    with_format_correction,
)
from loomsphere_harness.reports import (  # noqa: E402
    ReportError,
    parse_build_report,
    parse_qa_report,
    parse_review_verdict,
)
from loomsphere_harness.schemas.architect_proposal import (  # noqa: E402
    PAYLOAD_AMBIGUOUS,
    PAYLOAD_INVALID,
    PAYLOAD_MISSING,
    ProposalValidationError,
    parse_architect_proposal,
    select_payload,
)
from loomsphere_harness.schemas.intake import (  # noqa: E402
    IntakeReportError,
    parse_intake_assessment,
)


def fenced_qa(**overrides) -> str:
    return fenced({**QA_REPORT, **overrides})


def sequence_model(*, intake: list[str] | None = None,
                   proposal: list[str] | None = None,
                   calls: list[str] | None = None) -> FunctionModel:
    """Answer each stage from a script; the last entry repeats.

    Entries are *raw response text*, so a suite can answer with prose, with two
    fenced documents, or with a payload the contract rejects — the shapes this
    module exists to pin.
    """

    counters = {"intake": 0, "proposal": 0}
    scripts = {"intake": list(intake or []), "proposal": list(proposal or [])}

    def next_text(name: str, default: dict) -> str:
        script = scripts[name]
        if not script:
            return fenced(default)
        index = min(counters[name], len(script) - 1)
        counters[name] += 1
        return script[index]

    def respond(messages, info):
        prompt = "\n".join(
            str(getattr(part, "content", ""))
            for message in messages for part in getattr(message, "parts", ())
        )
        if calls is not None:
            calls.append(prompt)
        name = "intake" if "LoomSphere Intake agent" in prompt else "proposal"
        default = INTAKE_ASSESSMENT if name == "intake" else PROPOSAL
        return ModelResponse(parts=[TextPart(next_text(name, default))])

    return FunctionModel(respond)


class ReportClassification(unittest.TestCase):
    """An unreadable answer is classified; it is never guessed at."""

    def code(self, parser, text: str) -> str:
        with self.assertRaises(ReportError) as raised:
            parser(text)
        return raised.exception.code

    def test_a_response_with_no_payload_is_unreadable_not_invalid(self) -> None:
        for text in ("I could not finish.", "", "   "):
            self.assertEqual(self.code(parse_qa_report, text), "report-unreadable")

    def test_two_different_matching_reports_are_refused_as_ambiguous(self) -> None:
        # Two readable QA reports mean the answer never declares which one it is.
        # Ranking them would invent a decision the model did not make.
        text = f"{fenced_qa()}\n\nFor comparison:\n\n{fenced_qa(summary='Another reading.')}"
        self.assertEqual(self.code(parse_qa_report, text), "report-ambiguous")

    def test_an_example_declared_with_the_report_is_never_a_second_answer(self) -> None:
        example = {"verdict": "passed", "summary": "an example", "acceptance_criteria": []}
        text = f"{fenced(QA_REPORT)}\n\nThe contract looks like:\n\n{fenced(example)}"
        self.assertEqual(self.code(parse_qa_report, text), "report-ambiguous")

    def test_a_quoted_example_before_the_report_is_not_the_payload(self) -> None:
        quoted = fenced({"verdict": "example"})
        report = parse_review_verdict(f"{quoted}\n\nMy verdict:\n\n{fenced(REVIEW_VERDICT)}")
        self.assertEqual(report.verdict, "accepted")

    def test_an_unquoted_example_is_not_mistaken_for_the_verdict(self) -> None:
        # A prose-embedded object stays a candidate; only the declared one wins.
        report = parse_review_verdict(
            f'Shape: {{"verdict": "changes-required"}}\n\n{fenced(REVIEW_VERDICT)}'
        )
        self.assertEqual(report.verdict, "accepted")

    def test_a_non_string_field_is_a_contract_failure(self) -> None:
        # Coercing would record an answer the Agent never gave.
        self.assertEqual(
            self.code(parse_build_report, fenced({**BUILD_REPORT, "summary": 123})),
            "report-invalid",
        )
        self.assertEqual(
            self.code(
                parse_build_report,
                fenced({**BUILD_REPORT, "files": [{"path": 5, "change": "add"}]}),
            ),
            "report-invalid",
        )

    def test_a_report_must_state_its_acceptance_criteria(self) -> None:
        report = {key: value for key, value in QA_REPORT.items()
                  if key != "acceptance_criteria"}
        self.assertEqual(self.code(parse_qa_report, fenced(report)), "report-invalid")

    def test_duplicate_criteria_and_unknown_verdicts_stay_refused(self) -> None:
        twice = {**QA_REPORT,
                 "acceptance_criteria": QA_REPORT["acceptance_criteria"] * 2}
        self.assertEqual(self.code(parse_qa_report, fenced(twice)), "report-invalid")
        self.assertEqual(
            self.code(parse_qa_report, fenced({**QA_REPORT, "verdict": "lgtm"})),
            "report-invalid",
        )

    def test_the_stage_parsers_classify_their_own_refusals(self) -> None:
        missing = "I could not produce that document."
        with self.assertRaises(IntakeReportError) as intake:
            parse_intake_assessment(missing)
        self.assertEqual(intake.exception.code, PAYLOAD_MISSING)
        with self.assertRaises(ProposalValidationError) as architect:
            parse_architect_proposal(missing)
        self.assertEqual(architect.exception.code, PAYLOAD_MISSING)

        # Shape-matched but contract-breaking: a class of its own, not "missing".
        with self.assertRaises(IntakeReportError) as broken_intake:
            parse_intake_assessment(
                fenced({"intent": {"summary": "x", "request_type": "not-a-type"}})
            )
        self.assertEqual(broken_intake.exception.code, PAYLOAD_INVALID)
        with self.assertRaises(ProposalValidationError) as broken_proposal:
            parse_architect_proposal(
                fenced({"summary": "s", "components": [],
                        "acceptance_criteria_mapping": []})
            )
        self.assertEqual(broken_proposal.exception.code, PAYLOAD_INVALID)

    def test_the_shared_selector_names_ambiguity(self) -> None:
        text = f'{fenced({"intent": {"summary": "x"}})}\n'
        text += fenced({"intent": {"summary": "y"}})
        with self.assertRaises(ProposalValidationError) as raised:
            select_payload(text, role="Intake",
                           matches=lambda item: isinstance(item.get("intent"), dict))
        self.assertEqual(raised.exception.code, PAYLOAD_AMBIGUOUS)


class FormatCorrectionPrompt(unittest.TestCase):
    def test_the_correction_keeps_the_request_and_adds_only_the_complaint(self) -> None:
        original = ComposedPrompt(text="The original request.\n", digest="a" * 64,
                                  persona_digest="b" * 64)
        corrected = with_format_correction(
            original, role="QA", detail="The report is missing 'acceptance_criteria'."
        )
        self.assertTrue(corrected.text.startswith("The original request.\n"))
        self.assertIn("## Format correction", corrected.text)
        self.assertIn("missing 'acceptance_criteria'", corrected.text)
        self.assertIn("exactly one fenced json block", corrected.text)
        self.assertEqual(corrected.persona_digest, original.persona_digest)
        self.assertNotEqual(corrected.digest, original.digest)

    def test_the_correction_is_deterministic_and_bounded(self) -> None:
        original = ComposedPrompt(text="r", digest="a" * 64, persona_digest="b" * 64)
        detail = "x " * 5_000
        first = with_format_correction(original, role="Architect", detail=detail)
        second = with_format_correction(original, role="Architect", detail=detail)
        self.assertEqual(first.digest, second.digest)
        self.assertLess(len(first.text), 2_000)
        self.assertNotIn("\n\n\n", first.text)


class StageCorrection(RuntimeCase):
    """Intake and Architect: one corrective attempt, then fail closed."""

    def run_task(self, **model_options):
        calls: list[str] = []
        engine, task_id = self.submitted(
            model=sequence_model(calls=calls, **model_options)
        )
        return engine, task_id, engine.run(task_id), calls

    def events(self, view) -> list[str]:
        return [entry["event"] for entry in view["audit"]]

    def test_intake_formatting_failure_is_corrected_once(self) -> None:
        engine, task_id, view, calls = self.run_task(
            intake=["I could not produce a document.", fenced(INTAKE_ASSESSMENT)]
        )

        self.assertEqual(view["task"]["status"], "awaiting_approval")
        self.assertEqual([run["role"] for run in view["runs"]],
                         ["intake", "intake", "architect"])
        self.assertEqual(view["runs"][0]["failure_reason"], "invalid-intake")
        self.assertEqual(view["runs"][1]["status"], "succeeded")
        self.assertEqual(self.events(view).count("intake.format-retry"), 1)
        # The corrective prompt is the original request plus the complaint.
        self.assertEqual(len([call for call in calls if "Format correction" in call]), 1)
        self.assertGreater(engine.store.verify_audit_chain(), 0)

    def test_a_second_intake_failure_ends_the_stage(self) -> None:
        engine, task_id, view, _ = self.run_task(
            intake=["I could not produce a document."]
        )

        self.assertEqual(view["task"]["status"], "intake_failed")
        self.assertEqual([run["role"] for run in view["runs"]], ["intake", "intake"])
        self.assertEqual(self.events(view).count("intake.format-retry"), 1)
        self.assertEqual(view["runs"][-1]["failure_reason"], "invalid-intake")
        self.assertIsNone(view["proposal"])

    def test_architect_formatting_failure_is_corrected_once(self) -> None:
        engine, task_id, view, _ = self.run_task(
            proposal=[fenced({"summary": "only a summary"}), fenced(PROPOSAL)]
        )

        self.assertEqual(view["task"]["status"], "awaiting_approval")
        self.assertEqual([run["role"] for run in view["runs"]],
                         ["intake", "architect", "architect"])
        self.assertEqual(view["runs"][1]["failure_reason"], "invalid-proposal")
        self.assertEqual(self.events(view).count("architect.format-retry"), 1)

    def test_a_valid_answer_is_never_asked_twice(self) -> None:
        engine, task_id, view, _ = self.run_task()

        self.assertEqual([run["role"] for run in view["runs"]], ["intake", "architect"])
        self.assertEqual(
            [event for event in self.events(view) if "format-retry" in event], []
        )

    def test_a_truncated_response_is_not_retried_as_a_formatting_problem(self) -> None:
        # A truncated turn is a budget problem, not a formatting one: re-asking
        # without raising the ceiling would spend the same budget again.
        def truncated(messages, info):
            return ModelResponse(parts=[TextPart('```json\n{"intent":')],
                                 finish_reason="length")

        engine, task_id = self.submitted(model=FunctionModel(truncated))
        view = engine.run(task_id)

        self.assertEqual(view["task"]["status"], "intake_failed")
        self.assertEqual([run["role"] for run in view["runs"]], ["intake"])
        self.assertEqual(view["runs"][0]["failure_reason"], "response-truncated")
        self.assertNotIn("intake.format-retry", self.events(view))


class QaCorrection(LoopCase):
    """QA: one corrective attempt against the same recorded evidence."""

    def qa_runs(self, view) -> list[dict]:
        return [run for run in view["runs"] if run["role"] == "qa"]

    def test_a_qa_formatting_failure_is_corrected_once(self) -> None:
        engine, task_id, _ = self.approved_task(
            qa_reports=[{"summary": "I could not decide."}, QA_REPORT]
        )

        view = engine.implement(task_id)

        self.assertEqual(view["implementation"]["result"], "delivered")
        runs = self.qa_runs(view)
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["failure_reason"], "invalid-qa-report")
        self.assertEqual(runs[1]["status"], "succeeded")
        events = [entry["event"] for entry in view["audit"]]
        self.assertEqual(events.count("iteration.qa-format-retry"), 1)
        self.assertEqual(view["iterations"][0]["qa_report"]["verdict"], "passed")

    def test_a_second_qa_formatting_failure_fails_closed(self) -> None:
        engine, task_id, _ = self.approved_task(
            qa_reports=[{"summary": "I could not decide."}]
        )

        view = engine.implement(task_id)

        self.assertEqual(view["implementation"]["result"], "failed")
        self.assertEqual(view["implementation"]["reason"], "invalid-qa-report")
        self.assertEqual(view["task"]["status"], "qa_failed")
        self.assertEqual(len(self.qa_runs(view)), 2)
        self.assertEqual([run["role"] for run in view["runs"]],
                         ["intake", "architect", "builder", "qa", "qa"])
        self.assertEqual(view["iterations"][0]["status"], "qa_failed")

    def test_a_corrected_adverse_verdict_is_a_decision_not_a_format_problem(self) -> None:
        # The second answer is readable *and* adverse: it is recorded and the
        # iteration is reworked. It is never re-asked in the hope of a pass.
        engine, task_id, _ = self.approved_task(
            max_iterations=1,
            qa_reports=[{"summary": "I could not decide."},
                        {**QA_REPORT, "verdict": "failed", "findings": [
                            {"severity": "blocking", "description": "No route.",
                             "evidence": "the diff"}]}],
        )

        view = engine.implement(task_id)

        self.assertEqual(len(self.qa_runs(view)), 2)
        self.assertEqual(self.qa_runs(view)[-1]["status"], "succeeded")
        self.assertEqual(view["iterations"][0]["qa_report"]["verdict"], "failed")
        self.assertEqual([run["role"] for run in view["runs"]][-1], "qa")


class BuilderIsNeverRerun(LoopCase):
    def test_an_unreadable_build_report_never_re_invokes_the_builder(self) -> None:
        engine, task_id, _ = self.approved_task(
            build_report={"summary": "I wrote the files but cannot describe them."}
        )

        view = engine.implement(task_id)

        self.assertEqual(view["implementation"]["result"], "failed")
        self.assertEqual(view["implementation"]["reason"], "invalid-build-report")
        self.assertEqual([run["role"] for run in view["runs"]],
                         ["intake", "architect", "builder"])
        self.assertEqual(view["iterations"][0]["status"], "failed")
        self.assertIsNone(view["iterations"][0].get("qa_report"))
        self.assertEqual(view["task"]["status"], "implementation_failed")
        events = [entry["event"] for entry in view["audit"]]
        self.assertNotIn("iteration.qa-started", events)
        self.assertNotIn("iteration.validation-started", events)

    def test_no_correction_is_offered_to_a_builder_either(self) -> None:
        engine, task_id, _ = self.approved_task(
            build_report={"summary": "cannot describe it"}
        )

        view = engine.implement(task_id)

        events = [entry["event"] for entry in view["audit"]]
        self.assertEqual([event for event in events if "format-retry" in event], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
