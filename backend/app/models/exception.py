"""The engineering exception (waiver) registry model."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import Actor, ExceptionId, NonEmptyStr, ShortStr, UTCDateTime

# An exception may never be granted for longer than this in one go.
# Longer-lived waivers must be renewed explicitly, with human approval.
MAX_EXCEPTION_WINDOW = timedelta(days=90)


class ExceptionType(StrEnum):
    SKIPPED_INTEGRATION_TEST = "skipped_integration_test"
    WAIVED_QUALITY_GATE = "waived_quality_gate"
    WAIVED_RELEASE_READINESS_CHECK = "waived_release_readiness_check"


class ExceptionStatus(StrEnum):
    ACTIVE = "active"
    RETIREMENT_PROPOSED = "retirement_proposed"
    RETIREMENT_APPROVED = "retirement_approved"
    RETIRED = "retired"


# Explicit lifecycle. Anything not listed here is a prohibited transition.
# Note there is no ACTIVE -> RETIRED shortcut: retirement always goes through
# a proposal, a human approval, and a verification of the merged change.
ALLOWED_TRANSITIONS: dict[ExceptionStatus, frozenset[ExceptionStatus]] = {
    ExceptionStatus.ACTIVE: frozenset({ExceptionStatus.RETIREMENT_PROPOSED}),
    ExceptionStatus.RETIREMENT_PROPOSED: frozenset(
        {ExceptionStatus.ACTIVE, ExceptionStatus.RETIREMENT_APPROVED}
    ),
    ExceptionStatus.RETIREMENT_APPROVED: frozenset(
        {ExceptionStatus.ACTIVE, ExceptionStatus.RETIRED}
    ),
    ExceptionStatus.RETIRED: frozenset(),
}


class ExceptionCreate(BaseModel):
    """Input for registering a new exception. Validated strictly."""

    model_config = ConfigDict(extra="forbid")

    id: ExceptionId
    project: ShortStr
    type: ExceptionType
    title: ShortStr
    reason: NonEmptyStr
    # Owner is optional at the schema level so unowned waivers found in the
    # wild can still be registered; the policy engine flags them.
    owner: ShortStr | None = None
    expires_at: UTCDateTime
    affected_check: ShortStr
    remediation_target: NonEmptyStr | None = None
    related_issue: ShortStr | None = None
    related_merge_request: ShortStr | None = None


class ExceptionRecord(ExceptionCreate):
    status: ExceptionStatus = ExceptionStatus.ACTIVE
    renewal_count: int = Field(default=0, ge=0)
    created_at: UTCDateTime
    created_by: Actor
    updated_at: UTCDateTime
    updated_by: Actor

    @model_validator(mode="after")
    def _check_timestamps(self) -> "ExceptionRecord":
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be before created_at")
        if self.renewal_count == 0 and self.expires_at - self.created_at > MAX_EXCEPTION_WINDOW:
            raise ValueError(
                f"expiry window exceeds maximum of {MAX_EXCEPTION_WINDOW.days} days"
            )
        return self
