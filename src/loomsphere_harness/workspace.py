"""Isolated task worktrees and the exact state they record.

The portable runtime executes a Builder inside a git worktree that is *not* the
developer's checkout. This module owns that boundary: it creates and prunes the
worktree, records one commit per Builder iteration, digests the resulting tree so
validation evidence can be proven to belong to the bytes that were reviewed, and
screens the change set before delivery.

It is extracted from LoomSphere's ``agent_workspace`` and ``workspace_state``
services. Every git invocation is argument-listed, time limited, and free of shell
interpretation; hooks and signing are disabled so host configuration cannot change
what the runtime records. Nothing here pushes to a remote: the standalone runtime
stops at a human acceptance decision.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT_SECONDS = 60
DIFF_EXCERPT_CHARS = 12_000
BRANCH_PREFIX = "loomsphere/task-"
TASK_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
COMMIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
RETAINED_REF_PATTERN = re.compile(r"refs/loomsphere/[A-Za-z0-9._/-]+")

#: A change set must not publish material a human never reviewed as source. These
#: patterns are the last line of defence for an agent that wrote a credential file
#: into its worktree: the change is refused instead of being recorded as delivered.
SECRET_PATH_PATTERN = re.compile(
    r"(^|/)(\.env(\..+)?|\.netrc|\.npmrc|\.pypirc|id_rsa|id_dsa|id_ecdsa|id_ed25519|"
    r"credentials|credentials\.json|secrets?\.(json|ya?ml|toml|txt)|"
    r"[^/]+\.(pem|key|p12|pfx|keystore|jks|ppk))$",
    re.IGNORECASE,
)
SECRET_CONTENT_PATTERN = re.compile(
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    rb"gh[pousr]_[A-Za-z0-9]{20,}|"
    rb"github_pat_[A-Za-z0-9_]{20,}|"
    rb"xox[baprs]-[A-Za-z0-9-]{10,}|"
    rb"AKIA[0-9A-Z]{16}"
)

GIT_IDENTITY: tuple[str, ...] = (
    "-c",
    "user.name=LoomSphere Builder",
    "-c",
    "user.email=builder@loomsphere.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=/dev/null",
)


class WorkspaceError(RuntimeError):
    """A worktree operation could not be performed. The runtime fails closed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class TaskWorkspace:
    """One isolated worktree pinned to a base commit, on its own private branch."""

    path: Path
    branch: str
    commit: str
    base_commit: str


@dataclass(frozen=True)
class ChangeSet:
    """The recorded result of one Builder iteration."""

    base_commit: str
    head_commit: str
    changed_files: list[dict[str, str]]
    diff: str
    workspace_digest: str

    @property
    def paths(self) -> list[str]:
        return [entry["path"] for entry in self.changed_files]


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _git(path: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise WorkspaceError("git-unavailable", f"git could not run: {error}") from error
    if completed.returncode != 0:
        raise WorkspaceError(
            "git-failed",
            f"git {' '.join(args)} failed: {completed.stderr.strip()[:2000]}",
        )
    return completed.stdout


def _git_bytes(path: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise WorkspaceError("git-unavailable", f"git could not run: {error}") from error
    if completed.returncode != 0:
        raise WorkspaceError(
            "git-failed",
            f"git {' '.join(args)} failed: "
            f"{completed.stderr.decode('utf-8', 'replace').strip()[:2000]}",
        )
    return completed.stdout


def _git_succeeds(root: Path, arguments: list[str]) -> bool:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def validate_task_key(task_key: str) -> str:
    """Reject a task identifier that could become a path or a git option."""

    if not TASK_KEY_PATTERN.fullmatch(task_key):
        raise WorkspaceError(
            "invalid-task-key",
            f"The task identifier is not usable as a branch or directory name: {task_key!r}",
        )
    return task_key


def task_branch_name(task_key: str) -> str:
    return f"{BRANCH_PREFIX}{validate_task_key(task_key).lower()}"


def workspace_path_for(workspace_root: str | Path, task_key: str) -> Path:
    return Path(workspace_root) / f"task-{validate_task_key(task_key).lower()}"


def assert_within(root: str | Path, candidate: str | Path) -> Path:
    """Return the resolved candidate only when it is strictly inside ``root``."""

    resolved_root = Path(root).expanduser().resolve()
    resolved_candidate = Path(candidate).expanduser().resolve()
    if resolved_candidate == resolved_root or not resolved_candidate.is_relative_to(
        resolved_root
    ):
        raise WorkspaceError(
            "workspace-path-outside-root",
            "The workspace path is outside the configured workspace root.",
        )
    return resolved_candidate


def assert_disjoint(repository_root: str | Path, workspace_root: str | Path) -> None:
    root = Path(repository_root).expanduser().resolve()
    workspace = Path(workspace_root).expanduser().resolve()
    if workspace == root or workspace.is_relative_to(root) or root.is_relative_to(workspace):
        raise WorkspaceError(
            "workspace-overlaps-repository",
            "The workspace root must be outside the developer's working tree.",
        )


def resolve_repository_root(repository_root: str | Path) -> Path:
    root = Path(repository_root).expanduser().resolve()
    if not root.is_dir():
        raise WorkspaceError(
            "repository-root-missing", "The configured repository root does not exist."
        )
    if not (root / ".git").exists() and not is_worktree(root):
        raise WorkspaceError(
            "repository-root-invalid",
            "The configured repository root is not a git working tree.",
        )
    return root


def resolve_commit(repository_root: str | Path, reference: str) -> str:
    root = resolve_repository_root(repository_root)
    if not reference or reference.startswith("-") or ".." in reference:
        raise WorkspaceError(
            "invalid-reference", "The base commit is not a valid git reference."
        )
    commit = _git(root, "rev-parse", "--verify", f"{reference}^{{commit}}").strip()
    if not COMMIT_SHA_PATTERN.fullmatch(commit):
        raise WorkspaceError(
            "invalid-reference", "The base commit did not resolve to a commit."
        )
    return commit


def is_worktree(path: Path) -> bool:
    """True when ``path`` is a usable git worktree root."""

    if not path.is_dir():
        return False
    try:
        root = _git(path, "rev-parse", "--show-toplevel").strip()
    except WorkspaceError:
        return False
    return bool(root) and Path(root).resolve() == path.resolve()


def head_commit(path: Path) -> str:
    commit = _git(path, "rev-parse", "HEAD").strip()
    if not COMMIT_SHA_PATTERN.fullmatch(commit):
        raise WorkspaceError("invalid-commit", f"HEAD is not a commit hash: {commit!r}")
    return commit


def create_task_workspace(
    repository_root: str | Path,
    workspace_root: str | Path,
    task_key: str,
    base_commit: str,
) -> TaskWorkspace:
    """Create a fresh worktree pinned to ``base_commit`` on a private branch."""

    root = resolve_repository_root(repository_root)
    resolved_root_dir = Path(workspace_root).expanduser().resolve()
    assert_disjoint(root, resolved_root_dir)

    commit = resolve_commit(root, base_commit)
    branch = task_branch_name(task_key)
    path = assert_within(resolved_root_dir, workspace_path_for(resolved_root_dir, task_key))

    resolved_root_dir.mkdir(parents=True, exist_ok=True)

    # Predictable recovery: a leftover directory or branch from an earlier attempt
    # is discarded before the new worktree is created.
    if path.exists():
        shutil.rmtree(path)
    _git_succeeds(root, ["worktree", "prune"])
    if _git_succeeds(root, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]):
        _git(root, "branch", "-D", branch)

    _git(root, "worktree", "add", "--force", "-B", branch, str(path), commit)
    if not path.is_dir():
        raise WorkspaceError(
            "workspace-not-created",
            "Git reported success but the workspace directory is missing.",
        )
    return TaskWorkspace(path=path, branch=branch, commit=commit, base_commit=commit)


def ensure_task_workspace(
    repository_root: str | Path,
    workspace_root: str | Path,
    task_key: str,
    base_commit: str,
) -> TaskWorkspace:
    """Return the task worktree, creating it only when it does not exist yet.

    An existing worktree is reused verbatim so a rework iteration builds on the
    previous iteration instead of restarting from the base commit.
    """

    path = assert_within(
        Path(workspace_root).expanduser().resolve(),
        workspace_path_for(workspace_root, task_key),
    )
    if is_worktree(path):
        return TaskWorkspace(
            path=path,
            branch=task_branch_name(task_key),
            commit=head_commit(path),
            base_commit=base_commit,
        )
    return create_task_workspace(repository_root, workspace_root, task_key, base_commit)


def remove_task_workspace(
    repository_root: str | Path,
    workspace_root: str | Path,
    path: str | Path,
    branch: str | None = None,
) -> bool:
    """Remove a worktree and its task branch; returns whether anything changed."""

    root = resolve_repository_root(repository_root)
    target = assert_within(Path(workspace_root).expanduser().resolve(), path)

    removed = False
    if target.exists():
        if not _git_succeeds(root, ["worktree", "remove", "--force", str(target)]):
            shutil.rmtree(target)
        removed = True
    _git_succeeds(root, ["worktree", "prune"])

    if branch is not None and _git_succeeds(
        root, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
    ):
        _git(root, "branch", "-D", branch)
        removed = True
    return removed


def commit_worktree(path: Path, message: str) -> str:
    """Stage every change and record one commit. Returns the new HEAD.

    Raises ``WorkspaceError('no-changes')`` when the agent produced no modification,
    so an empty iteration can never be recorded as an implementation.
    """

    _git(path, "add", "--all", "--force")
    staged = _git(path, "diff", "--cached", "--name-only").strip()
    if not staged:
        raise WorkspaceError(
            "no-changes",
            "The Builder left the worktree unmodified; nothing could be committed.",
        )
    _git(path, *GIT_IDENTITY, "commit", "--no-verify", "-m", message)
    return head_commit(path)


def retain_commit(repository_root: str | Path, reference: str, commit: str) -> None:
    """Keep ``commit`` reachable under a private ref outside every branch.

    A rejected attempt is retained as evidence, but no branch, remote-tracking ref,
    or developer-owned ref is touched: the ref lives under ``refs/loomsphere/``.
    """

    if not RETAINED_REF_PATTERN.fullmatch(reference):
        raise WorkspaceError(
            "invalid-retained-ref", f"The retained evidence reference is not allowed: {reference!r}"
        )
    if not COMMIT_SHA_PATTERN.fullmatch(commit):
        raise WorkspaceError(
            "invalid-commit", f"The retained commit is not a commit hash: {commit!r}"
        )
    root = resolve_repository_root(repository_root)
    _git(root, "update-ref", reference, commit)


def changed_files(path: Path, base_commit: str, head: str) -> list[dict[str, str]]:
    raw = _git_bytes(path, "diff", "--name-status", "-z", f"{base_commit}..{head}")
    parts = [item.decode("utf-8", "replace") for item in raw.split(b"\0") if item]
    entries: list[dict[str, str]] = []
    index = 0
    while index + 1 < len(parts):
        entries.append({"status": parts[index], "path": parts[index + 1]})
        index += 2
    return entries


def diff_text(path: Path, base_commit: str, head: str) -> str:
    return _git(
        path, "diff", "--no-color", "--no-ext-diff", "--unified=3", f"{base_commit}..{head}"
    )


def diff_excerpt(text: str, limit: int = DIFF_EXCERPT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [diff truncated by the harness]"


def inventory(path: Path) -> list[tuple[str, str, str]]:
    """Every tracked or untracked-but-not-ignored file with its content digest."""

    raw = _git_bytes(path, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    names = [item.decode("utf-8", "replace") for item in raw.split(b"\0") if item]
    entries: list[tuple[str, str, str]] = []
    for name in sorted(set(names)):
        file_path = path / name
        if not file_path.is_file():
            # A deleted tracked file is part of the state, recorded by absence.
            entries.append((name, "deleted", ""))
            continue
        try:
            payload = file_path.read_bytes()
        except OSError as error:
            raise WorkspaceError(
                "unreadable-file", f"{name} could not be read: {error}"
            ) from error
        mode = "executable" if file_path.stat().st_mode & 0o111 else "file"
        entries.append((name, mode, sha256_hex(payload)))
    return entries


def workspace_digest(path: Path) -> str:
    """Content digest of the worktree, stable across repeated observations."""

    hasher = hashlib.sha256()
    for name, mode, digest in inventory(path):
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(mode.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(digest.encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def is_clean(path: Path) -> bool:
    """True when the worktree has no staged or unstaged change."""

    return not _git(path, "status", "--porcelain").strip()


def inspect_change_set(
    path: Path,
    *,
    base_commit: str,
    reviewed_commit: str,
    reported_paths: set[str] | None = None,
    max_files: int = 200,
    max_scan_bytes: int = 1_000_000,
) -> ChangeSet:
    """Verify the worktree still holds exactly the reviewed commit, then report it.

    Delivery publishes the commit validation exercised and the Reviewer accepted.
    It is refused when the worktree carries uncommitted work that was never
    validated, when HEAD is not the reviewed commit, when the commit touches a path
    the implementation did not report, or when it looks like a credential.
    """

    if not is_worktree(path):
        raise WorkspaceError("not-a-worktree", "The task worktree is missing or unusable.")
    if not is_clean(path):
        raise WorkspaceError(
            "workspace-dirty",
            "The task worktree has uncommitted changes that were never validated.",
        )
    current = head_commit(path)
    if current != reviewed_commit:
        raise WorkspaceError(
            "commit-mismatch",
            "The worktree head is not the reviewed implementation commit.",
        )

    changes = changed_files(path, base_commit, current)
    if not changes:
        raise WorkspaceError(
            "empty-change-set", "The reviewed commit does not change any file."
        )
    if len(changes) > max_files:
        raise WorkspaceError(
            "too-many-files", f"The reviewed commit changes more than {max_files} files."
        )

    for entry in changes:
        name = entry["path"]
        if reported_paths is not None and name not in reported_paths:
            raise WorkspaceError(
                "unauthorized-path",
                f"The reviewed commit contains a path the implementation did not report: {name}",
            )
        if SECRET_PATH_PATTERN.search(name):
            raise WorkspaceError(
                "secret-path",
                f"The reviewed commit contains a file that must never be published: {name}",
            )
        if entry["status"].startswith("D"):
            continue
        file_path = path / name
        if not file_path.is_file():
            continue
        if file_path.stat().st_size > max_scan_bytes:
            raise WorkspaceError(
                "file-too-large",
                f"{name} is too large to screen before delivery.",
            )
        if SECRET_CONTENT_PATTERN.search(file_path.read_bytes()):
            raise WorkspaceError(
                "secret-content",
                f"{name} contains content that looks like a credential.",
            )

    return ChangeSet(
        base_commit=base_commit,
        head_commit=current,
        changed_files=changes,
        diff=diff_text(path, base_commit, current),
        workspace_digest=workspace_digest(path),
    )


__all__ = [
    "BRANCH_PREFIX",
    "ChangeSet",
    "COMMIT_SHA_PATTERN",
    "DIFF_EXCERPT_CHARS",
    "GIT_IDENTITY",
    "GIT_TIMEOUT_SECONDS",
    "RETAINED_REF_PATTERN",
    "SECRET_CONTENT_PATTERN",
    "SECRET_PATH_PATTERN",
    "TaskWorkspace",
    "WorkspaceError",
    "assert_disjoint",
    "assert_within",
    "changed_files",
    "commit_worktree",
    "create_task_workspace",
    "diff_excerpt",
    "diff_text",
    "ensure_task_workspace",
    "head_commit",
    "inspect_change_set",
    "inventory",
    "is_clean",
    "is_worktree",
    "remove_task_workspace",
    "resolve_commit",
    "resolve_repository_root",
    "retain_commit",
    "sha256_hex",
    "task_branch_name",
    "validate_task_key",
    "workspace_digest",
    "workspace_path_for",
]
