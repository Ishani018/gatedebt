"""Scenario B: pipeline-gate-recovery (pipeline mode).

Runs the real dependency-lock gate in a subprocess against a workspace with a
deliberately stale manifest, classifies the failure, applies the approved
recovery procedure and retries. See ``scenarios/pipeline-recovery/README.md``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from app.models import FailureClassification
from app.services.policy import PIPELINE_GATE_RECOVERY

from .harness import SCENARIOS_DIR, Scenario


@dataclass(frozen=True)
class GateRun:
    exit_code: int | None  # None: timed out
    result: dict | None  # parsed JSON result line, if the gate produced one
    stderr: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and self.result is not None and self.result.get("status") == "passed"

    def describe(self) -> str:
        if self.exit_code is None:
            return "timed out"
        code = self.result.get("code") if self.result else "no result line"
        tail = f"; stderr: {self.stderr.strip().splitlines()[-1]}" if self.stderr.strip() else ""
        return f"exit {self.exit_code}, {code}{tail}"


class PipelineGateRecovery(Scenario):
    requirement = PIPELINE_GATE_RECOVERY
    exercised_check = "quality-gate:dependency-lock"
    fixture_dir = SCENARIOS_DIR / "pipeline-recovery"

    def _run_gate(self, workspace: Path) -> GateRun:
        gate = self.fixture_dir / self.fixture["gate_script"]
        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(gate), str(workspace)],
                capture_output=True,
                text=True,
                timeout=self.fixture["timeout_seconds"],
                cwd=self.workdir,
            )
        except subprocess.TimeoutExpired:
            return GateRun(None, None, "")
        result = None
        lines = proc.stdout.strip().splitlines()
        if lines:
            try:
                parsed = json.loads(lines[-1])
                result = parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                pass
        run = GateRun(proc.returncode, result, proc.stderr)
        self.rec.log(f"gate run on {workspace.name}: {run.describe()}")
        return run

    def _write_manifest(self, workspace: Path, digest: str) -> None:
        manifest = {"lockfile": self.fixture["lockfile"], "lock_sha256": digest}
        (workspace / "build-manifest.json").write_text(json.dumps(manifest, indent=2))

    def execute(self) -> None:
        fx = self.fixture
        workspace = self.workdir / "workspace"
        workspace.mkdir()
        lockfile = workspace / fx["lockfile"]
        shutil.copyfile(self.fixture_dir / fx["lockfile"], lockfile)

        # Inject: the manifest records the digest of the previous lockfile.
        stale = hashlib.sha256(fx["stale_lock_content"].encode()).hexdigest()
        self._write_manifest(workspace, stale)
        self.rec.log(f"injected fault {fx['fault']}")

        first = self._run_gate(workspace)
        failed = first.exit_code not in (None, 0)
        self.rec.check("injected_failure_detected", failed, first.describe())
        matches = (
            first.exit_code == fx["expected_exit_code"]
            and first.result is not None
            and first.result.get("code") == fx["expected_failure_code"]
        )
        if first.passed:
            self.rec.classification = FailureClassification.INJECTED_NOT_DETECTED
        elif matches:
            self.rec.classification = FailureClassification.EXPECTED_INJECTED
        else:
            self.rec.classification = FailureClassification.UNEXPECTED_INFRASTRUCTURE
        self.rec.check(
            "failure_classified_expected",
            matches,
            f"classified {self.rec.classification.value}; expected exit {fx['expected_exit_code']} "
            f"{fx['expected_failure_code']}, got {first.describe()}",
        )
        if not matches:
            return

        # Recover: the approved procedure, then a bounded retry.
        self.rec.recovery_attempted = True
        self._write_manifest(workspace, hashlib.sha256(lockfile.read_bytes()).hexdigest())
        manifest = json.loads((workspace / "build-manifest.json").read_text())
        regenerated = manifest["lock_sha256"] == hashlib.sha256(lockfile.read_bytes()).hexdigest()
        self.rec.assertion("manifest_regenerated", regenerated, fx["recovery_procedure"])

        retry = None
        attempts = 0
        while attempts < fx["max_retries"]:
            attempts += 1
            retry = self._run_gate(workspace)
            if retry.passed:
                break
        retried_ok = retry is not None and retry.passed
        self.rec.assertion(
            "retry_within_limit", retried_ok, f"{attempts}/{fx['max_retries']} retries; last {retry.describe() if retry else 'none'}"
        )
        self.rec.recovery_succeeded = regenerated and retried_ok
        self.rec.check("retry_workflow_passed", retried_ok, retry.describe() if retry else "no retry ran")

        # The waived gate itself, on a fresh copy of the recovered workspace.
        fresh = self.workdir / "verify"
        shutil.copytree(workspace, fresh)
        final = self._run_gate(fresh)
        self.rec.check(self.exercised_check, final.passed, final.describe())
