"""The worktree and change-set gates that every iteration must pass.

These tests drive the ported module directly: they prove an iteration gets a
private worktree pinned to the approved commit, that a rework reuses it, and that
delivery refuses a change set that was not reported, was never committed, or looks
like a credential.
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
    commit_worktree,
    create_task_workspace,
    ensure_task_workspace,
    head_commit,
    inspect_change_set,
    is_clean,
    is_worktree,
    remove_task_workspace,
    resolve_commit,
    resolve_repository_root,
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

    def test_creating_again_discards_the_previous_attempt(self) -> None:
        first = self.workspace()
        (first.path / "scratch.txt").write_text("attempt one\n", encoding="utf-8")
        commit_worktree(first.path, "attempt one")

        second = self.workspace()

        self.assertEqual(head_commit(second.path), self.base)
        self.assertEqual(is_clean(second.path), True)
        self.assertEqual((second.path / "scratch.txt").exists(), False)

    def test_an_existing_worktree_is_reused_so_rework_builds_on_it(self) -> None:
        first = self.workspace()
        (first.path / "change.py").write_text("VALUE = 1\n", encoding="utf-8")
        attempt = commit_worktree(first.path, "attempt one")

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
    def test_a_commit_records_the_modification_and_returns_the_new_head(self) -> None:
        workspace = self.workspace()
        (workspace.path / "hello.py").write_text("VALUE = 1\n", encoding="utf-8")

        committed = commit_worktree(workspace.path, "add hello")

        self.assertNotEqual(committed, self.base)
        self.assertEqual(head_commit(workspace.path), committed)
        self.assertTrue(is_clean(workspace.path))

    def test_an_iteration_without_a_modification_is_refused(self) -> None:
        workspace = self.workspace()

        with self.assertRaises(WorkspaceError) as caught:
            commit_worktree(workspace.path, "nothing")

        self.assertEqual(caught.exception.code, "no-changes")


class ChangeSetTests(WorkspaceCase):
    def reviewed(self, task_key: str = "task-1"):
        workspace = self.workspace(task_key)
        (workspace.path / "hello.py").write_text("VALUE = 1\n", encoding="utf-8")
        commit = commit_worktree(workspace.path, "add hello")
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
        commit = commit_worktree(workspace.path, "add env")

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
        commit = commit_worktree(workspace.path, "add credentials")

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
        commit = commit_worktree(workspace.path, "add big file")

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
        commit = commit_worktree(workspace.path, "add files")

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
