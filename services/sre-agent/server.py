from fastmcp import FastMCP
from src.health import register_health_tools
from src.docker_tools import register_docker_tools

# Initialize the MCP server once
mcp = FastMCP("SRE Bug Hunter")

# Register tools from your modules
register_health_tools(mcp)
register_docker_tools(mcp)


if __name__ == "__main__":
    mcp.run()