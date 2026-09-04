"""Minimal authenticated Streamable HTTP plugin used by the conformance suite."""

import os

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

TOKEN = os.getenv("AEGIS_PLUGIN_SERVICE_TOKEN")
if not TOKEN:
    raise RuntimeError("AEGIS_PLUGIN_SERVICE_TOKEN is required")

auth = StaticTokenVerifier(tokens={TOKEN: {"sub": "aegis-core", "client_id": "aegis-core"}})
mcp = FastMCP("hello-aegis", auth=auth)


@mcp.tool
def hello(name: str = "operator") -> str:
    """Return a static greeting without accessing networks, secrets, or storage."""
    return f"Hello, {name}. Aegis plugin isolation is working."


async def live(_request):
    return JSONResponse({"status": "live"})


async def ready(_request):
    return JSONResponse({"status": "ready"})


async def version(_request):
    return JSONResponse({"id": "hello-aegis", "version": "0.1.0", "apiVersion": "aegis.dev/v1alpha1"})


async def metrics(_request):
    return JSONResponse({"plugin": "hello-aegis", "status": "ready", "tools": 1})


async def logs(request):
    lines = min(max(int(request.query_params.get("lines", "50")), 1), 200)
    return JSONResponse({"lines": lines, "logs": "hello-aegis ready"})


mcp_app = mcp.http_app(path="/mcp", stateless_http=True)
app = Starlette(
    routes=[
        Route("/health/live", live),
        Route("/health/ready", ready),
        Route("/version", version),
        Route("/metrics", metrics),
        Route("/logs", logs),
        Mount("/", app=mcp_app),
    ],
    lifespan=mcp_app.lifespan,
)
