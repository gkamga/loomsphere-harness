"""Replaceable seams of the portable runtime.

The standalone CLI binds local implementations (OS user, a Git checkout, SQLite).
The LoomSphere application keeps its own database queue, API, and authentication
and may bind different implementations without the portable package importing any
application module.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from loomsphere_harness.agent_runner import AgentInvocation, AgentRunResult


@dataclass(frozen=True)
class Principal:
    """Who performed an action. Never supplied by a model."""

    id: str
    kind: str = "local-user"


class IdentityProvider(Protocol):
    def current(self) -> Principal: ...


@dataclass(frozen=True)
class RepositorySnapshot:
    path: str
    ref: str
    commit: str
    dirty: bool


class RepositoryProvider(Protocol):
    """Resolves the repository a task is analysed against."""

    def snapshot(self) -> RepositorySnapshot: ...


@runtime_checkable
class ExecutionEnvironment(Protocol):
    """The workspace an Intake/Architect run may read, and nothing more.

    This is the read-only boundary: the engine refuses an environment that reports
    itself writable, and the model tools it grants are the read-only set. A future
    sandboxed or remote environment can satisfy this without changing the engine.
    """

    @property
    def workspace_path(self) -> Path: ...

    @property
    def writable(self) -> bool: ...


@runtime_checkable
class TaskStore(Protocol):
    """Persistence of tasks, runs, evidence, proposals, decisions, and the audit trail.

    ``SqliteStore`` is the local implementation. Every status change is expected to
    be an atomic compare-and-set, the proposal and audit rows append-only, and
    ``decide`` bound to the digest of the exact current proposal.
    """

    path: Path

    def exists(self) -> bool: ...
    def initialize(self, *, repo_path: str, harness_root: str, suite_digest: str,
                   actor: str) -> None: ...
    def check(self) -> None: ...
    def audit(self, task_id: str | None, actor: str, event: str,
              detail: dict[str, Any]) -> None: ...
    def audit_events(self, task_id: str | None = None) -> list[dict[str, Any]]: ...
    def verify_audit_chain(self) -> int: ...
    def create_task(self, *, title: str, requirements: str, acceptance_criteria: str,
                    repo_ref: str, repo_commit: str, repo_dirty: bool,
                    actor: str) -> dict[str, Any]: ...
    def task(self, task_id: str) -> dict[str, Any] | None: ...
    def tasks(self) -> list[dict[str, Any]]: ...
    def begin_run(self, *, task_id: str, role: str, expect: tuple[str, ...],
                  running_status: str, actor: str, evidence: dict[str, Any]) -> str: ...
    def fail_run(self, *, run_id: str, task_id: str, failed_status: str, running_status: str,
                 actor: str, reason: str, detail: str, response_text: str | None,
                 events: dict[str, Any] | None) -> None: ...
    def succeed_intake(self, *, run_id: str, task_id: str, response_text: str,
                       events: dict[str, Any], digest: str, document: dict[str, Any],
                       needs_clarification: bool, actor: str) -> None: ...
    def succeed_architect(self, *, run_id: str, task_id: str, response_text: str,
                          events: dict[str, Any], digest: str, document: dict[str, Any],
                          actor: str) -> str: ...
    def recover_interrupted(self, actor: str) -> list[str]: ...
    def runs(self, task_id: str) -> list[dict[str, Any]]: ...
    def intake(self, task_id: str) -> dict[str, Any] | None: ...
    def proposal(self, task_id: str) -> dict[str, Any] | None: ...
    def approval(self, task_id: str) -> dict[str, Any] | None: ...
    def decide(self, *, task_id: str, proposal_digest: str, decision: str, actor: str,
               reason: str | None, repo_commit: str | None) -> dict[str, Any]: ...


@runtime_checkable
class ApprovalGate(Protocol):
    """Explicit human approval or rejection of one exact proposal.

    The decision must quote the SHA-256 digest of the stored proposal, the decider
    comes from an ``IdentityProvider`` and never from a model, and a refusal is
    audited. Implementations must not offer any bypass.
    """

    def approve(self, task_id: str, *, proposal_digest: str) -> dict[str, Any]: ...
    def reject(self, task_id: str, *, proposal_digest: str, reason: str) -> dict[str, Any]: ...


class AgentRunner(Protocol):
    """The existing runner contract: ``PydanticHarnessRunner`` satisfies it."""

    def run(self, invocation: AgentInvocation) -> AgentRunResult: ...
