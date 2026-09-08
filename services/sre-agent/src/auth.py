"""Fail-closed authentication helpers for Aegis HTTP and MCP endpoints."""

from __future__ import annotations

import os
import secrets
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import Request


class AuthenticationError(ValueError):
    pass


def validate_bearer_header(header: str | None, expected: str | None) -> None:
    if not expected:
        raise AuthenticationError("service authentication is not configured")
    if not header or not header.startswith("Bearer "):
        raise AuthenticationError("missing bearer token")
    provided = header.removeprefix("Bearer ").strip()
    if not provided or not secrets.compare_digest(provided, expected):
        raise AuthenticationError("invalid bearer token")


def validate_shared_secret(provided: str | None, expected: str | None) -> None:
    if not expected:
        raise AuthenticationError("webhook authentication is not configured")
    if not provided or not secrets.compare_digest(provided, expected):
        raise AuthenticationError("invalid webhook secret")


def require_mcp_auth(request: Any) -> None:
    from fastapi import HTTPException, status

    expected = os.getenv("AEGIS_MCP_API_KEY")
    try:
        validate_bearer_header(request.headers.get("authorization"), expected)
    except AuthenticationError as exc:
        code = status.HTTP_503_SERVICE_UNAVAILABLE if not expected else status.HTTP_401_UNAUTHORIZED
        raise HTTPException(status_code=code, detail=str(exc)) from exc


def require_jira_webhook_auth(request: Any) -> None:
    from fastapi import HTTPException, status

    expected = os.getenv("AEGIS_JIRA_WEBHOOK_SECRET")
    try:
        validate_shared_secret(request.headers.get("x-aegis-webhook-secret"), expected)
    except AuthenticationError as exc:
        code = status.HTTP_503_SERVICE_UNAVAILABLE if not expected else status.HTTP_401_UNAUTHORIZED
        raise HTTPException(status_code=code, detail=str(exc)) from exc


def enforce_body_limit(request: Any, max_bytes: int = 262_144) -> None:
    from fastapi import HTTPException

    raw_length = request.headers.get("content-length")
    if not raw_length:
        return
    try:
        content_length = int(raw_length)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid content-length") from exc
    if content_length < 0 or content_length > max_bytes:
        raise HTTPException(status_code=413, detail="request body exceeds policy limit")


async def read_bounded_body(request: Any, max_bytes: int = 262_144) -> bytes:
    """Bound streaming/chunked bodies as well as Content-Length requests."""

    from fastapi import HTTPException

    enforce_body_limit(request, max_bytes=max_bytes)
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="request body exceeds policy limit")
        chunks.append(chunk)
    return b"".join(chunks)
