"""Portable lifecycle-hook loader and evaluator.

Extracted from the LoomSphere application's ``lifecycle_hooks`` module: the registry
loader, the pure check evaluators, and the invocation-event decision. The
application-owned parts (SQLAlchemy payload construction from stored rows, the
durable ``lifecycle_hook_executions`` table, and the review-progression and delivery
guards) stay in the application; they have no meaning for Intake -> Architect.

A hook cannot allow anything: it can only turn an event into ``continue`` or
``block``. Every mandatory gate of the runtime runs independently of it. The
registry is loaded from the trusted harness root (``.github/hooks``), never from the
target repository.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from loomsphere_harness.agent_runner import AgentExecutionError
from loomsphere_harness.schemas.lifecycle_hooks import (
    AFTER_AGENT_COMPLETION,
    BEFORE_AGENT_INVOCATION,
    BEFORE_DELIVERY,
    BEFORE_REVIEW_PROGRESSION,
    CHECK_FORBIDDEN_CHANGED_PATHS,
    CHECK_NOTE,
    CHECK_REQUIRED_EVIDENCE,
    CHECK_REQUIRED_SPECIALIST_VERDICT,
    CHECK_TASK_STATUS,
    DECISION_BLOCK,
    DECISION_CONTINUE,
    EVENT_TYPES,
    MAX_HOOKS_PER_EVENT,
    HookCheck,
    HookDefinition,
    HookDefinitionError,
    HookExecutionOutcome,
    HookFinding,
    HookOutcome,
    LifecycleEventPayload,
)

logger = logging.getLogger(__name__)

#: The failure reason recorded on a run that a blocking hook denied.
FAILURE_HOOK_DENIED = "hook-denied"

#: The API error code when a blocking hook denies a transition.
HOOK_DENIED = "HOOK_DENIED"

#: Registry faults. A broken registry is a deployment fault, not a hook decision,
#: so it is reported with its own code instead of being read as a denial.
HOOK_REGISTRY_MISSING = "hook-registry-missing"
HOOK_REGISTRY_INVALID = "hook-registry-invalid"
HOOK_REGISTRY_DUPLICATE = "hook-registry-duplicate"
HOOK_REGISTRY_CROWDED = "hook-registry-crowded"

HOOK_FILE_SUFFIX = ".json"


class LifecycleHookBlocked(AgentExecutionError):
    """A blocking hook denied an agent invocation, so the agent never ran."""

    def __init__(self, *, event: str, hook: str, reason: str) -> None:
        detail = (
            f"The lifecycle hook '{hook}' denied the '{event}' event: {reason}"
        )
        super().__init__(FAILURE_HOOK_DENIED, detail)
        self.event = event
        self.hook = hook


@dataclass(frozen=True)
class HookRegistry:
    """The hooks a deployment has registered, in deterministic execution order."""

    hooks: tuple[HookDefinition, ...]
    digest: str
    root: str

    def for_event(self, event: str) -> tuple[HookDefinition, ...]:
        return tuple(hook for hook in self.hooks if event in hook.events)


def _digest(document: object) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _hook_document(hook: HookDefinition) -> dict[str, object]:
    return {
        "name": hook.name,
        "version": hook.version,
        "events": list(hook.events),
        "priority": hook.priority,
        "mode": hook.mode,
        "capabilities": list(hook.capabilities),
        "checks": [check.model_dump(mode="json") for check in hook.checks],
    }


def registry_digest(hooks: Sequence[HookDefinition]) -> str:
    """A digest over the semantic content of the registry.

    Recorded on every execution, so an archived decision names the set of policies
    that was in force when it was made.
    """

    ordered = [
        _hook_document(hook) for hook in sorted(hooks, key=lambda hook: (hook.priority, hook.name))
    ]
    return _digest(ordered)


def load_registry(root: str | Path) -> HookRegistry:
    """Read every hook definition in the trusted root, refusing anything unusable."""

    hooks_root = Path(root).expanduser()
    if not hooks_root.exists():
        # A deployment with no hooks is the normal case: nothing is registered and
        # every event continues. An unreadable path is not an error, but a path that
        # exists and is not a directory is.
        return HookRegistry(hooks=(), digest=registry_digest(()), root=str(hooks_root))
    if not hooks_root.is_dir():
        raise HookDefinitionError(
            HOOK_REGISTRY_INVALID,
            f"The hooks root '{hooks_root}' is not a directory.",
        )

    definitions: list[HookDefinition] = []
    seen: dict[str, str] = {}
    for path in sorted(hooks_root.glob(f"*{HOOK_FILE_SUFFIX}")):
        if not path.is_file():
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise HookDefinitionError(
                HOOK_REGISTRY_INVALID, f"{path.name} could not be read: {error}"
            ) from error
        except json.JSONDecodeError as error:
            raise HookDefinitionError(
                HOOK_REGISTRY_INVALID, f"{path.name} is not valid JSON: {error}"
            ) from error
        if not isinstance(document, dict):
            raise HookDefinitionError(
                HOOK_REGISTRY_INVALID, f"{path.name} must contain one JSON object."
            )
        try:
            hook = HookDefinition.model_validate(document)
        except ValueError as error:
            raise HookDefinitionError(
                HOOK_REGISTRY_INVALID, f"{path.name}: {error}"
            ) from error
        if hook.name in seen:
            raise HookDefinitionError(
                HOOK_REGISTRY_DUPLICATE,
                f"hook '{hook.name}' is declared twice: {seen[hook.name]} and {path.name}.",
            )
        seen[hook.name] = path.name
        definitions.append(hook)

    ordered = tuple(sorted(definitions, key=lambda hook: (hook.priority, hook.name)))
    for event in EVENT_TYPES:
        count = len([hook for hook in ordered if event in hook.events])
        if count > MAX_HOOKS_PER_EVENT:
            raise HookDefinitionError(
                HOOK_REGISTRY_CROWDED,
                f"{count} hooks are registered for '{event}', which exceeds the "
                f"limit of {MAX_HOOKS_PER_EVENT}.",
            )
    return HookRegistry(hooks=ordered, digest=registry_digest(ordered), root=str(hooks_root))


# --------------------------------------------------------------------------- #
# Checks: pure functions over the payload
# --------------------------------------------------------------------------- #


def _status_finding(check: HookCheck, payload: LifecycleEventPayload) -> HookFinding:
    allowed = set(check.allowed_statuses)
    forbidden = set(check.forbidden_statuses)
    if allowed and payload.task_status not in allowed:
        return HookFinding(
            kind=check.kind,
            passed=False,
            detail=(
                f"the task is '{payload.task_status}', which is not one of "
                f"{', '.join(sorted(allowed))}"
            ),
        )
    if forbidden and payload.task_status in forbidden:
        return HookFinding(
            kind=check.kind,
            passed=False,
            detail=f"the task is '{payload.task_status}', which is excluded",
        )
    return HookFinding(kind=check.kind, passed=True, detail=f"state '{payload.task_status}' is allowed")


def _paths_finding(check: HookCheck, payload: LifecycleEventPayload) -> HookFinding:
    compiled = [(value, re.compile(value)) for value in check.path_patterns]
    matched: list[str] = []
    for path in payload.changed_paths:
        for _, pattern in compiled:
            if pattern.search(path):
                matched.append(path)
                break
    if matched:
        shown = ", ".join(sorted(set(matched))[:5])
        return HookFinding(
            kind=check.kind,
            passed=False,
            detail=f"the recorded change set touches {shown}",
        )
    return HookFinding(
        kind=check.kind,
        passed=True,
        detail=f"{len(payload.changed_paths)} recorded path(s) matched no pattern",
    )


def _evidence_finding(check: HookCheck, payload: LifecycleEventPayload) -> HookFinding:
    available = set(payload.available_evidence)
    missing = [kind for kind in check.evidence if kind not in available]
    if missing:
        return HookFinding(
            kind=check.kind,
            passed=False,
            detail=f"the recorded evidence is missing {', '.join(missing)}",
        )
    return HookFinding(
        kind=check.kind,
        passed=True,
        detail=f"the recorded evidence includes {', '.join(check.evidence)}",
    )


def _specialist_finding(check: HookCheck, payload: LifecycleEventPayload) -> HookFinding:
    approving = set(payload.approving_specialists)
    missing = [name for name in check.specialists if name not in approving]
    if missing:
        return HookFinding(
            kind=check.kind,
            passed=False,
            detail=f"no approving verdict is recorded from {', '.join(missing)}",
        )
    return HookFinding(
        kind=check.kind,
        passed=True,
        detail=f"approving verdicts are recorded from {', '.join(check.specialists)}",
    )


def _note_finding(check: HookCheck, payload: LifecycleEventPayload) -> HookFinding:
    return HookFinding(kind=check.kind, passed=True, detail=check.text)


_CHECKS = {
    CHECK_TASK_STATUS: _status_finding,
    CHECK_FORBIDDEN_CHANGED_PATHS: _paths_finding,
    CHECK_REQUIRED_EVIDENCE: _evidence_finding,
    CHECK_REQUIRED_SPECIALIST_VERDICT: _specialist_finding,
    CHECK_NOTE: _note_finding,
}


def evaluate_hook(
    hook: HookDefinition, payload: LifecycleEventPayload
) -> tuple[HookExecutionOutcome, str]:
    """Apply one hook to one event. Returns the outcome and the execution status.

    A blocked invocation is decided here and nowhere else: only a blocking hook can
    turn a failed check into a block, and only a check can fail.
    """

    findings: list[HookFinding] = []
    observations: list[str] = []
    blocked_reason = ""
    status = "succeeded"
    try:
        for check in hook.checks:
            findings.append(_CHECKS[check.kind](check, payload))
    except Exception as error:  # noqa: BLE001 - recorded, then fail closed if required.
        status = "failed"
        logger.exception(
            "Lifecycle hook %s failed to evaluate %s", hook.name, payload.event
        )
        if hook.blocking:
            blocked_reason = (
                f"the hook could not be evaluated ({type(error).__name__}), and a "
                "blocking hook fails closed"
            )
            return (
                HookExecutionOutcome(
                    hook=hook.name,
                    version=hook.version,
                    mode=hook.mode,
                    priority=hook.priority,
                    decision=DECISION_BLOCK,
                    reason=blocked_reason,
                    findings=findings,
                    observations=observations,
                ),
                status,
            )
        observations.append(
            f"{hook.name}: the hook could not be evaluated and is an observer, so the "
            f"event continued ({type(error).__name__})"
        )

    for finding in findings:
        if finding.kind == CHECK_NOTE:
            if finding.detail:
                observations.append(f"{hook.name}: {finding.detail}")
            continue
        if finding.passed:
            continue
        check = next(item for item in hook.checks if item.kind == finding.kind)
        if hook.blocking:
            blocked_reason = check.reason or finding.detail
            break
        observations.append(f"{hook.name}: observed {finding.detail}")

    decision = DECISION_BLOCK if blocked_reason else DECISION_CONTINUE
    return (
        HookExecutionOutcome(
            hook=hook.name,
            version=hook.version,
            mode=hook.mode,
            priority=hook.priority,
            decision=decision,
            reason=blocked_reason,
            findings=findings,
            observations=observations[:32],
        ),
        status,
    )


def evaluate(payload: LifecycleEventPayload, registry: HookRegistry) -> HookOutcome:
    """Evaluate every registered hook for one event, without recording anything."""

    executions = [
        evaluate_hook(hook, payload)[0] for hook in registry.for_event(payload.event)
    ]
    blocked = [execution for execution in executions if execution.blocked]
    return HookOutcome(
        event=payload.event,
        subject_key=payload.subject_key,
        decision=DECISION_BLOCK if blocked else DECISION_CONTINUE,
        reason="; ".join(execution.reason for execution in blocked),
        executions=executions,
    )


# --------------------------------------------------------------------------- #
# Durable dispatch
# --------------------------------------------------------------------------- #

def denying_hook(outcome: HookOutcome) -> str:
    for execution in outcome.executions:
        if execution.blocked:
            return execution.hook
    return "unknown"  # pragma: no cover - blocked implies a denying hook.


__all__ = [
    "FAILURE_HOOK_DENIED",
    "HOOK_DENIED",
    "HOOK_REGISTRY_CROWDED",
    "HOOK_REGISTRY_DUPLICATE",
    "HOOK_REGISTRY_INVALID",
    "HOOK_REGISTRY_MISSING",
    "HookRegistry",
    "LifecycleHookBlocked",
    "denying_hook",
    "evaluate",
    "evaluate_hook",
    "load_registry",
    "registry_digest",
]
