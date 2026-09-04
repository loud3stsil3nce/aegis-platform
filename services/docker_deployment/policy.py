"""Exact target/image policy and persistent idempotency for Docker deployment."""

from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class DeploymentProxyPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class ProxyTarget:
    service: str
    container: str
    image_repository: str
    compose_project: str
    project_directory: str
    compose_file: str
    image_variable: str


@dataclass(frozen=True)
class ValidatedImageRequest:
    target: ProxyTarget
    image_reference: str
    idempotency_key: str
    operation: str


class DeploymentProxyPolicy:
    def __init__(self, targets: dict[str, ProxyTarget]):
        if not targets or any(key != value.service for key, value in targets.items()):
            raise DeploymentProxyPolicyError("proxy targets must be explicit and keyed by service")
        self.targets = dict(targets)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DeploymentProxyPolicy":
        if not isinstance(value, dict) or set(value) != {"targets"} or not isinstance(value["targets"], list):
            raise DeploymentProxyPolicyError("proxy policy must contain only a target list")
        targets = {}
        for item in value["targets"]:
            fields = {
                "service", "container", "imageRepository", "composeProject",
                "projectDirectory", "composeFile", "imageVariable",
            }
            if not isinstance(item, dict) or set(item) != fields:
                raise DeploymentProxyPolicyError("proxy target fields are invalid")
            if any(not isinstance(item[field], str) or not item[field] for field in item):
                raise DeploymentProxyPolicyError("proxy target values must be non-empty strings")
            target = ProxyTarget(
                item["service"], item["container"], item["imageRepository"],
                item["composeProject"], item["projectDirectory"], item["composeFile"],
                item["imageVariable"],
            )
            if target.service in targets:
                raise DeploymentProxyPolicyError("proxy target service is duplicated")
            targets[target.service] = target
        return cls(targets)

    def validate(self, *, service: str, image_reference: str, idempotency_key: str) -> ValidatedImageRequest:
        target = self.targets.get(service)
        if target is None:
            raise DeploymentProxyPolicyError("deployment target is not registered")
        prefix = target.image_repository + "@"
        if not image_reference.startswith(prefix) or not _DIGEST.fullmatch(image_reference[len(prefix):]):
            raise DeploymentProxyPolicyError("image is outside the immutable repository allowlist")
        try:
            proposal_text, operation = idempotency_key.rsplit(":", 1)
            proposal_id = uuid.UUID(proposal_text)
        except (AttributeError, ValueError) as exc:
            raise DeploymentProxyPolicyError("idempotency key is invalid") from exc
        if str(proposal_id) != proposal_text or operation not in {"deploy", "rollback"}:
            raise DeploymentProxyPolicyError("idempotency key is invalid")
        return ValidatedImageRequest(target, image_reference, idempotency_key, operation)


class DeploymentIdempotencyStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS image_idempotency (
                     idempotency_key TEXT PRIMARY KEY, service TEXT NOT NULL,
                     image_reference TEXT NOT NULL, status TEXT NOT NULL,
                     created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                   )"""
            )

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def claim(self, request: ValidatedImageRequest) -> str:
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM image_idempotency WHERE idempotency_key=?",
                (request.idempotency_key,),
            ).fetchone()
            if row:
                if row["service"] != request.target.service or row["image_reference"] != request.image_reference:
                    raise DeploymentProxyPolicyError("idempotency key is bound to another operation")
                if row["status"] == "APPLIED":
                    return "APPLIED"
                raise DeploymentProxyPolicyError(
                    f"image operation outcome is indeterminate ({row['status'].lower()})"
                )
            connection.execute(
                "INSERT INTO image_idempotency VALUES(?,?,?,?,?,?)",
                (request.idempotency_key, request.target.service, request.image_reference,
                 "IN_PROGRESS", now, now),
            )
        return "CLAIMED"

    def finish(self, idempotency_key: str, status: str) -> None:
        if status not in {"APPLIED", "FAILED"}:
            raise DeploymentProxyPolicyError("invalid idempotency result")
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            result = connection.execute(
                """UPDATE image_idempotency SET status=?,updated_at=?
                   WHERE idempotency_key=? AND status='IN_PROGRESS'""",
                (status, now, idempotency_key),
            )
            if result.rowcount != 1:
                raise DeploymentProxyPolicyError("idempotency operation is not in progress")
