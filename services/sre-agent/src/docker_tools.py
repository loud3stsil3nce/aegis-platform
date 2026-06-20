import subprocess
from fastmcp import FastMCP
from src.security import validate_command
import re
import math
import urllib.request
import urllib.error
import json
from datetime import datetime
from src.db.database import async_session                                                                     
from src.db.models import AgentLog 

PROJECT_ROOT = "/home/rafi/shariahcompliantscreener"
STATUS_FILE_PATH = "/home/rafi/shariahcompliantscreener/status.json"
def register_docker_tools(mcp: FastMCP):

    @mcp.tool()
    def list_containers() -> str:
        """Lists currently running Docker containers."""
        try:
            # Security Guardrail
            validate_command('docker')
            
            # Using the CLI to get formatted output
            cmd = ['docker', 'ps', '--format', '{{.Names}} (Image: {{.Image}}, Status: {{.Status}})']
            result = subprocess.check_output(cmd, text=True)
            
            if not result.strip():
                return "No containers are currently running."
            
            return f"Currently running containers:\n{result}"
            
        except Exception as e:
            return f"Infrastructure Error: Could not reach Docker. Details: {str(e)}"
        
    @mcp.tool()
    def check_app_health(container_name: str = "shariahscreener") -> str:
        """Checks the health and uptime of the Shariah Compliance Screener container."""
        try:
            validate_command('docker')
            
            cmd = ["docker", "inspect", "--format", "{{.State.Status}} (Started: {{.State.StartedAt}})", container_name]
            status = subprocess.check_output(cmd, text=True).strip()
            
            return f"Container '{container_name}' status: {status}"
        except subprocess.CalledProcessError:
            return f"Error: Container '{container_name}' not found."
        except Exception as e:
            return f"Infrastructure Error: {str(e)}"
        
    @mcp.tool()
    def get_recent_logs(container_name: str = "shariahscreener", lines: int = 50) -> str:
        """
        Fetches the last N lines of logs from the container. 
        Use this to debug crashes, quota errors, or feature bugs.
        """
        try:
            validate_command('docker')
            
            # We use --tail to prevent loading massive log files into memory
            cmd = ['docker', 'logs', '--tail', str(lines), container_name]
            logs = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
            
            if not logs.strip():
                return "No logs found for this container."
                
            return f"Last {lines} lines of logs for {container_name}:\n\n{logs}"
            
        except subprocess.CalledProcessError as e:
            return f"Error retrieving logs: {e.output.decode() if e.output else 'Container not found.'}"
        except Exception as e:
            return f"Infrastructure Error: {str(e)}"
        
        
    @mcp.tool()
    def search_logs(keyword: str, container_name: str = "shariahscreener") -> str:
        """Searches logs for a specific keyword."""
        try:
            validate_command('docker')
            # 1. Fetch logs safely using subprocess
            cmd = ['docker', 'logs', '--tail', '200', container_name]
            logs = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
            
            # 2. Filter in Python (Safe from injection)
            matches = [line for line in logs.splitlines() if keyword.lower() in line.lower()]
            
            if not matches:
                return f"No occurrences of '{keyword}' found."
                
            return f"Found {len(matches)} occurrences:\n" + "\n".join(matches)
        except Exception as e:
            return f"Error: {str(e)}"
        
    @mcp.tool()
    def restart_container(container_name: str = "shariahscreener") -> str:
        """
        Restarts a Docker container. 
        Use this ONLY if the container is unhealthy or if logs indicate a fatal crash that requires a reboot.
        """
        try:
            validate_command('docker')
            # We use subprocess.run for actions that modify state
            cmd = ['docker', 'restart', container_name]
            
            # check=True ensures an exception is thrown if the command fails
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            
            return f"Successfully initiated restart sequence for container: '{container_name}'."
        except subprocess.CalledProcessError as e:
            return f"Failed to restart '{container_name}'. Error: {e.stderr}"
        except Exception as e:
            return f"System Error during restart: {str(e)}"
        
    @mcp.tool()
    def analyze_latency_p95(container_name: str = "shariahscreener", lines: int = 500) -> str:
        """
        Analyzes recent logs to calculate the 95th percentile (P95) response time.
        Use this to determine if the application is experiencing performance degradation.
        """
        try:
            validate_command('docker')
            cmd = ['docker', 'logs', '--tail', str(lines), container_name]
            logs = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
            
            # Regex to find response times in logs (e.g., "Response time: 142ms" or "completed in 0.4s")
            # Adjust the regex based on how your specific app logs its execution time
            latencies = []
            pattern = r'(\d+(?:\.\d+)?)\s*(ms|s)' 
            
            for line in logs.splitlines():
                match = re.search(pattern, line)
                if match:
                    value = float(match.group(1))
                    unit = match.group(2)
                    # Normalize to milliseconds
                    if unit == 's':
                        value *= 1000
                    latencies.append(value)
                    
            if not latencies:
                return "No latency metrics found in the recent logs to analyze."
                
            # Math: Calculate P95
            latencies.sort()
            p95_index = math.ceil(0.95 * len(latencies)) - 1
            p95_latency = latencies[p95_index]
            avg_latency = sum(latencies) / len(latencies)
            
            return (f"Analyzed {len(latencies)} requests.\n"
                    f"Average Latency: {avg_latency:.2f}ms\n"
                    f"P95 Latency: {p95_latency:.2f}ms\n"
                    f"(This means 95% of all requests were served in {p95_latency:.2f}ms or less).")
                    
        except Exception as e:
            return f"Error analyzing latency: {str(e)}"
        
        
    @mcp.tool()
    def ping_web_app(url: str = "http://localhost:8501") -> str:
        """
        Performs a synthetic health check by pinging the web application.
        Use this to verify if the Streamlit app is actually serving pages, not just if Docker is running.
        """
        try:
            # A simple GET request with a 5-second timeout so the AI doesn't hang
            with urllib.request.urlopen(url, timeout=5) as response:
                status = response.getcode()
                return f"✅ SUCCESS: The application at {url} is online and returned HTTP Status {status}."
                
        except urllib.error.HTTPError as e:
            return f"⚠️ WARNING: The container is running, but the app crashed. HTTP Status: {e.code}."
            
        except urllib.error.URLError as e:
            return f"🚨 CRITICAL: The application at {url} is completely unreachable. Details: {e.reason}"
            
        except Exception as e:
            return f"System Error: {str(e)}"
    
    
    @mcp.tool()
    def kill_container(target: str = "shariahscreener") -> str:
        """
        Kills a container. 
        If 'target' is a specific ID/name, it kills that. 
        If it's 'shariahscreener', it uses the service filter to find all matching active containers.
        """
        try:
            # 1. Get all matching container IDs
            # -q: only IDs, -f: filter, -a: include stopped containers (optional, usually -q is enough)
            cmd = ["docker", "ps", "-q", "-f", f"name={target}"]
            output = subprocess.check_output(cmd, text=True).strip()
            
            if not output:
                return f"✅ No running containers matching '{target}' found."

            # 2. Split by newline to handle cases where multiple containers match
            container_ids = output.splitlines()
            
            results = []
            for cid in container_ids:
                # Force stop and remove
                subprocess.run(["docker", "stop", cid], check=True, capture_output=True)
                subprocess.run(["docker", "rm", cid], check=True, capture_output=True)
                results.append(cid[:12])
                
            return f"💀 Exorcised {len(results)} container(s): {', '.join(results)}."
            
        except subprocess.CalledProcessError as e:
            # Capture stderr for better debugging
            error_msg = e.stderr.decode().strip() if isinstance(e.stderr, bytes) else str(e)
            return f"🚨 Failed to kill container: {error_msg}"
          
        
    @mcp.tool()
    async def update_and_restart_app() -> str:
        """
        Full Deployment Pipeline:
        1. Removes any rogue manual containers.
        2. Brings down existing compose project.
        3. Rebuilds and restarts the stack.
        4. Updates the operational dashboard status.
        """
        try:
            # Step 1: Clean the environment
            kill_container("shariahscreener")
            subprocess.run(["docker-compose", "down"], cwd=PROJECT_ROOT, check=True)
            
            # Step 2: Deploy
            result = subprocess.run(
                ["docker-compose", "up", "-d", "--build"], 
                cwd=PROJECT_ROOT, 
                capture_output=True, 
                text=True, 
                check=True
            )
            
            # Step 3: Success Reporting
            await update_status_dashboard("running", "success", "Pipeline complete: Cleanup, Rebuild, and Restart successful.")
            return "✅ Pipeline successful: Rogue cleanup + Rebuild + Restart complete."
            
        except subprocess.CalledProcessError as e:
            # Step 4: Failure Reporting
            await update_status_dashboard("error", "failed", f"Pipeline crashed: {e.stderr[:50]}...")
            return f"🚨 CRITICAL: Pipeline failed. Details: {e.stderr}"
            
        
    @mcp.tool()
    async def update_status_dashboard(status: str, result: str, notes: str) -> str:
        """
        Updates the operational status dashboard for the system by saving metrics to the database.          
        """
        status_data = {
            "timestamp": datetime.utcnow().isoformat(),
            "system": {
                "container_name": "shariahcompliantscreener-shariahscreener-1",
                "status": status,
                "port": 8501
            },
            "last_action": {
                "type": "maintenance",
                "timestamp": datetime.utcnow().isoformat(),
                "result": result,
                "notes": notes
            }
        }
        
        try:
            async with async_session() as session:
                async with session.begin():
                    log_entry = AgentLog(
                        container_name="shariahcompliantscreener-shariahscreener-1",
                        log_level="INFO" if result == "success" else "ERROR",
                        message=notes,
                        status_snapshot=status_data
                    )
                    session.add(log_entry)
            return "✅ Dashboard status updated in PostgreSQL database."
        except Exception as e:
            return f"Failed to update database: {str(e)}"
