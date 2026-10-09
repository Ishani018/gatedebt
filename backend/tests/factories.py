"""Test-only builders. Evidence built here uses real sources only where a test
needs trusted evidence; it is never loaded by the application."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import (
    CheckOutcome,
    CheckResult,
    CleanupStatus,
    EvidenceRecord,
    EvidenceSource,
    ExceptionRecord,
    ExceptionType,
    FailureClassification,
    RecoveryResult,
    RehearsalMode,
)
from app.services.policy import APPROVED_SCENARIOS

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
HEAD = "a" * 40
OLD_COMMIT = "b" * 40


def make_exception(**overrides) -> ExceptionRecord:
    fields = dict(
        id="EXC-001",
        project="demo/payments",
        type=ExceptionType.SKIPPED_INTEGRATION_TEST,
        title="Skip flaky migration integration test",
        reason="Migration test blocked the 2.3 release",
        owner="alice",
        expires_at=NOW + timedelta(days=30),
        affected_check="integration:test_migration",
        remediation_target="Fix migration 0042 and re-enable the test",
        created_at=NOW - timedelta(days=10),
        created_by="user:alice",
        updated_at=NOW - timedelta(days=10),
        updated_by="user:alice",
    )
    fields.update(overrides)
    return ExceptionRecord(**fields)


def make_evidence(exc: ExceptionRecord, scenario_id: str = "db-migration-recovery", **overrides) -> EvidenceRecord:
    scenario = APPROVED_SCENARIOS.get(scenario_id)
    checks = list(scenario.required_checks) if scenario else []
    checks.append(exc.affected_check)
    mode = scenario.mode if scenario else RehearsalMode.SANDBOX
    fields = dict(
        id=f"EVD-{scenario_id}-1",
        run_id=f"RUN-{scenario_id}-1",
        exception_id=exc.id,
        scenario_id=scenario_id,
        mode=mode,
        source=EvidenceSource.GITLAB_CI if mode == RehearsalMode.PIPELINE else EvidenceSource.LOCAL_SANDBOX,
        commit_sha=HEAD,
        pipeline_id=101 if mode == RehearsalMode.PIPELINE else None,
        job_id=202 if mode == RehearsalMode.PIPELINE else None,
        started_at=NOW - timedelta(hours=1, minutes=5),
        finished_at=NOW - timedelta(hours=1),
        injected_failure_classification=FailureClassification.EXPECTED_INJECTED,
        check_results=[CheckResult(check_id=c, outcome=CheckOutcome.PASSED) for c in checks],
        recovery=RecoveryResult(
            attempted=True,
            succeeded=True,
            assertions=[CheckResult(check_id="row_count_preserved", outcome=CheckOutcome.PASSED)],
        ),
        cleanup_status=CleanupStatus.VERIFIED,
    )
    fields.update(overrides)
    return EvidenceRecord.seal(**fields)


def with_check(evidence: EvidenceRecord, check_id: str, outcome: CheckOutcome) -> EvidenceRecord:
    results = [r for r in evidence.check_results if r.check_id != check_id]
    results.append(CheckResult(check_id=check_id, outcome=outcome))
    fields = evidence.model_dump(exclude={"digest"})
    fields["check_results"] = results
    return EvidenceRecord.seal(**fields)
