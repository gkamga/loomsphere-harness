"""Typed contracts for lifecycle hooks: events, definitions, and outcomes.

A lifecycle hook is a small, declarative policy that LoomSphere evaluates at one of
four fixed points in the SDLC. Hooks exist so an operator can observe or restrict
progression without editing the orchestrator, and they are deliberately not a plugin
system: a hook is a JSON document that selects checks from the factory's own check
vocabulary. There is no uploaded code, no marketplace, and no second orchestrator.

Three properties make the mechanism safe to rely on:

* **Typed payloads.** Every event carries the identifiers a decision may be based on:
  the task, the stage, the agent run, the recorded evidence rows, the commit, and the
  position in the loop. A hook cannot read anything that is not in the payload.
* **Explicit authorization.** A hook declares the capabilities it uses, and every
  check declares the capabilities it needs. A definition that asks for a check it did
  not authorize is refused when the registry is loaded, not silently narrowed.
* **One-directional authority.** A hook may return `continue` or `block`. It can never
  return "allow": the factory's mandatory gates run first and independently, so a hook
  can only add a refusal. An observer hook cannot block at all.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: The four extension points. Each one sits at a place the orchestrator already
#: decided is a transition, so no hook can invent a new transition.
BEFORE_AGENT_INVOCATION = "before-agent-invocation"
AFTER_AGENT_COMPLETION = "after-agent-completion"
BEFORE_REVIEW_PROGRESSION = "before-review-progression"
BEFORE_DELIVERY = "before-delivery"

EVENT_TYPES: tuple[str, ...] = (
    BEFORE_AGENT_INVOCATION,
    AFTER_AGENT_COMPLETION,
    BEFORE_REVIEW_PROGRESSION,
    BEFORE_DELIVERY,
)

#: A hook either runs before an agent, after an agent, or before a transition that
#: moves the task forward.
AGENT_EVENTS: tuple[str, ...] = (BEFORE_AGENT_INVOCATION, AFTER_AGENT_COMPLETION)
TRANSITION_EVENTS: tuple[str, ...] = (BEFORE_REVIEW_PROGRESSION, BEFORE_DELIVERY)

#: What a hook may read. The dispatcher refuses a check that needs a capability the
#: hook did not declare, so the list is the hook's whole view of the factory.
CAP_READ_TASK = "read-task"
CAP_READ_RUN = "read-run"
CAP_READ_ROUTE = "read-route"
CAP_READ_CHANGES = "read-changes"
CAP_READ_EVIDENCE = "read-evidence"

CAPABILITIES: tuple[str, ...] = (
    CAP_READ_TASK,
    CAP_READ_RUN,
    CAP_READ_ROUTE,
    CAP_READ_CHANGES,
    CAP_READ_EVIDENCE,
)

#: The checks a hook may compose. Each entry lists the capabilities it needs. This
#: tuple is the whole vocabulary: adding a check is a code change with tests, whereas
#: adding a hook that uses one is a data change.
CHECK_NOTE = "note"
CHECK_TASK_STATUS = "task-status"
CHECK_FORBIDDEN_CHANGED_PATHS = "forbidden-changed-paths"
CHECK_REQUIRED_EVIDENCE = "required-evidence"
CHECK_REQUIRED_SPECIALIST_VERDICT = "required-specialist-verdict"

CHECK_CAPABILITIES: dict[str, tuple[str, ...]] = {
    CHECK_NOTE: (),
    CHECK_TASK_STATUS: (CAP_READ_TASK,),
    CHECK_FORBIDDEN_CHANGED_PATHS: (CAP_READ_CHANGES,),
    CHECK_REQUIRED_EVIDENCE: (CAP_READ_EVIDENCE,),
    CHECK_REQUIRED_SPECIALIST_VERDICT: (CAP_READ_ROUTE, CAP_READ_EVIDENCE),
}

CHECK_KINDS: tuple[str, ...] = tuple(CHECK_CAPABILITIES)

#: What the recorded evidence of a run, an implementation, or a delivery can be.
#: `required-evidence` names these; the dispatcher resolves them from stored rows.
EVIDENCE_KINDS: tuple[str, ...] = (
    "implementation",
    "validation",
    "qa",
    "review",
    "specialist-reviews",
    "acceptance-audit",
    "skill-selection",
)

#: Observer hooks record what they saw; blocking hooks may refuse a transition.
MODE_OBSERVER = "observer"
MODE_BLOCKING = "blocking"
MODES: tuple[str, ...] = (MODE_OBSERVER, MODE_BLOCKING)

DECISION_CONTINUE = "continue"
DECISION_BLOCK = "block"
DECISIONS: tuple[str, ...] = (DECISION_CONTINUE, DECISION_BLOCK)

#: Bounds that keep a definition and its evidence small enough to inspect.
MAX_HOOKS_PER_EVENT = 32
MAX_CHECKS_PER_HOOK = 16
MAX_PATTERNS_PER_CHECK = 32
MAX_OBSERVATIONS = 32
MIN_REASON_LENGTH = 20
HOOK_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+){0,7}$")
VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){0,2}$")

#: The definition format this build understands.
HOOK_SCHEMA_VERSION = 1


class HookDefinitionError(ValueError):
    """A hook definition is unusable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HookCheck(_Strict):
    """One declarative check a hook applies to an event."""

    kind: str
    #: Human-readable reason used when the check prevents a transition.
    reason: str = Field(default="", max_length=500)
    #: task-status
    allowed_statuses: list[str] = Field(default_factory=list, max_length=32)
    forbidden_statuses: list[str] = Field(default_factory=list, max_length=32)
    #: forbidden-changed-paths
    path_patterns: list[str] = Field(default_factory=list, max_length=MAX_PATTERNS_PER_CHECK)
    #: required-evidence
    evidence: list[str] = Field(default_factory=list, max_length=len(EVIDENCE_KINDS))
    #: required-specialist-verdict
    specialists: list[str] = Field(default_factory=list, max_length=8)
    #: note
    text: str = Field(default="", max_length=500)

    @field_validator("kind")
    @classmethod
    def known_kind(cls, value: str) -> str:
        if value not in CHECK_CAPABILITIES:
            raise ValueError(f"unknown check kind '{value}'")
        return value

    @field_validator("path_patterns")
    @classmethod
    def valid_patterns(cls, values: list[str]) -> list[str]:
        compiled: list[str] = []
        for value in values:
            if not value or len(value) > 200:
                raise ValueError("a path pattern must be 1-200 characters")
            try:
                re.compile(value)
            except re.error as error:
                raise ValueError(f"invalid path pattern '{value}': {error}") from error
            compiled.append(value)
        return compiled

    @field_validator("evidence")
    @classmethod
    def known_evidence(cls, values: list[str]) -> list[str]:
        for value in values:
            if value not in EVIDENCE_KINDS:
                raise ValueError(f"unknown evidence kind '{value}'")
        return values

    @field_validator(
        "allowed_statuses", "forbidden_statuses", "specialists", "evidence"
    )
    @classmethod
    def unique_values(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("values must be unique")
        return values

    @field_validator("specialists")
    @classmethod
    def valid_specialist_names(cls, values: list[str]) -> list[str]:
        for value in values:
            if not HOOK_NAME_PATTERN.fullmatch(value):
                raise ValueError(f"'{value}' is not a catalog specialist name")
        return values

    @model_validator(mode="after")
    def use_only_its_own_fields(self) -> HookCheck:
        used = {
            CHECK_TASK_STATUS: {"allowed_statuses", "forbidden_statuses"},
            CHECK_FORBIDDEN_CHANGED_PATHS: {"path_patterns"},
            CHECK_REQUIRED_EVIDENCE: {"evidence"},
            CHECK_REQUIRED_SPECIALIST_VERDICT: {"specialists"},
            CHECK_NOTE: {"text"},
        }[self.kind]
        stray = [
            name
            for name in (
                "allowed_statuses",
                "forbidden_statuses",
                "path_patterns",
                "evidence",
                "specialists",
                "text",
            )
            if name not in used and getattr(self, name)
        ]
        if stray:
            raise ValueError(
                f"check '{self.kind}' does not use {', '.join(sorted(stray))}"
            )
        if not self.reason and self.kind != CHECK_NOTE:
            raise ValueError("every non-note check needs a reason")
        if self.kind == CHECK_NOTE and not self.text:
            raise ValueError("a note check needs text")
        if self.kind == CHECK_TASK_STATUS and not (
            self.allowed_statuses or self.forbidden_statuses
        ):
            raise ValueError("a task-status check needs allowed or forbidden statuses")
        if self.kind == CHECK_FORBIDDEN_CHANGED_PATHS and not self.path_patterns:
            raise ValueError("a forbidden-changed-paths check needs path_patterns")
        if self.kind == CHECK_REQUIRED_EVIDENCE and not self.evidence:
            raise ValueError("a required-evidence check needs evidence kinds")
        if self.kind == CHECK_REQUIRED_SPECIALIST_VERDICT and not self.specialists:
            raise ValueError("a required-specialist-verdict check needs specialists")
        return self

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        return CHECK_CAPABILITIES[self.kind]


class HookDefinition(_Strict):
    """One operator-authored extension, as it appears in the trusted root."""

    hook_version: int
    name: str
    version: str
    label: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=20, max_length=2000)
    events: list[str] = Field(min_length=1, max_length=len(EVENT_TYPES))
    priority: int = Field(ge=0, le=10000)
    mode: str
    capabilities: list[str] = Field(default_factory=list, max_length=len(CAPABILITIES))
    checks: list[HookCheck] = Field(min_length=1, max_length=MAX_CHECKS_PER_HOOK)

    @field_validator("hook_version")
    @classmethod
    def supported_schema(cls, value: int) -> int:
        if value != HOOK_SCHEMA_VERSION:
            raise ValueError(
                f"this build understands hook_version {HOOK_SCHEMA_VERSION}"
            )
        return value

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not HOOK_NAME_PATTERN.fullmatch(value):
            raise ValueError("a hook name must be lower-case kebab-case")
        return value

    @field_validator("version")
    @classmethod
    def valid_version(cls, value: str) -> str:
        if not VERSION_PATTERN.fullmatch(value):
            raise ValueError("a hook version must look like 1 or 1.2 or 1.2.3")
        return value

    @field_validator("events")
    @classmethod
    def known_events(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("events must be unique")
        for value in values:
            if value not in EVENT_TYPES:
                raise ValueError(f"unknown lifecycle event '{value}'")
        return values

    @field_validator("mode")
    @classmethod
    def known_mode(cls, value: str) -> str:
        if value not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        return value

    @field_validator("capabilities")
    @classmethod
    def known_capabilities(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("capabilities must be unique")
        for value in values:
            if value not in CAPABILITIES:
                raise ValueError(f"unknown capability '{value}'")
        return values

    @model_validator(mode="after")
    def authorize_its_checks(self) -> HookDefinition:
        granted = set(self.capabilities)
        for check in self.checks:
            missing = sorted(set(check.required_capabilities) - granted)
            if missing:
                raise ValueError(
                    f"check '{check.kind}' needs the capability "
                    f"{', '.join(missing)}, which the hook does not declare"
                )
        # An observer may run any check: a failed check becomes an observation.
        # What it can never do is deny, which the dispatcher enforces by never
        # turning an observer's finding into a block decision.
        return self

    @property
    def blocking(self) -> bool:
        return self.mode == MODE_BLOCKING


class LifecycleEventPayload(_Strict):
    """The typed facts one event carries.

    Everything a hook may read is here, and nothing else is: the dispatcher builds
    the payload from stored rows at the moment of the event.
    """

    event: str
    task_id: UUID
    task_status: str
    intake_policy: str | None = None
    #: The stage the event belongs to: a factory stage name or a catalog stage.
    stage: str | None = None
    agent_role: str | None = None
    agent_run_id: UUID | None = None
    factory_job_id: UUID | None = None
    iteration: int | None = None
    agent_attempt: int | None = None
    repository_ref: str | None = None
    repository_commit: str | None = None
    prompt_digest: str | None = None
    skill_selection_digest: str | None = None
    route_digest: str | None = None
    required_specialists: list[str] = Field(default_factory=list)
    implementation_run_id: UUID | None = None
    validation_run_id: UUID | None = None
    qa_review_id: UUID | None = None
    reviewer_review_id: UUID | None = None
    acceptance_audit_id: UUID | None = None
    changed_paths: list[str] = Field(default_factory=list)
    #: Which evidence kinds are recorded for this subject right now, from the
    #: EVIDENCE_KINDS vocabulary. A hook reads this rather than querying anything.
    available_evidence: list[str] = Field(default_factory=list)
    #: Specialists with an approving verdict bound to the current implementation.
    approving_specialists: list[str] = Field(default_factory=list)
    delivery_repository: str | None = None
    delivery_branch: str | None = None

    @field_validator("event")
    @classmethod
    def known_event(cls, value: str) -> str:
        if value not in EVENT_TYPES:
            raise ValueError(f"unknown lifecycle event '{value}'")
        return value

    def as_document(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @property
    def subject_key(self) -> str:
        """A stable identity for the decision this event asks for.

        Two deliveries of the same event produce the same key, so a retry reuses the
        recorded outcome instead of deciding twice. A new attempt gets a new key,
        because a new agent run or implementation really is a new decision.
        """

        parts = [self.event, f"task:{self.task_id}"]
        if self.agent_run_id is not None:
            parts.append(f"run:{self.agent_run_id}")
        if self.implementation_run_id is not None:
            parts.append(f"implementation:{self.implementation_run_id}")
        if self.event == BEFORE_DELIVERY and self.repository_commit:
            parts.append(f"commit:{self.repository_commit}")
        return "|".join(parts)


class HookFinding(BaseModel):
    """The result of one check, recorded on the execution row."""

    kind: str
    passed: bool
    detail: str


class HookExecutionOutcome(BaseModel):
    """What one hook decided at one event."""

    hook: str
    version: str
    mode: str
    priority: int
    decision: str
    reason: str = ""
    findings: list[HookFinding] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    reused: bool = False

    @property
    def blocked(self) -> bool:
        return self.decision == DECISION_BLOCK


class HookOutcome(BaseModel):
    """The aggregate decision for one event, across every registered hook."""

    event: str
    subject_key: str
    decision: str = DECISION_CONTINUE
    reason: str = ""
    executions: list[HookExecutionOutcome] = Field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.decision == DECISION_BLOCK

    @property
    def observations(self) -> list[str]:
        return [
            observation
            for execution in self.executions
            for observation in execution.observations
        ]


__all__ = [
    "AFTER_AGENT_COMPLETION",
    "AGENT_EVENTS",
    "BEFORE_AGENT_INVOCATION",
    "BEFORE_DELIVERY",
    "BEFORE_REVIEW_PROGRESSION",
    "CAPABILITIES",
    "CAP_READ_CHANGES",
    "CAP_READ_EVIDENCE",
    "CAP_READ_ROUTE",
    "CAP_READ_RUN",
    "CAP_READ_TASK",
    "CHECK_CAPABILITIES",
    "CHECK_FORBIDDEN_CHANGED_PATHS",
    "CHECK_KINDS",
    "CHECK_NOTE",
    "CHECK_REQUIRED_EVIDENCE",
    "CHECK_REQUIRED_SPECIALIST_VERDICT",
    "CHECK_TASK_STATUS",
    "DECISION_BLOCK",
    "DECISION_CONTINUE",
    "DECISIONS",
    "EVENT_TYPES",
    "EVIDENCE_KINDS",
    "HOOK_SCHEMA_VERSION",
    "HookCheck",
    "HookDefinition",
    "HookDefinitionError",
    "HookExecutionOutcome",
    "HookFinding",
    "HookOutcome",
    "LifecycleEventPayload",
    "MODE_BLOCKING",
    "MODE_OBSERVER",
    "MODES",
    "MAX_HOOKS_PER_EVENT",
    "MAX_OBSERVATIONS",
    "TRANSITION_EVENTS",
];
