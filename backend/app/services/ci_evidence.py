"""Server-side verification of rehearsal evidence produced by GitLab CI.

A caller only says *where* to look (project, pipeline, scenario). Everything
else - pipeline and job status, commit, ref, job identity and the report
itself - is fetched from the GitLab API with the server's own token and
cross-checked. Nothing the caller or the report claims is trusted on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from app.models import EvidenceRecord, EvidenceSource, ExceptionRecord
from app.rehearsal import SCENARIOS, RehearsalReport
from app.services.policy import PolicyConfig, evidence_rejections, rehearsal_failures

from app.integrations.gitlab import GitLabClient, GitLabError

EVIDENCE_ARTIFACT_DIR = "gatedebt-evidence"


def ci_job_name(scenario_id: str) -> str:
    """The .gitlab-ci.yml job that runs a scenario. Must match the YAML."""
    return f"rehearse:{scenario_id}"


def evidence_artifact_path(scenario_id: str) -> str:
    return f"{EVIDENCE_ARTIFACT_DIR}/{scenario_id}.json"


class CiEvidenceRejected(Exception):
    def __init__(self, *reason_codes: str, unavailable: bool = False) -> None:
        self.reason_codes = list(reason_codes)
        self.unavailable = unavailable
        super().__init__(", ".join(self.reason_codes))


@dataclass(frozen=True)
class CiProvenance:
    project_id: int
    pipeline_id: int
    job_id: int
    job_name: str
    ref: str
    commit_sha: str
    pipeline_web_url: str | None
    job_web_url: str | None


@dataclass(frozen=True)
class VerifiedCiEvidence:
    evidence: EvidenceRecord
    report: RehearsalReport
    provenance: CiProvenance


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _call(fn, *args, not_found: str):
    try:
        result = fn(*args)
    except GitLabError as err:
        if err.status == 404:
            raise CiEvidenceRejected(not_found) from None
        raise CiEvidenceRejected("CI_VERIFICATION_UNAVAILABLE", unavailable=True) from None
    return result


def verify_ci_evidence(
    client: GitLabClient,
    *,
    project_id: int,
    pipeline_id: int,
    scenario_id: str,
    exception: ExceptionRecord,
    trusted_projects: frozenset[int],
    trusted_refs: frozenset[str],
    policy: PolicyConfig,
    now: datetime,
    job_id: int | None = None,
    expected_commit: str | None = None,
) -> VerifiedCiEvidence:
    """Return verified evidence or raise ``CiEvidenceRejected``. Fails closed."""
    scenario = SCENARIOS.get(scenario_id)
    if scenario is None:
        raise CiEvidenceRejected("UNKNOWN_SCENARIO")
    if project_id not in trusted_projects:
        raise CiEvidenceRejected("CI_PROJECT_NOT_TRUSTED")

    # 1. Pipeline: right project, finished successfully, on a trusted ref.
    pipeline = _call(client.get_pipeline, project_id, pipeline_id, not_found="CI_PIPELINE_NOT_FOUND")
    if not isinstance(pipeline, dict) or pipeline.get("id") != pipeline_id or pipeline.get("project_id") != project_id:
        raise CiEvidenceRejected("CI_PIPELINE_PROJECT_MISMATCH")
    if pipeline.get("status") != "success":
        raise CiEvidenceRejected("CI_PIPELINE_NOT_SUCCESSFUL")
    sha, ref = pipeline.get("sha"), pipeline.get("ref")
    if not isinstance(sha, str) or len(sha) != 40:
        raise CiEvidenceRejected("CI_PIPELINE_COMMIT_INVALID")
    if ref not in trusted_refs or pipeline.get("tag") is True:
        raise CiEvidenceRejected("CI_REF_NOT_TRUSTED")
    if expected_commit is not None and expected_commit != sha:
        raise CiEvidenceRejected("CI_COMMIT_MISMATCH")

    # 2. Job: the scenario's job in *this* pipeline, latest attempt, succeeded.
    expected_name = ci_job_name(scenario_id)
    jobs = _call(client.list_pipeline_jobs, project_id, pipeline_id, not_found="CI_PIPELINE_NOT_FOUND")
    named = [j for j in jobs if isinstance(j, dict) and j.get("name") == expected_name] if isinstance(jobs, list) else []
    if not named:
        raise CiEvidenceRejected("CI_JOB_NOT_FOUND")
    latest = max(named, key=lambda j: j.get("id") if isinstance(j.get("id"), int) else 0)
    if not isinstance(latest.get("id"), int):
        raise CiEvidenceRejected("CI_JOB_MISMATCH")
    if job_id is not None and job_id != latest.get("id"):
        raise CiEvidenceRejected("CI_JOB_MISMATCH")
    job = _call(client.get_job, project_id, latest["id"], not_found="CI_JOB_NOT_FOUND")
    if not isinstance(job, dict) or not isinstance(job.get("pipeline") or {}, dict):
        raise CiEvidenceRejected("CI_JOB_MISMATCH")
    job_pipeline = job.get("pipeline") or {}
    if (
        job.get("id") != latest["id"]
        or job.get("name") != expected_name
        or job_pipeline.get("id") != pipeline_id
        or job_pipeline.get("project_id") not in (None, project_id)
    ):
        raise CiEvidenceRejected("CI_JOB_MISMATCH")
    if job.get("status") != "success":
        raise CiEvidenceRejected("CI_JOB_NOT_SUCCESSFUL")
    if (job.get("commit") or {}).get("id") != sha or job_pipeline.get("sha", sha) != sha:
        raise CiEvidenceRejected("CI_JOB_COMMIT_MISMATCH")
    job_started, job_finished = _ts(job.get("started_at")), _ts(job.get("finished_at"))
    if job_started is None or job_finished is None:
        raise CiEvidenceRejected("CI_JOB_TIMESTAMPS_MISSING")

    # 3. The report, downloaded from that job's artifacts by the server.
    raw = _call(client.get_job_artifact, project_id, job["id"], evidence_artifact_path(scenario_id),
                not_found="CI_ARTIFACT_MISSING")
    try:
        report = RehearsalReport.model_validate_json(raw)
    except (ValidationError, ValueError):
        raise CiEvidenceRejected("CI_ARTIFACT_MALFORMED") from None
    evidence = report.evidence

    reasons: list[str] = []
    if evidence.source != EvidenceSource.GITLAB_CI or report.commit_origin != "gitlab_ci":
        reasons.append("CI_REPORT_SOURCE_MISMATCH")
    if evidence.pipeline_id != pipeline_id:
        reasons.append("CI_REPORT_PIPELINE_MISMATCH")
    if evidence.job_id != job["id"]:
        reasons.append("CI_REPORT_JOB_MISMATCH")
    if evidence.commit_sha != sha:
        reasons.append("CI_REPORT_COMMIT_MISMATCH")
    if evidence.scenario_id != scenario_id or evidence.mode != scenario.requirement.mode:
        reasons.append("CI_REPORT_SCENARIO_MISMATCH")
    if evidence.exception_id != exception.id:
        reasons.append("CI_REPORT_EXCEPTION_MISMATCH")
    if report.ci is None or report.ci.project_id != project_id:
        reasons.append("CI_REPORT_PROJECT_MISMATCH")
    skew = policy.clock_skew
    if evidence.started_at < job_started - skew or evidence.finished_at > job_finished + skew:
        reasons.append("CI_REPORT_OUTSIDE_JOB_WINDOW")
    # Existing policy rules: digest, age, future-dating, trust, scenario fit.
    reasons.extend(evidence_rejections(evidence, exception, sha, now, policy))
    # The verdict is recomputed by policy; the report's own verdict is ignored.
    failures = rehearsal_failures(evidence, scenario.checks_required())
    if failures:
        reasons.append("CI_REHEARSAL_FAILED")
        reasons.extend(failures)
    if reasons:
        raise CiEvidenceRejected(*dict.fromkeys(reasons))

    return VerifiedCiEvidence(
        evidence=evidence,
        report=report,
        provenance=CiProvenance(
            project_id=project_id, pipeline_id=pipeline_id, job_id=job["id"], job_name=expected_name, ref=ref,
            commit_sha=sha, pipeline_web_url=pipeline.get("web_url"), job_web_url=job.get("web_url"),
        ),
    )
