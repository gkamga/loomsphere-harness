"""Intake -> Architect -> explicit human approval, and nothing after it.

This is the whole standalone workflow of phase 2C.1. It reuses the LoomSphere
prompt composers, skill selection, catalog, schemas, and PydanticAI runner, and
adds only what a portable runtime must own: configuration, SQLite persistence, a
local identity, a Git repository provider, and the approval gate. There is no
Builder, QA, or Reviewer stage here and no code path that starts one; the run table
accepts only the ``intake`` and ``architect`` roles.

Authority never comes from a model: the approver is the local identity, an approval
must quote the SHA-256 digest of the exact stored proposal, and the model holds only
read-only file tools.
"""

from __future__ import annotations

import dataclasses
import os
import uuid
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from loomsphere_harness.agent_catalog import AgentCatalogError, load_catalog
from loomsphere_harness.agent_runner import AgentExecutionError, AgentInvocation
from loomsphere_harness.architect_prompt import (
    ArchitectPromptError,
    ComposedPrompt,
    compose_architect_prompt,
    load_persona_text,
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
#: The only stages this runtime can execute.
STAGES = (INTAKE, ARCHITECT)
FAILURE_INVALID_INTAKE = "invalid-intake"
FAILURE_INVALID_PROPOSAL = "invalid-proposal"


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
        invocation = AgentInvocation(
            run_id=uuid.uuid4(),
            workspace_path=self.environment.workspace_path,
            prompt=composed.text,
            timeout_seconds=self.config.timeout_seconds,
            read_only=True,
            allowed_tools=declared_tools(persona_path),
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
            return
        if role == INTAKE:
            self._record_intake(task_id, run_id, running, failed, actor, result)
        else:
            self._record_proposal(task_id, run_id, running, failed, actor, result)

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

    def _record_intake(self, task_id, run_id, running, failed, actor, result) -> None:
        try:
            assessment = parse_intake_assessment(result.response_text)
        except IntakeReportError as error:
            self.store.fail_run(run_id=run_id, task_id=task_id, failed_status=failed,
                                running_status=running, actor=actor, reason=FAILURE_INVALID_INTAKE,
                                detail=str(error)[:2000], response_text=result.response_text,
                                events=result.events)
            return
        self.store.succeed_intake(
            run_id=run_id, task_id=task_id, response_text=result.response_text,
            events=result.events, digest=intake_digest(assessment),
            document=canonical_intake(assessment),
            needs_clarification=assessment.needs_clarification, actor=actor,
        )

    def _record_proposal(self, task_id, run_id, running, failed, actor, result) -> None:
        try:
            proposal = parse_architect_proposal(result.response_text)
        except ProposalValidationError as error:
            self.store.fail_run(run_id=run_id, task_id=task_id, failed_status=failed,
                                running_status=running, actor=actor, reason=FAILURE_INVALID_PROPOSAL,
                                detail=str(error)[:2000], response_text=result.response_text,
                                events=result.events)
            return
        self.store.succeed_architect(
            run_id=run_id, task_id=task_id, response_text=result.response_text,
            events=result.events, digest=proposal_digest(proposal),
            document=canonical_proposal(proposal), actor=actor,
        )

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
            "runs": [
                {k: v for k, v in run.items() if k != "response_text"}
                for run in self.store.runs(task_id)
            ],
            "audit": self.store.audit_events(task_id),
        }
