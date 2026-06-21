from fastmcp import FastMCP
from src.health import register_health_tools
from src.docker_tools import register_docker_tools
from src.db.database import engine
from src.db.models import Base
from src.vcs_tools import register_vcs_tools
import asyncio
# Initialize the MCP server once
mcp = FastMCP("SRE Bug Hunter")

# Register tools from your modules
register_health_tools(mcp)
register_docker_tools(mcp)
register_vcs_tools(mcp)

  
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# Run the database initialization before the FastMCP server runs
asyncio.run(init_db())
        
if __name__ == "__main__":
    mcp.run()