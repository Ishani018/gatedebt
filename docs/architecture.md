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
| Rehearsal engine (sandbox DB-migration, pipeline gate) | `backend/app/rehearsal/`, `scenarios/` | Milestone 3 |
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

## Storage decision

One authority: **SQLite** (stdlib `sqlite3`, no ORM). Exceptions are mutable
rows guarded by the transition table; evidence, approvals and audit events are
insert-only. GitLab is a *source* of evidence (pipelines, artifacts) and a
*target* for proposals (issues/MRs), never a second copy of registry state.
