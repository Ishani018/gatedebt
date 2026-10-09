"""Identity and authorisation boundary.

``Authenticator`` is the interface a real identity provider (planned: GitLab
OAuth / OIDC) must implement. Only ``DevHeaderAuthenticator`` exists today,
and it is NOT authentication: it believes whatever the
``X-GateDebt-Dev-User`` header says. It is refused in production.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fastapi import Request
from pydantic import TypeAdapter, ValidationError

from app.config import AuthMode, Settings
from app.models.common import Actor

DEV_USER_HEADER = "X-GateDebt-Dev-User"


@dataclass(frozen=True)
class Identity:
    actor: str  # e.g. "user:alice", "agent:mock-investigator", "ci:pipeline-12"
    method: str  # how the identity was established, recorded with approvals
    # True only for a real, verified identity provider. Never for dev headers.
    verified: bool

    @property
    def is_human(self) -> bool:
        return self.actor.startswith("user:")


class Authenticator(Protocol):
    name: str

    def authenticate(self, request: Request) -> Identity | None:
        """Return the caller's identity, or None if unauthenticated."""


class DevHeaderAuthenticator:
    name = "dev-header (development only, unverified)"

    def authenticate(self, request: Request) -> Identity | None:
        value = request.headers.get(DEV_USER_HEADER)
        if not value:
            return None
        try:
            actor = TypeAdapter(Actor).validate_python(value)
        except ValidationError:
            return None
        return Identity(actor=actor, method="dev-header", verified=False)


class NoAuthenticator:
    name = "none (no identity provider configured; writes refused)"

    def authenticate(self, request: Request) -> Identity | None:
        return None


def build_authenticator(settings: Settings) -> Authenticator:
    if settings.auth_mode == AuthMode.DEV_HEADER:
        return DevHeaderAuthenticator()
    return NoAuthenticator()


def approval_denial(identity: Identity, settings: Settings) -> str | None:
    """Reason code if this identity may not approve anything, else None."""
    if not identity.is_human:
        return "APPROVER_MUST_BE_HUMAN"
    if settings.is_production and not identity.verified:
        return "APPROVER_NOT_VERIFIED"
    if identity.actor not in settings.approvers:
        return "APPROVER_NOT_AUTHORIZED"
    return None
