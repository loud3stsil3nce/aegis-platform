"""Authenticated exact-allowlist image replacement proxy."""

from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from .compose_backend import ComposeBackend, ComposeBackendError
from .policy import (
    DeploymentIdempotencyStore,
    DeploymentProxyPolicy,
    DeploymentProxyPolicyError,
)


MAX_BODY_BYTES = 4_096
MAX_FIELD_BYTES = 300


class ImageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_reference: str = Field(min_length=1, max_length=MAX_FIELD_BYTES)
    idempotency_key: str = Field(min_length=1, max_length=MAX_FIELD_BYTES)


class RequestBodyLimitMiddleware:
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
            message = await receive(); messages.append(message)
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    return await JSONResponse({"detail": "request body exceeds policy limit"}, status_code=413)(scope, receive, send)
                if not message.get("more_body", False):
                    break
            else:
                break

        async def replay_receive():
            return messages.pop(0) if messages else {"type": "http.request", "body": b"", "more_body": False}

        return await self.app(scope, replay_receive, send)


app = FastAPI(title="Aegis Docker Deployment Proxy", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(RequestBodyLimitMiddleware)
_operation_lock = threading.RLock()
_backend = ComposeBackend()


def _policy() -> DeploymentProxyPolicy:
    path = Path(os.getenv("AEGIS_DEPLOYMENT_PROXY_POLICY", "/run/config/docker-deployment-policy.json"))
    try:
        raw = path.read_bytes()
        if not raw or len(raw) > 16_384:
            raise ValueError
        value = json.loads(raw)
        return DeploymentProxyPolicy.from_dict(value)
    except (OSError, ValueError, json.JSONDecodeError, DeploymentProxyPolicyError) as exc:
        raise HTTPException(status_code=503, detail="deployment proxy policy unavailable") from exc


def _store() -> DeploymentIdempotencyStore:
    return DeploymentIdempotencyStore(
        os.getenv("AEGIS_DEPLOYMENT_PROXY_STATE", "/var/lib/aegis-deployment/idempotency.sqlite3")
    )


def _authenticate(authorization: Annotated[Optional[str], Header()] = None) -> None:
    token_file = os.getenv("AEGIS_DEPLOYMENT_PROXY_TOKEN_FILE")
    if not token_file:
        raise HTTPException(status_code=503, detail="deployment proxy authentication not configured")
    try:
        raw = Path(token_file).read_bytes()
        if not raw or len(raw) > MAX_BODY_BYTES:
            raise ValueError
        expected = raw.decode("utf-8", errors="strict").strip()
    except (OSError, ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=503, detail="deployment proxy authentication unavailable") from exc
    if not expected or not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    provided = authorization.removeprefix("Bearer ").strip()
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")


def _target(service: str):
    target = _policy().targets.get(service)
    if target is None:
        raise HTTPException(status_code=404, detail="deployment target not registered")
    return target


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/targets/{service}/state", dependencies=[Depends(_authenticate)])
def target_state(service: str) -> dict[str, object]:
    try:
        return _backend.state(_target(service))
    except ComposeBackendError as exc:
        raise HTTPException(status_code=503, detail="deployment target state unavailable") from exc


@app.post("/v1/targets/{service}/image", dependencies=[Depends(_authenticate)])
def replace_image(service: str, body: ImageRequest) -> dict[str, str]:
    try:
        request = _policy().validate(
            service=service, image_reference=body.image_reference,
            idempotency_key=body.idempotency_key,
        )
    except DeploymentProxyPolicyError as exc:
        raise HTTPException(status_code=404, detail="deployment operation is outside policy") from exc
    with _operation_lock:
        claimed = False
        try:
            claim = _store().claim(request)
            if claim == "APPLIED":
                return {"target": service, "status": "already_applied", "idempotency_key": body.idempotency_key}
            claimed = True
            _backend.apply(request.target, request.image_reference, request.operation)
            if _backend.state(request.target).get("image") != request.image_reference:
                raise ComposeBackendError("replacement image verification failed")
            _store().finish(body.idempotency_key, "APPLIED")
        except (DeploymentProxyPolicyError, ComposeBackendError) as exc:
            if claimed:
                try:
                    _store().finish(body.idempotency_key, "FAILED")
                except DeploymentProxyPolicyError:
                    pass
            raise HTTPException(status_code=409, detail="deployment operation failed or is indeterminate") from exc
    return {"target": service, "status": "accepted", "idempotency_key": body.idempotency_key}
