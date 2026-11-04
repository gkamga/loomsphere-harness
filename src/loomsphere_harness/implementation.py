"""The implementation loop: Builder, validation, QA, Reviewer, deliverable.

The loop reuses the same boundaries the rest of the runtime already enforces:

* only the Builder role is granted a writable worktree, and only inside the task
  worktree created from the approved commit;
* validation runs in the configured sandbox against the recorded change set, and a
  sandbox that cannot be created is a failure, never a pass;
* every model invocation goes through ``Engine.invoke_role`` so the run, the prompt
  digest, the persona digest, the skill selection, and the audit chain are recorded
  exactly as they are for Intake and Architect;
* the acceptance contract comes from the human approval, not from a model, and a
  report that drops or denies a criterion blocks delivery;
* the deliverable is frozen, digest-addressed, and decided by a human.

Nothing here writes to the target repository's working tree: the change set lives
on a task branch in a private worktree, and promotion is a separate human decision.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any, NoReturn, Sequence

from loomsphere_harness.engine import (
    BUILDER,
    QA,
    REVIEWER,
    Engine,
    EngineError,
    RoleOutcome,
)
from loomsphere_harness.implementation_prompt import (
    LoopPromptError,
    compose_builder_prompt,
    compose_qa_prompt,
    compose_reviewer_prompt,
)
from loomsphere_harness.reports import (
    BuildReport,
    CriterionClaim,
    QaReport,
    ReportError,
    ReviewVerdict,
    missing_criteria,
    parse_build_report,
    parse_qa_report,
    parse_review_verdict,
    unmet_criteria,
)
from loomsphere_harness.schemas.architect_proposal import extract_payload
from loomsphere_harness.schemas.acceptance_contract import (
    AcceptanceContract,
    AcceptanceContractError,
    build_contract,
    contract_digest,
)
from loomsphere_harness.validation import (
    VALIDATION_STATUS_SUCCEEDED,
    ValidationOutcome,
    ValidationPlanError,
    execute_validation,
    plan_validation,
)
from loomsphere_harness.workspace import (
    ChangeSet,
    DIFF_EXCERPT_CHARS,
    TaskWorkspace,
    WorkspaceError,
    commit_worktree,
    diff_excerpt,
    ensure_task_workspace,
    inspect_change_set,
    is_worktree,
    workspace_path_for,
)

DELIVERABLE_SCHEMA_VERSION = "1.0.0"

REASON_ITERATIONS_EXHAUSTED = "iterations-exhausted"
REASON_INVALID_BUILD_REPORT = "invalid-build-report"
REASON_INVALID_QA_REPORT = "invalid-qa-report"
REASON_INVALID_REVIEW_REPORT = "invalid-review-report"
REASON_VALIDATION_FAILED = "validation-failed"
REASON_QA_BLOCKED = "qa-blocked"
REASON_REVIEW_BLOCKED = "review-changes-required"
REASON_REVIEW_REJECTED = "review-rejected"
REASON_CONTRACT_UNAVAILABLE = "acceptance-contract-unavailable"

#: A Reviewer report-format failure may be retried this many times per iteration.
MAX_REVIEW_RETRIES = 1
REASON_REVIEW_RETRY_REFUSED = "review-retry-refused"
#: Verdicts that already carry a Reviewer decision; they are never re-asked.
ADVERSE_VERDICTS = ("rejected", "changes-required")

COMMIT_MESSAGE = "loomsphere-harness: {task} iteration {number}"
DETAIL_MAX_CHARS = 4_000

#: Iteration statuses this loop assigns. ``failed`` stays the terminal failure.
ITERATION_BUILDING = "building"
ITERATION_BUILT = "built"
ITERATION_VALIDATING = "validating"
ITERATION_QA_RUNNING = "qa_running"
ITERATION_REVIEW_RUNNING = "review_running"
ITERATION_READY = "ready"
ITERATION_CHANGES_REQUIRED = "changes_required"
ITERATION_FAILED = "failed"
ITERATION_VALIDATION_FAILED = "validation_failed"
ITERATION_QA_FAILED = "qa_failed"
ITERATION_REVIEW_FAILED = "review_failed"


def _document_digest(document: Any) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _claims(claims: Sequence[CriterionClaim]) -> list[dict[str, str]]:
    return [dataclasses.asdict(claim) for claim in claims]


class ImplementationStage:
    """One approved task's build/verify/review loop."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.store = engine.store

    # -- entry point ---------------------------------------------------------
    def run(self, task_id: str) -> dict[str, Any]:
        """Drive iterations until a deliverable is frozen or the loop fails closed."""

        engine = self.engine
        task = engine._task(task_id)
        if task["status"] not in ("approved", "changes_required"):
            raise EngineError(
                "not-approved",
                f"Task {task_id} is {task['status']}; an approved task is required.",
            )
        if engine.config.sandbox is None:
            # Fail closed before anything runs: without a sandbox image there is
            # no way to validate an implementation, and the host is never used.
            raise EngineError(
                "sandbox-not-configured",
                "A [sandbox] section with an image is required before implementation; "
                "the runtime never executes or validates on the host.",
            )

        contract, criteria_lines = self._contract(task_id)
        max_iterations = max(1, engine.config.max_iterations)
        feedback: tuple[str, ...] = ()
        iteration: dict[str, Any] | None = None

        for _ in range(max_iterations):
            task = engine._task(task_id)
            iteration = self.store.open_iteration(
                task_id=task_id,
                base_commit=task["repo_commit"],
                actor=engine.identity.current().id,
            )
            outcome = self._iterate(task, iteration, contract, criteria_lines, feedback)
            if outcome["result"] == "ready":
                self._publish(task, iteration, contract, outcome)
                return {"result": "delivered", "iteration_id": iteration["id"]}
            if outcome["result"] == "failed":
                return outcome
            feedback = tuple(outcome["feedback"])

        assert iteration is not None  # the loop body always assigns it
        return self._record_failure(
            task_id,
            iteration["id"],
            reason=REASON_ITERATIONS_EXHAUSTED,
            detail=(
                f"The loop still required changes after {max_iterations} iteration(s); "
                "a human must decide what happens next."
            ),
            expect=("changes_required",),
        )

    # -- Reviewer-only recovery ----------------------------------------------
    def retry_review(self, task_id: str) -> dict[str, Any]:
        """Re-run only the Reviewer after a report-format failure.

        Builder, validation, and QA are not repeated: their recorded evidence is
        re-verified against the approval, the worktree, and the stored runs, and the
        retry is refused when anything is missing, stale, or inconsistent. The failed
        run is never rewritten; a new Reviewer run and new audit events are appended.
        """

        engine = self.engine
        store = self.store
        refuse = self._refuse
        task = engine._task(task_id)
        if task["status"] != "review_failed":
            refuse(f"Task {task_id} is {task['status']}; only review_failed can be retried.")
        iteration = store.iteration(task_id)
        if iteration is None or iteration["status"] != ITERATION_REVIEW_FAILED:
            refuse("The latest iteration is not a failed review.")
        iteration_id = iteration["id"]
        if iteration.get("failure_reason") != REASON_INVALID_REVIEW_REPORT:
            refuse(
                f"The review failed with {iteration.get('failure_reason')!r}; only an "
                f"{REASON_INVALID_REVIEW_REPORT!r} format failure is retryable."
            )
        if store.deliverable(task_id) is not None or store.acceptance(task_id) is not None:
            refuse("A deliverable or delivery decision already exists.")
        if store.review_retries(task_id, iteration_id) >= MAX_REVIEW_RETRIES:
            raise EngineError(
                "review-retry-exhausted",
                f"Iteration {iteration_id} already used its {MAX_REVIEW_RETRIES} Reviewer retry.",
            )

        runs = {run["id"]: run for run in store.runs(task_id)}
        failed_run = self._format_failed_review_run(task_id, iteration, runs)

        # The approval must still bind the exact proposal the contract comes from.
        proposal = store.proposal(task_id)
        approval = store.approval(task_id)
        if proposal is None or approval is None:
            refuse("The approved proposal or its approval is missing.")
        if (
            approval["decision"] != "approved"
            or proposal["status"] != "approved"
            or approval["proposal_id"] != proposal["id"]
            or approval["proposal_digest"] != proposal["digest"]
            or _document_digest(proposal["document"]) != proposal["digest"]
        ):
            refuse("The original approval no longer matches the stored proposal.")
        if approval.get("repo_commit") not in (None, task["repo_commit"]) or (
            iteration["base_commit"] != task["repo_commit"]
        ):
            refuse("The approved repository commit does not match the iteration base.")
        contract, criteria_lines = self._contract(task_id)

        # Builder and QA evidence must still be the runs the iteration recorded.
        report = self._recorded_report(
            runs, iteration.get("builder_run_id"), "builder", parse_build_report,
            iteration.get("build_digest"),
        )
        qa = self._recorded_report(
            runs, iteration.get("qa_run_id"), "qa", parse_qa_report, iteration.get("qa_digest")
        )
        if qa.verdict != "passed" or qa.blocking_findings or self._criteria_problems(
            qa.acceptance_criteria, contract
        ):
            refuse("The recorded QA evidence does not support a Reviewer run.")

        validation = self._recorded_validation(iteration)

        # The worktree must still hold exactly the validated, QA-checked commit.
        path = workspace_path_for(engine.workspace_root, task_id)
        recorded = iteration.get("workspace_path")
        if (
            not is_worktree(path)
            or not recorded
            or Path(recorded).resolve() != path.resolve()
        ):
            refuse("The task worktree is missing or is not the recorded workspace.")
        try:
            changeset = inspect_change_set(
                path, base_commit=iteration["base_commit"],
                reviewed_commit=str(iteration.get("commit_sha")),
                reported_paths=set(report.paths),
            )
        except WorkspaceError as error:
            refuse(f"The worktree no longer matches the recorded change set ({error.code}).")
        if (
            changeset.workspace_digest != iteration.get("workspace_digest")
            or validation.workspace_digest != iteration.get("workspace_digest")
            or diff_excerpt(changeset.diff, DIFF_EXCERPT_CHARS) != iteration.get("diff_excerpt")
        ):
            refuse("The worktree content differs from the validated and reviewed change set.")

        task = engine._task(task_id)
        try:
            review_prompt, selection = self._review_prompt(
                task, report=report, qa=qa, diff=changeset.diff, command=validation.command,
                exit_code=validation.exit_code, criteria_lines=criteria_lines,
            )
        except LoopPromptError as error:
            raise EngineError(error.code, error.message) from error

        actor = engine.identity.current().id
        attempt = store.authorize_review_retry(
            task_id=task_id, iteration_id=iteration_id, actor=actor,
            limit=MAX_REVIEW_RETRIES,
            detail={
                "failed_run_id": failed_run["id"],
                "failure_reason": REASON_INVALID_REVIEW_REPORT,
                "failure_detail": str(failed_run.get("failure_detail") or "")[:500],
                "proposal_digest": proposal["digest"],
                "contract_digest": contract_digest(contract),
                "commit": changeset.head_commit,
                "workspace_digest": changeset.workspace_digest,
                "validation_output_digest": validation.output_digest,
                "builder_run_id": iteration["builder_run_id"],
                "qa_run_id": iteration["qa_run_id"],
            },
        )
        review_outcome = engine.invoke_role(
            task=task, role=REVIEWER, composed=review_prompt, selection=selection,
            expect=("review_failed",), running="review_running",
            failed="review_failed", workspace_path=path, writable=False,
        )
        outcome = self._finish_review(
            task_id=task_id, iteration_id=iteration_id, contract=contract,
            review_outcome=review_outcome, changeset=changeset, report=report, qa=qa,
            validation=validation, workspace=TaskWorkspace(
                path=path, branch=str(iteration.get("branch")), commit=changeset.head_commit,
                base_commit=iteration["base_commit"],
            ),
        )
        if outcome["result"] == "ready":
            published = self._publish(task, iteration, contract, outcome)
            return {"result": "delivered", "iteration_id": iteration_id,
                    "retry_attempt": attempt, "reviewer_run_id": review_outcome.run_id,
                    **published}
        return {**outcome, "retry_attempt": attempt}

    @staticmethod
    def _refuse(message: str) -> NoReturn:
        raise EngineError(REASON_REVIEW_RETRY_REFUSED, message)

    def _format_failed_review_run(
        self, task_id: str, iteration: dict[str, Any], runs: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """The failed Reviewer run, proven to be a report-format failure.

        The stored response is parsed again: a response that now parses was not a
        format failure, and one that states an adverse verdict is a real decision.
        Neither is ever re-asked in the hope of a better answer.
        """

        reviewers = [run for run in runs.values() if run["role"] == REVIEWER]
        qa_run = runs.get(iteration.get("qa_run_id") or "")
        if not reviewers or qa_run is None:
            self._refuse("The failed Reviewer run or the QA run is missing.")
        failed = max(reviewers, key=lambda run: run["started_at"])
        if (
            failed["status"] != "failed"
            or failed.get("failure_reason") != REASON_INVALID_REVIEW_REPORT
            or failed["started_at"] < qa_run["started_at"]
            or iteration.get("reviewer_run_id") is not None
        ):
            self._refuse("The latest Reviewer run is not a recorded report-format failure.")
        text = failed.get("response_text") or ""
        try:
            parse_review_verdict(text)
        except ReportError:
            pass
        else:
            self._refuse("The failed Reviewer response is a valid report; it is not a format failure.")
        try:
            verdict = str(extract_payload(text).get("verdict", "")).strip().lower()
        except Exception:  # noqa: BLE001 - an unreadable response carries no verdict
            verdict = ""
        if verdict in ADVERSE_VERDICTS:
            self._refuse(f"The malformed report already states the verdict {verdict!r}.")
        return failed

    def _recorded_report(
        self, runs: dict[str, dict[str, Any]], run_id: str | None, role: str, parser: Any,
        expected_digest: str | None,
    ) -> Any:
        run = runs.get(run_id or "")
        if run is None or run["role"] != role or run["status"] != "succeeded":
            self._refuse(f"The recorded {role} run is missing or did not succeed.")
        try:
            report = parser(run.get("response_text") or "")
        except ReportError:
            self._refuse(f"The recorded {role} response no longer parses.")
        if not expected_digest or report.digest != expected_digest:
            self._refuse(f"The recorded {role} report does not match its stored digest.")
        return report

    def _recorded_validation(self, iteration: dict[str, Any]) -> ValidationOutcome:
        record = iteration.get("validation")
        if (
            not isinstance(record, dict)
            or record.get("status") != VALIDATION_STATUS_SUCCEEDED
            or record.get("exit_code") != 0
            or iteration.get("validation_exit_code") != 0
            or not record.get("command")
            or not record.get("output_digest")
        ):
            self._refuse("The recorded validation evidence is missing or did not succeed.")
        return ValidationOutcome(
            command=tuple(record["command"]),
            status=record["status"],
            exit_code=record["exit_code"],
            duration_ms=int(record.get("duration_ms") or 0),
            output="",
            output_digest=record["output_digest"],
            workspace_digest=str(record.get("workspace_digest") or ""),
            failure_reason=record.get("failure_reason"),
            sandbox_image=record.get("sandbox_image"),
            sandbox_image_id=record.get("sandbox_image_id"),
            execution=dict(record.get("execution") or {}),
        )

    # -- one iteration -------------------------------------------------------
    def _iterate(
        self,
        task: dict[str, Any],
        iteration: dict[str, Any],
        contract: AcceptanceContract,
        criteria_lines: tuple[str, ...],
        feedback: tuple[str, ...],
    ) -> dict[str, Any]:
        engine = self.engine
        task_id = task["id"]
        iteration_id = iteration["id"]
        number = iteration["number"]
        actor = engine.identity.current().id

        # 1. The private worktree: created from the approved commit the first time,
        #    reused for rework so the next attempt builds on the previous one.
        try:
            workspace = ensure_task_workspace(
                engine.repo_path, engine.workspace_root, task_id, iteration["base_commit"]
            )
        except WorkspaceError as error:
            return self._record_failure(
                task_id, iteration_id, reason=error.code, detail=error.message,
                expect=("implementation_running",),
            )
        self.store.update_iteration(
            iteration_id=iteration_id, actor=actor, event="workspace.ready",
            fields={"branch": workspace.branch, "workspace_path": str(workspace.path)},
            detail={"branch": workspace.branch, "head": workspace.commit},
        )

        # 2. Builder: the only writable invocation in the whole runtime.
        persona, _, selection = engine.persona_and_skills(task, BUILDER)
        try:
            prompt = compose_builder_prompt(
                persona=persona,
                task_title=task["title"],
                requirements=task["requirements"],
                acceptance_criteria=task["acceptance_criteria"],
                task_identifier=task_id,
                base_commit=iteration["base_commit"],
                iteration=number,
                max_iterations=max(1, engine.config.max_iterations),
                previous_feedback=feedback,
                approved_criteria=criteria_lines,
                skills=selection.documents,
            )
        except LoopPromptError as error:
            return self._record_failure(
                task_id, iteration_id, reason=error.code, detail=error.message,
                expect=("implementation_running",),
            )

        builder = engine.invoke_role(
            task=task, role=BUILDER, composed=prompt, selection=selection,
            expect=("implementation_running",), running="implementation_running",
            failed="implementation_failed", workspace_path=workspace.path, writable=True,
        )
        if not builder.succeeded:
            return self._failed_run(builder, task_id, iteration_id, running="implementation_running")

        try:
            report = parse_build_report(builder.response_text)
        except ReportError as error:
            self._fail_run(
                builder, task_id, running="implementation_running",
                failed="implementation_failed", reason=REASON_INVALID_BUILD_REPORT,
                detail=error.message,
            )
            return self._record_failure(
                task_id, iteration_id, reason=REASON_INVALID_BUILD_REPORT,
                detail=error.message, expect=None,
            )
        self.store.succeed_run(
            run_id=builder.run_id, response_text=builder.response_text, events=dict(builder.events)
        )

        # 3. Record the change set: one commit, then the gate that proves the
        #    worktree is clean, at the reviewed commit, and free of credentials.
        try:
            commit = commit_worktree(
                workspace.path, COMMIT_MESSAGE.format(task=task_id, number=number)
            )
            changeset = inspect_change_set(
                workspace.path,
                base_commit=iteration["base_commit"],
                reviewed_commit=commit,
                reported_paths=set(report.paths),
            )
        except WorkspaceError as error:
            return self._record_failure(
                task_id, iteration_id, reason=error.code, detail=error.message,
                expect=("implementation_running",),
                iteration_status=ITERATION_FAILED,
                extra={"builder_run_id": builder.run_id, "build_report": dataclasses.asdict(report),
                       "build_digest": report.digest},
            )

        self.store.update_iteration(
            iteration_id=iteration_id, actor=actor, event="iteration.built",
            fields={
                "status": ITERATION_BUILT,
                "commit_sha": changeset.head_commit,
                "workspace_digest": changeset.workspace_digest,
                "diff_excerpt": diff_excerpt(changeset.diff, DIFF_EXCERPT_CHARS),
                "builder_run_id": builder.run_id,
                "build_report": dataclasses.asdict(report),
                "build_digest": report.digest,
            },
            detail={"commit": changeset.head_commit, "files": len(changeset.changed_files),
                    "base_commit": changeset.base_commit},
        )

        # 4. Executed validation, in the sandbox, against this exact commit.
        outcome = self._validate(workspace.path, iteration_id, task_id, actor)
        if outcome is None:
            return self._record_failure(
                task_id, iteration_id, reason=REASON_VALIDATION_FAILED,
                detail="The recorded change set could not be validated.",
                expect=("implementation_running",),
                iteration_status=ITERATION_VALIDATION_FAILED,
            )
        if isinstance(outcome, dict):
            return outcome
        validation = outcome

        # 5. QA reads the recorded evidence; it never re-runs the suite itself.
        task = engine._task(task_id)
        persona, _, selection = engine.persona_and_skills(task, QA)
        try:
            qa_prompt = compose_qa_prompt(
                persona=persona,
                task_title=task["title"],
                requirements=task["requirements"],
                acceptance_criteria=task["acceptance_criteria"],
                task_identifier=task_id,
                implementation_summary=report.summary,
                implementation_files=changeset.paths,
                diff_excerpt=diff_excerpt(changeset.diff, DIFF_EXCERPT_CHARS),
                validation_command=validation.command,
                validation_exit_code=validation.exit_code,
                validation_output=str(validation.output),
                approved_criteria=criteria_lines,
                skills=selection.documents,
            )
        except LoopPromptError as error:
            return self._record_failure(
                task_id, iteration_id, reason=error.code, detail=error.message,
                expect=("implementation_running",),
            )

        self.store.update_iteration(
            iteration_id=iteration_id, actor=actor, event="iteration.qa-started",
            fields={"status": ITERATION_QA_RUNNING}, detail={"iteration": number},
        )
        qa_outcome = engine.invoke_role(
            task=task, role=QA, composed=qa_prompt, selection=selection,
            expect=("implementation_running",), running="qa_running",
            failed="qa_failed", workspace_path=workspace.path, writable=False,
        )
        if not qa_outcome.succeeded:
            return self._failed_run(qa_outcome, task_id, iteration_id, running="implementation_running")

        try:
            qa = parse_qa_report(qa_outcome.response_text)
        except ReportError as error:
            self._fail_run(
                qa_outcome, task_id, running="qa_running", failed="qa_failed",
                reason=REASON_INVALID_QA_REPORT, detail=error.message,
            )
            return self._record_failure(
                task_id, iteration_id, reason=REASON_INVALID_QA_REPORT,
                detail=error.message, expect=None, iteration_status=ITERATION_QA_FAILED,
            )
        self.store.succeed_run(
            run_id=qa_outcome.run_id, response_text=qa_outcome.response_text,
            events=dict(qa_outcome.events),
        )
        self.store.update_iteration(
            iteration_id=iteration_id, actor=actor, event="qa.recorded",
            fields={"qa_report": dataclasses.asdict(qa), "qa_digest": qa.digest,
                    "qa_run_id": qa_outcome.run_id},
            detail={"verdict": qa.verdict, "findings": len(qa.findings)},
        )

        problems = self._criteria_problems(qa.acceptance_criteria, contract)
        if qa.verdict != "passed" or qa.blocking_findings or problems:
            return self._rework(
                task_id, iteration_id, expect=("qa_running",), reason=REASON_QA_BLOCKED,
                feedback=self._qa_feedback(qa, problems),
            )

        # 6. Reviewer: the last automated decision before the human gate.
        task = engine._task(task_id)
        try:
            review_prompt, selection = self._review_prompt(
                task, report=report, qa=qa, diff=changeset.diff,
                command=validation.command, exit_code=validation.exit_code,
                criteria_lines=criteria_lines,
            )
        except LoopPromptError as error:
            return self._record_failure(
                task_id, iteration_id, reason=error.code, detail=error.message,
                expect=("qa_running",),
            )

        self.store.update_iteration(
            iteration_id=iteration_id, actor=actor, event="iteration.review-started",
            fields={"status": ITERATION_REVIEW_RUNNING}, detail={"iteration": number},
        )
        review_outcome = engine.invoke_role(
            task=task, role=REVIEWER, composed=review_prompt, selection=selection,
            expect=("qa_running",), running="review_running",
            failed="review_failed", workspace_path=workspace.path, writable=False,
        )
        return self._finish_review(
            task_id=task_id, iteration_id=iteration_id, contract=contract,
            review_outcome=review_outcome, changeset=changeset, report=report, qa=qa,
            validation=validation, workspace=workspace,
        )

    def _review_prompt(
        self,
        task: dict[str, Any],
        *,
        report: BuildReport,
        qa: QaReport,
        diff: str,
        command: Sequence[str],
        exit_code: int | None,
        criteria_lines: Sequence[str],
    ) -> tuple[Any, Any]:
        """Compose the Reviewer prompt from recorded evidence only."""

        persona, _, selection = self.engine.persona_and_skills(task, REVIEWER)
        prompt = compose_reviewer_prompt(
            persona=persona,
            task_title=task["title"],
            requirements=task["requirements"],
            acceptance_criteria=task["acceptance_criteria"],
            task_identifier=task["id"],
            implementation_summary=report.summary,
            diff_excerpt=diff_excerpt(diff, DIFF_EXCERPT_CHARS),
            validation_command=command,
            validation_exit_code=exit_code,
            qa_verdict=qa.verdict,
            qa_summary=qa.summary,
            qa_findings=[finding.description for finding in qa.findings],
            approved_criteria=criteria_lines,
            skills=selection.documents,
        )
        return prompt, selection

    def _finish_review(
        self,
        *,
        task_id: str,
        iteration_id: str,
        contract: AcceptanceContract,
        review_outcome: RoleOutcome,
        changeset: ChangeSet,
        report: BuildReport,
        qa: QaReport,
        validation: ValidationOutcome,
        workspace: Any,
    ) -> dict[str, Any]:
        """Judge one Reviewer invocation: shared by the loop and the format retry."""

        actor = self.engine.identity.current().id
        if not review_outcome.succeeded:
            return self._failed_run(review_outcome, task_id, iteration_id, running="qa_running")

        try:
            review = parse_review_verdict(review_outcome.response_text)
        except ReportError as error:
            self._fail_run(
                review_outcome, task_id, running="review_running", failed="review_failed",
                reason=REASON_INVALID_REVIEW_REPORT, detail=error.message,
            )
            return self._record_failure(
                task_id, iteration_id, reason=REASON_INVALID_REVIEW_REPORT,
                detail=error.message, expect=None, iteration_status=ITERATION_REVIEW_FAILED,
            )
        self.store.succeed_run(
            run_id=review_outcome.run_id, response_text=review_outcome.response_text,
            events=dict(review_outcome.events),
        )
        self.store.update_iteration(
            iteration_id=iteration_id, actor=actor, event="review.recorded",
            fields={"review_report": dataclasses.asdict(review),
                    "review_digest": review.digest, "reviewer_run_id": review_outcome.run_id},
            detail={"verdict": review.verdict, "findings": len(review.findings)},
        )

        problems = self._criteria_problems(review.acceptance_criteria, contract)
        if review.verdict == "rejected":
            return self._record_failure(
                task_id, iteration_id, reason=REASON_REVIEW_REJECTED,
                detail=review.summary or "The Reviewer rejected the implementation.",
                expect=("review_running",), iteration_status=ITERATION_REVIEW_FAILED,
            )
        if review.verdict != "accepted" or review.blocking_findings or problems:
            return self._rework(
                task_id, iteration_id, expect=("review_running",), reason=REASON_REVIEW_BLOCKED,
                feedback=self._review_feedback(review, problems),
            )

        # 7. Freeze the deliverable the human decides on.
        self.store.update_iteration(
            iteration_id=iteration_id, actor=actor, event="iteration.ready",
            fields={"status": ITERATION_READY},
            detail={"commit": changeset.head_commit, "criteria": len(contract.criteria)},
        )
        return {
            "result": "ready", "changeset": changeset, "report": report, "qa": qa,
            "review": review, "validation": validation, "workspace": workspace,
        }

    # -- validation ----------------------------------------------------------
    def _validate(
        self, workspace_path: Any, iteration_id: str, task_id: str, actor: str
    ) -> ValidationOutcome | dict[str, Any] | None:
        """Plan and execute the validation command, recording the evidence."""

        engine = self.engine
        validation_config = engine.config.validation
        self.store.update_iteration(
            iteration_id=iteration_id, actor=actor, event="iteration.validation-started",
            fields={"status": ITERATION_VALIDATING}, detail={"iteration_id": iteration_id},
        )
        try:
            plan = plan_validation(workspace_path, allowlist=validation_config.allowlist)
        except ValidationPlanError as error:
            return self._record_failure(
                task_id, iteration_id, reason=error.code, detail=error.message,
                expect=("implementation_running",),
                iteration_status=ITERATION_VALIDATION_FAILED,
            )

        record = execute_validation(
            plan,
            workspace_path,
            timeout_seconds=validation_config.timeout_seconds,
            excerpt_chars=validation_config.excerpt_chars,
            allowlist=validation_config.allowlist,
            sandbox=engine.config.sandbox,
            executor=engine.sandbox_executor,
        )
        self.store.update_iteration(
            iteration_id=iteration_id, actor=actor, event="validation.recorded",
            fields={
                "validation": record.as_record(),
                "validation_exit_code": record.exit_code,
            },
            detail={"status": record.status, "command": list(record.command),
                    "exit_code": record.exit_code, "reason": record.failure_reason},
        )
        if not record.succeeded:
            detail = (
                f"`{' '.join(record.command)}` did not succeed "
                f"({record.failure_reason or record.status}, exit {record.exit_code})."
            )
            return self._record_failure(
                task_id, iteration_id,
                reason=record.failure_reason or REASON_VALIDATION_FAILED,
                detail=f"{detail}\n{record.output}", expect=("implementation_running",),
                iteration_status=ITERATION_VALIDATION_FAILED,
            )
        return record

    # -- criteria ------------------------------------------------------------
    def _contract(self, task_id: str) -> tuple[AcceptanceContract, tuple[str, ...]]:
        """Rebuild the frozen contract from the proposal the human approved."""

        proposal = self.store.proposal(task_id)
        if proposal is None:
            raise EngineError(
                "proposal-missing", f"Task {task_id} has no approved proposal to implement."
            )
        document = proposal.get("document") or {}
        criteria = document.get("acceptance_criteria_mapping") or []
        try:
            contract = build_contract(list(criteria))
        except AcceptanceContractError as error:
            raise EngineError(REASON_CONTRACT_UNAVAILABLE, error.message) from error
        return contract, tuple(f"{item.id}: {item.text}" for item in contract.criteria)

    def _criteria_problems(
        self, claims: Sequence[CriterionClaim], contract: AcceptanceContract
    ) -> list[str]:
        """Every way a role's report contradicts the approved contract."""

        problems: list[str] = []
        missing = missing_criteria(list(claims), [item.id for item in contract.criteria])
        if missing:
            problems.append(f"Criteria missing from the report: {', '.join(missing)}.")
        unmet = unmet_criteria(list(claims))
        if unmet:
            problems.append(f"Criteria not reported as met: {', '.join(unmet)}.")
        return problems

    @staticmethod
    def _qa_feedback(qa: QaReport, problems: Sequence[str]) -> tuple[str, ...]:
        feedback = [f"QA verdict: {qa.verdict}. {qa.summary}".strip()]
        feedback.extend(
            f"QA finding ({finding.severity}): {finding.description}"
            for finding in qa.blocking_findings
        )
        feedback.extend(problems)
        return tuple(feedback)

    @staticmethod
    def _review_feedback(review: ReviewVerdict, problems: Sequence[str]) -> tuple[str, ...]:
        feedback = [f"Reviewer verdict: {review.verdict}. {review.summary}".strip()]
        feedback.extend(
            f"Reviewer finding ({finding.severity}): {finding.description}"
            for finding in review.blocking_findings
        )
        feedback.extend(problems)
        return tuple(feedback)

    # -- deliverable ---------------------------------------------------------
    def _publish(
        self,
        task: dict[str, Any],
        iteration: dict[str, Any],
        contract: AcceptanceContract,
        outcome: dict[str, Any],
    ) -> dict[str, Any]:
        """Freeze the reviewed change set into the deliverable a human decides on."""

        changeset: ChangeSet = outcome["changeset"]
        report: BuildReport = outcome["report"]
        qa: QaReport = outcome["qa"]
        review: ReviewVerdict = outcome["review"]
        validation: ValidationOutcome = outcome["validation"]
        workspace = outcome["workspace"]
        actor = self.engine.identity.current().id
        task_id = task["id"]
        # Re-read the iteration: the builder/QA/Reviewer run identifiers were
        # recorded after the copy the loop was holding.
        iteration = self.store.iteration(task_id) or iteration

        document: dict[str, Any] = {
            "schema_version": DELIVERABLE_SCHEMA_VERSION,
            "task": {
                "id": task_id,
                "title": task["title"],
                "requirements": task["requirements"],
                "acceptance_criteria": task["acceptance_criteria"],
            },
            "proposal_digest": (self.store.proposal(task_id) or {}).get("digest"),
            "iteration": {
                "id": iteration["id"],
                "number": iteration["number"],
                "base_commit": changeset.base_commit,
                "commit": changeset.head_commit,
                "branch": workspace.branch,
                "workspace_digest": changeset.workspace_digest,
            },
            "changed_files": changeset.changed_files,
            "diff_excerpt": diff_excerpt(changeset.diff, DIFF_EXCERPT_CHARS),
            "acceptance_contract": {
                "schema_version": contract.schema_version,
                "version": contract.version,
                "digest": contract_digest(contract),
                "criteria": [item.model_dump(mode="json") for item in contract.criteria],
            },
            "verification_coverage": self._verification_coverage(contract, validation),
            "builder": {
                "report": dataclasses.asdict(report),
                "digest": report.digest,
                "run_id": iteration.get("builder_run_id"),
            },
            "validation": {
                "record": validation.as_record(),
                "exit_code": validation.exit_code,
                "status": validation.status,
            },
            "qa": {
                "report": dataclasses.asdict(qa),
                "digest": qa.digest,
                "run_id": iteration.get("qa_run_id"),
            },
            "review": {
                "report": dataclasses.asdict(review),
                "digest": review.digest,
                "run_id": iteration.get("reviewer_run_id"),
            },
        }
        digest = _document_digest(document)
        self.store.create_deliverable(
            task_id=task_id, iteration_id=iteration["id"], digest=digest,
            document=document, actor=actor,
        )
        return {"digest": digest}

    @staticmethod
    def _verification_coverage(
        contract: AcceptanceContract, validation: ValidationOutcome
    ) -> dict[str, Any]:
        """What the harness actually executed, never what the proposal declared.

        The approved contract may name pytest selectors, but the runtime executes
        only the allowlisted validation command and never maps a declared selector
        onto it, so a declared selector is never reported as executed.
        """

        return {
            "executed_command": list(validation.command),
            "executed_exit_code": validation.exit_code,
            "criteria": [
                {
                    "id": item.id,
                    "declared": item.verification.model_dump(mode="json"),
                    "selector_executed": False,
                }
                for item in contract.criteria
            ],
            "note": (
                "Declared selectors are not executed individually; only the executed "
                "command above ran."
            ),
        }

    # -- failure and rework --------------------------------------------------
    def _fail_run(
        self, outcome: RoleOutcome, task_id: str, *, running: str, failed: str,
        reason: str, detail: str,
    ) -> None:
        """Record a post-processing failure against a run that is still running."""

        self.store.fail_run(
            run_id=outcome.run_id, task_id=task_id, failed_status=failed,
            running_status=running, actor=self.engine.identity.current().id,
            reason=reason, detail=detail[:DETAIL_MAX_CHARS],
            response_text=outcome.response_text, events=dict(outcome.events),
        )

    def _failed_run(
        self, outcome: RoleOutcome, task_id: str, iteration_id: str, *, running: str
    ) -> dict[str, Any]:
        """A role invocation failed: its run already carries the failure."""

        error = outcome.error
        reason = error.reason if error is not None else "run-failed"
        detail = error.detail if error is not None else "The invocation failed."
        return self._record_failure(
            task_id, iteration_id, reason=reason, detail=detail, expect=None,
        )

    def _record_failure(
        self,
        task_id: str,
        iteration_id: str,
        *,
        reason: str,
        detail: str,
        expect: tuple[str, ...] | None,
        iteration_status: str = ITERATION_FAILED,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Move the task to a failed status and record why on the iteration.

        ``expect`` is omitted when the run record already moved the task, so the
        status transition is never attempted twice.
        """

        actor = self.engine.identity.current().id
        if expect is not None:
            self.store.set_status(
                task_id, "implementation_failed", actor=actor, expect=expect,
                event="iteration.failed",
                detail={"iteration_id": iteration_id, "reason": reason},
            )
        fields: dict[str, Any] = {
            "status": iteration_status,
            "failure_reason": reason,
            "failure_detail": detail[:DETAIL_MAX_CHARS],
        }
        fields.update(extra or {})
        self.store.update_iteration(
            iteration_id=iteration_id, actor=actor, event="iteration.failed",
            fields=fields, detail={"reason": reason},
        )
        return {"result": "failed", "reason": reason, "detail": detail[:DETAIL_MAX_CHARS]}

    def _rework(
        self,
        task_id: str,
        iteration_id: str,
        *,
        expect: tuple[str, ...],
        reason: str,
        feedback: tuple[str, ...],
    ) -> dict[str, Any]:
        """Send the task back for another iteration with the recorded feedback."""

        actor = self.engine.identity.current().id
        self.store.update_iteration(
            iteration_id=iteration_id, actor=actor, event="iteration.changes-required",
            fields={"status": ITERATION_CHANGES_REQUIRED}, detail={"reason": reason},
        )
        self.store.set_status(
            task_id, "changes_required", actor=actor, expect=expect,
            event="iteration.changes-required",
            detail={"iteration_id": iteration_id, "reason": reason},
        )
        return {"result": "changes_required", "feedback": feedback}


__all__ = [
    "DELIVERABLE_SCHEMA_VERSION",
    "ImplementationStage",
    "MAX_REVIEW_RETRIES",
    "REASON_REVIEW_RETRY_REFUSED",
    "REASON_CONTRACT_UNAVAILABLE",
    "REASON_INVALID_BUILD_REPORT",
    "REASON_INVALID_QA_REPORT",
    "REASON_INVALID_REVIEW_REPORT",
    "REASON_ITERATIONS_EXHAUSTED",
    "REASON_QA_BLOCKED",
    "REASON_REVIEW_BLOCKED",
    "REASON_REVIEW_REJECTED",
    "REASON_VALIDATION_FAILED",
]
