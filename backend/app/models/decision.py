"""Decision reports, approvals, retirement verification and audit events."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .common import Actor, CommitSha, ExceptionId, NonEmptyStr, UTCDateTime


class Recommendation(StrEnum):
    INVESTIGATE = "investigate"
    KEEP_OPEN = "keep_open"
    REMEDIATE = "remediate"
    PROPOSE_RETIREMENT = "propose_retirement"
    RENEWAL_REQUIRES_APPROVAL = "renewal_requires_approval"


class ExpiryState(StrEnum):
    ACTIVE = "active"
    DUE = "due"
    EXPIRED = "expired"


class DecisionReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    exception_id: ExceptionId
    evaluated_at: UTCDateTime
    commit_sha: CommitSha
    expiry_state: ExpiryState
    recommendation: Recommendation
    reason_codes: list[str]
    supporting_evidence_ids: list[str]
    rejected_evidence: dict[str, list[str]] = Field(default_factory=dict)
    failed_checks: list[str]
    missing_checks: list[str]
    risks: list[str]
    requires_human_approval: bool


class ApprovalKind(StrEnum):
    RETIREMENT = "retirement"
    RENEWAL = "renewal"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, StringConstraints(pattern=r"^APR-[A-Za-z0-9-]{1,64}$")]
    exception_id: ExceptionId
    kind: ApprovalKind
    decision: ApprovalDecision
    # Approvals must come from a human; agents and CI cannot approve.
    approver: Annotated[str, StringConstraints(pattern=r"^user:[A-Za-z0-9._@-]{1,64}$")]
    comment: NonEmptyStr
    decided_at: UTCDateTime
    commit_sha: CommitSha
    evidence_ids: list[str] = Field(default_factory=list)
    # Only meaningful for renewals: the explicitly approved new expiry.
    new_expires_at: UTCDateTime | None = None


class RetirementVerification(BaseModel):
    """Observed repository/pipeline state after the remediation change merged."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    exception_id: ExceptionId
    merged_commit_sha: CommitSha
    merge_request: NonEmptyStr | None = None
    change_merged: bool
    waiver_still_present: bool
    pipeline_status: Annotated[str, StringConstraints(pattern=r"^(success|failed|canceled|running|pending|skipped|unknown)$")]
    verified_at: UTCDateTime
    observed_by: Actor


class AuditEvent(BaseModel):
    """Append-only record of a significant operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, StringConstraints(pattern=r"^AUD-[A-Za-z0-9-]{1,64}$")]
    occurred_at: UTCDateTime
    actor: Actor
    action: Annotated[str, StringConstraints(pattern=r"^[a-z_.]{1,64}$")]
    exception_id: ExceptionId | None = None
    details: dict[str, Any] = Field(default_factory=dict)
