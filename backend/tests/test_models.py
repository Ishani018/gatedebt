from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.models import ExceptionCreate, ExceptionRecord, RecoveryResult

from .factories import HEAD, NOW, make_evidence, make_exception


def test_valid_exception_creation():
    exc = make_exception()
    assert exc.status == "active"
    assert exc.expires_at.tzinfo == timezone.utc


def test_non_utc_timestamps_are_normalised_to_utc():
    ist = timezone(timedelta(hours=5, minutes=30))
    exc = make_exception(expires_at=datetime(2026, 11, 1, 5, 30, tzinfo=ist))
    assert exc.expires_at == datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
    assert exc.expires_at.utcoffset() == timedelta(0)


def test_naive_timestamp_rejected():
    with pytest.raises(ValidationError, match="timezone-aware"):
        make_exception(expires_at=datetime(2026, 11, 1))


def test_expiry_before_creation_rejected():
    with pytest.raises(ValidationError, match="expires_at must be after created_at"):
        make_exception(expires_at=NOW - timedelta(days=11))


def test_expiry_window_limited():
    with pytest.raises(ValidationError, match="maximum of 90 days"):
        make_exception(expires_at=NOW + timedelta(days=200))


def test_unknown_exception_type_rejected():
    with pytest.raises(ValidationError):
        make_exception(type="disabled_everything")


@pytest.mark.parametrize("field", ["id", "project", "type", "title", "reason", "expires_at", "affected_check"])
def test_missing_required_fields_rejected(field):
    payload = make_exception().model_dump()
    payload.pop(field)
    with pytest.raises(ValidationError):
        ExceptionCreate.model_validate({k: v for k, v in payload.items() if k in ExceptionCreate.model_fields})


@pytest.mark.parametrize("bad", ["", "   ", "x" * 121])
def test_blank_or_oversized_title_rejected(bad):
    with pytest.raises(ValidationError):
        make_exception(title=bad)


def test_bad_identifiers_rejected():
    with pytest.raises(ValidationError):
        make_exception(id="exc 1; drop table")
    with pytest.raises(ValidationError):
        make_exception(created_by="alice")  # actor must be typed, e.g. user:alice


def test_extra_fields_rejected():
    payload = {k: v for k, v in make_exception().model_dump().items() if k in ExceptionCreate.model_fields}
    payload["status"] = "retired"
    with pytest.raises(ValidationError):
        ExceptionCreate.model_validate(payload)


def test_owner_may_be_missing_at_schema_level():
    assert make_exception(owner=None).owner is None


def test_evidence_digest_detects_tampering():
    evidence = make_evidence(make_exception())
    assert evidence.digest_matches()
    tampered = evidence.model_copy(update={"commit_sha": HEAD.replace("a", "c")})
    assert not tampered.digest_matches()


def test_evidence_finish_before_start_rejected():
    with pytest.raises(ValidationError):
        make_evidence(make_exception(), finished_at=NOW - timedelta(days=1), started_at=NOW)


def test_recovery_cannot_succeed_unattempted():
    with pytest.raises(ValidationError):
        RecoveryResult(attempted=False, succeeded=True)


def test_record_round_trips_through_json():
    exc = make_exception()
    assert ExceptionRecord.model_validate_json(exc.model_dump_json()) == exc
