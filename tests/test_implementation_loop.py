"""The implementation loop, driven by a scripted model and a fake sandbox runtime.

These tests exercise the real loop: a private worktree from the approved commit, a
writable Builder invocation, one commit, the change-set gate, executed validation
through the sandbox executor, QA, Reviewer, and the frozen deliverable a human
decides on. The model is scripted and the container runtime is a fake, so nothing
leaves the machine and no container is created — but every gate, status
transition, and recorded artefact is the production one.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from subprocess import CompletedProcess

from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime_support import (  # noqa: E402
    INTAKE_ASSESSMENT,
    PROPOSAL,
    RuntimeCase,
    fenced,
)

from loomsphere_harness.cli import render_recovery  # noqa: E402
from loomsphere_harness.config import SandboxConfig, ValidationConfig  # noqa: E402
from loomsphere_harness.engine import Engine, EngineError  # noqa: E402
from loomsphere_harness.pydantic_harness import PydanticHarnessRunner  # noqa: E402
from loomsphere_harness.repository import GitRepository, OsUserIdentity  # noqa: E402
from loomsphere_harness.sandbox import SandboxExecutor, SandboxPolicy  # noqa: E402
from loomsphere_harness.store import SqliteStore, StoreError  # noqa: E402

IMAGE_ID = "sha256:" + "b" * 64
CRITERION_ID = "AC-1"
CRITERION_TEXT = "GET /hello returns 200"

BUILD_REPORT = {
    "summary": "Added the /hello route with an automated test.",
    "files": [{"path": "tests/test_hello.py", "change": "add"}],
    "acceptance_criteria": [
        {"criterion_id": CRITERION_ID, "criterion": CRITERION_TEXT,
         "status": "met", "evidence": "tests/test_hello.py::test_hello"},
    ],
    "tests_run": ["python -m pytest -q (executed by the harness sandbox)"],
    "notes": [],
}
QA_REPORT = {
    "verdict": "passed",
    "summary": "The diff adds the route and the recorded pytest run passed.",
    "findings": [],
    "acceptance_criteria": [
        {"criterion_id": CRITERION_ID, "criterion": CRITERION_TEXT,
         "status": "met", "evidence": "recorded pytest exit 0"},
    ],
    "residual_risks": [],
}
REVIEW_VERDICT = {
    "verdict": "accepted",
    "summary": "The change set is minimal and the evidence belongs to this commit.",
    "findings": [],
    "acceptance_criteria": [
        {"criterion_id": CRITERION_ID, "criterion": CRITERION_TEXT,
         "status": "met", "evidence": "recorded pytest exit 0"},
    ],
    "follow_ups": [],
}
QA_FAILED = {
    "verdict": "failed",
    "summary": "The recorded run cannot be relied on: the diff has no assertion.",
    "findings": [
        {"severity": "blocking", "description": "The test asserts nothing.",
         "evidence": "tests/test_hello.py::test_hello"},
    ],
    "acceptance_criteria": [
        {"criterion_id": CRITERION_ID, "criterion": CRITERION_TEXT,
         "status": "unmet", "evidence": "no observed assertion"},
    ],
    "residual_risks": [],
}
QA_OMITS_CRITERION = {
    "verdict": "passed",
    "summary": "Looks fine as far as I checked.",
    "findings": [],
    "acceptance_criteria": [],
    "residual_risks": [],
}
REVIEW_REJECTED = {
    "verdict": "rejected",
    "summary": "The change set cannot satisfy the requirement: the route is absent.",
    "findings": [
        {"severity": "blocking", "description": "No route was added.",
         "evidence": "the diff adds only a test"},
    ],
    "acceptance_criteria": [
        {"criterion_id": CRITERION_ID, "criterion": CRITERION_TEXT,
         "status": "unmet", "evidence": "no route in the diff"},
    ],
    "follow_ups": [],
}

REVIEW_INFO_SEVERITY = {
    **REVIEW_VERDICT,
    "findings": [
        {"severity": "info", "description": "A purely informational note.",
         "evidence": "the diff"},
    ],
}
REVIEW_REJECTED_INFO_SEVERITY = {**REVIEW_REJECTED, "findings": REVIEW_INFO_SEVERITY["findings"]}
REVIEW_CHANGES_REQUIRED = {
    "verdict": "changes-required",
    "summary": "The change set needs one more pass before a human sees it.",
    "findings": [
        {"severity": "blocking", "description": "The test covers nothing yet.",
         "evidence": "the diff adds only a placeholder"},
    ],
    "acceptance_criteria": [
        {"criterion_id": CRITERION_ID, "criterion": CRITERION_TEXT,
         "status": "met", "evidence": "recorded pytest exit 0"},
    ],
    "follow_ups": ["Cover the route."],
}

BUILT_FILES = {
    "tests/test_hello.py": (
        '"""Automated test for the /hello route."""\n\n\n'
        "def test_hello() -> None:\n"
        "    assert True\n"
    ),
}


def out_of_budget(message: str = "the run reached its total token budget") -> Exception:
    """The failure the real Demandro Builder hit after writing five files."""

    return UsageLimitExceeded(message)


def fake_runtime(
    *,
    exit_code: int = 0,
    selector_collection_exit_code: int = 0,
    selector_exit_code: int = 0,
    output: str = "1 passed\n",
    calls: list[list[str]] | None = None,
):
    """A stand-in container runtime: it records, it never launches anything."""

    def runner(arguments, *, capture_output=True, text=True, timeout=0, check=False):
        if calls is not None:
            calls.append(list(arguments))
        if arguments[1:3] == ["image", "inspect"]:
            return CompletedProcess(arguments, 0, f"{IMAGE_ID}\n", "")
        command = arguments[arguments.index("python:3.12-slim") + 1 :]
        if "--collect-only" in command:
            return CompletedProcess(
                arguments, selector_collection_exit_code, output, ""
            )
        if command[:4] == ["python", "-m", "pytest", "--version"]:
            return CompletedProcess(arguments, 0, output, "")
        if len(command) > 4 and command[:3] == ["python", "-m", "pytest"]:
            return CompletedProcess(arguments, selector_exit_code, output, "")
        return CompletedProcess(arguments, exit_code, output, "")

    return runner


def loop_model(
    *,
    worktree: Path,
    progress: dict[str, int] | None = None,
    build_report: dict | None = None,
    qa_reports: list[dict] | None = None,
    review_verdicts: list[dict] | None = None,
    unreported_files: dict[str, str] | None = None,
    proposal: dict | None = None,
    builder_failures: dict[int, Exception] | None = None,
) -> FunctionModel:
    """Answer as Intake, Architect, Builder, QA, and Reviewer, in that order.

    The Builder answer writes the file a real Builder would have written — and only
    changes it a little each attempt, so a rework iteration still produces a real
    commit. The QA and Reviewer answers are consumed in sequence, so a suite can
    script a rework and the attempt that follows it. The `progress` counters are
    owned by the caller because the runtime builds a fresh model per agent run.

    `builder_failures` maps a Builder attempt number to the exception that attempt
    raises *after* writing its files, which is how a run that runs out of budget
    leaves usable work behind. Because the attempt counter lives in `progress`, the
    script survives the runtime building a new model for every run.
    """

    counts = progress if progress is not None else {"builder": 0, "qa": 0, "review": 0}
    qa_sequence = list(qa_reports or [QA_REPORT])
    review_sequence = list(review_verdicts or [REVIEW_VERDICT])
    failures = dict(builder_failures or {})

    def next_report(sequence: list[dict], name: str) -> dict:
        index = min(counts[name], len(sequence) - 1)
        counts[name] += 1
        return sequence[index]

    def respond(messages, info):
        prompt = "\n".join(
            str(getattr(part, "content", ""))
            for message in messages for part in getattr(message, "parts", ())
        )
        if "LoomSphere Intake agent" in prompt:
            return ModelResponse(parts=[TextPart(fenced(INTAKE_ASSESSMENT))])
        if "# Role: Builder" in prompt:
            counts["builder"] += 1
            files = dict(BUILT_FILES)
            files["tests/test_hello.py"] += f"# attempt {counts['builder']}\n"
            files.update(unreported_files or {})
            for relative, content in files.items():
                target = Path(worktree) / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            failure = failures.get(counts["builder"])
            if failure is not None:
                # The files are on disk; the run never gets to answer.
                raise failure
            return ModelResponse(parts=[TextPart(fenced(build_report or BUILD_REPORT))])
        if "# Role: QA" in prompt:
            return ModelResponse(parts=[TextPart(fenced(next_report(qa_sequence, "qa")))])
        if "# Role: Reviewer" in prompt:
            verdict = next_report(review_sequence, "review")
            return ModelResponse(parts=[TextPart(fenced(verdict))])
        return ModelResponse(parts=[TextPart(fenced(proposal or PROPOSAL))])

    return FunctionModel(respond)


class LoopCase(RuntimeCase):
    def loop_engine(
        self,
        *,
        sandbox: bool = True,
        validation_exit_code: int = 0,
        selector_collection_exit_code: int = 0,
        selector_exit_code: int = 0,
        max_iterations: int = 2,
        **model_options,
    ):
        config = self.config()
        if sandbox:
            config = replace(
                config,
                sandbox=SandboxConfig(image="python:3.12-slim"),
                validation=ValidationConfig(),
                max_iterations=max_iterations,
            )
        else:
            config = replace(config, max_iterations=max_iterations)

        calls: list[list[str]] = []
        executor = SandboxExecutor(
            SandboxPolicy(
                image="python:3.12-slim",
                timeout_seconds=60,
                cpus=1.0,
                memory_mb=512,
                pids_limit=64,
                tmpfs_mb=64,
            ),
            runtime="docker",
            startup_grace_seconds=5,
            runner=fake_runtime(
                exit_code=validation_exit_code,
                selector_collection_exit_code=selector_collection_exit_code,
                selector_exit_code=selector_exit_code,
                calls=calls,
            ),
        )

        state: dict[str, Path] = {"worktree": Path("/dev/null")}
        # One counter set per engine: the runtime builds a fresh model for every
        # agent run, so the scripted answers must live outside the factory.
        progress: dict[str, int] = {"builder": 0, "qa": 0, "review": 0}

        def factory(runner_config):
            return PydanticHarnessRunner(
                runner_config,
                model=loop_model(
                    worktree=state["worktree"], progress=progress, **model_options
                ),
            )

        engine = Engine(
            store=SqliteStore(self.state),
            config=config,
            repository=GitRepository(self.repo),
            identity=OsUserIdentity("alice"),
            repo_path=self.repo,
            runner_factory=factory,
            sandbox_executor=executor,
        )
        return engine, state, calls

    def approved_task(
        self,
        *,
        sandbox: bool = True,
        validation_exit_code: int = 0,
        max_iterations: int = 2,
        **model_options,
    ):
        engine, state, calls = self.loop_engine(
            sandbox=sandbox,
            validation_exit_code=validation_exit_code,
            max_iterations=max_iterations,
            **model_options,
        )
        engine.initialize()
        task = engine.submit(
            title="Add GET /hello",
            requirements="Add GET /hello.",
            acceptance_criteria="GET /hello returns 200.",
        )
        task_id = task["id"]
        state["worktree"] = engine.workspace_root / f"task-{task_id.lower()}"
        view = engine.run(task_id)
        self.assertEqual(view["task"]["status"], "awaiting_approval")
        engine.approve(task_id, proposal_digest=view["proposal"]["digest"])
        return engine, task_id, calls

    def head_of(self, repo: Path) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip()

    def porcelain(self, repo: Path) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout


class ImplementationLoop(LoopCase):
    # -- the accepted path ----------------------------------------------------
    def test_an_approved_task_is_built_validated_reviewed_and_frozen(self) -> None:
        engine, task_id, calls = self.approved_task()
        head_before = self.head_of(self.repo)

        view = engine.implement(task_id)

        self.assertEqual(view["implementation"]["result"], "delivered")
        self.assertEqual(view["task"]["status"], "awaiting_acceptance")
        self.assertEqual(
            [run["role"] for run in view["runs"]],
            ["intake", "architect", "builder", "qa", "reviewer"],
        )
        self.assertTrue(all(run["status"] == "succeeded" for run in view["runs"]))

        iterations = view["iterations"]
        self.assertEqual(len(iterations), 1)
        iteration = iterations[0]
        self.assertEqual(iteration["status"], "ready")
        self.assertEqual(iteration["number"], 1)
        self.assertTrue(iteration["commit_sha"])
        self.assertEqual(iteration["build_report"]["summary"], BUILD_REPORT["summary"])
        self.assertEqual(iteration["qa_report"]["verdict"], "passed")
        self.assertEqual(iteration["review_report"]["verdict"], "accepted")
        self.assertEqual(iteration["validation"]["command"], ["python", "-m", "pytest", "-q"])
        self.assertEqual(iteration["validation_exit_code"], 0)

        # The deliverable binds the reviewed commit, the contract, and the evidence.
        deliverable = view["deliverable"]
        self.assertEqual(len(deliverable["digest"]), 64)
        document = deliverable["document"]
        self.assertEqual(document["iteration"]["commit"], iteration["commit_sha"])
        self.assertEqual(
            [item["id"] for item in document["acceptance_contract"]["criteria"]],
            [CRITERION_ID],
        )
        self.assertEqual(
            [entry["path"] for entry in document["changed_files"]],
            ["tests/test_hello.py"],
        )
        self.assertIn("test_hello.py", document["diff_excerpt"])
        self.assertEqual(document["validation"]["record"]["sandbox_image_id"], IMAGE_ID)
        coverage = document["verification_coverage"]
        self.assertEqual(coverage["environment_check"]["status"], "succeeded")
        verification = coverage["criteria"][0]
        self.assertEqual(verification["status"], "succeeded")
        self.assertTrue(verification["selector_exists"])
        self.assertTrue(verification["selector_executed"])
        self.assertEqual(
            verification["execution"]["command"],
            ["python", "-m", "pytest", "-q", "tests/test_hello.py::test_hello"],
        )
        self.assertEqual(verification["execution"]["exit_code"], 0)
        self.assertEqual(
            verification["execution"]["source_digest"],
            iteration["validation"]["workspace_digest"],
        )
        self.assertEqual(verification["execution"]["sandbox_image_id"], IMAGE_ID)

        # Validation really went through the sandbox boundary.
        container_calls = [call for call in calls if call[1:2] == ["run"]]
        self.assertEqual(len(container_calls), 4)
        arguments = container_calls[0]
        for flag in ("--cap-drop", "--security-opt", "--pids-limit", "--memory",
                     "--cpus", "--read-only", "--tmpfs"):
            self.assertIn(flag, arguments)
        self.assertIn("no-new-privileges:true", arguments)
        self.assertEqual(arguments[arguments.index("--network") + 1], "none")
        self.assertNotIn("--privileged", arguments)
        executed_commands = [
            call[call.index("python:3.12-slim") + 1 :] for call in container_calls
        ]
        self.assertEqual(
            executed_commands[1:],
            [
                ["python", "-m", "pytest", "--version"],
                [
                    "python", "-m", "pytest", "--collect-only", "-q",
                    "tests/test_hello.py::test_hello",
                ],
                ["python", "-m", "pytest", "-q", "tests/test_hello.py::test_hello"],
            ],
        )

        # The developer's working tree is untouched: the work is on a task branch.
        self.assertEqual(self.head_of(self.repo), head_before)
        self.assertEqual(self.porcelain(self.repo), "")
        self.assertGreater(engine.store.verify_audit_chain(), 0)

    def test_a_missing_mandatory_selector_stops_before_qa_and_review(self) -> None:
        engine, task_id, _ = self.approved_task(selector_collection_exit_code=4)

        view = engine.implement(task_id)

        self.assertEqual(view["implementation"]["result"], "failed")
        self.assertEqual(
            view["implementation"]["reason"], "acceptance-verification-incomplete"
        )
        self.assertEqual(view["task"]["status"], "implementation_failed")
        self.assertEqual(
            [run["role"] for run in view["runs"]],
            ["intake", "architect", "builder"],
        )
        coverage = view["iterations"][0]["validation"]["verification_coverage"]
        self.assertEqual(coverage["criteria"][0]["status"], "failed")
        self.assertFalse(coverage["criteria"][0]["selector_exists"])
        self.assertFalse(coverage["criteria"][0]["selector_executed"])
        self.assertEqual(
            coverage["criteria"][0]["failure_reason"],
            "acceptance-pytest-selector-not-found",
        )

    def test_a_failing_mandatory_selector_stops_before_qa_and_review(self) -> None:
        engine, task_id, _ = self.approved_task(selector_exit_code=1)

        view = engine.implement(task_id)

        self.assertEqual(view["implementation"]["result"], "failed")
        self.assertEqual(
            view["implementation"]["reason"], "acceptance-verification-incomplete"
        )
        self.assertEqual(
            [run["role"] for run in view["runs"]],
            ["intake", "architect", "builder"],
        )
        result = view["iterations"][0]["validation"]["verification_coverage"]["criteria"][0]
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["selector_exists"])
        self.assertTrue(result["selector_executed"])
        self.assertEqual(result["execution"]["exit_code"], 1)

    def test_a_mandatory_unverified_obligation_stays_unverified_and_blocks_readiness(
        self,
    ) -> None:
        proposal = {
            **PROPOSAL,
            "acceptance_criteria_mapping": [
                {
                    "criterion": "GET /hello returns 200",
                    "approach": "Human review required.",
                    "mandatory": True,
                    "verification": {"kind": "unverified"},
                }
            ],
        }
        engine, task_id, _ = self.approved_task(proposal=proposal)

        view = engine.implement(task_id)

        self.assertEqual(view["implementation"]["result"], "failed")
        self.assertEqual(
            view["implementation"]["reason"], "acceptance-verification-incomplete"
        )
        self.assertEqual(view["task"]["status"], "implementation_failed")
        coverage = view["iterations"][0]["validation"]["verification_coverage"]
        self.assertEqual(coverage["criteria"][0]["status"], "unverified")
        self.assertFalse(coverage["criteria"][0]["selector_executed"])
        self.assertEqual(
            coverage["criteria"][0]["failure_reason"],
            "acceptance-obligation-unverified",
        )

    def test_every_stage_start_is_recorded_once_under_one_name(self) -> None:
        """A stage start must not appear twice, and iteration changes say so."""

        engine, task_id, _ = self.approved_task()
        engine.implement(task_id)

        events = [event["event"] for event in engine.store.audit_events(task_id)]
        for stage in ("intake", "architect", "builder", "qa", "reviewer"):
            self.assertEqual(
                events.count(f"{stage}.started"), 1,
                f"{stage}.started should be recorded exactly once: {events}",
            )
        self.assertEqual(events.count("qa.started"), 1, events)
        self.assertNotIn("review.started", events)
        for transition in ("iteration.qa-started", "iteration.review-started",
                           "iteration.validation-started"):
            self.assertEqual(events.count(transition), 1, events)

    def test_the_delivery_decision_is_bound_to_the_deliverable_digest(self) -> None:
        engine, task_id, _ = self.approved_task()
        digest = engine.implement(task_id)["deliverable"]["digest"]

        with self.assertRaises(StoreError) as wrong_digest:
            engine.accept_delivery(task_id, deliverable_digest="0" * 64)
        self.assertEqual(wrong_digest.exception.code, "digest-mismatch")

        decision = engine.accept_delivery(task_id, deliverable_digest=digest)
        self.assertEqual(decision["decision"], "accepted")
        self.assertEqual(engine.describe(task_id)["task"]["status"], "delivered")

        with self.assertRaises(StoreError) as settled:
            engine.reject_delivery(task_id, deliverable_digest=digest, reason="too late")
        self.assertEqual(settled.exception.code, "not-awaiting-acceptance")

    def test_a_rejected_delivery_keeps_its_reason(self) -> None:
        engine, task_id, _ = self.approved_task()
        digest = engine.implement(task_id)["deliverable"]["digest"]

        decision = engine.reject_delivery(
            task_id,
            deliverable_digest=digest,
            reason="The endpoint needs a test for the 404 path.",
        )

        self.assertEqual(decision["decision"], "rejected")
        self.assertEqual(decision["reason"], "The endpoint needs a test for the 404 path.")
        self.assertEqual(engine.describe(task_id)["task"]["status"], "delivery_rejected")

    # -- the fail-closed branches ---------------------------------------------
    def test_a_missing_sandbox_stops_before_anything_runs(self) -> None:
        engine, task_id, _ = self.approved_task(sandbox=False)

        with self.assertRaises(EngineError) as caught:
            engine.implement(task_id)

        self.assertEqual(caught.exception.code, "sandbox-not-configured")
        self.assertEqual(engine.store.iterations(task_id), [])
        self.assertEqual(engine.describe(task_id)["task"]["status"], "approved")

    def test_a_failing_validation_run_fails_the_task_with_its_evidence(self) -> None:
        engine, task_id, _ = self.approved_task(validation_exit_code=1)

        view = engine.implement(task_id)

        self.assertEqual(view["implementation"]["result"], "failed")
        self.assertEqual(view["implementation"]["reason"], "validation-failed")
        self.assertEqual(view["task"]["status"], "implementation_failed")

        iteration = view["iterations"][0]
        self.assertEqual(iteration["status"], "validation_failed")
        self.assertEqual(iteration["validation"]["exit_code"], 1)
        self.assertEqual(iteration["failure_reason"], "validation-failed")
        self.assertIsNone(view["deliverable"])

        # QA and Reviewer never ran: an unvalidated change set is not reviewed.
        self.assertEqual(
            [run["role"] for run in view["runs"]],
            ["intake", "architect", "builder"],
        )

    def test_an_unapproved_task_cannot_be_implemented(self) -> None:
        engine, task_id = self.submitted()

        with self.assertRaises(EngineError) as caught:
            engine.implement(task_id)

        self.assertEqual(caught.exception.code, "not-approved")
        self.assertEqual(engine.store.iterations(task_id), [])

    # -- rework and the terminal failures -------------------------------------
    def test_a_failed_qa_verdict_is_reworked_into_a_second_iteration(self) -> None:
        engine, task_id, _ = self.approved_task(qa_reports=[QA_FAILED, QA_REPORT])

        view = engine.implement(task_id)

        self.assertEqual(view["implementation"]["result"], "delivered")
        self.assertEqual([item["number"] for item in view["iterations"]], [1, 2])
        self.assertEqual([item["status"] for item in view["iterations"]],
                         ["changes_required", "ready"])
        self.assertEqual(
            [item["qa_report"]["verdict"] for item in view["iterations"]],
            ["failed", "passed"],
        )
        # The rework builds on the rejected attempt rather than starting over.
        first, second = view["iterations"]
        self.assertNotEqual(first["commit_sha"], second["commit_sha"])
        self.assertEqual(second["base_commit"], first["base_commit"])
        self.assertEqual(view["task"]["status"], "awaiting_acceptance")
        self.assertIsNotNone(view["deliverable"])

    def test_exhausting_the_allowed_iterations_fails_the_task(self) -> None:
        engine, task_id, _ = self.approved_task(qa_reports=[QA_FAILED], max_iterations=1)

        view = engine.implement(task_id)

        self.assertEqual(view["implementation"]["result"], "failed")
        self.assertEqual(view["implementation"]["reason"], "iterations-exhausted")
        self.assertEqual(view["task"]["status"], "implementation_failed")
        self.assertEqual(len(view["iterations"]), 1)
        self.assertEqual(view["iterations"][0]["failure_reason"], "iterations-exhausted")
        self.assertIsNone(view["deliverable"])

    def test_a_rejected_review_fails_the_task_terminally(self) -> None:
        engine, task_id, _ = self.approved_task(review_verdicts=[REVIEW_REJECTED])

        view = engine.implement(task_id)

        self.assertEqual(view["implementation"]["result"], "failed")
        self.assertEqual(view["implementation"]["reason"], "review-rejected")
        self.assertEqual(view["task"]["status"], "implementation_failed")
        self.assertEqual(view["iterations"][0]["status"], "review_failed")
        self.assertIsNone(view["deliverable"])

    def test_a_report_that_omits_an_approved_criterion_is_never_delivered(self) -> None:
        # The first QA verdict says "passed" without claiming the approved
        # criterion, so the omission is sent back instead of reaching the human.
        engine, task_id, _ = self.approved_task(
            qa_reports=[QA_OMITS_CRITERION, QA_REPORT]
        )

        view = engine.implement(task_id)

        self.assertEqual(view["implementation"]["result"], "delivered")
        first, second = view["iterations"]
        self.assertEqual(first["status"], "changes_required")
        self.assertEqual(first["qa_report"]["acceptance_criteria"], [])
        self.assertEqual(
            [claim["criterion_id"] for claim in second["qa_report"]["acceptance_criteria"]],
            [CRITERION_ID],
        )

    def test_an_unreported_file_in_the_commit_blocks_the_iteration(self) -> None:
        engine, task_id, _ = self.approved_task(
            unreported_files={"tests/test_unreported.py": "def test_hidden() -> None:\n    pass\n"},
            max_iterations=1,
        )

        view = engine.implement(task_id)

        self.assertEqual(view["implementation"]["result"], "failed")
        self.assertEqual(view["implementation"]["reason"], "unauthorized-path")
        self.assertEqual(view["task"]["status"], "implementation_failed")
        self.assertIsNone(view["deliverable"])


class ReviewerFormatRecovery(LoopCase):
    """Reviewer-only recovery after an ``invalid-review-report`` failure."""

    def failed_review(self, **options):
        engine, task_id, calls = self.approved_task(
            review_verdicts=options.pop("review_verdicts", [REVIEW_INFO_SEVERITY, REVIEW_VERDICT]),
            **options,
        )
        view = engine.implement(task_id)
        self.assertEqual(view["implementation"]["reason"], "invalid-review-report")
        self.assertEqual(view["task"]["status"], "review_failed")
        return engine, task_id, calls

    def corrupt(self, statement: str, *parameters: str) -> None:
        """Damage recorded evidence the way a bug would; for these tests only."""

        with closing(sqlite3.connect(self.state)) as db:
            db.execute(statement, parameters)
            db.commit()

    def reviewer_runs(self, engine, task_id):
        return [run for run in engine.store.runs(task_id) if run["role"] == "reviewer"]

    def assertRefused(self, engine, task_id, code="review-retry-refused"):
        runs_before = len(engine.store.runs(task_id))
        audit_before = len(engine.store.audit_events(task_id))
        with self.assertRaises(EngineError) as raised:
            engine.retry_review(task_id)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(len(engine.store.runs(task_id)), runs_before)
        self.assertEqual(len(engine.store.audit_events(task_id)), audit_before)
        self.assertEqual(engine._task(task_id)["status"], "review_failed")

    def test_only_the_reviewer_runs_again_and_the_failed_run_is_kept(self) -> None:
        engine, task_id, calls = self.failed_review()
        failed = self.reviewer_runs(engine, task_id)[0]
        containers_before = len([c for c in calls if c[1:2] == ["run"]])

        view = engine.retry_review(task_id)

        self.assertEqual(view["implementation"]["result"], "delivered")
        self.assertEqual(view["task"]["status"], "awaiting_acceptance")
        roles = [run["role"] for run in view["runs"]]
        self.assertEqual(roles.count("builder"), 1)
        self.assertEqual(roles.count("qa"), 1)
        self.assertEqual(roles.count("reviewer"), 2)
        self.assertEqual(len([c for c in calls if c[1:2] == ["run"]]), containers_before)
        first, second = self.reviewer_runs(engine, task_id)
        self.assertEqual(first, failed)
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["failure_reason"], "invalid-review-report")
        self.assertEqual(second["status"], "succeeded")
        self.assertEqual(len(view["iterations"]), 1)
        self.assertEqual(view["iterations"][0]["status"], "ready")
        self.assertEqual(view["iterations"][0]["reviewer_run_id"], second["id"])

        events = [event["event"] for event in view["audit"]]
        self.assertEqual(events.count("review.retry-authorised"), 1)
        self.assertEqual(events.count("reviewer.started"), 2)
        self.assertEqual(events.count("builder.started"), 1)
        retry = next(e for e in view["audit"] if e["event"] == "review.retry-authorised")
        self.assertEqual(retry["detail"]["failed_run_id"], failed["id"])
        self.assertEqual(retry["detail"]["proposal_digest"], view["approval"]["proposal_digest"])
        self.assertEqual(
            retry["detail"]["commit"], view["iterations"][0]["commit_sha"]
        )
        self.assertGreater(engine.store.verify_audit_chain(), 0)

    def test_recovery_still_stops_at_the_human_delivery_gate(self) -> None:
        engine, task_id, _ = self.failed_review()

        view = engine.retry_review(task_id)

        self.assertEqual(view["task"]["status"], "awaiting_acceptance")
        self.assertIsNone(view["acceptance"])
        digest = view["deliverable"]["digest"]
        with self.assertRaises(StoreError):
            engine.accept_delivery(task_id, deliverable_digest="0" * 64)
        self.assertEqual(
            engine.accept_delivery(task_id, deliverable_digest=digest)["decision"], "accepted"
        )

    def test_the_deliverable_records_exact_declared_selector_execution(self) -> None:
        engine, task_id, _ = self.failed_review()

        coverage = engine.retry_review(task_id)["deliverable"]["document"]["verification_coverage"]

        self.assertEqual(coverage["executed_command"], ["python", "-m", "pytest", "-q"])
        self.assertTrue(coverage["criteria"])
        self.assertTrue(all(item["selector_executed"] for item in coverage["criteria"]))
        self.assertTrue(
            all(item["status"] == "succeeded" for item in coverage["criteria"])
        )

    def test_review_retry_refuses_missing_recorded_selector_evidence(self) -> None:
        engine, task_id, _ = self.failed_review()
        iteration = engine.store.iteration(task_id)
        self.assertIsNotNone(iteration)
        validation = dict(iteration["validation"])
        validation.pop("verification_coverage")
        engine.store.update_iteration(
            iteration_id=iteration["id"],
            actor=engine.identity.current().id,
            event="test.verification-evidence-removed",
            fields={"validation": validation},
            detail={},
        )
        reviewers_before = len(self.reviewer_runs(engine, task_id))

        with self.assertRaises(EngineError) as raised:
            engine.retry_review(task_id)

        self.assertEqual(raised.exception.code, "review-retry-refused")
        self.assertEqual(len(self.reviewer_runs(engine, task_id)), reviewers_before)
        self.assertEqual(engine.describe(task_id)["task"]["status"], "review_failed")
        self.assertIsNone(engine.describe(task_id)["deliverable"])

    def test_the_retry_is_limited_to_one_attempt(self) -> None:
        engine, task_id, _ = self.failed_review(
            review_verdicts=[REVIEW_INFO_SEVERITY, REVIEW_INFO_SEVERITY, REVIEW_VERDICT]
        )

        view = engine.retry_review(task_id)

        self.assertEqual(view["implementation"]["reason"], "invalid-review-report")
        self.assertEqual(view["task"]["status"], "review_failed")
        self.assertEqual(len(self.reviewer_runs(engine, task_id)), 2)
        self.assertRefused(engine, task_id, code="review-retry-exhausted")
        self.assertEqual(len(self.reviewer_runs(engine, task_id)), 2)
        self.assertIsNone(view["deliverable"])

    def test_a_legitimate_rejection_is_never_retried(self) -> None:
        engine, task_id, _ = self.approved_task(review_verdicts=[REVIEW_REJECTED, REVIEW_VERDICT])
        engine.implement(task_id)

        with self.assertRaises(EngineError) as raised:
            engine.retry_review(task_id)

        self.assertEqual(raised.exception.code, "review-retry-refused")
        self.assertEqual(len(self.reviewer_runs(engine, task_id)), 1)

    def test_a_malformed_report_that_already_rejects_is_not_asked_again(self) -> None:
        engine, task_id, _ = self.failed_review(
            review_verdicts=[REVIEW_REJECTED_INFO_SEVERITY, REVIEW_VERDICT]
        )

        self.assertRefused(engine, task_id)

    def test_a_task_that_is_not_review_failed_cannot_be_retried(self) -> None:
        engine, task_id, _ = self.approved_task()

        with self.assertRaises(EngineError) as raised:
            engine.retry_review(task_id)

        self.assertEqual(raised.exception.code, "review-retry-refused")

    def test_an_uncommitted_change_in_the_worktree_blocks_recovery(self) -> None:
        engine, task_id, _ = self.failed_review()
        worktree = engine.workspace_root / f"task-{task_id.lower()}"
        (worktree / "tests" / "test_hello.py").write_text("# tampered\n", encoding="utf-8")

        self.assertRefused(engine, task_id)

    def test_a_different_commit_in_the_worktree_blocks_recovery(self) -> None:
        engine, task_id, _ = self.failed_review()
        worktree = engine.workspace_root / f"task-{task_id.lower()}"
        (worktree / "tests" / "test_hello.py").write_text("# other\n", encoding="utf-8")
        for arguments in (
            ["add", "-A"],
            ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "other"],
        ):
            subprocess.run(["git", "-C", str(worktree), *arguments], check=True)

        self.assertRefused(engine, task_id)

    def test_a_missing_worktree_blocks_recovery(self) -> None:
        engine, task_id, _ = self.failed_review()
        worktree = engine.workspace_root / f"task-{task_id.lower()}"
        shutil.rmtree(worktree)

        self.assertRefused(engine, task_id)

    def test_missing_or_failed_validation_evidence_blocks_recovery(self) -> None:
        engine, task_id, _ = self.failed_review()
        # A test-only database: corrupt the recorded evidence the way a bug would.
        self.corrupt("UPDATE iterations SET validation = NULL WHERE task_id = ?", task_id)

        self.assertRefused(engine, task_id)

    def test_a_qa_report_that_no_longer_matches_its_digest_blocks_recovery(self) -> None:
        engine, task_id, _ = self.failed_review()
        self.corrupt("UPDATE iterations SET qa_digest = 'x' WHERE task_id = ?", task_id)

        self.assertRefused(engine, task_id)

    def test_a_reviewer_response_that_parses_is_not_a_format_failure(self) -> None:
        engine, task_id, _ = self.failed_review()
        self.corrupt(
            "UPDATE runs SET response_text = ? WHERE role = 'reviewer'", fenced(REVIEW_VERDICT)
        )

        self.assertRefused(engine, task_id)


class BuilderRecovery(LoopCase):
    """Human-authorised recovery of work a failed Builder already wrote.

    The scripted Builder writes its files and then stops the way the real run did —
    the model's token budget is exhausted — so a change set exists but a build
    report does not. Every test here checks that the recovery reuses the production
    gate rather than going around it: the Builder is never invoked again, the
    original failure record is never rewritten, and validation, QA, the Reviewer,
    and the human delivery decision all still decide the outcome.
    """

    def worktree(self, engine, task_id):
        return engine.workspace_root / f"task-{task_id.lower()}"

    def failed_builder(self, **options):
        options.setdefault("builder_failures", {1: out_of_budget()})
        engine, task_id, calls = self.approved_task(**options)
        view = engine.implement(task_id)
        self.assertEqual(view["implementation"]["reason"], "resource-limit-exceeded")
        self.assertEqual(view["task"]["status"], "implementation_failed")
        self.assertIsNone(view["deliverable"])
        return engine, task_id, calls

    def inspection(self, engine, task_id):
        return engine.inspect_recovery(task_id)["recovery"]

    def snapshot_digest(self, engine, task_id):
        inspection = self.inspection(engine, task_id)
        self.assertTrue(inspection["eligible"], inspection["obstacles"])
        return inspection["snapshot"]["digest"]

    def assertRefused(self, engine, task_id, *, digest="0" * 64):
        """A refusal changes nothing at all: no run, no audit event, no commit."""

        worktree = self.worktree(engine, task_id)
        task_before = engine._task(task_id)
        iteration_before = engine.store.iteration(task_id)
        runs_before = engine.store.runs(task_id)
        audit_before = engine.store.audit_events(task_id)
        recoveries_before = engine.store.recoveries(task_id)
        head_before = self.head_of(worktree)
        porcelain_before = self.porcelain(worktree)

        with self.assertRaises(EngineError) as raised:
            engine.recover_builder(task_id, snapshot_digest=digest)

        self.assertEqual(raised.exception.code, "recovery-refused")
        self.assertEqual(engine._task(task_id), task_before)
        self.assertEqual(engine.store.iteration(task_id), iteration_before)
        self.assertEqual(engine.store.runs(task_id), runs_before)
        self.assertEqual(engine.store.audit_events(task_id), audit_before)
        self.assertEqual(engine.store.recoveries(task_id), recoveries_before)
        self.assertEqual(self.head_of(worktree), head_before)
        self.assertEqual(self.porcelain(worktree), porcelain_before)
        self.assertEqual(self.porcelain(self.repo), "")
        return raised.exception

    # -- the failure preserves usable work ------------------------------------
    def test_a_builder_that_runs_out_of_budget_leaves_its_work_behind(self) -> None:
        engine, task_id, calls = self.failed_builder()
        iteration = engine.store.iteration(task_id)
        worktree = self.worktree(engine, task_id)

        self.assertEqual(iteration["status"], "failed")
        self.assertEqual(iteration["failure_reason"], "resource-limit-exceeded")
        # No Builder answer was ever produced, so no report was recorded.
        self.assertIsNone(iteration["build_report"])
        self.assertIsNone(iteration["build_digest"])
        # ...but the files the Builder wrote were frozen on the task branch.
        self.assertTrue((worktree / "tests" / "test_hello.py").exists())
        self.assertEqual(self.head_of(worktree), iteration["commit_sha"])
        self.assertNotEqual(iteration["commit_sha"], iteration["base_commit"])
        self.assertEqual(self.porcelain(worktree), "")
        self.assertEqual(
            [run["role"] for run in engine.store.runs(task_id)],
            ["intake", "architect", "builder"],
        )
        # Nothing was validated, reviewed, or delivered for a failed run.
        self.assertEqual(calls, [])
        self.assertEqual(self.porcelain(self.repo), "")
        self.assertIsNone(engine.describe(task_id)["acceptance"])

    def test_inspecting_the_preserved_work_changes_nothing(self) -> None:
        engine, task_id, _ = self.failed_builder()
        worktree = self.worktree(engine, task_id)
        head_before = self.head_of(worktree)
        porcelain_before = self.porcelain(worktree)
        runs_before = len(engine.store.runs(task_id))
        audit_before = len(engine.store.audit_events(task_id))

        first = self.inspection(engine, task_id)
        second = self.inspection(engine, task_id)

        self.assertEqual(first, second)
        self.assertTrue(first["eligible"])
        self.assertEqual(first["obstacles"], [])
        self.assertEqual(first["limit"], 1)
        self.assertEqual(first["recoveries"], [])
        self.assertEqual(len(first["snapshot"]["digest"]), 64)
        self.assertEqual(
            [entry["path"] for entry in first["snapshot"]["files"]],
            ["tests/test_hello.py"],
        )
        self.assertEqual(first["snapshot"]["files"][0]["mode"], "file")
        self.assertEqual(first["builder_run"]["failure_reason"], "resource-limit-exceeded")
        self.assertEqual(first["iteration"]["status"], "failed")
        self.assertIn(first["snapshot"]["digest"], first["next"])
        self.assertIn("recover-builder", first["next"])
        # The human-facing view is rendered from this exact structure.
        rendered = render_recovery({"recovery": first})
        self.assertIn(first["snapshot"]["digest"], rendered)
        self.assertIn("recover-builder", rendered)
        self.assertIn("tests/test_hello.py", rendered)
        self.assertIn("authorized recoveries 0 of 1", rendered)
        # Reading is not changing: same commit, same worktree, same records.
        self.assertEqual(self.head_of(worktree), head_before)
        self.assertEqual(self.porcelain(worktree), porcelain_before)
        self.assertEqual(len(engine.store.runs(task_id)), runs_before)
        self.assertEqual(len(engine.store.audit_events(task_id)), audit_before)
        self.assertEqual(engine._task(task_id)["status"], "implementation_failed")

    # -- the authorised recovery ----------------------------------------------
    def test_recovering_delivers_through_the_existing_pipeline(self) -> None:
        engine, task_id, calls = self.failed_builder()
        failure_detail = engine.store.iteration(task_id)["failure_detail"]
        digest = self.snapshot_digest(engine, task_id)

        view = engine.recover_builder(task_id, snapshot_digest=digest)

        self.assertEqual(view["implementation"]["result"], "delivered")
        self.assertEqual(view["implementation"]["snapshot_digest"], digest)
        self.assertEqual(view["task"]["status"], "awaiting_acceptance")
        self.assertIsNone(view["acceptance"])
        # The Builder is never asked again; QA and the Reviewer still are.
        self.assertEqual(
            [run["role"] for run in view["runs"]],
            ["intake", "architect", "builder", "qa", "reviewer"],
        )
        self.assertTrue(any("--network" in call for call in calls))

        document = view["deliverable"]["document"]
        builder = document["builder"]
        self.assertEqual(builder["status"], "not-recorded")
        self.assertIsNone(builder["report"])
        self.assertIsNone(builder["digest"])
        self.assertIsNone(builder["tests_run"])
        self.assertEqual(builder["test_execution"], "not-recorded")
        self.assertEqual(builder["reason"], "resource-limit-exceeded")
        self.assertEqual(builder["run_id"], view["implementation"]["builder_run_id"])

        recovery = document["recovery"]
        self.assertEqual(recovery["acceptance"], "pending-human-acceptance")
        self.assertEqual(recovery["builder_report"], "not-recorded")
        self.assertEqual(recovery["builder_tests_run"], "not-recorded")
        self.assertEqual(recovery["authorized_by"], "alice")
        self.assertEqual(recovery["snapshot_digest"], digest)
        self.assertEqual(recovery["recovery_digest"], document["iteration"]["workspace_digest"])
        self.assertEqual(recovery["failed_builder_run_id"], builder["run_id"])
        self.assertEqual(
            [entry["path"] for entry in recovery["changed_files"]],
            ["tests/test_hello.py"],
        )
        self.assertEqual(
            [entry["path"] for entry in document["changed_files"]],
            ["tests/test_hello.py"],
        )

        recovered = engine.store.iteration(task_id)
        self.assertEqual(recovered["status"], "ready")
        self.assertIsNotNone(recovered["qa_run_id"])
        self.assertIsNotNone(recovered["reviewer_run_id"])
        # The iteration's own failure record is never rewritten by a recovery.
        self.assertEqual(recovered["failure_reason"], "resource-limit-exceeded")
        self.assertEqual(recovered["failure_detail"], failure_detail)

        events = [event["event"] for event in view["audit"]]
        self.assertEqual(events.count("recovery.authorized"), 1)
        self.assertEqual(events.count("recovery.frozen"), 1)
        self.assertEqual(events.count("builder.started"), 1)
        self.assertEqual(events.count("qa.started"), 1)
        self.assertEqual(events.count("reviewer.started"), 1)
        self.assertGreater(engine.store.verify_audit_chain(), 0)

    def test_the_failed_builder_run_is_kept_exactly_as_it_failed(self) -> None:
        engine, task_id, _ = self.failed_builder()
        before = [run for run in engine.store.runs(task_id) if run["role"] == "builder"]
        digest = self.snapshot_digest(engine, task_id)

        engine.recover_builder(task_id, snapshot_digest=digest)

        after = [run for run in engine.store.runs(task_id) if run["role"] == "builder"]
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0], before[0])
        self.assertEqual(after[0]["status"], "failed")
        self.assertEqual(after[0]["failure_reason"], "resource-limit-exceeded")

    def test_the_recovered_delivery_still_waits_for_a_human(self) -> None:
        engine, task_id, _ = self.failed_builder()
        digest = self.snapshot_digest(engine, task_id)

        view = engine.recover_builder(task_id, snapshot_digest=digest)

        self.assertIsNone(view["acceptance"])
        with self.assertRaises(StoreError):
            engine.accept_delivery(task_id, deliverable_digest="0" * 64)
        decision = engine.accept_delivery(
            task_id, deliverable_digest=view["deliverable"]["digest"]
        )
        self.assertEqual(decision["decision"], "accepted")

    def test_the_qa_role_still_gates_the_recovered_work(self) -> None:
        engine, task_id, _ = self.failed_builder(qa_reports=[QA_FAILED])
        digest = self.snapshot_digest(engine, task_id)

        view = engine.recover_builder(task_id, snapshot_digest=digest)

        self.assertEqual(view["implementation"]["result"], "changes_required")
        self.assertEqual(view["task"]["status"], "changes_required")
        self.assertIsNone(view["deliverable"])
        self.assertEqual(engine.store.iteration(task_id)["status"], "changes_required")

    def test_the_reviewer_still_gates_the_recovered_work(self) -> None:
        engine, task_id, _ = self.failed_builder(
            review_verdicts=[REVIEW_CHANGES_REQUIRED]
        )
        digest = self.snapshot_digest(engine, task_id)

        view = engine.recover_builder(task_id, snapshot_digest=digest)

        self.assertEqual(view["implementation"]["result"], "changes_required")
        self.assertEqual(view["task"]["status"], "changes_required")
        self.assertIsNone(view["deliverable"])
        self.assertEqual(engine.store.iteration(task_id)["status"], "changes_required")

    def test_a_rejection_still_ends_the_recovered_work(self) -> None:
        engine, task_id, _ = self.failed_builder(review_verdicts=[REVIEW_REJECTED])
        digest = self.snapshot_digest(engine, task_id)

        view = engine.recover_builder(task_id, snapshot_digest=digest)

        self.assertEqual(view["implementation"]["result"], "failed")
        self.assertEqual(view["implementation"]["reason"], "review-rejected")
        self.assertEqual(view["task"]["status"], "implementation_failed")
        self.assertEqual(engine.store.iteration(task_id)["status"], "review_failed")
        self.assertIsNone(view["deliverable"])

    def test_validation_still_gates_the_recovered_work(self) -> None:
        engine, task_id, _ = self.failed_builder(validation_exit_code=1)
        digest = self.snapshot_digest(engine, task_id)

        view = engine.recover_builder(task_id, snapshot_digest=digest)

        self.assertEqual(view["implementation"]["result"], "failed")
        self.assertEqual(view["task"]["status"], "implementation_failed")
        self.assertEqual(engine.store.iteration(task_id)["status"], "validation_failed")
        self.assertIsNone(view["deliverable"])
        self.assertEqual([run["role"] for run in view["runs"]].count("qa"), 0)

    # -- what a recovery refuses ----------------------------------------------
    def test_a_wrong_snapshot_digest_is_refused(self) -> None:
        engine, task_id, _ = self.failed_builder()
        digest = self.snapshot_digest(engine, task_id)

        with self.assertRaises(EngineError) as raised:
            engine.recover_builder(task_id, snapshot_digest="0" * 64)

        self.assertEqual(raised.exception.code, "recovery-refused")
        self.assertIn("not the digest of the work", str(raised.exception))
        self.assertIn(digest, str(raised.exception))
        self.assertEqual(engine._task(task_id)["status"], "implementation_failed")
        self.assertEqual(engine.store.recoveries(task_id), [])

    def test_a_malformed_digest_is_refused_before_anything_is_read(self) -> None:
        engine, task_id, _ = self.failed_builder()

        error = self.assertRefused(engine, task_id, digest="not-a-digest")

        self.assertIn("64-character digest", str(error))

    def test_the_authorisation_is_limited_to_one_recovery(self) -> None:
        engine, task_id, _ = self.failed_builder(validation_exit_code=1)
        digest = self.snapshot_digest(engine, task_id)
        engine.recover_builder(task_id, snapshot_digest=digest)
        self.assertEqual(len(engine.store.recoveries(task_id)), 1)

        inspection = self.inspection(engine, task_id)
        self.assertFalse(inspection["eligible"])
        self.assertTrue(
            any(
                "already used its 1 authorised recovery" in obstacle
                for obstacle in inspection["obstacles"]
            ),
            inspection["obstacles"],
        )
        error = self.assertRefused(engine, task_id, digest=digest)
        self.assertIn("already used its 1 authorised recovery", str(error))

    def test_a_worktree_changed_after_the_freeze_is_refused(self) -> None:
        engine, task_id, _ = self.failed_builder()
        digest = self.snapshot_digest(engine, task_id)
        worktree = self.worktree(engine, task_id)
        (worktree / "tests" / "test_hello.py").write_text("# tampered\n", encoding="utf-8")

        inspection = self.inspection(engine, task_id)

        self.assertFalse(inspection["eligible"])
        self.assertTrue(
            any("workspace-dirty" in obstacle for obstacle in inspection["obstacles"]),
            inspection["obstacles"],
        )
        self.assertRefused(engine, task_id, digest=digest)
        # The refusal left the changed work exactly where the human found it.
        self.assertEqual(
            (worktree / "tests" / "test_hello.py").read_text(encoding="utf-8"),
            "# tampered\n",
        )

    def test_a_missing_worktree_is_refused(self) -> None:
        engine, task_id, _ = self.failed_builder()
        shutil.rmtree(self.worktree(engine, task_id))

        inspection = self.inspection(engine, task_id)

        self.assertFalse(inspection["eligible"])
        self.assertIsNone(inspection["snapshot"])
        self.assertEqual(inspection["limit"], 1)
        self.assertEqual(inspection["recoveries"], [])
        self.assertTrue(inspection["obstacles"])
        self.assertIsNone(inspection["next"])
        rendered = render_recovery({"recovery": inspection})
        self.assertIn("Not recoverable:", rendered)
        self.assertNotIn("Recovery available", rendered)
        with self.assertRaises(EngineError) as raised:
            engine.recover_builder(task_id, snapshot_digest="0" * 64)
        self.assertEqual(raised.exception.code, "recovery-refused")
        self.assertEqual(engine.store.recoveries(task_id), [])

    def test_work_that_fails_the_secret_screen_is_never_committed(self) -> None:
        engine, task_id, _ = self.failed_builder(
            unreported_files={".env": "AWS_SECRET_ACCESS_KEY=not-a-real-key\n"}
        )
        worktree = self.worktree(engine, task_id)

        self.assertIsNone(engine.store.iteration(task_id)["commit_sha"])
        inspection = self.inspection(engine, task_id)
        self.assertFalse(inspection["eligible"])
        self.assertIsNone(inspection["snapshot"])
        self.assertTrue(
            any("never committed a change set" in item for item in inspection["obstacles"]),
            inspection["obstacles"],
        )
        self.assertRefused(engine, task_id)
        # Refusing to freeze the work did not delete it.
        self.assertTrue((worktree / ".env").exists())
        self.assertTrue((worktree / "tests" / "test_hello.py").exists())

    def test_a_healthy_task_is_not_a_recovery_candidate(self) -> None:
        engine, task_id, _ = self.approved_task()
        view = engine.implement(task_id)
        self.assertEqual(view["task"]["status"], "awaiting_acceptance")

        inspection = self.inspection(engine, task_id)

        self.assertFalse(inspection["eligible"])
        self.assertTrue(
            any("only implementation_failed" in item for item in inspection["obstacles"]),
            inspection["obstacles"],
        )
        digest = inspection["snapshot"]["digest"]
        self.assertRefused(engine, task_id, digest=digest)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
