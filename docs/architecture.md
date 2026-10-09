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
| Persistence (SQLite, append-only audit/evidence/approvals) | `backend/app/store.py` | done |
| Lifecycle service (all state changes) | `backend/app/services/lifecycle.py` | done |
| Auth boundary (dev header only; GitLab identity planned) | `backend/app/auth.py`, `backend/app/config.py` | done (dev only) |
| FastAPI | `backend/app/api/main.py` | done |
| GitLab CI pipeline | `.gitlab-ci.yml` | done (simulated locally; real run pending) |
| GitLab API client (read-only) | `backend/app/integrations/gitlab.py` | done |
| CI evidence verification + ingestion | `backend/app/services/ci_evidence.py`, `backend/app/ingest_ci.py` | done |
| Duo MCP server (Path A: read-only + opt-in bounded tools as `agent:duo-mcp`) | `backend/app/duo/` | implemented; tested with MCP SDK client, not yet a real Duo session |
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
* Development trusts `local_sandbox` evidence by default (useful for the local
  demo); `GATEDEBT_EVIDENCE_TRUST=ci_only` switches it off, and production
  always trusts verified `gitlab_ci` evidence only.
* Dirty-tree evidence: flagged in the CLI report; excluded from decisions by
  the API lifecycle service (`EVIDENCE_UNCOMMITTED_SOURCE`).
* Local CLI rehearsal reports are not imported into the database; only
  API-run local rehearsals and server-verified CI reports are stored.

## GitLab CI evidence

### Trust model

A CI report is a JSON file a job wrote. Anyone who can edit the file, the
API request, or the database could claim anything, so none of those are
trusted. What *is* trusted:

1. **The GitLab API, reached with the server's own token** (HTTPS only; the
   token is never logged, never in `repr`, and stripped from cross-host
   redirects because artifact downloads may go through a CDN).
2. **The code on a trusted ref.** The report is produced by the rehearsal
   code at the pipeline's commit. Restricting `GATEDEBT_GITLAB_TRUSTED_REFS`
   to protected branches means only reviewed code can produce trusted reports.

### Verification (`verify_ci_evidence`)

| Check | Reason code on failure |
|---|---|
| project in allowlist | `CI_PROJECT_NOT_TRUSTED` (403, GitLab not even called) |
| pipeline exists / matches project | `CI_PIPELINE_NOT_FOUND`, `CI_PIPELINE_PROJECT_MISMATCH` |
| pipeline `success` | `CI_PIPELINE_NOT_SUCCESSFUL` |
| trusted ref, not a tag | `CI_REF_NOT_TRUSTED` |
| caller's expected commit | `CI_COMMIT_MISMATCH` |
| latest `rehearse:<scenario>` job in the pipeline | `CI_JOB_NOT_FOUND`, `CI_JOB_MISMATCH` |
| job `success`, same commit, has timestamps | `CI_JOB_NOT_SUCCESSFUL`, `CI_JOB_COMMIT_MISMATCH`, `CI_JOB_TIMESTAMPS_MISSING` |
| artifact present and a valid report | `CI_ARTIFACT_MISSING`, `CI_ARTIFACT_MALFORMED` |
| report source/pipeline/job/commit/project/scenario/exception | `CI_REPORT_*_MISMATCH` |
| report timestamps inside the job's run | `CI_REPORT_OUTSIDE_JOB_WINDOW` |
| existing policy (`evidence_rejections`) | `EVIDENCE_DIGEST_MISMATCH`, `EVIDENCE_TOO_OLD`, `EVIDENCE_FROM_FUTURE`, ... |
| verdict recomputed (`rehearsal_failures`) | `CI_REHEARSAL_FAILED` + check codes |
| GitLab unreachable / 5xx / auth error / not configured | `CI_VERIFICATION_UNAVAILABLE` / `..._NOT_CONFIGURED` (503) |

On success the evidence row and a `ci_verifications` row (project, pipeline,
job, ref, commit, URLs, who/when) are written in one transaction.
`UNIQUE(project_id, job_id)` plus a pre-check keep it to one record per job.
`ci_verifications` is append-only (migration 2).

Defence in depth: any stored `gitlab_ci` evidence **without** a
`ci_verifications` row is excluded from decisions
(`EVIDENCE_CI_PROVENANCE_UNVERIFIED`), so a row written straight into the
database cannot count.

### Who can trigger ingestion

| Path | Authentication | Use |
|---|---|---|
| `POST /exceptions/{id}/ci-evidence` | any identity from the configured `Authenticator` (today: dev header only; production: none → 401) | development / demo |
| `python -m app.ingest_ci` | shell access to the server; audited as `system:operator-cli` | production until real HTTP auth exists |

Ingestion only ever stores evidence that GitLab vouches for, so the caller's
identity controls *who may ask*, not *what is believed*.

## Storage

One authority: **SQLite** (stdlib `sqlite3`, no ORM, `backend/app/store.py`).
GitLab is a *source* of evidence (pipelines, artifacts) and a *target* for
proposals (issues/MRs), never a second copy of registry state. No YAML registry.

* **Schema versioning:** ordered migrations in `MIGRATIONS`, tracked with
  `PRAGMA user_version`, each applied in its own transaction. A database newer
  than the code is refused.
* **Append-only:** `evidence`, `approvals`, `decisions` (proposal snapshots),
  `verifications` and `audit_events` have `BEFORE UPDATE/DELETE` triggers that
  abort. `exceptions` rows cannot be deleted; they change only through the
  lifecycle service.
* **Transactions:** every write is one `BEGIN IMMEDIATE` transaction holding
  the state change and its audit events, so a crash leaves no partial write and
  concurrent read-check-write sequences serialise. Exception updates also carry
  an optimistic `WHERE status = ? AND updated_at = ?` guard.
* **Timestamps:** columns use fixed-width UTC (`2026-01-01T00:00:00.000000Z`) so
  text order equals time order; full records are stored as validated model JSON.
* **WAL mode** for concurrent readers alongside one writer.

## API and lifecycle service

Handlers in `app/api/main.py` only translate HTTP. `app/services/lifecycle.py`
is the single code path that changes state, and it delegates every rule to the
existing policy/decision engines:

* `GET /decision` calls `decision.evaluate()` and writes nothing.
* A proposal requires `evaluate()` to return `propose_retirement`, then moves
  `active → retirement_proposed` and stores the decision snapshot.
* A retirement approval re-runs `evaluate()` against the proposal's commit; if
  newer or aged-out evidence means it is no longer eligible, it is refused.
* Renewals use `renewal_issues()`; retirement uses `retirement_issues()` with
  the stored approval and the submitted verification.
* Every refusal (401/403/409/422 on approvals, proposals, verifications) is
  written to the audit trail. Audit details hold IDs, reason codes and auth
  method only — no headers, tokens or request bodies.

### Evidence provenance (beyond the policy engine)

Before evidence reaches `evaluate()`, stored source-state metadata is checked:
evidence from a working tree with uncommitted changes
(`EVIDENCE_UNCOMMITTED_SOURCE`) or unknown tree state for a local HEAD
(`EVIDENCE_SOURCE_STATE_UNKNOWN`) is excluded and reported in
`rejected_evidence`. Local rehearsals through the API always run against the
local git HEAD, and are refused entirely in production.

### Identity and authorisation

| | development | production |
|---|---|---|
| Authenticator | `DevHeaderAuthenticator` — trusts `X-GateDebt-Dev-User`, `verified=False` | none yet → every write 401 |
| Trusted evidence | `local_sandbox`, `gitlab_ci` | `gitlab_ci` only |
| Local rehearsals | allowed | 403 |
| Manual verification | allowed (human/CI actors) | 403 — must come from GitLab |
| Approvals | human actor in `GATEDEBT_APPROVERS` | also requires `verified=True` |

Always: agents and CI cannot approve (`APPROVER_MUST_BE_HUMAN`); the actor who
proposed a retirement cannot approve it (`SELF_APPROVAL_FORBIDDEN`); agents
cannot submit verifications. Configuring `dev-header` with `production` fails
at startup.

The future GitLab integration implements `Authenticator.authenticate(request)
-> Identity | None` with `verified=True`; nothing else needs to change.
