"""Narrow, sanitized Docker read adapter for Aegis observability."""

from __future__ import annotations

import os
import re
import secrets
from typing import Annotated

import docker
from docker.errors import DockerException, NotFound
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status


MAX_LOG_LINES = 200
MAX_RESULT_BYTES = 16_384
_SECRET_PATTERN = re.compile(
    r"(?i)(password|passwd|token|secret|api[_-]?key|authorization)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)

app = FastAPI(title="Aegis Docker Observer", docs_url=None, redoc_url=None)


def _allowed_containers() -> frozenset[str]:
    raw = os.getenv("AEGIS_ALLOWED_CONTAINERS", "")
    return frozenset(value.strip() for value in raw.split(",") if value.strip())


def _authenticate(authorization: Annotated[str | None, Header()] = None) -> None:
    expected = os.getenv("AEGIS_DOCKER_OBSERVER_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="observer authentication not configured")
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


def _container(name: str):
    if name not in _allowed_containers():
        raise HTTPException(status_code=404, detail="container not registered")
    try:
        return _client().containers.get(name)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail="registered container unavailable") from exc
    except DockerException as exc:
        raise HTTPException(status_code=503, detail="Docker daemon unavailable") from exc


def _state(container) -> dict[str, object]:
    container.reload()
    raw_state = container.attrs.get("State") or {}
    raw_health = raw_state.get("Health") or {}
    return {
        "name": container.name,
        "status": raw_state.get("Status", "unknown"),
        "health": raw_health.get("Status", "not-configured"),
        "restarting": bool(raw_state.get("Restarting", False)),
        "restart_count": int(container.attrs.get("RestartCount", 0)),
        "started_at": raw_state.get("StartedAt"),
    }


def _redact_and_bound(value: str) -> str:
    redacted = _SECRET_PATTERN.sub(r"\1\2[REDACTED]", value)
    encoded = redacted.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_RESULT_BYTES:
        return redacted
    suffix = "\n...[result truncated by Aegis policy]"
    room = max(0, MAX_RESULT_BYTES - len(suffix.encode("utf-8")))
    return encoded[:room].decode("utf-8", errors="ignore") + suffix


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/containers", dependencies=[Depends(_authenticate)])
def list_containers() -> dict[str, list[dict[str, object]]]:
    states: list[dict[str, object]] = []
    for name in sorted(_allowed_containers()):
        try:
            states.append(_state(_container(name)))
        except HTTPException as exc:
            states.append({"name": name, "status": "unavailable", "error": exc.detail})
    return {"containers": states}


@app.get("/v1/containers/{name}/state", dependencies=[Depends(_authenticate)])
def container_state(name: str) -> dict[str, object]:
    return _state(_container(name))


@app.get("/v1/containers/{name}/logs", dependencies=[Depends(_authenticate)])
def container_logs(
    name: str,
    lines: Annotated[int, Query(ge=1, le=MAX_LOG_LINES)] = 50,
) -> dict[str, object]:
    try:
        raw = _container(name).logs(tail=lines, stdout=True, stderr=True)
    except DockerException as exc:
        raise HTTPException(status_code=503, detail="container logs unavailable") from exc
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    return {"name": name, "lines": lines, "logs": _redact_and_bound(text)}
