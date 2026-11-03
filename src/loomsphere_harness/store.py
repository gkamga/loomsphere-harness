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

SCHEMA_VERSION = 1
GENESIS = "0" * 64


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
        """Fail closed on a missing, foreign, or newer database."""

        if not self.exists():
            raise StoreError("not-initialized", "No state found; run `init` first.")
        connection = self._connect()
        try:
            if not self._has_schema(connection):
                raise StoreError("state-corrupt", "The state database has no schema.")
            version = connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if version is None or version["value"] != str(SCHEMA_VERSION):
                raise StoreError("state-version", "The state database version is unsupported.")
        except sqlite3.DatabaseError as error:
            raise StoreError("state-corrupt", f"The state database is unreadable: {error}") from error
        finally:
            connection.close()

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
        """Mark runs left 'running' by a crashed process and reopen their stage."""

        recovered: list[str] = []
        with self._tx() as db:
            for run in db.execute("SELECT id, task_id, role FROM runs WHERE status='running'").fetchall():
                db.execute("UPDATE runs SET status='interrupted', failure_reason='interrupted',"
                           " failure_detail='The process stopped before the run finished.',"
                           " finished_at=? WHERE id=?", (now(), run["id"]))
                failed = "intake_failed" if run["role"] == "intake" else "architect_failed"
                running = "intake_running" if run["role"] == "intake" else "architect_running"
                self._set_status(db, run["task_id"], failed, actor, (running,), "run.interrupted",
                                 {"run_id": run["id"]})
                recovered.append(run["id"])
        return recovered

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
