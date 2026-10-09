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
| 2. Deterministic backend (models, policy, evidence validation, decisions) | done — 77 tests |
| 3. Rehearsal scenarios (sandbox + pipeline) | next |
| 4. API & SQLite persistence | planned |
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

## Docs

* [Architecture, lifecycle and evidence rules](docs/architecture.md)

## License

MIT — see [LICENSE](LICENSE).
