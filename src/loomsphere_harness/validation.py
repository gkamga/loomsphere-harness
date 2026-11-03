"""Executed validation for one implementation iteration.

Validation is not delegated to an agent: the runtime plans a bounded command from
the worktree contents, checks it against an explicit allowlist, runs it inside the
shared sandbox boundary, and records the exit code, the duration, the output
digest, the sandbox image identity, and the worktree digest the evidence belongs
to. A command that is not allowlisted is never executed.

Because the worktree digest is computed before and after the command, a validation
command that mutates the tracked tree cannot be mistaken for valid evidence. When
the sandbox cannot be created the command is not executed at all: the outcome is
an error, never a pass, and there is no host-side fallback.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from loomsphere_harness.sandbox import (
    REASON_SOURCE_MUTATED,
    REASON_SOURCE_UNVERIFIABLE,
    SandboxError,
    SandboxExecution,
    SandboxExecutor,
    SandboxPolicy,
    SandboxUnavailableError,
    executor_from_settings,
)

VALIDATION_STATUS_SUCCEEDED = "succeeded"
VALIDATION_STATUS_FAILED = "failed"
VALIDATION_STATUS_ERROR = "error"

REASON_NO_PLAN = "no-validation-plan"
REASON_NOT_ALLOWLISTED = "validation-command-not-allowed"
REASON_UNAVAILABLE = "validation-command-unavailable"
REASON_TIMED_OUT = "validation-timed-out"
REASON_WORKSPACE_CHANGED = "validation-mutated-worktree"
REASON_DIGEST_ERROR = "validation-digest-failed"
REASON_SANDBOX_UNAVAILABLE = "validation-sandbox-unavailable"
REASON_SANDBOX_FAILED = "validation-sandbox-failed"

#: Only these variables reach the validated code. Database URLs, tokens, and any
#: other secret in the operator's environment are deliberately not forwarded, and
#: the sandbox refuses any variable outside its own allowlist.
BASE_ENV = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TZ": "UTC",
    "CI": "1",
    "NO_COLOR": "1",
    # Validation must observe the worktree, not modify it. Byte-code and cache
    # directories would otherwise appear as untracked files, and the post-run
    # worktree digest would report a spurious mutation.
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTEST_ADDOPTS": "-p no:cacheprovider",
}


class ValidationPlanError(RuntimeError):
    """No allowlisted validation command applies to this worktree."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ValidationPlan:
    command: tuple[str, ...]

    @property
    def digest(self) -> str:
        payload = json.dumps(list(self.command), separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ValidationOutcome:
    command: tuple[str, ...]
    status: str
    exit_code: int | None
    duration_ms: int
    output: str
    output_digest: str
    workspace_digest: str
    failure_reason: str | None
    sandbox_image: str | None = None
    sandbox_image_id: str | None = None
    execution: dict[str, object] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status == VALIDATION_STATUS_SUCCEEDED

    def as_record(self) -> dict[str, object]:
        return {
            "command": list(self.command),
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "output_digest": self.output_digest,
            "workspace_digest": self.workspace_digest,
            "failure_reason": self.failure_reason,
            "sandbox_image": self.sandbox_image,
            "sandbox_image_id": self.sandbox_image_id,
            "execution": self.execution,
        }


def _package_test_script(workspace: Path) -> str | None:
    manifest = workspace / "package.json"
    if not manifest.is_file():
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        return None
    test = scripts.get("test")
    if not isinstance(test, str):
        return None
    stripped = test.strip()
    if not stripped or stripped.lower() == 'echo "error: no test specified" && exit 1':
        return None
    return stripped


def _candidates(workspace: Path) -> list[list[str]]:
    candidates: list[list[str]] = []

    if _package_test_script(workspace) is not None and (
        workspace / "node_modules"
    ).is_dir():
        candidates.append(["npm", "run", "test", "--silent"])

    python_markers = (
        workspace / "pyproject.toml",
        workspace / "pytest.ini",
        workspace / "setup.cfg",
        workspace / "tox.ini",
    )
    if any(marker.is_file() for marker in python_markers) or (
        (workspace / "tests").is_dir()
    ):
        candidates.append(["python", "-m", "pytest", "-q"])
        # A standard-library suite needs no installed dependency, so it is the one
        # command that works in a bare image with the network disabled.
        candidates.append(["python", "-m", "unittest", "discover", "-s", "tests"])

    if (workspace / "test").is_dir() or any(workspace.glob("*.test.js")):
        candidates.append(["node", "--test"])

    return candidates


def plan_validation(
    workspace: Path,
    *,
    allowlist: list[list[str]] | tuple[tuple[str, ...], ...],
) -> ValidationPlan:
    """Choose the one validation command that applies to this worktree."""

    permitted = [list(entry) for entry in allowlist]
    for candidate in _candidates(workspace):
        if candidate in permitted:
            return ValidationPlan(tuple(candidate))

    raise ValidationPlanError(
        REASON_NO_PLAN,
        "No allowlisted validation command applies to this worktree; "
        "the implementation cannot be validated automatically.",
    )


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def _error_outcome(
    plan: ValidationPlan,
    *,
    failure_reason: str,
    output: str,
    exit_code: int | None = None,
    duration_ms: int = 0,
    workspace_digest_value: str = "0" * 64,
    execution: dict[str, object] | None = None,
    sandbox_image: str | None = None,
    sandbox_image_id: str | None = None,
) -> ValidationOutcome:
    return ValidationOutcome(
        command=plan.command,
        status=VALIDATION_STATUS_ERROR,
        exit_code=exit_code,
        duration_ms=duration_ms,
        output=output,
        output_digest=_digest_text(output),
        workspace_digest=workspace_digest_value,
        failure_reason=failure_reason,
        sandbox_image=sandbox_image,
        sandbox_image_id=sandbox_image_id,
        execution=execution or {},
    )


def _map_sandbox_failure(execution: SandboxExecution) -> str:
    if execution.failure_reason == REASON_SOURCE_UNVERIFIABLE:
        return REASON_DIGEST_ERROR
    if execution.failure_reason == REASON_SOURCE_MUTATED:
        return REASON_WORKSPACE_CHANGED
    return REASON_SANDBOX_FAILED


def _resolve_executor(
    sandbox: object,
    *,
    timeout_seconds: int,
    executor: SandboxExecutor | None,
) -> tuple[SandboxExecutor, SandboxPolicy]:
    """Build the executor for this run, keeping an injected one's runtime settings."""

    template = executor if executor is not None else executor_from_settings(sandbox)
    policy = template.policy.with_timeout(timeout_seconds)
    if executor is not None:
        return (
            SandboxExecutor(
                policy,
                runtime=template.runtime,
                startup_grace_seconds=template.startup_grace_seconds,
                max_output_chars=template.max_output_chars,
                runner=getattr(template, "_runner", None),
            ),
            policy,
        )
    return (
        SandboxExecutor(
            policy,
            runtime=template.runtime,
            startup_grace_seconds=template.startup_grace_seconds,
        ),
        policy,
    )


def execute_validation(
    plan: ValidationPlan,
    workspace: Path,
    *,
    timeout_seconds: int,
    excerpt_chars: int,
    allowlist: list[list[str]] | tuple[tuple[str, ...], ...],
    sandbox: object,
    executor: SandboxExecutor | None = None,
) -> ValidationOutcome:
    """Run the planned command once inside the sandbox and record what happened."""

    if list(plan.command) not in [list(entry) for entry in allowlist]:
        raise ValidationPlanError(
            REASON_NOT_ALLOWLISTED,
            "The planned validation command is not allowlisted.",
        )

    resolved_executor, resolved_policy = _resolve_executor(
        sandbox, timeout_seconds=timeout_seconds, executor=executor
    )

    try:
        execution = resolved_executor.execute(
            plan.command, workspace, environment=dict(BASE_ENV)
        )
    except SandboxUnavailableError as error:
        # Fail closed: the command was not executed, so this cannot be a pass.
        return _error_outcome(
            plan,
            failure_reason=(
                REASON_DIGEST_ERROR
                if error.code == REASON_SOURCE_UNVERIFIABLE
                else REASON_SANDBOX_UNAVAILABLE
            ),
            output=f"{error.code}: {error.message}",
            sandbox_image=resolved_policy.image,
        )
    except SandboxError as error:
        return _error_outcome(
            plan,
            failure_reason=REASON_SANDBOX_UNAVAILABLE,
            output=f"{error.code}: {error.message}",
            sandbox_image=resolved_policy.image,
        )

    evidence = execution.as_evidence()
    if execution.timed_out:
        return _error_outcome(
            plan,
            failure_reason=REASON_TIMED_OUT,
            output=(
                f"The validation command did not finish within {timeout_seconds} "
                f"seconds.\n{execution.output}"
            ),
            exit_code=execution.exit_code,
            duration_ms=execution.duration_ms,
            workspace_digest_value=execution.source_digest,
            execution=evidence,
            sandbox_image=resolved_policy.image,
            sandbox_image_id=execution.image_id,
        )

    if execution.failure_reason is not None:
        return _error_outcome(
            plan,
            failure_reason=_map_sandbox_failure(execution),
            output=f"{execution.failure_reason}: {execution.output}",
            exit_code=execution.exit_code,
            duration_ms=execution.duration_ms,
            workspace_digest_value=execution.source_digest,
            execution=evidence,
            sandbox_image=resolved_policy.image,
            sandbox_image_id=execution.image_id,
        )

    status = (
        VALIDATION_STATUS_SUCCEEDED
        if execution.exit_code == 0
        else VALIDATION_STATUS_FAILED
    )
    return ValidationOutcome(
        command=plan.command,
        status=status,
        exit_code=execution.exit_code,
        duration_ms=execution.duration_ms,
        output=execution.output[:excerpt_chars],
        output_digest=execution.output_digest,
        workspace_digest=execution.source_digest,
        failure_reason=None,
        sandbox_image=resolved_policy.image,
        sandbox_image_id=execution.image_id,
        execution=evidence,
    )


def policy_for(sandbox: object, *, timeout_seconds: int) -> SandboxPolicy:
    """The policy the configured sandbox settings produce at one timeout."""

    return executor_from_settings(sandbox).policy.with_timeout(timeout_seconds)


__all__ = [
    "BASE_ENV",
    "REASON_DIGEST_ERROR",
    "REASON_NO_PLAN",
    "REASON_NOT_ALLOWLISTED",
    "REASON_SANDBOX_FAILED",
    "REASON_SANDBOX_UNAVAILABLE",
    "REASON_TIMED_OUT",
    "REASON_UNAVAILABLE",
    "REASON_WORKSPACE_CHANGED",
    "VALIDATION_STATUS_ERROR",
    "VALIDATION_STATUS_FAILED",
    "VALIDATION_STATUS_SUCCEEDED",
    "ValidationOutcome",
    "ValidationPlan",
    "ValidationPlanError",
    "execute_validation",
    "plan_validation",
    "policy_for",
]
