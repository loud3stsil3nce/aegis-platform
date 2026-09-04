"""End-to-end authenticated MCP verification; never prints the service token."""

import asyncio
import os

from fastmcp import Client


async def main() -> None:
    token = os.environ["AEGIS_PLUGIN_SERVICE_TOKEN"]
    async with Client("http://127.0.0.1:8090/mcp", auth=token) as client:
        tools = await client.list_tools()
        names = [tool.name for tool in tools]
        if names != ["hello"]:
            raise RuntimeError(f"unexpected tool inventory: {names}")
        result = await client.call_tool("hello", {"name": "Aegis"})
        if "plugin isolation is working" not in str(result.data):
            raise RuntimeError("unexpected hello tool result")
        print("authenticated MCP list/call: pass")


if __name__ == "__main__":
    asyncio.run(main())
