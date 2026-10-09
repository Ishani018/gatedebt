"""Deterministic policy engine.

Everything that decides whether an action is *permitted* lives here, in plain
Python. Agents and the UI may propose actions; this module decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.models import (
    MAX_EXCEPTION_WINDOW,
    ALLOWED_TRANSITIONS,
    ApprovalDecision,
    ApprovalKind,
    ApprovalRecord,
    CheckOutcome,
    CleanupStatus,
    EvidenceRecord,
    EvidenceSource,
    ExceptionRecord,
    ExceptionStatus,
    ExceptionType,
    ExpiryState,
    FailureClassification,
    RehearsalMode,
    RetirementVerification,
)


class PolicyViolation(Exception):
    """Raised when a requested action is not permitted by policy."""

    def __init__(self, reason_codes: list[str]):
        self.reason_codes = list(reason_codes)
        super().__init__(", ".join(self.reason_codes))


@dataclass(frozen=True)
class ScenarioRequirement:
    scenario_id: str
    mode: RehearsalMode
    required_checks: tuple[str, ...]


# The approved rehearsal scenarios. Only these can be requested or accepted
# as evidence; anything else is rejected as an unknown scenario.
SANDBOX_DB_MIGRATION = ScenarioRequirement(
    scenario_id="db-migration-recovery",
    mode=RehearsalMode.SANDBOX,
    required_checks=(
        "injected_failure_detected",
        "recovery_procedure_completed",
        "post_recovery_schema_valid",
        "post_recovery_data_intact",
    ),
)
PIPELINE_GATE_RECOVERY = ScenarioRequirement(
    scenario_id="pipeline-gate-recovery",
    mode=RehearsalMode.PIPELINE,
    required_checks=(
        "injected_failure_detected",
        "failure_classified_expected",
        "retry_workflow_passed",
    ),
)
APPROVED_SCENARIOS: dict[str, ScenarioRequirement] = {
    s.scenario_id: s for s in (SANDBOX_DB_MIGRATION, PIPELINE_GATE_RECOVERY)
}

REQUIRED_SCENARIOS: dict[ExceptionType, tuple[ScenarioRequirement, ...]] = {
    ExceptionType.SKIPPED_INTEGRATION_TEST: (SANDBOX_DB_MIGRATION,),
    ExceptionType.WAIVED_QUALITY_GATE: (PIPELINE_GATE_RECOVERY,),
    ExceptionType.WAIVED_RELEASE_READINESS_CHECK: (SANDBOX_DB_MIGRATION, PIPELINE_GATE_RECOVERY),
}


@dataclass(frozen=True)
class PolicyConfig:
    due_window: timedelta = timedelta(days=7)
    max_evidence_age: timedelta = timedelta(days=7)
    # Small tolerance for clock skew between runners and the backend.
    clock_skew: timedelta = timedelta(minutes=5)
    trusted_sources: frozenset[EvidenceSource] = field(
        default_factory=lambda: frozenset({EvidenceSource.LOCAL_SANDBOX, EvidenceSource.GITLAB_CI})
    )
    repeated_renewal_threshold: int = 2


DEFAULT_POLICY = PolicyConfig()


# ---------------------------------------------------------------- exceptions

def expiry_state(exc: ExceptionRecord, now: datetime, config: PolicyConfig = DEFAULT_POLICY) -> ExpiryState:
    if now >= exc.expires_at:
        return ExpiryState.EXPIRED
    if exc.expires_at - now <= config.due_window:
        return ExpiryState.DUE
    return ExpiryState.ACTIVE


def completeness_issues(exc: ExceptionRecord) -> list[str]:
    issues = []
    if not exc.owner:
        issues.append("OWNER_MISSING")
    if not exc.remediation_target:
        issues.append("REMEDIATION_TARGET_MISSING")
    return issues


def required_checks(exc: ExceptionRecord) -> dict[str, tuple[str, ...]]:
    """Checks that must pass, per scenario, before retirement can be proposed.

    The waived check itself (``affected_check``) is always required: the
    point of retiring a waiver is that the waived check now passes.
    """
    return {
        req.scenario_id: (*req.required_checks, exc.affected_check)
        for req in REQUIRED_SCENARIOS[exc.type]
    }


def check_transition(current: ExceptionStatus, target: ExceptionStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise PolicyViolation([f"TRANSITION_FORBIDDEN:{current.value}->{target.value}"])


# ------------------------------------------------------------------ evidence

def evidence_rejections(
    evidence: EvidenceRecord,
    exc: ExceptionRecord,
    current_commit: str,
    now: datetime,
    config: PolicyConfig = DEFAULT_POLICY,
) -> list[str]:
    """Provenance checks. Any reason here means the record cannot be used at all."""
    reasons = []
    if evidence.exception_id != exc.id:
        reasons.append("EVIDENCE_EXCEPTION_MISMATCH")
    if not evidence.digest_matches():
        reasons.append("EVIDENCE_DIGEST_MISMATCH")
    if evidence.source not in config.trusted_sources:
        reasons.append("EVIDENCE_UNTRUSTED_SOURCE")
    if evidence.commit_sha != current_commit:
        reasons.append("EVIDENCE_STALE_COMMIT")
    if evidence.finished_at > now + config.clock_skew:
        reasons.append("EVIDENCE_FROM_FUTURE")
    elif now - evidence.finished_at > config.max_evidence_age:
        reasons.append("EVIDENCE_TOO_OLD")

    scenario = APPROVED_SCENARIOS.get(evidence.scenario_id)
    if scenario is None:
        reasons.append("EVIDENCE_UNKNOWN_SCENARIO")
    else:
        if scenario.mode != evidence.mode:
            reasons.append("EVIDENCE_MODE_MISMATCH")
        if scenario not in REQUIRED_SCENARIOS[exc.type]:
            reasons.append("EVIDENCE_SCENARIO_NOT_REQUIRED")
    if evidence.source == EvidenceSource.GITLAB_CI and (
        evidence.pipeline_id is None or evidence.job_id is None
    ):
        reasons.append("EVIDENCE_PIPELINE_REFERENCE_MISSING")
    return reasons


def rehearsal_failures(evidence: EvidenceRecord, checks: tuple[str, ...]) -> list[str]:
    """Outcome checks for trusted evidence. Empty list means the rehearsal passed."""
    failures = []
    match evidence.injected_failure_classification:
        case FailureClassification.UNEXPECTED_INFRASTRUCTURE:
            failures.append("UNEXPECTED_INFRASTRUCTURE_FAILURE")
        case FailureClassification.INJECTED_NOT_DETECTED:
            failures.append("INJECTED_FAILURE_NOT_DETECTED")

    if evidence.recovery is None or not evidence.recovery.attempted:
        failures.append("RECOVERY_MISSING")
    elif not evidence.recovery.succeeded:
        failures.append("RECOVERY_FAILED")
    else:
        for assertion in evidence.recovery.assertions:
            if assertion.outcome != CheckOutcome.PASSED:
                failures.append(f"RECOVERY_ASSERTION_FAILED:{assertion.check_id}")

    if evidence.cleanup_status == CleanupStatus.FAILED:
        failures.append("CLEANUP_FAILED")
    elif evidence.cleanup_status == CleanupStatus.UNKNOWN:
        failures.append("CLEANUP_UNVERIFIED")

    outcomes: dict[str, CheckOutcome] = {}
    for result in evidence.check_results:
        # A check reported twice counts as its worst outcome.
        if outcomes.get(result.check_id, CheckOutcome.PASSED) == CheckOutcome.PASSED:
            outcomes[result.check_id] = result.outcome
    for check_id in checks:
        outcome = outcomes.get(check_id)
        if outcome is None:
            failures.append(f"CHECK_MISSING:{check_id}")
        elif outcome != CheckOutcome.PASSED:
            failures.append(f"CHECK_FAILED:{check_id}")
    return failures


# ------------------------------------------------------- approvals/retirement

def renewal_issues(exc: ExceptionRecord, approval: ApprovalRecord, now: datetime) -> list[str]:
    issues = []
    if exc.status != ExceptionStatus.ACTIVE:
        issues.append("RENEWAL_REQUIRES_ACTIVE_EXCEPTION")
    if approval.exception_id != exc.id:
        issues.append("APPROVAL_EXCEPTION_MISMATCH")
    if approval.kind != ApprovalKind.RENEWAL:
        issues.append("APPROVAL_KIND_MISMATCH")
    if approval.decision != ApprovalDecision.APPROVED:
        issues.append("RENEWAL_NOT_APPROVED")
    new_expiry = approval.new_expires_at
    if new_expiry is None:
        issues.append("RENEWAL_EXPIRY_MISSING")
    else:
        if new_expiry <= max(now, exc.expires_at):
            issues.append("RENEWAL_EXPIRY_NOT_EXTENDED")
        if new_expiry - now > MAX_EXCEPTION_WINDOW:
            issues.append("RENEWAL_WINDOW_TOO_LONG")
    return issues


def retirement_issues(
    exc: ExceptionRecord,
    approval: ApprovalRecord | None,
    verification: RetirementVerification,
) -> list[str]:
    """An exception is retired only after approval AND a verified merged change."""
    issues = []
    if exc.status != ExceptionStatus.RETIREMENT_APPROVED:
        issues.append("RETIREMENT_NOT_APPROVED")
    if (
        approval is None
        or approval.exception_id != exc.id
        or approval.kind != ApprovalKind.RETIREMENT
        or approval.decision != ApprovalDecision.APPROVED
    ):
        issues.append("APPROVAL_MISSING")
    if verification.exception_id != exc.id:
        issues.append("VERIFICATION_EXCEPTION_MISMATCH")
    if not verification.change_merged:
        issues.append("CHANGE_NOT_MERGED")
    if verification.waiver_still_present:
        issues.append("WAIVER_STILL_PRESENT")
    if verification.pipeline_status != "success":
        issues.append("VERIFICATION_PIPELINE_NOT_SUCCESSFUL")
    if approval is not None and verification.verified_at < approval.decided_at:
        issues.append("VERIFICATION_PREDATES_APPROVAL")
    return issues
