"""Local identity and Git repository providers."""

from __future__ import annotations

import getpass
import subprocess
from pathlib import Path

from loomsphere_harness.interfaces import Principal, RepositorySnapshot


class RepositoryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class OsUserIdentity:
    """The operating-system user running the CLI."""

    def __init__(self, override: str | None = None) -> None:
        self._override = override

    def current(self) -> Principal:
        name = (self._override or getpass.getuser()).strip()
        if not name:
            raise RepositoryError("identity-unavailable", "No local user identity.")
        return Principal(id=name)


def _git(path: Path, *args: str) -> str:
    try:
        completed = subprocess.run(  # fixed argv, no shell
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RepositoryError("git-unavailable", "Git could not be run.") from error
    if completed.returncode != 0:
        raise RepositoryError(
            "not-a-git-repository", f"{path} is not usable as a Git repository."
        )
    return completed.stdout.strip()


class GitRepository:
    """An already-existing Git repository. It is never created or modified."""

    def __init__(self, path: str | Path) -> None:
        candidate = Path(path).expanduser().resolve()
        if not candidate.is_dir():
            raise RepositoryError("repository-missing", f"{candidate} is not a directory.")
        self.path = Path(_git(candidate, "rev-parse", "--show-toplevel")).resolve()

    def snapshot(self) -> RepositorySnapshot:
        commit = _git(self.path, "rev-parse", "--verify", "HEAD")
        ref = _git(self.path, "rev-parse", "--abbrev-ref", "HEAD")
        dirty = bool(_git(self.path, "status", "--porcelain"))
        return RepositorySnapshot(
            path=str(self.path), ref=ref, commit=commit, dirty=dirty
        )
