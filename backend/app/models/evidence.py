"""Rehearsal evidence: structured, traceable results of an actual execution."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .common import CommitSha, ExceptionId, ShortStr, UTCDateTime, canonical_digest


class RehearsalMode(StrEnum):
    SANDBOX = "sandbox"
    PIPELINE = "pipeline"


class EvidenceSource(StrEnum):
    """Where an evidence record was produced.

    LOCAL_SANDBOX and GITLAB_CI are real executions. TEST_FIXTURE exists only
    so unit tests can build records; the policy engine never trusts it.
    """

    LOCAL_SANDBOX = "local_sandbox"
    GITLAB_CI = "gitlab_ci"
    TEST_FIXTURE = "test_fixture"


class CheckOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class FailureClassification(StrEnum):
    # The rehearsal injected a known failure and it was detected as expected.
    EXPECTED_INJECTED = "expected_injected"
    # Something broke that the scenario did not plan for (runner died,
    # dependency missing, timeout...). Never counts as a passing rehearsal.
    UNEXPECTED_INFRASTRUCTURE = "unexpected_infrastructure"
    # The injected failure was not observed: the check under test is blind.
    INJECTED_NOT_DETECTED = "injected_not_detected"


class CleanupStatus(StrEnum):
    VERIFIED = "verified"
    FAILED = "failed"
    UNKNOWN = "unknown"


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: ShortStr
    outcome: CheckOutcome
    detail: Annotated[str, StringConstraints(max_length=2000)] = ""


class RecoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempted: bool
    succeeded: bool
    assertions: list[CheckResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> "RecoveryResult":
        if self.succeeded and not self.attempted:
            raise ValueError("recovery cannot succeed without being attempted")
        return self


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ShortStr
    # A local relative path or a GitLab job artifact URL.
    location: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None


class EvidenceRecord(BaseModel):
    """Immutable evidence record. ``digest`` seals the content at creation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, StringConstraints(pattern=r"^EVD-[A-Za-z0-9-]{1,64}$")]
    run_id: Annotated[str, StringConstraints(pattern=r"^RUN-[A-Za-z0-9-]{1,64}$")]
    exception_id: ExceptionId
    scenario_id: ShortStr
    mode: RehearsalMode
    source: EvidenceSource
    commit_sha: CommitSha
    pipeline_id: int | None = Field(default=None, ge=1)
    job_id: int | None = Field(default=None, ge=1)
    started_at: UTCDateTime
    finished_at: UTCDateTime
    injected_failure_classification: FailureClassification
    check_results: list[CheckResult]
    recovery: RecoveryResult | None
    cleanup_status: CleanupStatus
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def _timestamps(self) -> "EvidenceRecord":
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not be before started_at")
        return self

    def content_payload(self) -> dict:
        return self.model_dump(mode="json", exclude={"digest"})

    def compute_digest(self) -> str:
        return canonical_digest(self.content_payload())

    def digest_matches(self) -> bool:
        return self.compute_digest() == self.digest

    @classmethod
    def seal(cls, **fields) -> "EvidenceRecord":
        """Build a record and compute its digest from the content."""
        draft = cls.model_validate({**fields, "digest": "0" * 64})
        return draft.model_copy(update={"digest": draft.compute_digest()})
