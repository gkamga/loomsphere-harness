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
import re
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
    declared_verdicts,
    missing_criteria,
    parse_build_report,
    parse_qa_report,
    parse_review_verdict,
    unmet_criteria,
)
from loomsphere_harness.schemas.architect_proposal import extract_payload
from loomsphere_harness.architect_prompt import ComposedPrompt, with_format_correction
from loomsphere_harness.skill_selection import SkillSelection
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
    execute_acceptance_verifications,
    execute_validation,
    plan_validation,
)
from loomsphere_harness.workspace import (
    ChangeSet,
    DIFF_EXCERPT_CHARS,
    TaskWorkspace,
    WorkspaceError,
    changed_paths,
    commit_worktree,
    current_branch,
    diff_excerpt,
    ensure_task_workspace,
    inspect_change_set,
    is_ancestor,
    is_worktree,
    recoverable_inventory,
    resume_task_workspace,
    screen_change_set,
    workspace_path_for,
)

DELIVERABLE_SCHEMA_VERSION = "1.0.0"

REASON_ITERATIONS_EXHAUSTED = "iterations-exhausted"
REASON_INVALID_BUILD_REPORT = "invalid-build-report"
REASON_INVALID_QA_REPORT = "invalid-qa-report"
REASON_INVALID_REVIEW_REPORT = "invalid-review-report"
REASON_VALIDATION_FAILED = "validation-failed"
REASON_ACCEPTANCE_VERIFICATION_INCOMPLETE = "acceptance-verification-incomplete"
REASON_QA_BLOCKED = "qa-blocked"
REASON_REVIEW_BLOCKED = "review-changes-required"
REASON_REVIEW_REJECTED = "review-rejected"
REASON_CONTRACT_UNAVAILABLE = "acceptance-contract-unavailable"

#: A Reviewer report-format failure may be retried this many times per iteration.
MAX_REVIEW_RETRIES = 1
REASON_REVIEW_RETRY_REFUSED = "review-retry-refused"
#: Verdicts that already carry a Reviewer decision; they are never re-asked.
ADVERSE_VERDICTS = ("rejected", "changes-required")

#: A QA report-format failure may be corrected this many times per iteration, in
#: the same iteration and against the same recorded evidence. A corrected report
#: that is merely adverse is a decision, not a format failure, and is never
#: re-asked. The bound is a constant so no configuration can lengthen it.
MAX_QA_FORMAT_ATTEMPTS = 2
QA_FORMAT_RETRY_EVENT = "iteration.qa-format-retry"
#: QA verdicts that already carry a decision. A QA answer whose payload declares one
#: of these is never asked again, however malformed the rest of the report is, and
#: however many documents the answer offers: if *any* parseable candidate declares
#: `failed`, a second answer could only reverse the decision, not clarify it.
ADVERSE_QA_VERDICTS = ("failed",)

#: A failed Builder's preserved change set may be recovered this many times per
#: iteration. Authorization is recorded, so the limit survives a restart.
MAX_BUILDER_RECOVERIES = 1
REASON_RECOVERY_REFUSED = "recovery-refused"
REASON_RECOVERY_ELIGIBLE = "recovery-eligible"
#: An authorised recovery that stopped before delivery; the claim is closed with
#: this reason so the task is never left open.
REASON_RECOVERY_INCOMPLETE = "recovery-incomplete"
#: A Builder run that stopped without writing a report leaves an interrupted run
#: rather than a failed one; the worktree is treated exactly the same way. Two
#: spellings exist because the run is marked by the store when a process dies:
#: ``store.recover_interrupted`` writes plain ``interrupted``, while the loop writes
#: the role-qualified reason. Both mean the same thing to recovery, so both are
#: accepted rather than one silently making a crashed Builder unrecoverable.
REASON_BUILDER_INTERRUPTED = "builder-interrupted"
REASON_RUN_INTERRUPTED = "interrupted"

#: Failure reasons that describe a run which stopped *after* it may have written
#: files: resource and time limits, provider faults, harness faults, an empty
#: response, and an interrupted process. A refused or invalid *report* is excluded
#: because it means the Builder reached the end of its turn and answered.
RECOVERABLE_FAILURES = (
    "resource-limit-exceeded",
    "timed-out",
    "provider-unavailable",
    "model-error",
    "harness-error",
    "empty-response",
    REASON_BUILDER_INTERRUPTED,
    REASON_RUN_INTERRUPTED,
)
#: Run statuses that mean the Builder did not finish its turn.
RECOVERABLE_RUN_STATUSES = ("failed", "interrupted")

#: A Builder that stopped having written nothing at all may be restarted this many
#: times per task. The authorization is recorded in an immutable, single-row-per-
#: failed-iteration table, so the limit survives a process restart and a concurrent
#: invocation cannot exceed it.
MAX_BUILDER_RESTARTS = 1
REASON_RESTART_REFUSED = "restart-refused"
REASON_RESTART_EXHAUSTED = "restart-exhausted"
#: Failure reasons that mean the Builder's turn was cut short rather than answered:
#: the pilot's timeout, plus the two spellings a dead process leaves behind. A
#: model or provider fault is deliberately absent: it says the run was spent, not
#: interrupted, and repeating it would only spend the same budget again.
RESTARTABLE_FAILURES = ("timed-out", REASON_BUILDER_INTERRUPTED, REASON_RUN_INTERRUPTED)
_SHA256_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")

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

        validation = self._recorded_validation(iteration, contract)

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

    # -- human-authorised recovery of a failed Builder -----------------------
    def inspect_recovery(self, task_id: str) -> dict[str, Any]:
        """Report what a failed Builder left behind, without changing anything.

        Read-only by construction. It resolves an existing worktree, reads the
        change set, screens it, and recomputes the snapshot digest. It never
        creates a workspace, stages a file, commits, resets, cleans, or deletes
        anything, so a `recover-builder` refusal can never have damaged the work
        it refused.

        Two shapes of preserved work are reported. A Builder that failed while the
        process was alive had its work committed at the moment of failure, so the
        change set is read from that commit. A process that died took the failure
        path with it and left the files uncommitted; those are read and screened
        here the same way, and the snapshot says the commit has not been written
        yet, because writing it is what `recover-builder` does.
        """

        engine = self.engine
        store = self.store
        task = engine._task(task_id)
        iteration = store.iteration(task_id)
        runs = store.runs(task_id)
        builder_run = self._failed_builder_run(runs)

        view: dict[str, Any] = {
            "task": {"id": task_id, "title": task["title"], "status": task["status"]},
            "iteration": (
                None
                if iteration is None
                else {
                    "id": iteration["id"],
                    "number": iteration["number"],
                    "status": iteration["status"],
                    "failure_reason": iteration.get("failure_reason"),
                    "failure_detail": iteration.get("failure_detail"),
                    "commit": iteration.get("commit_sha"),
                    "base_commit": iteration.get("base_commit"),
                }
            ),
            "builder_run": (
                None
                if builder_run is None
                else {
                    "id": builder_run["id"],
                    "status": builder_run["status"],
                    "failure_reason": builder_run.get("failure_reason"),
                    "failure_detail": builder_run.get("failure_detail"),
                }
            ),
            "recoveries": [
                {
                    "id": item["id"],
                    "snapshot_digest": item["snapshot_digest"],
                    "recovery_digest": item["recovery_digest"],
                    "created_at": item["created_at"],
                }
                for item in store.recoveries(task_id)
            ],
            "limit": MAX_BUILDER_RECOVERIES,
            "eligible": False,
            "obstacles": [],
            "workspace": None,
            "change_set": None,
            "snapshot": None,
            "next": None,
            "restart": None,
        }

        snapshot: dict[str, Any] | None = None
        changeset: ChangeSet | None = None
        snapshot_error: str | None = None
        preserved = False
        worktree_ready = False
        changed_names: list[str] | None = None
        has_commit = iteration is not None and bool(iteration.get("commit_sha"))
        # A failed Builder either committed its work when the failure was recorded,
        # or the process died before it could. The second case is still inspectable:
        # the files are there and screening them is read-only.
        uncommitted = (
            iteration is not None
            and not has_commit
            and builder_run is not None
            and builder_run.get("failure_reason") in RECOVERABLE_FAILURES
        )
        if iteration is not None and (has_commit or uncommitted):
            path = workspace_path_for(engine.workspace_root, task_id)
            if is_worktree(path):
                try:
                    resumed = resume_task_workspace(engine.workspace_root, task_id)
                    view["workspace"] = {
                        "path": str(path),
                        "branch": resumed.branch,
                        "recorded_branch": iteration.get("branch"),
                        "recorded_path": iteration.get("workspace_path"),
                    }
                    if has_commit:
                        changeset = inspect_change_set(
                            path,
                            base_commit=iteration["base_commit"],
                            reviewed_commit=str(iteration.get("commit_sha")),
                        )
                    digest, entries = recoverable_inventory(path, iteration["base_commit"])
                    worktree_ready = True
                    if not has_commit:
                        changed_names = [entry["path"] for entry in entries]
                        # Work exists only if the inventory found it. An empty
                        # change set means the Builder left nothing behind, and
                        # advertising a recovery for it would send a human to
                        # authorise the freezing of no work at all.
                        preserved = bool(entries)
                        if entries:
                            screen_change_set(path, changed_names)
                except WorkspaceError as error:
                    snapshot_error = f"{error.code}: {error.message}"
                    preserved = False
                else:
                    if has_commit or entries:
                        snapshot = {
                            "digest": digest,
                            "files": entries,
                            "committed": has_commit,
                        }
                    if changeset is not None:
                        view["change_set"] = {
                            "base_commit": changeset.base_commit,
                            "head_commit": changeset.head_commit,
                            "workspace_digest": changeset.workspace_digest,
                            "changed_files": changeset.changed_files,
                            # The failed Builder never produced a report, so there is
                            # no claimed file list to compare against the change set.
                            "reported_files": None,
                        }

        if snapshot is None:
            # No digest can be shown, so report why from the same list
            # `recover-builder` enforces rather than inventing a reason here.
            obstacles = self._recovery_obstacles(
                task, iteration, builder_run, None, preserved=preserved
            )
            if snapshot_error is not None:
                obstacles.append(snapshot_error)
            elif not obstacles:
                obstacles.append(
                    "The task worktree holding the failed Builder's work is not available."
                )
            view["obstacles"] = obstacles
        else:
            view["snapshot"] = snapshot
            view["obstacles"] = self._recovery_obstacles(
                task, iteration, builder_run, changeset, preserved=preserved
            )
            view["eligible"] = not view["obstacles"]
            if view["eligible"]:
                view["next"] = (
                    f"loomsphere-harness recover-builder {task_id} "
                    f"--snapshot-digest {snapshot['digest']}"
                )
        if not view["eligible"]:
            view["next"] = None

        # Recovery is not the only exit from a failed Builder. When the failure was
        # an interruption and nothing at all was written, a human can authorise one
        # restart instead. Both paths are reported from their own oracle, and the
        # restart is never offered while preserved work exists.
        used = len(store.builder_restarts(task_id))
        restart_obstacles = self._restart_obstacles(
            task,
            iteration,
            builder_run,
            changed_names=changed_names,
            worktree_ready=worktree_ready,
        )
        view["restart"] = {
            "eligible": not restart_obstacles,
            "obstacles": restart_obstacles,
            "authorized": [
                {
                    "id": item["id"],
                    "failed_iteration_id": item["failed_iteration_id"],
                    "failed_run_id": item["failed_run_id"],
                    "failure_reason": item["failure_reason"],
                    "restart_number": item["restart_number"],
                    "created_at": item["created_at"],
                }
                for item in store.builder_restarts(task_id)
            ],
            "limit": MAX_BUILDER_RESTARTS,
            "used": used,
            "next": (
                None
                if restart_obstacles or builder_run is None
                else (
                    f"loomsphere-harness restart-builder {task_id} "
                    f"--failed-run {builder_run['id']}"
                )
            ),
        }
        return view

    def recover_builder(self, task_id: str, *, snapshot_digest: str) -> dict[str, Any]:
        """Freeze a failed Builder's preserved work and take it through the gate.

        The Builder is never invoked again. The preserved worktree is snapshotted,
        compared to the digest a human quoted from `inspect-recovery`, committed
        exactly as it stands, and then validated, QA-checked, and reviewed by the
        same stages a normal iteration uses. The original failed run, its failure
        reason, and the iteration's own failure record are never rewritten.

        The whole recovery is taken under an exclusive lease on the task, so a
        second process running a repair pass cannot mistake this live recovery
        for an interrupted stage and close it underneath.
        """

        engine = self.engine
        store = self.store
        task = engine._task(task_id)
        with store.recovery_lease(task_id):
            return self._recover_builder_under_lease(task, snapshot_digest=snapshot_digest)

    def _recover_builder_under_lease(
        self, task: dict[str, Any], *, snapshot_digest: str
    ) -> dict[str, Any]:
        task_id = task["id"]
        engine = self.engine
        store = self.store
        iteration = store.iteration(task_id)
        runs = store.runs(task_id)
        builder_run = self._failed_builder_run(runs)
        if iteration is None or builder_run is None:
            self._refuse_recovery(
                "The task has no failed Builder run and no iteration to recover."
            )
        if not _SHA256_PATTERN.match(snapshot_digest):
            self._refuse_recovery(
                "The snapshot digest must be the 64-character digest printed by "
                "`inspect-recovery`."
            )

        # A process that died took the freeze with it, so the work is still loose
        # in the worktree and no commit records it. Preserving it now, under the
        # same screen, is the only way an interrupted Builder can be recovered.
        # Everything that disqualifies the task is checked first, so a refused
        # recovery still leaves the worktree byte-for-byte as it found it.
        if not iteration.get("commit_sha"):
            blocked = self._recovery_obstacles(
                task, iteration, builder_run, None, preserved=True
            )
            if blocked:
                self._refuse_recovery(" ".join(blocked))
            # The submitted digest is compared to the loose work before anything
            # is committed or recorded.
            try:
                current_digest, _ = recoverable_inventory(
                    workspace_path_for(engine.workspace_root, task_id),
                    iteration["base_commit"],
                )
            except WorkspaceError as error:
                self._refuse_recovery(
                    f"The interrupted work cannot be inspected ({error.code})."
                )
            if current_digest != snapshot_digest:
                self._refuse_recovery(
                    "The supplied digest is not the digest of the work that would be "
                    f"recovered (current snapshot {current_digest})."
                )
            iteration = self._preserve_interrupted_work(task, iteration, builder_run)

        obstacles = self._recovery_obstacles(task, iteration, builder_run, None)
        if obstacles:
            self._refuse_recovery(" ".join(obstacles))

        # The approval must still bind the exact proposal the contract comes from,
        # and the same repository commit the loop is building on.
        proposal = store.proposal(task_id)
        approval = store.approval(task_id)
        if (
            proposal is None
            or approval is None
            or approval["decision"] != "approved"
            or proposal["status"] != "approved"
            or approval["proposal_id"] != proposal["id"]
            or approval["proposal_digest"] != proposal["digest"]
            or _document_digest(proposal["document"]) != proposal["digest"]
        ):
            self._refuse_recovery("The original approval no longer matches the proposal.")
        if approval.get("repo_commit") not in (None, task["repo_commit"]) or (
            iteration["base_commit"] != task["repo_commit"]
        ):
            self._refuse_recovery(
                "The approved repository commit does not match the iteration base, so "
                "the preserved work would be recovered on top of the wrong commit."
            )
        contract, criteria_lines = self._contract(task_id)

        # The worktree must still hold exactly the commit the failure recorded, on
        # the branch the record names.
        path = workspace_path_for(engine.workspace_root, task_id)
        recorded = iteration.get("workspace_path")
        if not is_worktree(path) or not recorded or Path(recorded).resolve() != path.resolve():
            self._refuse_recovery("The task worktree is missing or is not the recorded workspace.")
        try:
            workspace = resume_task_workspace(engine.workspace_root, task_id)
            commit = str(iteration.get("commit_sha") or "")
            if workspace.branch != iteration.get("branch"):
                self._refuse_recovery(
                    f"The worktree has branch {workspace.branch!r} checked out, not the "
                    f"recorded {iteration.get('branch')!r}; the recovered work would be "
                    "committed somewhere the record does not describe."
                )
            if workspace.commit != commit:
                self._refuse_recovery(
                    "The worktree head has moved since the change set was committed; "
                    "the recorded commit is no longer what would be recovered."
                )
            if not is_ancestor(path, iteration["base_commit"], commit):
                self._refuse_recovery("The recorded commit is not on top of the approved base.")
            changeset = inspect_change_set(
                path, base_commit=iteration["base_commit"], reviewed_commit=commit
            )
            digest, entries = recoverable_inventory(path, iteration["base_commit"])
        except WorkspaceError as error:
            self._refuse_recovery(f"The worktree no longer matches its record ({error.code}).")

        if (
            changeset.workspace_digest != iteration.get("workspace_digest")
            or changeset.head_commit != commit
        ):
            self._refuse_recovery("The preserved content differs from the recorded change set.")
        if digest != snapshot_digest:
            self._refuse_recovery(
                "The supplied digest is not the digest of the work that would be "
                f"recovered (current snapshot {digest})."
            )

        recovery_note = self._recovery_note(
            task=task, iteration=iteration, builder_run=builder_run, changeset=changeset,
            digest=digest, entries=entries,
        )
        actor = engine.identity.current().id
        record = store.authorize_builder_recovery(
            task_id=task_id,
            iteration_id=iteration["id"],
            actor=actor,
            recovery_digest=changeset.workspace_digest,
            snapshot_digest=digest,
            base_commit=changeset.base_commit,
            head_commit=changeset.head_commit,
            changed_files=entries,
            contract_digest=contract_digest(contract),
            failure={
                "reason": builder_run.get("failure_reason"),
                "detail": str(builder_run.get("failure_detail") or "")[:DETAIL_MAX_CHARS],
            },
            builder_run_id=builder_run["id"],
            limit=MAX_BUILDER_RECOVERIES,
        )

        try:
            outcome = self._recover_after_build(
                task=engine._task(task_id), iteration=iteration, contract=contract,
                criteria_lines=criteria_lines, workspace=workspace, changeset=changeset,
                recovery=recovery_note,
            )
            if outcome["result"] != "ready":
                return {**outcome, "recovery_id": record["id"], "snapshot_digest": digest}
            published = self._publish(
                engine._task(task_id), iteration, contract, outcome
            )
            return {
                "result": "delivered", "iteration_id": iteration["id"],
                "recovery_id": record["id"], "snapshot_digest": digest,
                "builder_run_id": builder_run["id"], **published,
            }
        except Exception:
            # The recovery was claimed before it ran. If it did not finish the task
            # would otherwise stay in a running state for ever, so the claim is
            # closed here rather than left open.
            self._close_open_recovery(task_id, iteration["id"])
            raise

    def _preserve_interrupted_work(
        self,
        task: dict[str, Any],
        iteration: dict[str, Any],
        builder_run: dict[str, Any],
    ) -> dict[str, Any]:
        """Commit the work an interrupted Builder left uncommitted, under the screen.

        The failure path only freezes work while a process is alive to run it. A
        crash stops before that, so the files are still loose in the worktree and
        the iteration records no commit — which would otherwise make the
        interrupted case inspectable but never recoverable.

        Discovery, screening, staging, and the commit cover the same exact set, the
        same way the live failure path does. Nothing is deleted, and the failure
        record is not rewritten: only the preserved change set is written, so the
        preserved content is identical to what an inspection of the same worktree
        reported before this ran.
        """

        engine = self.engine
        store = self.store
        task_id = task["id"]
        path = workspace_path_for(engine.workspace_root, task_id)
        recorded = iteration.get("workspace_path")
        if not is_worktree(path) or not recorded or Path(recorded).resolve() != path.resolve():
            self._refuse_recovery(
                "The task worktree is missing, so none of the interrupted work survived."
            )
        base_commit = iteration["base_commit"]
        try:
            names = changed_paths(path, base_commit)
            if not names:
                self._refuse_recovery(
                    "The interrupted run left no change set to preserve, so there is "
                    "nothing to authorize."
                )
            screen_change_set(path, names)
            commit = commit_worktree(
                path,
                COMMIT_MESSAGE.format(task=task_id, number=iteration["number"])
                + " [unfinished Builder work preserved after interruption]",
                expected=names,
                base_commit=base_commit,
            )
            changeset = inspect_change_set(
                path, base_commit=base_commit, reviewed_commit=commit
            )
        except WorkspaceError as error:
            self._refuse_recovery(
                "The work the interrupted run left behind could not be preserved "
                f"({error.code}: {error.message})"
            )

        store.update_iteration(
            iteration_id=iteration["id"], actor=engine.identity.current().id,
            event="recovery.preserved",
            fields={
                "commit_sha": changeset.head_commit,
                "workspace_digest": changeset.workspace_digest,
                "diff_excerpt": diff_excerpt(changeset.diff, DIFF_EXCERPT_CHARS),
            },
            detail={
                "commit": changeset.head_commit,
                "files": len(changeset.changed_files),
                "builder_run_id": builder_run["id"],
                "failure_reason": builder_run.get("failure_reason"),
            },
        )
        return store.iteration(task_id)

    def _close_open_recovery(self, task_id: str, iteration_id: str) -> None:
        """Close a claimed recovery that did not reach a decision of its own."""
        if self.engine._task(task_id)["status"] != "implementation_running":
            return
        self._record_failure(
            task_id, iteration_id,
            reason=REASON_RECOVERY_INCOMPLETE,
            detail=(
                "The authorised recovery stopped before it reached a decision. The "
                "preserved work is still committed on the task branch and the "
                "original failure record is unchanged."
            ),
            expect=("implementation_running",),
            iteration_status=ITERATION_FAILED,
        )

    def _recover_after_build(
        self,
        *,
        task: dict[str, Any],
        iteration: dict[str, Any],
        contract: AcceptanceContract,
        criteria_lines: tuple[str, ...],
        workspace: TaskWorkspace,
        changeset: ChangeSet,
        recovery: dict[str, Any],
    ) -> dict[str, Any]:
        """Validation, QA, and review for a recovered change set.

        The same three stages a normal iteration runs, in the same order, against
        the same records. The only difference is that the Builder prompt is never
        composed, no build report exists, and the QA and Reviewer prompts carry the
        recovery provenance so neither role can silently assume one was.
        """

        engine = self.engine
        store = self.store
        task_id = task["id"]
        iteration_id = iteration["id"]
        actor = engine.identity.current().id

        store.update_iteration(
            iteration_id=iteration_id, actor=actor, event="recovery.frozen",
            fields={
                "commit_sha": changeset.head_commit,
                "workspace_digest": changeset.workspace_digest,
                "diff_excerpt": diff_excerpt(changeset.diff, DIFF_EXCERPT_CHARS),
            },
            detail={
                "commit": changeset.head_commit,
                "files": len(changeset.changed_files),
                "snapshot_digest": recovery["snapshot_digest"],
                "builder_run_id": recovery["builder_run_id"],
            },
        )

        outcome = self._validate(
            workspace.path, iteration_id, task_id, actor, contract, changeset=changeset
        )
        if outcome is None:
            return self._record_failure(
                task_id, iteration_id, reason=REASON_VALIDATION_FAILED,
                detail="The recovered change set could not be validated.",
                expect=("implementation_running",),
                iteration_status=ITERATION_VALIDATION_FAILED,
            )
        if isinstance(outcome, dict):
            return outcome
        validation = outcome

        persona, _, selection = engine.persona_and_skills(task, QA)
        try:
            qa_prompt = compose_qa_prompt(
                persona=persona,
                task_title=task["title"],
                requirements=task["requirements"],
                acceptance_criteria=task["acceptance_criteria"],
                task_identifier=task_id,
                implementation_summary="",
                implementation_files=changeset.paths,
                diff_excerpt=diff_excerpt(changeset.diff, DIFF_EXCERPT_CHARS),
                validation_command=validation.command,
                validation_exit_code=validation.exit_code,
                validation_output=str(validation.output),
                approved_criteria=criteria_lines,
                skills=selection.documents,
                recovery=recovery,
            )
        except LoopPromptError as error:
            return self._record_failure(
                task_id, iteration_id, reason=error.code, detail=error.message,
                expect=("implementation_running",),
            )

        # QA runs through the same bounded, audited path a normal iteration uses, so
        # a recovered-Builder run cannot be held to a different standard of answer
        # or a different number of attempts.
        qa = self._run_qa(
            task, task_id=task_id, iteration_id=iteration_id,
            number=iteration["number"], contract=contract,
            workspace_path=workspace.path, qa_prompt=qa_prompt,
            selection=selection, actor=actor,
        )
        if isinstance(qa, dict):
            return qa

        try:
            review_prompt, review_selection = self._review_prompt(
                task, report=None, qa=qa, diff=changeset.diff, command=validation.command,
                exit_code=validation.exit_code, criteria_lines=criteria_lines,
                recovery=recovery,
            )
        except LoopPromptError as error:
            return self._record_failure(
                task_id, iteration_id, reason=error.code, detail=error.message,
                expect=("qa_running",),
            )

        store.update_iteration(
            iteration_id=iteration_id, actor=actor, event="iteration.review-started",
            fields={"status": ITERATION_REVIEW_RUNNING}, detail={"iteration": iteration["number"]},
        )
        review_outcome = engine.invoke_role(
            task=task, role=REVIEWER, composed=review_prompt, selection=review_selection,
            expect=("qa_running",), running="review_running",
            failed="review_failed", workspace_path=workspace.path, writable=False,
        )
        return self._finish_review(
            task_id=task_id, iteration_id=iteration_id, contract=contract,
            review_outcome=review_outcome, changeset=changeset, report=None, qa=qa,
            validation=validation, workspace=workspace,
        )

    def _recovery_obstacles(
        self,
        task: dict[str, Any],
        iteration: dict[str, Any] | None,
        builder_run: dict[str, Any] | None,
        changeset: ChangeSet | None,
        *,
        preserved: bool = False,
    ) -> list[str]:
        """Every reason the preserved work cannot be recovered, in one place.

        `inspect-recovery` reports these and `recover-builder` refuses on the same
        list, so what a human is shown and what the gate enforces cannot drift.

        ``preserved`` says the work set aside by an interrupted run has been read
        and screened but is not committed yet, which is the state a crash leaves
        behind: the commit is written by the preservation step, not by the failure.
        """

        store = self.store
        task_id = task["id"]
        obstacles: list[str] = []
        if task["status"] != "implementation_failed":
            obstacles.append(
                f"Task {task_id} is {task['status']}; only implementation_failed can be recovered."
            )
        if iteration is None:
            obstacles.append("The task has no iteration to recover.")
        else:
            if iteration["status"] != ITERATION_FAILED:
                obstacles.append(
                    f"Iteration {iteration['number']} is {iteration['status']}; "
                    "only a failed iteration can be recovered."
                )
            if not iteration.get("commit_sha") and not preserved:
                obstacles.append(
                    "The failed run never committed a change set and none of the work "
                    "left behind could be preserved, so there is nothing to authorize "
                    "here; a worktree that holds no change at all can be restarted with "
                    "`restart-builder`."
                )
            if iteration.get("build_report") or iteration.get("build_digest"):
                obstacles.append(
                    "This iteration already has a Builder report; a normal retry applies, "
                    "not a recovery."
                )
            if iteration["base_commit"] != task.get("repo_commit"):
                obstacles.append(
                    "The iteration base commit does not match the approved repository commit."
                )
        if builder_run is None:
            obstacles.append("No failed Builder run is recorded for this task.")
        else:
            if builder_run["status"] not in RECOVERABLE_RUN_STATUSES:
                obstacles.append(
                    f"The Builder run is {builder_run['status']}, not a failed run."
                )
            reason = builder_run.get("failure_reason")
            if reason not in RECOVERABLE_FAILURES:
                obstacles.append(
                    f"The Builder failed with {reason!r}; recovery applies only to runs that "
                    "stopped before answering, not to a refused or unparseable report."
                )
        if store.deliverable(task_id) is not None or store.acceptance(task_id) is not None:
            obstacles.append("A deliverable or delivery decision already exists.")
        used = len(
            [item for item in store.recoveries(task_id)
             if iteration is None or item["iteration_id"] == iteration["id"]]
        )
        if used >= MAX_BUILDER_RECOVERIES:
            obstacles.append(
                f"Iteration {iteration['number'] if iteration else '?'} already used its "
                f"{MAX_BUILDER_RECOVERIES} authorised recovery."
            )
        if changeset is not None and iteration is not None:
            if changeset.workspace_digest != iteration.get("workspace_digest"):
                obstacles.append(
                    "The preserved worktree content differs from the recorded change set."
                )
        obstacles.extend(self._approval_obstacles(task, iteration))
        return obstacles

    def _approval_obstacles(
        self, task: dict[str, Any], iteration: dict[str, Any] | None
    ) -> list[str]:
        """Approval and worktree-branch blockers, read-only.

        Shared by inspection and by recovery before any mutation, so a candidate is
        never reported eligible, or preserved, when authorization would refuse it.
        """

        store = self.store
        task_id = task["id"]
        found: list[str] = []
        proposal = store.proposal(task_id)
        approval = store.approval(task_id)
        if (
            proposal is None
            or approval is None
            or approval["decision"] != "approved"
            or proposal["status"] != "approved"
            or approval["proposal_id"] != proposal["id"]
            or approval["proposal_digest"] != proposal["digest"]
            or _document_digest(proposal["document"]) != proposal["digest"]
        ):
            found.append("The original approval no longer matches the proposal.")
        elif approval.get("repo_commit") not in (None, task["repo_commit"]):
            found.append(
                "The approved repository commit does not match the iteration base, so "
                "the preserved work would be recovered on top of the wrong commit."
            )
        if iteration is not None and iteration.get("branch"):
            path = workspace_path_for(self.engine.workspace_root, task_id)
            if is_worktree(path):
                try:
                    actual = current_branch(path)
                except WorkspaceError as error:
                    found.append(f"The worktree branch cannot be read ({error.code}).")
                else:
                    if actual != iteration["branch"]:
                        found.append(
                            f"The worktree has branch {actual!r} checked out, not the "
                            f"recorded {iteration['branch']!r}."
                        )
        return found

    @staticmethod
    def _failed_builder_run(runs: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
        """The Builder run that stopped, or ``None`` when the Builder succeeded."""

        builder = [
            run for run in runs
            if run["role"] == "builder" and run["status"] in RECOVERABLE_RUN_STATUSES
        ]
        return builder[-1] if builder else None

    def _recovery_note(
        self,
        *,
        task: dict[str, Any],
        iteration: dict[str, Any],
        builder_run: dict[str, Any],
        changeset: ChangeSet,
        digest: str,
        entries: Sequence[dict[str, str]],
    ) -> dict[str, Any]:
        """The provenance QA and the Reviewer are told, and the document records."""

        return {
            "failure_reason": builder_run.get("failure_reason"),
            "failure_detail": str(builder_run.get("failure_detail") or ""),
            "builder_run_id": builder_run["id"],
            "base_commit": changeset.base_commit,
            "head_commit": changeset.head_commit,
            "snapshot_digest": digest,
            "recovery_digest": changeset.workspace_digest,
            "changed_files": list(entries),
            "iteration": iteration["number"],
            "task": task["id"],
        }

    @staticmethod
    def _refuse_recovery(message: str) -> NoReturn:
        raise EngineError(REASON_RECOVERY_REFUSED, message)

    # -- human-authorised restart of an interrupted Builder ------------------
    def _restart_obstacles(
        self,
        task: dict[str, Any],
        iteration: dict[str, Any] | None,
        builder_run: dict[str, Any] | None,
        *,
        changed_names: Sequence[str] | None,
        worktree_ready: bool,
    ) -> list[str]:
        """Every reason an interrupted Builder cannot be restarted, in one place.

        `inspect-recovery` reports these and `restart-builder` refuses on the same
        list, so what a human is shown and what the gate enforces cannot drift.

        ``changed_names`` is the change set read from the task worktree, including
        untracked and ignored paths, or ``None`` when it could not be read. A
        restart is only ever available for a run that left nothing behind: as soon
        as any preserved work exists, ``recover-builder`` is the correct path and
        discarding it in favour of a fresh attempt would be a silent reset.
        """

        store = self.store
        task_id = task["id"]
        obstacles: list[str] = []
        if task["status"] != "implementation_failed":
            obstacles.append(
                f"Task {task_id} is {task['status']}; only implementation_failed can be "
                "restarted."
            )
        if iteration is None:
            obstacles.append("The task has no iteration to restart.")
        else:
            if iteration["status"] != ITERATION_FAILED:
                obstacles.append(
                    f"Iteration {iteration['number']} is {iteration['status']}; "
                    "only a failed iteration can be restarted."
                )
            if iteration.get("commit_sha"):
                obstacles.append(
                    "The failed iteration recorded a committed change set, so preserved "
                    "work exists; use `inspect-recovery` and `recover-builder` instead."
                )
            if iteration.get("build_report") or iteration.get("build_digest"):
                obstacles.append(
                    "This iteration already has a Builder report, so the Builder finished "
                    "its turn; a restart applies only to an interrupted run."
                )
            if iteration["base_commit"] != task.get("repo_commit"):
                obstacles.append(
                    "The iteration base commit is not the approved repository commit, so a "
                    "restart would not start from the authorized commit."
                )
        if builder_run is None:
            obstacles.append("No failed Builder run is recorded for this task.")
        else:
            if builder_run["status"] not in RECOVERABLE_RUN_STATUSES:
                obstacles.append(
                    f"The Builder run is {builder_run['status']}, not a failed run."
                )
            reason = builder_run.get("failure_reason")
            if reason not in RESTARTABLE_FAILURES:
                obstacles.append(
                    f"The Builder stopped with {reason!r}; a restart applies only to an "
                    "interruption that cut the run short "
                    f"({', '.join(RESTARTABLE_FAILURES)})."
                )
        if store.deliverable(task_id) is not None or store.acceptance(task_id) is not None:
            obstacles.append("A deliverable or delivery decision already exists.")
        # Only a failure that committed nothing can leave an empty worktree; when a
        # commit exists the refusal above already names the preserved work.
        if iteration is not None and not iteration.get("commit_sha"):
            if not worktree_ready:
                obstacles.append(
                    "The task worktree is missing, so the absence of changes cannot be "
                    "confirmed."
                )
            elif changed_names is None:
                obstacles.append(
                    "The task worktree cannot be inspected, so the absence of changes "
                    "cannot be confirmed."
                )
            elif changed_names:
                shown = ", ".join(changed_names[:5])
                extra = (
                    "" if len(changed_names) <= 5 else f" and {len(changed_names) - 5} more"
                )
                obstacles.append(
                    f"The worktree still holds {len(changed_names)} changed path(s): "
                    f"{shown}{extra}. Preserved work must be recovered, not discarded."
                )
        used = len(store.builder_restarts(task_id))
        if used >= MAX_BUILDER_RESTARTS:
            if task["status"] == "approved":
                obstacles.append(
                    "An authorized Builder restart is already recorded and the task is "
                    "approved; run `implement` to spend that authorization."
                )
            else:
                obstacles.append(
                    f"The task already used its {MAX_BUILDER_RESTARTS} authorized Builder "
                    "restart; a further attempt requires a new task."
                )
        obstacles.extend(self._approval_obstacles(task, iteration))
        return obstacles

    @staticmethod
    def _refuse_restart(message: str) -> NoReturn:
        raise EngineError(REASON_RESTART_REFUSED, message)

    def restart_builder(self, task_id: str, *, failed_run_id: str) -> dict[str, Any]:
        """Authorize one restart of an interrupted Builder that wrote nothing.

        The failed run, its failure reason, its iteration, and the audit trail that
        recorded them are never rewritten. What is added is a separately recorded
        authorization naming the failed run and iteration, the approved proposal
        and approval, the contract digests, and the digest of the empty change set
        it verified. The task returns to ``approved`` and the ordinary stage runs
        again, so validation, QA, the Reviewer, and the human delivery decision are
        unchanged.

        The authorization is written in the same transaction that re-checks every
        precondition, and it is unique per failed iteration, so a second process
        cannot authorize a second restart. The whole operation holds the task's
        recovery lease, so a concurrent repair pass cannot mistake the live restart
        for an interrupted stage.
        """

        engine = self.engine
        store = self.store
        task = engine._task(task_id)
        with store.recovery_lease(task_id):
            return self._restart_builder_under_lease(task, failed_run_id=failed_run_id)

    def _restart_builder_under_lease(
        self, task: dict[str, Any], *, failed_run_id: str
    ) -> dict[str, Any]:
        task_id = task["id"]
        engine = self.engine
        store = self.store
        # Fail closed before authorizing anything: `run` refuses without a sandbox,
        # and an authorization spent on a run that cannot start would burn the one
        # restart the task is allowed.
        if engine.config.sandbox is None:
            self._refuse_restart(
                "No sandbox is configured, so the Builder cannot be started safely."
            )
        iteration = store.iteration(task_id)
        runs = store.runs(task_id)
        builder_run = self._failed_builder_run(runs)
        if builder_run is not None and builder_run["id"] != failed_run_id:
            self._refuse_restart(
                f"The recorded failed Builder run is {builder_run['id']!r}, not "
                f"{failed_run_id!r}; restart exactly the run that was inspected."
            )

        # Read the worktree without touching it. `changed_paths` unions the tracked
        # diff with every untracked path, ignored ones included, so a single stray
        # file is enough to refuse.
        path = workspace_path_for(engine.workspace_root, task_id)
        worktree_ready = is_worktree(path)
        changed_names: list[str] | None = None
        if worktree_ready and iteration is not None and not iteration.get("commit_sha"):
            try:
                changed_names = changed_paths(path, iteration["base_commit"])
            except WorkspaceError:
                changed_names = None
        obstacles = self._restart_obstacles(
            task,
            iteration,
            builder_run,
            changed_names=changed_names,
            worktree_ready=worktree_ready,
        )
        if obstacles:
            self._refuse_restart(" ".join(obstacles))
        if iteration is None or builder_run is None:  # pragma: no cover - guarded above
            self._refuse_restart("The task has no failed Builder run and no iteration.")

        # The worktree must still be the trusted one: the recorded branch, the exact
        # approved commit, and that commit in the current history.
        try:
            resumed = resume_task_workspace(engine.workspace_root, task_id)
            if resumed.commit != iteration["base_commit"] or resumed.commit != task["repo_commit"]:
                self._refuse_restart(
                    f"The worktree is at {resumed.commit}, not the approved commit "
                    f"{task['repo_commit']}."
                )
            if not is_ancestor(path, task["repo_commit"], resumed.commit):
                self._refuse_restart(
                    "The approved commit is not in the worktree history, so the "
                    "worktree is not the trusted one."
                )
            digest, entries = recoverable_inventory(path, iteration["base_commit"])
            residual = changed_paths(path, iteration["base_commit"])
        except WorkspaceError as error:
            self._refuse_restart(
                f"The task worktree cannot be verified ({error.code}): {error.message}"
            )
        if entries or residual:
            self._refuse_restart(
                f"The worktree holds {len(residual)} changed path(s), so preserved work "
                "exists; use `inspect-recovery` and `recover-builder` instead."
            )

        contract, _criteria = self._contract(task_id)
        proposal = store.proposal(task_id)
        approval = store.approval(task_id)
        if proposal is None or approval is None:
            self._refuse_restart("The approved proposal or approval is missing.")
        record = store.authorize_builder_restart(
            task_id=task_id,
            failed_iteration_id=iteration["id"],
            failed_run_id=failed_run_id,
            actor=engine.identity.current().id,
            reason=None,
            base_commit=iteration["base_commit"],
            proposal_id=proposal["id"],
            proposal_digest=proposal["digest"],
            approval_id=approval["id"],
            contract_digest=contract_digest(contract),
            empty_snapshot_digest=digest,
            limit=MAX_BUILDER_RESTARTS,
        )

        # The authorization is recorded and the task is back at `approved`; the
        # unchanged stage opens a fresh iteration and appends a new Builder run. A
        # failure inside it is recorded by the stage itself, exactly as it would be
        # for any other iteration, and nothing here retries.
        outcome = self.run(task_id)
        return {
            "restart_id": record["id"],
            "restart_number": record["restart_number"],
            "failed_iteration_id": iteration["id"],
            "failed_run_id": failed_run_id,
            "failure_reason": record["failure_reason"],
            "base_commit": record["base_commit"],
            "empty_snapshot_digest": record["empty_snapshot_digest"],
            **outcome,
        }

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

    def _recorded_validation(
        self, iteration: dict[str, Any], contract: AcceptanceContract
    ) -> ValidationOutcome:
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
        coverage = record.get("verification_coverage")
        coverage_criteria = coverage.get("criteria") if isinstance(coverage, dict) else None
        if not isinstance(coverage_criteria, list):
            self._refuse("The recorded acceptance verification evidence is missing.")
        by_id = {
            item.get("id"): item
            for item in coverage_criteria
            if isinstance(item, dict)
        }
        if len(coverage_criteria) != len(contract.criteria) or len(by_id) != len(
            contract.criteria
        ):
            self._refuse("The recorded acceptance verification set is incomplete.")
        expected_source = record.get("workspace_digest")
        expected_image = record.get("sandbox_image_id")
        if (
            not isinstance(expected_source, str)
            or not isinstance(expected_image, str)
            or not expected_image.startswith("sha256:")
        ):
            self._refuse("The recorded validation is not bound to a source and image.")
        if any(item.verification.kind == "pytest" for item in contract.criteria):
            environment = coverage.get("environment_check")
            if (
                not isinstance(environment, dict)
                or environment.get("command")
                != ["python", "-m", "pytest", "--version"]
                or environment.get("status") != VALIDATION_STATUS_SUCCEEDED
                or environment.get("source_digest") != expected_source
                or environment.get("source_digest_after") != expected_source
                or environment.get("sandbox_image_id") != expected_image
            ):
                self._refuse(
                    "The recorded pytest environment check does not match the "
                    "validated source and sandbox image."
                )
        for criterion in contract.criteria:
            evidence = by_id.get(criterion.id)
            if (
                evidence is None
                or evidence.get("declared") != criterion.verification.model_dump(mode="json")
                or evidence.get("mandatory") != criterion.mandatory
            ):
                self._refuse(
                    f"The recorded acceptance verification for {criterion.id} is missing "
                    "or does not match the approved contract."
                )
            if not criterion.mandatory:
                continue
            if evidence.get("status") != VALIDATION_STATUS_SUCCEEDED:
                self._refuse(
                    f"The mandatory acceptance verification for {criterion.id} "
                    f"did not succeed ({evidence.get('failure_reason') or evidence.get('status')})."
                )
            if criterion.verification.kind != "pytest":
                self._refuse(
                    f"The mandatory acceptance verification for {criterion.id} has no "
                    "supported machine execution route."
                )
            selector = criterion.verification.selector
            expected_command = ["python", "-m", "pytest", "-q", selector]
            expected_collection = [
                "python", "-m", "pytest", "--collect-only", "-q", selector
            ]
            execution = evidence.get("execution")
            collection = evidence.get("collection")
            if (
                evidence.get("selector_exists") is not True
                or evidence.get("selector_executed") is not True
                or not isinstance(execution, dict)
                or execution.get("command") != expected_command
                or execution.get("status") != VALIDATION_STATUS_SUCCEEDED
                or execution.get("exit_code") != 0
                or execution.get("source_digest") != expected_source
                or execution.get("source_digest_after") != expected_source
                or execution.get("sandbox_image_id") != expected_image
                or execution.get("sandbox_image") != record.get("sandbox_image")
                or not execution.get("output_digest")
                or not isinstance(collection, dict)
                or collection.get("command") != expected_collection
                or collection.get("status") != VALIDATION_STATUS_SUCCEEDED
                or collection.get("exit_code") != 0
                or collection.get("source_digest") != expected_source
                or collection.get("source_digest_after") != expected_source
                or collection.get("sandbox_image_id") != expected_image
                or collection.get("sandbox_image") != record.get("sandbox_image")
                or not collection.get("output_digest")
            ):
                self._refuse(
                    f"The mandatory acceptance verification evidence for {criterion.id} "
                    "does not bind a successful exact-selector run to the validated source "
                    "and sandbox image."
                )
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
            verification_coverage=dict(coverage),
        )

    # -- one iteration -------------------------------------------------------
    def _run_qa(
        self,
        task: dict[str, Any],
        *,
        task_id: str,
        iteration_id: str,
        number: int,
        contract: AcceptanceContract,
        workspace_path: Path,
        qa_prompt: ComposedPrompt,
        selection: SkillSelection,
        actor: str,
    ) -> QaReport | dict[str, Any]:
        """Ask QA, and at most once more when the answer is unreadable.

        Both ways into QA come through here — the normal iteration and the
        human-authorised recovered-Builder run — so the attempt bound, the separate
        run records, the audit event, and the evidence QA reads cannot drift between
        them. A returned ``QaReport`` was recorded and passed; a returned ``dict`` is
        the already-recorded failure the caller must hand back unchanged.

        QA may be asked once more when its answer is unreadable. The evidence it
        reads, the change set, and the contract are unchanged: only the complaint
        about the answer's structure is added, so a second answer cannot be a second
        opinion manufactured out of new material. An answer that already declared an
        adverse verdict in any of the documents it offered is a decision and is never
        re-asked.
        """

        engine = self.engine
        self.store.update_iteration(
            iteration_id=iteration_id, actor=actor, event="iteration.qa-started",
            fields={"status": ITERATION_QA_RUNNING}, detail={"iteration": number},
        )
        qa_outcome: RoleOutcome | None = None
        qa: QaReport | None = None
        prompt = qa_prompt
        expect: tuple[str, ...] = ("implementation_running",)
        for attempt in range(1, MAX_QA_FORMAT_ATTEMPTS + 1):
            qa_outcome = engine.invoke_role(
                task=task, role=QA, composed=prompt, selection=selection,
                expect=expect, running="qa_running",
                failed="qa_failed", workspace_path=workspace_path, writable=False,
            )
            if not qa_outcome.succeeded:
                return self._failed_run(
                    qa_outcome, task_id, iteration_id, running="implementation_running"
                )
            try:
                qa = parse_qa_report(qa_outcome.response_text)
            except ReportError as error:
                self._fail_run(
                    qa_outcome, task_id, running="qa_running", failed="qa_failed",
                    reason=REASON_INVALID_QA_REPORT, detail=error.message,
                )
                detail = error.message
                # Every document this answer offered is read, not just the first
                # one: an answer that declares both `failed` and `passed` has made
                # an adverse decision, and the order it wrote them in must not
                # decide whether it earns a second look.
                adverse = [
                    verdict
                    for verdict in declared_verdicts(qa_outcome.response_text, role=QA)
                    if verdict in ADVERSE_QA_VERDICTS
                ]
                if adverse:
                    # The answer states its decision in a parseable payload and gets
                    # the body wrong. Re-asking would only invite a different
                    # verdict, so the declaration stands and the refusal is final.
                    detail = (
                        f"{detail} The answer declared the verdict {adverse[0]!r}, so "
                        "it is not asked again: a second answer could only reverse "
                        "that decision, not clarify it."
                    )
                if adverse or attempt == MAX_QA_FORMAT_ATTEMPTS:
                    return self._record_failure(
                        task_id, iteration_id, reason=REASON_INVALID_QA_REPORT,
                        detail=detail, expect=None, iteration_status=ITERATION_QA_FAILED,
                    )
                # The refused answer stays recorded on its own run; the correction
                # is a separate run, and the audit trail says why it exists.
                self.store.audit(
                    task_id, actor, QA_FORMAT_RETRY_EVENT,
                    {
                        "iteration_id": iteration_id, "attempt": attempt,
                        "max_attempts": MAX_QA_FORMAT_ATTEMPTS,
                        "failed_run_id": qa_outcome.run_id, "code": error.code,
                        "remedy": "format-correction",
                    },
                )
                expect = ("qa_failed",)
                prompt = with_format_correction(
                    qa_prompt, role="QA", detail=error.message
                )
                continue
            break
        if qa is None or qa_outcome is None:  # pragma: no cover - the loop assigns both
            raise EngineError(
                REASON_INVALID_QA_REPORT, "The QA report could not be recorded."
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
        return qa

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
            # The run already moved the task to implementation_failed, but the
            # worktree still holds whatever the Builder wrote before it stopped.
            # Preserve it so a human can examine and authorise exactly that work.
            error = builder.error
            return self._record_failure(
                task_id, iteration_id,
                reason=error.reason if error is not None else "run-failed",
                detail=error.detail if error is not None else "The invocation failed.",
                expect=None,
                extra=self._freeze_builder_work(workspace, task_id, iteration),
            )

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
        #    Discovery, screening, staging, and the commit describe the same set:
        #    staging runs with ``--force``, so anything discovery left out (an
        #    ignored file, say) would be committed without ever being screened.
        try:
            expected = changed_paths(workspace.path, iteration["base_commit"])
            screen_change_set(workspace.path, expected)
            commit = commit_worktree(
                workspace.path,
                COMMIT_MESSAGE.format(task=task_id, number=number),
                expected=expected,
                base_commit=iteration["base_commit"],
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
        outcome = self._validate(
            workspace.path, iteration_id, task_id, actor, contract, changeset=changeset
        )
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

        # 5b. QA goes through the same bounded, audited path on every route into it.
        qa = self._run_qa(
            task, task_id=task_id, iteration_id=iteration_id, number=number,
            contract=contract, workspace_path=workspace.path, qa_prompt=qa_prompt,
            selection=selection, actor=actor,
        )
        if isinstance(qa, dict):
            return qa

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
        report: BuildReport | None,
        qa: QaReport,
        diff: str,
        command: Sequence[str],
        exit_code: int | None,
        criteria_lines: Sequence[str],
        recovery: dict[str, Any] | None = None,
    ) -> tuple[Any, Any]:
        """Compose the Reviewer prompt from recorded evidence only."""

        persona, _, selection = self.engine.persona_and_skills(task, REVIEWER)
        prompt = compose_reviewer_prompt(
            persona=persona,
            task_title=task["title"],
            requirements=task["requirements"],
            acceptance_criteria=task["acceptance_criteria"],
            task_identifier=task["id"],
            implementation_summary=report.summary if report is not None else "",
            diff_excerpt=diff_excerpt(diff, DIFF_EXCERPT_CHARS),
            validation_command=command,
            validation_exit_code=exit_code,
            qa_verdict=qa.verdict,
            qa_summary=qa.summary,
            qa_findings=[finding.description for finding in qa.findings],
            approved_criteria=criteria_lines,
            skills=selection.documents,
            recovery=recovery,
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
        report: BuildReport | None,
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
        self,
        workspace_path: Any,
        iteration_id: str,
        task_id: str,
        actor: str,
        contract: AcceptanceContract,
        *,
        changeset: ChangeSet | None = None,
    ) -> ValidationOutcome | dict[str, Any] | None:
        """Plan and execute the validation command, recording the evidence.

        ``changeset`` binds the recorded execution to the exact commit that is being
        validated. A recovery always supplies it; the loop records it on the
        iteration once validation has run.
        """

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
        if record.succeeded:
            verification_coverage = execute_acceptance_verifications(
                contract,
                workspace_path,
                timeout_seconds=validation_config.timeout_seconds,
                sandbox=engine.config.sandbox,
                executor=engine.sandbox_executor,
                expected_source_digest=record.workspace_digest,
                expected_image_id=record.sandbox_image_id,
            )
        else:
            verification_coverage = {
                "environment_check": None,
                "criteria": [
                    {
                        "id": item.id,
                        "mandatory": item.mandatory,
                        "declared": item.verification.model_dump(mode="json"),
                        "status": "not_run",
                        "selector_exists": None,
                        "selector_executed": False,
                        "failure_reason": "general-validation-did-not-succeed",
                        "collection": None,
                        "execution": None,
                    }
                    for item in contract.criteria
                ],
            }
        record = dataclasses.replace(
            record, verification_coverage=verification_coverage
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
        if changeset is not None and record.workspace_digest != changeset.workspace_digest:
            return self._record_failure(
                task_id, iteration_id, reason=REASON_VALIDATION_FAILED,
                detail=(
                    "The validated source digest does not match the committed "
                    f"change set ({record.workspace_digest} != {changeset.workspace_digest})."
                ),
                expect=("implementation_running",),
                iteration_status=ITERATION_VALIDATION_FAILED,
            )
        failed_obligations = [
            item
            for item in record.verification_coverage.get("criteria", [])
            if item.get("mandatory") and item.get("status") != VALIDATION_STATUS_SUCCEEDED
        ]
        if failed_obligations:
            details = "; ".join(
                self._verification_failure_detail(item)
                for item in failed_obligations
            )
            return self._record_failure(
                task_id,
                iteration_id,
                reason=REASON_ACCEPTANCE_VERIFICATION_INCOMPLETE,
                detail=(
                    "Mandatory acceptance verification obligations lack required "
                    f"successful execution evidence: {details}."
                ),
                expect=("implementation_running",),
                iteration_status=ITERATION_VALIDATION_FAILED,
            )
        return record

    @staticmethod
    def _verification_failure_detail(item: dict[str, Any]) -> str:
        evidence = item.get("execution") or item.get("collection") or {}
        exit_code = evidence.get("exit_code") if isinstance(evidence, dict) else None
        reason = item.get("failure_reason") or item.get("status")
        obstacle = str(item.get("obstacle") or "").strip()
        return (
            f"{item['id']} ({item['status']}, exit {exit_code}, reason {reason})"
            + (f": {obstacle}" if obstacle else "")
        )

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
        report: BuildReport | None = outcome["report"]
        qa: QaReport = outcome["qa"]
        review: ReviewVerdict = outcome["review"]
        validation: ValidationOutcome = outcome["validation"]
        workspace = outcome["workspace"]
        actor = self.engine.identity.current().id
        task_id = task["id"]
        # Re-read the iteration: the builder/QA/Reviewer run identifiers were
        # recorded after the copy the loop was holding.
        iteration = self.store.iteration(task_id) or iteration
        recovery = self.store.recovery_for_iteration(iteration["id"])

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
            "verification_coverage": self._verification_coverage(validation),
            "builder": (
                {
                    "report": dataclasses.asdict(report),
                    "digest": report.digest,
                    "run_id": iteration.get("builder_run_id"),
                    "status": "reported",
                }
                if report is not None
                else {
                    # The Builder stopped before writing a report. Nothing is
                    # inferred, summarised, or simulated in its place.
                    "report": None,
                    "digest": None,
                    "run_id": recovery["builder_run_id"] if recovery else None,
                    "status": "not-recorded",
                    "reason": recovery["failure_reason"] if recovery else None,
                    "detail": recovery["failure_detail"] if recovery else None,
                    "tests_run": None,
                    "test_execution": "not-recorded",
                }
            ),
            "recovery": (
                None
                if recovery is None
                else {
                    "id": recovery["id"],
                    "authorized_by": recovery["actor"],
                    "authorized_at": recovery["created_at"],
                    "failed_builder_run_id": recovery["builder_run_id"],
                    "failure_reason": recovery["failure_reason"],
                    "failure_detail": recovery["failure_detail"],
                    "base_commit": recovery["base_commit"],
                    "head_commit": recovery["head_commit"],
                    "snapshot_digest": recovery["snapshot_digest"],
                    "recovery_digest": recovery["recovery_digest"],
                    "changed_files": recovery["changed_files"],
                    "contract_digest": recovery["contract_digest"],
                    "builder_report": "not-recorded",
                    "builder_tests_run": "not-recorded",
                    "acceptance": "pending-human-acceptance",
                }
            ),
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
        validation: ValidationOutcome,
    ) -> dict[str, Any]:
        """Bind each approved obligation to its recorded execution or explicit gap."""

        return {
            "executed_command": list(validation.command),
            "executed_exit_code": validation.exit_code,
            "environment_check": validation.verification_coverage.get(
                "environment_check"
            ),
            "criteria": validation.verification_coverage.get("criteria", []),
            "note": (
                "Only a successful exact-selector execution with matching source "
                "digest and sandbox image identity counts as verified. Unverified, "
                "unsupported, and not-run obligations remain explicit."
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

    def _freeze_builder_work(
        self, workspace: TaskWorkspace, task_id: str, iteration: dict[str, Any]
    ) -> dict[str, Any]:
        """Preserve whatever a failed Builder wrote, and say exactly what it is.

        The files are committed on the task branch under a message that says they
        are unfinished, so a later attempt that reuses the worktree cannot silently
        lose them and a human can put a single digest on exactly what survived.

        This is preservation, not acceptance. Nothing here is validated, QA never
        sees it, no build report is invented, and only `recover-builder` can take
        the work further. A change set that looks like a credential is left on disk
        untouched and is deliberately not committed, so it cannot be recovered.
        A freeze that fails changes nothing about the recorded failure.

        Discovery, screening, and the commit cover the same exact set: the commit
        is told which paths were screened and refuses anything else, including the
        ignored files that ``git add --force`` would otherwise sweep in.
        """

        path = workspace.path
        base_commit = iteration["base_commit"]
        try:
            names = changed_paths(path, base_commit)
            if not names:
                return {}
            screen_change_set(path, names)
            commit = commit_worktree(
                path,
                COMMIT_MESSAGE.format(task=task_id, number=iteration["number"])
                + " [unfinished Builder work preserved after failure]",
                expected=names,
                base_commit=base_commit,
            )
            changeset = inspect_change_set(path, base_commit=base_commit, reviewed_commit=commit)
        except WorkspaceError:
            return {}
        return {
            "commit_sha": changeset.head_commit,
            "workspace_digest": changeset.workspace_digest,
            "diff_excerpt": diff_excerpt(changeset.diff, DIFF_EXCERPT_CHARS),
        }

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
    "ADVERSE_QA_VERDICTS",
    "MAX_BUILDER_RECOVERIES",
    "MAX_BUILDER_RESTARTS",
    "MAX_QA_FORMAT_ATTEMPTS",
    "MAX_REVIEW_RETRIES",
    "QA_FORMAT_RETRY_EVENT",
    "RECOVERABLE_FAILURES",
    "RESTARTABLE_FAILURES",
    "REASON_BUILDER_INTERRUPTED",
    "REASON_RECOVERY_ELIGIBLE",
    "REASON_RECOVERY_INCOMPLETE",
    "REASON_RECOVERY_REFUSED",
    "REASON_RESTART_EXHAUSTED",
    "REASON_RESTART_REFUSED",
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
