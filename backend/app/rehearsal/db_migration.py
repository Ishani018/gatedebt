"""Scenario A: db-migration-recovery (sandbox mode).

A partially applied SQLite migration, recovered by restoring a backup and
re-applying the remediated migration in a transaction. See
``scenarios/database-migration/README.md``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.models import FailureClassification
from app.services.policy import SANDBOX_DB_MIGRATION

from .harness import SCENARIOS_DIR, Scenario


def split_statements(sql: str) -> list[str]:
    """Split a fixture file into complete SQL statements."""
    statements, buffer = [], ""
    for line in sql.splitlines(keepends=True):
        if line.lstrip().startswith("--") and not buffer.strip():
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise ValueError(f"incomplete SQL statement in fixture: {buffer.strip()[:80]}")
    return statements


class DbMigrationRecovery(Scenario):
    requirement = SANDBOX_DB_MIGRATION
    exercised_check = "integration:migration_0042"
    fixture_dir = SCENARIOS_DIR / "database-migration"

    def __init__(self, workdir: Path, recorder) -> None:
        super().__init__(workdir, recorder)
        self._connections: list[sqlite3.Connection] = []

    # ---------------------------------------------------------------- helpers

    def _sql(self, key: str) -> str:
        return (self.fixture_dir / self.fixture[key]).read_text()

    def _connect(self, name: str) -> sqlite3.Connection:
        path = self.workdir / name
        # Refuse to touch anything outside the scenario's temporary directory.
        if path.resolve().parent != self.workdir.resolve():
            raise RuntimeError(f"refusing to open database outside sandbox: {path}")
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        self._connections.append(conn)
        return conn

    def _seed(self, conn: sqlite3.Connection) -> None:
        conn.executescript(self._sql("seed"))

    @staticmethod
    def _version(conn: sqlite3.Connection) -> int:
        return conn.execute("SELECT version FROM schema_version").fetchone()[0]

    @staticmethod
    def _tables(conn: sqlite3.Connection) -> set[str]:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    @staticmethod
    def _schema(conn: sqlite3.Connection) -> list[tuple]:
        return conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()

    @staticmethod
    def _rows(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
        return {
            "customers": conn.execute("SELECT id, email, name, created_at FROM customers ORDER BY id").fetchall(),
            "invoices": conn.execute("SELECT id, customer_id, amount_cents FROM invoices ORDER BY id").fetchall(),
        }

    def _apply_transactional(self, conn: sqlite3.Connection, statements: list[str]) -> None:
        conn.execute("BEGIN")
        try:
            for statement in statements:
                conn.execute(statement)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    # --------------------------------------------------------------- scenario

    def execute(self) -> None:
        fx = self.fixture
        db = self._connect("app.db")
        self._seed(db)
        schema_before = self._schema(db)
        rows_before = self._rows(db)
        backup = self._connect("backup.db")
        db.backup(backup)
        self.rec.log(f"seeded schema v{self._version(db)} and took backup")

        # Inject: the shipped migration, statement by statement in autocommit.
        error: sqlite3.Error | None = None
        for statement in split_statements(self._sql("broken_migration")):
            try:
                db.execute(statement)
            except sqlite3.Error as exc:
                error = exc
                break
        expected = fx["expected_error"]
        if error is None:
            self.rec.classification = FailureClassification.INJECTED_NOT_DETECTED
            self.rec.check("injected_failure_detected", False, "broken migration applied without error")
            return
        if type(error).__name__ != expected["type"] or expected["message_contains"] not in str(error):
            self.rec.classification = FailureClassification.UNEXPECTED_INFRASTRUCTURE
            self.rec.check("injected_failure_detected", False, f"unexpected error {type(error).__name__}: {error}")
            return
        self.rec.classification = FailureClassification.EXPECTED_INJECTED
        partial = fx["partial_artifact_table"] in self._tables(db) and self._version(db) == fx["from_version"]
        self.rec.check(
            "injected_failure_detected",
            partial,
            f"{type(error).__name__}: {error}; partial state "
            + ("observed" if partial else "NOT observed")
            + f" ({fx['partial_artifact_table']} present, version {self._version(db)})",
        )

        # Recover: restore the backup, then apply the fixed migration atomically.
        self.rec.recovery_attempted = True
        backup.backup(db)
        restored = self._schema(db) == schema_before and self._version(db) == fx["from_version"]
        self.rec.assertion("pre_migration_backup_restored", restored, f"version {self._version(db)}")
        try:
            self._apply_transactional(db, split_statements(self._sql("fixed_migration")))
            committed = self._version(db) == fx["to_version"]
            self.rec.assertion("fixed_migration_committed", committed, f"version {self._version(db)}")
        except sqlite3.Error as exc:
            committed = False
            self.rec.assertion("fixed_migration_committed", False, f"{type(exc).__name__}: {exc}")
        self.rec.recovery_succeeded = restored and committed
        self.rec.check("recovery_procedure_completed", self.rec.recovery_succeeded)

        self._verify_schema(db)
        self._verify_data(db, rows_before)
        self._run_waived_integration_test()

    def _verify_schema(self, db: sqlite3.Connection) -> None:
        fx = self.fixture
        columns = [[c[1], c[2], c[3], c[4]] for c in db.execute("PRAGMA table_info(customers)")]
        problems = []
        if columns != fx["expected_customer_columns"]:
            problems.append(f"customers columns {columns}")
        if fx["partial_artifact_table"] not in self._tables(db):
            problems.append(f"{fx['partial_artifact_table']} missing")
        if self._version(db) != fx["to_version"]:
            problems.append(f"version {self._version(db)}")
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            problems.append(f"integrity_check {integrity}")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            problems.append("foreign key violations")
        self.rec.check("post_recovery_schema_valid", not problems, "; ".join(problems) or "schema v42, integrity ok")

    def _verify_data(self, db: sqlite3.Connection, rows_before: dict[str, list[tuple]]) -> None:
        fx = self.fixture
        rows_after = self._rows(db)
        problems = [f"{table} rows changed" for table in rows_before if rows_before[table] != rows_after[table]]
        for table, count in fx["expected_row_counts"].items():
            if len(rows_after[table]) != count:
                problems.append(f"{table} has {len(rows_after[table])} rows, expected {count}")
        tiers = {r[0] for r in db.execute("SELECT DISTINCT tier FROM customers")}
        if tiers != {fx["expected_default_tier"]}:
            problems.append(f"tier values {sorted(map(str, tiers))}")
        detail = "; ".join(problems) or ", ".join(f"{t}={len(r)} rows unchanged" for t, r in rows_after.items())
        self.rec.check("post_recovery_data_intact", not problems, detail)

    def _run_waived_integration_test(self) -> None:
        """The integration test that was skipped: run it for real."""
        fx = self.fixture
        fixed = split_statements(self._sql("fixed_migration"))
        problems = []

        clean = self._connect("integration_clean.db")
        self._seed(clean)
        try:
            self._apply_transactional(clean, fixed)
            if self._version(clean) != fx["to_version"]:
                problems.append("fresh apply did not reach target version")
        except sqlite3.Error as exc:
            problems.append(f"fresh apply failed: {exc}")

        atomic = self._connect("integration_atomic.db")
        self._seed(atomic)
        try:
            self._apply_transactional(atomic, [*fixed, "SELECT * FROM __gatedebt_forced_failure__"])
            problems.append("forced mid-migration failure did not raise")
        except sqlite3.OperationalError:
            if fx["partial_artifact_table"] in self._tables(atomic) or self._version(atomic) != fx["from_version"]:
                problems.append("failed migration left partial state (not atomic)")
        self.rec.check(
            self.exercised_check,
            not problems,
            "; ".join(problems) or "applies cleanly on fresh database; mid-migration failure rolls back fully",
        )

    # ---------------------------------------------------------------- cleanup

    def release(self) -> None:
        for conn in self._connections:
            conn.close()

    def leaked_resources(self) -> list[str]:
        leaked = []
        for conn in self._connections:
            try:
                conn.execute("SELECT 1")
                leaked.append("open sqlite connection")
            except sqlite3.ProgrammingError:
                pass  # closed, as expected
        return leaked
