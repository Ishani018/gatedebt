from datetime import timedelta

import pytest

from app.models import (
    CheckOutcome,
    CleanupStatus,
    ExceptionStatus,
    ExceptionType,
    FailureClassification,
    Recommendation,
)
from app.services.decision import evaluate

from .factories import HEAD, NOW, OLD_COMMIT, make_evidence, make_exception, with_check


def test_passing_evidence_proposes_retirement_but_requires_approval():
    exc = make_exception()
    report = evaluate(exc, [make_evidence(exc)], HEAD, NOW)
    assert report.recommendation == Recommendation.PROPOSE_RETIREMENT
    assert report.requires_human_approval
    assert report.supporting_evidence_ids == ["EVD-db-migration-recovery-1"]
    assert "ALL_REQUIRED_CHECKS_PASSED" in report.reason_codes
    # A recommendation never changes the exception itself.
    assert exc.status == ExceptionStatus.ACTIVE


def test_no_evidence_and_not_due_keeps_open():
    report = evaluate(make_exception(), [], HEAD, NOW)
    assert report.recommendation == Recommendation.KEEP_OPEN
    assert report.missing_checks == ["db-migration-recovery:*"]
    assert not report.requires_human_approval


def test_due_without_evidence_needs_investigation():
    report = evaluate(make_exception(expires_at=NOW + timedelta(days=3)), [], HEAD, NOW)
    assert report.recommendation == Recommendation.INVESTIGATE
    assert {"EXCEPTION_DUE", "REHEARSAL_NEEDED"} <= set(report.reason_codes)


def test_expired_without_evidence_requires_renewal_approval():
    exc = make_exception(expires_at=NOW - timedelta(days=1))
    report = evaluate(exc, [], HEAD, NOW)
    assert report.recommendation == Recommendation.RENEWAL_REQUIRES_APPROVAL
    assert report.requires_human_approval
    assert report.risks


def test_missing_owner_blocks_retirement_even_with_passing_evidence():
    exc = make_exception(owner=None)
    report = evaluate(exc, [make_evidence(exc)], HEAD, NOW)
    assert report.recommendation == Recommendation.INVESTIGATE
    assert "OWNER_MISSING" in report.reason_codes


def test_failing_check_recommends_remediation():
    exc = make_exception()
    evidence = with_check(make_evidence(exc), exc.affected_check, CheckOutcome.FAILED)
    report = evaluate(exc, [evidence], HEAD, NOW)
    assert report.recommendation == Recommendation.REMEDIATE
    assert report.failed_checks == [f"db-migration-recovery:{exc.affected_check}"]


def test_missing_recovery_evidence_blocks_retirement():
    exc = make_exception()
    report = evaluate(exc, [make_evidence(exc, recovery=None)], HEAD, NOW)
    assert report.recommendation == Recommendation.REMEDIATE
    assert "db-migration-recovery:RECOVERY_MISSING" in report.reason_codes


def test_stale_evidence_cannot_authorise_newer_revision():
    exc = make_exception()
    report = evaluate(exc, [make_evidence(exc, commit_sha=OLD_COMMIT)], HEAD, NOW)
    assert report.recommendation != Recommendation.PROPOSE_RETIREMENT
    assert report.rejected_evidence["EVD-db-migration-recovery-1"] == ["EVIDENCE_STALE_COMMIT"]
    assert report.supporting_evidence_ids == []


def test_unexpected_infrastructure_failure_is_inconclusive_not_a_pass():
    exc = make_exception()
    evidence = make_evidence(exc, injected_failure_classification=FailureClassification.UNEXPECTED_INFRASTRUCTURE)
    report = evaluate(exc, [evidence], HEAD, NOW)
    assert report.recommendation == Recommendation.INVESTIGATE
    assert "REHEARSAL_INCONCLUSIVE" in report.reason_codes


def test_failed_cleanup_blocks_retirement():
    exc = make_exception()
    report = evaluate(exc, [make_evidence(exc, cleanup_status=CleanupStatus.FAILED)], HEAD, NOW)
    assert report.recommendation == Recommendation.INVESTIGATE
    assert "db-migration-recovery:CLEANUP_FAILED" in report.reason_codes


def test_latest_run_wins():
    exc = make_exception()
    old_pass = make_evidence(exc, id="EVD-old", finished_at=NOW - timedelta(hours=3), started_at=NOW - timedelta(hours=4))
    new_fail = with_check(make_evidence(exc, id="EVD-new"), "post_recovery_schema_valid", CheckOutcome.FAILED)
    report = evaluate(exc, [old_pass, new_fail], HEAD, NOW)
    assert report.recommendation == Recommendation.REMEDIATE
    assert report.supporting_evidence_ids == ["EVD-new"]


def test_release_readiness_needs_both_modes():
    exc = make_exception(type=ExceptionType.WAIVED_RELEASE_READINESS_CHECK)
    only_sandbox = evaluate(exc, [make_evidence(exc)], HEAD, NOW)
    assert only_sandbox.recommendation != Recommendation.PROPOSE_RETIREMENT
    assert "EVIDENCE_MISSING:pipeline-gate-recovery" in only_sandbox.reason_codes

    both = evaluate(exc, [make_evidence(exc), make_evidence(exc, scenario_id="pipeline-gate-recovery")], HEAD, NOW)
    assert both.recommendation == Recommendation.PROPOSE_RETIREMENT


def test_repeatedly_renewed_flagged():
    exc = make_exception(renewal_count=3)
    report = evaluate(exc, [], HEAD, NOW)
    assert report.recommendation == Recommendation.INVESTIGATE
    assert "REPEATEDLY_RENEWED" in report.reason_codes


def test_retired_exception_cannot_be_evaluated():
    with pytest.raises(ValueError):
        evaluate(make_exception(status=ExceptionStatus.RETIRED), [], HEAD, NOW)
