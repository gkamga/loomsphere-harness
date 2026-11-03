"""Configuration parsing, report extraction, and prompt composition.

These tests cover the three places where the standalone runtime refuses input:
configuration (which must never carry a credential and never provision a
network), model reports (where a quoted JSON example must not become the
payload), and the composed prompts (whose text is what the model is held to).
"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from loomsphere_harness.config import (
    DEFAULT_VALIDATION_ALLOWLIST,
    ConfigError,
    SandboxConfig,
    load_config,
    parse_config,
)
from loomsphere_harness.implementation_prompt import (
    compose_builder_prompt,
    compose_qa_prompt,
    compose_reviewer_prompt,
    load_role_persona,
)
from loomsphere_harness.reports import (
    ReportError,
    missing_criteria,
    parse_build_report,
    parse_qa_report,
    parse_review_verdict,
    report_digest,
    unmet_criteria,
)

from support import TempCase


def config(**document):
    return parse_config(document, environ={"LOOMSPHERE_MODEL_API_KEY": "secret-value"})


class ConfigDefaultsTests(unittest.TestCase):
    def test_an_empty_document_declares_no_sandbox_or_workspace(self) -> None:
        runtime = config()

        self.assertIsNone(runtime.sandbox)
        self.assertIsNone(runtime.workspace_root)
        self.assertEqual(runtime.max_iterations, 2)
        self.assertEqual(runtime.validation.allowlist, DEFAULT_VALIDATION_ALLOWLIST)
        # No endpoint, model, or credential is invented.
        self.assertIsNone(runtime.model.base_url)
        self.assertIsNone(runtime.model.model_id)

    def test_an_unknown_section_or_key_is_refused(self) -> None:
        with self.assertRaises(ConfigError) as section:
            config(deployment={})
        self.assertEqual(section.exception.code, "config-invalid")

        with self.assertRaises(ConfigError) as key:
            config(sandbox={"image": "python:3.12-slim", "network_mode": "host"})
        self.assertEqual(key.exception.code, "config-invalid")

    def test_a_credential_value_in_the_file_is_refused(self) -> None:
        for section, key in (("model", "api_key"), ("model", "token"), ("sandbox", "secret")):
            with self.subTest(section=section, key=key):
                with self.assertRaises(ConfigError) as caught:
                    config(**{section: {key: "sk-live"}})
                self.assertEqual(caught.exception.code, "config-secret-in-file")
                # The refusal must not echo the value.
                self.assertNotIn("sk-live", caught.exception.message)

    def test_the_credential_comes_from_the_named_environment_variable(self) -> None:
        runtime = parse_config(
            {"model": {"api_key_env": "OTHER_KEY", "approved_hosts": ["openrouter.ai"]}},
            environ={"OTHER_KEY": "  from-environment  ", "LOOMSPHERE_MODEL_API_KEY": "x"},
        )

        self.assertEqual(runtime.model.api_key, "from-environment")
        self.assertEqual(runtime.model.approved_hosts, ("openrouter.ai",))

    def test_an_absent_credential_stays_absent(self) -> None:
        runtime = parse_config({"model": {}}, environ={})

        self.assertIsNone(runtime.model.api_key)
        self.assertTrue(runtime.model.require_api_key)


class SandboxConfigTests(unittest.TestCase):
    def test_a_container_image_is_required(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            config(sandbox={"runtime": "docker"})

        self.assertEqual(caught.exception.code, "config-invalid")
        self.assertIn("image", caught.exception.message)

    def test_a_host_runtime_is_refused(self) -> None:
        for runtime in ("host", "local", "process", "shell"):
            with self.subTest(runtime=runtime):
                with self.assertRaises(ConfigError) as caught:
                    config(sandbox={"image": "python:3.12-slim", "runtime": runtime})
                self.assertEqual(caught.exception.code, "config-invalid")

    def test_a_network_is_refused(self) -> None:
        for network in ("bridge", "host", "container:other", ""):
            with self.subTest(network=network):
                with self.assertRaises(ConfigError) as caught:
                    config(sandbox={"image": "python:3.12-slim", "network": network})
                self.assertEqual(caught.exception.code, "config-invalid")

    def test_a_declared_sandbox_keeps_every_bound(self) -> None:
        runtime = config(
            sandbox={
                "image": "python:3.12-slim",
                "cpus": 1.5,
                "memory_mb": 512,
                "pids_limit": 64,
                "tmpfs_mb": 32,
                "timeout_seconds": 120,
                "startup_grace_seconds": 5,
            }
        )

        sandbox = runtime.sandbox
        assert isinstance(sandbox, SandboxConfig)
        self.assertEqual(
            (
                sandbox.image,
                sandbox.runtime,
                sandbox.network,
                sandbox.cpus,
                sandbox.memory_mb,
                sandbox.pids_limit,
                sandbox.tmpfs_mb,
                sandbox.timeout_seconds,
                sandbox.startup_grace_seconds,
            ),
            ("python:3.12-slim", "docker", "none", 1.5, 512, 64, 32, 120, 5),
        )

    def test_a_nonsensical_bound_is_refused(self) -> None:
        for overrides in ({"cpus": 0}, {"memory_mb": 0}, {"pids_limit": -1},
                          {"cpus": "many"}, {"memory_mb": True}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ConfigError) as caught:
                    config(sandbox={"image": "python:3.12-slim", **overrides})
                self.assertEqual(caught.exception.code, "config-invalid")


class ValidationConfigTests(unittest.TestCase):
    def test_an_operator_may_narrow_the_allowlist(self) -> None:
        runtime = config(validation={"allowlist": [["python", "-m", "pytest", "-q"]],
                                     "timeout_seconds": 60, "excerpt_chars": 800})

        self.assertEqual(runtime.validation.allowlist, (("python", "-m", "pytest", "-q"),))
        self.assertEqual(runtime.validation.timeout_seconds, 60)
        self.assertEqual(runtime.validation.excerpt_chars, 800)

    def test_a_malformed_allowlist_is_refused(self) -> None:
        for raw in ("python -m pytest", [["python"], "npm"], [[]], [[1, 2]]):
            with self.subTest(raw=raw):
                with self.assertRaises(ConfigError) as caught:
                    config(validation={"allowlist": raw})
                self.assertEqual(caught.exception.code, "config-invalid")


class ConfigLoadingTests(TempCase):
    def test_a_missing_explicit_file_is_reported(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            load_config(self.tmp / "absent.toml", environ={})

        self.assertEqual(caught.exception.code, "config-missing")

    def test_a_file_is_read_and_its_source_recorded(self) -> None:
        path = self.tmp / "config.toml"
        path.write_text(
            "[implementation]\nmax_iterations = 3\n\n"
            "[sandbox]\nimage = 'python:3.12-slim'\n",
            encoding="utf-8",
        )

        runtime = load_config(path, environ={})

        self.assertEqual(runtime.source, str(path))
        self.assertEqual(runtime.max_iterations, 3)
        assert runtime.sandbox is not None
        self.assertEqual(runtime.sandbox.image, "python:3.12-slim")

    def test_an_unreadable_file_is_refused(self) -> None:
        path = self.tmp / "config.toml"
        path.write_text("this is not toml = = =\n", encoding="utf-8")

        with self.assertRaises(ConfigError) as caught:
            load_config(path, environ={})

        self.assertEqual(caught.exception.code, "config-invalid")

    def test_the_environment_can_name_the_configuration(self) -> None:
        path = self.tmp / "from-env.toml"
        path.write_text("[implementation]\nmax_iterations = 4\n", encoding="utf-8")

        with patch.dict(os.environ, {"LOOMSPHERE_HARNESS_CONFIG": str(path)}):
            runtime = load_config(None, environ={})

        self.assertEqual(runtime.max_iterations, 4)


BUILD = {
    "summary": "Added the greeting module and its test.",
    "files": [{"path": "src/greeting.py", "change": "added"},
              {"path": "tests/test_greeting.py", "change": "added"}],
    "acceptance_criteria": [{"criterion_id": "AC-1", "status": "met",
                             "evidence": "tests/test_greeting.py::test_greeting"}],
    "tests_run": ["python -m pytest -q"],
    "notes": ["No new dependencies."],
}

QA = {
    "verdict": "passed",
    "summary": "Both tests pass and the module matches the approved scope.",
    "findings": [{"severity": "minor", "description": "Constant could be named.",
                  "evidence": "src/greeting.py:1"}],
    "acceptance_criteria": [{"criterion_id": "AC-1", "status": "met",
                             "evidence": "python -m pytest -q"}],
    "residual_risks": [],
}

REVIEW = {
    "verdict": "accepted",
    "summary": "The change is correct and bounded.",
    "findings": [],
    "acceptance_criteria": [{"criterion_id": "AC-1", "status": "met",
                             "evidence": "diff"}],
    "follow_ups": ["Extend coverage to empty input."],
}


def as_json(payload: dict) -> str:
    return json.dumps(payload, indent=2)


class ReportParsingTests(unittest.TestCase):
    def test_a_build_report_is_parsed_and_digested(self) -> None:
        report = parse_build_report(as_json(BUILD))

        self.assertEqual(report.summary, BUILD["summary"])
        self.assertEqual([file.path for file in report.files],
                         ["src/greeting.py", "tests/test_greeting.py"])
        self.assertEqual([claim.criterion_id for claim in report.acceptance_criteria],
                         ["AC-1"])
        self.assertEqual(report.tests_run, ["python -m pytest -q"])
        self.assertRegex(report.digest, r"^[0-9a-f]{64}$")
        # The same payload always digests the same way.
        self.assertEqual(report.digest, parse_build_report(as_json(BUILD)).digest)

    def test_surrounding_prose_and_a_fence_are_tolerated(self) -> None:
        text = f"Here is my report.\n\n```json\n{as_json(BUILD)}\n```\n\nThanks.\n"

        self.assertEqual(parse_build_report(text).summary, BUILD["summary"])

    def test_a_quoted_example_cannot_become_the_payload(self) -> None:
        # The intended payload is complete; the example earlier in the response is
        # a different, minimal object. Extraction must keep the intended one.
        example = json.dumps({"summary": "example only", "files": []})
        text = (
            "I will follow this shape:\n\n"
            f"```json\n{example}\n```\n\n"
            "My actual report is:\n\n"
            f"```json\n{as_json(BUILD)}\n```\n"
        )

        report = parse_build_report(text)

        self.assertEqual(report.summary, BUILD["summary"])
        self.assertNotEqual(report.digest, report_digest({"summary": "example only",
                                                          "files": []}))

    def test_an_inline_quoted_example_is_not_taken_as_the_payload(self) -> None:
        text = (
            'The format is {"summary": "example", "files": []} and mine is:\n\n'
            f"{as_json(BUILD)}\n"
        )

        self.assertEqual(parse_build_report(text).summary, BUILD["summary"])

    def test_a_response_without_a_json_object_is_unreadable(self) -> None:
        for text in ("I could not finish.", "", "[]", "null"):
            with self.subTest(text=text):
                with self.assertRaises(ReportError) as caught:
                    parse_build_report(text)
                self.assertEqual(caught.exception.code, "report-unreadable")

    def test_a_missing_required_field_is_refused(self) -> None:
        broken = dict(BUILD)
        del broken["summary"]

        with self.assertRaises(ReportError) as caught:
            parse_build_report(as_json(broken))

        self.assertEqual(caught.exception.code, "report-invalid")

    def test_a_path_that_escapes_the_worktree_is_refused(self) -> None:
        broken = dict(BUILD, files=[{"path": "../outside.py", "change": "added"}])

        with self.assertRaises(ReportError) as caught:
            parse_build_report(as_json(broken))

        self.assertEqual(caught.exception.code, "report-invalid")

    def test_a_qa_report_keeps_its_verdict_and_findings(self) -> None:
        report = parse_qa_report(as_json(QA))

        self.assertEqual(report.verdict, "passed")
        self.assertEqual([finding.severity for finding in report.findings], ["minor"])
        self.assertEqual(report.findings[0].description, "Constant could be named.")
        self.assertEqual(report.residual_risks, [])

    def test_an_unknown_verdict_is_refused(self) -> None:
        for verdict in ("ok", "lgtm", ""):
            with self.subTest(verdict=verdict):
                with self.assertRaises(ReportError) as caught:
                    parse_qa_report(as_json(dict(QA, verdict=verdict)))
                self.assertEqual(caught.exception.code, "report-invalid")

    def test_a_review_verdict_is_parsed(self) -> None:
        review = parse_review_verdict(as_json(REVIEW))

        self.assertEqual(review.verdict, "accepted")
        self.assertEqual(review.follow_ups, ["Extend coverage to empty input."])

    def test_a_review_verdict_must_be_one_of_the_three(self) -> None:
        for verdict in ("approve", "merged", ""):
            with self.subTest(verdict=verdict):
                with self.assertRaises(ReportError) as caught:
                    parse_review_verdict(as_json(dict(REVIEW, verdict=verdict)))
                self.assertEqual(caught.exception.code, "report-invalid")


class ContractCoverageTests(unittest.TestCase):
    def test_an_unmentioned_criterion_is_missing(self) -> None:
        qa = parse_qa_report(as_json(QA))

        self.assertEqual(missing_criteria(qa.acceptance_criteria, ["AC-1"]), [])
        self.assertEqual(missing_criteria(qa.acceptance_criteria, ["AC-1", "AC-2"]),
                         ["AC-2"])
        self.assertEqual(missing_criteria([], ["AC-1"]), ["AC-1"])

    def test_only_a_met_claim_counts_as_satisfied(self) -> None:
        claims = parse_build_report(
            as_json(dict(BUILD, acceptance_criteria=[
                {"criterion_id": "AC-1", "status": "met"},
                {"criterion_id": "AC-2", "status": "unmet"},
                {"criterion_id": "AC-3", "status": "unknown"},
            ]))
        ).acceptance_criteria

        self.assertEqual(unmet_criteria(claims), ["AC-2", "AC-3"])


class PromptCompositionTests(unittest.TestCase):
    persona = "You are the Builder for this attempt."

    def builder(self, **overrides):
        arguments = dict(
            persona=self.persona,
            task_title="Add a greeting module",
            requirements="Return 'hello' from a function.",
            acceptance_criteria="AC-1: greet() returns 'hello'.",
            task_identifier="LOOM-1",
            base_commit="a" * 40,
            iteration=1,
            max_iterations=2,
            approved_criteria=["AC-1: greet() returns 'hello'."],
        )
        arguments.update(overrides)
        return compose_builder_prompt(**arguments)

    def test_the_builder_prompt_carries_the_frozen_scope(self) -> None:
        prompt = self.builder()

        self.assertIn(self.persona, prompt.text)
        self.assertIn("## Approved scope", prompt.text)
        self.assertIn("Task: Add a greeting module", prompt.text)
        self.assertIn("Task identifier: LOOM-1", prompt.text)
        self.assertIn("Iteration 1 of at most 2.", prompt.text)
        self.assertIn("a" * 40, prompt.text)
        self.assertIn("## Approved acceptance contract", prompt.text)
        self.assertIn("- AC-1: greet() returns 'hello'.", prompt.text)
        self.assertIn("first implementation attempt", prompt.text)
        self.assertTrue(prompt.text.endswith("\n"))
        self.assertRegex(prompt.digest, r"^[0-9a-f]{64}$")
        self.assertRegex(prompt.persona_digest, r"^[0-9a-f]{64}$")

    def test_feedback_and_iteration_change_the_digest(self) -> None:
        first = self.builder()
        second = self.builder(iteration=2, previous_feedback=["The test is missing."])

        self.assertNotEqual(first.digest, second.digest)
        self.assertIn("The test is missing.", second.text)
        self.assertIn("Iteration 2 of at most 2.", second.text)
        self.assertEqual(first.persona_digest, second.persona_digest)
        self.assertEqual(first.digest, self.builder().digest)

    def test_an_absent_acceptance_contract_is_stated_not_invented(self) -> None:
        prompt = self.builder(approved_criteria=[])

        self.assertNotIn("## Approved acceptance contract", prompt.text)
        self.assertIn("Acceptance criteria:\nAC-1: greet() returns 'hello'.", prompt.text)

    def test_the_qa_and_reviewer_prompts_name_their_own_role(self) -> None:
        qa = compose_qa_prompt(
            persona="You are QA.",
            task_title="Add a greeting module",
            requirements="Return 'hello' from a function.",
            acceptance_criteria="AC-1: greet() returns 'hello'.",
            task_identifier="LOOM-1",
            implementation_summary="Added greet().",
            implementation_files=["src/greeting.py"],
            diff_excerpt="+def greet():\n+    return 'hello'\n",
            validation_command=["python", "-m", "pytest", "-q"],
            validation_exit_code=0,
            validation_output="1 passed",
            approved_criteria=["AC-1"],
        )
        review = compose_reviewer_prompt(
            persona="You are the reviewer.",
            task_title="Add a greeting module",
            requirements="Return 'hello' from a function.",
            acceptance_criteria="AC-1: greet() returns 'hello'.",
            task_identifier="LOOM-1",
            implementation_summary="Added greet().",
            diff_excerpt="+def greet():\n+    return 'hello'\n",
            validation_command=["python", "-m", "pytest", "-q"],
            validation_exit_code=0,
            qa_verdict="passed",
            qa_summary="Tests pass.",
            qa_findings=["minor: constant could be named"],
            approved_criteria=["AC-1"],
        )

        self.assertIn("You are QA.", qa.text)
        self.assertIn("You are the reviewer.", review.text)
        self.assertIn("Task identifier: LOOM-1", qa.text)
        self.assertIn("Task identifier: LOOM-1", review.text)
        # QA sees the executed validation; the reviewer sees QA's verdict.
        self.assertIn("Exit code: 0", qa.text)
        self.assertIn("1 passed", qa.text)
        self.assertIn("Verdict: passed", review.text)
        self.assertIn("- minor: constant could be named", review.text)
        self.assertNotEqual(qa.digest, review.digest)

    def test_a_persona_file_is_read_in_full(self) -> None:
        with self.assertRaises(Exception):
            load_role_persona("", role="Builder")


if __name__ == "__main__":
    unittest.main()
