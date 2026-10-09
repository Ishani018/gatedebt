"""GitLab CI evidence verification and ingestion.

Offline and deterministic: GitLab is replaced by ``FakeGitLab``, which serves
pipeline/job JSON shaped like the documented API responses. The reports it
serves are produced by actually running the rehearsal harness with a
simulated GitLab CI environment.
"""

import json
import urllib.request
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.auth import Identity
from app.config import AuthMode, ConfigError, Environment, EvidenceTrust, Settings
from app.integrations.gitlab import GitLabError, HttpGitLabClient, _NoTokenAcrossHosts
from app.models import EvidenceRecord, utcnow
from app.rehearsal import DbMigrationRecovery, PipelineGateRecovery, RehearsalReport, run_rehearsal
from app.rehearsal import cli
from app.rehearsal.summary import summarize, write_junit
from app.services.ci_evidence import ci_job_name, evidence_artifact_path

from .test_api import AGENT, ALICE, BOB, CI, SHA, approve, audit_actions, create, dev, make_settings, payload, status

PROJECT, PIPELINE, JOB = 31, 501, 9001
REPO_ROOT = Path(__file__).resolve().parents[2]


def ci_env(**overrides):
    env = {"GITLAB_CI": "true", "CI_COMMIT_SHA": SHA, "CI_PIPELINE_ID": str(PIPELINE), "CI_JOB_ID": str(JOB),
           "CI_PROJECT_ID": str(PROJECT), "CI_COMMIT_REF_NAME": "main",
           "CI_JOB_NAME": "rehearse:db-migration-recovery"}
    env.update({k: str(v) for k, v in overrides.items()})
    return env


def real_report(tmp_path, scenario=DbMigrationRecovery, exception_id="EXC-001", **env) -> RehearsalReport:
    ctx = cli.build_context(exception_id, None, "gitlab_ci", env=ci_env(**env))
    report, _ = run_rehearsal(scenario, ctx, tmp_path / "ci-artifacts")
    return report


def reseal(report: RehearsalReport, **evidence_changes) -> RehearsalReport:
    """A forger with write access to the report re-computing the digest."""
    fields = {**report.evidence.model_dump(exclude={"digest"}), **evidence_changes}
    return report.model_copy(update={"evidence": EvidenceRecord.seal(**fields)})


class FakeGitLab:
    def __init__(self, report: RehearsalReport | bytes | None, scenario_id="db-migration-recovery"):
        ev = report.evidence if isinstance(report, RehearsalReport) else None
        started = (ev.started_at if ev else utcnow()) - timedelta(seconds=5)
        finished = (ev.finished_at if ev else utcnow()) + timedelta(seconds=5)
        self.pipeline = {"id": PIPELINE, "project_id": PROJECT, "sha": SHA, "ref": "main", "status": "success",
                         "tag": False, "web_url": f"https://gitlab.example/p/-/pipelines/{PIPELINE}"}
        self.jobs = [
            {"id": JOB - 1, "name": "test", "status": "success"},
            {"id": JOB, "name": ci_job_name(scenario_id), "status": "success"},
        ]
        self.job = {"id": JOB, "name": ci_job_name(scenario_id), "status": "success",
                    "pipeline": {"id": PIPELINE, "project_id": PROJECT, "ref": "main", "sha": SHA,
                                 "status": "success"},
                    "commit": {"id": SHA}, "ref": "main", "started_at": started.isoformat(),
                    "finished_at": finished.isoformat(), "web_url": f"https://gitlab.example/p/-/jobs/{JOB}"}
        body = report.model_dump_json().encode() if isinstance(report, RehearsalReport) else report
        self.artifacts = {} if body is None else {(JOB, evidence_artifact_path(scenario_id)): body}
        self.error: GitLabError | None = None
        self.calls: list[tuple] = []

    def _maybe_fail(self):
        if self.error:
            raise self.error

    def get_pipeline(self, project_id, pipeline_id):
        self.calls.append(("pipeline", project_id, pipeline_id))
        self._maybe_fail()
        if pipeline_id != PIPELINE:
            raise GitLabError(404, "Not Found")
        return dict(self.pipeline)

    def list_pipeline_jobs(self, project_id, pipeline_id):
        self.calls.append(("jobs", project_id, pipeline_id))
        return [dict(j) for j in self.jobs]

    def get_job(self, project_id, job_id):
        self.calls.append(("job", project_id, job_id))
        if job_id != self.job["id"]:
            raise GitLabError(404, "Not Found")
        return json.loads(json.dumps(self.job))

    def get_job_artifact(self, project_id, job_id, path):
        self.calls.append(("artifact", project_id, job_id, path))
        if (job_id, path) not in self.artifacts:
            raise GitLabError(404, "Not Found")
        return self.artifacts[(job_id, path)]


@pytest.fixture(scope="module")
def module_tmp(tmp_path_factory):
    return tmp_path_factory.mktemp("ci")


@pytest.fixture(scope="module")
def good_report(module_tmp):
    report = real_report(module_tmp)
    assert report.verdict == "passed"
    return report


def app_with(tmp_path, fake, **overrides):
    settings = make_settings(tmp_path, gitlab_project_ids=frozenset({PROJECT}),
                             gitlab_client_factory=lambda: fake, **overrides)
    return TestClient(create_app(settings))


def ingest(client, who=ALICE, **body):
    return client.post("/exceptions/EXC-001/ci-evidence",
                       json={"pipeline_id": PIPELINE, "scenario_id": "db-migration-recovery", **body}, headers=who)


def reasons(response):
    return response.json()["detail"]["reason_codes"]


# --------------------------------------------------------------- valid path

def test_verified_ci_evidence_is_ingested(tmp_path, good_report):
    fake = FakeGitLab(good_report)
    client = app_with(tmp_path, fake)
    create(client)
    r = ingest(client)
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["commit_origin"] == "gitlab_ci"
    assert row["provenance_issues"] == []
    assert row["ci_verification"]["job_id"] == JOB
    assert row["ci_verification"]["ref"] == "main"
    assert row["evidence"]["source"] == "gitlab_ci"
    assert [c[0] for c in fake.calls] == ["pipeline", "jobs", "job", "artifact"]
    assert "evidence.ci_ingested" in audit_actions(client)
    # Evidence is a recommendation input only: nothing changes state.
    assert status(client) == "active"
    decision = client.get("/exceptions/EXC-001/decision", params={"commit": SHA}).json()
    assert decision["recommendation"] == "propose_retirement"
    assert decision["supporting_evidence_ids"] == [good_report.evidence.id]


def test_successful_pipeline_cannot_bypass_human_approval_or_verification(tmp_path, good_report):
    client = app_with(tmp_path, FakeGitLab(good_report), evidence_trust=EvidenceTrust.CI_ONLY)
    create(client)
    assert ingest(client).status_code == 201
    assert status(client) == "active"
    assert client.post("/exceptions/EXC-001/retirement-proposal", json={"commit_sha": SHA},
                       headers=CI).status_code == 201
    assert status(client) == "retirement_proposed"
    for who in (CI, AGENT):
        assert approve(client, who).status_code == 403
    assert approve(client, dev("user:mallory")).status_code == 403
    assert approve(client, BOB).status_code == 201
    assert status(client) == "retirement_approved"  # not retired without post-merge verification
    r = client.post("/exceptions/EXC-001/verifications", headers=BOB, json={
        "merged_commit_sha": "c" * 40, "change_merged": False, "waiver_still_present": True,
        "pipeline_status": "success"})
    assert r.json()["retired"] is False
    assert status(client) == "retirement_approved"


def test_pipeline_scenario_evidence(tmp_path, module_tmp):
    report = real_report(module_tmp, PipelineGateRecovery, exception_id="EXC-002",
                         CI_JOB_NAME="rehearse:pipeline-gate-recovery")
    client = app_with(tmp_path, FakeGitLab(report, "pipeline-gate-recovery"))
    create(client, id="EXC-002", type="waived_quality_gate", affected_check="quality-gate:dependency-lock")
    r = client.post("/exceptions/EXC-002/ci-evidence", headers=ALICE,
                    json={"pipeline_id": PIPELINE, "scenario_id": "pipeline-gate-recovery"})
    assert r.status_code == 201, r.text
    decision = client.get("/exceptions/EXC-002/decision", params={"commit": SHA}).json()
    assert decision["recommendation"] == "propose_retirement"


# ------------------------------------------------- pipeline / job mismatch

@pytest.mark.parametrize(
    "mutate, code, reason",
    [
        (lambda f: f.pipeline.update(status="failed"), 422, "CI_PIPELINE_NOT_SUCCESSFUL"),
        (lambda f: f.pipeline.update(status="running"), 422, "CI_PIPELINE_NOT_SUCCESSFUL"),
        (lambda f: f.pipeline.update(project_id=99), 422, "CI_PIPELINE_PROJECT_MISMATCH"),
        (lambda f: f.pipeline.update(ref="feature/evil"), 422, "CI_REF_NOT_TRUSTED"),
        (lambda f: f.pipeline.update(tag=True), 422, "CI_REF_NOT_TRUSTED"),
        (lambda f: f.pipeline.update(sha="d" * 40), 422, "CI_JOB_COMMIT_MISMATCH"),
        (lambda f: f.jobs.pop(), 422, "CI_JOB_NOT_FOUND"),
        (lambda f: f.job.update(status="failed"), 422, "CI_JOB_NOT_SUCCESSFUL"),
        (lambda f: f.job.update(name="test"), 422, "CI_JOB_MISMATCH"),
        (lambda f: f.job["pipeline"].update(id=PIPELINE + 1), 422, "CI_JOB_MISMATCH"),
        (lambda f: f.job["commit"].update(id="d" * 40), 422, "CI_JOB_COMMIT_MISMATCH"),
        (lambda f: f.job.update(finished_at=None), 422, "CI_JOB_TIMESTAMPS_MISSING"),
        (lambda f: f.artifacts.clear(), 422, "CI_ARTIFACT_MISSING"),
    ],
)
def test_pipeline_and_job_verification(tmp_path, good_report, mutate, code, reason):
    fake = FakeGitLab(good_report)
    mutate(fake)
    client = app_with(tmp_path, fake)
    create(client)
    r = ingest(client)
    assert r.status_code == code, r.text
    assert reason in reasons(r)
    assert client.get("/exceptions/EXC-001/evidence").json() == []
    assert audit_actions(client)[-1] == "evidence.ci_rejected"


def test_unknown_pipeline(tmp_path, good_report):
    client = app_with(tmp_path, FakeGitLab(good_report))
    create(client)
    r = ingest(client, pipeline_id=PIPELINE + 7)
    assert (r.status_code, reasons(r)) == (422, ["CI_PIPELINE_NOT_FOUND"])


def test_untrusted_project(tmp_path, good_report):
    fake = FakeGitLab(good_report)
    client = app_with(tmp_path, fake)
    create(client)
    r = ingest(client, project_id=99)
    assert (r.status_code, reasons(r)) == (403, ["CI_PROJECT_NOT_TRUSTED"])
    assert fake.calls == []  # never even asked GitLab


def test_job_id_must_be_the_scenario_job(tmp_path, good_report):
    client = app_with(tmp_path, FakeGitLab(good_report))
    create(client)
    r = ingest(client, job_id=JOB - 1)  # the "test" job in the same pipeline
    assert reasons(r) == ["CI_JOB_MISMATCH"]
    assert ingest(client, job_id=JOB).status_code == 201


def test_expected_commit_must_match(tmp_path, good_report):
    client = app_with(tmp_path, FakeGitLab(good_report))
    create(client)
    assert reasons(ingest(client, commit_sha="d" * 40)) == ["CI_COMMIT_MISMATCH"]


def test_retried_job_uses_latest_attempt(tmp_path, good_report):
    fake = FakeGitLab(good_report)
    fake.jobs.append({"id": JOB - 5, "name": ci_job_name("db-migration-recovery"), "status": "failed"})
    client = app_with(tmp_path, fake)
    create(client)
    assert ingest(client).status_code == 201


# ------------------------------------------------------- report contents

@pytest.mark.parametrize(
    "build, reason",
    [
        (lambda r: b"not json", "CI_ARTIFACT_MALFORMED"),
        (lambda r: b'{"verdict": "passed"}', "CI_ARTIFACT_MALFORMED"),
        (lambda r: reseal(r, pipeline_id=PIPELINE + 1), "CI_REPORT_PIPELINE_MISMATCH"),
        (lambda r: reseal(r, job_id=JOB + 1), "CI_REPORT_JOB_MISMATCH"),
        (lambda r: reseal(r, commit_sha="d" * 40), "CI_REPORT_COMMIT_MISMATCH"),
        (lambda r: reseal(r, exception_id="EXC-999"), "CI_REPORT_EXCEPTION_MISMATCH"),
        (lambda r: reseal(r, scenario_id="pipeline-gate-recovery"), "CI_REPORT_SCENARIO_MISMATCH"),
        (lambda r: r.model_copy(update={"ci": None}), "CI_REPORT_PROJECT_MISMATCH"),
        (lambda r: reseal(r, source="local_sandbox"), "CI_REPORT_SOURCE_MISMATCH"),
        # Edited after sealing (e.g. a failing check flipped) without re-sealing.
        (lambda r: r.model_copy(update={"evidence": r.evidence.model_copy(update={"job_id": JOB, "run_id": "RUN-x"})}),
         "EVIDENCE_DIGEST_MISMATCH"),
    ],
)
def test_report_contents_verified(tmp_path, good_report, build, reason):
    built = build(good_report)
    client = app_with(tmp_path, FakeGitLab(built if isinstance(built, RehearsalReport) else built))
    create(client)
    r = ingest(client)
    assert r.status_code == 422, r.text
    assert reason in reasons(r)


def test_report_verdict_is_recomputed_not_trusted(tmp_path, good_report):
    failing = good_report.evidence.check_results[:-1]  # drop the waived check, claim "passed"
    forged = reseal(good_report, check_results=failing).model_copy(update={"verdict": "passed", "failures": []})
    client = app_with(tmp_path, FakeGitLab(forged))
    create(client)
    r = ingest(client)
    assert "CI_REHEARSAL_FAILED" in reasons(r)
    assert "CHECK_MISSING:integration:migration_0042" in reasons(r)


def test_failed_real_rehearsal_rejected(tmp_path, module_tmp):
    class SkipsIntegrationTest(DbMigrationRecovery):
        def _run_waived_integration_test(self):
            pass

    report = real_report(module_tmp, SkipsIntegrationTest)
    assert report.verdict == "failed"
    client = app_with(tmp_path, FakeGitLab(report))
    create(client)
    assert "CI_REHEARSAL_FAILED" in reasons(ingest(client))


def test_expired_and_future_evidence_rejected(tmp_path, good_report):
    ev = good_report.evidence
    for shift, reason in ((timedelta(days=-9), "EVIDENCE_TOO_OLD"), (timedelta(hours=2), "EVIDENCE_FROM_FUTURE")):
        moved = reseal(good_report, started_at=ev.started_at + shift, finished_at=ev.finished_at + shift)
        (tmp_path / reason).mkdir()
        client = app_with(tmp_path / reason, FakeGitLab(moved))  # job window moves with it
        create(client)
        assert reason in reasons(ingest(client))


def test_report_outside_job_window_rejected(tmp_path, good_report):
    fake = FakeGitLab(good_report)
    fake.job["started_at"] = (good_report.evidence.finished_at + timedelta(minutes=30)).isoformat()
    fake.job["finished_at"] = (good_report.evidence.finished_at + timedelta(minutes=31)).isoformat()
    client = app_with(tmp_path, fake)
    create(client)
    assert "CI_REPORT_OUTSIDE_JOB_WINDOW" in reasons(ingest(client))


def test_duplicate_ingestion_rejected(tmp_path, good_report):
    client = app_with(tmp_path, FakeGitLab(good_report))
    create(client)
    assert ingest(client).status_code == 201
    r = ingest(client)
    assert (r.status_code, reasons(r)) == (409, ["CI_EVIDENCE_DUPLICATE"])
    assert len(client.get("/exceptions/EXC-001/evidence").json()) == 1


def test_one_evidence_record_per_ci_job(tmp_path, good_report):
    """Even if a job's artifact yielded a different (validly sealed) record,
    a CI job can back at most one evidence record, and nothing half-commits."""
    fake = FakeGitLab(good_report)
    client = app_with(tmp_path, fake)
    create(client)
    assert ingest(client).status_code == 201
    other = reseal(good_report, id="EVD-second-record")
    fake.artifacts[(JOB, evidence_artifact_path("db-migration-recovery"))] = other.model_dump_json().encode()
    r = ingest(client)
    assert (r.status_code, reasons(r)) == (409, ["CI_EVIDENCE_DUPLICATE"])
    assert [row["evidence"]["id"] for row in client.get("/exceptions/EXC-001/evidence").json()] == [
        good_report.evidence.id]
    assert audit_actions(client)[-1] == "evidence.ci_rejected"


# ----------------------------------------------- forged client metadata

@pytest.mark.parametrize(
    "extra",
    [
        {"source": "gitlab_ci"},
        {"status": "success"},
        {"report": {"verdict": "passed"}},
        {"evidence": {"source": "gitlab_ci"}},
        {"ref": "main"},
    ],
)
def test_client_cannot_supply_trust_metadata(tmp_path, good_report, extra):
    fake = FakeGitLab(good_report)
    client = app_with(tmp_path, fake)
    create(client)
    assert ingest(client, **extra).status_code == 422
    assert fake.calls == []


def test_unverified_gitlab_ci_rows_never_count(tmp_path, good_report):
    """A validly sealed gitlab_ci record written straight into the database,
    bypassing ingestion, is excluded for lack of server-side verification."""
    client = app_with(tmp_path, FakeGitLab(good_report))
    create(client)
    store = client.app.state.lifecycle.store
    with store.transaction() as conn:
        store.insert_evidence(conn, good_report.evidence, commit_origin="gitlab_ci", working_tree_dirty=False,
                              recorded_by="ci:forged")
    decision = client.get("/exceptions/EXC-001/decision", params={"commit": SHA}).json()
    assert decision["recommendation"] != "propose_retirement"
    assert decision["rejected_evidence"][good_report.evidence.id] == ["EVIDENCE_CI_PROVENANCE_UNVERIFIED"]


def test_ingestion_requires_identity(tmp_path, good_report):
    fake = FakeGitLab(good_report)
    client = app_with(tmp_path, fake)
    create(client)
    assert ingest(client, who={}).status_code == 401
    assert fake.calls == []


def test_scenario_must_be_required_for_exception(tmp_path, good_report):
    client = app_with(tmp_path, FakeGitLab(good_report))
    create(client)
    r = ingest(client, scenario_id="pipeline-gate-recovery")
    assert reasons(r) == ["SCENARIO_NOT_REQUIRED_FOR_EXCEPTION_TYPE"]


# ------------------------------------------------------ availability

@pytest.mark.parametrize("error", [GitLabError(500, "boom"), GitLabError(None, "TimeoutError"), GitLabError(401, "x")])
def test_gitlab_errors_fail_closed(tmp_path, good_report, error):
    fake = FakeGitLab(good_report)
    fake.error = error
    client = app_with(tmp_path, fake)
    create(client)
    r = ingest(client)
    assert (r.status_code, reasons(r)) == (503, ["CI_VERIFICATION_UNAVAILABLE"])
    assert client.get("/exceptions/EXC-001/evidence").json() == []


def test_not_configured_fails_closed(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path)))
    create(client)
    r = ingest(client, project_id=PROJECT)
    assert (r.status_code, reasons(r)) == (503, ["CI_VERIFICATION_NOT_CONFIGURED"])
    assert client.get("/health").json()["ci_verification"] == "not configured"


# ------------------------------------------- trust policy & production

def test_ci_only_trust_ignores_local_evidence(tmp_path, good_report):
    client = app_with(tmp_path, FakeGitLab(good_report), evidence_trust=EvidenceTrust.CI_ONLY)
    create(client)
    client.post("/exceptions/EXC-001/rehearsals", json={"scenario_id": "db-migration-recovery"}, headers=ALICE)
    decision = client.get("/exceptions/EXC-001/decision", params={"commit": SHA}).json()
    assert decision["recommendation"] == "keep_open"
    assert list(decision["rejected_evidence"].values()) == [["EVIDENCE_UNTRUSTED_SOURCE"]]
    assert ingest(client).status_code == 201
    decision = client.get("/exceptions/EXC-001/decision", params={"commit": SHA}).json()
    assert decision["recommendation"] == "propose_retirement"
    assert client.get("/health").json()["evidence_trust"] == "ci_only"


def test_production_ingestion_path(tmp_path, good_report):
    settings = make_settings(tmp_path, environment=Environment.PRODUCTION, auth_mode=AuthMode.NONE,
                             gitlab_project_ids=frozenset({PROJECT}),
                             gitlab_client_factory=lambda: FakeGitLab(good_report))
    app = create_app(settings)
    client = TestClient(app)
    assert client.post("/exceptions/EXC-001/ci-evidence", headers=ALICE,
                       json={"pipeline_id": PIPELINE, "scenario_id": "db-migration-recovery"}).status_code == 401
    lifecycle = app.state.lifecycle
    operator = Identity("system:ingest-cli", "server-cli", verified=True)
    from app.models import ExceptionCreate
    lifecycle.create(ExceptionCreate(**payload()), operator)
    row = lifecycle.ingest_ci_evidence("EXC-001", operator, pipeline_id=PIPELINE, scenario_id="db-migration-recovery")
    assert row["provenance_issues"] == []
    assert lifecycle.decision("EXC-001", SHA).recommendation.value == "propose_retirement"
    assert lifecycle.get("EXC-001").status.value == "active"


# ------------------------------------------------- HTTP client hygiene

def test_token_never_in_repr_or_health(tmp_path):
    settings = Settings(db_path=tmp_path / "x.db", gitlab_url="https://gitlab.example", gitlab_token="glpat-SECRET",
                        gitlab_project_ids=frozenset({PROJECT}))
    assert "SECRET" not in repr(settings)
    assert "SECRET" not in repr(settings.gitlab_client())
    client = TestClient(create_app(settings))
    health = client.get("/health").json()
    assert health["ci_verification"] == "configured"
    assert "SECRET" not in json.dumps(health)


def test_http_gitlab_url_rejected(tmp_path):
    with pytest.raises(ConfigError):
        Settings(db_path=tmp_path / "x.db", gitlab_url="http://gitlab.example")
    with pytest.raises(ValueError):
        HttpGitLabClient("http://gitlab.example", "t")


def _redirect(original, new):
    req = urllib.request.Request(original, headers={"PRIVATE-TOKEN": "glpat-SECRET"})
    return _NoTokenAcrossHosts().redirect_request(req, None, 302, "Found", {}, new)


def test_token_stripped_on_cross_host_redirect():
    same = _redirect("https://gitlab.example/api/v4/x", "https://gitlab.example/api/v4/y")
    assert same.get_header("Private-token") == "glpat-SECRET"
    cdn = _redirect("https://gitlab.example/api/v4/x", "https://cdn.example/blob")
    assert cdn.get_header("Private-token") is None
    with pytest.raises(GitLabError):
        _redirect("https://gitlab.example/api/v4/x", "http://cdn.example/blob")


def test_http_errors_mapped_without_token(monkeypatch):
    client = HttpGitLabClient("https://gitlab.example", "glpat-SECRET")

    def boom(request, timeout):
        assert request.full_url == "https://gitlab.example/api/v4/projects/31/pipelines/501"
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(client._opener, "open", boom)
    with pytest.raises(GitLabError) as err:
        client.get_pipeline(31, 501)
    assert err.value.status == 404
    assert "SECRET" not in str(err.value)


# ----------------------------------------------------- pipeline & summary

def test_gitlab_ci_yaml_matches_trust_contract():
    config = yaml.safe_load((REPO_ROOT / ".gitlab-ci.yml").read_text())
    assert config["stages"] == ["validate", "test", "rehearse", "evaluate"]
    assert config["default"]["image"].startswith("python:3.12")
    # Keys GitLab's parser accepts under `default:` (ALLOWED_KEYS in
    # lib/gitlab/ci/config/entry/default.rb); anything else invalidates the pipeline.
    allowed_default = {"after_script", "artifacts", "before_script", "cache", "hooks", "id_tokens", "image",
                       "interruptible", "retry", "services", "tags", "timeout"}
    assert set(config["default"]) <= allowed_default
    for job in ("validate", "test", ".rehearse", "evidence-summary"):
        assert config[job]["timeout"].endswith("minutes")
    from app.rehearsal import SCENARIOS
    for scenario_id in SCENARIOS:
        job = config[ci_job_name(scenario_id)]
        assert job["extends"] == ".rehearse"
        assert job["variables"]["SCENARIO_ID"] == scenario_id
    template = config[".rehearse"]
    assert template["artifacts"]["when"] == "always"
    assert "gatedebt-evidence/" in template["artifacts"]["paths"]
    script = " ".join(template["script"])
    assert "--source gitlab_ci" in script and "--evidence-out" in script
    assert "$CI_PROJECT_DIR/gatedebt-evidence/$SCENARIO_ID.json" in script
    assert evidence_artifact_path("x") == "gatedebt-evidence/x.json"
    text = (REPO_ROOT / ".gitlab-ci.yml").read_text()
    assert "TOKEN" not in text  # the pipeline needs no credentials
    for job in ("validate", "test", "evidence-summary"):
        assert job in config


def test_summary_self_check(tmp_path, good_report, module_tmp):
    evidence_dir = tmp_path / "gatedebt-evidence"
    evidence_dir.mkdir()
    (evidence_dir / "db-migration-recovery.json").write_text(good_report.model_dump_json())
    ok, summary = summarize(evidence_dir)
    assert not ok  # pipeline-gate report missing
    by_id = {s["scenario_id"]: s for s in summary["scenarios"]}
    assert by_id["db-migration-recovery"]["status"] == "passed"
    assert by_id["db-migration-recovery"]["project_id"] == PROJECT
    assert by_id["pipeline-gate-recovery"]["problems"] == ["REPORT_MISSING"]

    pipeline = real_report(module_tmp, PipelineGateRecovery, exception_id="EXC-002")
    (evidence_dir / "pipeline-gate-recovery.json").write_text(pipeline.model_dump_json())
    assert summarize(evidence_dir)[0] is True

    tampered = good_report.model_copy(update={"evidence": good_report.evidence.model_copy(update={"job_id": 1})})
    (evidence_dir / "db-migration-recovery.json").write_text(tampered.model_dump_json())
    ok, summary = summarize(evidence_dir)
    assert not ok
    write_junit(summary, tmp_path / "junit.xml")
    xml = (tmp_path / "junit.xml").read_text()
    assert "policy:EVIDENCE_DIGEST_MISMATCH" in xml


def test_cli_evidence_out_and_summarize_exit_codes(tmp_path):
    out = tmp_path / "gatedebt-evidence" / "db-migration-recovery.json"
    code = cli.main(["run", "db-migration-recovery", "--exception-id", "EXC-001", "--source", "gitlab_ci",
                     "--artifacts-dir", str(tmp_path / "runs"), "--evidence-out", str(out)], env=ci_env())
    assert code == cli.EXIT_PASSED
    report = RehearsalReport.model_validate_json(out.read_text())
    assert report.ci.project_id == PROJECT and report.evidence.job_id == JOB
    assert cli.main(["summarize", str(out.parent), "--out", str(tmp_path / "summary")]) == cli.EXIT_FAILED
    assert json.loads((tmp_path / "summary" / "summary.json").read_text())["all_passed"] is False


# ------------------------------------------------------- operator command

def test_operator_cli(tmp_path, good_report, monkeypatch, capsys):
    from app import ingest_ci

    settings = make_settings(tmp_path, environment=Environment.PRODUCTION, auth_mode=AuthMode.NONE,
                             gitlab_project_ids=frozenset({PROJECT}),
                             gitlab_client_factory=lambda: FakeGitLab(good_report))
    monkeypatch.setattr(ingest_ci.Settings, "from_env", classmethod(lambda cls: settings))
    from app.models import ExceptionCreate
    from app.services.lifecycle import Lifecycle
    from app.store import Store
    Lifecycle(Store(settings.db_path), settings).create(ExceptionCreate(**payload()), ingest_ci.OPERATOR)

    argv = ["--exception-id", "EXC-001", "--pipeline-id", str(PIPELINE), "--scenario", "db-migration-recovery"]
    assert ingest_ci.main(argv) == 0
    assert f"job {JOB}" in capsys.readouterr().out
    assert ingest_ci.main(argv) == 1  # duplicate
    assert "CI_EVIDENCE_DUPLICATE" in capsys.readouterr().err
    assert ingest_ci.main([*argv, "--commit", "nope"]) == 2
