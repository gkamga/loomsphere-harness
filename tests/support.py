"""Shared helpers: a hermetic harness Git repository and a disposable consumer."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "tools" / "harness_install.py"
PAYLOAD = (".github", "docs", "tools", "consumer-paths.json", "harness.manifest.json", "README.md")
GIT_ENV = ["-c", "user.email=test@example.invalid", "-c", "user.name=test", "-c", "commit.gpgsign=false"]


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *GIT_ENV, *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"git {args} failed: {done.stderr}")
    return done.stdout.strip()


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def make_harness_repo(parent: Path) -> Path:
    """Copy the working tree under test into a fresh Git repository (one commit)."""

    repo = parent / "harness"
    repo.mkdir()
    for name in PAYLOAD:
        source = ROOT / name
        if source.is_dir():
            ignore = ["__pycache__"] + (["workflows"] if name == ".github" else [])
            shutil.copytree(source, repo / name, ignore=shutil.ignore_patterns(*ignore))
        elif source.exists():
            shutil.copy2(source, repo / name)
    git(repo, "init", "-q", "-b", "main")
    commit_all(repo, "harness v1")
    return repo


def make_consumer_repo(parent: Path, name: str = "consumer") -> Path:
    repo = parent / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("# unrelated project\n", encoding="utf-8")
    commit_all(repo, "init")
    return repo


def run_installer(*args: object) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(INSTALLER), *map(str, args)], capture_output=True, text=True)


def run_tool(*args: object, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True, cwd=cwd)


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-test.")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def lock(self, consumer: Path) -> dict:
        return json.loads((consumer / ".harness-lock.json").read_text(encoding="utf-8"))
