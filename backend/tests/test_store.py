import sqlite3

import pytest

from app.models import ApprovalDecision, ApprovalKind, ApprovalRecord, ExceptionStatus, utcnow
from app.store import SCHEMA_VERSION, DuplicateError, StaleWriteError, Store

from .factories import HEAD, make_evidence, make_exception


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "gatedebt.sqlite3")


def _seed(store):
    exc = make_exception()
    with store.transaction() as conn:
        store.insert_exception(conn, exc)
        store.insert_evidence(conn, make_evidence(exc), commit_origin="provided", working_tree_dirty=False,
                              recorded_by="user:alice")
        store.insert_approval(conn, ApprovalRecord(
            id="APR-1", exception_id=exc.id, kind=ApprovalKind.RETIREMENT, decision=ApprovalDecision.REJECTED,
            approver="user:bob", comment="not yet", decided_at=utcnow(), commit_sha=HEAD), "dev-header")
        store.append_audit(conn, "user:alice", "exception.created", exc.id, {"type": exc.type.value})
    return exc


def test_schema_versioned_and_migration_idempotent(store):
    assert store.schema_version() == SCHEMA_VERSION
    Store(store.path)  # re-opening does not re-run migrations
    assert store.schema_version() == SCHEMA_VERSION


def test_newer_schema_is_refused(store):
    conn = sqlite3.connect(store.path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="newer than this code"):
        Store(store.path)


def test_round_trip(store):
    exc = _seed(store)
    reopened = Store(store.path)
    with reopened.reader() as conn:
        assert reopened.get_exception(conn, exc.id) == exc
        [row] = reopened.list_evidence(conn, exc.id)
        assert row["evidence"].digest_matches()
        assert row["working_tree_dirty"] is False
        assert reopened.list_approvals(conn, exc.id)[0].approver == "user:bob"
        assert [e.action for e in reopened.list_audit(conn, exc.id)] == ["exception.created"]


def test_timestamps_stored_as_fixed_width_utc(store):
    _seed(store)
    with store.reader() as conn:
        row = conn.execute("SELECT expires_at, created_at FROM exceptions").fetchone()
    for value in row:
        assert value.endswith("Z") and len(value) == len("2026-01-01T00:00:00.000000Z")


def test_duplicate_exception_rejected(store):
    _seed(store)
    with pytest.raises(DuplicateError):
        with store.transaction() as conn:
            store.insert_exception(conn, make_exception())


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE evidence SET commit_sha = 'f'",
        "DELETE FROM evidence",
        "UPDATE approvals SET decision = 'approved'",
        "DELETE FROM approvals",
        "UPDATE audit_events SET actor = 'user:mallory'",
        "DELETE FROM audit_events",
        "DELETE FROM exceptions",
    ],
)
def test_history_cannot_be_rewritten(store, sql):
    _seed(store)
    with pytest.raises(sqlite3.DatabaseError, match="append-only|never deleted"):
        with store.transaction() as conn:
            conn.execute(sql)


def test_transaction_rolls_back_on_error(store):
    exc = make_exception()
    with pytest.raises(RuntimeError):
        with store.transaction() as conn:
            store.insert_exception(conn, exc)
            store.append_audit(conn, "user:alice", "exception.created", exc.id)
            raise RuntimeError("crash mid-transaction")
    with store.reader() as conn:
        assert store.get_exception(conn, exc.id) is None
        assert store.list_audit(conn) == []


def test_stale_write_detected(store):
    exc = _seed(store)
    newer = exc.model_copy(update={"status": ExceptionStatus.RETIREMENT_PROPOSED, "updated_at": utcnow()})
    with store.transaction() as conn:
        store.update_exception(conn, newer, exc.status, exc.updated_at)
    with pytest.raises(StaleWriteError):
        with store.transaction() as conn:  # second writer still holds the old version
            store.update_exception(conn, newer, exc.status, exc.updated_at)


def test_foreign_keys_enforced(store):
    with pytest.raises(DuplicateError):
        with store.transaction() as conn:
            store.insert_evidence(conn, make_evidence(make_exception(id="EXC-404")), commit_origin="provided",
                                  working_tree_dirty=False, recorded_by="user:alice")


def test_upgrade_from_schema_v1_preserves_data(tmp_path):
    from app.store import MIGRATIONS, _split

    path = tmp_path / "v1.sqlite3"
    conn = sqlite3.connect(path, isolation_level=None)
    for statement in _split(MIGRATIONS[0]):
        conn.execute(statement)
    conn.execute("PRAGMA user_version = 1")
    exc = make_exception()
    conn.execute(
        "INSERT INTO exceptions (id, project, type, status, owner, expires_at, created_at, updated_at, record_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (exc.id, exc.project, exc.type.value, exc.status.value, exc.owner, "x", "x", "x", exc.model_dump_json()),
    )
    conn.close()
    store = Store(path)
    assert store.schema_version() == SCHEMA_VERSION == 2
    with store.reader() as conn:
        assert store.get_exception(conn, exc.id) == exc
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert {"ci_verifications_no_update", "ci_verifications_no_delete"} <= triggers
