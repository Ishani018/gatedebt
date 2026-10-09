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
| 5. GitLab CI/CD + verified CI evidence ingestion | done (pipeline simulated locally; first real GitLab run pending) |
| 6. GitLab Duo integration, Path A: local MCP server (read-only + bounded tools) | implemented and tested locally; not yet invoked from a real Duo client |
| 7. React dashboard | planned |
| 8. End-to-end verification | planned |
| 9. Docs & demo script | planned |

GitLab Duo: a GateDebt **MCP server** for Duo Agentic Chat / Duo CLI is implemented
(see [GitLab Duo (MCP)](#gitlab-duo-mcp)). It has been tested with the official MCP SDK
client, **not yet with a real Duo session**. No GitLab-hosted agent or flow exists yet.

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

## GitLab CI/CD

`.gitlab-ci.yml` runs on `python:3.12-slim` from a clean checkout:

| Stage | Job | What it does | Artifacts (30 days) |
|---|---|---|---|
| validate | `validate` | byte-compile, list approved scenarios, check fixture JSON | – |
| test | `test` | full pytest suite | JUnit (`junit-tests.xml`) |
| rehearse | `rehearse:db-migration-recovery` | real SQLite migration failure + recovery in the job's temp dir | `gatedebt-evidence/db-migration-recovery.json`, `artifacts/runs/` |
| rehearse | `rehearse:pipeline-gate-recovery` | real gate failure, classification, recovery, retry | `gatedebt-evidence/pipeline-gate-recovery.json`, `artifacts/runs/` |
| evaluate | `evidence-summary` | self-check of both reports; table in the job log | `gatedebt-summary/summary.json`, JUnit of every rehearsal check |

* All jobs are safe to run automatically: rehearsals only touch throwaway files
  inside the job container. The pipeline uses **no secrets**.
* Reports are published even when a rehearsal fails (`artifacts: when: always`),
  but the job fails, so the pipeline fails and the report can never be ingested.
* Each report records the GitLab project, pipeline, job, ref, commit and
  timestamps — as *self-reported* metadata. Trust comes from the server check below.
* Choose which exceptions the rehearsals are evidence for with the pipeline
  variables `GATEDEBT_DB_EXCEPTION_ID` (default `EXC-001`) and
  `GATEDEBT_PIPELINE_EXCEPTION_ID` (default `EXC-002`) under
  *Build → Pipelines → Run pipeline*.

### Ingesting CI evidence (server-verified)

```bash
# HTTP (development auth header shown)
curl -X POST localhost:8000/exceptions/EXC-001/ci-evidence -H 'content-type: application/json' \
  -H 'X-GateDebt-Dev-User: user:alice' -d '{"pipeline_id": 1234, "scenario_id": "db-migration-recovery"}'
# or on the server itself (production path; audited as system:operator-cli)
python -m app.ingest_ci --exception-id EXC-001 --pipeline-id 1234 --scenario db-migration-recovery
```

The caller supplies only **pointers** (pipeline ID, scenario, optionally project
ID / job ID / expected commit). Any other field — `source`, `status`, a report —
is rejected with 422. The server then, with its own read-only token:

1. requires the project to be in `GATEDEBT_GITLAB_PROJECT_IDS`;
2. reads the pipeline: same project, `status == success`, ref in
   `GATEDEBT_GITLAB_TRUSTED_REFS`, not a tag, commit matches `commit_sha` if given;
3. finds the latest `rehearse:<scenario>` job in that pipeline: `success`,
   same pipeline and commit;
4. downloads `gatedebt-evidence/<scenario>.json` from that job's artifacts;
5. checks the report: `source=gitlab_ci`, pipeline/job/commit/project/
   scenario/exception all match what GitLab said, timestamps inside the job's
   run window, digest valid, not stale/expired/future-dated (existing policy),
   and the verdict **recomputed** by the policy engine;
6. stores the evidence and its CI verification in one transaction (one record
   per CI job; duplicates → 409).

GitLab errors or missing configuration → **503**, nothing stored. Every
rejection is audited. Ingested evidence never changes an exception's status:
proposal, human approval and post-merge verification are still required.

| | Local rehearsal evidence | Verified GitLab CI evidence |
|---|---|---|
| Produced by | `POST /rehearsals` or the CLI on a developer machine | the `rehearse:*` jobs |
| Stored via | `POST /exceptions/{id}/rehearsals` (dev only) | `POST .../ci-evidence` or `python -m app.ingest_ci` |
| Commit binding | local `HEAD`; uncommitted changes → excluded | the pipeline's commit, as reported by GitLab |
| Counts when | `GATEDEBT_EVIDENCE_TRUST=local_and_ci` (dev default) | always (if verified) |
| Production | never trusted | the only trusted source |

### Putting it on GitLab (manual steps)

1. Create a **public** project on gitlab.com and push this branch as `main`:
   `git remote add gitlab git@gitlab.com:<you>/gatedebt.git && git push gitlab HEAD:main`
2. Keep `main` protected (Settings → Repository → Protected branches); only
   protected refs should be listed in `GATEDEBT_GITLAB_TRUSTED_REFS`, because
   anyone who can push to a trusted ref can change the rehearsal code.
3. Make sure a runner is available (Settings → CI/CD → Runners → instance
   runners; gitlab.com may ask you to verify your account first).
4. The pipeline runs on push. No CI/CD variables or secrets are needed.
5. For the server: create a token with only the `read_api` scope — a project
   access token with the Reporter role if your plan offers it, otherwise a
   personal access token with a short expiry. Set `GATEDEBT_GITLAB_URL`,
   `GATEDEBT_GITLAB_TOKEN`, `GATEDEBT_GITLAB_PROJECT_IDS` (the numeric ID on
   the project overview page) and `GATEDEBT_GITLAB_TRUSTED_REFS=main` in the
   server's environment. Never in the repository or `.gitlab-ci.yml`.

### End-to-end demo path

1. Push to GitLab; wait for the pipeline to go green (5 jobs).
2. Start the API with `GATEDEBT_EVIDENCE_TRUST=ci_only`, the GitLab settings
   above and `GATEDEBT_APPROVERS=user:bob`.
3. Create `EXC-001` (`affected_check: integration:migration_0042`).
4. `GET /exceptions/EXC-001/decision?commit=<pipeline sha>` → `keep_open`.
5. `POST /exceptions/EXC-001/ci-evidence` with the pipeline ID → 201, linked to
   the job URL. Decision → `propose_retirement`; status still `active`.
6. Propose as `agent:mock`, try to approve as the agent (403), approve as
   `user:bob`, submit a verification with the waiver still present (not
   retired), then a clean one → `retired`. `GET /audit` shows every step.

## GitLab Duo (MCP)

`backend/app/duo/` is a local **stdio MCP server** (official `mcp` Python SDK)
that GitLab Duo Agentic Chat (VS Code, JetBrains) or the GitLab Duo CLI starts
from `.gitlab/duo/mcp.json`. It acts as the fixed identity `agent:duo-mcp`
against your local GateDebt database.

| Tool | Kind | Notes |
|---|---|---|
| `list_exceptions`, `get_exception`, `get_evidence`, `get_decision` | read-only | same data and rules as the HTTP API |
| `retirement_brief` | read-only | GateDebt's deterministic decision + template-based explanation, evidence used/rejected, next **human** steps |
| `run_rehearsal` | stateful, opt-in | approved scenario enum only; development only; dirty tree → does not qualify |
| `ingest_ci_evidence` | stateful, opt-in | pointers only; existing server-side GitLab verification |

Not available to the agent at all: propose, approve, reject, renew, verify,
create/edit exceptions, free-text, paths, URLs, commands or SQL. Every input
schema is closed (`additionalProperties: false`), every string argument is a
pattern or an enum, internal errors are never shown to the model, and every
call is audited as `agent.tool_called`.

Set up (on your machine):

```bash
cp .gitlab/duo/mcp.json.example .gitlab/duo/mcp.json   # git-ignored
# edit the three /ABSOLUTE/PATH/TO/gatedebt placeholders
# optional: add "--enable-stateful-tools" to "args" to expose run_rehearsal / ingest_ci_evidence
```

Then in the top-level group: *Settings → GitLab Duo → Change configuration →
Allow external MCP tools*, restart the IDE/Duo CLI, open Agentic Chat in this
workspace and ask e.g. *"Use GateDebt to explain whether EXC-001 can be retired."*
Read tools are pre-approved in the example config; stateful tools always ask
you to confirm each call.

## Docs

* [Architecture, lifecycle and evidence rules](docs/architecture.md)
* [GitLab Duo integration](docs/duo-integration.md)

## License

MIT — see [LICENSE](LICENSE).
