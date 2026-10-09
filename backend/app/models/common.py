"""Shared field types and helpers for GateDebt models."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import AfterValidator, StringConstraints


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware_utc(value: datetime) -> datetime:
    """Reject naive datetimes and normalise everything else to UTC."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("timestamp must be timezone-aware (e.g. 2026-01-01T00:00:00Z)")
    return value.astimezone(timezone.utc)


UTCDateTime = Annotated[datetime, AfterValidator(_require_aware_utc)]

# Who or what initiated an operation, e.g. "user:alice", "ci:pipeline-123",
# "agent:mock-investigator", "system:policy-engine".
Actor = Annotated[
    str,
    StringConstraints(pattern=r"^(user|system|agent|ci):[A-Za-z0-9._@-]{1,64}$"),
]

CommitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]

ExceptionId = Annotated[str, StringConstraints(pattern=r"^EXC-[A-Z0-9-]{1,32}$")]

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
ShortStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


def canonical_digest(payload: dict[str, Any]) -> str:
    """SHA-256 over a canonical JSON encoding. Used to detect tampered evidence."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
