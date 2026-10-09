"""Exception lifecycle service: the only code path that changes stored state.

API handlers call this; this calls the existing policy and decision engines.
Every state change happens inside one ``BEGIN IMMEDIATE`` transaction together
with its audit events, so a failure leaves no partial writes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import ValidationError

from app.auth import Identity, approval_denial
from app.config import Settings
from app.models import (
    ApprovalDecision,
    ApprovalKind,
    ApprovalRecord,
    DecisionReport,
    EvidenceSource,
    ExceptionCreate,
    ExceptionRecord,
    ExceptionStatus,
    Recommendation,
    RetirementVerification,
    utcnow,
)
from app.rehearsal import SCENARIOS, RehearsalReport, RunContext, run_rehearsal
from app.services.ci_evidence import CiEvidenceRejected, verify_ci_evidence
from app.services.decision import evaluate
from app.services.policy import (
    REQUIRED_SCENARIOS,
    PolicyViolation,
    check_transition,
    evidence_rejections,
    renewal_issues,
    retirement_issues,
)
from app.store import DuplicateError, StaleWriteError, Store, new_id

PROPOSAL = "retirement_proposal"


class DomainError(Exception):
    status_code = 400

    def __init__(self, *reason_codes: str):
        self.reason_codes = list(reason_codes)
        super().__init__(", ".join(self.reason_codes))


class NotFound(DomainError):
    status_code = 404


class Unauthenticated(DomainError):
    status_code = 401


class Forbidden(DomainError):
    status_code = 403


class Conflict(DomainError):
    status_code = 409


class Invalid(DomainError):
    status_code = 422


class Unavailable(DomainError):
    status_code = 503


def provenance_issues(
    commit_origin: str,
    working_tree_dirty: bool | None,
    source: EvidenceSource | None = None,
    ci_verified: bool = False,
) -> list[str]:
    """Source-state rules for stored evidence, applied before the policy engine.

    A run from a working tree with uncommitted changes did not execute the
    commit it names, so it cannot count as evidence for that commit. Evidence
    labelled ``gitlab_ci`` counts only if this server verified it with GitLab.
    """
    if source == EvidenceSource.GITLAB_CI and not ci_verified:
        return ["EVIDENCE_CI_PROVENANCE_UNVERIFIED"]
    if working_tree_dirty:
        return ["EVIDENCE_UNCOMMITTED_SOURCE"]
    if commit_origin == "local_git_head" and working_tree_dirty is None:
        return ["EVIDENCE_SOURCE_STATE_UNKNOWN"]
    return []


def _row_issues(row: dict[str, Any]) -> list[str]:
    return provenance_issues(
        row["commit_origin"], row["working_tree_dirty"], row["evidence"].source, row["ci_verification"] is not None
    )


class Lifecycle:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _require_identity(identity: Identity | None) -> Identity:
        if identity is None:
            raise Unauthenticated("AUTHENTICATION_REQUIRED")
        return identity

    def _get(self, conn, exception_id: str) -> ExceptionRecord:
        exc = self.store.get_exception(conn, exception_id)
        if exc is None:
            raise NotFound("EXCEPTION_NOT_FOUND")
        return exc

    def _resolve_commit(self, commit: str | None) -> str:
        if commit:
            return commit
        if self.settings.is_production:
            raise Invalid("COMMIT_REQUIRED")
        sha = self.settings.repo_state().commit_sha
        if sha is None:
            raise Conflict("COMMIT_UNAVAILABLE")
        return sha

    def _deny(self, actor: str, action: str, exception_id: str | None, error: DomainError) -> DomainError:
        """Record a refused attempt in its own transaction, return the error to raise."""
        with self.store.transaction() as conn:
            if exception_id and self.store.get_exception(conn, exception_id) is None:
                exception_id = None
            self.store.append_audit(conn, actor, action, exception_id, {"reason_codes": error.reason_codes})
        return error

    def _save(self, conn, old: ExceptionRecord, identity: Identity, now: datetime, **changes: Any) -> ExceptionRecord:
        target = changes.get("status", old.status)
        if target != old.status:
            try:
                check_transition(old.status, target)
            except PolicyViolation as err:
                raise Conflict(*err.reason_codes) from err
        try:
            new = ExceptionRecord.model_validate(
                {**old.model_dump(), **changes, "updated_at": now, "updated_by": identity.actor}
            )
            self.store.update_exception(conn, new, old.status, old.updated_at)
        except ValidationError as err:
            raise Invalid("INVALID_EXCEPTION_UPDATE") from err
        except StaleWriteError as err:
            raise Conflict("CONCURRENT_MODIFICATION") from err
        if target != old.status:
            self.store.append_audit(
                conn, identity.actor, "exception.status_changed", old.id,
                {"from": old.status.value, "to": target.value},
            )
        return new

    def _decide(self, conn, exc: ExceptionRecord, commit: str, now: datetime) -> DecisionReport:
        usable, excluded = [], {}
        for row in self.store.list_evidence(conn, exc.id):
            issues = _row_issues(row)
            if issues:
                # Report every problem, including the policy engine's own.
                policy_reasons = evidence_rejections(row["evidence"], exc, commit, now, self.settings.policy)
                excluded[row["evidence"].id] = [*issues, *(r for r in policy_reasons if r not in issues)]
            else:
                usable.append(row["evidence"])
        try:
            report = evaluate(exc, usable, commit, now, self.settings.policy)
        except ValueError as err:
            raise Conflict("EXCEPTION_RETIRED") from err
        if not excluded:
            return report
        reasons = report.reason_codes if "EVIDENCE_REJECTED" in report.reason_codes else [
            *report.reason_codes, "EVIDENCE_REJECTED"]
        return report.model_copy(
            update={"rejected_evidence": {**report.rejected_evidence, **excluded}, "reason_codes": reasons}
        )

    # ------------------------------------------------------------------ reads

    def get(self, exception_id: str) -> ExceptionRecord:
        with self.store.reader() as conn:
            return self._get(conn, exception_id)

    def list(self, status: ExceptionStatus | None = None) -> list[ExceptionRecord]:
        with self.store.reader() as conn:
            return self.store.list_exceptions(conn, status)

    def evidence(self, exception_id: str) -> list[dict[str, Any]]:
        with self.store.reader() as conn:
            self._get(conn, exception_id)
            rows = self.store.list_evidence(conn, exception_id)
        for row in rows:
            row["provenance_issues"] = _row_issues(row)
        return rows

    def approvals(self, exception_id: str) -> list[ApprovalRecord]:
        with self.store.reader() as conn:
            self._get(conn, exception_id)
            return self.store.list_approvals(conn, exception_id)

    def audit(self, exception_id: str):
        with self.store.reader() as conn:
            self._get(conn, exception_id)
            return self.store.list_audit(conn, exception_id)

    def decision(self, exception_id: str, commit: str | None) -> DecisionReport:
        """Read-only: computes a recommendation, never changes state."""
        commit = self._resolve_commit(commit)
        with self.store.reader() as conn:
            return self._decide(conn, self._get(conn, exception_id), commit, utcnow())

    # ----------------------------------------------------------------- writes

    def create(self, data: ExceptionCreate, identity: Identity | None) -> ExceptionRecord:
        identity = self._require_identity(identity)
        now = utcnow()
        try:
            exc = ExceptionRecord(
                **data.model_dump(), created_at=now, created_by=identity.actor, updated_at=now, updated_by=identity.actor
            )
        except ValidationError as err:
            raise Invalid(*(e["msg"] for e in err.errors())) from err
        with self.store.transaction() as conn:
            try:
                self.store.insert_exception(conn, exc)
            except DuplicateError as err:
                raise Conflict("EXCEPTION_ID_EXISTS") from err
            self.store.append_audit(
                conn, identity.actor, "exception.created", exc.id,
                {"type": exc.type.value, "owner": exc.owner, "expires_at": exc.expires_at.isoformat(),
                 "auth_method": identity.method},
            )
        return exc

    def rehearse(self, exception_id: str, scenario_id: str, identity: Identity | None) -> tuple[RehearsalReport, list[str]]:
        identity = self._require_identity(identity)
        if self.settings.is_production:
            # Production evidence must come from GitLab CI, not this server.
            raise Forbidden("LOCAL_REHEARSALS_DISABLED_IN_PRODUCTION")
        scenario = SCENARIOS.get(scenario_id)
        if scenario is None:
            raise Invalid("UNKNOWN_SCENARIO")
        exc = self.get(exception_id)
        if exc.status == ExceptionStatus.RETIRED:
            raise Conflict("EXCEPTION_RETIRED")
        if scenario.requirement not in REQUIRED_SCENARIOS[exc.type]:
            raise Invalid("SCENARIO_NOT_REQUIRED_FOR_EXCEPTION_TYPE")
        state = self.settings.repo_state()
        if state.commit_sha is None:
            raise Conflict("COMMIT_UNAVAILABLE")

        ctx = RunContext(exc.id, state.commit_sha, "local_git_head", EvidenceSource.LOCAL_SANDBOX,
                         working_tree_dirty=state.working_tree_dirty)
        report, _ = run_rehearsal(scenario, ctx, self.settings.artifacts_dir)
        evidence = report.evidence
        if not evidence.digest_matches():
            raise Conflict("EVIDENCE_DIGEST_MISMATCH")
        issues = provenance_issues(report.commit_origin, report.working_tree_dirty)
        with self.store.transaction() as conn:
            self._get(conn, exc.id)
            self.store.insert_evidence(
                conn, evidence, commit_origin=report.commit_origin,
                working_tree_dirty=report.working_tree_dirty, recorded_by=identity.actor,
            )
            self.store.append_audit(
                conn, identity.actor, "evidence.recorded", exc.id,
                {"evidence_id": evidence.id, "run_id": evidence.run_id, "scenario_id": evidence.scenario_id,
                 "source": evidence.source.value, "commit_sha": evidence.commit_sha, "verdict": report.verdict,
                 "provenance_issues": issues},
            )
        return report, issues

    def ingest_ci_evidence(
        self,
        exception_id: str,
        identity: Identity | None,
        *,
        pipeline_id: int,
        scenario_id: str,
        project_id: int | None = None,
        job_id: int | None = None,
        expected_commit: str | None = None,
    ) -> dict[str, Any]:
        """Fetch and verify a CI rehearsal report from GitLab, then store it.

        The caller only points at a pipeline; every fact is re-read from the
        GitLab API with the server's token. Storing evidence never changes the
        exception's status.
        """
        identity = self._require_identity(identity)
        settings = self.settings
        if not settings.ci_verification_configured:
            raise Unavailable("CI_VERIFICATION_NOT_CONFIGURED")
        if project_id is None:
            if len(settings.gitlab_project_ids) != 1:
                raise Invalid("CI_PROJECT_ID_REQUIRED")
            project_id = next(iter(settings.gitlab_project_ids))
        exc = self.get(exception_id)
        if exc.status == ExceptionStatus.RETIRED:
            raise Conflict("EXCEPTION_RETIRED")
        scenario = SCENARIOS.get(scenario_id)
        if scenario is None:
            raise Invalid("UNKNOWN_SCENARIO")
        if scenario.requirement not in REQUIRED_SCENARIOS[exc.type]:
            raise Invalid("SCENARIO_NOT_REQUIRED_FOR_EXCEPTION_TYPE")

        attempt = {"project_id": project_id, "pipeline_id": pipeline_id, "scenario_id": scenario_id,
                   "job_id": job_id}
        client = settings.gitlab_client()
        if client is None:
            raise self._deny(identity.actor, "evidence.ci_rejected", exc.id,
                             Unavailable("CI_VERIFICATION_NOT_CONFIGURED"))
        try:
            verified = verify_ci_evidence(
                client, project_id=project_id, pipeline_id=pipeline_id, scenario_id=scenario_id, exception=exc,
                trusted_projects=settings.gitlab_project_ids, trusted_refs=settings.gitlab_trusted_refs,
                policy=settings.policy, now=utcnow(), job_id=job_id, expected_commit=expected_commit,
            )
        except CiEvidenceRejected as err:
            if err.unavailable:
                error: DomainError = Unavailable(*err.reason_codes)
            elif "CI_PROJECT_NOT_TRUSTED" in err.reason_codes:
                error = Forbidden(*err.reason_codes)
            else:
                error = Invalid(*err.reason_codes)
            with self.store.transaction() as conn:
                self.store.append_audit(conn, identity.actor, "evidence.ci_rejected", exc.id,
                                        {**attempt, "reason_codes": error.reason_codes})
            raise error from None

        evidence, provenance = verified.evidence, verified.provenance
        try:
            with self.store.transaction() as conn:
                self._get(conn, exc.id)
                if self.store.ci_job_ingested(conn, provenance.project_id, provenance.job_id):
                    raise DuplicateError(f"CI job {provenance.job_id} already ingested")
                # Evidence and its CI verification commit together or not at all.
                self.store.insert_evidence(conn, evidence, commit_origin="gitlab_ci", working_tree_dirty=False,
                                           recorded_by=identity.actor)
                self.store.insert_ci_verification(conn, exc.id, evidence.id, provenance, identity.actor)
                self.store.append_audit(conn, identity.actor, "evidence.ci_ingested", exc.id, {
                    "evidence_id": evidence.id, "project_id": provenance.project_id,
                    "pipeline_id": provenance.pipeline_id, "job_id": provenance.job_id, "ref": provenance.ref,
                    "commit_sha": provenance.commit_sha, "scenario_id": scenario_id,
                })
        except DuplicateError:
            raise self._deny(identity.actor, "evidence.ci_rejected", exc.id,
                             Conflict("CI_EVIDENCE_DUPLICATE")) from None
        [row] = [r for r in self.evidence(exc.id) if r["evidence"].id == evidence.id]
        return row

    def propose_retirement(self, exception_id: str, commit: str | None, identity: Identity | None) -> DecisionReport:
        identity = self._require_identity(identity)
        commit = self._resolve_commit(commit)
        now = utcnow()
        with self.store.transaction() as conn:
            exc = self._get(conn, exception_id)
            report = self._decide(conn, exc, commit, now)
            refused = None
            if exc.status != ExceptionStatus.ACTIVE:
                refused = Conflict(f"TRANSITION_FORBIDDEN:{exc.status.value}->{ExceptionStatus.RETIREMENT_PROPOSED.value}")
            elif report.recommendation != Recommendation.PROPOSE_RETIREMENT:
                refused = Conflict("NOT_ELIGIBLE_FOR_RETIREMENT", *report.reason_codes)
            if refused:
                self.store.append_audit(conn, identity.actor, "retirement.proposal_refused", exc.id,
                                        {"reason_codes": refused.reason_codes, "commit_sha": commit})
            else:
                self.store.insert_decision(conn, new_id("DEC"), PROPOSAL, report, identity.actor)
                self._save(conn, exc, identity, now, status=ExceptionStatus.RETIREMENT_PROPOSED)
                self.store.append_audit(conn, identity.actor, "retirement.proposed", exc.id,
                                        {"commit_sha": commit, "evidence_ids": report.supporting_evidence_ids})
        if refused:
            raise refused
        return report

    def approve(
        self,
        exception_id: str,
        kind: ApprovalKind,
        decision: ApprovalDecision,
        comment: str,
        identity: Identity | None,
        new_expires_at: datetime | None = None,
        commit: str | None = None,
    ) -> tuple[ApprovalRecord, ExceptionRecord]:
        if identity is None:
            raise self._deny("system:api", "approval.denied", exception_id, Unauthenticated("AUTHENTICATION_REQUIRED"))
        denial = approval_denial(identity, self.settings)
        if denial:
            raise self._deny(identity.actor, "approval.denied", exception_id, Forbidden(denial))
        if kind == ApprovalKind.RETIREMENT and new_expires_at is not None:
            raise Invalid("NEW_EXPIRY_ONLY_FOR_RENEWAL")
        if kind == ApprovalKind.RENEWAL and decision == ApprovalDecision.APPROVED and new_expires_at is None:
            raise Invalid("RENEWAL_EXPIRY_MISSING")
        if kind == ApprovalKind.RENEWAL:
            commit = self._resolve_commit(commit)

        now = utcnow()
        refused: DomainError | None = None
        with self.store.transaction() as conn:
            exc = self._get(conn, exception_id)
            changes: dict[str, Any] = {}
            if kind == ApprovalKind.RETIREMENT:
                proposal = self.store.latest_decision(conn, exc.id, PROPOSAL)
                if exc.status != ExceptionStatus.RETIREMENT_PROPOSED or proposal is None:
                    refused = Conflict("RETIREMENT_NOT_PROPOSED")
                elif proposal[1] == identity.actor:
                    refused = Forbidden("SELF_APPROVAL_FORBIDDEN")
                else:
                    report = proposal[0]
                    approval = ApprovalRecord(
                        id=new_id("APR"), exception_id=exc.id, kind=kind, decision=decision,
                        approver=identity.actor, comment=comment, decided_at=now,
                        commit_sha=report.commit_sha, evidence_ids=report.supporting_evidence_ids,
                    )
                    if decision == ApprovalDecision.APPROVED:
                        # Re-check: evidence may have aged out since the proposal.
                        recheck = self._decide(conn, exc, report.commit_sha, now)
                        if recheck.recommendation != Recommendation.PROPOSE_RETIREMENT:
                            refused = Conflict("PROPOSAL_NO_LONGER_VALID", *recheck.reason_codes)
                        changes["status"] = ExceptionStatus.RETIREMENT_APPROVED
                    else:
                        changes["status"] = ExceptionStatus.ACTIVE
            else:
                if exc.status != ExceptionStatus.ACTIVE:
                    refused = Conflict("RENEWAL_REQUIRES_ACTIVE_EXCEPTION")
                else:
                    approval = ApprovalRecord(
                        id=new_id("APR"), exception_id=exc.id, kind=kind, decision=decision,
                        approver=identity.actor, comment=comment, decided_at=now,
                        commit_sha=commit, new_expires_at=new_expires_at,
                    )
                    if decision == ApprovalDecision.APPROVED:
                        issues = renewal_issues(exc, approval, now)
                        if issues:
                            refused = Invalid(*issues)
                        changes = {"expires_at": new_expires_at, "renewal_count": exc.renewal_count + 1}

            if refused:
                self.store.append_audit(conn, identity.actor, "approval.refused", exc.id,
                                        {"kind": kind.value, "decision": decision.value,
                                         "reason_codes": refused.reason_codes})
            else:
                self.store.insert_approval(conn, approval, identity.method)
                self.store.append_audit(conn, identity.actor, "approval.recorded", exc.id,
                                        {"approval_id": approval.id, "kind": kind.value, "decision": decision.value,
                                         "auth_method": identity.method, "verified_identity": identity.verified})
                if changes:
                    exc = self._save(conn, exc, identity, now, **changes)
                    if kind == ApprovalKind.RENEWAL:
                        self.store.append_audit(conn, identity.actor, "exception.renewed", exc.id,
                                                {"expires_at": exc.expires_at.isoformat(),
                                                 "renewal_count": exc.renewal_count})
        if refused:
            raise refused
        return approval, exc

    def verify_retirement(
        self,
        exception_id: str,
        identity: Identity | None,
        *,
        merged_commit_sha: str,
        merge_request: str | None,
        change_merged: bool,
        waiver_still_present: bool,
        pipeline_status: str,
    ) -> tuple[str, list[str], ExceptionRecord]:
        identity = self._require_identity(identity)
        if self.settings.is_production:
            # In production this observation must be read from GitLab, not typed in.
            raise Forbidden("VERIFICATION_REQUIRES_GITLAB_INTEGRATION")
        if identity.actor.startswith("agent:"):
            raise Forbidden("AGENTS_CANNOT_SUBMIT_VERIFICATION")
        now = utcnow()
        with self.store.transaction() as conn:
            exc = self._get(conn, exception_id)
            if exc.status != ExceptionStatus.RETIREMENT_APPROVED:
                refused = Conflict("RETIREMENT_NOT_APPROVED")
                self.store.append_audit(conn, identity.actor, "retirement.verification_refused", exc.id,
                                        {"reason_codes": refused.reason_codes})
            else:
                refused = None
                approvals = [
                    a for a in self.store.list_approvals(conn, exc.id)
                    if a.kind == ApprovalKind.RETIREMENT and a.decision == ApprovalDecision.APPROVED
                ]
                try:
                    verification = RetirementVerification(
                        exception_id=exc.id, merged_commit_sha=merged_commit_sha, merge_request=merge_request,
                        change_merged=change_merged, waiver_still_present=waiver_still_present,
                        pipeline_status=pipeline_status, verified_at=now, observed_by=identity.actor,
                    )
                except ValidationError as err:
                    raise Invalid("INVALID_VERIFICATION") from err
                issues = retirement_issues(exc, approvals[-1] if approvals else None, verification)
                verification_id = self.store.insert_verification(
                    conn, verification, f"manual:{identity.method}", issues
                )
                self.store.append_audit(conn, identity.actor, "retirement.verification_recorded", exc.id,
                                        {"verification_id": verification_id, "issues": issues,
                                         "observation_source": f"manual:{identity.method}"})
                if not issues:
                    exc = self._save(conn, exc, identity, now, status=ExceptionStatus.RETIRED)
        if refused:
            raise refused
        return verification_id, issues, exc
