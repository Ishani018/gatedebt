"""Decision engine: turns an exception plus its evidence into a recommendation.

A recommendation is advice. ``propose_retirement`` never retires anything; it
only makes the exception eligible for a human-approved retirement proposal.
"""

from __future__ import annotations

from datetime import datetime

from app.models import (
    DecisionReport,
    EvidenceRecord,
    ExceptionRecord,
    ExceptionStatus,
    ExpiryState,
    Recommendation,
)

from .policy import (
    DEFAULT_POLICY,
    PolicyConfig,
    completeness_issues,
    evidence_rejections,
    expiry_state,
    rehearsal_failures,
    required_checks,
)

_APPROVAL_REQUIRED = {
    Recommendation.PROPOSE_RETIREMENT,
    Recommendation.RENEWAL_REQUIRES_APPROVAL,
    Recommendation.REMEDIATE,
}
_INCONCLUSIVE = {"UNEXPECTED_INFRASTRUCTURE_FAILURE", "CLEANUP_FAILED", "CLEANUP_UNVERIFIED"}


def evaluate(
    exc: ExceptionRecord,
    evidence: list[EvidenceRecord],
    current_commit: str,
    now: datetime,
    config: PolicyConfig = DEFAULT_POLICY,
) -> DecisionReport:
    if exc.status == ExceptionStatus.RETIRED:
        raise ValueError(f"{exc.id} is already retired")

    reasons: list[str] = []
    risks: list[str] = []
    state = expiry_state(exc, now, config)
    if state == ExpiryState.EXPIRED:
        reasons.append("EXCEPTION_EXPIRED")
        risks.append("Waiver is past its expiry date and still in effect.")
    elif state == ExpiryState.DUE:
        reasons.append("EXCEPTION_DUE")

    incomplete = completeness_issues(exc)
    reasons.extend(incomplete)
    if "OWNER_MISSING" in incomplete:
        risks.append("Nobody is accountable for removing this waiver.")
    if exc.renewal_count >= config.repeated_renewal_threshold:
        reasons.append("REPEATEDLY_RENEWED")
        risks.append(f"Waiver has been renewed {exc.renewal_count} times.")

    # Provenance first: untrusted, stale or tampered records are set aside.
    rejected: dict[str, list[str]] = {}
    usable: list[EvidenceRecord] = []
    for record in evidence:
        problems = evidence_rejections(record, exc, current_commit, now, config)
        if problems:
            rejected[record.id] = problems
        else:
            usable.append(record)
    if rejected:
        reasons.append("EVIDENCE_REJECTED")

    # Then outcomes: the latest usable run per required scenario decides.
    supporting: list[str] = []
    failed: list[str] = []
    missing: list[str] = []
    outcome_codes: set[str] = set()
    all_passed = True
    for scenario_id, checks in required_checks(exc).items():
        runs = [r for r in usable if r.scenario_id == scenario_id]
        if not runs:
            all_passed = False
            missing.append(f"{scenario_id}:*")
            reasons.append(f"EVIDENCE_MISSING:{scenario_id}")
            continue
        latest = max(runs, key=lambda r: r.finished_at)
        supporting.append(latest.id)
        problems = rehearsal_failures(latest, checks)
        if problems:
            all_passed = False
            reasons.extend(f"{scenario_id}:{p}" for p in problems)
            outcome_codes.update(p.split(":", 1)[0] for p in problems)
            for p in problems:
                code, _, check = p.partition(":")
                if code == "CHECK_FAILED":
                    failed.append(f"{scenario_id}:{check}")
                elif code == "CHECK_MISSING":
                    missing.append(f"{scenario_id}:{check}")

    if incomplete:
        recommendation = Recommendation.INVESTIGATE
    elif all_passed:
        recommendation = Recommendation.PROPOSE_RETIREMENT
        reasons.append("ALL_REQUIRED_CHECKS_PASSED")
    elif outcome_codes & _INCONCLUSIVE:
        recommendation = Recommendation.INVESTIGATE
        reasons.append("REHEARSAL_INCONCLUSIVE")
    elif outcome_codes:
        recommendation = Recommendation.REMEDIATE
        risks.append("Recovery rehearsal shows the waived problem is not fixed.")
    elif state == ExpiryState.EXPIRED:
        recommendation = Recommendation.RENEWAL_REQUIRES_APPROVAL
    elif state == ExpiryState.DUE or "REPEATEDLY_RENEWED" in reasons:
        recommendation = Recommendation.INVESTIGATE
        reasons.append("REHEARSAL_NEEDED")
    else:
        recommendation = Recommendation.KEEP_OPEN

    return DecisionReport(
        exception_id=exc.id,
        evaluated_at=now,
        commit_sha=current_commit,
        expiry_state=state,
        recommendation=recommendation,
        reason_codes=reasons,
        supporting_evidence_ids=supporting,
        rejected_evidence=rejected,
        failed_checks=failed,
        missing_checks=missing,
        risks=risks,
        requires_human_approval=recommendation in _APPROVAL_REQUIRED,
    )
