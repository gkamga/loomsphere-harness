"""The standalone workflow: Intake -> Architect -> approval -> delivery.

Phase 2C.1 established Intake, Architect, and the explicit human approval gate.
Phase 2C.2 adds the stages that turn an approval into reviewed software: Builder,
executed validation, QA, Reviewer, and a second human decision on the deliverable.
The loop itself lives in ``loomsphere_harness.implementation``; this module owns the
configuration, persistence, identity, repository provider, prompt composition, and
the single place where a model invocation is dispatched and recorded.

Authority never comes from a model: the approvers are the local identity, an
approval must quote the SHA-256 digest of the exact stored proposal, a delivery
decision must quote the digest of the exact frozen deliverable, and only the
Builder role is granted a writable worktree.
"""

from __future__ import annotations

import dataclasses
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from loomsphere_harness.agent_catalog import AgentCatalogError, load_catalog
from loomsphere_harness.agent_runner import (
    AgentExecutionError,
    AgentInvocation,
    AgentRunResult,
)
from loomsphere_harness.architect_prompt import (
    ArchitectPromptError,
    ComposedPrompt,
    compose_architect_prompt,
    load_persona_text,
    with_format_correction,
)
from loomsphere_harness.assets import (
    AssetIntegrityError,
    CatalogSettings,
    TrustedAssets,
    verify_assets,
)
from loomsphere_harness.config import RuntimeConfig
from loomsphere_harness.harness_settings import bind_settings
from loomsphere_harness.approval import LocalApprovalGate
from loomsphere_harness.interfaces import (
    AgentRunner,
    ApprovalGate,
    ExecutionEnvironment,
    IdentityProvider,
    RepositoryProvider,
    TaskStore,
)
from loomsphere_harness.intake_prompt import compose_intake_prompt
from loomsphere_harness.lifecycle_hooks import (
    LifecycleHookBlocked,
    denying_hook,
    evaluate,
    load_registry,
)
from loomsphere_harness.schemas.lifecycle_hooks import (
    AFTER_AGENT_COMPLETION,
    BEFORE_AGENT_INVOCATION,
    HookDefinitionError,
    LifecycleEventPayload,
)
from loomsphere_harness.pydantic_harness import (
    PydanticHarnessRunner,
    runtime_identity,
)
from loomsphere_harness.schemas.architect_proposal import (
    ProposalValidationError,
    canonical_proposal,
    parse_architect_proposal,
    proposal_digest,
)
from loomsphere_harness.schemas.intake import (
    IntakeAssessment,
    IntakeReportError,
    canonical_intake,
    intake_digest,
    parse_intake_assessment,
)
from loomsphere_harness.skill_selection import (
    STAGE_FOR_ROLE,
    SkillSelection,
    TaskFacts,
    intake_facts,
    select_role_skills,
)
from loomsphere_harness.skills import SkillLoadError, declared_tools

INTAKE = "intake"
ARCHITECT = "architect"
BUILDER = "builder"
QA = "qa"
REVIEWER = "reviewer"
#: The stages this runtime executes, in workflow order.
STAGES = (INTAKE, ARCHITECT, BUILDER, QA, REVIEWER)
#: Stages a model performs during the implementation loop.
IMPLEMENTATION_STAGES = (BUILDER, QA, REVIEWER)
FAILURE_INVALID_INTAKE = "invalid-intake"
FAILURE_INVALID_PROPOSAL = "invalid-proposal"

#: The Intake and Architect stages may correct a formatting mistake once. A second
#: refusal is final: the stage fails with the classifier for its role and the human
#: decides what happens next. The bound is a constant, not a setting, so no
#: configuration can turn a formatting problem into an unbounded retry loop.
MAX_FORMAT_ATTEMPTS = 2
FORMAT_RETRY_EVENT = "format-retry"


@dataclass(frozen=True)
class RoleOutcome:
    """What one implementation-stage invocation produced.

    ``error`` is a recorded, fail-closed outcome: the run row already carries the
    failure, so the loop only has to decide what the iteration status becomes.
    """

    run_id: str
    result: AgentRunResult | None = None
    error: AgentExecutionError | None = None

    @property
    def succeeded(self) -> bool:
        return self.result is not None

    @property
    def response_text(self) -> str:
        return self.result.response_text if self.result is not None else ""

    @property
    def events(self) -> dict[str, object]:
        return self.result.events if self.result is not None else {}



class LocalReadOnlyEnvironment:
    """The target repository worktree, exposed to the model read-only."""

    writable = False

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()

    @property
    def workspace_path(self) -> Path:
        return self._path


class EngineError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def default_state_dir(repo_path: Path) -> Path:
    """A per-repository state directory outside the repository."""

    import hashlib

    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    token = hashlib.sha256(str(repo_path).encode("utf-8")).hexdigest()[:16]
    return base / "loomsphere-harness" / "projects" / token


def state_path(repo_path: Path, config: RuntimeConfig) -> Path:
    directory = config.state_dir or default_state_dir(repo_path)
    resolved = directory.expanduser().resolve()
    repo = repo_path.resolve()
    if resolved == repo or repo in resolved.parents:
        raise EngineError(
            "state-inside-repository",
            "The state directory must be outside the target repository.",
        )
    return resolved / "state.sqlite"


class Engine:
    def __init__(
        self,
        *,
        store: TaskStore,
        config: RuntimeConfig,
        repository: RepositoryProvider,
        identity: IdentityProvider,
        repo_path: Path,
        runner_factory: Callable[[Any], AgentRunner] | None = None,
        approvals: ApprovalGate | None = None,
        environment: ExecutionEnvironment | None = None,
        sandbox_executor: Any | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.repository = repository
        self.identity = identity
        self.repo_path = repo_path.resolve()
        self.environment = environment or LocalReadOnlyEnvironment(self.repo_path)
        if self.environment.writable:
            raise EngineError("environment-writable",
                              "Intake and Architect run only in a read-only environment.")
        self.approvals = approvals or LocalApprovalGate(
            store=store, identity=identity, repository=repository)
        # Test seam: the default is the real PydanticAI adapter.
        self._runner_factory = runner_factory or PydanticHarnessRunner
        # Test seam: the default is a container executor built from the settings.
        self.sandbox_executor = sandbox_executor

    # -- setup ---------------------------------------------------------------
    def _assets(self) -> TrustedAssets:
        try:
            assets = verify_assets(self.config.harness_root, target_repo=self.repo_path)
        except AssetIntegrityError as error:
            if self.store.exists():
                self.store.audit(None, self.identity.current().id, "assets.rejected",
                                 {"code": error.code})
            raise
        bind_settings(CatalogSettings(assets))
        return assets

    @property
    def workspace_root(self) -> Path:
        """Where task worktrees live: configured, else beside the state file."""

        if self.config.workspace_root is not None:
            return self.config.workspace_root.expanduser().resolve()
        return (self.store.path.parent / "workspaces").resolve()

    def initialize(self) -> dict[str, Any]:
        assets = self._assets()
        snapshot = self.repository.snapshot()
        actor = self.identity.current().id
        self.store.initialize(
            repo_path=str(self.repo_path),
            harness_root=str(assets.root),
            suite_digest=assets.suite_digest,
            actor=actor,
        )
        return {"repo": snapshot.path, "commit": snapshot.commit, "suite_digest": assets.suite_digest,
                "assets": assets.file_count, "state": str(self.store.path)}

    def recover(self) -> list[str]:
        self.store.check()
        return self.store.recover_interrupted(self.identity.current().id)

    # -- submit ----------------------------------------------------------------
    def submit(self, *, title: str, requirements: str, acceptance_criteria: str) -> dict[str, Any]:
        self.store.check()
        for label, value in (("title", title), ("requirements", requirements),
                             ("acceptance criteria", acceptance_criteria)):
            if not value.strip():
                raise EngineError("invalid-request", f"The {label} must not be blank.")
        snapshot = self.repository.snapshot()
        return self.store.create_task(
            title=title.strip(), requirements=requirements.strip(),
            acceptance_criteria=acceptance_criteria.strip(), repo_ref=snapshot.ref,
            repo_commit=snapshot.commit, repo_dirty=snapshot.dirty,
            actor=self.identity.current().id,
        )

    # -- stages ----------------------------------------------------------------
    def run(self, task_id: str) -> dict[str, Any]:
        """Run whichever of Intake and Architect the task needs, in order."""

        self.recover()
        task = self._task(task_id)
        if task["status"] in ("submitted", "intake_failed"):
            self.run_intake(task_id)
            task = self._task(task_id)
        if task["status"] in ("intake_complete", "architect_failed"):
            self.run_architect(task_id)
        return self.describe(task_id)

    def run_intake(self, task_id: str) -> None:
        self._stage(task_id, INTAKE, ("submitted", "intake_failed"), "intake_running", "intake_failed")

    def run_architect(self, task_id: str) -> None:
        self._stage(task_id, ARCHITECT, ("intake_complete", "architect_failed"),
                    "architect_running", "architect_failed")

    def _stage(self, task_id: str, role: str, expect: tuple[str, ...], running: str, failed: str) -> None:
        if role not in STAGES:  # pragma: no cover - guarded by callers
            raise EngineError("stage-not-supported", f"{role} cannot run in this runtime.")
        self.store.check()
        task = self._task(task_id)
        if task["status"] not in expect:
            raise EngineError("invalid-transition",
                              f"Task {task_id} is {task['status']}; cannot run {role}.")
        actor = self.identity.current().id
        assets = self._assets()

        snapshot = self.repository.snapshot()
        if snapshot.commit != task["repo_commit"]:
            self.store.audit(task_id, actor, "repository.changed",
                             {"recorded": task["repo_commit"], "current": snapshot.commit})
            raise EngineError(
                "repository-changed",
                f"HEAD moved from {task['repo_commit'][:12]} to {snapshot.commit[:12]} since "
                "the task was submitted; submit the requirement again.",
            )

        try:
            hooks = load_registry(assets.hooks_root)
        except HookDefinitionError as error:
            self.store.audit(task_id, actor, f"{role}.hook-registry-invalid", {"code": error.code})
            raise EngineError(error.code, f"The lifecycle hook registry is unusable: {error}") from error

        persona_path = assets.persona_path(role)
        try:
            composed, selection = self._compose(task, role, assets)
        except (AgentCatalogError, ArchitectPromptError, SkillLoadError, ValidationError) as error:
            code = getattr(error, "code", "prompt-composition-failed")
            self.store.audit(task_id, actor, f"{role}.composition-failed", {"code": str(code)})
            raise EngineError(str(code), f"The {role} prompt could not be composed: {error}") from error

        model_config = dataclasses.replace(self.config.model, workspace_root=str(self.environment.workspace_path))
        model_name, runtime = runtime_identity(model_config)
        catalog_digest = load_catalog(str(assets.catalog_root), str(assets.agents_root),
                                      str(assets.skills_root)).digest
        recorder = self._record_intake if role == INTAKE else self._record_proposal
        expect_status = expect
        prompt = composed
        for attempt in range(1, MAX_FORMAT_ATTEMPTS + 1):
            invocation = AgentInvocation(
                run_id=uuid.uuid4(),
                workspace_path=self.environment.workspace_path,
                prompt=prompt.text,
                timeout_seconds=self.config.timeout_seconds,
                read_only=True,
                allowed_tools=declared_tools(persona_path),
                role=role,
            )
            run_id = self.store.begin_run(
                task_id=task_id, role=role, expect=expect_status, running_status=running,
                actor=actor,
                evidence={
                    "prompt_digest": prompt.digest, "persona_digest": prompt.persona_digest,
                    "catalog_digest": catalog_digest,
                    "suite_digest": assets.suite_digest, "skill_selection": selection.evidence(),
                    "model": model_name, "runtime": runtime,
                },
            )
            try:
                self._dispatch_hooks(BEFORE_AGENT_INVOCATION, hooks, task, task_id, run_id, role,
                                     snapshot.commit, prompt.digest, selection, actor)
                result = self._runner_factory(model_config).run(invocation)
                self._dispatch_hooks(AFTER_AGENT_COMPLETION, hooks, task, task_id, run_id, role,
                                     snapshot.commit, prompt.digest, selection, actor)
            except AgentExecutionError as error:
                self.store.fail_run(run_id=run_id, task_id=task_id, failed_status=failed,
                                    running_status=running, actor=actor, reason=error.reason,
                                    detail=error.detail, response_text=error.response_text,
                                    events=error.events)
                return
            refusal = recorder(task_id, run_id, running, failed, actor, result)
            if refusal is None:
                return
            if attempt == MAX_FORMAT_ATTEMPTS:
                return
            # The refused attempt stays recorded exactly as it happened; the one
            # corrective attempt is a separate run, announced in the audit trail.
            self.store.audit(task_id, actor, f"{role}.{FORMAT_RETRY_EVENT}", {
                "role": role, "attempt": attempt, "max_attempts": MAX_FORMAT_ATTEMPTS,
                "failed_run_id": run_id, "code": refusal.code,
                "failure_detail": refusal.message[:500], "remedy": "format-correction",
            })
            expect_status = (failed,)
            prompt = with_format_correction(composed, role=role.capitalize(),
                                            detail=refusal.message)

    def _dispatch_hooks(self, event, hooks, task, task_id, run_id, role, commit, prompt_digest,
                        selection, actor) -> None:
        """Evaluate the trusted hooks for one invocation event; a hook can only block.

        The outcome is recorded in the hash-chained audit trail. With no hook
        registered (the shipped default) this records nothing and changes nothing.
        """
        if not hooks.for_event(event):
            return
        payload = LifecycleEventPayload(
            event=event,
            task_id=uuid.uuid5(uuid.NAMESPACE_URL, f"loomsphere-harness:{task_id}"),
            task_status=task["status"],
            stage=role,
            agent_role=role,
            agent_run_id=uuid.uuid5(uuid.NAMESPACE_URL, f"loomsphere-harness:{run_id}"),
            repository_commit=commit,
            prompt_digest=prompt_digest,
            skill_selection_digest=selection.evidence().get("digest"),
        )
        outcome = evaluate(payload, hooks)
        self.store.audit(task_id, actor, f"hook.{event}", {
            "registry_digest": hooks.digest, "run_id": run_id, "decision": outcome.decision,
            "executions": [
                {"hook": e.hook, "decision": e.decision, "blocked": e.blocked, "reason": e.reason}
                for e in outcome.executions
            ],
        })
        if outcome.blocked:
            raise LifecycleHookBlocked(event=event, hook=denying_hook(outcome), reason=outcome.reason)

    # -- implementation stages ----------------------------------------------------
    def persona_and_skills(
        self, task: dict[str, Any], role: str
    ) -> tuple[str, Path, SkillSelection]:
        """Load the role persona and the skills the task facts select."""

        assets = self._assets()
        definition = assets.persona_path(role)
        persona = load_persona_text(
            definition,
            role=role.capitalize(),
            missing_code=f"{role.upper()}_PERSONA_MISSING",
            empty_code=f"{role.upper()}_PERSONA_EMPTY",
        )
        facts = TaskFacts(task["title"], task["requirements"], task["acceptance_criteria"])
        selection = select_role_skills(
            facts, role=role, stage=STAGE_FOR_ROLE[role], definition_path=definition,
            skills_root=assets.skills_root, tools=declared_tools(definition),
        )
        return persona, definition, selection

    def invoke_role(
        self,
        *,
        task: dict[str, Any],
        role: str,
        composed: ComposedPrompt,
        selection: SkillSelection,
        expect: tuple[str, ...],
        running: str,
        failed: str,
        workspace_path: Path,
        writable: bool,
    ) -> RoleOutcome:
        """Invoke one implementation role and record its run.

        A failed invocation is recorded here and returned, never raised: the loop
        owns the iteration's status. A lifecycle hook that blocks still raises,
        because the run it guards must not be treated as ordinary evidence.
        """

        if role not in IMPLEMENTATION_STAGES:  # pragma: no cover - guarded by callers
            raise EngineError("stage-not-supported", f"{role} is not an implementation stage.")
        task_id = task["id"]
        actor = self.identity.current().id
        assets = self._assets()
        try:
            hooks = load_registry(assets.hooks_root)
        except HookDefinitionError as error:
            self.store.audit(task_id, actor, f"{role}.hook-registry-invalid", {"code": error.code})
            raise EngineError(error.code, f"The lifecycle hook registry is unusable: {error}") from error
        definition = assets.persona_path(role)
        model_config = dataclasses.replace(
            self.config.model, workspace_root=str(workspace_path)
        )
        model_name, runtime = runtime_identity(model_config)
        invocation = AgentInvocation(
            run_id=uuid.uuid4(),
            workspace_path=workspace_path,
            prompt=composed.text,
            timeout_seconds=self.config.timeout_seconds,
            read_only=not writable,
            allowed_tools=declared_tools(definition),
            role=role,
        )
        run_id = self.store.begin_run(
            task_id=task_id, role=role, expect=expect, running_status=running, actor=actor,
            evidence={
                "prompt_digest": composed.digest, "persona_digest": composed.persona_digest,
                "catalog_digest": load_catalog(str(assets.catalog_root), str(assets.agents_root),
                                               str(assets.skills_root)).digest,
                "suite_digest": assets.suite_digest, "skill_selection": selection.evidence(),
                "model": model_name, "runtime": runtime,
            },
        )
        snapshot = self.repository.snapshot()
        try:
            self._dispatch_hooks(BEFORE_AGENT_INVOCATION, hooks, task, task_id, run_id, role,
                                 snapshot.commit, composed.digest, selection, actor)
            result = self._runner_factory(model_config).run(invocation)
            self._dispatch_hooks(AFTER_AGENT_COMPLETION, hooks, task, task_id, run_id, role,
                                 snapshot.commit, composed.digest, selection, actor)
        except AgentExecutionError as error:
            self.store.fail_run(run_id=run_id, task_id=task_id, failed_status=failed,
                                running_status=running, actor=actor, reason=error.reason,
                                detail=error.detail, response_text=error.response_text,
                                events=error.events)
            return RoleOutcome(run_id=run_id, error=error)
        return RoleOutcome(run_id=run_id, result=result)

    def _record_intake(self, task_id, run_id, running, failed, actor, result
                       ) -> IntakeReportError | None:
        """Record the Intake assessment, or the refusal that stopped it."""

        try:
            assessment = parse_intake_assessment(result.response_text)
        except IntakeReportError as error:
            self.store.fail_run(run_id=run_id, task_id=task_id, failed_status=failed,
                                running_status=running, actor=actor, reason=FAILURE_INVALID_INTAKE,
                                detail=str(error)[:2000], response_text=result.response_text,
                                events=result.events)
            return error
        self.store.succeed_intake(
            run_id=run_id, task_id=task_id, response_text=result.response_text,
            events=result.events, digest=intake_digest(assessment),
            document=canonical_intake(assessment),
            needs_clarification=assessment.needs_clarification, actor=actor,
        )
        return None

    def _record_proposal(self, task_id, run_id, running, failed, actor, result
                         ) -> ProposalValidationError | None:
        """Record the Architect proposal, or the refusal that stopped it."""

        try:
            proposal = parse_architect_proposal(result.response_text)
        except ProposalValidationError as error:
            self.store.fail_run(run_id=run_id, task_id=task_id, failed_status=failed,
                                running_status=running, actor=actor, reason=FAILURE_INVALID_PROPOSAL,
                                detail=str(error)[:2000], response_text=result.response_text,
                                events=result.events)
            return error
        self.store.succeed_architect(
            run_id=run_id, task_id=task_id, response_text=result.response_text,
            events=result.events, digest=proposal_digest(proposal),
            document=canonical_proposal(proposal), actor=actor,
        )
        return None

    def _compose(self, task: dict[str, Any], role: str, assets: TrustedAssets
                 ) -> tuple[ComposedPrompt, SkillSelection]:
        definition = assets.persona_path(role)
        persona = load_persona_text(
            definition,
            role=role.capitalize(),
            missing_code=f"{role.upper()}_PERSONA_MISSING",
            empty_code=f"{role.upper()}_PERSONA_EMPTY",
        )
        facts = TaskFacts(task["title"], task["requirements"], task["acceptance_criteria"])
        stored = self.store.intake(task["id"]) if role == ARCHITECT else None
        assessment = IntakeAssessment.model_validate(stored["document"]) if stored else None
        context = intake_facts(assessment, digest=stored["digest"]) if stored else None
        selection = select_role_skills(
            facts, role=role, stage=STAGE_FOR_ROLE[role], definition_path=definition,
            skills_root=assets.skills_root, tools=declared_tools(definition), intake=context,
        )
        if role == INTAKE:
            catalog = load_catalog(str(assets.catalog_root), str(assets.agents_root),
                                   str(assets.skills_root))
            composed = compose_intake_prompt(
                persona=persona, task_title=task["title"], requirements=task["requirements"],
                acceptance_criteria=task["acceptance_criteria"], task_identifier=task["id"],
                repository_reference=task["repo_ref"], repository_commit=task["repo_commit"],
                specialists=tuple((entry.name, entry.label) for entry in catalog.entries),
                catalog_digest=catalog.digest, skills=selection.documents,
            )
        else:
            payload = assessment.model_dump(mode="json")
            composed = compose_architect_prompt(
                persona=persona, task_title=task["title"], requirements=task["requirements"],
                acceptance_criteria=task["acceptance_criteria"], task_identifier=task["id"],
                repository_reference=task["repo_ref"], repository_commit=task["repo_commit"],
                intake={
                    "assessment_id": stored["id"], "digest": stored["digest"],
                    "intent_summary": payload["intent"]["summary"],
                    "request_type": payload["intent"]["request_type"],
                    "intake_confidence": payload["intent"]["confidence"],
                    "risk_level": payload["risk"]["level"],
                    "implication_areas": assessment.implication_areas,
                    "affected_components": payload["affected_components"],
                    "implications": payload["implications"],
                    "ambiguities": payload["ambiguities"],
                    "specialist_recommendations": payload["specialist_recommendations"],
                    "clarifications_resolved": False,
                    "clarification_answers": [],
                },
                skills=selection.documents,
            )
        return composed, selection

    # -- approval ----------------------------------------------------------------
    def approve(self, task_id: str, *, proposal_digest: str) -> dict[str, Any]:
        self.recover()
        self._task(task_id)
        return self.approvals.approve(task_id, proposal_digest=proposal_digest)

    def reject(self, task_id: str, *, proposal_digest: str, reason: str) -> dict[str, Any]:
        if not reason.strip():
            raise EngineError("reason-required", "A rejection needs a reason.")
        self.recover()
        self._task(task_id)
        return self.approvals.reject(task_id, proposal_digest=proposal_digest, reason=reason)

    # -- implementation, review, and human acceptance -----------------------------
    def implement(self, task_id: str) -> dict[str, Any]:
        """Run the approved implementation loop: Builder, validation, QA, Reviewer."""

        from loomsphere_harness.implementation import ImplementationStage

        self.recover()
        outcome = ImplementationStage(self).run(task_id)
        return {"implementation": outcome, **self.describe(task_id)}

    def retry_review(self, task_id: str) -> dict[str, Any]:
        """Re-run only the Reviewer after a report-format failure, under strict limits."""

        from loomsphere_harness.implementation import ImplementationStage

        self.recover()
        outcome = ImplementationStage(self).retry_review(task_id)
        return {"implementation": outcome, **self.describe(task_id)}

    def inspect_recovery(self, task_id: str) -> dict[str, Any]:
        """Report what a failed Builder preserved, without changing anything.

        Deliberately does not call `recover()`. Reaping interrupted runs writes run
        and task statuses and appends audit events, so an inspection that reaped
        first would mutate the state it promises to describe and would make a
        read-only command depend on which process happened to run it.
        """

        from loomsphere_harness.implementation import ImplementationStage

        inspection = ImplementationStage(self).inspect_recovery(task_id)
        return {"recovery": inspection, **self.describe(task_id)}

    def recover_builder(self, task_id: str, *, snapshot_digest: str) -> dict[str, Any]:
        """Recover a failed Builder's preserved change set under human authorization."""

        from loomsphere_harness.implementation import ImplementationStage

        self.recover()
        outcome = ImplementationStage(self).recover_builder(
            task_id, snapshot_digest=snapshot_digest
        )
        return {"implementation": outcome, **self.describe(task_id)}

    def accept_delivery(
        self, task_id: str, *, deliverable_digest: str, reason: str | None = None
    ) -> dict[str, Any]:
        """Record a human acceptance of the frozen deliverable."""

        self.recover()
        self._task(task_id)
        return self.store.decide_delivery(
            task_id=task_id, deliverable_digest=deliverable_digest, decision="accepted",
            actor=self.identity.current().id, reason=reason,
        )

    def reject_delivery(
        self, task_id: str, *, deliverable_digest: str, reason: str
    ) -> dict[str, Any]:
        """Record a human rejection of a deliverable with a required reason."""

        if not reason.strip():
            raise EngineError("reason-required", "A rejection needs a reason.")
        self.recover()
        self._task(task_id)
        return self.store.decide_delivery(
            task_id=task_id, deliverable_digest=deliverable_digest, decision="rejected",
            actor=self.identity.current().id, reason=reason,
        )

    # -- read model ----------------------------------------------------------------
    def _task(self, task_id: str) -> dict[str, Any]:
        task = self.store.task(task_id)
        if task is None:
            raise EngineError("task-not-found", f"No task {task_id}.")
        return task

    def describe(self, task_id: str) -> dict[str, Any]:
        task = self._task(task_id)
        proposal = self.store.proposal(task_id)
        return {
            "task": task,
            "intake": self.store.intake(task_id),
            "proposal": proposal,
            "approval": self.store.approval(task_id),
            "iterations": self.store.iterations(task_id),
            "deliverable": self.store.deliverable(task_id),
            "acceptance": self.store.acceptance(task_id),
            "runs": [
                {k: v for k, v in run.items() if k != "response_text"}
                for run in self.store.runs(task_id)
            ],
            "audit": self.store.audit_events(task_id),
        }
