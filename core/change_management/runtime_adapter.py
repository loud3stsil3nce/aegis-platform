"""Bounded HTTP adapter for the isolated immutable-image runtime proxy."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


MAX_RESPONSE_BYTES = 16_384


class RuntimeAdapterError(RuntimeError):
    pass


class HttpDeploymentAdapter:
    def __init__(
        self, base_url: str, token_provider: Callable[[], str],
        *, request_timeout: float = 5.0, poll_interval: float = 0.5,
    ):
        parsed = urllib.parse.urlsplit(base_url)
        if (
            parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
        ):
            raise ValueError("deployment proxy URL must be an internal HTTP origin")
        if request_timeout <= 0 or poll_interval < 0:
            raise ValueError("deployment proxy timeouts are invalid")
        self.base_url = base_url.rstrip("/")
        self.token_provider = token_provider
        self.request_timeout = request_timeout
        self.poll_interval = poll_interval

    def _request(
        self, method: str, service: str, payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        safe_service = urllib.parse.quote(service, safe="")
        suffix = "state" if method == "GET" else "image"
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        token = self.token_provider()
        if not isinstance(token, str) or not token:
            raise RuntimeAdapterError("deployment proxy authentication unavailable")
        request = urllib.request.Request(
            f"{self.base_url}/v1/targets/{safe_service}/{suffix}", data=body, method=method,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeAdapterError("deployment proxy request failed") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeAdapterError("deployment proxy response exceeds policy limit")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeAdapterError("deployment proxy returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeAdapterError("deployment proxy returned an invalid object")
        return value

    def state(self, service: str) -> dict[str, Any]:
        return self._request("GET", service)

    def current_image(self, service: str) -> str:
        image = self.state(service).get("image")
        if (
            not isinstance(image, str) or not image or len(image.encode()) > 300
            or any(character.isspace() or ord(character) < 32 for character in image)
        ):
            raise RuntimeAdapterError("deployment proxy returned an invalid image reference")
        return image

    def deploy_image(self, service: str, image_reference: str, idempotency_key: str) -> None:
        result = self._request("POST", service, {
            "image_reference": image_reference,
            "idempotency_key": idempotency_key,
        })
        if result.get("status") not in {"accepted", "already_applied"}:
            raise RuntimeAdapterError("deployment proxy did not accept image change")

    def wait_healthy(self, service: str, timeout_seconds: int) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            state = self.state(service)
            if state.get("status") == "running" and state.get("health") in {"healthy", "ready"}:
                return True
            time.sleep(self.poll_interval)
        return False
