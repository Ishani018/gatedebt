# pipeline-gate-recovery

Pipeline rehearsal (`mode: pipeline`). Designed to run as a GitLab CI job, and
runnable locally (evidence is then labelled `local_sandbox`, never `gitlab_ci`).

1. Build a workspace with `requirements.lock` and a `build-manifest.json` whose
   digest is deliberately **stale** (the `stale_lock_digest` fault).
2. Run the real gate (`gate.py`) in a subprocess with a timeout.
3. **Detect/classify**: the failure counts as the injected one only if the exit
   code is 3 *and* the gate reports `LOCK_DIGEST_MISMATCH`. Exit 0 means the
   gate is blind; any other exit code, missing output or a timeout is an
   unexpected infrastructure failure.
4. **Recover**: the approved procedure `regenerate_lock_digest` rewrites the
   manifest digest from the lockfile, then the gate is retried (max 1 retry).
5. **Waived check** `quality-gate:dependency-lock`: the gate passes again on a
   fresh copy of the recovered workspace.
6. **Cleanup**: workspace removed and verified.
