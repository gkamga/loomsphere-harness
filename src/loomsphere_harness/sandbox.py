"""The single execution boundary for untrusted, Builder-controlled code.

A Builder writes code the runtime then has to execute to learn whether it works.
Running that command on the operator's machine would give it the operator's
filesystem, network, credentials, and unbounded CPU: this module replaces that with
one controlled container boundary, extracted from LoomSphere's ``sandbox`` service.

Guarantees provided to the caller
---------------------------------
* the command runs in a disposable container created from a pinned image whose
  identity (``sha256:...``) is returned as evidence;
* the container is never privileged, never receives the Docker socket, and is
  never started from a host subprocess fallback;
* the container runs as an unprivileged user with all Linux capabilities dropped
  and ``no-new-privileges`` set;
* CPU, memory, PID, and wall-clock limits are explicit and recorded;
* the network is disabled: the standalone runtime has no internal-network
  provisioning, so anything other than ``network="none"`` is refused;
* the writable layer is read-only. Only ``/tmp`` is writable, and it is a
  disposable ``tmpfs``;
* the worktree is mounted read-only and its content digest is verified again
  after execution, so a mutation cannot be mistaken for valid evidence;
* only an explicit environment allowlist is forwarded, so host secrets, tokens,
  and database URLs cannot reach the command;
* when the runtime or the image is missing the executor raises
  ``SandboxUnavailableError``. Callers translate that into a failed outcome.
  Nothing is ever executed on the host as a fallback.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

from loomsphere_harness.workspace import WorkspaceError, workspace_digest

#: Failure codes. They are stored verbatim in recorded evidence, so they are stable.
REASON_RUNTIME_UNAVAILABLE = "sandbox-runtime-unavailable"
REASON_IMAGE_UNAVAILABLE = "sandbox-image-unavailable"
REASON_NETWORK_NOT_RESTRICTED = "sandbox-network-not-restricted"
REASON_LAUNCH_FAILED = "sandbox-launch-failed"
REASON_TIMED_OUT = "sandbox-timed-out"
REASON_SOURCE_UNVERIFIABLE = "sandbox-source-unverifiable"
REASON_SOURCE_MUTATED = "sandbox-source-mutated"
REASON_POLICY_INVALID = "sandbox-policy-invalid"

#: Docker's documented "the daemon could not run the container" exit code, which
#: means the container never executed the command: it is never evidence that the
#: command failed on its own merits.
_DOCKER_LAUNCH_ERROR_EXIT_CODE = 125

#: The only host variables that may reach untrusted code. ``PATH`` is omitted on
#: purpose: the container image supplies its own, and forwarding the host ``PATH``
#: would both leak host layout and could shadow the image's interpreter.
SANDBOX_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "CI",
        "LANG",
        "LC_ALL",
        "NO_COLOR",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "PYTEST_ADDOPTS",
        "TERM",
        "TZ",
    }
)

#: The only writable location inside the container.
SANDBOX_TMP = "/tmp"

#: The fixed in-container mount point of the worktree.
SANDBOX_WORKDIR = "/workspace"

#: The unprivileged account every sandbox process runs as.
SANDBOX_USER = "65534:65534"


class SandboxError(RuntimeError):
    """Base class for sandbox failures that are recorded as evidence."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SandboxPolicyError(SandboxError):
    """The requested sandbox policy is unsafe or internally inconsistent."""


class SandboxUnavailableError(SandboxError):
    """The sandbox cannot be created, so the command must not run at all."""


@dataclass(frozen=True)
class SandboxPolicy:
    """The complete, explicit boundary applied to one execution."""

    image: str
    timeout_seconds: int
    cpus: float = 2.0
    memory_mb: int = 2_048
    pids_limit: int = 256
    tmpfs_mb: int = 256
    network: str = "none"
    user: str = SANDBOX_USER
    workdir: str = SANDBOX_WORKDIR
    read_only_source: bool = True

    def __post_init__(self) -> None:
        if not self.image or not self.image.strip():
            raise SandboxPolicyError(REASON_POLICY_INVALID, "Sandbox image is empty.")
        if self.timeout_seconds <= 0:
            raise SandboxPolicyError(
                REASON_POLICY_INVALID, "Sandbox timeout must be positive."
            )
        if self.cpus <= 0:
            raise SandboxPolicyError(
                REASON_POLICY_INVALID, "Sandbox CPU limit must be positive."
            )
        if self.memory_mb < 64:
            raise SandboxPolicyError(
                REASON_POLICY_INVALID, "Sandbox memory limit is too small to run."
            )
        if self.pids_limit < 16:
            raise SandboxPolicyError(
                REASON_POLICY_INVALID, "Sandbox PID limit is too small to run."
            )
        if self.tmpfs_mb < 16:
            raise SandboxPolicyError(
                REASON_POLICY_INVALID, "Sandbox temporary storage is too small."
            )
        if self.network != "none":
            raise SandboxPolicyError(
                REASON_POLICY_INVALID,
                "The standalone runtime provisions no internal network; the sandbox "
                "network must be 'none'.",
            )
        if not self.user or self.user.split(":")[0].strip() in ("", "0"):
            raise SandboxPolicyError(
                REASON_POLICY_INVALID, "The sandbox must run as a non-root user."
            )
        if not self.workdir.startswith("/") or self.workdir == "/":
            raise SandboxPolicyError(
                REASON_POLICY_INVALID, "The sandbox workdir must be an absolute path."
            )

    def with_timeout(self, timeout_seconds: int) -> "SandboxPolicy":
        return replace(self, timeout_seconds=timeout_seconds)

    def describe(self) -> dict[str, object]:
        return {
            "cpus": self.cpus,
            "memory_mb": self.memory_mb,
            "pids_limit": self.pids_limit,
            "tmpfs_mb": self.tmpfs_mb,
            "timeout_seconds": self.timeout_seconds,
            "network": self.network,
            "user": self.user,
            "workdir": self.workdir,
            "read_only_source": self.read_only_source,
        }


@dataclass(frozen=True)
class SandboxExecution:
    """What the trusted runtime observed about one sandboxed execution."""

    policy: SandboxPolicy
    command: tuple[str, ...]
    container: str
    image_id: str | None
    source_digest: str
    source_digest_after: str | None
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    output: str
    output_digest: str
    failure_reason: str | None

    @property
    def succeeded(self) -> bool:
        return (
            self.exit_code == 0 and not self.timed_out and self.failure_reason is None
        )

    def as_evidence(self) -> dict[str, object]:
        """The recorded evidence.

        It binds the result to the exact command, image identity, source digest,
        resource limits, and exit status, so a later reader can tell what was
        executed and under which boundary.
        """

        return {
            "engine": "docker-sandbox",
            "image": self.policy.image,
            "image_id": self.image_id,
            "container": self.container,
            "command": list(self.command),
            "source_digest": self.source_digest,
            "source_digest_after": self.source_digest_after,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "output_digest": self.output_digest,
            "failure_reason": self.failure_reason,
            "limits": self.policy.describe(),
            "environment_allowlist": sorted(SANDBOX_ENVIRONMENT_ALLOWLIST),
        }


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def _truncate_output(output: str, limit: int) -> str:
    if len(output) <= limit:
        return output
    head = max(limit // 2, 1)
    tail = max(limit - head, 1)
    return (
        f"{output[:head]}\n... [output truncated by the harness sandbox] ...\n"
        f"{output[-tail:]}"
    )


def sanitize_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    """Return only allowlisted variables, refusing anything unexpected.

    Refusing instead of dropping makes a mistaken attempt to forward a secret a
    loud failure rather than a silent leak or a silently missing variable.
    """

    sanitized: dict[str, str] = {}
    for name, value in (environment or {}).items():
        if name not in SANDBOX_ENVIRONMENT_ALLOWLIST:
            raise SandboxPolicyError(
                REASON_POLICY_INVALID,
                f"Environment variable {name!r} is not allowed inside the sandbox.",
            )
        if "\0" in value:
            raise SandboxPolicyError(
                REASON_POLICY_INVALID,
                f"Environment variable {name!r} contains a NUL byte.",
            )
        sanitized[name] = value
    return sanitized


class SandboxExecutor:
    """Create the boundary, run one command in it, and report the evidence."""

    def __init__(
        self,
        policy: SandboxPolicy,
        *,
        runtime: str = "docker",
        startup_grace_seconds: int = 30,
        max_output_chars: int = 1_000_000,
        runner: object | None = None,
    ) -> None:
        if startup_grace_seconds <= 0:
            raise SandboxPolicyError(
                REASON_POLICY_INVALID, "Sandbox startup grace must be positive."
            )
        if max_output_chars < 1_000:
            raise SandboxPolicyError(
                REASON_POLICY_INVALID, "Sandbox output limit is too small."
            )
        self.policy = policy
        self.runtime = runtime
        self.startup_grace_seconds = startup_grace_seconds
        self.max_output_chars = max_output_chars
        # Tests inject a runner to prove the fail-closed branches without a
        # container runtime. Production always uses ``subprocess.run``.
        self._runner = runner or subprocess.run

    # -- runtime inspection ---------------------------------------------------

    def _run_runtime(
        self, arguments: Sequence[str], *, timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner(  # type: ignore[call-arg]
                [self.runtime, *arguments],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as error:
            raise SandboxUnavailableError(
                REASON_RUNTIME_UNAVAILABLE,
                f"The container runtime {self.runtime!r} is not available.",
            ) from error
        except (OSError, subprocess.SubprocessError) as error:
            raise SandboxUnavailableError(
                REASON_RUNTIME_UNAVAILABLE,
                f"The container runtime {self.runtime!r} could not be used: {error}",
            ) from error

    def resolve_image_id(self) -> str:
        completed = self._run_runtime(
            ["image", "inspect", "--format", "{{.Id}}", self.policy.image]
        )
        image_id = (completed.stdout or "").strip()
        if completed.returncode != 0 or not image_id.startswith("sha256:"):
            detail = (completed.stderr or completed.stdout or "").strip()[:300]
            raise SandboxUnavailableError(
                REASON_IMAGE_UNAVAILABLE,
                f"The sandbox image {self.policy.image!r} is not available: {detail}",
            )
        return image_id

    # -- command construction -------------------------------------------------

    def _container_arguments(
        self,
        *,
        container: str,
        command: Sequence[str],
        workspace: Path,
        environment: Mapping[str, str],
    ) -> list[str]:
        policy = self.policy
        mount_mode = "ro" if policy.read_only_source else "rw"
        arguments = [
            "run",
            "--rm",
            "--name",
            container,
            "--label",
            "loomsphere.sandbox=true",
            # No --privileged, no --cap-add, no Docker socket mount.
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            policy.user,
            "--pids-limit",
            str(policy.pids_limit),
            "--memory",
            f"{policy.memory_mb}m",
            "--memory-swap",
            f"{policy.memory_mb}m",
            "--cpus",
            f"{policy.cpus}",
            "--read-only",
            "--tmpfs",
            f"{SANDBOX_TMP}:rw,nosuid,nodev,size={policy.tmpfs_mb}m,mode=1777",
            "--workdir",
            policy.workdir,
            "--mount",
            f"type=bind,source={workspace},target={policy.workdir},{mount_mode}",
            "-e",
            f"HOME={SANDBOX_TMP}",
            "-e",
            f"TMPDIR={SANDBOX_TMP}",
            "--network",
            "none",
        ]
        for name, value in sorted(environment.items()):
            arguments += ["-e", f"{name}={value}"]
        arguments.append(policy.image)
        arguments.extend(command)
        return arguments

    def _force_remove(self, container: str) -> None:
        try:
            self._runner(  # type: ignore[call-arg]
                [self.runtime, "rm", "-f", container],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            # Cleanup failure must not become a host-side retry. ``--rm`` and the
            # container name still make the leftover explicit.
            pass

    # -- execution ------------------------------------------------------------

    def execute(
        self,
        command: Sequence[str],
        workspace: Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> SandboxExecution:
        """Run ``command`` once inside the boundary and return its evidence.

        Raises ``SandboxUnavailableError`` when the boundary cannot be created.
        The caller must treat that as "not executed"; there is no host fallback.
        """

        if not command:
            raise SandboxPolicyError(REASON_POLICY_INVALID, "Empty sandbox command.")
        if any("\0" in argument for argument in command):
            raise SandboxPolicyError(
                REASON_POLICY_INVALID, "The sandbox command contains a NUL byte."
            )
        resolved_workspace = Path(workspace).resolve()
        if not resolved_workspace.is_dir():
            raise SandboxUnavailableError(
                REASON_SOURCE_UNVERIFIABLE, "The sandbox workspace is missing."
            )
        if "," in str(resolved_workspace):
            # ``--mount`` parses a comma-separated list, so a comma in the source
            # path would silently change the mount specification.
            raise SandboxPolicyError(
                REASON_POLICY_INVALID,
                "The sandbox workspace path must not contain a comma.",
            )

        sanitized = sanitize_environment(environment)
        image_id = self.resolve_image_id()

        try:
            digest_before = workspace_digest(resolved_workspace)
        except WorkspaceError as error:
            raise SandboxUnavailableError(
                REASON_SOURCE_UNVERIFIABLE, error.message
            ) from error

        container = f"loomsphere-sandbox-{uuid.uuid4().hex[:16]}"
        arguments = self._container_arguments(
            container=container,
            command=command,
            workspace=resolved_workspace,
            environment=sanitized,
        )

        started = time.monotonic()
        output = ""
        exit_code: int | None = None
        timed_out = False
        failure_reason: str | None = None
        try:
            completed = self._runner(  # type: ignore[call-arg]
                [self.runtime, *arguments],
                capture_output=True,
                text=True,
                timeout=self.policy.timeout_seconds + self.startup_grace_seconds,
                check=False,
            )
            output = (completed.stdout or "") + (completed.stderr or "")
            exit_code = completed.returncode
            if exit_code == _DOCKER_LAUNCH_ERROR_EXIT_CODE:
                failure_reason = REASON_LAUNCH_FAILED
        except subprocess.TimeoutExpired as error:
            timed_out = True
            failure_reason = REASON_TIMED_OUT
            output = self._timeout_output(error)
            self._force_remove(container)
        except FileNotFoundError as error:  # pragma: no cover - runtime checked above
            raise SandboxUnavailableError(
                REASON_RUNTIME_UNAVAILABLE,
                f"The container runtime {self.runtime!r} disappeared.",
            ) from error
        except (OSError, subprocess.SubprocessError) as error:
            failure_reason = REASON_LAUNCH_FAILED
            output = str(error)
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)

        digest_after: str | None = None
        try:
            digest_after = workspace_digest(resolved_workspace)
        except WorkspaceError:
            failure_reason = failure_reason or REASON_SOURCE_UNVERIFIABLE
        if digest_after is not None and digest_after != digest_before:
            # Defence in depth: the executor verified the mount mode, and it also
            # verifies the content it handed over was not modified.
            failure_reason = REASON_SOURCE_MUTATED

        output = _truncate_output(output, self.max_output_chars)
        return SandboxExecution(
            policy=self.policy,
            command=tuple(command),
            container=container,
            image_id=image_id,
            source_digest=digest_before,
            source_digest_after=digest_after,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_ms=duration_ms,
            output=output,
            output_digest=_digest_text(output),
            failure_reason=failure_reason,
        )

    @staticmethod
    def _timeout_output(error: subprocess.TimeoutExpired) -> str:
        parts: list[str] = []
        for stream in (error.stdout, error.stderr):
            if stream is None:
                continue
            parts.append(
                stream if isinstance(stream, str) else stream.decode("utf-8", "replace")
            )
        return "\n".join(parts)


def executor_from_settings(settings: object) -> SandboxExecutor:
    """Build the executor from the configured sandbox settings.

    Duck-typed on purpose so ``config`` does not import this module and create a
    cycle with the workspace digest helper.
    """

    policy = SandboxPolicy(
        image=str(getattr(settings, "image")),
        timeout_seconds=int(getattr(settings, "timeout_seconds")),
        cpus=float(getattr(settings, "cpus")),
        memory_mb=int(getattr(settings, "memory_mb")),
        pids_limit=int(getattr(settings, "pids_limit")),
        tmpfs_mb=int(getattr(settings, "tmpfs_mb")),
        network=str(getattr(settings, "network")),
    )
    return SandboxExecutor(
        policy,
        runtime=str(getattr(settings, "runtime")),
        startup_grace_seconds=int(getattr(settings, "startup_grace_seconds", 30)),
    )


__all__ = [
    "REASON_IMAGE_UNAVAILABLE",
    "REASON_LAUNCH_FAILED",
    "REASON_NETWORK_NOT_RESTRICTED",
    "REASON_POLICY_INVALID",
    "REASON_RUNTIME_UNAVAILABLE",
    "REASON_SOURCE_MUTATED",
    "REASON_SOURCE_UNVERIFIABLE",
    "REASON_TIMED_OUT",
    "SANDBOX_ENVIRONMENT_ALLOWLIST",
    "SANDBOX_TMP",
    "SANDBOX_USER",
    "SANDBOX_WORKDIR",
    "SandboxError",
    "SandboxExecution",
    "SandboxExecutor",
    "SandboxPolicy",
    "SandboxPolicyError",
    "SandboxUnavailableError",
    "executor_from_settings",
    "sanitize_environment",
]
