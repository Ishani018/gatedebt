"""Operator command: verify and ingest a GitLab CI rehearsal report.

    python -m app.ingest_ci --exception-id EXC-001 --pipeline-id 1234 \\
        --scenario db-migration-recovery [--project-id 31] [--job-id 5678] [--commit SHA]

Runs on the GateDebt server with its own settings (GATEDEBT_* environment,
including the GitLab token). The operator is authenticated by having shell
access to that host; the action is audited as ``system:operator-cli``. This is
the production ingestion path until a real HTTP identity provider exists.

Exit codes: 0 ingested, 1 rejected, 2 configuration/usage error, 3 GitLab unavailable.
"""

from __future__ import annotations

import argparse
import re
import sys

from app.auth import Identity
from app.config import ConfigError, Settings
from app.services.lifecycle import DomainError, Lifecycle, Unavailable
from app.store import Store

OPERATOR = Identity(actor="system:operator-cli", method="server-shell", verified=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ingest_ci", description=__doc__.split("\n\n")[0])
    parser.add_argument("--exception-id", required=True)
    parser.add_argument("--pipeline-id", type=int, required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--project-id", type=int)
    parser.add_argument("--job-id", type=int)
    parser.add_argument("--commit")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if args.commit is not None and not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        print("error: --commit must be a 40-character lowercase hex SHA", file=sys.stderr)
        return 2
    try:
        settings = Settings.from_env()
    except ConfigError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    lifecycle = Lifecycle(Store(settings.db_path), settings)
    try:
        row = lifecycle.ingest_ci_evidence(
            args.exception_id, OPERATOR, pipeline_id=args.pipeline_id, scenario_id=args.scenario,
            project_id=args.project_id, job_id=args.job_id, expected_commit=args.commit,
        )
    except Unavailable as err:
        print(f"unavailable: {', '.join(err.reason_codes)}", file=sys.stderr)
        return 3
    except DomainError as err:
        print(f"rejected: {', '.join(err.reason_codes)}", file=sys.stderr)
        return 1
    ci = row["ci_verification"]
    print(f"ingested {row['evidence'].id} from project {ci['project_id']} pipeline {ci['pipeline_id']} "
          f"job {ci['job_id']} ({ci['ref']} @ {ci['commit_sha'][:12]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
