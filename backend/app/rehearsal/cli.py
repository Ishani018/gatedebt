"""Command-line entry point.

    python -m app.rehearsal list
    python -m app.rehearsal run <scenario-id> --exception-id EXC-001 [--commit SHA]
                                [--source local_sandbox|gitlab_ci] [--artifacts-dir DIR]

Exit codes: 0 rehearsal passed, 1 rehearsal failed (report still written),
2 invalid input or environment (no rehearsal ran).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from app.models import EvidenceSource
from app.models.common import ExceptionId

from . import SCENARIOS
from .harness import DEFAULT_ARTIFACTS_DIR, REPO_ROOT, RunContext, run_rehearsal

EXIT_PASSED, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
_SHA = re.compile(r"^[0-9a-f]{40}$")


class UsageError(Exception):
    pass


def _git(*args: str) -> str | None:
    try:
        proc = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _positive_int(env: dict[str, str], name: str) -> int:
    value = env.get(name, "")
    if not value.isdigit() or int(value) < 1:
        raise UsageError(f"{name} is missing or invalid")
    return int(value)


def build_context(
    exception_id: str, commit: str | None, source: str, env: dict[str, str] | None = None
) -> RunContext:
    env = dict(os.environ) if env is None else env
    try:
        TypeAdapter(ExceptionId).validate_python(exception_id)
    except ValidationError:
        raise UsageError(f"invalid exception id {exception_id!r} (expected e.g. EXC-001)") from None
    if commit is not None and not _SHA.match(commit):
        raise UsageError("--commit must be a full 40-character lowercase hex SHA")

    if source == EvidenceSource.GITLAB_CI:
        # CI evidence must come from an actual CI job; never claim it locally.
        if env.get("GITLAB_CI") != "true":
            raise UsageError("--source gitlab_ci is only allowed inside a GitLab CI job (GITLAB_CI=true)")
        ci_sha = env.get("CI_COMMIT_SHA", "")
        if not _SHA.match(ci_sha):
            raise UsageError("CI_COMMIT_SHA is missing or invalid")
        if commit is not None and commit != ci_sha:
            raise UsageError("--commit does not match CI_COMMIT_SHA")
        return RunContext(
            exception_id=exception_id,
            commit_sha=ci_sha,
            commit_origin="gitlab_ci",
            source=EvidenceSource.GITLAB_CI,
            pipeline_id=_positive_int(env, "CI_PIPELINE_ID"),
            job_id=_positive_int(env, "CI_JOB_ID"),
        )

    if source != EvidenceSource.LOCAL_SANDBOX:
        raise UsageError(f"unsupported evidence source {source!r}")
    if commit is not None:
        return RunContext(exception_id, commit, "provided", EvidenceSource.LOCAL_SANDBOX)
    head = (_git("rev-parse", "HEAD") or "").strip()
    if not _SHA.match(head):
        raise UsageError("no commit SHA available: pass --commit <sha> (not inside a git checkout)")
    status = _git("status", "--porcelain")
    return RunContext(
        exception_id=exception_id,
        commit_sha=head,
        commit_origin="local_git_head",
        source=EvidenceSource.LOCAL_SANDBOX,
        working_tree_dirty=None if status is None else bool(status.strip()),
    )


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.rehearsal", description="Run an approved GateDebt rehearsal.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list approved scenarios")
    run = sub.add_parser("run", help="run one approved scenario")
    run.add_argument("scenario", choices=sorted(SCENARIOS))
    run.add_argument("--exception-id", required=True)
    run.add_argument("--commit", help="40-char commit SHA the evidence applies to (default: local git HEAD)")
    run.add_argument(
        "--source",
        default=EvidenceSource.LOCAL_SANDBOX.value,
        choices=[EvidenceSource.LOCAL_SANDBOX.value, EvidenceSource.GITLAB_CI.value],
    )
    run.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACTS_DIR)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_PASSED

    if args.command == "list":
        for scenario_id, cls in sorted(SCENARIOS.items()):
            print(f"{scenario_id}\tmode={cls.requirement.mode.value}\texercises={cls.exercised_check}")
        return EXIT_PASSED

    try:
        ctx = build_context(args.exception_id, args.commit, args.source, env)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    report, path = run_rehearsal(SCENARIOS[args.scenario], ctx, args.artifacts_dir)
    evidence = report.evidence
    print(f"scenario:       {evidence.scenario_id} ({evidence.mode.value})")
    print(f"run:            {evidence.run_id}  evidence: {evidence.id}")
    print(f"commit:         {evidence.commit_sha} ({report.commit_origin})")
    print(f"source:         {evidence.source.value} - {report.execution_note}")
    print(f"classification: {evidence.injected_failure_classification.value}")
    print(f"cleanup:        {evidence.cleanup_status.value}")
    for result in evidence.check_results:
        print(f"  [{result.outcome.value:>6}] {result.check_id}  {result.detail}")
    print(f"verdict:        {report.verdict.upper()}")
    for failure in report.failures:
        print(f"  - {failure}")
    print(f"report:         {path}")
    return EXIT_PASSED if report.verdict == "passed" else EXIT_FAILED
