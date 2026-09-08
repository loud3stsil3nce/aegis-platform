"""Exact-allowlist Docker restart proxy for governed Aegis lifecycle actions."""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import docker
from docker.errors import DockerException, NotFound
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse


MAX_BODY_BYTES = 4_096
MAX_IDEMPOTENCY_KEY_BYTES = 200


class RestartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=MAX_IDEMPOTENCY_KEY_BYTES)


class RequestBodyLimitMiddleware:
    """Bound fixed-length and chunked bodies before request parsing."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                if int(raw_length) > MAX_BODY_BYTES:
                    return await JSONResponse({"detail": "request body exceeds policy limit"}, status_code=413)(scope, receive, send)
            except ValueError:
                return await JSONResponse({"detail": "invalid content-length"}, status_code=400)(scope, receive, send)

        messages, received = [], 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    return await JSONResponse({"detail": "request body exceeds policy limit"}, status_code=413)(scope, receive, send)
                if not message.get("more_body", False):
                    break
            else:
                break

        async def replay_receive():
            if messages:
                return messages.pop(0)
            return {"type": "http.request", "body": b"", "more_body": False}

        return await self.app(scope, replay_receive, send)


app = FastAPI(
    title="Aegis Docker Lifecycle Proxy", docs_url=None, redoc_url=None,
    openapi_url=None,
)
app.add_middleware(RequestBodyLimitMiddleware)
_restart_lock = threading.RLock()


def _idempotency_path() -> Path:
    return Path(os.getenv("AEGIS_LIFECYCLE_STATE_PATH", "/var/lib/aegis-lifecycle/idempotency.sqlite3"))


def _idempotency_connection() -> sqlite3.Connection:
    path = _idempotency_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE IF NOT EXISTS restart_idempotency (
             idempotency_key TEXT PRIMARY KEY, target TEXT NOT NULL,
             status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
           )"""
    )
    connection.commit()
    return connection


def _claim_restart(key: str, target: str) -> str:
    now = datetime.now(timezone.utc).isoformat()
    with _idempotency_connection() as connection:
        row = connection.execute(
            "SELECT target,status FROM restart_idempotency WHERE idempotency_key=?", (key,)
        ).fetchone()
        if row is not None:
            if row["target"] != target:
                raise HTTPException(status_code=409, detail="idempotency key is bound to another target")
            return str(row["status"])
        connection.execute(
            "INSERT INTO restart_idempotency VALUES(?,?,?,?,?)",
            (key, target, "IN_PROGRESS", now, now),
        )
    return "CLAIMED"


def _finish_restart(key: str, status: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _idempotency_connection() as connection:
        connection.execute(
            "UPDATE restart_idempotency SET status=?,updated_at=? WHERE idempotency_key=?",
            (status, now, key),
        )


def _allowed_targets() -> frozenset[str]:
    raw = os.getenv("AEGIS_LIFECYCLE_ALLOWED_TARGETS", "")
    return frozenset(value.strip() for value in raw.split(",") if value.strip())


def _authenticate(authorization: Annotated[Optional[str], Header()] = None) -> None:
    expected = os.getenv("AEGIS_DOCKER_LIFECYCLE_TOKEN")
    token_file = os.getenv("AEGIS_DOCKER_LIFECYCLE_TOKEN_FILE")
    if expected and token_file:
        raise HTTPException(status_code=503, detail="ambiguous lifecycle authentication configuration")
    if token_file:
        try:
            raw = Path(token_file).read_bytes()
        except OSError as exc:
            raise HTTPException(status_code=503, detail="lifecycle authentication unavailable") from exc
        if not raw or len(raw) > MAX_BODY_BYTES:
            raise HTTPException(status_code=503, detail="lifecycle authentication invalid")
        expected = raw.decode("utf-8", errors="strict").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="lifecycle authentication not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    provided = authorization.removeprefix("Bearer ").strip()
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")


def _client():
    try:
        return docker.from_env()
    except DockerException as exc:
        raise HTTPException(status_code=503, detail="Docker daemon unavailable") from exc


def _container(target: str):
    if target not in _allowed_targets():
        raise HTTPException(status_code=404, detail="lifecycle target not registered")
    try:
        return _client().containers.get(target)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail="registered lifecycle target unavailable") from exc
    except DockerException as exc:
        raise HTTPException(status_code=503, detail="Docker daemon unavailable") from exc


def _state(container) -> dict[str, object]:
    try:
        container.reload()
    except DockerException as exc:
        raise HTTPException(status_code=503, detail="lifecycle target state unavailable") from exc
    raw = container.attrs.get("State") or {}
    health = raw.get("Health") or {}
    return {
        "target": container.name,
        "status": raw.get("Status", "unknown"),
        "health": health.get("Status", "not-configured"),
        "restarting": bool(raw.get("Restarting", False)),
        "restart_count": int(container.attrs.get("RestartCount", 0)),
        "started_at": raw.get("StartedAt"),
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/targets/{target}/state", dependencies=[Depends(_authenticate)])
def target_state(target: str) -> dict[str, object]:
    return _state(_container(target))


@app.post("/v1/targets/{target}/restart", dependencies=[Depends(_authenticate)])
def restart_target(target: str, request: RestartRequest) -> dict[str, object]:
    if target not in _allowed_targets():
        raise HTTPException(status_code=404, detail="lifecycle target not registered")
    # The lock deliberately spans Docker restart: concurrent duplicate requests
    # cannot both cross the idempotency check.
    with _restart_lock:
        claim = _claim_restart(request.idempotency_key, target)
        if claim == "APPLIED":
            return {"target": target, "status": "already_applied", "idempotency_key": request.idempotency_key}
        if claim in {"IN_PROGRESS", "FAILED"}:
            raise HTTPException(
                status_code=409,
                detail=f"restart outcome is indeterminate ({claim.lower()}); inspect target state before recovery",
            )
        container = _container(target)
        try:
            container.restart()
        except DockerException as exc:
            _finish_restart(request.idempotency_key, "FAILED")
            raise HTTPException(status_code=503, detail="lifecycle restart failed") from exc
        _finish_restart(request.idempotency_key, "APPLIED")
        return {"target": target, "status": "accepted", "idempotency_key": request.idempotency_key}
