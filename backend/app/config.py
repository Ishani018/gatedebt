"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

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

    def __post_init__(self) -> None:
        if self.environment == Environment.PRODUCTION and self.auth_mode == AuthMode.DEV_HEADER:
            raise ConfigError("dev-header auth cannot be used in production")

    @property
    def is_production(self) -> bool:
        return self.environment == Environment.PRODUCTION

    @property
    def policy(self) -> PolicyConfig:
        # Production trusts only evidence produced by GitLab CI.
        if self.is_production:
            return PolicyConfig(trusted_sources=frozenset({EvidenceSource.GITLAB_CI}))
        return DEFAULT_POLICY

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ) if env is None else env
        try:
            environment = Environment(env.get("GATEDEBT_ENV", "development"))
            default_auth = AuthMode.NONE if environment == Environment.PRODUCTION else AuthMode.DEV_HEADER
            auth_mode = AuthMode(env.get("GATEDEBT_AUTH_MODE", default_auth.value))
        except ValueError as err:
            raise ConfigError(str(err)) from err
        approvers = frozenset(a.strip() for a in env.get("GATEDEBT_APPROVERS", "").split(",") if a.strip())
        return cls(
            environment=environment,
            db_path=Path(env.get("GATEDEBT_DB_PATH", str(REPO_ROOT / "gatedebt.sqlite3"))),
            artifacts_dir=Path(env.get("GATEDEBT_ARTIFACTS_DIR", str(DEFAULT_ARTIFACTS_DIR))),
            auth_mode=auth_mode,
            approvers=approvers,
        )
