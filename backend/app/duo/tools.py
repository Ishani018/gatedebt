"""GateDebt tools exposed to GitLab Duo (or any MCP client) as ``agent:duo-mcp``.

Everything here goes through the existing ``Lifecycle`` service, so every
policy, provenance and audit rule applies unchanged. The tool set is a closed
allowlist: no tool proposes, approves, renews, verifies, edits or creates
exceptions, and no argument is a free-form string, path, URL, command or query.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.auth import Identity
from app.models import ExceptionStatus, Recommendation, utcnow
from app.models.common import CommitSha, ExceptionId
from app.rehearsal import SCENARIOS
from app.services.lifecycle import Lifecycle
from app.services.policy import expiry_state

AGENT = Identity(actor="agent:duo-mcp", method="mcp-stdio", verified=False)

# Closed set of approved scenarios, rendered as a JSON-schema enum.
ScenarioId = StrEnum("ScenarioId", {s.replace("-", "_"): s for s in sorted(SCENARIOS)})

AGENT_MAY_NOT = [
    "approve or reject anything",
    "propose retirement",
    "renew or extend an exception",
    "change an exception's status",
    "create, edit or upload evidence",
    "submit post-merge verification",
]


# ------------------------------------------------------------------ inputs

class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListExceptionsArgs(_Args):
    status: ExceptionStatus | None = Field(default=None, description="Optional status filter.")


class ExceptionArgs(_Args):
    exception_id: ExceptionId = Field(description="Exception ID, e.g. EXC-001.")


class DecisionArgs(ExceptionArgs):
    commit_sha: CommitSha | None = Field(
        default=None,
        description="40-hex commit the decision is for. Defaults to the server's local HEAD in development.",
    )


class RehearsalArgs(ExceptionArgs):
    scenario_id: ScenarioId = Field(description="One of GateDebt's approved rehearsal scenarios.")


class CiEvidenceArgs(ExceptionArgs):
    pipeline_id: int = Field(ge=1, le=2**53, description="GitLab pipeline ID on the configured trusted project.")
    scenario_id: ScenarioId = Field(description="Scenario whose rehearse:<scenario> job to verify.")
    commit_sha: CommitSha | None = Field(default=None, description="Optional expected pipeline commit.")


# ----------------------------------------------------------------- briefs

_SIMPLE_REASONS = {
    "EXCEPTION_EXPIRED": "The waiver is past its expiry date.",
    "EXCEPTION_DUE": "The waiver expires within the due window.",
    "OWNER_MISSING": "No owner is recorded, so nobody is accountable for removing it.",
    "REMEDIATION_TARGET_MISSING": "No remediation target is recorded.",
    "REPEATEDLY_RENEWED": "The waiver has been renewed repeatedly.",
    "EVIDENCE_REJECTED": "Some stored evidence was rejected (see rejected_evidence); it does not count.",
    "ALL_REQUIRED_CHECKS_PASSED": "Every required rehearsal check passed on usable evidence for this commit.",
    "REHEARSAL_INCONCLUSIVE": "The latest rehearsal was inconclusive (infrastructure or cleanup problem).",
    "REHEARSAL_NEEDED": "A rehearsal should be run before the waiver expires.",
}
_PREFIX_REASONS = {
    "EVIDENCE_MISSING": "No usable evidence for scenario {0}.",
}
_SCENARIO_REASONS = {
    "CHECK_FAILED": "Scenario {0}: check {1} failed.",
    "CHECK_MISSING": "Scenario {0}: check {1} did not run.",
    "RECOVERY_MISSING": "Scenario {0}: no recovery was attempted.",
    "RECOVERY_FAILED": "Scenario {0}: the recovery procedure failed.",
    "RECOVERY_ASSERTION_FAILED": "Scenario {0}: recovery assertion {1} failed.",
    "CLEANUP_FAILED": "Scenario {0}: cleanup failed.",
    "CLEANUP_UNVERIFIED": "Scenario {0}: cleanup was not verified.",
    "UNEXPECTED_INFRASTRUCTURE_FAILURE": "Scenario {0}: an unexpected infrastructure failure occurred.",
    "INJECTED_FAILURE_NOT_DETECTED": "Scenario {0}: the injected failure was not detected.",
}


def explain_reason(code: str) -> str:
    """Fixed, template-based wording for a policy reason code (no LLM)."""
    if code in _SIMPLE_REASONS:
        return _SIMPLE_REASONS[code]
    head, _, rest = code.partition(":")
    if head in _PREFIX_REASONS:
        return _PREFIX_REASONS[head].format(rest)
    if head in SCENARIOS:
        inner, _, detail = rest.partition(":")
        if inner in _SCENARIO_REASONS:
            return _SCENARIO_REASONS[inner].format(head, detail)
    return f"Policy reason code {code}."


_NEXT_STEPS = {
    Recommendation.PROPOSE_RETIREMENT: [
        "A person (not this agent) may propose retirement in GateDebt.",
        "A human approver other than the proposer must approve it.",
        "The waiver-removal change must merge, and post-merge verification must pass, before it is retired.",
    ],
    Recommendation.REMEDIATE: ["Fix the failing checks, then re-run the rehearsal on the new commit."],
    Recommendation.INVESTIGATE: ["Resolve the listed issues (ownership, inconclusive runs, or missing rehearsals)."],
    Recommendation.RENEWAL_REQUIRES_APPROVAL: [
        "Either remediate, or a human approver renews the waiver with an explicit new expiry.",
    ],
    Recommendation.KEEP_OPEN: ["No action required yet; rehearse before the waiver becomes due."],
}


def retirement_brief(lifecycle: Lifecycle, exception_id: str, commit_sha: str | None) -> dict[str, Any]:
    """Explain GateDebt's deterministic decision. Read-only; never changes state."""
    exc = lifecycle.get(exception_id)
    decision = lifecycle.decision(exception_id, commit_sha)
    rows = lifecycle.evidence(exception_id)
    supporting = set(decision.supporting_evidence_ids)
    evidence = []
    for row in rows:
        ev = row["evidence"]
        evidence.append({
            "evidence_id": ev.id,
            "scenario_id": ev.scenario_id,
            "source": ev.source.value,
            "commit_sha": ev.commit_sha,
            "finished_at": ev.finished_at.isoformat(),
            "ci_verified": row["ci_verification"] is not None,
            "used_by_decision": ev.id in supporting,
            "rejected_because": decision.rejected_evidence.get(ev.id, []),
        })
    return {
        "notice": (
            "Generated by GateDebt from its deterministic policy engine. This is not an approval, "
            "and an agent's wording cannot change it."
        ),
        "exception": {
            "id": exc.id,
            "title": exc.title,
            "status": exc.status.value,
            "expiry_state": expiry_state(exc, utcnow(), lifecycle.settings.policy).value,
            "expires_at": exc.expires_at.isoformat(),
            "affected_check": exc.affected_check,
            "owner": exc.owner,
        },
        "recommendation": decision.recommendation.value,
        "eligible_for_retirement_proposal": decision.recommendation == Recommendation.PROPOSE_RETIREMENT,
        "requires_human_approval": decision.requires_human_approval,
        "commit_sha": decision.commit_sha,
        "explanation": [explain_reason(code) for code in decision.reason_codes],
        "reason_codes": decision.reason_codes,
        "failed_checks": decision.failed_checks,
        "missing_checks": decision.missing_checks,
        "risks": decision.risks,
        "evidence": evidence,
        "next_human_steps": _NEXT_STEPS[decision.recommendation],
        "agent_may_not": AGENT_MAY_NOT,
    }


# ------------------------------------------------------------------ tools

def _exception_view(lifecycle: Lifecycle, exc) -> dict[str, Any]:
    data = exc.model_dump(mode="json")
    data["expiry_state"] = expiry_state(exc, utcnow(), lifecycle.settings.policy).value
    return data


def _evidence_rows(lifecycle: Lifecycle, exception_id: str) -> dict[str, Any]:
    rows = []
    for row in lifecycle.evidence(exception_id):
        rows.append({
            "evidence": row["evidence"].model_dump(mode="json"),
            "commit_origin": row["commit_origin"],
            "working_tree_dirty": row["working_tree_dirty"],
            "provenance_issues": row["provenance_issues"],
            "ci_verification": row["ci_verification"],
            "recorded_by": row["recorded_by"],
        })
    return {"exception_id": exception_id, "evidence": rows}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args: type[_Args]
    read_only: bool
    handler: Callable[[Lifecycle, Any], dict[str, Any]]


READ_TOOLS = [
    ToolSpec(
        "list_exceptions",
        "List registered engineering exceptions (waivers) with their computed expiry state.",
        ListExceptionsArgs, True,
        lambda lc, a: {"exceptions": [_exception_view(lc, e) for e in lc.list(a.status)]},
    ),
    ToolSpec(
        "get_exception", "Get one exception by ID.", ExceptionArgs, True,
        lambda lc, a: _exception_view(lc, lc.get(a.exception_id)),
    ),
    ToolSpec(
        "get_evidence",
        "Get stored rehearsal evidence for an exception, with provenance issues and GitLab CI verification.",
        ExceptionArgs, True,
        lambda lc, a: _evidence_rows(lc, a.exception_id),
    ),
    ToolSpec(
        "get_decision",
        "Get GateDebt's deterministic, read-only recommendation for an exception at a commit.",
        DecisionArgs, True,
        lambda lc, a: lc.decision(a.exception_id, a.commit_sha).model_dump(mode="json"),
    ),
    ToolSpec(
        "retirement_brief",
        "Explain GateDebt's decision and evidence for retiring an exception, with required human steps. "
        "Read-only; it cannot approve or propose anything.",
        DecisionArgs, True,
        lambda lc, a: retirement_brief(lc, a.exception_id, a.commit_sha),
    ),
]


def _run_rehearsal(lc: Lifecycle, a: RehearsalArgs) -> dict[str, Any]:
    report, issues = lc.rehearse(a.exception_id, a.scenario_id.value, AGENT)
    return {
        "verdict": report.verdict,
        "failures": report.failures,
        "provenance_issues": issues,
        "execution_note": report.execution_note,
        "evidence_id": report.evidence.id,
        "note": "Evidence was recorded by GateDebt's harness. It does not change the exception's status.",
    }


def _ingest(lc: Lifecycle, a: CiEvidenceArgs) -> dict[str, Any]:
    row = lc.ingest_ci_evidence(
        a.exception_id, AGENT, pipeline_id=a.pipeline_id, scenario_id=a.scenario_id.value,
        expected_commit=a.commit_sha,
    )
    return {
        "evidence_id": row["evidence"].id,
        "ci_verification": row["ci_verification"],
        "provenance_issues": row["provenance_issues"],
        "note": "Verified server-side against the GitLab API. It does not change the exception's status.",
    }


STATEFUL_TOOLS = [
    ToolSpec(
        "run_rehearsal",
        "Run one approved recovery rehearsal locally in an isolated temporary sandbox (development only) "
        "and record its evidence. Requires human confirmation in the client.",
        RehearsalArgs, False, _run_rehearsal,
    ),
    ToolSpec(
        "ingest_ci_evidence",
        "Ask GateDebt to verify a GitLab CI rehearsal job via the GitLab API and record it as evidence. "
        "Requires human confirmation in the client.",
        CiEvidenceArgs, False, _ingest,
    ),
]


def tool_specs(enable_stateful: bool) -> list[ToolSpec]:
    return READ_TOOLS + (STATEFUL_TOOLS if enable_stateful else [])
