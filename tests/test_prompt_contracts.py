"""Prompt contracts must match what the runtime parses and can actually verify."""

from __future__ import annotations

import unittest

from loomsphere_harness.architect_prompt import RESPONSE_CONTRACT, compose_architect_prompt
from loomsphere_harness.implementation_prompt import (
    QA_CONTRACT,
    REVIEWER_CONTRACT,
    SEVERITY_GUIDE,
)
from loomsphere_harness.reports import (
    SEVERITIES,
    ReportError,
    parse_qa_report,
    parse_review_verdict,
)
from loomsphere_harness.schemas.acceptance_contract import AcceptanceVerification


def report(severity: str, verdict: str = "accepted") -> str:
    """A report that satisfies every required field, so only severity can fail."""

    return (
        "```json\n"
        '{"verdict": "%s", "summary": "s", "findings": '
        '[{"severity": "%s", "description": "d"}], "acceptance_criteria": [],'
        ' "residual_risks": [], "follow_ups": []}\n'
        "```"
    ) % (verdict, severity)


class SeverityContract(unittest.TestCase):
    def test_reviewer_and_qa_prompts_enumerate_every_valid_severity(self) -> None:
        for contract in (REVIEWER_CONTRACT, QA_CONTRACT):
            self.assertIn(SEVERITY_GUIDE, contract)
        for severity in SEVERITIES:
            self.assertIn(f"`{severity}`:", SEVERITY_GUIDE)

    def test_the_guide_forbids_info_and_explains_each_meaning(self) -> None:
        self.assertIn("never as `info`", SEVERITY_GUIDE)
        self.assertEqual(SEVERITIES, {"blocking", "major", "minor"})
        for line in SEVERITY_GUIDE.splitlines():
            if line.startswith("- `"):
                self.assertGreater(len(line.split(":", 1)[1].strip()), 20)

    def test_the_parser_stays_strict_about_unknown_severities(self) -> None:
        for severity in ("info", "critical", ""):
            with self.assertRaises(ReportError) as raised:
                parse_review_verdict(report(severity))
            self.assertIn("unknown severity", raised.exception.message)
            with self.assertRaises(ReportError):
                parse_qa_report(report(severity, verdict="passed"))

    def test_every_documented_severity_parses(self) -> None:
        for severity in SEVERITIES:
            self.assertEqual(parse_review_verdict(report(severity)).findings[0].severity, severity)


class StandaloneArchitectGuidance(unittest.TestCase):
    def compose(self, **options) -> str:
        return compose_architect_prompt(
            persona="persona",
            task_title="t",
            requirements="r",
            acceptance_criteria="a",
            repository_reference="main",
            repository_commit="a" * 40,
            task_identifier="T-0001",
            **options,
        ).text

    def test_the_standalone_architect_is_not_offered_protected_artifacts(self) -> None:
        text = self.compose()
        self.assertIn('"kind": "pytest"', text)
        self.assertNotIn('"kind": "protected-pytest"', text)
        self.assertNotIn('"artifact"', RESPONSE_CONTRACT)
        self.assertNotIn("trusted-artifact-key", text)
        self.assertIn("never emit ``protected-pytest``", text)

    def test_the_advertised_example_is_a_valid_verification(self) -> None:
        verification = AcceptanceVerification.model_validate(
            {"kind": "pytest", "selector": "tests/test_file.py::test_case"}
        )
        self.assertEqual(verification.kind, "pytest")

    def test_protected_pytest_validation_is_unchanged(self) -> None:
        with self.assertRaises(Exception):
            AcceptanceVerification.model_validate({"kind": "protected-pytest", "selector": "t"})
        pinned = AcceptanceVerification.model_validate(
            {
                "kind": "protected-pytest",
                "artifact": "slug",
                "artifact_version": 1,
                "artifact_digest": "a" * 64,
                "selector": "test_case",
            }
        )
        self.assertEqual(pinned.kind, "protected-pytest")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
