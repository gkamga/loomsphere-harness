"""SQLite persistence for tasks, runs, evidence, approvals, and the audit trail.

Everything the runtime needs after a restart lives here. Approvals and audit events
are append-only (enforced by triggers), the audit trail is hash-chained so an edit
is detectable, and an approval row is bound to the SHA-256 digest of the exact
proposal it decided.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:  # pragma: no cover - the fallback exists only for platforms without POSIX locks
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

SCHEMA_VERSION = 5
#: Versions this build can open. A v1, v2, v3, or v4 database is migrated forward
#: by ``initialize``; ``check`` refuses anything else so no stage runs on data it
#: cannot read.
SUPPORTED_SCHEMA_VERSIONS = (1, 2, 3, 4, SCHEMA_VERSION)
GENESIS = "0" * 64

#: Every role that can own a run row in schema v2.
RUN_ROLES = ("intake", "architect", "builder", "qa", "reviewer")

#: Audit event that records one authorised Reviewer-only retry. Counting these
#: events is the durable retry limit; no schema column is needed.
REVIEW_RETRY_EVENT = "review.retry-authorised"

#: Audit event that records one human authorisation of a failed Builder's
#: preserved change set. The row it accompanies is what the recovery reads back.
RECOVERY_EVENT = "recovery.authorized"

#: Iteration statuses a Builder recovery may claim: ``failed`` is the normal
#: shape, ``building`` is what an interrupted process leaves behind.
RECOVERABLE_ITERATION_STATUSES = ("failed", "building")

#: Audit event that records one human authorisation to restart a Builder that
#: stopped without writing anything. The immutable row it accompanies is the
#: durable limit: counting it is what makes the restart one-time across process
#: restarts and concurrent invocations.
BUILDER_RESTART_EVENT = "builder.restart-authorized"

#: Run statuses that mean a Builder run did not finish its turn. A restart is
#: only ever authorized against one of these.
INTERRUPTED_RUN_STATUSES = ("failed", "interrupted")
RESTARTABLE_FAILURES = ("timed-out", "builder-interrupted", "interrupted")
MAX_BUILDER_RESTARTS = 1

#: Audit event that records one human authorisation to re-run only the QA stage.
#: The immutable row it accompanies is the durable limit, so the entitlement
#: survives a process restart and cannot be spent by a concurrent invocation.
QA_RECOVERY_EVENT = "qa.recovery-authorized"

#: Failure reasons a QA re-run may be authorised against. Every one of them means
#: the QA invocation stopped before it produced a report, so there is no recorded
#: QA verdict that a retry could contradict or overrule. A run that answered and
#: was refused for its format is not in this list: it made a decision.
QA_RECOVERABLE_FAILURES = (
    "resource-limit-exceeded", "timed-out", "provider-unavailable", "model-error",
    "harness-error", "empty-response", "builder-interrupted", "interrupted",
)

#: One authorised QA re-run per failed iteration. Raising this is a policy change,
#: not a configuration detail, so the store refuses any other value out loud.
MAX_QA_RECOVERIES = 1


class StoreError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


SCHEMA = """
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
    'awaiting_approval','approved','rejected',
    'implementation_running','implementation_failed','changes_required',
    'qa_running','qa_failed','review_running','review_failed',
    'awaiting_acceptance','delivered','delivery_rejected')),
  repo_ref TEXT NOT NULL, repo_commit TEXT NOT NULL, repo_dirty INTEGER NOT NULL,
  created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE runs (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  role TEXT NOT NULL CHECK (role IN ('intake','architect','builder','qa','reviewer')),
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
CREATE TABLE iterations (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  number INTEGER NOT NULL CHECK (number >= 1),
  status TEXT NOT NULL CHECK (status IN (
    'building','built','validation_failed','validating','qa_running','qa_failed',
    'review_running','review_failed','ready','changes_required','failed')),
  base_commit TEXT NOT NULL, branch TEXT, workspace_path TEXT,
  commit_sha TEXT, workspace_digest TEXT, diff_excerpt TEXT,
  builder_run_id TEXT REFERENCES runs(id), build_report TEXT, build_digest TEXT,
  validation TEXT, validation_exit_code INTEGER,
  qa_run_id TEXT REFERENCES runs(id), qa_report TEXT, qa_digest TEXT,
  reviewer_run_id TEXT REFERENCES runs(id), review_report TEXT, review_digest TEXT,
  failure_reason TEXT, failure_detail TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (task_id, number)
);
CREATE TABLE deliverables (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  iteration_id TEXT NOT NULL REFERENCES iterations(id),
  digest TEXT NOT NULL, document TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE recoveries (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  iteration_id TEXT NOT NULL UNIQUE REFERENCES iterations(id),
  builder_run_id TEXT REFERENCES runs(id),
  failure_reason TEXT NOT NULL, failure_detail TEXT,
  base_commit TEXT NOT NULL, head_commit TEXT NOT NULL,
  snapshot_digest TEXT NOT NULL, recovery_digest TEXT NOT NULL,
  changed_files TEXT NOT NULL, contract_digest TEXT NOT NULL,
  actor TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE acceptances (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  deliverable_id TEXT NOT NULL UNIQUE REFERENCES deliverables(id),
  deliverable_digest TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('accepted','rejected')),
  actor TEXT NOT NULL, reason TEXT, decided_at TEXT NOT NULL
);
CREATE TABLE builder_restarts (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  failed_iteration_id TEXT NOT NULL UNIQUE REFERENCES iterations(id),
  failed_run_id TEXT NOT NULL REFERENCES runs(id),
  restart_number INTEGER NOT NULL CHECK (restart_number >= 1),
  failure_reason TEXT NOT NULL, base_commit TEXT NOT NULL,
  proposal_id TEXT NOT NULL REFERENCES proposals(id),
  proposal_digest TEXT NOT NULL,
  approval_id TEXT NOT NULL REFERENCES approvals(id),
  contract_digest TEXT NOT NULL,
  empty_snapshot_digest TEXT NOT NULL,
  actor TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL
);
CREATE TABLE qa_recoveries (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  iteration_id TEXT NOT NULL UNIQUE REFERENCES iterations(id),
  failed_run_id TEXT NOT NULL REFERENCES runs(id),
  recovery_number INTEGER NOT NULL CHECK (recovery_number >= 1),
  failure_reason TEXT NOT NULL, failure_detail TEXT,
  base_commit TEXT NOT NULL, head_commit TEXT NOT NULL,
  workspace_digest TEXT NOT NULL,
  builder_run_id TEXT REFERENCES runs(id),
  build_digest TEXT NOT NULL,
  proposal_id TEXT NOT NULL REFERENCES proposals(id),
  proposal_digest TEXT NOT NULL,
  approval_id TEXT NOT NULL REFERENCES approvals(id),
  contract_digest TEXT NOT NULL,
  validation_output_digest TEXT NOT NULL,
  sandbox_image_id TEXT NOT NULL,
  actor TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL
);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_events
  BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_events
  BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
CREATE TRIGGER approvals_no_update BEFORE UPDATE ON approvals
  BEGIN SELECT RAISE(ABORT, 'approvals are append-only'); END;
CREATE TRIGGER approvals_no_delete BEFORE DELETE ON approvals
  BEGIN SELECT RAISE(ABORT, 'approvals are append-only'); END;
CREATE TRIGGER proposals_immutable BEFORE UPDATE OF digest, document, task_id, run_id
  ON proposals BEGIN SELECT RAISE(ABORT, 'a proposal is immutable'); END;
CREATE TRIGGER proposals_no_delete BEFORE DELETE ON proposals
  BEGIN SELECT RAISE(ABORT, 'a proposal cannot be deleted'); END;
CREATE TRIGGER intake_no_change BEFORE UPDATE ON intake_assessments
  BEGIN SELECT RAISE(ABORT, 'an assessment is immutable'); END;
CREATE TRIGGER deliverables_immutable BEFORE UPDATE ON deliverables
  BEGIN SELECT RAISE(ABORT, 'a deliverable is immutable'); END;
CREATE TRIGGER deliverables_no_delete BEFORE DELETE ON deliverables
  BEGIN SELECT RAISE(ABORT, 'a deliverable cannot be deleted'); END;
CREATE TRIGGER acceptances_no_update BEFORE UPDATE ON acceptances
  BEGIN SELECT RAISE(ABORT, 'a delivery decision is append-only'); END;
CREATE TRIGGER acceptances_no_delete BEFORE DELETE ON acceptances
  BEGIN SELECT RAISE(ABORT, 'a delivery decision is append-only'); END;
CREATE TRIGGER recoveries_no_update BEFORE UPDATE ON recoveries
  BEGIN SELECT RAISE(ABORT, 'a recovery record is immutable'); END;
CREATE TRIGGER recoveries_no_delete BEFORE DELETE ON recoveries
  BEGIN SELECT RAISE(ABORT, 'a recovery record cannot be deleted'); END;
CREATE TRIGGER builder_restarts_no_update BEFORE UPDATE ON builder_restarts
  BEGIN SELECT RAISE(ABORT, 'a Builder restart record is immutable'); END;
CREATE TRIGGER builder_restarts_no_delete BEFORE DELETE ON builder_restarts
  BEGIN SELECT RAISE(ABORT, 'a Builder restart record cannot be deleted'); END;
CREATE TRIGGER qa_recoveries_no_update BEFORE UPDATE ON qa_recoveries
  BEGIN SELECT RAISE(ABORT, 'a QA recovery record is immutable'); END;
CREATE TRIGGER qa_recoveries_no_delete BEFORE DELETE ON qa_recoveries
  BEGIN SELECT RAISE(ABORT, 'a QA recovery record cannot be deleted'); END;
"""

#: Tables added by schema v2. Kept separate so the v1→v2 migration applies exactly
#: the objects a v1 database is missing.
V2_TABLES = """
CREATE TABLE IF NOT EXISTS iterations (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  number INTEGER NOT NULL CHECK (number >= 1),
  status TEXT NOT NULL CHECK (status IN (
    'building','built','validation_failed','validating','qa_running','qa_failed',
    'review_running','review_failed','ready','changes_required','failed')),
  base_commit TEXT NOT NULL, branch TEXT, workspace_path TEXT,
  commit_sha TEXT, workspace_digest TEXT, diff_excerpt TEXT,
  builder_run_id TEXT REFERENCES runs(id), build_report TEXT, build_digest TEXT,
  validation TEXT, validation_exit_code INTEGER,
  qa_run_id TEXT REFERENCES runs(id), qa_report TEXT, qa_digest TEXT,
  reviewer_run_id TEXT REFERENCES runs(id), review_report TEXT, review_digest TEXT,
  failure_reason TEXT, failure_detail TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (task_id, number)
);
CREATE TABLE IF NOT EXISTS deliverables (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  iteration_id TEXT NOT NULL REFERENCES iterations(id),
  digest TEXT NOT NULL, document TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS acceptances (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  deliverable_id TEXT NOT NULL UNIQUE REFERENCES deliverables(id),
  deliverable_digest TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('accepted','rejected')),
  actor TEXT NOT NULL, reason TEXT, decided_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS deliverables_immutable BEFORE UPDATE ON deliverables
  BEGIN SELECT RAISE(ABORT, 'a deliverable is immutable'); END;
CREATE TRIGGER IF NOT EXISTS deliverables_no_delete BEFORE DELETE ON deliverables
  BEGIN SELECT RAISE(ABORT, 'a deliverable cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS acceptances_no_update BEFORE UPDATE ON acceptances
  BEGIN SELECT RAISE(ABORT, 'a delivery decision is append-only'); END;
CREATE TRIGGER IF NOT EXISTS acceptances_no_delete BEFORE DELETE ON acceptances
  BEGIN SELECT RAISE(ABORT, 'a delivery decision is append-only'); END;
"""

#: Objects added by schema v3: the human-authorised recovery of a failed
#: Builder's preserved change set. The row is immutable and holds the digest the
#: human quoted, so a later stage can prove what exactly was authorised.
V3_TABLES = """
CREATE TABLE IF NOT EXISTS recoveries (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  iteration_id TEXT NOT NULL UNIQUE REFERENCES iterations(id),
  builder_run_id TEXT REFERENCES runs(id),
  failure_reason TEXT NOT NULL, failure_detail TEXT,
  base_commit TEXT NOT NULL, head_commit TEXT NOT NULL,
  snapshot_digest TEXT NOT NULL, recovery_digest TEXT NOT NULL,
  changed_files TEXT NOT NULL, contract_digest TEXT NOT NULL,
  actor TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS recoveries_no_update BEFORE UPDATE ON recoveries
  BEGIN SELECT RAISE(ABORT, 'a recovery record is immutable'); END;
CREATE TRIGGER IF NOT EXISTS recoveries_no_delete BEFORE DELETE ON recoveries
  BEGIN SELECT RAISE(ABORT, 'a recovery record cannot be deleted'); END;
"""

#: Objects added by schema v4: the human-authorised, one-time restart of a Builder
#: that stopped before writing a change set. The row is immutable and unique per
#: failed iteration, so the authorization cannot be replayed or duplicated.
V4_TABLES = """
CREATE TABLE IF NOT EXISTS builder_restarts (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  failed_iteration_id TEXT NOT NULL UNIQUE REFERENCES iterations(id),
  failed_run_id TEXT NOT NULL REFERENCES runs(id),
  restart_number INTEGER NOT NULL CHECK (restart_number >= 1),
  failure_reason TEXT NOT NULL, base_commit TEXT NOT NULL,
  proposal_id TEXT NOT NULL REFERENCES proposals(id),
  proposal_digest TEXT NOT NULL,
  approval_id TEXT NOT NULL REFERENCES approvals(id),
  contract_digest TEXT NOT NULL,
  empty_snapshot_digest TEXT NOT NULL,
  actor TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS builder_restarts_no_update BEFORE UPDATE ON builder_restarts
  BEGIN SELECT RAISE(ABORT, 'a Builder restart record is immutable'); END;
CREATE TRIGGER IF NOT EXISTS builder_restarts_no_delete BEFORE DELETE ON builder_restarts
  BEGIN SELECT RAISE(ABORT, 'a Builder restart record cannot be deleted'); END;
"""

#: Objects added by schema v5: the human-authorised re-run of a QA stage whose
#: invocation failed before it produced a report. The row is immutable, unique per
#: iteration, and binds the exact change set, validation run, and approval that the
#: retry is allowed to run against, so a retry cannot drift onto other evidence.
V5_TABLES = """
CREATE TABLE IF NOT EXISTS qa_recoveries (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  iteration_id TEXT NOT NULL UNIQUE REFERENCES iterations(id),
  failed_run_id TEXT NOT NULL REFERENCES runs(id),
  recovery_number INTEGER NOT NULL CHECK (recovery_number >= 1),
  failure_reason TEXT NOT NULL, failure_detail TEXT,
  base_commit TEXT NOT NULL, head_commit TEXT NOT NULL,
  workspace_digest TEXT NOT NULL,
  builder_run_id TEXT REFERENCES runs(id),
  build_digest TEXT NOT NULL,
  proposal_id TEXT NOT NULL REFERENCES proposals(id),
  proposal_digest TEXT NOT NULL,
  approval_id TEXT NOT NULL REFERENCES approvals(id),
  contract_digest TEXT NOT NULL,
  validation_output_digest TEXT NOT NULL,
  sandbox_image_id TEXT NOT NULL,
  actor TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS qa_recoveries_no_update BEFORE UPDATE ON qa_recoveries
  BEGIN SELECT RAISE(ABORT, 'a QA recovery record is immutable'); END;
CREATE TRIGGER IF NOT EXISTS qa_recoveries_no_delete BEFORE DELETE ON qa_recoveries
  BEGIN SELECT RAISE(ABORT, 'a QA recovery record cannot be deleted'); END;
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _row(row: sqlite3.Row | None, *json_fields: str) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for name in json_fields:
        if data.get(name) is not None:
            data[name] = json.loads(data[name])
    return data


class SqliteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # -- lifecycle ---------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def exists(self) -> bool:
        return self.path.is_file()

    def initialize(
        self, *, repo_path: str, harness_root: str, suite_digest: str, actor: str
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fresh = not self.path.exists()
        connection = self._connect()
        try:
            if fresh or not self._has_schema(connection):
                connection.executescript(SCHEMA)
                connection.execute(
                    "INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
                )
            else:
                row = connection.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                version = int(row["value"]) if row is not None else 0
                if version not in SUPPORTED_SCHEMA_VERSIONS:
                    raise StoreError(
                        "state-version", "The state database version is unsupported."
                    )
                if version < SCHEMA_VERSION:
                    self._migrate(connection, version)
        finally:
            connection.close()
        with self._tx() as db:
            existing = db.execute("SELECT repo_path FROM project").fetchone()
            if existing is not None:
                if existing["repo_path"] != repo_path:
                    raise StoreError(
                        "project-mismatch",
                        "This state database belongs to a different repository.",
                    )
                return
            db.execute(
                "INSERT INTO project VALUES (1, ?, ?, ?, ?)",
                (repo_path, harness_root, suite_digest, now()),
            )
            self._audit(db, None, actor, "project.initialized", {"repo_path": repo_path, "suite_digest": suite_digest})
        try:
            self.path.chmod(0o600)
        except OSError:  # pragma: no cover
            pass

    @staticmethod
    def _has_schema(connection: sqlite3.Connection) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'meta'"
        ).fetchone() is not None

    def check(self) -> None:
        """Fail closed on a missing, foreign, or unreadable database.

        A database written by an older supported schema is migrated forward in
        place instead of being rejected: the migration only adds tables and
        widens two `CHECK` constraints, so no recorded evidence is lost.
        """

        if not self.exists():
            raise StoreError("not-initialized", "No state found; run `init` first.")
        connection = self._connect()
        try:
            if not self._has_schema(connection):
                raise StoreError("state-corrupt", "The state database has no schema.")
            row = connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                raise StoreError("state-version", "The state database version is unsupported.")
            version = int(row["value"])
            if version not in SUPPORTED_SCHEMA_VERSIONS:
                raise StoreError("state-version", "The state database version is unsupported.")
            if version < SCHEMA_VERSION:
                self._migrate(connection, version)
        except sqlite3.DatabaseError as error:
            raise StoreError("state-corrupt", f"The state database is unreadable: {error}") from error
        finally:
            connection.close()

    def _migrate(self, connection: sqlite3.Connection, version: int) -> None:
        """Apply every forward migration from ``version`` to the current schema."""

        if version == 1:
            self._migrate_v1_to_v2(connection)
            version = 2
        if version == 2:
            self._migrate_v2_to_v3(connection)
            version = 3
        if version == 3:
            self._migrate_v3_to_v4(connection)
            version = 4
        if version == 4:
            self._migrate_v4_to_v5(connection)
            version = 5
        if version != SCHEMA_VERSION:  # pragma: no cover - unreachable by construction
            raise StoreError("state-version", "The state database version is unsupported.")

    def _migrate_v4_to_v5(self, connection: sqlite3.Connection) -> None:
        """Add the QA-recovery authorization table without touching a row.

        v5, like v4 and v3, only creates objects, so every previously recorded row
        is read back unchanged and compared by count. The tables counted include
        the two later evidence tables, so a migration that dropped a recovery or a
        restart record would be caught here rather than at the next stage.
        """

        previous = {
            table: connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("tasks", "runs", "iterations", "recoveries", "builder_restarts")
        }
        connection.executescript(V5_TABLES)
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("UPDATE meta SET value = '5' WHERE key = 'schema_version'")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        for table, count in previous.items():
            now_count = connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            if now_count != count:
                raise StoreError(
                    "state-corrupt", "The schema migration did not preserve every recorded row."
                )

    def _migrate_v3_to_v4(self, connection: sqlite3.Connection) -> None:
        """Add the Builder-restart authorization table without touching a row.

        v4 only creates objects, so the existing evidence is read back unchanged
        and compared by row count, exactly as v2→v3 does.
        """

        previous = {
            table: connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("tasks", "runs", "iterations", "recoveries")
        }
        connection.executescript(V4_TABLES)
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        for table, count in previous.items():
            now_count = connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            if now_count != count:
                raise StoreError(
                    "state-corrupt", "The schema migration did not preserve every recorded row."
                )

    def _migrate_v2_to_v3(self, connection: sqlite3.Connection) -> None:
        """Add the builder-recovery table without touching a recorded row.

        v3 only creates objects, so unlike v1→v2 there is no table rebuild: the
        existing evidence is read back unchanged and compared by row count.
        """

        previous = {
            table: connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("tasks", "runs", "iterations")
        }
        connection.executescript(V3_TABLES)
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        for table, count in previous.items():
            now_count = connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            if now_count != count:
                raise StoreError(
                    "state-corrupt", "The schema migration did not preserve every recorded row."
                )

    def _migrate_v1_to_v2(self, connection: sqlite3.Connection) -> None:
        """Widen the role and status constraints and add the implementation tables.

        SQLite cannot alter a `CHECK` constraint, so the two affected tables are
        rebuilt with their rows copied verbatim. Foreign keys are enforced again
        as soon as the rebuilt tables are back under their real names.
        """

        previous_tasks = connection.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        previous_runs = connection.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            # The new tables are created first and are idempotent, so a retry after
            # a failed rebuild starts from a consistent place.
            connection.executescript(V2_TABLES)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE tasks_v2 ("
                " id TEXT PRIMARY KEY, seq INTEGER NOT NULL UNIQUE,"
                " title TEXT NOT NULL, requirements TEXT NOT NULL,"
                " acceptance_criteria TEXT NOT NULL,"
                " status TEXT NOT NULL CHECK (status IN ("
                "  'submitted','intake_running','intake_failed','needs_clarification',"
                "  'intake_complete','architect_running','architect_failed',"
                "  'awaiting_approval','approved','rejected',"
                "  'implementation_running','implementation_failed','changes_required',"
                "  'qa_running','qa_failed','review_running','review_failed',"
                "  'awaiting_acceptance','delivered','delivery_rejected')),"
                " repo_ref TEXT NOT NULL, repo_commit TEXT NOT NULL,"
                " repo_dirty INTEGER NOT NULL, created_by TEXT NOT NULL,"
                " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO tasks_v2 SELECT id, seq, title, requirements,"
                " acceptance_criteria, status, repo_ref, repo_commit, repo_dirty,"
                " created_by, created_at, updated_at FROM tasks"
            )
            connection.execute("DROP TABLE tasks")
            connection.execute("ALTER TABLE tasks_v2 RENAME TO tasks")
            connection.execute(
                "CREATE TABLE runs_v2 ("
                " id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),"
                " role TEXT NOT NULL CHECK (role IN"
                "  ('intake','architect','builder','qa','reviewer')),"
                " status TEXT NOT NULL CHECK (status IN"
                "  ('running','succeeded','failed','interrupted')),"
                " prompt_digest TEXT, persona_digest TEXT, catalog_digest TEXT,"
                " suite_digest TEXT, skill_selection TEXT, model TEXT, runtime TEXT,"
                " response_text TEXT, failure_reason TEXT, failure_detail TEXT,"
                " events TEXT, started_at TEXT NOT NULL, finished_at TEXT)"
            )
            connection.execute(
                "INSERT INTO runs_v2 SELECT id, task_id, role, status, prompt_digest,"
                " persona_digest, catalog_digest, suite_digest, skill_selection, model,"
                " runtime, response_text, failure_reason, failure_detail, events,"
                " started_at, finished_at FROM runs"
            )
            connection.execute("DROP TABLE runs")
            connection.execute("ALTER TABLE runs_v2 RENAME TO runs")
            connection.execute(
                "UPDATE meta SET value = '2' WHERE key = 'schema_version'"
            )
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")
        tasks_now = connection.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        runs_now = connection.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        if (tasks_now, runs_now) != (previous_tasks, previous_runs):
            raise StoreError(
                "state-corrupt", "The schema migration did not preserve every recorded row."
            )

    # -- audit -------------------------------------------------------------
    def _audit(
        self, db: sqlite3.Connection, task_id: str | None, actor: str, event: str, detail: dict[str, Any]
    ) -> None:
        last = db.execute("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
        previous = last["hash"] if last else GENESIS
        at = now()
        body = _dump({"task_id": task_id, "at": at, "actor": actor, "event": event, "detail": detail})
        digest = hashlib.sha256((previous + body).encode("utf-8")).hexdigest()
        db.execute(
            "INSERT INTO audit_events (task_id, at, actor, event, detail, prev_hash, hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, at, actor, event, _dump(detail), previous, digest),
        )

    def audit(self, task_id: str | None, actor: str, event: str, detail: dict[str, Any]) -> None:
        with self._tx() as db:
            self._audit(db, task_id, actor, event, detail)

    def audit_events(self, task_id: str | None = None) -> list[dict[str, Any]]:
        db = self._connect()
        try:
            if task_id is None:
                rows = db.execute("SELECT * FROM audit_events ORDER BY seq").fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM audit_events WHERE task_id = ? ORDER BY seq", (task_id,)
                ).fetchall()
        finally:
            db.close()
        return [_row(row, "detail") for row in rows]  # type: ignore[misc]

    def verify_audit_chain(self) -> int:
        """Return the number of events, or raise when the chain is broken."""

        db = self._connect()
        try:
            rows = db.execute("SELECT * FROM audit_events ORDER BY seq").fetchall()
        finally:
            db.close()
        previous = GENESIS
        for row in rows:
            body = _dump(
                {
                    "task_id": row["task_id"],
                    "at": row["at"],
                    "actor": row["actor"],
                    "event": row["event"],
                    "detail": json.loads(row["detail"]),
                }
            )
            expected = hashlib.sha256((previous + body).encode("utf-8")).hexdigest()
            if row["prev_hash"] != previous or row["hash"] != expected:
                raise StoreError("audit-chain-broken", f"Audit event {row['seq']} does not verify.")
            previous = row["hash"]
        return len(rows)

    # -- project / tasks -----------------------------------------------------
    def project(self) -> dict[str, Any]:
        db = self._connect()
        try:
            return _row(db.execute("SELECT * FROM project").fetchone())  # type: ignore[return-value]
        finally:
            db.close()

    def create_task(
        self, *, title: str, requirements: str, acceptance_criteria: str,
        repo_ref: str, repo_commit: str, repo_dirty: bool, actor: str,
    ) -> dict[str, Any]:
        with self._tx() as db:
            seq = (db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM tasks").fetchone()[0])
            task_id = f"T-{seq:04d}"
            stamp = now()
            db.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, 'submitted', ?, ?, ?, ?, ?, ?)",
                (task_id, seq, title, requirements, acceptance_criteria, repo_ref,
                 repo_commit, int(repo_dirty), actor, stamp, stamp),
            )
            self._audit(db, task_id, actor, "task.submitted",
                        {"repo_commit": repo_commit, "repo_ref": repo_ref, "repo_dirty": repo_dirty})
        return self.task(task_id)  # type: ignore[return-value]

    def task(self, task_id: str) -> dict[str, Any] | None:
        db = self._connect()
        try:
            return _row(db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
        finally:
            db.close()

    def tasks(self) -> list[dict[str, Any]]:
        db = self._connect()
        try:
            return [dict(r) for r in db.execute("SELECT * FROM tasks ORDER BY seq").fetchall()]
        finally:
            db.close()

    def set_status(self, task_id: str, status: str, *, actor: str, expect: tuple[str, ...], event: str,
                   detail: dict[str, Any] | None = None) -> None:
        with self._tx() as db:
            self._set_status(db, task_id, status, actor, expect, event, detail or {})

    def _set_status(self, db: sqlite3.Connection, task_id: str, status: str, actor: str,
                    expect: tuple[str, ...], event: str, detail: dict[str, Any]) -> None:
        current = db.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if current is None:
            raise StoreError("task-not-found", f"No task {task_id}.")
        if current["status"] not in expect:
            raise StoreError(
                "invalid-transition",
                f"Task {task_id} is {current['status']}; expected one of {list(expect)}.",
            )
        db.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", (status, now(), task_id))
        self._audit(db, task_id, actor, event, {"from": current["status"], "to": status, **detail})

    # -- runs ----------------------------------------------------------------
    def begin_run(self, *, task_id: str, role: str, expect: tuple[str, ...], running_status: str,
                  actor: str, evidence: dict[str, Any]) -> str:
        """Atomically claim the stage: status check, run row, and audit together."""

        if role not in RUN_ROLES:
            raise StoreError("invalid-role", f"Unknown run role {role!r}.")
        run_id = str(uuid.uuid4())
        with self._tx() as db:
            self._set_status(db, task_id, running_status, actor, expect, f"{role}.started",
                             {"run_id": run_id})
            db.execute(
                "INSERT INTO runs (id, task_id, role, status, prompt_digest, persona_digest,"
                " catalog_digest, suite_digest, skill_selection, model, runtime, started_at)"
                " VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, task_id, role, evidence.get("prompt_digest"), evidence.get("persona_digest"),
                 evidence.get("catalog_digest"), evidence.get("suite_digest"),
                 _dump(evidence.get("skill_selection")), evidence.get("model"),
                 evidence.get("runtime"), now()),
            )
        return run_id

    def fail_run(self, *, run_id: str, task_id: str, failed_status: str, running_status: str,
                 actor: str, reason: str, detail: str, response_text: str | None,
                 events: dict[str, Any] | None) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE runs SET status='failed', failure_reason=?, failure_detail=?,"
                " response_text=?, events=?, finished_at=? WHERE id=?",
                (reason, detail, response_text, _dump(events or {}), now(), run_id),
            )
            self._set_status(db, task_id, failed_status, actor, (running_status,), "run.failed",
                             {"run_id": run_id, "reason": reason})

    def succeed_run(self, *, run_id: str, response_text: str, events: dict[str, Any]) -> None:
        """Mark a run successful without changing the task's stage status.

        The implementation stages keep the task in one stage status while several
        runs contribute evidence, so the caller owns the status transition.
        """

        with self._tx() as db:
            cursor = db.execute(
                "UPDATE runs SET status='succeeded', response_text=?, events=?,"
                " finished_at=? WHERE id=? AND status='running'",
                (response_text, _dump(events), now(), run_id),
            )
            if cursor.rowcount == 0:
                raise StoreError("run-not-running", f"Run {run_id} is not running.")

    def succeed_intake(self, *, run_id: str, task_id: str, response_text: str, events: dict[str, Any],
                       digest: str, document: dict[str, Any], needs_clarification: bool, actor: str) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE runs SET status='succeeded', response_text=?, events=?, finished_at=? WHERE id=?",
                (response_text, _dump(events), now(), run_id),
            )
            db.execute(
                "INSERT INTO intake_assessments VALUES (?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), task_id, run_id, digest, _dump(document), now()),
            )
            target = "needs_clarification" if needs_clarification else "intake_complete"
            self._set_status(db, task_id, target, actor, ("intake_running",), "intake.recorded",
                             {"run_id": run_id, "digest": digest})

    def succeed_architect(self, *, run_id: str, task_id: str, response_text: str, events: dict[str, Any],
                          digest: str, document: dict[str, Any], actor: str) -> str:
        proposal_id = str(uuid.uuid4())
        with self._tx() as db:
            db.execute(
                "UPDATE runs SET status='succeeded', response_text=?, events=?, finished_at=? WHERE id=?",
                (response_text, _dump(events), now(), run_id),
            )
            db.execute(
                "INSERT INTO proposals VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (proposal_id, task_id, run_id, digest, _dump(document), now()),
            )
            self._set_status(db, task_id, "awaiting_approval", actor, ("architect_running",),
                             "proposal.recorded", {"run_id": run_id, "proposal_id": proposal_id, "digest": digest})
        return proposal_id

    def recover_interrupted(self, actor: str) -> list[str]:
        """Mark runs left 'running' by a crashed process and reopen their stage.

        A stage that a live recovery is holding open is left completely alone.
        A recovery keeps the task in a running status across its sandbox
        validation, its QA run, and its Reviewer run, and the validation phase
        has no run row at all, so from the outside it looks exactly like a stage
        whose process died. The recovering process therefore holds an exclusive
        lease on the task for the whole recovery, and any task holding a lease
        is skipped here. The operating system releases the lease when the owning
        process exits, including when it is killed, so a recovery that really
        died is still repaired by the next call.

        Repairing a running status with no run row at all requires positive
        evidence that the stage was an authorised recovery claim. A normal
        implementation has the same shape between its stages — the Builder run
        has already succeeded while validation holds the stage, and QA has
        already succeeded while the Reviewer prompt is composed — so repairing
        on the shape alone would fail a live implementation whenever another
        process happened to run ``status``. Only a ``recoveries`` row for the
        task's current iteration identifies the runless stage as a claim.
        """

        running_status = {
            "intake": ("intake_failed", "intake_running"),
            "architect": ("architect_failed", "architect_running"),
            "builder": ("implementation_failed", "implementation_running"),
            "qa": ("qa_failed", "qa_running"),
            "reviewer": ("review_failed", "review_running"),
        }
        recovered: list[str] = []
        with self._tx() as db:
            in_play = {
                row["task_id"]
                for row in db.execute(
                    "SELECT DISTINCT task_id FROM runs WHERE status = 'running'"
                ).fetchall()
            }
            placeholders = ",".join("?" for _ in running_status.values())
            in_play |= {
                row["id"]
                for row in db.execute(
                    f"SELECT id FROM tasks WHERE status IN ({placeholders})",
                    tuple(running for _, running in running_status.values()),
                ).fetchall()
            }
            leased = {task_id for task_id in in_play if self._recovery_in_progress(task_id)}
            for run in db.execute("SELECT id, task_id, role FROM runs WHERE status='running'").fetchall():
                if run["task_id"] in leased:
                    continue
                db.execute("UPDATE runs SET status='interrupted', failure_reason='interrupted',"
                           " failure_detail='The process stopped before the run finished.',"
                           " finished_at=? WHERE id=?", (now(), run["id"]))
                failed, running = running_status.get(
                    run["role"], ("implementation_failed", "implementation_running")
                )
                task = db.execute(
                    "SELECT status FROM tasks WHERE id = ?", (run["task_id"],)
                ).fetchone()
                if task is None or task["status"] != running:
                    # The stage already moved on; only the run row needs repairing.
                    recovered.append(run["id"])
                    continue
                self._set_status(db, run["task_id"], failed, actor, (running,), "run.interrupted",
                                 {"run_id": run["id"]})
                if run["role"] == "builder":
                    self._close_interrupted_iteration(db, run["task_id"])
                if run["role"] == "qa":
                    self._close_interrupted_qa_iteration(db, run["task_id"])
                recovered.append(run["id"])
            # A stage can also be claimed before its run row exists: the claim and
            # the run are written together, but a recovery claim holds the stage
            # open across validation. A process that stops in that window leaves a
            # task in a running status with nothing running, which nothing else
            # would ever reopen.
            #
            # Only an authorised recovery claim can be repaired this way, so the
            # claim itself is required before anything is changed. A normal
            # implementation is runless in exactly the same way between its
            # stages — the Builder run has succeeded while validation runs, and QA
            # has succeeded while the Reviewer prompt is composed — but it has no
            # ``recoveries`` row for its current iteration. Matching on the shape
            # alone would fail a live implementation whose validation merely
            # overlapped another process's ``status``.
            #
            # A QA re-run is claimed by its own row in ``qa_recoveries`` and holds
            # the stage across a prompt composition that has no run row either, so
            # that claim is the positive evidence for the QA stage. The Builder
            # rule is unchanged: only a ``recoveries`` row identifies its runless
            # stage, and a QA claim never satisfies it.
            for failed, running in running_status.values():
                if running == "qa_running":
                    claim = (
                        " AND EXISTS ("
                        "  SELECT 1 FROM iterations i"
                        "  JOIN qa_recoveries q ON q.iteration_id = i.id"
                        "  WHERE i.task_id = t.id"
                        "  AND i.number = (SELECT MAX(number) FROM iterations"
                        "                  WHERE task_id = t.id))"
                    )
                else:
                    claim = (
                        " AND EXISTS ("
                        "  SELECT 1 FROM iterations i"
                        "  JOIN recoveries r ON r.iteration_id = i.id"
                        "  WHERE i.task_id = t.id"
                        "  AND i.number = (SELECT MAX(number) FROM iterations"
                        "                  WHERE task_id = t.id))"
                    )
                for task in db.execute(
                    "SELECT t.id FROM tasks t"
                    " WHERE t.status = ?"
                    " AND t.id NOT IN (SELECT task_id FROM runs WHERE status = 'running')"
                    + claim,
                    (running,),
                ).fetchall():
                    if task["id"] in leased:
                        continue
                    self._set_status(db, task["id"], failed, actor, (running,),
                                     "stage.interrupted", {"run_id": None})
                    if running == "implementation_running":
                        self._close_interrupted_iteration(db, task["id"])
                    elif running == "qa_running":
                        self._close_interrupted_qa_iteration(db, task["id"])
                    recovered.append(task["id"])
        return recovered

    @staticmethod
    def _close_interrupted_iteration(db: sqlite3.Connection, task_id: str) -> None:
        """Stop the Builder's open iteration where the interrupted run stopped.

        An iteration left ``building`` by a dead process describes a stage that is
        not running, and every recovery path requires a failed iteration, so
        leaving it open would make the preserved work inspectable but permanently
        unrecoverable.
        """

        db.execute(
            "UPDATE iterations SET status = 'failed', failure_reason = 'interrupted',"
            " failure_detail = 'The process stopped before the run finished.',"
            " updated_at = ? WHERE task_id = ? AND status = 'building'",
            (now(), task_id),
        )

    @staticmethod
    def _close_interrupted_qa_iteration(db: sqlite3.Connection, task_id: str) -> None:
        """Stop a QA stage left open by a dead process on the iteration that owns it.

        A live QA failure closes its iteration as it records the failure. A process
        that dies does not, so the iteration would stay ``qa_running`` for ever —
        and every QA recovery path requires a failed iteration, which would make the
        recorded failed run inspectable but never recoverable. The run and the
        audit chain that recorded it are untouched: only the stage is closed.
        """

        db.execute(
            "UPDATE iterations SET status = 'failed', failure_reason = 'interrupted',"
            " failure_detail = 'The process stopped before the run finished.',"
            " updated_at = ? WHERE task_id = ? AND status = 'qa_running'",
            (now(), task_id),
        )

    # -- recovery leases -------------------------------------------------------
    def _recovery_lease_path(self, task_id: str) -> Path:
        """Where the exclusive lease for one task's live recovery is kept."""

        return self.path.with_name(f"{self.path.name}.{task_id}.recovery.lock")

    def _recovery_in_progress(self, task_id: str) -> bool:
        """Whether another process currently holds the recovery lease.

        The check takes the lease and drops it again, so it observes the same
        kernel state a recovering process depends on. It never waits.
        """

        if fcntl is None:  # pragma: no cover - POSIX-only fallback
            return False
        path = self._recovery_lease_path(task_id)
        if not path.exists():
            return False
        try:
            handle = open(path, "a+b")
        except OSError:  # pragma: no cover - unreadable lock file is not a recovery
            return False
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        return False

    @contextmanager
    def recovery_lease(self, task_id: str) -> Iterator[None]:
        """Hold the exclusive lease for the whole of one task's implementation.

        The lease is a kernel lock, not a recorded fact: it is released the
        moment the holding process dies, so `recover_interrupted` never has to
        guess whether a running stage belongs to a live implementation or to a
        dead one. Holding it also prevents a separate `implement` process from
        consuming a restart while its authorizing process is opening the next
        iteration.
        """

        if fcntl is None:  # pragma: no cover - POSIX-only fallback
            raise StoreError(
                "recovery-unsupported",
                "Recoveries need POSIX file locks, which this platform does not provide.",
            )
        path = self._recovery_lease_path(task_id)
        try:
            handle = open(path, "a+b")
        except OSError as error:
            raise StoreError(
                "recovery-unsupported", f"The recovery lease could not be opened: {error}"
            ) from error
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise StoreError(
                    "recovery-active",
                    f"Another process is already recovering task {task_id}.",
                ) from error
            yield
        finally:
            handle.close()

    @contextmanager
    def implementation_lease(self, task_id: str) -> Iterator[None]:
        """Serialize ordinary implementation against restart and recovery work."""

        if fcntl is None:  # pragma: no cover - restart/recovery are unsupported here
            yield
            return
        with self.recovery_lease(task_id):
            yield

    def runs(self, task_id: str) -> list[dict[str, Any]]:
        db = self._connect()
        try:
            rows = db.execute("SELECT * FROM runs WHERE task_id = ? ORDER BY started_at", (task_id,)).fetchall()
        finally:
            db.close()
        return [_row(r, "skill_selection", "events") for r in rows]  # type: ignore[misc]

    def intake(self, task_id: str) -> dict[str, Any] | None:
        db = self._connect()
        try:
            return _row(db.execute(
                "SELECT * FROM intake_assessments WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                (task_id,)).fetchone(), "document")
        finally:
            db.close()

    def proposal(self, task_id: str) -> dict[str, Any] | None:
        db = self._connect()
        try:
            return _row(db.execute(
                "SELECT * FROM proposals WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                (task_id,)).fetchone(), "document")
        finally:
            db.close()

    def approval(self, task_id: str) -> dict[str, Any] | None:
        db = self._connect()
        try:
            return _row(db.execute(
                "SELECT * FROM approvals WHERE task_id = ? ORDER BY decided_at DESC LIMIT 1",
                (task_id,)).fetchone())
        finally:
            db.close()

    def decide(self, *, task_id: str, proposal_digest: str, decision: str, actor: str,
               reason: str | None, repo_commit: str | None) -> dict[str, Any]:
        """Record a human decision bound to the digest of the exact current proposal."""

        with self._tx() as db:
            task = db.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise StoreError("task-not-found", f"No task {task_id}.")
            proposal = db.execute(
                "SELECT id, digest, status FROM proposals WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                (task_id,)).fetchone()
            if task["status"] != "awaiting_approval" or proposal is None or proposal["status"] != "pending":
                raise StoreError("not-awaiting-approval",
                                 f"Task {task_id} is {task['status']}; there is no pending proposal.")
            if proposal["digest"] != proposal_digest:
                raise StoreError("digest-mismatch",
                                 "The supplied digest is not the digest of the current proposal.")
            db.execute("INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (str(uuid.uuid4()), task_id, proposal["id"], proposal["digest"], decision,
                        actor, reason, repo_commit, now()))
            db.execute("UPDATE proposals SET status = ? WHERE id = ?", (decision, proposal["id"]))
            self._set_status(db, task_id, decision, actor, ("awaiting_approval",), f"proposal.{decision}",
                             {"proposal_id": proposal["id"], "proposal_digest": proposal["digest"], "reason": reason})
        return self.approval(task_id)  # type: ignore[return-value]

    # -- implementation, validation, and review ------------------------------
    #: Columns ``update_iteration`` may write. A field outside this set is a
    #: programming error, not something to interpolate into SQL.
    ITERATION_FIELDS = (
        "status", "branch", "workspace_path", "commit_sha", "workspace_digest",
        "diff_excerpt", "builder_run_id", "build_report", "build_digest",
        "validation", "validation_exit_code", "qa_run_id", "qa_report", "qa_digest",
        "reviewer_run_id", "review_report", "review_digest", "failure_reason",
        "failure_detail",
    )

    #: Iteration columns stored as JSON.
    ITERATION_JSON_FIELDS = ("build_report", "validation", "qa_report", "review_report")

    def open_iteration(
        self, *, task_id: str, base_commit: str, actor: str
    ) -> dict[str, Any]:
        """Create the next iteration for a task and claim the builder stage."""

        with self._tx() as db:
            task = db.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise StoreError("task-not-found", f"No task {task_id}.")
            if task["status"] not in ("approved", "changes_required"):
                raise StoreError(
                    "not-approved",
                    f"Task {task_id} is {task['status']}; an approved task is required.",
                )
            number = db.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 FROM iterations WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            iteration_id = f"{task_id}-I{number}"
            stamp = now()
            db.execute(
                "INSERT INTO iterations (id, task_id, number, status, base_commit,"
                " created_at, updated_at) VALUES (?, ?, ?, 'building', ?, ?, ?)",
                (iteration_id, task_id, number, base_commit, stamp, stamp),
            )
            self._set_status(db, task_id, "implementation_running", actor,
                             ("approved", "changes_required"), "iteration.started",
                             {"iteration_id": iteration_id, "number": number,
                              "base_commit": base_commit})
        return self.iteration(task_id)  # type: ignore[return-value]

    def update_iteration(
        self, *, iteration_id: str, actor: str, event: str,
        fields: dict[str, Any], detail: dict[str, Any] | None = None,
    ) -> None:
        """Record one iteration's evidence atomically with its audit entry."""

        unknown = [name for name in fields if name not in self.ITERATION_FIELDS]
        if unknown:
            raise StoreError("invalid-field", f"Unknown iteration field(s): {unknown}.")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [
            _dump(value) if name in self.ITERATION_JSON_FIELDS and value is not None else value
            for name, value in fields.items()
        ]
        with self._tx() as db:
            cursor = db.execute(
                f"UPDATE iterations SET {assignments}, updated_at = ? WHERE id = ?",
                (*values, now(), iteration_id),
            )
            if cursor.rowcount == 0:
                raise StoreError("iteration-not-found", f"No iteration {iteration_id}.")
            row = db.execute(
                "SELECT task_id FROM iterations WHERE id = ?", (iteration_id,)
            ).fetchone()
            self._audit(db, row["task_id"], actor, event,
                        {"iteration_id": iteration_id, **(detail or {}),
                         "fields": sorted(fields)})

    def review_retries(self, task_id: str, iteration_id: str) -> int:
        """How many Reviewer-only retries were authorised for one iteration."""

        return sum(
            1
            for event in self.audit_events(task_id)
            if event["event"] == REVIEW_RETRY_EVENT
            and event["detail"].get("iteration_id") == iteration_id
        )

    def authorize_review_retry(
        self, *, task_id: str, iteration_id: str, actor: str, limit: int,
        detail: dict[str, Any],
    ) -> int:
        """Consume one Reviewer retry for a report-format failure, atomically.

        The status check, the limit check, the append-only audit event, and the
        iteration reopening commit together. The failed run row is never touched;
        the retry's own run is appended afterwards by the normal stage claim.
        Returns the number of retries now used.
        """

        with self._tx() as db:
            task = db.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise StoreError("task-not-found", f"No task {task_id}.")
            if task["status"] != "review_failed":
                raise StoreError(
                    "invalid-transition",
                    f"Task {task_id} is {task['status']}; expected review_failed.",
                )
            used = sum(
                1
                for row in db.execute(
                    "SELECT detail FROM audit_events WHERE task_id = ? AND event = ?",
                    (task_id, REVIEW_RETRY_EVENT),
                ).fetchall()
                if json.loads(row["detail"]).get("iteration_id") == iteration_id
            )
            if used >= limit:
                raise StoreError(
                    "review-retry-exhausted",
                    f"Iteration {iteration_id} already used its {limit} Reviewer retry.",
                )
            cursor = db.execute(
                "UPDATE iterations SET status = 'review_running', failure_reason = NULL,"
                " failure_detail = NULL, updated_at = ? WHERE id = ? AND task_id = ?"
                " AND status = 'review_failed'",
                (now(), iteration_id, task_id),
            )
            if cursor.rowcount == 0:
                raise StoreError(
                    "invalid-transition", f"Iteration {iteration_id} is not review_failed."
                )
            self._audit(
                db, task_id, actor, REVIEW_RETRY_EVENT,
                {"iteration_id": iteration_id, "attempt": used + 1, "limit": limit, **detail},
            )
        return used + 1

    def recovery(self, task_id: str) -> dict[str, Any] | None:
        """The most recent authorisation of a failed Builder's change set."""

        db = self._connect()
        try:
            return _row(db.execute(
                "SELECT * FROM recoveries WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                (task_id,)).fetchone(), "changed_files")
        finally:
            db.close()

    def recovery_for_iteration(self, iteration_id: str) -> dict[str, Any] | None:
        db = self._connect()
        try:
            return _row(db.execute(
                "SELECT * FROM recoveries WHERE iteration_id = ?", (iteration_id,)
            ).fetchone(), "changed_files")
        finally:
            db.close()

    def recoveries(self, task_id: str) -> list[dict[str, Any]]:
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT * FROM recoveries WHERE task_id = ? ORDER BY created_at", (task_id,)
            ).fetchall()
        finally:
            db.close()
        return [_row(row, "changed_files") for row in rows]  # type: ignore[misc]

    def authorize_builder_recovery(
        self, *, task_id: str, iteration_id: str, actor: str, recovery_digest: str,
        snapshot_digest: str, base_commit: str, head_commit: str,
        changed_files: list[dict[str, str]], contract_digest: str,
        failure: dict[str, Any], builder_run_id: str | None, limit: int,
    ) -> dict[str, Any]:
        """Claim the one authorised recovery of a failed Builder, atomically.

        The task status check, the per-iteration limit, the immutable recovery
        row, the audit event, and the reopening of the iteration commit together,
        so two concurrent recoveries cannot both proceed. The failed Builder's
        run row, its failure reason, and the iteration's own ``failure_reason``
        and ``failure_detail`` are never modified: the original failure stays
        visible next to the recovery that followed it.
        """

        recovery_id = str(uuid.uuid4())
        with self._tx() as db:
            task = db.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise StoreError("task-not-found", f"No task {task_id}.")
            if task["status"] != "implementation_failed":
                raise StoreError(
                    "invalid-transition",
                    f"Task {task_id} is {task['status']}; expected implementation_failed.",
                )
            used = db.execute(
                "SELECT COUNT(*) AS n FROM recoveries WHERE iteration_id = ?", (iteration_id,)
            ).fetchone()["n"]
            if used >= limit:
                raise StoreError(
                    "recovery-exhausted",
                    f"Iteration {iteration_id} already used its {limit} authorised recovery.",
                )
            cursor = db.execute(
                "UPDATE tasks SET status = 'implementation_running', updated_at = ?"
                " WHERE id = ? AND status = 'implementation_failed'",
                (now(), task_id),
            )
            if cursor.rowcount == 0:
                raise StoreError(
                    "invalid-transition", f"Task {task_id} is no longer implementation_failed."
                )
            placeholders = ",".join("?" for _ in RECOVERABLE_ITERATION_STATUSES)
            cursor = db.execute(
                "UPDATE iterations SET status = 'building', updated_at = ?"
                f" WHERE id = ? AND task_id = ? AND status IN ({placeholders})",
                (now(), iteration_id, task_id, *RECOVERABLE_ITERATION_STATUSES),
            )
            if cursor.rowcount == 0:
                raise StoreError(
                    "invalid-transition",
                    f"Iteration {iteration_id} is not in a recoverable state.",
                )
            db.execute(
                "INSERT INTO recoveries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    recovery_id, task_id, iteration_id, builder_run_id,
                    str(failure.get("reason") or "unknown"),
                    failure.get("detail"), base_commit, head_commit,
                    snapshot_digest, recovery_digest, _dump(changed_files),
                    contract_digest, actor, now(),
                ),
            )
            self._audit(
                db, task_id, actor, RECOVERY_EVENT,
                {
                    "recovery_id": recovery_id,
                    "iteration_id": iteration_id,
                    "builder_run_id": builder_run_id,
                    "failure_reason": failure.get("reason"),
                    "recovery_digest": recovery_digest,
                    "snapshot_digest": snapshot_digest,
                    "contract_digest": contract_digest,
                    "base_commit": base_commit,
                    "head_commit": head_commit,
                    "changed_files": [entry["path"] for entry in changed_files],
                    "limit": limit,
                    "attempt": used + 1,
                },
            )
        return self.recovery_for_iteration(iteration_id)  # type: ignore[return-value]

    def qa_recoveries(self, task_id: str) -> list[dict[str, Any]]:
        """Every recorded authorization to re-run the QA stage for one task."""

        db = self._connect()
        try:
            rows = db.execute(
                "SELECT * FROM qa_recoveries WHERE task_id = ? ORDER BY created_at",
                (task_id,),
            ).fetchall()
        finally:
            db.close()
        return [dict(row) for row in rows]

    def qa_recovery_for_iteration(self, iteration_id: str) -> dict[str, Any] | None:
        db = self._connect()
        try:
            return _row(db.execute(
                "SELECT * FROM qa_recoveries WHERE iteration_id = ?", (iteration_id,)
            ).fetchone())
        finally:
            db.close()

    def authorize_qa_recovery(
        self, *, task_id: str, iteration_id: str, failed_run_id: str, actor: str,
        reason: str | None, base_commit: str, head_commit: str, workspace_digest: str,
        builder_run_id: str | None, build_digest: str, proposal_id: str,
        proposal_digest: str, approval_id: str, contract_digest: str,
        validation_output_digest: str, sandbox_image_id: str, limit: int,
    ) -> dict[str, Any]:
        """Claim the one authorised re-run of a failed QA stage, atomically.

        Every condition a QA re-run depends on is re-checked here, inside the write
        transaction, rather than trusted from the caller: the task status, the
        one-per-iteration limit, the failed QA run and its reason, the absence of
        any QA verdict, the exact change set and validation run that are being
        re-examined, and the approval that binds the proposal and contract. Two
        concurrent invocations therefore cannot both authorise, and a recorded row
        cannot be replayed.

        The task moves to ``qa_running`` and the iteration with it, so the
        authorization is the only thing that permits the next QA invocation and a
        process that stops between the claim and the call leaves a state a later
        repair pass can close. The failed run, its failure reason, and every audit
        event already recorded are never modified.
        """

        recovery_id = str(uuid.uuid4())
        with self._tx() as db:
            task = db.execute(
                "SELECT status, repo_commit FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise StoreError("task-not-found", f"No task {task_id}.")
            if limit != MAX_QA_RECOVERIES:
                raise StoreError(
                    "qa-recovery-exhausted",
                    f"The authorised QA re-run limit is fixed at {MAX_QA_RECOVERIES}.",
                )
            # The authorisation count is checked before the status so that a second
            # concurrent attempt reports exhaustion, which is what happened, rather
            # than the transition this attempt never reached.
            used = db.execute(
                "SELECT COUNT(*) AS n FROM qa_recoveries WHERE task_id = ?", (task_id,)
            ).fetchone()["n"]
            if used >= limit:
                raise StoreError(
                    "qa-recovery-exhausted",
                    f"Task {task_id} already used its {limit} authorised QA retry.",
                )
            if task["status"] != "qa_failed":
                raise StoreError(
                    "invalid-transition",
                    f"Task {task_id} is {task['status']}; expected qa_failed.",
                )
            iteration = db.execute(
                "SELECT number, status, base_commit, commit_sha, workspace_digest,"
                " builder_run_id, build_report, build_digest,"
                " qa_run_id, qa_report, qa_digest, created_at"
                " FROM iterations WHERE id = ? AND task_id = ?",
                (iteration_id, task_id),
            ).fetchone()
            if iteration is None:
                raise StoreError(
                    "iteration-not-found", f"No iteration {iteration_id} for {task_id}."
                )
            latest_iteration = db.execute(
                "SELECT id FROM iterations WHERE task_id = ? ORDER BY number DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if latest_iteration is None or latest_iteration["id"] != iteration_id:
                raise StoreError(
                    "qa-recovery-refused",
                    "The supplied iteration is not the task's latest iteration.",
                )
            if iteration["status"] != "failed":
                raise StoreError(
                    "invalid-transition",
                    f"Iteration {iteration_id} is {iteration['status']}; expected failed.",
                )
            if (
                iteration["qa_report"] is not None
                or iteration["qa_digest"] is not None
                or iteration["qa_run_id"] is not None
            ):
                raise StoreError(
                    "qa-recovery-refused",
                    "The iteration recorded a QA verdict, so there is nothing to re-run.",
                )
            # A QA re-run examines the exact change set and validation run already on
            # record. Nothing is re-executed, so a single byte of drift in either
            # would make the retry a decision about evidence no human authorised.
            if (
                iteration["commit_sha"] != head_commit
                or iteration["workspace_digest"] != workspace_digest
                or iteration["base_commit"] != base_commit
            ):
                raise StoreError(
                    "qa-recovery-refused",
                    "The recorded change set does not match the one being re-examined.",
                )
            if iteration["build_report"] is None or iteration["build_digest"] != build_digest:
                raise StoreError(
                    "qa-recovery-refused",
                    "The iteration does not record the Builder report the retry relies on.",
                )
            if (
                builder_run_id is not None
                and iteration["builder_run_id"] is not None
                and iteration["builder_run_id"] != builder_run_id
            ):
                raise StoreError(
                    "qa-recovery-refused",
                    "The supplied Builder run is not the one recorded on the iteration.",
                )
            if iteration["base_commit"] != task["repo_commit"] or base_commit != task["repo_commit"]:
                raise StoreError(
                    "qa-recovery-refused",
                    "The iteration base commit is not the approved repository commit.",
                )
            placeholders = ",".join("?" for _ in INTERRUPTED_RUN_STATUSES)
            run = db.execute(
                "SELECT id, role, status, failure_reason, failure_detail, started_at FROM runs"
                f" WHERE id = ? AND task_id = ? AND status IN ({placeholders})",
                (failed_run_id, task_id, *INTERRUPTED_RUN_STATUSES),
            ).fetchone()
            if run is None or run["role"] != "qa":
                raise StoreError(
                    "qa-recovery-refused", "The supplied id is not a failed QA run."
                )
            if run["failure_reason"] not in QA_RECOVERABLE_FAILURES:
                raise StoreError(
                    "qa-recovery-refused",
                    "The failed QA run did not stop for a recoverable execution failure.",
                )
            latest_qa_run = db.execute(
                "SELECT id FROM runs WHERE task_id = ? AND role = 'qa'"
                " ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if (
                run["started_at"] < iteration["created_at"]
                or latest_qa_run is None
                or latest_qa_run["id"] != failed_run_id
            ):
                raise StoreError(
                    "qa-recovery-refused",
                    "The supplied QA run is not the failed run for this iteration.",
                )
            if db.execute(
                "SELECT 1 FROM deliverables WHERE task_id = ? LIMIT 1", (task_id,)
            ).fetchone() is not None or db.execute(
                "SELECT 1 FROM acceptances WHERE task_id = ? LIMIT 1", (task_id,)
            ).fetchone() is not None:
                raise StoreError(
                    "qa-recovery-refused", "A deliverable or delivery decision already exists."
                )
            proposal = db.execute(
                "SELECT id, digest, status, document FROM proposals"
                " WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            approval = db.execute(
                "SELECT id, proposal_id, proposal_digest, decision, repo_commit FROM approvals"
                " WHERE task_id = ? ORDER BY decided_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if (
                proposal is None
                or approval is None
                or proposal["id"] != proposal_id
                or proposal["digest"] != proposal_digest
                or proposal["status"] != "approved"
                or hashlib.sha256(proposal["document"].encode("utf-8")).hexdigest()
                != proposal["digest"]
                or approval["id"] != approval_id
                or approval["decision"] != "approved"
                or approval["proposal_id"] != proposal["id"]
                or approval["proposal_digest"] != proposal["digest"]
            ):
                raise StoreError(
                    "qa-recovery-refused", "The original approval no longer matches the proposal."
                )
            if approval["repo_commit"] not in (None, task["repo_commit"]):
                raise StoreError(
                    "qa-recovery-refused",
                    "The approved repository commit is not the one the task recorded.",
                )
            db.execute(
                "INSERT INTO qa_recoveries VALUES"
                " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    recovery_id, task_id, iteration_id, failed_run_id, used + 1,
                    str(run["failure_reason"] or "unknown"),
                    (str(run["failure_detail"] or "") or None), base_commit,
                    head_commit, workspace_digest, builder_run_id, build_digest,
                    proposal["id"], proposal["digest"], approval["id"], contract_digest,
                    validation_output_digest, sandbox_image_id, actor, reason, now(),
                ),
            )
            # The status transition is audited with the authorization, so the one
            # event that permits a second QA invocation carries the exact record it
            # authorized.
            self._set_status(
                db, task_id, "qa_running", actor, ("qa_failed",), QA_RECOVERY_EVENT,
                {
                    "recovery_id": recovery_id,
                    "iteration_id": iteration_id,
                    "failed_run_id": failed_run_id,
                    "failure_reason": run["failure_reason"],
                    "workspace_digest": workspace_digest,
                    "build_digest": build_digest,
                    "contract_digest": contract_digest,
                    "validation_output_digest": validation_output_digest,
                    "sandbox_image_id": sandbox_image_id,
                    "limit": limit,
                    "attempt": used + 1,
                },
            )
            # The iteration follows the task, so the state left behind if this
            # process stops before QA is invoked is one the repair pass can close
            # rather than a `qa_failed` task pointing at a `qa_running` stage.
            db.execute(
                "UPDATE iterations SET status = 'qa_running', updated_at = ?"
                " WHERE id = ? AND status = 'failed'",
                (now(), iteration_id),
            )
        return self.qa_recovery_for_iteration(iteration_id)  # type: ignore[return-value]

    def builder_restarts(self, task_id: str) -> list[dict[str, Any]]:
        """Every recorded authorization to restart this task's Builder."""

        db = self._connect()
        try:
            rows = db.execute(
                "SELECT * FROM builder_restarts WHERE task_id = ? ORDER BY created_at",
                (task_id,),
            ).fetchall()
        finally:
            db.close()
        return [dict(row) for row in rows]

    def builder_restart_for_iteration(self, iteration_id: str) -> dict[str, Any] | None:
        db = self._connect()
        try:
            return _row(db.execute(
                "SELECT * FROM builder_restarts WHERE failed_iteration_id = ?", (iteration_id,)
            ).fetchone())
        finally:
            db.close()

    def authorize_builder_restart(
        self, *, task_id: str, failed_iteration_id: str, failed_run_id: str, actor: str,
        reason: str | None, base_commit: str, proposal_id: str,
        proposal_digest: str, approval_id: str, contract_digest: str,
        empty_snapshot_digest: str, limit: int,
    ) -> dict[str, Any]:
        """Claim the one authorized restart of a Builder that wrote nothing, atomically.

        Every condition a restart depends on is re-checked here, inside the write
        transaction, rather than trusted from the caller: the task status, the
        single-authorization limit, the failed iteration and run, the absence of a
        Builder report, deliverable, or acceptance, and the approval that binds the
        proposal and contract. Two concurrent invocations therefore cannot both
        authorize, and a row that already exists cannot be replayed.

        The task moves back to ``approved`` so the normal implementation loop opens
        the next iteration; the authorization itself is the only thing that permits
        it. The failed run, the failed iteration, and every audit event already
        recorded are never modified.
        """

        restart_id = str(uuid.uuid4())
        with self._tx() as db:
            task = db.execute(
                "SELECT status, repo_commit FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise StoreError("task-not-found", f"No task {task_id}.")
            if limit != MAX_BUILDER_RESTARTS:
                raise StoreError(
                    "restart-exhausted",
                    f"The authorized Builder restart limit is fixed at "
                    f"{MAX_BUILDER_RESTARTS}.",
                )
            # The authorization count is checked before the status so that a second
            # concurrent attempt reports exhaustion, which is what happened, rather
            # than the transition this attempt never reached.
            used = db.execute(
                "SELECT COUNT(*) AS n FROM builder_restarts WHERE task_id = ?", (task_id,)
            ).fetchone()["n"]
            if used >= limit:
                raise StoreError(
                    "restart-exhausted",
                    f"Task {task_id} already used its {limit} authorized Builder restart.",
                )
            if task["status"] != "implementation_failed":
                raise StoreError(
                    "invalid-transition",
                    f"Task {task_id} is {task['status']}; expected implementation_failed.",
                )
            iteration = db.execute(
                "SELECT number, status, base_commit, commit_sha, build_report, build_digest,"
                " builder_run_id, created_at"
                " FROM iterations WHERE id = ? AND task_id = ?",
                (failed_iteration_id, task_id),
            ).fetchone()
            if iteration is None:
                raise StoreError(
                    "iteration-not-found", f"No iteration {failed_iteration_id} for {task_id}."
                )
            latest_iteration = db.execute(
                "SELECT id FROM iterations WHERE task_id = ? ORDER BY number DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if latest_iteration is None or latest_iteration["id"] != failed_iteration_id:
                raise StoreError(
                    "restart-refused",
                    "The supplied failed iteration is not the task's latest iteration.",
                )
            if iteration["status"] != "failed":
                raise StoreError(
                    "invalid-transition",
                    f"Iteration {failed_iteration_id} is {iteration['status']}; expected failed.",
                )
            if iteration["commit_sha"] is not None:
                raise StoreError(
                    "restart-refused",
                    "The failed iteration recorded a commit, so preserved work exists.",
                )
            if iteration["build_report"] is not None or iteration["build_digest"] is not None:
                raise StoreError(
                    "restart-refused", "The failed iteration recorded a Builder report."
                )
            if iteration["base_commit"] != task["repo_commit"] or base_commit != task["repo_commit"]:
                raise StoreError(
                    "restart-refused",
                    "The iteration base commit is not the approved repository commit.",
                )
            placeholders = ",".join("?" for _ in INTERRUPTED_RUN_STATUSES)
            run = db.execute(
                "SELECT id, role, status, failure_reason, started_at FROM runs"
                f" WHERE id = ? AND task_id = ? AND status IN ({placeholders})",
                (failed_run_id, task_id, *INTERRUPTED_RUN_STATUSES),
            ).fetchone()
            if run is None or run["role"] != "builder":
                raise StoreError(
                    "restart-refused", "The supplied id is not a failed Builder run."
                )
            if run["failure_reason"] not in RESTARTABLE_FAILURES:
                raise StoreError(
                    "restart-refused",
                    "The failed Builder run did not stop for a restartable interruption.",
                )
            if (
                iteration["builder_run_id"] is not None
                and iteration["builder_run_id"] != failed_run_id
            ):
                raise StoreError(
                    "restart-refused",
                    "The supplied Builder run is not recorded on the failed iteration.",
                )
            latest_builder_run = db.execute(
                "SELECT id FROM runs WHERE task_id = ? AND role = 'builder'"
                " ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if (
                run["started_at"] < iteration["created_at"]
                or latest_builder_run is None
                or latest_builder_run["id"] != failed_run_id
            ):
                raise StoreError(
                    "restart-refused",
                    "The supplied Builder run is not the failed run for this iteration.",
                )
            if db.execute(
                "SELECT 1 FROM deliverables WHERE task_id = ? LIMIT 1", (task_id,)
            ).fetchone() is not None or db.execute(
                "SELECT 1 FROM acceptances WHERE task_id = ? LIMIT 1", (task_id,)
            ).fetchone() is not None:
                raise StoreError(
                    "restart-refused", "A deliverable or delivery decision already exists."
                )
            proposal = db.execute(
                "SELECT id, digest, status, document FROM proposals"
                " WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            approval = db.execute(
                "SELECT id, proposal_id, proposal_digest, decision, repo_commit FROM approvals"
                " WHERE task_id = ? ORDER BY decided_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if (
                proposal is None
                or approval is None
                or proposal["id"] != proposal_id
                or proposal["digest"] != proposal_digest
                or proposal["status"] != "approved"
                or hashlib.sha256(proposal["document"].encode("utf-8")).hexdigest()
                != proposal["digest"]
                or approval["id"] != approval_id
                or approval["decision"] != "approved"
                or approval["proposal_id"] != proposal["id"]
                or approval["proposal_digest"] != proposal["digest"]
            ):
                raise StoreError(
                    "restart-refused", "The original approval no longer matches the proposal."
                )
            if approval["repo_commit"] not in (None, task["repo_commit"]):
                raise StoreError(
                    "restart-refused",
                    "The approved repository commit is not the one the task recorded.",
                )
            db.execute(
                "INSERT INTO builder_restarts VALUES"
                " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    restart_id, task_id, failed_iteration_id, failed_run_id,
                    used + 1, str(run["failure_reason"] or "unknown"), base_commit,
                    proposal["id"], proposal["digest"], approval["id"], contract_digest,
                    empty_snapshot_digest, actor, reason, now(),
                ),
            )
            # The status transition is audited with the authorization, so the one
            # event that permits a second Builder run carries the exact record it
            # authorized. `approved` is the only status the loop opens an
            # iteration from other than `changes_required`, which this is not.
            self._set_status(
                db, task_id, "approved", actor, ("implementation_failed",),
                BUILDER_RESTART_EVENT,
                {
                    "restart_id": restart_id,
                    "failed_iteration_id": failed_iteration_id,
                    "failed_run_id": failed_run_id,
                    "failure_reason": str(run["failure_reason"] or "unknown"),
                    "restart_number": used + 1,
                    "base_commit": base_commit,
                    "proposal_id": proposal["id"],
                    "proposal_digest": proposal["digest"],
                    "approval_id": approval["id"],
                    "contract_digest": contract_digest,
                    "empty_snapshot_digest": empty_snapshot_digest,
                    "reason_note": reason,
                    "limit": limit,
                    "attempt": used + 1,
                },
            )
        return self.builder_restart_for_iteration(failed_iteration_id)  # type: ignore[return-value]

    def iteration(self, task_id: str) -> dict[str, Any] | None:
        db = self._connect()
        try:
            return _row(db.execute(
                "SELECT * FROM iterations WHERE task_id = ? ORDER BY number DESC LIMIT 1",
                (task_id,)).fetchone(), *self.ITERATION_JSON_FIELDS)
        finally:
            db.close()

    def iterations(self, task_id: str) -> list[dict[str, Any]]:
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT * FROM iterations WHERE task_id = ? ORDER BY number", (task_id,)
            ).fetchall()
        finally:
            db.close()
        return [_row(row, *self.ITERATION_JSON_FIELDS) for row in rows]  # type: ignore[misc]

    def create_deliverable(
        self, *, task_id: str, iteration_id: str, digest: str,
        document: dict[str, Any], actor: str,
    ) -> dict[str, Any]:
        """Freeze the reviewed change set as the deliverable a human decides on."""

        deliverable_id = str(uuid.uuid4())
        with self._tx() as db:
            self._set_status(db, task_id, "awaiting_acceptance", actor,
                             ("review_running",), "deliverable.created",
                             {"iteration_id": iteration_id, "deliverable_digest": digest})
            db.execute(
                "INSERT INTO deliverables VALUES (?, ?, ?, ?, ?, ?)",
                (deliverable_id, task_id, iteration_id, digest, _dump(document), now()),
            )
        return self.deliverable(task_id)  # type: ignore[return-value]

    def deliverable(self, task_id: str) -> dict[str, Any] | None:
        db = self._connect()
        try:
            return _row(db.execute(
                "SELECT * FROM deliverables WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                (task_id,)).fetchone(), "document")
        finally:
            db.close()

    def acceptance(self, task_id: str) -> dict[str, Any] | None:
        db = self._connect()
        try:
            return _row(db.execute(
                "SELECT * FROM acceptances WHERE task_id = ? ORDER BY decided_at DESC LIMIT 1",
                (task_id,)).fetchone())
        finally:
            db.close()

    def decide_delivery(
        self, *, task_id: str, deliverable_digest: str, decision: str, actor: str,
        reason: str | None,
    ) -> dict[str, Any]:
        """Record a human acceptance bound to the digest of the frozen deliverable."""

        if decision not in ("accepted", "rejected"):
            raise StoreError("invalid-decision", f"Unknown delivery decision {decision!r}.")
        with self._tx() as db:
            task = db.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise StoreError("task-not-found", f"No task {task_id}.")
            deliverable = db.execute(
                "SELECT id, digest FROM deliverables WHERE task_id = ?"
                " ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
            if task["status"] != "awaiting_acceptance" or deliverable is None:
                raise StoreError(
                    "not-awaiting-acceptance",
                    f"Task {task_id} is {task['status']}; there is no deliverable to decide.",
                )
            if deliverable["digest"] != deliverable_digest:
                raise StoreError(
                    "digest-mismatch",
                    "The supplied digest is not the digest of the current deliverable.",
                )
            db.execute(
                "INSERT INTO acceptances VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), task_id, deliverable["id"], deliverable["digest"],
                 decision, actor, reason, now()),
            )
            target = "delivered" if decision == "accepted" else "delivery_rejected"
            self._set_status(db, task_id, target, actor, ("awaiting_acceptance",),
                             f"delivery.{decision}",
                             {"deliverable_id": deliverable["id"],
                              "deliverable_digest": deliverable["digest"], "reason": reason})
        return self.acceptance(task_id)  # type: ignore[return-value]
