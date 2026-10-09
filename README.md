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
| 4. API & SQLite persistence | next |
| 5. GitLab CI/CD | planned |
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

## Docs

* [Architecture, lifecycle and evidence rules](docs/architecture.md)

## License

MIT — see [LICENSE](LICENSE).
