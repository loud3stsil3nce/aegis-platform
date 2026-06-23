import asyncio
from fastmcp import FastMCP
from src.vcs_tools import register_vcs_tools

mcp = FastMCP("Test")
register_vcs_tools(mcp)

async def test():
    tools = await mcp.list_tools()
    for t in tools:
        if t.name == "create_branch":
            mcp_tool = t.to_mcp_tool()
            print("Name:", mcp_tool.name)
            print("Description:", mcp_tool.description)
            print("Input Schema:", mcp_tool.inputSchema)

asyncio.run(test())
