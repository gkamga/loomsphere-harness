"""Schema migrations must keep every recorded row readable.

Phase 2C.1 wrote schema v1: two roles (``intake``, ``architect``) and no
implementation tables. Phase 2C.2 needs five roles, the delivery statuses, and the
iteration/deliverable/acceptance tables. Phase 2C.3 adds the ``recoveries`` table
that records a human's authorization to continue a failed Builder's work. SQLite
cannot alter a ``CHECK`` constraint, so v1 -> v2 rebuilds ``tasks`` and ``runs``;
these tests prove the rebuild preserves recorded evidence, that a v1 database ends
at the current version, and that everything stays fail-closed on a version it does
not know.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from loomsphere_harness.store import SCHEMA_VERSION, SqliteStore, StoreError

V1_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE project (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  repo_path TEXT NOT NULL, harness_root TEXT NOT NULL,
  suite_digest TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE tasks (
  id TEXT PRIMARY KEY, seq INTEGER NOT NULL UNIQUE,
  title TEXT NOT NULL, requirements TEXT NOT NULL, acceptance_criteria TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN (
    'submitted','intake_running','intake_failed','needs_clarification',
    'intake_complete','architect_running','architect_failed',
    'awaiting_approval','approved','rejected')),
  repo_ref TEXT NOT NULL, repo_commit TEXT NOT NULL, repo_dirty INTEGER NOT NULL,
  created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE runs (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  role TEXT NOT NULL CHECK (role IN ('intake','architect')),
  status TEXT NOT NULL CHECK (status IN ('running','succeeded','failed','interrupted')),
  prompt_digest TEXT, persona_digest TEXT, catalog_digest TEXT, suite_digest TEXT,
  skill_selection TEXT, model TEXT, runtime TEXT,
  response_text TEXT, failure_reason TEXT, failure_detail TEXT, events TEXT,
  started_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE intake_assessments (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  run_id TEXT NOT NULL REFERENCES runs(id), digest TEXT NOT NULL,
  document TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE proposals (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  run_id TEXT NOT NULL REFERENCES runs(id), digest TEXT NOT NULL,
  document TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN
    ('pending','approved','rejected')), created_at TEXT NOT NULL
);
CREATE TABLE approvals (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  proposal_id TEXT NOT NULL UNIQUE REFERENCES proposals(id),
  proposal_digest TEXT NOT NULL, decision TEXT NOT NULL CHECK (decision IN
    ('approved','rejected')), actor TEXT NOT NULL, reason TEXT,
  repo_commit TEXT, decided_at TEXT NOT NULL
);
CREATE TABLE audit_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, at TEXT NOT NULL,
  actor TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL,
  prev_hash TEXT NOT NULL, hash TEXT NOT NULL
);
"""

TASK_ID = "T-1"
RUN_ID = "R-1"


class Migration(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "state.sqlite3"

    # -- fixtures ------------------------------------------------------------
    def _v1_database(self, *, version: str = "1") -> Path:
        connection = sqlite3.connect(self.path)
        try:
            connection.executescript(V1_SCHEMA)
            connection.execute("INSERT INTO meta VALUES ('schema_version', ?)", (version,))
            connection.execute(
                "INSERT INTO project VALUES (1, '/repo', '/harness', 'suite', 'now')"
            )
            connection.execute(
                "INSERT INTO tasks VALUES (?, 1, 'Title', 'Requirements', 'Criteria',"
                " 'approved', 'main', ?, 0, 'tester', 'now', 'now')",
                (TASK_ID, "a" * 40),
            )
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, 'architect', 'succeeded', 'p', 'pe', 'c',"
                " 's', NULL, 'model', 'runtime', 'body', NULL, NULL, NULL, 'now', 'now')",
                (RUN_ID, TASK_ID),
            )
            connection.execute(
                "INSERT INTO proposals VALUES ('P-1', ?, ?, 'digest', '{}', 'approved', 'now')",
                (TASK_ID, RUN_ID),
            )
            connection.execute(
                "INSERT INTO approvals VALUES ('A-1', ?, 'P-1', 'digest', 'approved',"
                " 'tester', NULL, ?, 'now')",
                (TASK_ID, "a" * 40),
            )
            connection.commit()
        finally:
            connection.close()
        return self.path

    def _scalar(self, sql: str, parameters: tuple[object, ...] = ()) -> object:
        connection = sqlite3.connect(self.path)
        try:
            row = connection.execute(sql, parameters).fetchone()
            return None if row is None else row[0]
        finally:
            connection.close()

    # -- tests ---------------------------------------------------------------
    def test_open_migrates_forward_and_preserves_recorded_rows(self) -> None:
        self._v1_database()
        store = SqliteStore(self.path)
        store.check()

        self.assertEqual(
            self._scalar("SELECT value FROM meta WHERE key='schema_version'"),
            str(SCHEMA_VERSION),
        )
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM tasks"), 1)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM runs"), 1)
        self.assertEqual(self._scalar("SELECT status FROM tasks WHERE id=?", (TASK_ID,)), "approved")
        self.assertEqual(self._scalar("SELECT role FROM runs WHERE id=?", (RUN_ID,)), "architect")
        self.assertEqual(self._scalar("SELECT response_text FROM runs WHERE id=?", (RUN_ID,)), "body")
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM proposals"), 1)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM approvals"), 1)
        for table in ("iterations", "deliverables", "acceptances", "recoveries"):
            self.assertEqual(
                self._scalar("SELECT COUNT(*) FROM sqlite_master WHERE name=?", (table,)),
                1,
                f"{table} was not created by the migration",
            )

    def test_migrated_schema_accepts_the_new_roles_and_statuses(self) -> None:
        self._v1_database()
        SqliteStore(self.path).check()

        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "INSERT INTO tasks VALUES ('T-2', 2, 'Next', 'R', 'C', 'awaiting_acceptance',"
                " 'main', ?, 0, 'tester', 'now', 'now')",
                ("b" * 40,),
            )
            connection.execute(
                "INSERT INTO runs VALUES ('R-2', 'T-2', 'reviewer', 'succeeded', NULL, NULL,"
                " NULL, NULL, NULL, 'model', 'runtime', 'body', NULL, NULL, NULL, 'now', NULL)"
            )
            connection.commit()
        finally:
            connection.close()

        self.assertEqual(self._scalar("SELECT status FROM tasks WHERE id='T-2'"), "awaiting_acceptance")
        self.assertEqual(self._scalar("SELECT role FROM runs WHERE id='R-2'"), "reviewer")

    def test_migrated_schema_still_refuses_undeclared_roles_and_statuses(self) -> None:
        self._v1_database()
        SqliteStore(self.path).check()

        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO runs VALUES ('R-3', ?, 'auditor', 'running', NULL, NULL,"
                    " NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'now', NULL)",
                    (TASK_ID,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE tasks SET status = 'shipped' WHERE id = ?", (TASK_ID,)
                )
        finally:
            connection.close()

    def test_audit_chain_keeps_working_after_the_migration(self) -> None:
        self._v1_database()
        store = SqliteStore(self.path)
        store.check()
        self.assertEqual(store.verify_audit_chain(), 0)

        store.audit(TASK_ID, "tester", "post.migration", {"ok": True})

        self.assertEqual(store.verify_audit_chain(), 1)
        events = store.audit_events(TASK_ID)
        self.assertEqual([event["event"] for event in events], ["post.migration"])

    def test_initialize_migrates_then_refuses_a_foreign_repository(self) -> None:
        self._v1_database()
        store = SqliteStore(self.path)
        store.initialize(
            repo_path="/repo", harness_root="/harness", suite_digest="suite", actor="tester"
        )
        self.assertEqual(
            self._scalar("SELECT value FROM meta WHERE key='schema_version'"),
            str(SCHEMA_VERSION),
        )

        with self.assertRaises(StoreError) as caught:
            store.initialize(
                repo_path="/other", harness_root="/harness", suite_digest="suite", actor="tester"
            )
        self.assertEqual(caught.exception.code, "project-mismatch")

    def test_a_v2_database_gains_only_the_recovery_table(self) -> None:
        self._v1_database()
        SqliteStore(self.path).check()
        # Roll the version back to what a 2C.2 database recorded, with its rows intact.
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("DROP TABLE recoveries")
            connection.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
            connection.commit()
        finally:
            connection.close()

        store = SqliteStore(self.path)
        store.check()

        self.assertEqual(
            self._scalar("SELECT value FROM meta WHERE key='schema_version'"),
            str(SCHEMA_VERSION),
        )
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM tasks"), 1)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM runs"), 1)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM proposals"), 1)
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM sqlite_master WHERE name='recoveries'"), 1
        )
        self.assertEqual(store.verify_audit_chain(), 0)

    def test_a_recovery_record_cannot_be_rewritten_or_deleted(self) -> None:
        self._v1_database()
        SqliteStore(self.path).check()

        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "INSERT INTO iterations (id, task_id, number, status, base_commit,"
                " workspace_digest, created_at, updated_at) VALUES"
                " ('I-1', ?, 1, 'failed', ?, 'd', 'now', 'now')",
                (TASK_ID, "a" * 40),
            )
            connection.execute(
                "INSERT INTO recoveries VALUES ('RC-1', ?, 'I-1', NULL, 'r', 'd', ?, ?, 's',"
                " 'r', '[]', 'c', 'tester', 'now')",
                (TASK_ID, "a" * 40, "b" * 40),
            )
            connection.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE recoveries SET actor='other' WHERE id='RC-1'")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM recoveries WHERE id='RC-1'")
        finally:
            connection.close()

    def test_an_unknown_version_fails_closed_without_touching_the_database(self) -> None:
        self._v1_database(version="9")
        store = SqliteStore(self.path)

        with self.assertRaises(StoreError) as caught:
            store.check()

        self.assertEqual(caught.exception.code, "state-version")
        self.assertEqual(self._scalar("SELECT value FROM meta WHERE key='schema_version'"), "9")
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM sqlite_master WHERE name='iterations'"), 0
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
