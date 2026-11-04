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

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime_support import (  # noqa: E402
    INTAKE_ASSESSMENT,
    PROPOSAL,
    RuntimeCase,
    fenced,
)

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

BUILT_FILES = {
    "tests/test_hello.py": (
        '"""Automated test for the /hello route."""\n\n\n'
        "def test_hello() -> None:\n"
        "    assert True\n"
    ),
}


def fake_runtime(
    *, exit_code: int = 0, output: str = "1 passed\n", calls: list[list[str]] | None = None
):
    """A stand-in container runtime: it records, it never launches anything."""

    def runner(arguments, *, capture_output=True, text=True, timeout=0, check=False):
        if calls is not None:
            calls.append(list(arguments))
        if arguments[1:3] == ["image", "inspect"]:
            return CompletedProcess(arguments, 0, f"{IMAGE_ID}\n", "")
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
) -> FunctionModel:
    """Answer as Intake, Architect, Builder, QA, and Reviewer, in that order.

    The Builder answer writes the file a real Builder would have written — and only
    changes it a little each attempt, so a rework iteration still produces a real
    commit. The QA and Reviewer answers are consumed in sequence, so a suite can
    script a rework and the attempt that follows it. The `progress` counters are
    owned by the caller because the runtime builds a fresh model per agent run.
    """

    counts = progress if progress is not None else {"builder": 0, "qa": 0, "review": 0}
    qa_sequence = list(qa_reports or [QA_REPORT])
    review_sequence = list(review_verdicts or [REVIEW_VERDICT])

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
            return ModelResponse(parts=[TextPart(fenced(build_report or BUILD_REPORT))])
        if "# Role: QA" in prompt:
            return ModelResponse(parts=[TextPart(fenced(next_report(qa_sequence, "qa")))])
        if "# Role: Reviewer" in prompt:
            verdict = next_report(review_sequence, "review")
            return ModelResponse(parts=[TextPart(fenced(verdict))])
        return ModelResponse(parts=[TextPart(fenced(PROPOSAL))])

    return FunctionModel(respond)


class LoopCase(RuntimeCase):
    def loop_engine(
        self,
        *,
        sandbox: bool = True,
        validation_exit_code: int = 0,
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
            runner=fake_runtime(exit_code=validation_exit_code, calls=calls),
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

        # Validation really went through the sandbox boundary.
        container_calls = [call for call in calls if call[1:2] == ["run"]]
        self.assertEqual(len(container_calls), 1)
        arguments = container_calls[0]
        for flag in ("--cap-drop", "--security-opt", "--pids-limit", "--memory",
                     "--cpus", "--read-only", "--tmpfs"):
            self.assertIn(flag, arguments)
        self.assertIn("no-new-privileges:true", arguments)
        self.assertEqual(arguments[arguments.index("--network") + 1], "none")
        self.assertNotIn("--privileged", arguments)

        # The developer's working tree is untouched: the work is on a task branch.
        self.assertEqual(self.head_of(self.repo), head_before)
        self.assertEqual(self.porcelain(self.repo), "")
        self.assertGreater(engine.store.verify_audit_chain(), 0)

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

    def test_the_deliverable_never_claims_declared_selectors_were_executed(self) -> None:
        engine, task_id, _ = self.failed_review()

        coverage = engine.retry_review(task_id)["deliverable"]["document"]["verification_coverage"]

        self.assertEqual(coverage["executed_command"], ["python", "-m", "pytest", "-q"])
        self.assertTrue(coverage["criteria"])
        self.assertTrue(all(not item["selector_executed"] for item in coverage["criteria"]))

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
