"""The agent runner contract shared by the application and the standalone runtime.

Only the contract lives here: ``AgentInvocation`` in, ``AgentRunResult`` out,
``AgentExecutionError`` on a recordable failure. Concrete runners (the PydanticAI
adapter in ``loomsphere_harness.pydantic_harness``; the container and scripted
runners in the LoomSphere application) implement ``run(invocation) -> result``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID


class AgentExecutionError(RuntimeError):
    """An agent invocation failed in a way that must be recorded."""

    def __init__(
        self,
        reason: str,
        detail: str,
        *,
        exit_code: int | None = None,
        response_text: str | None = None,
        events: dict[str, object] | None = None,
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.exit_code = exit_code
        self.response_text = response_text
        self.events = events


@dataclass(frozen=True)
class AgentInvocation:
    run_id: UUID
    workspace_path: Path
    prompt: str
    timeout_seconds: int
    # Only the Builder role is allowed to modify the worktree. Architect, QA, and
    # Reviewer runs observe the same worktree through a read-only mount.
    read_only: bool = True
    #: The tools the trusted agent catalog authorizes for this role. A specialist's
    #: invocation carries its grant so the run record and the runner agree on what
    #: the stage was allowed to use; a tool outside the grant is never available.
    allowed_tools: tuple[str, ...] = ()
    # Recorded so a role-specific runner (and its logs) can tell which stage the
    # invocation belongs to. It never reaches the container command.
    role: str = "architect"

    @property
    def mount_mode(self) -> str:
        return "ro" if self.read_only else "rw"


@dataclass(frozen=True)
class AgentRunResult:
    response_text: str
    exit_code: int
    events: dict[str, object] = field(default_factory=dict)


__all__ = ["AgentExecutionError", "AgentInvocation", "AgentRunResult"]
