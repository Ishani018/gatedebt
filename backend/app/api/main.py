"""FastAPI application. Run with:

    uvicorn --factory app.api.main:create_app

Handlers only translate HTTP <-> the lifecycle service; no policy lives here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.auth import Identity, build_authenticator
from app.config import Settings
from app.models import (
    ApprovalDecision,
    ApprovalKind,
    ApprovalRecord,
    AuditEvent,
    DecisionReport,
    EvidenceRecord,
    ExceptionCreate,
    ExceptionRecord,
    ExceptionStatus,
    ExpiryState,
    utcnow,
)
from app.models.common import CommitSha, NonEmptyStr, UTCDateTime
from app.services.lifecycle import DomainError, Lifecycle
from app.services.policy import expiry_state
from app.store import Store

ExceptionIdPath = Annotated[str, Path(pattern=r"^EXC-[A-Z0-9-]{1,32}$")]
CommitQuery = Annotated[str | None, Query(pattern=r"^[0-9a-f]{40}$")]



def current_identity(request: Request) -> Identity | None:
    return request.app.state.authenticator.authenticate(request)


IdentityDep = Annotated[Identity | None, Depends(current_identity)]


# ----------------------------------------------------------------- schemas

class ExceptionView(ExceptionRecord):
    expiry_state: ExpiryState


class EvidenceView(BaseModel):
    evidence: EvidenceRecord
    commit_origin: str
    working_tree_dirty: bool | None
    provenance_issues: list[str]
    recorded_at: str
    recorded_by: str
    ci_verification: dict[str, Any] | None = None


class CiEvidenceRequest(BaseModel):
    """Pointers only. Source, status, commit and report content are never
    accepted from the caller; the server reads them from GitLab."""

    model_config = ConfigDict(extra="forbid")
    pipeline_id: int = Field(ge=1)
    scenario_id: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    project_id: int | None = Field(default=None, ge=1)
    job_id: int | None = Field(default=None, ge=1)
    commit_sha: CommitSha | None = None


class RehearsalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Checked against the approved registry; never used as a path or command.
    scenario_id: str = Field(pattern=r"^[a-z0-9-]{1,64}$")


class RehearsalView(BaseModel):
    verdict: Literal["passed", "failed"]
    failures: list[str]
    provenance_issues: list[str]
    execution_note: str
    evidence: EvidenceRecord


class ProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commit_sha: CommitSha | None = None


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: ApprovalKind
    decision: ApprovalDecision
    comment: NonEmptyStr
    new_expires_at: UTCDateTime | None = None
    commit_sha: CommitSha | None = None


class ApprovalView(BaseModel):
    approval: ApprovalRecord
    exception: ExceptionView


class VerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    merged_commit_sha: CommitSha
    merge_request: NonEmptyStr | None = None
    change_merged: bool
    waiver_still_present: bool
    pipeline_status: Literal["success", "failed", "canceled", "running", "pending", "skipped", "unknown"]


class VerificationView(BaseModel):
    verification_id: str
    retired: bool
    issues: list[str]
    exception: ExceptionView


def view(exc: ExceptionRecord, settings: Settings, now: datetime | None = None) -> ExceptionView:
    return ExceptionView(**exc.model_dump(), expiry_state=expiry_state(exc, now or utcnow(), settings.policy))


# --------------------------------------------------------------------- app

def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.db_path)
    lifecycle = Lifecycle(store, settings)
    authenticator = build_authenticator(settings)

    app = FastAPI(title="GateDebt", version="0.1.0")
    app.state.settings = settings
    app.state.lifecycle = lifecycle
    app.state.authenticator = authenticator

    @app.exception_handler(DomainError)
    async def domain_error(_: Request, err: DomainError) -> JSONResponse:
        return JSONResponse(status_code=err.status_code, content={"detail": {"reason_codes": err.reason_codes}})

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "environment": settings.environment.value,
            "authentication": authenticator.name,
            "trusted_evidence_sources": sorted(s.value for s in settings.policy.trusted_sources),
            "evidence_trust": settings.effective_trust.value,
            "ci_verification": "configured" if settings.ci_verification_configured else "not configured",
            "schema_version": store.schema_version(),
        }

    @app.post("/exceptions", status_code=201)
    def create_exception(body: ExceptionCreate, who: IdentityDep) -> ExceptionView:
        return view(lifecycle.create(body, who), settings)

    @app.get("/exceptions")
    def list_exceptions(status: ExceptionStatus | None = None) -> list[ExceptionView]:
        now = utcnow()
        return [view(e, settings, now) for e in lifecycle.list(status)]

    @app.get("/exceptions/{exception_id}")
    def get_exception(exception_id: ExceptionIdPath) -> ExceptionView:
        return view(lifecycle.get(exception_id), settings)

    @app.get("/exceptions/{exception_id}/evidence")
    def list_evidence(exception_id: ExceptionIdPath) -> list[EvidenceView]:
        return [EvidenceView(**row) for row in lifecycle.evidence(exception_id)]

    @app.get("/exceptions/{exception_id}/decision")
    def get_decision(exception_id: ExceptionIdPath, commit: CommitQuery = None) -> DecisionReport:
        return lifecycle.decision(exception_id, commit)

    @app.post("/exceptions/{exception_id}/rehearsals", status_code=201)
    def run_rehearsal(exception_id: ExceptionIdPath, body: RehearsalRequest, who: IdentityDep) -> RehearsalView:
        report, issues = lifecycle.rehearse(exception_id, body.scenario_id, who)
        return RehearsalView(
            verdict=report.verdict, failures=report.failures, provenance_issues=issues,
            execution_note=report.execution_note, evidence=report.evidence,
        )

    @app.post("/exceptions/{exception_id}/ci-evidence", status_code=201)
    def ingest_ci_evidence(exception_id: ExceptionIdPath, body: CiEvidenceRequest, who: IdentityDep) -> EvidenceView:
        row = lifecycle.ingest_ci_evidence(
            exception_id, who, pipeline_id=body.pipeline_id, scenario_id=body.scenario_id,
            project_id=body.project_id, job_id=body.job_id, expected_commit=body.commit_sha,
        )
        return EvidenceView(**row)

    @app.post("/exceptions/{exception_id}/retirement-proposal", status_code=201)
    def propose_retirement(exception_id: ExceptionIdPath, body: ProposalRequest, who: IdentityDep) -> DecisionReport:
        return lifecycle.propose_retirement(exception_id, body.commit_sha, who)

    @app.post("/exceptions/{exception_id}/approvals", status_code=201)
    def create_approval(exception_id: ExceptionIdPath, body: ApprovalRequest, who: IdentityDep) -> ApprovalView:
        approval, exc = lifecycle.approve(
            exception_id, body.kind, body.decision, body.comment, who,
            new_expires_at=body.new_expires_at, commit=body.commit_sha,
        )
        return ApprovalView(approval=approval, exception=view(exc, settings))

    @app.get("/exceptions/{exception_id}/approvals")
    def list_approvals(exception_id: ExceptionIdPath) -> list[ApprovalRecord]:
        return lifecycle.approvals(exception_id)

    @app.post("/exceptions/{exception_id}/verifications")
    def verify_retirement(exception_id: ExceptionIdPath, body: VerificationRequest, who: IdentityDep) -> VerificationView:
        verification_id, issues, exc = lifecycle.verify_retirement(exception_id, who, **body.model_dump())
        return VerificationView(
            verification_id=verification_id, retired=not issues, issues=issues, exception=view(exc, settings)
        )

    @app.get("/exceptions/{exception_id}/audit")
    def list_audit(exception_id: ExceptionIdPath) -> list[AuditEvent]:
        return lifecycle.audit(exception_id)

    return app
