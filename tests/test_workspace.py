"""The worktree and change-set gates that every iteration must pass.

These tests drive the ported module directly: they prove an iteration gets a
private worktree pinned to the approved commit, that a rework reuses it, and that
delivery refuses a change set that was not reported, was never committed, or looks
like a credential. The recovery tests prove the same boundary is not weakened when
a failed Builder's work is inspected instead of delivered: resuming never creates
anything, the inventory is a read-only digest of exactly what is on disk, and
screening writes nothing.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from loomsphere_harness.workspace import (
    ChangeSet,
    WorkspaceError,
    assert_disjoint,
    assert_within,
    changed_paths,
    commit_worktree,
    create_task_workspace,
    current_branch,
    ensure_task_workspace,
    head_commit,
    inspect_change_set,
    is_ancestor,
    is_clean,
    is_worktree,
    recoverable_inventory,
    remove_task_workspace,
    resolve_commit,
    resolve_repository_root,
    resume_task_workspace,
    screen_change_set,
    task_branch_name,
    validate_task_key,
    workspace_path_for,
)
from support import GIT_ENV, TempCase, make_consumer_repo


class WorkspaceCase(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = make_consumer_repo(self.tmp)
        self.root = self.tmp / "workspaces"
        self.base = resolve_commit(self.repo, "HEAD")

    def workspace(self, task_key: str = "task-1"):
        return create_task_workspace(self.repo, self.root, task_key, self.base)


class TaskKeyTests(unittest.TestCase):
    def test_a_key_becomes_a_path_and_a_private_branch(self) -> None:
        self.assertEqual(validate_task_key("task-1"), "task-1")
        self.assertEqual(task_branch_name("task-1"), "loomsphere/task-task-1")
        self.assertEqual(
            workspace_path_for("/tmp/ws", "TASK-1"), Path("/tmp/ws/task-task-1")
        )

    def test_a_key_that_could_escape_the_workspace_root_is_refused(self) -> None:
        for key in ("../escape", "with/slash", "", ".hidden", "-leading-dash", "a" * 65):
            with self.subTest(key=key):
                with self.assertRaises(WorkspaceError) as caught:
                    validate_task_key(key)
                self.assertEqual(caught.exception.code, "invalid-task-key")

    def test_the_workspace_root_must_not_live_inside_the_repository(self) -> None:
        assert_disjoint("/tmp/repo", "/tmp/workspaces")
        with self.assertRaises(WorkspaceError) as caught:
            assert_disjoint("/tmp/repo", "/tmp/repo/workspaces")
        self.assertEqual(caught.exception.code, "workspace-overlaps-repository")

    def test_a_path_outside_the_workspace_root_is_refused(self) -> None:
        assert_within("/tmp/workspaces", "/tmp/workspaces/task")
        with self.assertRaises(WorkspaceError) as caught:
            assert_within("/tmp/workspaces", "/tmp/elsewhere/task")
        self.assertEqual(caught.exception.code, "workspace-path-outside-root")


class CreateWorkspaceTests(WorkspaceCase):
    def test_a_new_worktree_is_pinned_to_the_approved_commit(self) -> None:
        workspace = self.workspace()

        self.assertTrue(workspace.path.is_dir())
        self.assertTrue(is_worktree(workspace.path))
        self.assertEqual(workspace.branch, "loomsphere/task-task-1")
        self.assertEqual(workspace.commit, self.base)
        self.assertEqual(head_commit(workspace.path), self.base)
        self.assertTrue(is_clean(workspace.path))

    def test_creating_again_preserves_the_previous_attempt(self) -> None:
        first = self.workspace()
        (first.path / "scratch.txt").write_text("attempt one\n", encoding="utf-8")
        attempt = commit_worktree(first.path, "attempt one", expected=["scratch.txt"])

        with self.assertRaises(WorkspaceError) as caught:
            self.workspace()

        self.assertEqual(caught.exception.code, "workspace-exists")
        # The refusal is the point: the preserved bytes are still there.
        self.assertTrue((first.path / "scratch.txt").is_file())
        self.assertEqual(head_commit(first.path), attempt)
        branches = subprocess.run(
            ["git", "-C", str(self.repo), *GIT_ENV, "show-ref", "--verify",
             "refs/heads/loomsphere/task-task-1"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(branches.returncode, 0, "the task branch was deleted")

    def test_replacing_requires_an_explicit_decision(self) -> None:
        first = self.workspace()
        (first.path / "scratch.txt").write_text("attempt one\n", encoding="utf-8")
        commit_worktree(first.path, "attempt one", expected=["scratch.txt"])

        second = create_task_workspace(
            self.repo, self.root, "task-1", self.base, replace_existing=True
        )

        self.assertEqual(head_commit(second.path), self.base)
        self.assertTrue(is_clean(second.path))
        self.assertFalse((second.path / "scratch.txt").exists())

    def test_an_existing_worktree_is_reused_so_rework_builds_on_it(self) -> None:
        first = self.workspace()
        (first.path / "change.py").write_text("VALUE = 1\n", encoding="utf-8")
        attempt = commit_worktree(first.path, "attempt one", expected=["change.py"])

        again = ensure_task_workspace(self.repo, self.root, "task-1", self.base)

        self.assertEqual(again.path, first.path)
        self.assertEqual(again.commit, attempt)
        self.assertEqual(again.base_commit, self.base)
        self.assertTrue((again.path / "change.py").is_file())

    def test_a_missing_worktree_is_created_again(self) -> None:
        workspace = self.workspace()
        remove_task_workspace(self.repo, self.root, workspace.path, workspace.branch)

        again = ensure_task_workspace(self.repo, self.root, "task-1", self.base)

        self.assertTrue(is_worktree(again.path))
        self.assertEqual(head_commit(again.path), self.base)


class CommitTests(WorkspaceCase):
    def committed_paths(self, path: Path, commit: str) -> list[str]:
        shown = subprocess.run(
            ["git", "-C", str(path), *GIT_ENV, "show", "--pretty=format:",
             "--name-only", "-z", commit],
            capture_output=True, text=True, check=True,
        )
        return sorted(name for name in shown.stdout.split("\0") if name)

    def test_a_commit_records_the_modification_and_returns_the_new_head(self) -> None:
        workspace = self.workspace()
        (workspace.path / "hello.py").write_text("VALUE = 1\n", encoding="utf-8")

        committed = commit_worktree(workspace.path, "add hello", expected=["hello.py"])

        self.assertNotEqual(committed, self.base)
        self.assertEqual(head_commit(workspace.path), committed)
        self.assertTrue(is_clean(workspace.path))

    def test_an_iteration_without_a_modification_is_refused(self) -> None:
        workspace = self.workspace()

        with self.assertRaises(WorkspaceError) as caught:
            commit_worktree(workspace.path, "nothing", expected=[])

        self.assertEqual(caught.exception.code, "no-changes")

    def test_a_commit_covers_exactly_the_set_it_was_given(self) -> None:
        workspace = self.workspace()
        (workspace.path / "reported.py").write_text("VALUE = 1\n", encoding="utf-8")

        committed = commit_worktree(
            workspace.path, "add reported", expected=["reported.py"]
        )

        self.assertEqual(
            self.committed_paths(workspace.path, committed), ["reported.py"]
        )
        self.assertTrue(is_clean(workspace.path))

    def test_a_file_outside_the_screened_set_is_never_committed(self) -> None:
        # The commit stages with --force, so a path the caller did not screen must
        # stop the commit rather than ride along with it. Nothing is left behind
        # but the untouched working tree.
        workspace = self.workspace()
        (workspace.path / "screened.py").write_text("VALUE = 1\n", encoding="utf-8")
        (workspace.path / "smuggled.py").write_text("VALUE = 2\n", encoding="utf-8")

        with self.assertRaises(WorkspaceError) as caught:
            commit_worktree(workspace.path, "add screened", expected=["screened.py"])

        self.assertEqual(caught.exception.code, "staged-set-mismatch")
        self.assertEqual(head_commit(workspace.path), self.base)
        self.assertFalse(is_clean(workspace.path))
        self.assertTrue((workspace.path / "smuggled.py").is_file())


class ChangeSetTests(WorkspaceCase):
    def reviewed(self, task_key: str = "task-1"):
        workspace = self.workspace(task_key)
        (workspace.path / "hello.py").write_text("VALUE = 1\n", encoding="utf-8")
        commit = commit_worktree(workspace.path, "add hello", expected=["hello.py"])
        return workspace, commit

    def test_the_reviewed_commit_is_reported_in_full(self) -> None:
        workspace, commit = self.reviewed()

        change_set = inspect_change_set(
            workspace.path,
            base_commit=self.base,
            reviewed_commit=commit,
            reported_paths={"hello.py"},
        )

        self.assertIsInstance(change_set, ChangeSet)
        self.assertEqual(change_set.paths, ["hello.py"])
        self.assertEqual(change_set.head_commit, commit)
        self.assertEqual(change_set.base_commit, self.base)
        self.assertIn("VALUE = 1", change_set.diff)
        self.assertRegex(change_set.workspace_digest, r"^[0-9a-f]{64}$")

    def test_a_path_the_implementation_did_not_report_blocks_delivery(self) -> None:
        workspace, commit = self.reviewed()

        with self.assertRaises(WorkspaceError) as caught:
            inspect_change_set(
                workspace.path,
                base_commit=self.base,
                reviewed_commit=commit,
                reported_paths={"other.py"},
            )

        self.assertEqual(caught.exception.code, "unauthorized-path")

    def test_uncommitted_work_that_was_never_validated_blocks_delivery(self) -> None:
        workspace, commit = self.reviewed()
        (workspace.path / "hello.py").write_text("VALUE = 2\n", encoding="utf-8")

        with self.assertRaises(WorkspaceError) as caught:
            inspect_change_set(
                workspace.path, base_commit=self.base, reviewed_commit=commit
            )

        self.assertEqual(caught.exception.code, "workspace-dirty")

    def test_a_head_that_is_not_the_reviewed_commit_blocks_delivery(self) -> None:
        workspace, _ = self.reviewed()

        with self.assertRaises(WorkspaceError) as caught:
            inspect_change_set(
                workspace.path, base_commit=self.base, reviewed_commit=self.base
            )

        self.assertEqual(caught.exception.code, "commit-mismatch")

    def test_a_commit_that_changes_nothing_blocks_delivery(self) -> None:
        workspace = self.workspace()
        # commit_worktree always refuses an empty change, so this is the only way
        # a Builder could produce a reviewed commit with nothing in it.
        subprocess.run(
            [
                "git", "-C", str(workspace.path), *GIT_ENV,
                "commit", "--allow-empty", "-q", "-m", "empty",
            ],
            check=True,
        )
        empty = head_commit(workspace.path)

        with self.assertRaises(WorkspaceError) as caught:
            inspect_change_set(
                workspace.path, base_commit=self.base, reviewed_commit=empty
            )

        self.assertEqual(caught.exception.code, "empty-change-set")

    def test_a_credential_path_blocks_delivery_even_when_reported(self) -> None:
        workspace = self.workspace()
        (workspace.path / ".env").write_text("TOKEN=placeholder\n", encoding="utf-8")
        commit = commit_worktree(workspace.path, "add env", expected=[".env"])

        with self.assertRaises(WorkspaceError) as caught:
            inspect_change_set(
                workspace.path,
                base_commit=self.base,
                reviewed_commit=commit,
                reported_paths={".env"},
            )

        self.assertEqual(caught.exception.code, "secret-path")

    def test_credential_content_blocks_delivery(self) -> None:
        workspace = self.workspace()
        (workspace.path / "credentials.txt").write_text(
            "AKIA" + "A" * 16 + "\n", encoding="utf-8"
        )
        commit = commit_worktree(workspace.path, "add credentials", expected=["credentials.txt"])

        with self.assertRaises(WorkspaceError) as caught:
            inspect_change_set(
                workspace.path,
                base_commit=self.base,
                reviewed_commit=commit,
                reported_paths={"credentials.txt"},
            )

        self.assertEqual(caught.exception.code, "secret-content")

    def test_a_file_too_large_to_screen_blocks_delivery(self) -> None:
        workspace = self.workspace()
        (workspace.path / "big.txt").write_text("x" * 2048, encoding="utf-8")
        commit = commit_worktree(workspace.path, "add big file", expected=["big.txt"])

        with self.assertRaises(WorkspaceError) as caught:
            inspect_change_set(
                workspace.path,
                base_commit=self.base,
                reviewed_commit=commit,
                max_scan_bytes=1024,
            )

        self.assertEqual(caught.exception.code, "file-too-large")

    def test_too_many_files_blocks_delivery(self) -> None:
        workspace = self.workspace()
        for index in range(3):
            (workspace.path / f"file{index}.txt").write_text("x\n", encoding="utf-8")
        commit = commit_worktree(
            workspace.path, "add files", expected=[f"file{index}.txt" for index in range(3)]
        )

        with self.assertRaises(WorkspaceError) as caught:
            inspect_change_set(
                workspace.path, base_commit=self.base, reviewed_commit=commit, max_files=2
            )

        self.assertEqual(caught.exception.code, "too-many-files")

    def test_a_directory_that_is_not_a_worktree_is_refused(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()

        with self.assertRaises(WorkspaceError) as caught:
            inspect_change_set(plain, base_commit=self.base, reviewed_commit=self.base)

        self.assertEqual(caught.exception.code, "not-a-worktree")


class RecoveryInventoryTests(WorkspaceCase):
    """The read-only inspection a failed Builder's work goes through."""

    def test_resuming_never_creates_a_worktree(self) -> None:
        with self.assertRaises(WorkspaceError) as caught:
            resume_task_workspace(self.root, "task-1")

        self.assertEqual(caught.exception.code, "workspace-missing")
        self.assertFalse(workspace_path_for(self.root, "task-1").exists())

    def test_resuming_reports_the_worktree_that_already_exists(self) -> None:
        workspace = self.workspace()
        (workspace.path / "change.py").write_text("VALUE = 1\n", encoding="utf-8")
        attempt = commit_worktree(workspace.path, "attempt one", expected=["change.py"])

        resumed = resume_task_workspace(self.root, "task-1")

        self.assertEqual(resumed.path, workspace.path)
        self.assertEqual(resumed.branch, workspace.branch)
        self.assertEqual(resumed.commit, attempt)

    def test_changed_paths_sees_committed_and_untracked_work(self) -> None:
        workspace = self.workspace()
        (workspace.path / "committed.py").write_text("VALUE = 1\n", encoding="utf-8")
        commit_worktree(workspace.path, "attempt one", expected=["committed.py"])
        (workspace.path / "untracked.py").write_text("VALUE = 2\n", encoding="utf-8")

        self.assertEqual(
            changed_paths(workspace.path, self.base), ["committed.py", "untracked.py"]
        )

    def test_the_inventory_digest_is_stable_and_tracks_content(self) -> None:
        workspace = self.workspace()
        (workspace.path / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
        (workspace.path / "b.py").write_text("print('a')\n", encoding="utf-8")

        digest, entries = recoverable_inventory(workspace.path, self.base)
        again, repeated = recoverable_inventory(workspace.path, self.base)

        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(digest, again)
        self.assertEqual(entries, repeated)
        self.assertEqual([entry["path"] for entry in entries], ["a.py", "b.py"])
        self.assertEqual([entry["mode"] for entry in entries], ["file", "file"])

        (workspace.path / "a.py").write_text("VALUE = 2\n", encoding="utf-8")

        self.assertNotEqual(recoverable_inventory(workspace.path, self.base)[0], digest)

    def test_the_inventory_records_a_deleted_file_by_absence(self) -> None:
        workspace = self.workspace()
        tracked = "README.md" if (workspace.path / "README.md").exists() else None
        if tracked is None:
            self.skipTest("the fixture repository has no tracked file to delete")
        (workspace.path / tracked).unlink()

        _, entries = recoverable_inventory(workspace.path, self.base)

        self.assertEqual(entries, [{"path": tracked, "mode": "deleted", "sha256": ""}])

    def test_the_inventory_refuses_too_many_files(self) -> None:
        workspace = self.workspace()
        for index in range(3):
            (workspace.path / f"file{index}.txt").write_text("x\n", encoding="utf-8")

        with self.assertRaises(WorkspaceError) as caught:
            recoverable_inventory(workspace.path, self.base, max_files=2)

        self.assertEqual(caught.exception.code, "too-many-files")

    def test_the_inventory_refuses_a_file_too_large_to_read(self) -> None:
        workspace = self.workspace()
        (workspace.path / "big.txt").write_text("x" * 2048, encoding="utf-8")

        with self.assertRaises(WorkspaceError) as caught:
            recoverable_inventory(workspace.path, self.base, max_scan_bytes=1024)

        self.assertEqual(caught.exception.code, "file-too-large")

    def test_the_inventory_requires_a_real_worktree(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()

        with self.assertRaises(WorkspaceError) as caught:
            recoverable_inventory(plain, self.base)

        self.assertEqual(caught.exception.code, "workspace-missing")

    def test_the_inventory_refuses_an_invalid_commit(self) -> None:
        workspace = self.workspace()

        with self.assertRaises(WorkspaceError) as caught:
            recoverable_inventory(workspace.path, "not-a-commit")

        self.assertEqual(caught.exception.code, "invalid-commit")

    def test_screening_refuses_a_credential_path(self) -> None:
        workspace = self.workspace()
        (workspace.path / ".env").write_text("TOKEN=placeholder\n", encoding="utf-8")

        with self.assertRaises(WorkspaceError) as caught:
            screen_change_set(workspace.path, [".env"])

        self.assertEqual(caught.exception.code, "secret-path")

    def test_screening_refuses_credential_content(self) -> None:
        workspace = self.workspace()
        (workspace.path / "credentials.txt").write_text(
            "AKIA" + "A" * 16 + "\n", encoding="utf-8"
        )

        with self.assertRaises(WorkspaceError) as caught:
            screen_change_set(workspace.path, ["credentials.txt"])

        self.assertEqual(caught.exception.code, "secret-content")

    def test_screening_writes_nothing(self) -> None:
        workspace = self.workspace()
        (workspace.path / "ok.py").write_text("VALUE = 1\n", encoding="utf-8")
        before = head_commit(workspace.path)

        screen_change_set(workspace.path, ["ok.py"])

        self.assertEqual(head_commit(workspace.path), before)
        self.assertFalse(is_clean(workspace.path))  # the file is still uncommitted

    def test_ancestry_is_checked_against_the_approved_base(self) -> None:
        workspace = self.workspace()
        (workspace.path / "change.py").write_text("VALUE = 1\n", encoding="utf-8")
        attempt = commit_worktree(workspace.path, "attempt one", expected=["change.py"])

        self.assertTrue(is_ancestor(workspace.path, self.base, attempt))
        self.assertFalse(is_ancestor(workspace.path, attempt, self.base))
        self.assertFalse(is_ancestor(workspace.path, "not-a-commit", attempt))


class ScreeningBoundaryTests(WorkspaceCase):
    """What discovery, screening, and the commit must agree on.

    The commit stages with ``--force`` so that Builders can add files a consumer
    repository ignores. Staging and screening therefore have to enumerate the
    same set, or a change could be screened as one list and committed as
    another.
    """

    def freeze(self, workspace, names: list[str]) -> str:
        """The exact sequence the Builder freeze runs: screen, then commit."""

        screen_change_set(workspace.path, names)
        return commit_worktree(workspace.path, "freeze", expected=names)

    def test_discovery_reports_a_git_ignored_file(self) -> None:
        workspace = self.workspace()
        (workspace.path / ".gitignore").write_text("*.local\n", encoding="utf-8")
        (workspace.path / "deploy.local").write_text("VALUE = 1\n", encoding="utf-8")

        self.assertEqual(
            changed_paths(workspace.path, self.base), [".gitignore", "deploy.local"]
        )

    def test_an_ignored_credential_is_screened_and_never_committed(self) -> None:
        workspace = self.workspace()
        (workspace.path / ".gitignore").write_text("*.local\n", encoding="utf-8")
        (workspace.path / "deploy.local").write_text(
            "AWS_KEY=AKIA" + "A" * 16 + "\n", encoding="utf-8"
        )

        names = changed_paths(workspace.path, self.base)

        with self.assertRaises(WorkspaceError) as caught:
            self.freeze(workspace, names)

        self.assertEqual(caught.exception.code, "secret-content")
        self.assertEqual(head_commit(workspace.path), self.base)
        # Nothing reached the history: the credential is still only on disk.
        self.assertTrue((workspace.path / "deploy.local").is_file())
        self.assertNotIn(
            "deploy.local", self.committed_files(workspace.path)
        )

    def committed_files(self, path: Path) -> list[str]:
        listed = subprocess.run(
            ["git", "-C", str(path), *GIT_ENV, "ls-tree", "-r", "--name-only", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return listed.stdout.splitlines()

    def test_a_symlink_in_the_change_set_is_refused(self) -> None:
        workspace = self.workspace()
        (workspace.path / "hello.py").write_text("VALUE = 1\n", encoding="utf-8")
        (workspace.path / "link.py").symlink_to("hello.py")

        with self.assertRaises(WorkspaceError) as caught:
            screen_change_set(workspace.path, ["link.py"])

        self.assertEqual(caught.exception.code, "symlink-refused")

    def test_a_symlink_that_escapes_the_worktree_is_refused(self) -> None:
        workspace = self.workspace()
        outside = self.tmp / "outside.txt"
        outside.write_text("VALUE = 1\n", encoding="utf-8")
        (workspace.path / "escape.py").symlink_to(outside)

        with self.assertRaises(WorkspaceError) as caught:
            screen_change_set(workspace.path, ["escape.py"])

        self.assertEqual(caught.exception.code, "symlink-refused")

    def test_the_recovery_inventory_refuses_a_symlink_too(self) -> None:
        workspace = self.workspace()
        (workspace.path / "hello.py").write_text("VALUE = 1\n", encoding="utf-8")
        (workspace.path / "link.py").symlink_to("hello.py")

        with self.assertRaises(WorkspaceError) as caught:
            recoverable_inventory(workspace.path, self.base)

        self.assertEqual(caught.exception.code, "symlink-refused")

    def test_the_branch_is_read_from_the_worktree_itself(self) -> None:
        workspace = self.workspace()

        self.assertEqual(current_branch(workspace.path), "loomsphere/task-task-1")
        self.assertEqual(workspace.branch, current_branch(workspace.path))

    def test_a_detached_head_has_no_branch_to_report(self) -> None:
        workspace = self.workspace()
        subprocess.run(
            ["git", "-C", str(workspace.path), *GIT_ENV, "checkout", "--detach"],
            capture_output=True, text=True, check=True,
        )

        with self.assertRaises(WorkspaceError) as caught:
            current_branch(workspace.path)

        self.assertEqual(caught.exception.code, "detached-head")


class RepositoryRootTests(TempCase):
    def test_a_real_working_tree_resolves_to_itself(self) -> None:
        repo = make_consumer_repo(self.tmp)

        self.assertEqual(resolve_repository_root(repo), repo.resolve())

    def test_a_directory_that_is_not_a_working_tree_is_refused(self) -> None:
        repo = make_consumer_repo(self.tmp)
        nested = repo / "package"
        nested.mkdir()

        with self.assertRaises(WorkspaceError) as caught:
            resolve_repository_root(nested)

        self.assertEqual(caught.exception.code, "repository-root-invalid")

    def test_a_missing_root_is_refused(self) -> None:
        with self.assertRaises(WorkspaceError) as caught:
            resolve_repository_root(self.tmp / "absent")

        self.assertEqual(caught.exception.code, "repository-root-missing")

    def test_a_reference_that_could_be_a_git_option_is_refused(self) -> None:
        repo = make_consumer_repo(self.tmp)

        for reference in ("--upload-pack=touch /tmp/x", "HEAD..main", ""):
            with self.subTest(reference=reference):
                with self.assertRaises(WorkspaceError) as caught:
                    resolve_commit(repo, reference)
                self.assertEqual(caught.exception.code, "invalid-reference")

    def test_a_readme_is_untouched_by_the_worktree_operations(self) -> None:
        repo = make_consumer_repo(self.tmp)
        before = (repo / "README.md").read_text(encoding="utf-8")

        create_task_workspace(repo, self.tmp / "workspaces", "task-1", "HEAD")

        self.assertEqual((repo / "README.md").read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
