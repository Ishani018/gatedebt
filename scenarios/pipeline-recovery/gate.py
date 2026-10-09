"""Dependency-lock quality gate used by the pipeline-gate-recovery rehearsal.

Usage: python gate.py <workspace>

Checks that build-manifest.json records the SHA-256 of the lockfile it names.
Prints exactly one JSON result line and exits with:
  0  passed
  3  LOCK_DIGEST_MISMATCH  (the controlled fixture failure)
  4  MANIFEST_INVALID
Any other exit code (e.g. an interpreter crash) is not a gate result.
"""

import hashlib
import json
import sys
from pathlib import Path


def main(workspace: Path) -> int:
    try:
        manifest = json.loads((workspace / "build-manifest.json").read_text())
        lockfile = workspace / manifest["lockfile"]
        recorded = manifest["lock_sha256"]
        if lockfile.parent.resolve() != workspace.resolve():
            raise ValueError("lockfile must live in the workspace")
        actual = hashlib.sha256(lockfile.read_bytes()).hexdigest()
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"gate": "dependency-lock", "status": "failed", "code": "MANIFEST_INVALID", "detail": str(exc)}))
        return 4
    if actual != recorded:
        print(json.dumps({"gate": "dependency-lock", "status": "failed", "code": "LOCK_DIGEST_MISMATCH",
                          "detail": f"manifest records {recorded[:12]}, lockfile is {actual[:12]}"}))
        return 3
    print(json.dumps({"gate": "dependency-lock", "status": "passed", "code": "OK", "detail": actual[:12]}))
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
