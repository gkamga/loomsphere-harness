"""Local explicit approval: the proposal-digest-bound human gate.

Implements :class:`~loomsphere_harness.interfaces.ApprovalGate` over any
``TaskStore``, ``IdentityProvider``, and ``RepositoryProvider``. It has no prompt, no
environment bypass, and no model input: the decider is the identity provider's
principal, and the decision is valid only for the exact stored proposal digest.
"""

from __future__ import annotations

from typing import Any

from loomsphere_harness.interfaces import IdentityProvider, RepositoryProvider, TaskStore
from loomsphere_harness.store import StoreError


class ApprovalError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LocalApprovalGate:
    def __init__(self, *, store: TaskStore, identity: IdentityProvider,
                 repository: RepositoryProvider) -> None:
        self._store = store
        self._identity = identity
        self._repository = repository

    def approve(self, task_id: str, *, proposal_digest: str) -> dict[str, Any]:
        return self._decide(task_id, "approved", proposal_digest, None)

    def reject(self, task_id: str, *, proposal_digest: str, reason: str) -> dict[str, Any]:
        if not reason.strip():
            raise ApprovalError("reason-required", "A rejection needs a reason.")
        return self._decide(task_id, "rejected", proposal_digest, reason.strip())

    def _decide(self, task_id: str, decision: str, digest: str, reason: str | None
                ) -> dict[str, Any]:
        principal = self._identity.current()
        snapshot = self._repository.snapshot()
        try:
            return self._store.decide(
                task_id=task_id, proposal_digest=digest.strip().lower(), decision=decision,
                actor=principal.id, reason=reason, repo_commit=snapshot.commit,
            )
        except StoreError as error:
            self._store.audit(task_id, principal.id, "approval.refused", {"code": error.code})
            raise
