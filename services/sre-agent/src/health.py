import subprocess
from fastmcp import FastMCP
from src.security import validate_command


def register_health_tools(mcp: FastMCP):

    @mcp.tool()
    def check_server_health() -> str:
        """Runs pre-approved system commands to check RAM and Disk."""
        try:
            # validates commands
            validate_command('free')
            validate_command('df')
            
            # Run the 'free -m' command to check RAM
            memory = subprocess.check_output(['free', '-m'], text=True)
            # Run the 'df -h' command to check Disk Space
            disk = subprocess.check_output(['df', '-h'], text=True)
            
            return f"--- MEMORY USAGE ---\n{memory}\n\n--- DISK USAGE ---\n{disk}"
        except Exception as e:
            return f"Failed to check server health: {str(e)}"


