import os

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

TOKEN = os.getenv("AEGIS_PLUGIN_SERVICE_TOKEN")
if not TOKEN: raise RuntimeError("AEGIS_PLUGIN_SERVICE_TOKEN is required")
mcp = FastMCP("remote-sample", auth=StaticTokenVerifier(tokens={TOKEN: {"sub": "aegis-core", "client_id": "aegis-core"}}))

@mcp.tool
def remote_status() -> str: return "remote sample ready"

async def live(_): return JSONResponse({"status": "live"})
async def ready(_): return JSONResponse({"status": "ready", "transport": "https"})
async def version(_): return JSONResponse({"id": "remote-sample", "version": "0.1.0"})
async def metrics(_): return JSONResponse({"tls": 1, "tools": 1})
async def logs(request): return JSONResponse({"logs": "remote sample ready", "lines": min(int(request.query_params.get("lines", 50)), 200)})

mcp_app = mcp.http_app(path="/mcp", stateless_http=True)
app = Starlette(routes=[Route("/health/live", live), Route("/health/ready", ready), Route("/version", version), Route("/metrics", metrics), Route("/logs", logs), Mount("/", app=mcp_app)], lifespan=mcp_app.lifespan)
