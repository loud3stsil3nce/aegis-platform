"""Bounded adapters for HTTP-contract and monitored-project plugin modes."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

MAX_RESPONSE_BYTES = 16_384


class ContractAdapterError(RuntimeError):
    pass


class HttpContractAdapter:
    def __init__(self, token_provider: Callable[[str], str], timeout: float = 5.0):
        self.token_provider, self.timeout = token_provider, timeout

    def _get(self, plugin: dict[str, Any], path: str, query: dict[str, int] | None = None) -> Any:
        runtime = plugin["manifest"]["spec"]["runtime"]
        base = runtime.get("endpoint", "").rstrip("/")
        if not base:
            raise ContractAdapterError("plugin endpoint is not configured")
        url = base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.token_provider(plugin['id'])}"}, method="GET"
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ContractAdapterError("plugin contract response exceeds policy limit")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContractAdapterError("plugin contract returned invalid JSON") from exc

    def health(self, plugin): return self._get(plugin, plugin["manifest"]["spec"]["health"]["readiness"])
    def version(self, plugin): return self._get(plugin, plugin["manifest"]["spec"]["health"]["version"])
    def metrics(self, plugin): return self._get(plugin, plugin["manifest"]["spec"]["health"]["metrics"])
    def logs(self, plugin, lines):
        payload = self._get(plugin, plugin["manifest"]["spec"]["health"]["logs"], {"lines": lines})
        return str(payload.get("logs", "")) if isinstance(payload, dict) else str(payload)


class MonitoredProjectAdapter:
    def _path(self, plugin: dict[str, Any], field: str) -> Path:
        runtime = plugin["manifest"]["spec"]["runtime"]
        root = Path(runtime["projectPath"]).resolve()
        path = (root / runtime[field]).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ContractAdapterError("monitored file escaped project root") from exc
        if not path.is_file():
            raise ContractAdapterError(f"monitored {field} is unavailable")
        return path

    def _json(self, plugin, field):
        raw = self._path(plugin, field).read_bytes()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ContractAdapterError("monitored result exceeds policy limit")
        return json.loads(raw)

    def health(self, plugin): return self._json(plugin, "statusFile")
    def version(self, plugin): return self._json(plugin, "versionFile")
    def metrics(self, plugin): return self._json(plugin, "metricsFile")
    def logs(self, plugin, lines):
        raw = self._path(plugin, "logFile").read_bytes()
        if len(raw) > MAX_RESPONSE_BYTES:
            raw = raw[-MAX_RESPONSE_BYTES:]
        return "\n".join(raw.decode("utf-8", errors="replace").splitlines()[-lines:])
