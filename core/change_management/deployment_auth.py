"""Bounded file-backed bearer identities for immutable deployment proposals."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_TOKEN_BYTES = 4_096
VALID_ROLES = frozenset({"requester", "approver", "executor"})


class DeploymentActorAuthorizationError(PermissionError):
    pass


@dataclass(frozen=True)
class DeploymentActor:
    actor_id: str
    roles: frozenset[str]


class DeploymentActorAuthenticator:
    def __init__(self, records: list[dict[str, Any]]):
        actors: list[tuple[str, DeploymentActor]] = []
        seen: set[str] = set()
        for record in records:
            actor_id = str(record.get("id", "")).strip()
            roles = frozenset(record.get("roles", []))
            token_file = Path(str(record.get("tokenFile", "")))
            if (
                not actor_id or len(actor_id.encode()) > 200 or actor_id in seen
                or not roles or not roles <= VALID_ROLES
            ):
                raise ValueError("deployment actor identity or roles are invalid")
            raw = token_file.read_bytes()
            if not raw or len(raw) > MAX_TOKEN_BYTES or not raw.strip():
                raise ValueError("deployment actor token file is empty or oversized")
            actors.append((hashlib.sha256(raw.strip()).hexdigest(), DeploymentActor(actor_id, roles)))
            seen.add(actor_id)
        if not actors:
            raise ValueError("at least one deployment actor is required")
        self._actors = tuple(actors)

    def authenticate(self, authorization: str | None) -> DeploymentActor:
        if not authorization or not authorization.startswith("Bearer "):
            raise DeploymentActorAuthorizationError("missing bearer token")
        candidate = authorization.removeprefix("Bearer ").strip().encode()
        if not candidate:
            raise DeploymentActorAuthorizationError("missing bearer token")
        digest = hashlib.sha256(candidate).hexdigest()
        match = None
        for expected, actor in self._actors:
            if secrets.compare_digest(expected, digest):
                match = actor
        if match is None:
            raise DeploymentActorAuthorizationError("invalid bearer token")
        return match

