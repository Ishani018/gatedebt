from datetime import timedelta

import pytest

from app.models import (
    ApprovalDecision,
    ApprovalKind,
    ApprovalRecord,
    CheckOutcome,
    CleanupStatus,
    EvidenceSource,
    ExceptionStatus,
    ExceptionType,
    ExpiryState,
    FailureClassification,
    RecoveryResult,
    RetirementVerification,
)
from app.services.policy import (
    PolicyViolation,
    check_transition,
    completeness_issues,
    evidence_rejections,
    expiry_state,
    rehearsal_failures,
    renewal_issues,
    required_checks,
    retirement_issues,
)

from .factories import HEAD, NOW, OLD_COMMIT, make_evidence, make_exception, with_check

SANDBOX_CHECKS = required_checks(make_exception())["db-migration-recovery"]


# ------------------------------------------------------------------ expiry

@pytest.mark.parametrize(
    "delta, expected",
    [
        (timedelta(days=30), ExpiryState.ACTIVE),
        (timedelta(days=7, seconds=1), ExpiryState.ACTIVE),
        (timedelta(days=7), ExpiryState.DUE),
        (timedelta(seconds=1), ExpiryState.DUE),
        (timedelta(0), ExpiryState.EXPIRED),
        (timedelta(days=-3), ExpiryState.EXPIRED),
    ],
)
def test_expiry_boundaries(delta, expected):
    exc = make_exception(expires_at=NOW + delta)
    assert expiry_state(exc, NOW) == expected


def test_completeness_flags_missing_owner_and_target():
    assert completeness_issues(make_exception(owner=None, remediation_target=None)) == [
        "OWNER_MISSING",
        "REMEDIATION_TARGET_MISSING",
    ]
    assert completeness_issues(make_exception()) == []


def test_required_checks_include_waived_check():
    exc = make_exception(type=ExceptionType.WAIVED_RELEASE_READINESS_CHECK, affected_check="release:readiness")
    checks = required_checks(exc)
    assert set(checks) == {"db-migration-recovery", "pipeline-gate-recovery"}
    assert all("release:readiness" in c for c in checks.values())


# -------------------------------------------------------------- transitions

@pytest.mark.parametrize(
    "current, target",
    [
        (ExceptionStatus.ACTIVE, ExceptionStatus.RETIREMENT_PROPOSED),
        (ExceptionStatus.RETIREMENT_PROPOSED, ExceptionStatus.RETIREMENT_APPROVED),
        (ExceptionStatus.RETIREMENT_PROPOSED, ExceptionStatus.ACTIVE),
        (ExceptionStatus.RETIREMENT_APPROVED, ExceptionStatus.RETIRED),
    ],
)
def test_permitted_transitions(current, target):
    check_transition(current, target)


@pytest.mark.parametrize(
    "current, target",
    [
        (ExceptionStatus.ACTIVE, ExceptionStatus.RETIRED),
        (ExceptionStatus.ACTIVE, ExceptionStatus.RETIREMENT_APPROVED),
        (ExceptionStatus.RETIREMENT_PROPOSED, ExceptionStatus.RETIRED),
        (ExceptionStatus.RETIRED, ExceptionStatus.ACTIVE),
    ],
)
def test_prohibited_transitions(current, target):
    with pytest.raises(PolicyViolation, match="TRANSITION_FORBIDDEN"):
        check_transition(current, target)


# ------------------------------------------------------------------ evidence

def test_good_evidence_accepted_and_passes():
    exc = make_exception()
    evidence = make_evidence(exc)
    assert evidence_rejections(evidence, exc, HEAD, NOW) == []
    assert rehearsal_failures(evidence, SANDBOX_CHECKS) == []


def test_stale_commit_evidence_rejected():
    exc = make_exception()
    evidence = make_evidence(exc, commit_sha=OLD_COMMIT)
    assert "EVIDENCE_STALE_COMMIT" in evidence_rejections(evidence, exc, HEAD, NOW)


def test_old_and_future_evidence_rejected():
    exc = make_exception()
    old = make_evidence(exc, started_at=NOW - timedelta(days=9), finished_at=NOW - timedelta(days=8))
    future = make_evidence(exc, started_at=NOW, finished_at=NOW + timedelta(hours=1))
    assert "EVIDENCE_TOO_OLD" in evidence_rejections(old, exc, HEAD, NOW)
    assert "EVIDENCE_FROM_FUTURE" in evidence_rejections(future, exc, HEAD, NOW)


def test_untrusted_and_tampered_evidence_rejected():
    exc = make_exception()
    fixture = make_evidence(exc, source=EvidenceSource.TEST_FIXTURE)
    assert "EVIDENCE_UNTRUSTED_SOURCE" in evidence_rejections(fixture, exc, HEAD, NOW)
    tampered = make_evidence(exc).model_copy(update={"cleanup_status": CleanupStatus.VERIFIED, "job_id": 999})
    assert "EVIDENCE_DIGEST_MISMATCH" in evidence_rejections(tampered, exc, HEAD, NOW)


def test_evidence_for_other_exception_or_unknown_scenario_rejected():
    exc = make_exception()
    other = make_evidence(make_exception(id="EXC-999"))
    assert "EVIDENCE_EXCEPTION_MISMATCH" in evidence_rejections(other, exc, HEAD, NOW)
    unknown = make_evidence(exc, scenario_id="rm-rf-prod")
    assert "EVIDENCE_UNKNOWN_SCENARIO" in evidence_rejections(unknown, exc, HEAD, NOW)


def test_pipeline_evidence_needs_pipeline_reference():
    exc = make_exception(type=ExceptionType.WAIVED_QUALITY_GATE)
    evidence = make_evidence(exc, scenario_id="pipeline-gate-recovery", pipeline_id=None)
    assert "EVIDENCE_PIPELINE_REFERENCE_MISSING" in evidence_rejections(evidence, exc, HEAD, NOW)


def test_failing_and_missing_checks_reported():
    exc = make_exception()
    evidence = with_check(make_evidence(exc), "post_recovery_data_intact", CheckOutcome.FAILED)
    fields = evidence.model_dump(exclude={"digest"})
    fields["check_results"] = [r for r in evidence.check_results if r.check_id != exc.affected_check]
    evidence = type(evidence).seal(**fields)
    failures = rehearsal_failures(evidence, SANDBOX_CHECKS)
    assert "CHECK_FAILED:post_recovery_data_intact" in failures
    assert f"CHECK_MISSING:{exc.affected_check}" in failures


def test_duplicate_check_counts_as_worst_outcome():
    exc = make_exception()
    evidence = make_evidence(exc)
    fields = evidence.model_dump(exclude={"digest"})
    fields["check_results"].append({"check_id": exc.affected_check, "outcome": "failed"})
    evidence = type(evidence).seal(**fields)
    assert f"CHECK_FAILED:{exc.affected_check}" in rehearsal_failures(evidence, SANDBOX_CHECKS)


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"recovery": None}, "RECOVERY_MISSING"),
        ({"recovery": RecoveryResult(attempted=True, succeeded=False)}, "RECOVERY_FAILED"),
        ({"cleanup_status": CleanupStatus.FAILED}, "CLEANUP_FAILED"),
        ({"cleanup_status": CleanupStatus.UNKNOWN}, "CLEANUP_UNVERIFIED"),
        (
            {"injected_failure_classification": FailureClassification.UNEXPECTED_INFRASTRUCTURE},
            "UNEXPECTED_INFRASTRUCTURE_FAILURE",
        ),
        (
            {"injected_failure_classification": FailureClassification.INJECTED_NOT_DETECTED},
            "INJECTED_FAILURE_NOT_DETECTED",
        ),
    ],
)
def test_rehearsal_outcome_failures(overrides, expected):
    evidence = make_evidence(make_exception(), **overrides)
    assert expected in rehearsal_failures(evidence, SANDBOX_CHECKS)


# ------------------------------------------------------ renewal & retirement

def _approval(**overrides):
    fields = dict(
        id="APR-1",
        exception_id="EXC-001",
        kind=ApprovalKind.RETIREMENT,
        decision=ApprovalDecision.APPROVED,
        approver="user:bob",
        comment="Evidence reviewed",
        decided_at=NOW,
        commit_sha=HEAD,
    )
    fields.update(overrides)
    return ApprovalRecord(**fields)


def _verification(**overrides):
    fields = dict(
        exception_id="EXC-001",
        merged_commit_sha="c" * 40,
        change_merged=True,
        waiver_still_present=False,
        pipeline_status="success",
        verified_at=NOW + timedelta(hours=1),
        observed_by="ci:pipeline-303",
    )
    fields.update(overrides)
    return RetirementVerification(**fields)


def test_agents_cannot_approve():
    with pytest.raises(ValueError):
        _approval(approver="agent:remediation-planner")


def test_retirement_allowed_only_with_approval_and_verified_merge():
    exc = make_exception(status=ExceptionStatus.RETIREMENT_APPROVED)
    assert retirement_issues(exc, _approval(), _verification()) == []


@pytest.mark.parametrize(
    "exc_status, approval, verification, expected",
    [
        (ExceptionStatus.RETIREMENT_PROPOSED, _approval(), _verification(), "RETIREMENT_NOT_APPROVED"),
        (ExceptionStatus.RETIREMENT_APPROVED, None, _verification(), "APPROVAL_MISSING"),
        (
            ExceptionStatus.RETIREMENT_APPROVED,
            _approval(decision=ApprovalDecision.REJECTED),
            _verification(),
            "APPROVAL_MISSING",
        ),
        (ExceptionStatus.RETIREMENT_APPROVED, _approval(), _verification(change_merged=False), "CHANGE_NOT_MERGED"),
        (
            ExceptionStatus.RETIREMENT_APPROVED,
            _approval(),
            _verification(waiver_still_present=True),
            "WAIVER_STILL_PRESENT",
        ),
        (
            ExceptionStatus.RETIREMENT_APPROVED,
            _approval(),
            _verification(pipeline_status="failed"),
            "VERIFICATION_PIPELINE_NOT_SUCCESSFUL",
        ),
        (
            ExceptionStatus.RETIREMENT_APPROVED,
            _approval(),
            _verification(verified_at=NOW - timedelta(hours=1)),
            "VERIFICATION_PREDATES_APPROVAL",
        ),
    ],
)
def test_retirement_blocked(exc_status, approval, verification, expected):
    exc = make_exception(status=exc_status)
    assert expected in retirement_issues(exc, approval, verification)


def test_renewal_rules():
    exc = make_exception(expires_at=NOW + timedelta(days=2))
    ok = _approval(kind=ApprovalKind.RENEWAL, new_expires_at=NOW + timedelta(days=30))
    assert renewal_issues(exc, ok, NOW) == []

    not_extended = _approval(kind=ApprovalKind.RENEWAL, new_expires_at=NOW + timedelta(days=1))
    assert "RENEWAL_EXPIRY_NOT_EXTENDED" in renewal_issues(exc, not_extended, NOW)

    too_long = _approval(kind=ApprovalKind.RENEWAL, new_expires_at=NOW + timedelta(days=120))
    assert "RENEWAL_WINDOW_TOO_LONG" in renewal_issues(exc, too_long, NOW)

    rejected = _approval(
        kind=ApprovalKind.RENEWAL, decision=ApprovalDecision.REJECTED, new_expires_at=NOW + timedelta(days=30)
    )
    assert "RENEWAL_NOT_APPROVED" in renewal_issues(exc, rejected, NOW)
