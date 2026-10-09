# GateDebt

Supervised DevSecOps exception-lifecycle and recovery-validation platform.

Teams waive tests and quality gates to unblock releases, then forget them.
GateDebt tracks those waivers, rehearses the failure they hide, collects
machine-verifiable evidence from real executions, and only lets a human retire
the waiver once the remediation is merged and verified.

> An AI saying "it's safe" is never evidence. Policy decisions are deterministic
> Python; agents only investigate and draft.

## Status

| Milestone | State |
|---|---|
| 1. Inspect & plan | done |
| 2. Deterministic backend (models, policy, evidence validation, decisions) | done |
| 3. Rehearsal engine (sandbox + pipeline scenarios, CLI, sealed evidence) | done |
| 4. API & SQLite persistence | done |
| 5. GitLab CI/CD | next |
| 6. Orchestration (mock coordinator, Duo boundary) | planned |
| 7. React dashboard | planned |
| 8. End-to-end verification | planned |
| 9. Docs & demo script | planned |

GitLab Duo Agent Platform is **not** integrated yet (access pending). Nothing in
this repository claims otherwise.

## Local development

Requires Python 3.12+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements-dev.txt
cd backend && ../.venv/bin/pytest
```

## Running rehearsals

From `backend/`:

```bash
../.venv/bin/python -m app.rehearsal list
../.venv/bin/python -m app.rehearsal run db-migration-recovery  --exception-id EXC-001
../.venv/bin/python -m app.rehearsal run pipeline-gate-recovery --exception-id EXC-002
```

* Exit code `0` = rehearsal passed, `1` = rehearsal failed (report still
  written), `2` = invalid input/environment (nothing ran).
* Each run writes `artifacts/runs/<RUN-ID>/report.json` (sealed
  `EvidenceRecord` + verdict + log) and `rehearsal.log`. Git-ignored.
* Commit: `--commit <40-hex sha>`, otherwise the local `git rev-parse HEAD`
  (recorded as `commit_origin: local_git_head`, with a dirty-tree flag). With no
  git checkout and no `--commit`, the CLI refuses to run.
* Local runs are always labelled `source: local_sandbox` and say "not a GitLab
  CI run". `--source gitlab_ci` only works inside a CI job (`GITLAB_CI=true`)
  and takes the commit, pipeline and job IDs from GitLab's predefined
  variables.

| Scenario | Mode | Injected failure | Recovery | Waived check it exercises |
|---|---|---|---|---|
| `db-migration-recovery` | sandbox | migration 0042 adds a `NOT NULL` column without default → partially applied | restore backup, apply fixed migration in one transaction | `integration:migration_0042` |
| `pipeline-gate-recovery` | pipeline | build manifest has a stale lockfile digest → gate exits 3 `LOCK_DIGEST_MISMATCH` | regenerate digest, retry gate (max 1) | `quality-gate:dependency-lock` |

Fixtures and step-by-step descriptions: [`scenarios/`](scenarios/).

## Running the API

From `backend/`:

```bash
GATEDEBT_APPROVERS=user:bob ../.venv/bin/uvicorn --factory app.api.main:create_app --reload
# http://127.0.0.1:8000/docs for interactive OpenAPI docs
```

> **Local authentication is not authentication.** In development the API
> believes the `X-GateDebt-Dev-User: user:alice` header. It exists to exercise
> the approval rules locally. `GATEDEBT_ENV=production` refuses that mode; until
> a real identity provider (GitLab OAuth/OIDC) implements the `Authenticator`
> interface in `app/auth.py`, production rejects every write with 401.

| Method & path | Purpose | Identity |
|---|---|---|
| `GET /health` | status, environment, auth mode, trusted evidence sources | – |
| `POST /exceptions` | register an exception (201; 409 duplicate; 422 invalid) | any |
| `GET /exceptions[?status=]` | list, with computed `expiry_state` | – |
| `GET /exceptions/{id}` | one exception | – |
| `GET /exceptions/{id}/evidence` | stored evidence + provenance issues | – |
| `GET /exceptions/{id}/decision[?commit=]` | **read-only** recommendation (dev default commit: local HEAD) | – |
| `POST /exceptions/{id}/rehearsals` | run an approved scenario locally (dev only) | any |
| `POST /exceptions/{id}/retirement-proposal` | propose retirement if policy says eligible | any (agents allowed) |
| `POST /exceptions/{id}/approvals` | approve/reject retirement, or renewal | human approver |
| `GET /exceptions/{id}/approvals` | approval history | – |
| `POST /exceptions/{id}/verifications` | post-merge verification → `retired` only if policy passes (dev only) | human or CI |
| `GET /exceptions/{id}/audit` | append-only audit trail | – |

Errors return `{"detail": {"reason_codes": [...]}}` with 401 (no identity),
403 (identity not allowed), 404, 409 (state conflict / not eligible) or 422.

Example retirement flow:

```bash
H='content-type: application/json'
curl -X POST localhost:8000/exceptions -H "$H" -H 'X-GateDebt-Dev-User: user:alice' -d '{
  "id":"EXC-001","project":"demo/payments","type":"skipped_integration_test",
  "title":"Skip migration 0042 test","reason":"Blocked 2.3 release","owner":"alice",
  "expires_at":"2026-12-01T00:00:00Z","affected_check":"integration:migration_0042",
  "remediation_target":"Fix migration 0042"}'
curl -X POST localhost:8000/exceptions/EXC-001/rehearsals -H "$H" -H 'X-GateDebt-Dev-User: user:alice' \
  -d '{"scenario_id":"db-migration-recovery"}'
curl localhost:8000/exceptions/EXC-001/decision
curl -X POST localhost:8000/exceptions/EXC-001/retirement-proposal -H "$H" -H 'X-GateDebt-Dev-User: agent:mock' -d '{}'
curl -X POST localhost:8000/exceptions/EXC-001/approvals -H "$H" -H 'X-GateDebt-Dev-User: user:bob' \
  -d '{"kind":"retirement","decision":"approved","comment":"Evidence reviewed"}'
curl -X POST localhost:8000/exceptions/EXC-001/verifications -H "$H" -H 'X-GateDebt-Dev-User: user:bob' \
  -d '{"merged_commit_sha":"<40-hex>","change_merged":true,"waiver_still_present":false,"pipeline_status":"success"}'
```

Rehearsals run against the local git `HEAD`. If the working tree has
uncommitted changes, the evidence is stored but flagged
`EVIDENCE_UNCOMMITTED_SOURCE` and cannot support retirement. Commit first.

## Docs

* [Architecture, lifecycle and evidence rules](docs/architecture.md)

## License

MIT — see [LICENSE](LICENSE).
