"""API tests. Each test gets a fresh temporary SQLite file and artifacts dir.
Rehearsals genuinely execute; repo state is injected so results do not depend
on the developer's working tree."""

import json
import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.auth import Identity
from app.config import AuthMode, ConfigError, Environment, RepoState, Settings
from app.models import ApprovalDecision, ApprovalKind, CheckOutcome, EvidenceRecord, EvidenceSource, utcnow
from app.services.lifecycle import Conflict

from .factories import make_evidence, make_exception, with_check

SHA = "e" * 40
OTHER_SHA = "f" * 40


def dev(actor):
    return {"X-GateDebt-Dev-User": actor}


ALICE, BOB, CAROL = dev("user:alice"), dev("user:bob"), dev("user:carol")
AGENT, CI = dev("agent:mock-investigator"), dev("ci:pipeline-9")


def make_settings(tmp_path, dirty=False, **overrides):
    fields = dict(
        db_path=tmp_path / "gatedebt.sqlite3",
        artifacts_dir=tmp_path / "artifacts",
        approvers=frozenset({"user:bob", "user:carol"}),
        repo_state=lambda: RepoState(SHA, dirty),
    )
    fields.update(overrides)
    return Settings(**fields)


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(make_settings(tmp_path)))


def payload(**overrides):
    body = {
        "id": "EXC-001",
        "project": "demo/payments",
        "type": "skipped_integration_test",
        "title": "Skip migration 0042 integration test",
        "reason": "Blocked the 2.3 release",
        "owner": "alice",
        "expires_at": (utcnow() + timedelta(days=30)).isoformat(),
        "affected_check": "integration:migration_0042",
        "remediation_target": "Fix migration 0042 and re-enable the test",
    }
    body.update(overrides)
    return body


def create(client, **overrides):
    r = client.post("/exceptions", json=payload(**overrides), headers=ALICE)
    assert r.status_code == 201, r.text
    return r.json()


def rehearse(client, scenario="db-migration-recovery", exc_id="EXC-001"):
    r = client.post(f"/exceptions/{exc_id}/rehearsals", json={"scenario_id": scenario}, headers=ALICE)
    assert r.status_code == 201, r.text
    return r.json()


def proposed(client, proposer=AGENT):
    create(client)
    assert rehearse(client)["verdict"] == "passed"
    r = client.post("/exceptions/EXC-001/retirement-proposal", json={}, headers=proposer)
    assert r.status_code == 201, r.text
    return r.json()


def approve(client, who=BOB, decision="approved", kind="retirement", **extra):
    return client.post("/exceptions/EXC-001/approvals",
                       json={"kind": kind, "decision": decision, "comment": "reviewed evidence", **extra}, headers=who)


def audit_actions(client):
    return [e["action"] for e in client.get("/exceptions/EXC-001/audit").json()]


def status(client):
    return client.get("/exceptions/EXC-001").json()["status"]


# ------------------------------------------------------------------ health

def test_health_labels_dev_auth(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["environment"] == "development"
    assert "development only, unverified" in body["authentication"]
    assert body["trusted_evidence_sources"] == ["gitlab_ci", "local_sandbox"]


# ---------------------------------------------------------- create/read

def test_create_and_read_exception(client):
    created = create(client)
    assert created["status"] == "active"
    assert created["created_by"] == "user:alice"
    assert created["expiry_state"] == "active"
    assert client.get("/exceptions/EXC-001").json() == created
    assert [e["id"] for e in client.get("/exceptions").json()] == ["EXC-001"]
    assert client.get("/exceptions", params={"status": "retired"}).json() == []
    assert audit_actions(client) == ["exception.created"]


def test_expiry_state_reported(client):
    create(client, id="EXC-DUE", expires_at=(utcnow() + timedelta(days=2)).isoformat())
    assert client.get("/exceptions/EXC-DUE").json()["expiry_state"] == "due"


@pytest.mark.parametrize(
    "overrides",
    [
        {"type": "disable_everything"},
        {"expires_at": "2026-12-01T00:00:00"},  # naive
        {"expires_at": (utcnow() - timedelta(days=1)).isoformat()},  # in the past
        {"expires_at": (utcnow() + timedelta(days=120)).isoformat()},  # too long
        {"id": "exc-lowercase"},
        {"title": ""},
        {"status": "retired"},  # client cannot set status
        {"created_by": "user:mallory"},  # nor provenance fields
    ],
)
def test_create_validation_errors(client, overrides):
    r = client.post("/exceptions", json=payload(**overrides), headers=ALICE)
    assert r.status_code == 422


def test_create_missing_field(client):
    body = payload()
    del body["affected_check"]
    assert client.post("/exceptions", json=body, headers=ALICE).status_code == 422


def test_duplicate_id_conflict(client):
    create(client)
    r = client.post("/exceptions", json=payload(), headers=ALICE)
    assert r.status_code == 409
    assert r.json()["detail"]["reason_codes"] == ["EXCEPTION_ID_EXISTS"]


@pytest.mark.parametrize("headers", [{}, dev("alice"), dev("root:x"), dev("user:" + "x" * 100)])
def test_create_requires_identity(client, headers):
    assert client.post("/exceptions", json=payload(), headers=headers).status_code == 401


def test_missing_records_404(client):
    for path in ["", "/evidence", "/decision", "/approvals", "/audit"]:
        r = client.get(f"/exceptions/EXC-404{path}", params={"commit": SHA} if path == "/decision" else None)
        assert r.status_code == 404, path
    assert client.get("/exceptions/not-an-id").status_code == 422


def test_persistence_survives_restart(tmp_path):
    first = TestClient(create_app(make_settings(tmp_path)))
    created = create(first)
    rehearse(first)
    second = TestClient(create_app(make_settings(tmp_path)))
    assert second.get("/exceptions/EXC-001").json() == created
    assert len(second.get("/exceptions/EXC-001/evidence").json()) == 1


# --------------------------------------------------------------- rehearsals

def test_rehearsal_records_real_evidence(client):
    create(client)
    result = rehearse(client)
    assert result["verdict"] == "passed"
    assert result["provenance_issues"] == []
    assert "not a GitLab CI run" in result["execution_note"]
    [row] = client.get("/exceptions/EXC-001/evidence").json()
    evidence = EvidenceRecord.model_validate(row["evidence"])
    assert evidence.digest_matches()
    assert (evidence.exception_id, evidence.commit_sha, evidence.source) == ("EXC-001", SHA, EvidenceSource.LOCAL_SANDBOX)
    assert "evidence.recorded" in audit_actions(client)


@pytest.mark.parametrize(
    "scenario, code, reason",
    [
        ("rm-rf-everything", 422, "UNKNOWN_SCENARIO"),
        ("pipeline-gate-recovery", 422, "SCENARIO_NOT_REQUIRED_FOR_EXCEPTION_TYPE"),
    ],
)
def test_rehearsal_only_runs_approved_required_scenarios(client, scenario, code, reason):
    create(client)
    r = client.post("/exceptions/EXC-001/rehearsals", json={"scenario_id": scenario}, headers=ALICE)
    assert r.status_code == code
    assert r.json()["detail"]["reason_codes"] == [reason]


@pytest.mark.parametrize("body", [{"scenario_id": "../../etc/passwd"}, {"scenario_id": "x", "command": "rm -rf /"}])
def test_rehearsal_rejects_paths_and_commands(client, body):
    create(client)
    assert client.post("/exceptions/EXC-001/rehearsals", json=body, headers=ALICE).status_code == 422


def test_rehearsal_requires_identity(client):
    create(client)
    r = client.post("/exceptions/EXC-001/rehearsals", json={"scenario_id": "db-migration-recovery"})
    assert r.status_code == 401


# ----------------------------------------------------------------- decision

def test_decision_is_read_only(client):
    create(client)
    rehearse(client)
    before = (status(client), audit_actions(client))
    for _ in range(2):
        decision = client.get("/exceptions/EXC-001/decision").json()
        assert decision["recommendation"] == "propose_retirement"
        assert decision["requires_human_approval"] is True
    assert (status(client), audit_actions(client)) == before
    assert status(client) == "active"


def test_decision_without_evidence_fails_closed(client):
    create(client)
    decision = client.get("/exceptions/EXC-001/decision").json()
    assert decision["recommendation"] == "keep_open"
    assert decision["missing_checks"] == ["db-migration-recovery:*"]


def test_stale_commit_evidence_rejected(client):
    create(client)
    rehearse(client)
    decision = client.get("/exceptions/EXC-001/decision", params={"commit": OTHER_SHA}).json()
    assert decision["recommendation"] != "propose_retirement"
    assert list(decision["rejected_evidence"].values()) == [["EVIDENCE_STALE_COMMIT"]]


def test_bad_commit_param_rejected(client):
    create(client)
    assert client.get("/exceptions/EXC-001/decision", params={"commit": "HEAD"}).status_code == 422


def test_uncommitted_source_evidence_excluded(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path, dirty=True)))
    create(client)
    result = rehearse(client)
    assert result["verdict"] == "passed"  # the run itself passed...
    assert result["provenance_issues"] == ["EVIDENCE_UNCOMMITTED_SOURCE"]
    decision = client.get("/exceptions/EXC-001/decision").json()
    assert decision["recommendation"] != "propose_retirement"  # ...but it cannot qualify
    assert list(decision["rejected_evidence"].values()) == [["EVIDENCE_UNCOMMITTED_SOURCE"]]
    r = client.post("/exceptions/EXC-001/retirement-proposal", json={}, headers=AGENT)
    assert r.status_code == 409


def test_tampered_evidence_rejected(client):
    create(client)
    rehearse(client)
    lifecycle = client.app.state.lifecycle
    [row] = lifecycle.evidence("EXC-001")
    forged = json.loads(row["evidence"].model_dump_json())
    forged["id"] = "EVD-forged"
    forged["check_results"] = [{**c, "outcome": "passed"} for c in forged["check_results"]]
    forged["cleanup_status"] = "verified"
    forged["finished_at"] = utcnow().isoformat()  # newest run, so it would win if trusted
    forged["started_at"] = forged["finished_at"]
    with lifecycle.store.transaction() as conn:  # bypasses the API, as an attacker with DB access would
        conn.execute(
            "INSERT INTO evidence (id, exception_id, scenario_id, source, commit_sha, finished_at, commit_origin,"
            " working_tree_dirty, recorded_at, recorded_by, record_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("EVD-forged", "EXC-001", forged["scenario_id"], "gitlab_ci", SHA, forged["finished_at"], "gitlab_ci",
             0, forged["finished_at"], "ci:forged", json.dumps({**forged, "source": "gitlab_ci"})),
        )
    decision = client.get("/exceptions/EXC-001/decision").json()
    assert "EVIDENCE_DIGEST_MISMATCH" in decision["rejected_evidence"]["EVD-forged"]
    assert "EVD-forged" not in decision["supporting_evidence_ids"]


# ---------------------------------------------------------------- proposals

def test_proposal_refused_when_not_eligible(client):
    create(client)
    r = client.post("/exceptions/EXC-001/retirement-proposal", json={}, headers=AGENT)
    assert r.status_code == 409
    assert r.json()["detail"]["reason_codes"][0] == "NOT_ELIGIBLE_FOR_RETIREMENT"
    assert status(client) == "active"
    assert audit_actions(client)[-1] == "retirement.proposal_refused"


def test_proposal_does_not_retire(client):
    report = proposed(client)
    assert report["recommendation"] == "propose_retirement"
    assert status(client) == "retirement_proposed"
    r = client.post("/exceptions/EXC-001/retirement-proposal", json={}, headers=AGENT)
    assert r.status_code == 409  # already proposed


# -------------------------------------------------------- approvals & auth

def test_full_retirement_workflow(client):
    proposed(client)
    r = approve(client, BOB)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["exception"]["status"] == "retirement_approved"
    assert body["approval"]["commit_sha"] == SHA
    assert body["approval"]["evidence_ids"]

    verification = {"merged_commit_sha": OTHER_SHA, "merge_request": "!42", "change_merged": True,
                    "waiver_still_present": False, "pipeline_status": "success"}
    r = client.post("/exceptions/EXC-001/verifications", json=verification, headers=BOB)
    assert r.status_code == 200, r.text
    assert r.json()["retired"] is True
    assert status(client) == "retired"
    assert audit_actions(client) == [
        "exception.created", "evidence.recorded", "exception.status_changed", "retirement.proposed",
        "approval.recorded", "exception.status_changed", "retirement.verification_recorded",
        "exception.status_changed",
    ]
    # A retired exception is final.
    assert client.get("/exceptions/EXC-001/decision").status_code == 409


def test_failed_verification_does_not_retire(client):
    proposed(client)
    approve(client, BOB)
    r = client.post("/exceptions/EXC-001/verifications", headers=BOB, json={
        "merged_commit_sha": OTHER_SHA, "change_merged": True, "waiver_still_present": True,
        "pipeline_status": "failed"})
    assert r.status_code == 200
    assert r.json()["retired"] is False
    assert set(r.json()["issues"]) == {"WAIVER_STILL_PRESENT", "VERIFICATION_PIPELINE_NOT_SUCCESSFUL"}
    assert status(client) == "retirement_approved"


def test_verification_requires_approved_retirement(client):
    proposed(client)
    r = client.post("/exceptions/EXC-001/verifications", headers=BOB, json={
        "merged_commit_sha": OTHER_SHA, "change_merged": True, "waiver_still_present": False,
        "pipeline_status": "success"})
    assert r.status_code == 409
    assert status(client) == "retirement_proposed"


def test_agents_cannot_submit_verification(client):
    proposed(client)
    approve(client, BOB)
    r = client.post("/exceptions/EXC-001/verifications", headers=AGENT, json={
        "merged_commit_sha": OTHER_SHA, "change_merged": True, "waiver_still_present": False,
        "pipeline_status": "success"})
    assert r.status_code == 403


def test_rejected_approval_returns_to_active(client):
    proposed(client)
    r = approve(client, BOB, decision="rejected")
    assert r.status_code == 201
    assert r.json()["exception"]["status"] == "active"
    assert client.get("/exceptions/EXC-001/approvals").json()[0]["decision"] == "rejected"


def test_unauthenticated_approval_rejected_and_audited(client):
    proposed(client)
    r = approve(client, {})
    assert r.status_code == 401
    assert status(client) == "retirement_proposed"
    events = client.get("/exceptions/EXC-001/audit").json()
    assert events[-1]["action"] == "approval.denied"
    assert events[-1]["details"] == {"reason_codes": ["AUTHENTICATION_REQUIRED"]}


@pytest.mark.parametrize(
    "who, reason",
    [
        (AGENT, "APPROVER_MUST_BE_HUMAN"),
        (CI, "APPROVER_MUST_BE_HUMAN"),
        (dev("user:mallory"), "APPROVER_NOT_AUTHORIZED"),
    ],
)
def test_forbidden_approvers(client, who, reason):
    proposed(client)
    r = approve(client, who)
    assert r.status_code == 403
    assert r.json()["detail"]["reason_codes"] == [reason]
    assert status(client) == "retirement_proposed"
    assert client.get("/exceptions/EXC-001/approvals").json() == []


def test_proposer_cannot_approve_own_proposal(client):
    proposed(client, proposer=BOB)
    r = approve(client, BOB)
    assert r.status_code == 403
    assert r.json()["detail"]["reason_codes"] == ["SELF_APPROVAL_FORBIDDEN"]
    assert approve(client, CAROL).status_code == 201


def test_retirement_approval_requires_proposal(client):
    create(client)
    r = approve(client, BOB)
    assert r.status_code == 409
    assert r.json()["detail"]["reason_codes"] == ["RETIREMENT_NOT_PROPOSED"]


def test_approval_rechecks_evidence(client):
    proposed(client)
    # A newer failing run lands after the proposal: the proposal is no longer valid.
    exc = make_exception(affected_check="integration:migration_0042")
    now = utcnow()
    failing = with_check(
        make_evidence(exc, id="EVD-newer", commit_sha=SHA, started_at=now - timedelta(seconds=2), finished_at=now),
        "post_recovery_data_intact", CheckOutcome.FAILED,
    )
    store = client.app.state.lifecycle.store
    with store.transaction() as conn:
        store.insert_evidence(conn, failing, commit_origin="provided", working_tree_dirty=False, recorded_by="user:alice")
    r = approve(client, BOB)
    assert r.status_code == 409
    assert r.json()["detail"]["reason_codes"][0] == "PROPOSAL_NO_LONGER_VALID"
    assert status(client) == "retirement_proposed"


# ------------------------------------------------------------------ renewal

def test_renewal_extends_expiry_explicitly(client):
    create(client)
    new_expiry = (utcnow() + timedelta(days=60)).isoformat()
    r = approve(client, BOB, kind="renewal", new_expires_at=new_expiry)
    assert r.status_code == 201, r.text
    exc = r.json()["exception"]
    assert exc["renewal_count"] == 1
    assert exc["status"] == "active"
    assert "exception.renewed" in audit_actions(client)


@pytest.mark.parametrize(
    "extra, code",
    [
        ({}, 422),  # no new expiry
        ({"new_expires_at": (utcnow() + timedelta(days=5)).isoformat()}, 422),  # not extended
        ({"new_expires_at": (utcnow() + timedelta(days=200)).isoformat()}, 422),  # too long
    ],
)
def test_invalid_renewals(client, extra, code):
    before = create(client)
    r = approve(client, BOB, kind="renewal", **extra)
    assert r.status_code == code
    assert client.get("/exceptions/EXC-001").json()["expires_at"] == before["expires_at"]


def test_rejected_renewal_changes_nothing(client):
    before = create(client)
    r = approve(client, BOB, kind="renewal", decision="rejected")
    assert r.status_code == 201
    after = client.get("/exceptions/EXC-001").json()
    assert (after["expires_at"], after["renewal_count"]) == (before["expires_at"], 0)


def test_retirement_approval_cannot_carry_expiry(client):
    proposed(client)
    r = approve(client, BOB, new_expires_at=(utcnow() + timedelta(days=10)).isoformat())
    assert r.status_code == 422


# ----------------------------------------------------------- production mode

def test_dev_auth_refused_in_production(tmp_path):
    with pytest.raises(ConfigError):
        make_settings(tmp_path, environment=Environment.PRODUCTION, auth_mode=AuthMode.DEV_HEADER)


def test_production_without_identity_provider_refuses_writes(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path, environment=Environment.PRODUCTION, auth_mode=AuthMode.NONE)))
    health = client.get("/health").json()
    assert health["trusted_evidence_sources"] == ["gitlab_ci"]
    assert health["authentication"].startswith("none")
    assert client.post("/exceptions", json=payload(), headers=ALICE).status_code == 401
    assert client.post("/exceptions/EXC-001/approvals", headers=BOB, json={
        "kind": "retirement", "decision": "approved", "comment": "ok"}).status_code == 401


def test_production_rules_in_service(tmp_path):
    settings = make_settings(tmp_path, environment=Environment.PRODUCTION, auth_mode=AuthMode.NONE)
    app = create_app(settings)
    lifecycle = app.state.lifecycle
    verified = Identity("user:bob", "gitlab-oidc", verified=True)
    unverified = Identity("user:bob", "dev-header", verified=False)
    from app.models import ExceptionCreate
    lifecycle.create(ExceptionCreate(**payload()), verified)

    with pytest.raises(Exception) as err:
        lifecycle.rehearse("EXC-001", "db-migration-recovery", verified)
    assert err.value.reason_codes == ["LOCAL_REHEARSALS_DISABLED_IN_PRODUCTION"]
    with pytest.raises(Exception) as err:
        lifecycle.approve("EXC-001", ApprovalKind.RENEWAL, ApprovalDecision.APPROVED, "ok", unverified)
    assert err.value.reason_codes == ["APPROVER_NOT_VERIFIED"]
    with pytest.raises(Exception) as err:
        lifecycle.decision("EXC-001", None)
    assert err.value.reason_codes == ["COMMIT_REQUIRED"]
    # Local sandbox evidence is not trusted in production.
    exc = make_exception(affected_check="integration:migration_0042")
    now = utcnow()
    local = make_evidence(exc, commit_sha=SHA, started_at=now - timedelta(seconds=2), finished_at=now)
    with lifecycle.store.transaction() as conn:
        lifecycle.store.insert_evidence(conn, local, commit_origin="provided", working_tree_dirty=False,
                                        recorded_by="user:bob")
    decision = lifecycle.decision("EXC-001", SHA)
    assert decision.rejected_evidence[local.id] == ["EVIDENCE_UNTRUSTED_SOURCE"]


def test_settings_from_env():
    s = Settings.from_env({"GATEDEBT_ENV": "production", "GATEDEBT_APPROVERS": "user:bob, user:carol"})
    assert s.auth_mode == AuthMode.NONE and s.approvers == {"user:bob", "user:carol"}
    with pytest.raises(ConfigError):
        Settings.from_env({"GATEDEBT_ENV": "production", "GATEDEBT_AUTH_MODE": "dev-header"})
    with pytest.raises(ConfigError):
        Settings.from_env({"GATEDEBT_ENV": "staging"})


# ------------------------------------------------- transactions & concurrency

def test_failure_mid_transaction_leaves_no_partial_write(client, monkeypatch):
    proposed(client)
    store = client.app.state.lifecycle.store
    real = store.append_audit

    def failing(conn, actor, action, *args, **kwargs):
        if action == "exception.status_changed":
            raise RuntimeError("disk full")
        return real(conn, actor, action, *args, **kwargs)

    monkeypatch.setattr(store, "append_audit", failing)
    with pytest.raises(RuntimeError):
        approve(client, BOB)
    monkeypatch.undo()
    assert status(client) == "retirement_proposed"
    assert client.get("/exceptions/EXC-001/approvals").json() == []
    assert "approval.recorded" not in audit_actions(client)


def test_concurrent_approvals_only_one_wins(client):
    proposed(client)
    lifecycle = client.app.state.lifecycle
    results, barrier = [], threading.Barrier(2)

    def worker(actor):
        barrier.wait()
        try:
            lifecycle.approve("EXC-001", ApprovalKind.RETIREMENT, ApprovalDecision.APPROVED, "ok",
                              Identity(actor, "dev-header", verified=False))
            results.append("ok")
        except Conflict as err:
            results.append(err.reason_codes[0])

    threads = [threading.Thread(target=worker, args=(a,)) for a in ("user:bob", "user:carol")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["RETIREMENT_NOT_PROPOSED", "ok"]
    assert len(client.get("/exceptions/EXC-001/approvals").json()) == 1
    assert status(client) == "retirement_approved"


def test_concurrent_creates_same_id(client):
    lifecycle = client.app.state.lifecycle
    from app.models import ExceptionCreate
    data = ExceptionCreate(**payload())
    results, barrier = [], threading.Barrier(4)

    def worker():
        barrier.wait()
        try:
            lifecycle.create(data, Identity("user:alice", "dev-header", verified=False))
            results.append("ok")
        except Conflict:
            results.append("conflict")

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["conflict", "conflict", "conflict", "ok"]
    assert audit_actions(client) == ["exception.created"]
