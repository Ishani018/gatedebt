"""SQLite persistence (stdlib ``sqlite3``, no ORM).

SQLite is the single authority for registry state. Evidence, approvals,
retirement verifications, decision snapshots and audit events are append-only:
enforced by triggers in the database itself, not just by this module.

Schema changes are versioned migrations tracked with ``PRAGMA user_version``.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models import (
    ApprovalRecord,
    AuditEvent,
    DecisionReport,
    EvidenceRecord,
    ExceptionRecord,
    ExceptionStatus,
    RetirementVerification,
    utcnow,
)


class DuplicateError(Exception):
    pass


class StaleWriteError(Exception):
    """The row changed between read and write (lost-update protection)."""


def iso(value: datetime) -> str:
    """Fixed-width UTC timestamp so text ordering equals time ordering."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_id(prefix: str) -> str:
    return f"{prefix}-{utcnow():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"


def _append_only(table: str) -> str:
    return f"""
CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
"""


# Append new migrations to the end; never edit one that has shipped.
MIGRATIONS: list[str] = [
    # 1: initial schema
    """
CREATE TABLE exceptions (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    owner TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE INDEX exceptions_status ON exceptions(status);
CREATE TRIGGER exceptions_no_delete BEFORE DELETE ON exceptions
BEGIN SELECT RAISE(ABORT, 'exceptions are never deleted; retire them'); END;

CREATE TABLE evidence (
    id TEXT PRIMARY KEY,
    exception_id TEXT NOT NULL REFERENCES exceptions(id),
    scenario_id TEXT NOT NULL,
    source TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    commit_origin TEXT NOT NULL,
    working_tree_dirty INTEGER,
    recorded_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE INDEX evidence_exception ON evidence(exception_id, finished_at);

CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    exception_id TEXT NOT NULL REFERENCES exceptions(id),
    kind TEXT NOT NULL,
    decision TEXT NOT NULL,
    approver TEXT NOT NULL,
    auth_method TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE INDEX approvals_exception ON approvals(exception_id, decided_at);

CREATE TABLE decisions (
    id TEXT PRIMARY KEY,
    exception_id TEXT NOT NULL REFERENCES exceptions(id),
    purpose TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    report_json TEXT NOT NULL
);
CREATE INDEX decisions_exception ON decisions(exception_id, created_at);

CREATE TABLE verifications (
    id TEXT PRIMARY KEY,
    exception_id TEXT NOT NULL REFERENCES exceptions(id),
    observation_source TEXT NOT NULL,
    issues_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    record_json TEXT NOT NULL
);

CREATE TABLE audit_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    exception_id TEXT,
    details_json TEXT NOT NULL
);
CREATE INDEX audit_exception ON audit_events(exception_id, seq);
"""
    + "".join(_append_only(t) for t in ("evidence", "approvals", "decisions", "verifications", "audit_events")),
    # 2: server-side GitLab CI provenance for ingested evidence
    """
CREATE TABLE ci_verifications (
    evidence_id TEXT PRIMARY KEY REFERENCES evidence(id),
    exception_id TEXT NOT NULL REFERENCES exceptions(id),
    project_id INTEGER NOT NULL,
    pipeline_id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    job_name TEXT NOT NULL,
    ref TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    pipeline_web_url TEXT,
    job_web_url TEXT,
    verified_at TEXT NOT NULL,
    verified_by TEXT NOT NULL,
    UNIQUE (project_id, job_id)
);
"""
    + _append_only("ci_verifications"),
]
SCHEMA_VERSION = len(MIGRATIONS)


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._migrate()

    # ------------------------------------------------------------ connections

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One write transaction. ``BEGIN IMMEDIATE`` takes the write lock up
        front, so read-check-write sequences (state transitions) are serialised."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _migrate(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            if current > SCHEMA_VERSION:
                raise RuntimeError(f"database schema v{current} is newer than this code (v{SCHEMA_VERSION})")
            for version in range(current + 1, SCHEMA_VERSION + 1):
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _split(MIGRATIONS[version - 1]):
                        conn.execute(statement)
                    conn.execute(f"PRAGMA user_version = {version}")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
        finally:
            conn.close()

    def schema_version(self) -> int:
        with self.reader() as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]

    # ------------------------------------------------------------- exceptions

    def insert_exception(self, conn: sqlite3.Connection, exc: ExceptionRecord) -> None:
        try:
            conn.execute(
                "INSERT INTO exceptions (id, project, type, status, owner, expires_at, created_at, updated_at, record_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (exc.id, exc.project, exc.type.value, exc.status.value, exc.owner, iso(exc.expires_at),
                 iso(exc.created_at), iso(exc.updated_at), exc.model_dump_json()),
            )
        except sqlite3.IntegrityError as err:
            raise DuplicateError(f"exception {exc.id} already exists") from err

    def update_exception(
        self, conn: sqlite3.Connection, exc: ExceptionRecord, expected_status: ExceptionStatus, expected_updated_at: datetime
    ) -> None:
        cursor = conn.execute(
            "UPDATE exceptions SET status = ?, owner = ?, expires_at = ?, updated_at = ?, record_json = ?"
            " WHERE id = ? AND status = ? AND updated_at = ?",
            (exc.status.value, exc.owner, iso(exc.expires_at), iso(exc.updated_at), exc.model_dump_json(),
             exc.id, expected_status.value, iso(expected_updated_at)),
        )
        if cursor.rowcount != 1:
            raise StaleWriteError(f"exception {exc.id} changed concurrently")

    def get_exception(self, conn: sqlite3.Connection, exception_id: str) -> ExceptionRecord | None:
        row = conn.execute("SELECT record_json FROM exceptions WHERE id = ?", (exception_id,)).fetchone()
        return ExceptionRecord.model_validate_json(row["record_json"]) if row else None

    def list_exceptions(self, conn: sqlite3.Connection, status: ExceptionStatus | None = None) -> list[ExceptionRecord]:
        if status is None:
            rows = conn.execute("SELECT record_json FROM exceptions ORDER BY expires_at, id").fetchall()
        else:
            rows = conn.execute(
                "SELECT record_json FROM exceptions WHERE status = ? ORDER BY expires_at, id", (status.value,)
            ).fetchall()
        return [ExceptionRecord.model_validate_json(r["record_json"]) for r in rows]

    # --------------------------------------------------------------- evidence

    def insert_evidence(
        self,
        conn: sqlite3.Connection,
        evidence: EvidenceRecord,
        *,
        commit_origin: str,
        working_tree_dirty: bool | None,
        recorded_by: str,
    ) -> None:
        try:
            conn.execute(
                "INSERT INTO evidence (id, exception_id, scenario_id, source, commit_sha, finished_at, commit_origin,"
                " working_tree_dirty, recorded_at, recorded_by, record_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (evidence.id, evidence.exception_id, evidence.scenario_id, evidence.source.value, evidence.commit_sha,
                 iso(evidence.finished_at), commit_origin,
                 None if working_tree_dirty is None else int(working_tree_dirty),
                 iso(utcnow()), recorded_by, evidence.model_dump_json()),
            )
        except sqlite3.IntegrityError as err:
            raise DuplicateError(f"evidence {evidence.id} rejected: {err}") from err

    def list_evidence(self, conn: sqlite3.Connection, exception_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT e.*, c.project_id AS ci_project_id, c.pipeline_id AS ci_pipeline_id, c.job_id AS ci_job_id,"
            " c.job_name AS ci_job_name, c.ref AS ci_ref, c.commit_sha AS ci_commit_sha,"
            " c.pipeline_web_url AS ci_pipeline_web_url, c.job_web_url AS ci_job_web_url,"
            " c.verified_at AS ci_verified_at, c.verified_by AS ci_verified_by"
            " FROM evidence e LEFT JOIN ci_verifications c ON c.evidence_id = e.id"
            " WHERE e.exception_id = ? ORDER BY e.finished_at, e.id",
            (exception_id,),
        ).fetchall()
        return [
            {
                "evidence": EvidenceRecord.model_validate_json(r["record_json"]),
                "commit_origin": r["commit_origin"],
                "working_tree_dirty": None if r["working_tree_dirty"] is None else bool(r["working_tree_dirty"]),
                "recorded_at": r["recorded_at"],
                "recorded_by": r["recorded_by"],
                "ci_verification": None if r["ci_job_id"] is None else {
                    key: r[f"ci_{key}"] for key in (
                        "project_id", "pipeline_id", "job_id", "job_name", "ref", "commit_sha",
                        "pipeline_web_url", "job_web_url", "verified_at", "verified_by")
                },
            }
            for r in rows
        ]

    def insert_ci_verification(
        self, conn: sqlite3.Connection, exception_id: str, evidence_id: str, provenance: Any, verified_by: str
    ) -> None:
        try:
            conn.execute(
                "INSERT INTO ci_verifications (evidence_id, exception_id, project_id, pipeline_id, job_id, job_name,"
                " ref, commit_sha, pipeline_web_url, job_web_url, verified_at, verified_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (evidence_id, exception_id, provenance.project_id, provenance.pipeline_id, provenance.job_id,
                 provenance.job_name, provenance.ref, provenance.commit_sha, provenance.pipeline_web_url,
                 provenance.job_web_url, iso(utcnow()), verified_by),
            )
        except sqlite3.IntegrityError as err:
            raise DuplicateError(f"CI job {provenance.job_id} already ingested") from err

    def ci_job_ingested(self, conn: sqlite3.Connection, project_id: int, job_id: int) -> bool:
        return conn.execute(
            "SELECT 1 FROM ci_verifications WHERE project_id = ? AND job_id = ?", (project_id, job_id)
        ).fetchone() is not None

    # -------------------------------------------------------------- approvals

    def insert_approval(self, conn: sqlite3.Connection, approval: ApprovalRecord, auth_method: str) -> None:
        conn.execute(
            "INSERT INTO approvals (id, exception_id, kind, decision, approver, auth_method, decided_at, record_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (approval.id, approval.exception_id, approval.kind.value, approval.decision.value, approval.approver,
             auth_method, iso(approval.decided_at), approval.model_dump_json()),
        )

    def list_approvals(self, conn: sqlite3.Connection, exception_id: str) -> list[ApprovalRecord]:
        rows = conn.execute(
            "SELECT record_json FROM approvals WHERE exception_id = ? ORDER BY decided_at, id", (exception_id,)
        ).fetchall()
        return [ApprovalRecord.model_validate_json(r["record_json"]) for r in rows]

    # -------------------------------------------------------------- decisions

    def insert_decision(
        self, conn: sqlite3.Connection, decision_id: str, purpose: str, report: DecisionReport, created_by: str
    ) -> None:
        conn.execute(
            "INSERT INTO decisions (id, exception_id, purpose, recommendation, created_at, created_by, report_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (decision_id, report.exception_id, purpose, report.recommendation.value, iso(report.evaluated_at),
             created_by, report.model_dump_json()),
        )

    def latest_decision(
        self, conn: sqlite3.Connection, exception_id: str, purpose: str
    ) -> tuple[DecisionReport, str] | None:
        row = conn.execute(
            "SELECT report_json, created_by FROM decisions WHERE exception_id = ? AND purpose = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (exception_id, purpose),
        ).fetchone()
        return (DecisionReport.model_validate_json(row["report_json"]), row["created_by"]) if row else None

    # ---------------------------------------------------------- verifications

    def insert_verification(
        self, conn: sqlite3.Connection, verification: RetirementVerification, source: str, issues: list[str]
    ) -> str:
        verification_id = new_id("VER")
        conn.execute(
            "INSERT INTO verifications (id, exception_id, observation_source, issues_json, recorded_at, record_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (verification_id, verification.exception_id, source, json.dumps(issues), iso(utcnow()),
             verification.model_dump_json()),
        )
        return verification_id

    # ------------------------------------------------------------------ audit

    def append_audit(
        self,
        conn: sqlite3.Connection,
        actor: str,
        action: str,
        exception_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            id=new_id("AUD"), occurred_at=utcnow(), actor=actor, action=action,
            exception_id=exception_id, details=details or {},
        )
        conn.execute(
            "INSERT INTO audit_events (id, occurred_at, actor, action, exception_id, details_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (event.id, iso(event.occurred_at), event.actor, event.action, event.exception_id,
             json.dumps(event.details, sort_keys=True, default=str)),
        )
        return event

    def list_audit(self, conn: sqlite3.Connection, exception_id: str | None = None) -> list[AuditEvent]:
        if exception_id is None:
            rows = conn.execute("SELECT * FROM audit_events ORDER BY seq").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE exception_id = ? ORDER BY seq", (exception_id,)
            ).fetchall()
        return [
            AuditEvent(
                id=r["id"], occurred_at=r["occurred_at"], actor=r["actor"], action=r["action"],
                exception_id=r["exception_id"], details=json.loads(r["details_json"]),
            )
            for r in rows
        ]


def _split(script: str) -> list[str]:
    statements, buffer = [], ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise ValueError("incomplete statement in migration")
    return statements
