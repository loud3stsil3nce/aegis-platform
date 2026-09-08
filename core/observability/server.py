"""Authenticated Streamable HTTP façade for read-only plugin observations."""

from __future__ import annotations

import os
import re
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from core.audit import AuditStore
from core.observability.adapters import HttpContractAdapter, MonitoredProjectAdapter
from core.observability.service import ObservabilityService
from core.registry import PluginRegistry

_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


REGISTRY_PATH = Path(_required("AEGIS_PLUGIN_REGISTRY_PATH"))
AUDIT_PATH = Path(_required("AEGIS_AUDIT_PATH"))
TOKEN_DIR = Path(_required("AEGIS_PLUGIN_TOKEN_DIR")).resolve()
INBOUND_TOKEN = _required("AEGIS_OBSERVABILITY_TOKEN")

registry = PluginRegistry(REGISTRY_PATH.parent)
audit = AuditStore(AUDIT_PATH)


def plugin_token(plugin_id: str) -> str:
    if not _PLUGIN_ID.fullmatch(plugin_id):
        raise RuntimeError("invalid plugin id")
    path = (TOKEN_DIR / f"{plugin_id}.token").resolve()
    try:
        path.relative_to(TOKEN_DIR)
    except ValueError as exc:
        raise RuntimeError("plugin token path escaped token directory") from exc
    raw = path.read_bytes()
    if not raw or len(raw) > 4096:
        raise RuntimeError("plugin token is missing or invalid")
    return raw.decode().strip()


http_adapter = HttpContractAdapter(plugin_token)
service = ObservabilityService(
    registry,
    audit,
    {"remote": http_adapter, "managed-container": http_adapter, "monitored-project": MonitoredProjectAdapter()},
)
auth = StaticTokenVerifier(tokens={INBOUND_TOKEN: {"sub": "aegis-operator", "client_id": "aegis-core"}})
mcp = FastMCP("Aegis Observability", auth=auth)


@mcp.tool
def list_plugins() -> list[dict[str, str]]:
    """List enabled registered plugin identities without secrets or application data."""
    return [
        {"id": item["id"], "version": item["version"], "mode": item["runtimeMode"], "status": item["status"]}
        for item in registry.list() if item["status"] == "enabled"
    ]


@mcp.tool
def get_plugin_health(plugin_id: str) -> str:
    """Return bounded, untrusted readiness data for one registered plugin."""
    return service.observe(plugin_id, "health", actor="mcp-operator")


@mcp.tool
def get_plugin_version(plugin_id: str) -> str:
    """Return bounded, untrusted version data for one registered plugin."""
    return service.observe(plugin_id, "version", actor="mcp-operator")


@mcp.tool
def get_plugin_metrics(plugin_id: str) -> str:
    """Return bounded, untrusted metrics for one registered plugin."""
    return service.observe(plugin_id, "metrics", actor="mcp-operator")


@mcp.tool
def get_plugin_logs(plugin_id: str, lines: int = 50) -> str:
    """Return redacted, bounded, untrusted log lines for one registered plugin."""
    return service.observe(plugin_id, "logs", actor="mcp-operator", lines=lines)


async def health(_request):
    return JSONResponse({"status": "ok"})


async def metrics(_request):
    events = audit.connection.execute("SELECT status,COUNT(*) AS count FROM audit_events GROUP BY status").fetchall()
    return JSONResponse({"auditEvents": {row["status"]: row["count"] for row in events}})


mcp_app = mcp.http_app(path="/mcp", stateless_http=True)
app = Starlette(
    routes=[Route("/health", health), Route("/metrics", metrics), Mount("/", app=mcp_app)],
    lifespan=mcp_app.lifespan,
)
