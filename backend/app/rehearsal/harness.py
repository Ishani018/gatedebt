"""Shared rehearsal harness.

Runs an approved scenario in a fresh temporary directory, always attempts and
verifies cleanup, and turns what actually happened into a sealed
``EvidenceRecord``. The verdict comes from the policy engine's own
``rehearsal_failures`` so the harness can never be more lenient than policy.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.models import (
    ArtifactRef,
    CheckOutcome,
    CheckResult,
    CleanupStatus,
    EvidenceRecord,
    EvidenceSource,
    FailureClassification,
    RecoveryResult,
    utcnow,
)
from app.services.policy import ScenarioRequirement, rehearsal_failures

REPO_ROOT = Path(__file__).resolve().parents[3]
SCENARIOS_DIR = REPO_ROOT / "scenarios"
DEFAULT_ARTIFACTS_DIR = REPO_ROOT / "artifacts" / "runs"


@dataclass(frozen=True)
class RunContext:
    """Who/what the run is for. Built and validated by the CLI."""

    exception_id: str
    commit_sha: str
    # "provided" (--commit), "local_git_head" (git rev-parse HEAD) or "gitlab_ci".
    commit_origin: str
    source: EvidenceSource
    pipeline_id: int | None = None
    job_id: int | None = None
    working_tree_dirty: bool | None = None


class Recorder:
    """Collects what a scenario actually observed. Nothing passes by default."""

    def __init__(self) -> None:
        self.checks: list[CheckResult] = []
        self.recovery_assertions: list[CheckResult] = []
        self.classification: FailureClassification | None = None
        self.recovery_attempted = False
        self.recovery_succeeded = False
        self.lines: list[str] = []

    def log(self, message: str) -> None:
        self.lines.append(f"{utcnow().isoformat()} {message}")

    def check(self, check_id: str, passed: bool, detail: str = "") -> bool:
        outcome = CheckOutcome.PASSED if passed else CheckOutcome.FAILED
        self.checks.append(CheckResult(check_id=check_id, outcome=outcome, detail=detail[:2000]))
        self.log(f"check {check_id}: {outcome.value} {detail}".rstrip())
        return passed

    def assertion(self, check_id: str, passed: bool, detail: str = "") -> bool:
        outcome = CheckOutcome.PASSED if passed else CheckOutcome.FAILED
        self.recovery_assertions.append(CheckResult(check_id=check_id, outcome=outcome, detail=detail[:2000]))
        self.log(f"recovery assertion {check_id}: {outcome.value} {detail}".rstrip())
        return passed


class Scenario(ABC):
    """An approved rehearsal. Subclasses run real steps and record observations."""

    requirement: ScenarioRequirement
    # The waived check this scenario genuinely executes. Evidence only covers
    # an exception whose ``affected_check`` is this ID.
    exercised_check: str
    fixture_dir: Path

    def __init__(self, workdir: Path, recorder: Recorder) -> None:
        self.workdir = workdir
        self.rec = recorder
        self.fixture = json.loads((self.fixture_dir / "fixture.json").read_text())

    @abstractmethod
    def execute(self) -> None:
        """Inject, detect, classify, recover and verify. May raise."""

    def release(self) -> None:
        """Close any resources the scenario opened."""

    def leaked_resources(self) -> list[str]:
        """Describe resources still open after ``release``."""
        return []

    @classmethod
    def checks_required(cls) -> tuple[str, ...]:
        return (*cls.requirement.required_checks, cls.exercised_check)


class RehearsalReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_version: Literal[1] = 1
    verdict: Literal["passed", "failed"]
    failures: list[str]
    exercised_check: str
    commit_origin: str
    working_tree_dirty: bool | None
    execution_note: str
    evidence: EvidenceRecord
    log: list[str]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cleanup(scenario: Scenario | None, workdir: Path, rec: Recorder) -> CleanupStatus:
    problems: list[str] = []
    if scenario is not None:
        try:
            scenario.release()
        except Exception as exc:  # noqa: BLE001 - recorded, then verified below
            problems.append(f"release failed: {type(exc).__name__}: {exc}")
    try:
        shutil.rmtree(workdir)
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        problems.append(f"remove failed: {type(exc).__name__}: {exc}")
    # Verify rather than trust the calls above.
    if workdir.exists():
        problems.append(f"temporary directory still exists: {workdir}")
    if scenario is not None:
        problems.extend(f"leaked: {r}" for r in scenario.leaked_resources())
    for problem in problems:
        rec.log(f"cleanup: {problem}")
    if problems:
        return CleanupStatus.FAILED
    rec.log("cleanup: verified (resources closed, temporary directory removed)")
    return CleanupStatus.VERIFIED


def execution_note(ctx: RunContext) -> str:
    if ctx.source == EvidenceSource.GITLAB_CI:
        return f"GitLab CI execution (pipeline {ctx.pipeline_id}, job {ctx.job_id})."
    note = "Local sandbox execution on this machine. This is not a GitLab CI run."
    if ctx.commit_origin == "local_git_head":
        note += " Commit is the local git HEAD"
        if ctx.working_tree_dirty:
            note += " and the working tree had uncommitted changes, so the code that ran may differ from it"
        note += "."
    return note


def run_rehearsal(
    scenario_cls: type[Scenario],
    ctx: RunContext,
    artifacts_dir: Path = DEFAULT_ARTIFACTS_DIR,
) -> tuple[RehearsalReport, Path]:
    """Execute one rehearsal and write its report. Returns (report, report path)."""
    suffix = f"{utcnow():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    run_id = f"RUN-{suffix}"
    rec = Recorder()
    started_at = utcnow()
    workdir = Path(tempfile.mkdtemp(prefix=f"gatedebt-{scenario_cls.requirement.scenario_id}-"))
    rec.log(f"run {run_id}: scenario {scenario_cls.requirement.scenario_id} in {workdir}")

    scenario: Scenario | None = None
    try:
        scenario = scenario_cls(workdir, rec)
        scenario.execute()
        if rec.classification is None:
            rec.log("scenario finished without classifying the injected failure")
            rec.classification = FailureClassification.UNEXPECTED_INFRASTRUCTURE
    except Exception as exc:  # noqa: BLE001 - any unplanned error is an infra failure
        rec.log(f"unexpected error: {type(exc).__name__}: {exc}")
        rec.classification = FailureClassification.UNEXPECTED_INFRASTRUCTURE
        if rec.recovery_attempted:
            rec.recovery_succeeded = False
    finally:
        cleanup_status = _cleanup(scenario, workdir, rec)
    finished_at = utcnow()

    run_dir = artifacts_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    log_path = run_dir / "rehearsal.log"
    log_path.write_text("\n".join(rec.lines) + "\n")

    recovery = None
    if rec.recovery_attempted:
        recovery = RecoveryResult(
            attempted=True,
            succeeded=rec.recovery_succeeded,
            assertions=rec.recovery_assertions,
        )
    evidence = EvidenceRecord.seal(
        id=f"EVD-{suffix}",
        run_id=run_id,
        exception_id=ctx.exception_id,
        scenario_id=scenario_cls.requirement.scenario_id,
        mode=scenario_cls.requirement.mode,
        source=ctx.source,
        commit_sha=ctx.commit_sha,
        pipeline_id=ctx.pipeline_id,
        job_id=ctx.job_id,
        started_at=started_at,
        finished_at=finished_at,
        injected_failure_classification=rec.classification,
        check_results=rec.checks,
        recovery=recovery,
        cleanup_status=cleanup_status,
        artifacts=[ArtifactRef(name="rehearsal.log", location=f"{run_id}/rehearsal.log", sha256=_sha256(log_path))],
    )
    failures = rehearsal_failures(evidence, scenario_cls.checks_required())
    report = RehearsalReport(
        verdict="failed" if failures else "passed",
        failures=failures,
        exercised_check=scenario_cls.exercised_check,
        commit_origin=ctx.commit_origin,
        working_tree_dirty=ctx.working_tree_dirty,
        execution_note=execution_note(ctx),
        evidence=evidence,
        log=rec.lines,
    )
    report_path = run_dir / "report.json"
    report_path.write_text(report.model_dump_json(indent=2) + "\n")
    return report, report_path
