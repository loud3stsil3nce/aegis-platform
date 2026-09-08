"""Bounded HTTP adapter for the separately authenticated lifecycle proxy."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


MAX_RESPONSE_BYTES = 16_384


class LifecycleAdapterError(RuntimeError):
    pass


class HttpLifecycleAdapter:
    def __init__(
        self, base_url: str, token_provider: Callable[[], str],
        *, request_timeout: float = 5.0, poll_interval: float = 0.5,
    ):
        parsed = urllib.parse.urlsplit(base_url)
        if (
            parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
        ):
            raise ValueError("lifecycle proxy URL must be an internal HTTP origin")
        self.base_url = base_url.rstrip("/")
        self.token_provider = token_provider
        self.request_timeout = request_timeout
        self.poll_interval = poll_interval

    def _request(self, method: str, target: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        safe_target = urllib.parse.quote(target, safe="")
        suffix = "state" if method == "GET" else "restart"
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        request = urllib.request.Request(
            f"{self.base_url}/v1/targets/{safe_target}/{suffix}", data=body, method=method,
            headers={"Authorization": f"Bearer {self.token_provider()}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LifecycleAdapterError("lifecycle proxy request failed") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise LifecycleAdapterError("lifecycle proxy response exceeds policy limit")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LifecycleAdapterError("lifecycle proxy returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise LifecycleAdapterError("lifecycle proxy returned an invalid object")
        return value

    def state(self, target: str) -> dict[str, Any]:
        return self._request("GET", target)

    def restart(self, target: str, idempotency_key: str) -> None:
        result = self._request("POST", target, {"idempotency_key": idempotency_key})
        if result.get("status") not in {"accepted", "already_applied"}:
            raise LifecycleAdapterError("lifecycle proxy did not accept restart")

    def wait_ready(self, target: str, timeout_seconds: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.state(target)
            if last.get("status") == "running" and last.get("health") in {"healthy", "ready"}:
                return last
            time.sleep(self.poll_interval)
        return last
