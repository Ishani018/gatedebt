"""Rehearsal engine tests. Every run here genuinely executes the scenario in a
temporary directory; failures are induced by editing copies of the fixtures."""

import json
import shutil
import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

from app.models import (
    CheckOutcome,
    CleanupStatus,
    EvidenceRecord,
    EvidenceSource,
    ExceptionStatus,
    ExceptionType,
    FailureClassification,
    Recommendation,
    RehearsalMode,
    RetirementVerification,
)
from app.rehearsal import SCENARIOS, DbMigrationRecovery, PipelineGateRecovery, RehearsalReport, RunContext, run_rehearsal
from app.rehearsal import cli
from app.services.decision import evaluate
from app.services.policy import APPROVED_SCENARIOS, evidence_rejections, rehearsal_failures, retirement_issues

from .factories import make_exception

COMMIT = "c" * 40
OTHER_COMMIT = "d" * 40
CTX = RunContext("EXC-001", COMMIT, "provided", EvidenceSource.LOCAL_SANDBOX)


def run(cls, tmp_path, ctx=CTX):
    report, path = run_rehearsal(cls, ctx, tmp_path / "artifacts")
    return report, path


def variant(base, tmp_path, edit, **attrs):
    """A scenario subclass running against an edited copy of its fixtures."""
    fixture_dir = tmp_path / f"fixture-{base.__name__}"
    shutil.copytree(base.fixture_dir, fixture_dir)
    edit(fixture_dir)
    return type(f"Variant{base.__name__}", (base,), {"fixture_dir": fixture_dir, **attrs})


def checks(report):
    return {c.check_id: c.outcome for c in report.evidence.check_results}


@pytest.fixture
def workdirs(monkeypatch):
    created = []
    real = tempfile.mkdtemp

    def tracking(*args, **kwargs):
        path = real(*args, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", tracking)
    yield created
    for path in created:
        shutil.rmtree(path, ignore_errors=True)


# ------------------------------------------------------------- registry (12)

def test_scenarios_match_approved_registry():
    assert set(SCENARIOS) == set(APPROVED_SCENARIOS)
    for scenario_id, cls in SCENARIOS.items():
        assert cls.requirement is APPROVED_SCENARIOS[scenario_id]
    assert SCENARIOS["db-migration-recovery"].requirement.mode == RehearsalMode.SANDBOX
    assert SCENARIOS["pipeline-gate-recovery"].requirement.mode == RehearsalMode.PIPELINE


# ------------------------------------------------ passing runs (1, 2, 3, 4)

@pytest.mark.parametrize("cls", [DbMigrationRecovery, PipelineGateRecovery])
def test_scenario_passes_end_to_end(cls, tmp_path, workdirs):
    report, _ = run(cls, tmp_path)
    evidence = report.evidence
    assert report.verdict == "passed", report.failures
    assert evidence.injected_failure_classification == FailureClassification.EXPECTED_INJECTED
    assert checks(report) == {c: CheckOutcome.PASSED for c in cls.checks_required()}
    assert evidence.recovery.attempted and evidence.recovery.succeeded
    assert evidence.cleanup_status == CleanupStatus.VERIFIED
    assert workdirs and not any(p.exists() for p in workdirs)


def test_db_injected_failure_is_real_and_partial(tmp_path):
    report, _ = run(DbMigrationRecovery, tmp_path)
    detail = next(c.detail for c in report.evidence.check_results if c.check_id == "injected_failure_detected")
    assert "Cannot add a NOT NULL column" in detail
    assert "partial state observed" in detail


def test_db_recovery_runs_every_assertion(tmp_path):
    report, _ = run(DbMigrationRecovery, tmp_path)
    assertions = {a.check_id: a.outcome for a in report.evidence.recovery.assertions}
    assert assertions == {
        "pre_migration_backup_restored": CheckOutcome.PASSED,
        "fixed_migration_committed": CheckOutcome.PASSED,
    }


def test_pipeline_recovery_runs_every_assertion(tmp_path):
    report, _ = run(PipelineGateRecovery, tmp_path)
    assertions = {a.check_id: a.outcome for a in report.evidence.recovery.assertions}
    assert assertions == {"manifest_regenerated": CheckOutcome.PASSED, "retry_within_limit": CheckOutcome.PASSED}
    detail = next(c.detail for c in report.evidence.check_results if c.check_id == "injected_failure_detected")
    assert detail.startswith("exit 3, LOCK_DIGEST_MISMATCH")


def test_schema_drift_is_detected(tmp_path):
    def edit(d):
        sql = (d / "migration_0042_fixed.sql").read_text()
        (d / "migration_0042_fixed.sql").write_text(sql.replace("DEFAULT 'standard'", "DEFAULT 'basic'"))

    report, _ = run(variant(DbMigrationRecovery, tmp_path, edit), tmp_path)
    outcomes = checks(report)
    assert outcomes["post_recovery_schema_valid"] == CheckOutcome.FAILED
    assert outcomes["post_recovery_data_intact"] == CheckOutcome.FAILED  # tier values wrong too
    assert report.verdict == "failed"


# ---------------------------------------------------- failing required check (8)

def test_data_loss_in_recovery_fails_data_check(tmp_path):
    def edit(d):
        sql = (d / "migration_0042_fixed.sql").read_text()
        (d / "migration_0042_fixed.sql").write_text(sql + "\nDELETE FROM invoices WHERE id = 6;\n")

    report, _ = run(variant(DbMigrationRecovery, tmp_path, edit), tmp_path)
    assert checks(report)["post_recovery_data_intact"] == CheckOutcome.FAILED
    assert "CHECK_FAILED:post_recovery_data_intact" in report.failures
    assert report.verdict == "failed"


def test_missing_required_check_fails(tmp_path):
    class SkipsIntegrationTest(DbMigrationRecovery):
        def _run_waived_integration_test(self):
            pass

    report, _ = run(SkipsIntegrationTest, tmp_path)
    assert "CHECK_MISSING:integration:migration_0042" in report.failures
    assert report.verdict == "failed"


def test_failed_retry_fails_pipeline(tmp_path):
    def edit(d):
        fx = json.loads((d / "fixture.json").read_text())
        fx["max_retries"] = 0
        (d / "fixture.json").write_text(json.dumps(fx))

    report, _ = run(variant(PipelineGateRecovery, tmp_path, edit), tmp_path)
    assert report.evidence.recovery.succeeded is False
    assert {"RECOVERY_FAILED", "CHECK_FAILED:retry_workflow_passed"} <= set(report.failures)


# ------------------------------------------------------- missing recovery (7)

def test_db_failure_not_injected_means_no_recovery(tmp_path):
    def edit(d):
        shutil.copyfile(d / "migration_0042_fixed.sql", d / "migration_0042_broken.sql")

    report, _ = run(variant(DbMigrationRecovery, tmp_path, edit), tmp_path)
    assert report.evidence.injected_failure_classification == FailureClassification.INJECTED_NOT_DETECTED
    assert report.evidence.recovery is None
    assert {"INJECTED_FAILURE_NOT_DETECTED", "RECOVERY_MISSING"} <= set(report.failures)
    assert report.verdict == "failed"


def test_pipeline_blind_gate_is_not_a_pass(tmp_path):
    def edit(d):
        fx = json.loads((d / "fixture.json").read_text())
        fx["stale_lock_content"] = (d / "requirements.lock").read_text()  # "stale" digest is actually correct
        (d / "fixture.json").write_text(json.dumps(fx))

    report, _ = run(variant(PipelineGateRecovery, tmp_path, edit), tmp_path)
    assert report.evidence.injected_failure_classification == FailureClassification.INJECTED_NOT_DETECTED
    assert report.evidence.recovery is None
    assert "RECOVERY_MISSING" in report.failures
    assert checks(report)["injected_failure_detected"] == CheckOutcome.FAILED


# ------------------------------------------------- unexpected infrastructure (6)

def test_unexpected_exception_is_infrastructure_failure(tmp_path, workdirs):
    class Crashes(DbMigrationRecovery):
        def _seed(self, conn):
            raise RuntimeError("disk vanished")

    report, _ = run(Crashes, tmp_path)
    assert report.evidence.injected_failure_classification == FailureClassification.UNEXPECTED_INFRASTRUCTURE
    assert report.verdict == "failed"
    assert "UNEXPECTED_INFRASTRUCTURE_FAILURE" in report.failures
    assert any("disk vanished" in line for line in report.log)
    # Cleanup still happened and was verified.
    assert report.evidence.cleanup_status == CleanupStatus.VERIFIED
    assert not any(p.exists() for p in workdirs)


def test_crash_during_recovery_marks_recovery_failed(tmp_path):
    class CrashesMidRecovery(DbMigrationRecovery):
        def _verify_schema(self, db):
            raise sqlite3.DatabaseError("database disk image is malformed")

    report, _ = run(CrashesMidRecovery, tmp_path)
    assert report.evidence.injected_failure_classification == FailureClassification.UNEXPECTED_INFRASTRUCTURE
    assert report.evidence.recovery.succeeded is False
    assert "CHECK_MISSING:post_recovery_schema_valid" in report.failures


def test_wrong_db_error_is_not_the_injected_failure(tmp_path):
    def edit(d):
        sql = (d / "migration_0042_broken.sql").read_text()
        (d / "migration_0042_broken.sql").write_text(sql.replace("ALTER TABLE customers", "ALTER TABLE no_such_table"))

    report, _ = run(variant(DbMigrationRecovery, tmp_path, edit), tmp_path)
    assert report.evidence.injected_failure_classification == FailureClassification.UNEXPECTED_INFRASTRUCTURE
    assert report.verdict == "failed"


def test_pipeline_gate_crash_is_infrastructure_failure(tmp_path):
    def edit(d):
        (d / "gate.py").write_text("raise SystemExit(1)\n")  # crashes like a broken runner

    report, _ = run(variant(PipelineGateRecovery, tmp_path, edit), tmp_path)
    assert report.evidence.injected_failure_classification == FailureClassification.UNEXPECTED_INFRASTRUCTURE
    assert checks(report)["failure_classified_expected"] == CheckOutcome.FAILED
    assert report.evidence.recovery is None
    assert report.verdict == "failed"


def test_pipeline_gate_timeout_is_infrastructure_failure(tmp_path):
    def edit(d):
        (d / "gate.py").write_text("import time\ntime.sleep(30)\n")
        fx = json.loads((d / "fixture.json").read_text())
        fx["timeout_seconds"] = 1
        (d / "fixture.json").write_text(json.dumps(fx))

    report, _ = run(variant(PipelineGateRecovery, tmp_path, edit), tmp_path)
    assert report.evidence.injected_failure_classification == FailureClassification.UNEXPECTED_INFRASTRUCTURE
    assert "timed out" in next(c.detail for c in report.evidence.check_results if c.check_id == "injected_failure_detected")


# ------------------------------------------------------------- cleanup (4, 5)

def test_cleanup_failure_prevents_pass(tmp_path, monkeypatch, workdirs):
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)
    report, _ = run(DbMigrationRecovery, tmp_path)
    monkeypatch.undo()
    assert report.evidence.cleanup_status == CleanupStatus.FAILED
    assert "CLEANUP_FAILED" in report.failures
    assert report.verdict == "failed"
    assert any("temporary directory still exists" in line for line in report.log)


def test_leaked_connection_prevents_pass(tmp_path):
    class LeaksConnection(DbMigrationRecovery):
        def release(self):
            for conn in self._connections[1:]:
                conn.close()

    report, _ = run(LeaksConnection, tmp_path)
    assert report.evidence.cleanup_status == CleanupStatus.FAILED
    assert any("open sqlite connection" in line for line in report.log)
    assert report.verdict == "failed"


# --------------------------------------------- evidence & serialisation (9, 10)

@pytest.mark.parametrize("cls", [DbMigrationRecovery, PipelineGateRecovery])
def test_report_serialises_and_evidence_validates(cls, tmp_path):
    report, path = run(cls, tmp_path)
    raw = json.loads(path.read_text())
    assert raw["verdict"] == "passed"
    loaded = RehearsalReport.model_validate_json(path.read_text())
    assert loaded == report
    evidence = EvidenceRecord.model_validate(raw["evidence"])
    assert evidence.digest_matches()
    assert evidence.run_id == path.parent.name
    assert evidence.started_at <= evidence.finished_at
    assert evidence.started_at.utcoffset() == timedelta(0)
    assert evidence.pipeline_id is None and evidence.job_id is None
    log_ref = evidence.artifacts[0]
    assert (path.parent.parent / log_ref.location).exists()
    assert "not a GitLab CI run" in raw["execution_note"]


def test_failed_report_is_still_written(tmp_path):
    class Crashes(DbMigrationRecovery):
        def execute(self):
            raise OSError("no space left on device")

    report, path = run(Crashes, tmp_path)
    assert path.exists()
    assert json.loads(path.read_text())["verdict"] == "failed"
    assert report.evidence.digest_matches()


# ------------------------------------------------------------ policy link (14)

def _exc_for(scenario_cls, **overrides):
    exc_type = {
        "db-migration-recovery": ExceptionType.SKIPPED_INTEGRATION_TEST,
        "pipeline-gate-recovery": ExceptionType.WAIVED_QUALITY_GATE,
    }[scenario_cls.requirement.scenario_id]
    return make_exception(type=exc_type, affected_check=scenario_cls.exercised_check, **overrides)


@pytest.mark.parametrize("cls", [DbMigrationRecovery, PipelineGateRecovery])
def test_real_evidence_accepted_by_policy_and_proposes_retirement_only(cls, tmp_path):
    report, _ = run(cls, tmp_path)
    exc = _exc_for(cls)
    now = report.evidence.finished_at
    assert evidence_rejections(report.evidence, exc, COMMIT, now) == []
    assert rehearsal_failures(report.evidence, cls.checks_required()) == []
    decision = evaluate(exc, [report.evidence], COMMIT, now)
    assert decision.recommendation == Recommendation.PROPOSE_RETIREMENT
    assert decision.requires_human_approval
    assert exc.status == ExceptionStatus.ACTIVE  # nothing retired automatically


def test_real_evidence_rejected_for_newer_commit(tmp_path):
    report, _ = run(DbMigrationRecovery, tmp_path)
    exc = _exc_for(DbMigrationRecovery)
    decision = evaluate(exc, [report.evidence], OTHER_COMMIT, report.evidence.finished_at)
    assert decision.recommendation != Recommendation.PROPOSE_RETIREMENT
    assert decision.rejected_evidence[report.evidence.id] == ["EVIDENCE_STALE_COMMIT"]


def test_real_evidence_does_not_cover_a_different_waived_check(tmp_path):
    report, _ = run(DbMigrationRecovery, tmp_path)
    exc = make_exception(affected_check="integration:something_else")
    decision = evaluate(exc, [report.evidence], COMMIT, report.evidence.finished_at)
    assert decision.recommendation == Recommendation.REMEDIATE
    assert "db-migration-recovery:integration:something_else" in decision.missing_checks


def test_failing_real_evidence_blocks_retirement(tmp_path):
    class SkipsIntegrationTest(DbMigrationRecovery):
        def _run_waived_integration_test(self):
            pass

    report, _ = run(SkipsIntegrationTest, tmp_path)
    decision = evaluate(_exc_for(DbMigrationRecovery), [report.evidence], COMMIT, report.evidence.finished_at)
    assert decision.recommendation != Recommendation.PROPOSE_RETIREMENT


def test_passing_rehearsal_alone_cannot_retire(tmp_path):
    report, _ = run(DbMigrationRecovery, tmp_path)
    exc = _exc_for(DbMigrationRecovery)
    verification = RetirementVerification(
        exception_id=exc.id,
        merged_commit_sha=COMMIT,
        change_merged=False,
        waiver_still_present=True,
        pipeline_status="unknown",
        verified_at=report.evidence.finished_at,
        observed_by="system:rehearsal",
    )
    issues = retirement_issues(exc, None, verification)
    assert {"RETIREMENT_NOT_APPROVED", "APPROVAL_MISSING", "CHANGE_NOT_MERGED", "WAIVER_STILL_PRESENT"} <= set(issues)


def test_release_readiness_needs_both_real_runs(tmp_path):
    db, _ = run(DbMigrationRecovery, tmp_path)
    exc = make_exception(type=ExceptionType.WAIVED_RELEASE_READINESS_CHECK, affected_check=DbMigrationRecovery.exercised_check)
    decision = evaluate(exc, [db.evidence], COMMIT, db.evidence.finished_at)
    assert "EVIDENCE_MISSING:pipeline-gate-recovery" in decision.reason_codes
    assert decision.recommendation != Recommendation.PROPOSE_RETIREMENT


# ------------------------------------------------------------------- CLI (11)

def test_cli_exit_code_success(tmp_path, capsys):
    code = cli.main(["run", "db-migration-recovery", "--exception-id", "EXC-001", "--commit", COMMIT,
                     "--artifacts-dir", str(tmp_path)])
    assert code == cli.EXIT_PASSED
    assert "verdict:        PASSED" in capsys.readouterr().out


def test_cli_exit_code_failure(tmp_path, monkeypatch, capsys):
    class Crashes(PipelineGateRecovery):
        def execute(self):
            raise RuntimeError("runner lost")

    monkeypatch.setitem(SCENARIOS, "pipeline-gate-recovery", Crashes)
    code = cli.main(["run", "pipeline-gate-recovery", "--exception-id", "EXC-001", "--commit", COMMIT,
                     "--artifacts-dir", str(tmp_path)])
    assert code == cli.EXIT_FAILED
    assert "verdict:        FAILED" in capsys.readouterr().out
    assert len(list(tmp_path.glob("RUN-*/report.json"))) == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "db-migration-recovery", "--exception-id", "exc-1", "--commit", COMMIT],
        ["run", "db-migration-recovery", "--exception-id", "EXC-001", "--commit", "abc123"],
        ["run", "db-migration-recovery", "--exception-id", "EXC-001", "--commit", COMMIT.upper()],
        ["run", "rm-rf", "--exception-id", "EXC-001", "--commit", COMMIT],
        ["run", "db-migration-recovery", "--exception-id", "EXC-001", "--source", "test_fixture"],
        ["run", "db-migration-recovery", "--exception-id", "EXC-001", "--source", "gitlab_ci"],
        ["run", "db-migration-recovery"],
    ],
)
def test_cli_rejects_invalid_input(argv, tmp_path, capsys):
    code = cli.main([*argv, "--artifacts-dir", str(tmp_path)], env={})
    assert code == cli.EXIT_USAGE
    assert not list(tmp_path.glob("RUN-*"))  # nothing ran


def test_local_mode_without_commit_fails_closed(monkeypatch):
    monkeypatch.setattr(cli, "_git", lambda *a: None)
    with pytest.raises(cli.UsageError, match="pass --commit"):
        cli.build_context("EXC-001", None, "local_sandbox", env={})


def test_local_mode_uses_git_head_and_reports_dirty_tree(monkeypatch):
    outputs = {("rev-parse", "HEAD"): COMMIT + "\n", ("status", "--porcelain"): " M file.py\n"}
    monkeypatch.setattr(cli, "_git", lambda *a: outputs[a])
    ctx = cli.build_context("EXC-001", None, "local_sandbox", env={})
    assert (ctx.commit_sha, ctx.commit_origin, ctx.working_tree_dirty) == (COMMIT, "local_git_head", True)
    assert ctx.source == EvidenceSource.LOCAL_SANDBOX


CI_ENV = {"GITLAB_CI": "true", "CI_COMMIT_SHA": COMMIT, "CI_PIPELINE_ID": "4242", "CI_JOB_ID": "777"}


def test_gitlab_ci_context_comes_from_ci_environment():
    ctx = cli.build_context("EXC-001", None, "gitlab_ci", env=CI_ENV)
    assert (ctx.source, ctx.commit_sha, ctx.pipeline_id, ctx.job_id) == (EvidenceSource.GITLAB_CI, COMMIT, 4242, 777)


@pytest.mark.parametrize(
    "env, commit",
    [
        ({**CI_ENV, "GITLAB_CI": ""}, None),
        ({**CI_ENV, "CI_JOB_ID": ""}, None),
        ({**CI_ENV, "CI_PIPELINE_ID": "-1"}, None),
        (CI_ENV, OTHER_COMMIT),
    ],
)
def test_gitlab_ci_context_rejects_incomplete_or_mismatched_env(env, commit):
    with pytest.raises(cli.UsageError):
        cli.build_context("EXC-001", commit, "gitlab_ci", env=env)


def test_gitlab_ci_evidence_carries_pipeline_references(tmp_path):
    ctx = cli.build_context("EXC-001", None, "gitlab_ci", env=CI_ENV)
    report, _ = run(PipelineGateRecovery, tmp_path, ctx)
    assert (report.evidence.pipeline_id, report.evidence.job_id) == (4242, 777)
    assert report.execution_note.startswith("GitLab CI execution")
    exc = _exc_for(PipelineGateRecovery)
    assert evidence_rejections(report.evidence, exc, COMMIT, report.evidence.finished_at) == []
