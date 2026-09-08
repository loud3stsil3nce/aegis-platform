"""End-to-end verifier for the deployed authenticated Observability MCP."""

import asyncio
import os

from fastmcp import Client
from fastmcp.exceptions import ToolError

EXPECTED_TOOLS = {
    "list_plugins", "get_plugin_health", "get_plugin_version",
    "get_plugin_metrics", "get_plugin_logs",
}


async def main() -> None:
    async with Client("http://127.0.0.1:8020/mcp", auth=os.environ["AEGIS_OBSERVABILITY_TOKEN"]) as client:
        names = {tool.name for tool in await client.list_tools()}
        if names != EXPECTED_TOOLS:
            raise RuntimeError(f"unexpected tool inventory: {sorted(names)}")
        plugins = await client.call_tool("list_plugins", {})
        listing = str(plugins.data)
        expected_plugins = ("hello-aegis", "monitored-sample", "remote-sample")
        if any(plugin_id not in listing for plugin_id in expected_plugins):
            raise RuntimeError("expected managed, monitored, and remote sample plugins were not listed")
        for plugin_id in expected_plugins:
            for tool in ("get_plugin_health", "get_plugin_version", "get_plugin_metrics", "get_plugin_logs"):
                arguments = {"plugin_id": plugin_id}
                if tool == "get_plugin_logs": arguments["lines"] = 25
                result = await client.call_tool(tool, arguments)
                value = str(result.data)
                if "UNTRUSTED_DATA" not in value:
                    raise RuntimeError(f"{plugin_id} {tool} result was not labeled untrusted")
                if plugin_id == "monitored-sample" and tool == "get_plugin_logs":
                    if "authorization=[REDACTED]" not in value or "IGNORE PREVIOUS INSTRUCTIONS" not in value:
                        raise RuntimeError("live prompt-injection/redaction fixture did not satisfy policy")
        try:
            await client.call_tool("get_plugin_health", {"plugin_id": "not-registered"})
        except ToolError:
            pass
        else:
            raise RuntimeError("unknown plugin observation unexpectedly succeeded")
        print("authenticated read-only observability MCP: pass")


if __name__ == "__main__":
    asyncio.run(main())
