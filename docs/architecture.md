# GateDebt architecture

## Principle

Deterministic Python decides what is **permitted**. Agents (mock today, GitLab
Duo later) may investigate, explain and draft — they never decide, approve,
merge, or produce test results.

## Components

| Component | Location | Status |
|---|---|---|
| Models (Exception, Evidence, Decision, Approval, Verification, Audit) | `backend/app/models/` | done |
| Policy engine (expiry, completeness, transitions, evidence validation, renewal/retirement rules) | `backend/app/services/policy.py` | done |
| Decision engine (recommendation + reason codes) | `backend/app/services/decision.py` | done |
| Rehearsal engine (sandbox DB-migration, pipeline gate) + CLI | `backend/app/rehearsal/`, `scenarios/` | done |
| Persistence (SQLite, append-only audit/evidence/approvals) | `backend/app/store.py` | Milestone 4 |
| FastAPI | `backend/app/api/` | Milestone 4 |
| GitLab CI | `.gitlab-ci.yml` | Milestone 5 |
| Orchestration (provider-neutral interface + labelled mock coordinator) | `backend/app/orchestration/` | Milestone 6 |
| Dashboard (React + Vite, JavaScript) | `frontend/` | Milestone 7 |

## Exception lifecycle

```
active ──propose──▶ retirement_proposed ──human approve──▶ retirement_approved ──verified merge──▶ retired
   ▲                       │ human reject                        │ verification fails / revoked
   └───────────────────────┴─────────────────────────────────────┘
```

* There is no `active → retired` path. Tested in `test_policy.py`.
* Expiry (`active` / `due` / `expired`) is **computed** from `expires_at`, not
  stored, so it can never drift.
* Renewal does not change status; it needs a human `ApprovalRecord` with an
  explicit new expiry (≤ 90 days from now). Expiry is never extended silently.

## Evidence rules (fail closed)

An evidence record is first checked for **provenance** — any failure and it is
ignored entirely:

* belongs to this exception, approved scenario, matching mode
* `commit_sha` equals the commit being decided on (stale evidence rejected)
* finished within `max_evidence_age` (7 days) and not in the future
* trusted source (`local_sandbox`, `gitlab_ci`; `test_fixture` never trusted)
* SHA-256 digest matches content (tamper detection)
* CI evidence carries pipeline + job IDs

Then the **outcome** of the latest usable run per required scenario:

* injected failure was detected (`expected_injected`); an
  `unexpected_infrastructure` failure is *inconclusive*, never a pass
* recovery attempted, succeeded, all assertions passed
* cleanup verified
* every scenario check **and the waived check itself** passed

## Recommendations

| Situation | Recommendation | Human approval |
|---|---|---|
| Owner or remediation target missing | `investigate` | no |
| All required evidence passes | `propose_retirement` | yes |
| Infra failure / cleanup failed | `investigate` | no |
| Trusted evidence shows failure | `remediate` | yes |
| Expired, no usable evidence | `renewal_requires_approval` | yes |
| Due or repeatedly renewed, no evidence | `investigate` | no |
| Otherwise | `keep_open` | no |

## Rehearsal engine

`run_rehearsal()` (`backend/app/rehearsal/harness.py`):

1. Creates a fresh temporary directory; the scenario may only touch files in it.
2. Runs the scenario. Scenarios record what they **observed** through a
   `Recorder`; nothing is passing by default and a check that never ran is
   simply absent (→ `CHECK_MISSING`).
3. Any unplanned exception → `unexpected_infrastructure`. A scenario that ends
   without classifying its injected failure is also treated that way.
4. Always releases resources, removes the directory, then **verifies** both
   (directory gone, no open SQLite connections) → `verified` / `failed`.
5. Writes `rehearsal.log`, seals an `EvidenceRecord` (log SHA-256 as an
   artifact reference), and computes the verdict with the policy engine's own
   `rehearsal_failures()`, so the harness can never be more lenient than policy.
6. Writes `report.json` for passing **and** failing runs.

Each scenario declares the one waived check it genuinely executes
(`exercised_check`). Evidence only covers an exception whose `affected_check`
is that ID; otherwise the decision engine reports the check as missing.

### Known gaps (to resolve in later milestones)

* `waived_release_readiness_check` requires both scenarios **and** the
  exception's `affected_check` in each. The two scenarios exercise different
  checks, so no real evidence can currently satisfy that type. Needs a
  release-readiness scenario or a policy refinement.
* The default policy trusts `local_sandbox` evidence (useful for the local
  demo). A production policy should trust `gitlab_ci` only.
* `local_git_head` evidence from a dirty working tree is flagged in the report
  but not rejected by policy.

## Storage decision

One authority: **SQLite** (stdlib `sqlite3`, no ORM). Exceptions are mutable
rows guarded by the transition table; evidence, approvals and audit events are
insert-only. GitLab is a *source* of evidence (pipelines, artifacts) and a
*target* for proposals (issues/MRs), never a second copy of registry state.
