"""Bounded file-backed bearer identities for the internal deployment API."""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from typing import Any

from .service import Actor, ActorAuthorizationError


MAX_TOKEN_BYTES = 4_096
VALID_ROLES = frozenset({"requester", "approver", "executor"})


class ActorAuthenticator:
    def __init__(self, records: list[dict[str, Any]]):
        actors: list[tuple[str, Actor]] = []
        seen_ids: set[str] = set()
        for record in records:
            actor_id = str(record.get("id", "")).strip()
            roles = frozenset(record.get("roles", []))
            path = Path(str(record.get("tokenFile", "")))
            if not actor_id or actor_id in seen_ids or not roles or not roles <= VALID_ROLES:
                raise ValueError("deployment actor identity or roles are invalid")
            raw = path.read_bytes()
            if not raw or len(raw) > MAX_TOKEN_BYTES:
                raise ValueError("deployment actor token file is empty or oversized")
            token = raw.strip()
            if not token:
                raise ValueError("deployment actor token is empty")
            actors.append((hashlib.sha256(token).hexdigest(), Actor(actor_id, roles)))
            seen_ids.add(actor_id)
        if not actors:
            raise ValueError("at least one deployment actor is required")
        self._actors = tuple(actors)

    def authenticate(self, authorization: str | None) -> Actor:
        if not authorization or not authorization.startswith("Bearer "):
            raise ActorAuthorizationError("missing bearer token")
        token = authorization.removeprefix("Bearer ").strip().encode()
        if not token:
            raise ActorAuthorizationError("missing bearer token")
        candidate = hashlib.sha256(token).hexdigest()
        match = None
        for digest, actor in self._actors:
            if secrets.compare_digest(digest, candidate):
                match = actor
        if match is None:
            raise ActorAuthorizationError("invalid bearer token")
        return match
