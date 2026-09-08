"""Read-only tools backed by the narrow Aegis Docker Observer API."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_MAX_LOG_LINES = 200
DEFAULT_TIMEOUT_SECONDS = 8.0


def _observer_url() -> str:
    value = os.getenv("AEGIS_DOCKER_OBSERVER_URL", "").rstrip("/")
    if not value:
        raise RuntimeError("AEGIS_DOCKER_OBSERVER_URL is not configured")
    return value


def _observer_token() -> str:
    value = os.getenv("AEGIS_DOCKER_OBSERVER_TOKEN", "")
    if not value:
        raise RuntimeError("AEGIS_DOCKER_OBSERVER_TOKEN is not configured")
    return value


def _get(path: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{_observer_url()}{path}",
        headers={"Authorization": f"Bearer {_observer_token()}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            payload = response.read(32_768)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ValueError("requested container is not registered") from exc
        raise RuntimeError(f"Docker observer returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Docker observer is unavailable") from exc
    return json.loads(payload.decode("utf-8"))


def list_registered_containers() -> str:
    return json.dumps(_get("/v1/containers"), sort_keys=True)


def get_container_health(container_name: str) -> str:
    if not isinstance(container_name, str) or not container_name:
        raise ValueError("container_name is required")
    name = urllib.parse.quote(container_name, safe="")
    return json.dumps(_get(f"/v1/containers/{name}/state"), sort_keys=True)


def get_bounded_logs(container_name: str, lines: int = 50) -> str:
    if not isinstance(container_name, str) or not container_name:
        raise ValueError("container_name is required")
    if not isinstance(lines, int) or isinstance(lines, bool):
        raise ValueError("lines must be an integer")
    safe_lines = max(1, min(lines, DEFAULT_MAX_LOG_LINES))
    name = urllib.parse.quote(container_name, safe="")
    payload = _get(f"/v1/containers/{name}/logs?lines={safe_lines}")
    return str(payload.get("logs", ""))


def search_bounded_logs(container_name: str, keyword: str, lines: int = 200) -> str:
    if not isinstance(keyword, str) or not keyword.strip():
        raise ValueError("keyword is required")
    if len(keyword) > 100:
        raise ValueError("keyword exceeds 100 characters")
    logs = get_bounded_logs(container_name, lines)
    needle = keyword.casefold()
    matches = [line for line in logs.splitlines() if needle in line.casefold()]
    return "\n".join(matches) if matches else "No matching log lines found."


def register_observability_tools(mcp: Any) -> None:
    mcp.tool()(list_registered_containers)
    mcp.tool()(get_container_health)
    mcp.tool()(get_bounded_logs)
    mcp.tool()(search_bounded_logs)
