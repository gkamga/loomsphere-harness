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

from loomsphere_harness.schemas.acceptance_contract import (
    AcceptanceContract,
    validate_selector,
)
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
REASON_PYTEST_UNAVAILABLE = "acceptance-pytest-environment-unavailable"
REASON_PYTEST_SELECTOR_NOT_FOUND = "acceptance-pytest-selector-not-found"
REASON_PYTEST_COLLECTION_FAILED = "acceptance-pytest-collection-failed"
REASON_PROTECTED_VERIFICATION_UNAVAILABLE = (
    "acceptance-protected-verification-unavailable"
)

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
    verification_coverage: dict[str, object] = field(default_factory=dict)

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
            "verification_coverage": self.verification_coverage,
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


def _execution_record(
    command: tuple[str, ...],
    execution: SandboxExecution | None,
    *,
    policy: SandboxPolicy,
    failure_reason: str | None = None,
    output: str = "",
) -> dict[str, object]:
    if execution is None:
        return {
            "command": list(command),
            "status": VALIDATION_STATUS_ERROR,
            "exit_code": None,
            "output": output,
            "output_digest": _digest_text(output),
            "source_digest": None,
            "source_digest_after": None,
            "sandbox_image": policy.image,
            "sandbox_image_id": None,
            "failure_reason": failure_reason,
            "execution": {},
        }

    if execution.timed_out:
        status = VALIDATION_STATUS_ERROR
        reason = REASON_TIMED_OUT
    elif execution.failure_reason is not None:
        status = VALIDATION_STATUS_ERROR
        reason = _map_sandbox_failure(execution)
    elif execution.exit_code == 0:
        status = VALIDATION_STATUS_SUCCEEDED
        reason = None
    else:
        status = VALIDATION_STATUS_FAILED
        reason = None

    return {
        "command": list(command),
        "status": status,
        "exit_code": execution.exit_code,
        "output": execution.output,
        "output_digest": execution.output_digest,
        "source_digest": execution.source_digest,
        "source_digest_after": execution.source_digest_after,
        "sandbox_image": policy.image,
        "sandbox_image_id": execution.image_id,
        "failure_reason": reason,
        "execution": execution.as_evidence(),
    }


def execute_acceptance_verifications(
    contract: AcceptanceContract,
    workspace: Path,
    *,
    timeout_seconds: int,
    sandbox: object,
    executor: SandboxExecutor | None,
    expected_source_digest: str,
    expected_image_id: str | None,
) -> dict[str, object]:
    """Execute supported declared selectors and preserve explicit gaps.

    Pytest is provided by the configured sandbox image, not installed during a
    run: all calls happen with network disabled and a read-only source mount.
    """

    resolved_executor, policy = _resolve_executor(
        sandbox, timeout_seconds=timeout_seconds, executor=executor
    )
    pytest_criteria = [
        item for item in contract.criteria if item.verification.kind == "pytest"
    ]
    environment_check: dict[str, object] | None = None

    def run(command: tuple[str, ...]) -> dict[str, object]:
        try:
            execution = resolved_executor.execute(
                command, workspace, environment=dict(BASE_ENV)
            )
        except SandboxError as error:
            return _execution_record(
                command,
                None,
                policy=policy,
                failure_reason=error.code,
                output=f"{error.code}: {error.message}",
            )
        return _execution_record(command, execution, policy=policy)

    def evidence_is_bound(record: dict[str, object]) -> bool:
        return (
            record["source_digest"] == expected_source_digest
            and record["source_digest_after"] == expected_source_digest
            and record["sandbox_image_id"] == expected_image_id
            and isinstance(expected_image_id, str)
            and expected_image_id.startswith("sha256:")
        )

    if pytest_criteria:
        environment_check = run(("python", "-m", "pytest", "--version"))
        if not evidence_is_bound(environment_check):
            if environment_check["status"] == VALIDATION_STATUS_FAILED:
                environment_check["failure_reason"] = REASON_PYTEST_UNAVAILABLE
            elif environment_check["status"] == VALIDATION_STATUS_SUCCEEDED:
                environment_check["status"] = VALIDATION_STATUS_ERROR
                environment_check["failure_reason"] = "acceptance-evidence-mismatch"
        elif environment_check["status"] == VALIDATION_STATUS_FAILED:
            environment_check["failure_reason"] = REASON_PYTEST_UNAVAILABLE

    results: list[dict[str, object]] = []
    for criterion in contract.criteria:
        verification = criterion.verification
        declared = verification.model_dump(mode="json")
        result: dict[str, object] = {
            "id": criterion.id,
            "mandatory": criterion.mandatory,
            "declared": declared,
            "status": "unverified",
            "selector_exists": None,
            "selector_executed": False,
            "failure_reason": None,
            "collection": None,
            "execution": None,
        }

        if verification.kind == "unverified":
            result["failure_reason"] = "acceptance-obligation-unverified"
            results.append(result)
            continue

        if verification.kind == "protected-pytest":
            result["status"] = "unsupported"
            result["failure_reason"] = REASON_PROTECTED_VERIFICATION_UNAVAILABLE
            result["obstacle"] = (
                "The approved protected-pytest artifact registry is not implemented "
                "in this runtime; the selector was not substituted or executed from "
                "the Builder worktree."
            )
            results.append(result)
            continue

        selector = verification.selector or ""
        validate_selector(selector)
        if environment_check is None or environment_check["status"] != VALIDATION_STATUS_SUCCEEDED:
            result["status"] = "error"
            result["failure_reason"] = (
                environment_check["failure_reason"]
                if environment_check is not None
                else REASON_PYTEST_UNAVAILABLE
            )
            result["obstacle"] = (
                str(environment_check.get("output") or environment_check["failure_reason"])
                if environment_check is not None
                else "No configured pytest sandbox environment was available."
            )
            results.append(result)
            continue

        collection_command = (
            "python", "-m", "pytest", "--collect-only", "-q", selector
        )
        collection = run(collection_command)
        result["collection"] = collection
        if not evidence_is_bound(collection):
            result["status"] = "error"
            result["failure_reason"] = (
                collection["failure_reason"] or "acceptance-evidence-mismatch"
            )
            result["obstacle"] = collection["output"]
            results.append(result)
            continue
        if collection["status"] == VALIDATION_STATUS_ERROR:
            result["status"] = "error"
            result["failure_reason"] = collection["failure_reason"]
            result["obstacle"] = collection["output"]
            results.append(result)
            continue
        if collection["exit_code"] != 0:
            if collection["exit_code"] in (4, 5):
                result["status"] = VALIDATION_STATUS_FAILED
                result["selector_exists"] = False
                result["failure_reason"] = REASON_PYTEST_SELECTOR_NOT_FOUND
            else:
                result["status"] = "error"
                result["failure_reason"] = REASON_PYTEST_COLLECTION_FAILED
            result["obstacle"] = collection["output"]
            results.append(result)
            continue

        result["selector_exists"] = True
        command = ("python", "-m", "pytest", "-q", selector)
        execution = run(command)
        result["execution"] = execution
        sandbox_evidence = execution["execution"]
        result["selector_executed"] = (
            isinstance(sandbox_evidence, dict)
            and bool(sandbox_evidence)
            and sandbox_evidence.get("failure_reason") != "sandbox-launch-failed"
        )
        if not evidence_is_bound(execution):
            result["status"] = "error"
            result["failure_reason"] = (
                execution["failure_reason"] or "acceptance-evidence-mismatch"
            )
            result["obstacle"] = execution["output"]
        elif execution["status"] == VALIDATION_STATUS_ERROR:
            result["status"] = "error"
            result["failure_reason"] = execution["failure_reason"]
            result["obstacle"] = execution["output"]
        elif execution["exit_code"] == 0:
            result["status"] = VALIDATION_STATUS_SUCCEEDED
        else:
            result["status"] = VALIDATION_STATUS_FAILED
            result["obstacle"] = execution["output"]
        results.append(result)

    return {
        "environment_check": environment_check,
        "criteria": results,
    }


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
    "REASON_PYTEST_COLLECTION_FAILED",
    "REASON_PYTEST_SELECTOR_NOT_FOUND",
    "REASON_PYTEST_UNAVAILABLE",
    "REASON_PROTECTED_VERIFICATION_UNAVAILABLE",
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
    "execute_acceptance_verifications",
    "execute_validation",
    "plan_validation",
    "policy_for",
]
