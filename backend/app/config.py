"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from app.integrations.gitlab import GitLabClient, HttpGitLabClient
from app.models import EvidenceSource
from app.rehearsal.harness import DEFAULT_ARTIFACTS_DIR, REPO_ROOT
from app.services.policy import DEFAULT_POLICY, PolicyConfig


class Environment(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class AuthMode(StrEnum):
    # Trusts a request header. Development only; never authentication.
    DEV_HEADER = "dev-header"
    # No authenticator configured: every write is refused.
    NONE = "none"


class EvidenceTrust(StrEnum):
    # Local sandbox runs and verified CI runs both count (development default).
    LOCAL_AND_CI = "local_and_ci"
    # Only server-verified GitLab CI evidence counts (forced in production).
    CI_ONLY = "ci_only"


@dataclass(frozen=True)
class RepoState:
    commit_sha: str | None
    working_tree_dirty: bool | None


_SHA = re.compile(r"^[0-9a-f]{40}$")


def local_repo_state() -> RepoState:
    def git(*args: str) -> str | None:
        try:
            proc = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return proc.stdout if proc.returncode == 0 else None

    head = (git("rev-parse", "HEAD") or "").strip()
    status = git("status", "--porcelain")
    return RepoState(
        commit_sha=head if _SHA.match(head) else None,
        working_tree_dirty=None if status is None else bool(status.strip()),
    )


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    environment: Environment = Environment.DEVELOPMENT
    db_path: Path = REPO_ROOT / "gatedebt.sqlite3"
    artifacts_dir: Path = DEFAULT_ARTIFACTS_DIR
    auth_mode: AuthMode = AuthMode.DEV_HEADER
    # Actors allowed to approve renewals and retirements, e.g. "user:alice".
    approvers: frozenset[str] = frozenset()
    repo_state: Callable[[], RepoState] = field(default=local_repo_state)
    evidence_trust: EvidenceTrust = EvidenceTrust.LOCAL_AND_CI
    # GitLab CI evidence verification. All of url, token and project IDs are
    # needed; without them CI ingestion fails closed (503).
    gitlab_url: str | None = None
    gitlab_token: str | None = field(default=None, repr=False)
    gitlab_project_ids: frozenset[int] = frozenset()
    # Only pipelines on these refs (ideally protected branches) are trusted.
    gitlab_trusted_refs: frozenset[str] = frozenset({"main"})
    # Tests inject a fake client here; never set in normal operation.
    gitlab_client_factory: Callable[[], GitLabClient | None] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.environment == Environment.PRODUCTION and self.auth_mode == AuthMode.DEV_HEADER:
            raise ConfigError("dev-header auth cannot be used in production")
        if self.gitlab_url is not None and not self.gitlab_url.startswith("https://"):
            raise ConfigError("GATEDEBT_GITLAB_URL must use https://")

    @property
    def effective_trust(self) -> EvidenceTrust:
        return EvidenceTrust.CI_ONLY if self.is_production else self.evidence_trust

    @property
    def ci_verification_configured(self) -> bool:
        if self.gitlab_client_factory is not None:
            return bool(self.gitlab_project_ids)
        return bool(self.gitlab_url and self.gitlab_token and self.gitlab_project_ids)

    def gitlab_client(self) -> GitLabClient | None:
        if not self.ci_verification_configured:
            return None
        if self.gitlab_client_factory is not None:
            return self.gitlab_client_factory()
        return HttpGitLabClient(self.gitlab_url, self.gitlab_token)

    @property
    def is_production(self) -> bool:
        return self.environment == Environment.PRODUCTION

    @property
    def policy(self) -> PolicyConfig:
        # Production (or ci_only) trusts only evidence produced by GitLab CI.
        if self.effective_trust == EvidenceTrust.CI_ONLY:
            return PolicyConfig(trusted_sources=frozenset({EvidenceSource.GITLAB_CI}))
        return DEFAULT_POLICY

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ) if env is None else env
        try:
            environment = Environment(env.get("GATEDEBT_ENV", "development"))
            default_auth = AuthMode.NONE if environment == Environment.PRODUCTION else AuthMode.DEV_HEADER
            auth_mode = AuthMode(env.get("GATEDEBT_AUTH_MODE", default_auth.value))
            trust = EvidenceTrust(env.get("GATEDEBT_EVIDENCE_TRUST", EvidenceTrust.LOCAL_AND_CI.value))
            project_ids = frozenset(
                int(p) for p in env.get("GATEDEBT_GITLAB_PROJECT_IDS", "").split(",") if p.strip()
            )
        except ValueError as err:
            raise ConfigError(str(err)) from err
        if any(p < 1 for p in project_ids):
            raise ConfigError("GATEDEBT_GITLAB_PROJECT_IDS must be positive integers")
        refs = frozenset(r.strip() for r in env.get("GATEDEBT_GITLAB_TRUSTED_REFS", "main").split(",") if r.strip())
        approvers = frozenset(a.strip() for a in env.get("GATEDEBT_APPROVERS", "").split(",") if a.strip())
        return cls(
            environment=environment,
            db_path=Path(env.get("GATEDEBT_DB_PATH", str(REPO_ROOT / "gatedebt.sqlite3"))),
            artifacts_dir=Path(env.get("GATEDEBT_ARTIFACTS_DIR", str(DEFAULT_ARTIFACTS_DIR))),
            auth_mode=auth_mode,
            approvers=approvers,
            evidence_trust=trust,
            gitlab_url=env.get("GATEDEBT_GITLAB_URL") or None,
            gitlab_token=env.get("GATEDEBT_GITLAB_TOKEN") or None,
            gitlab_project_ids=project_ids,
            gitlab_trusted_refs=refs,
        )
