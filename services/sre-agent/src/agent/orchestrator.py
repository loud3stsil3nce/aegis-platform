import os
import sys
import asyncio
import json
from typing import List, Dict, Any
from mcp import ClientSession
from mcp.client.sse import sse_client
from jira import JIRA
from anthropic import Anthropic

# Add path so we can import local modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.db.database import async_session
from src.db.models import AgentLog, SystemHealth, AuditTrail
from src.vcs_tools import active_issue_key
from datetime import datetime

# Setup Jira Client
JIRA_URL = os.getenv("JIRA_URL", "https://your-domain.atlassian.net")
JIRA_USER_EMAIL = os.getenv("JIRA_USER_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY")

jira = None
if JIRA_USER_EMAIL and JIRA_API_TOKEN:
    try:
        jira = JIRA(server=JIRA_URL, basic_auth=(JIRA_USER_EMAIL, JIRA_API_TOKEN))
        print("Connected to Jira successfully.")
    except Exception as e:
        print(f"Jira Connection Error: {str(e)}")

# Initialize Anthropic Client
anthropic = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Define Remote MCP URLs
REMOTE_SERVERS = {
    "screener": "http://shariah-screener:8001/mcp/sse",
    "reeftracker": "http://reeftracker:8003/sse",
    "messenger": "http://e2ee-messenger:8080/mcp/sse"
}

# Destructive actions requiring human approval
DESTRUCTIVE_ACTIONS = ["restart_container", "kill_container", "update_and_restart_app"]

async def execute_agent_sweep(issue_key: str = None, user_command: str = None):
    """
    Executes an autonomous system monitoring and stock compliance sweep.
    If no issue_key is provided, creates a new Jira task.
    """
    if not jira:
        print("Error: Jira client not configured. Sweep cancelled.")
        return

    # 1. Ensure we have an active Jira ticket context
    if not issue_key:
        try:
            issue = jira.create_issue(
                project=JIRA_PROJECT_KEY,
                summary=f"SRE Audit Sweep - {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC",
                description="Autonomous diagnostic sweep to check container health and stock watchlists.",
                issuetype={'name': 'Task'}
            )
            issue_key = issue.key
            print(f"Created new Jira Task: {issue_key}")
        except Exception as e:
            print(f"Failed to create Jira Issue: {str(e)}")
            return

    active_issue_key.set(issue_key)

    # 2. Establish connections to remote MCP servers
    sessions: Dict[str, ClientSession] = {}

    # We will use a context exit stack to clean up connections
    from contextlib import AsyncExitStack
    async with AsyncExitStack() as stack:
        # A. Connect to remote servers
        for name, url in REMOTE_SERVERS.items():
            try:
                print(f"Connecting to remote MCP server '{name}' at {url}...")
                transport = await asyncio.wait_for(stack.enter_async_context(sse_client(url)), timeout=10.0)
                session = await asyncio.wait_for(stack.enter_async_context(ClientSession(transport[0], transport[1])), timeout=10.0)
                await asyncio.wait_for(session.initialize(), timeout=10.0)
                sessions[name] = session
                print(f"Successfully connected to remote MCP '{name}'.")
            except Exception as e:
                print(f"Failed to connect to remote MCP server '{name}': {str(e)}")

        # B. Load local SRE tools from modules
        # Import SRE tools directly to query locally
        from src.health import register_health_tools
        from src.docker_tools import register_docker_tools
        from src.vcs_tools import register_vcs_tools
        from fastmcp import FastMCP

        local_mcp = FastMCP("Local SRE")
        register_health_tools(local_mcp)
        register_docker_tools(local_mcp)
        register_vcs_tools(local_mcp)

        # C. Aggregate tool schemas for Anthropic API
        all_tools = []
        tool_mappings = {}  # Map tool names to execution handlers

        # Aggregate Local SRE tools
        local_tools_list = await local_mcp.list_tools()
        for t in local_tools_list:
            mcp_tool = t.to_mcp_tool()
            all_tools.append({
                "name": mcp_tool.name,
                "description": mcp_tool.description,
                "input_schema": mcp_tool.inputSchema
            })
            tool_mappings[mcp_tool.name] = {"source": "local", "handler": local_mcp}

        # Aggregate Remote tools from sessions
        for mcp_name, session in sessions.items():
            try:
                remote_tools_list = await asyncio.wait_for(session.list_tools(), timeout=10.0)
                for t in remote_tools_list.tools:
                    # Prefix tool names to avoid namespace collisions
                    prefixed_name = f"{mcp_name}_{t.name}"
                    all_tools.append({
                        "name": prefixed_name,
                        "description": t.description,
                        "input_schema": t.inputSchema
                    })
                    tool_mappings[prefixed_name] = {
                        "source": "remote",
                        "handler": session,
                        "original_name": t.name
                    }
            except Exception as e:
                print(f"Failed to list tools for remote MCP server '{mcp_name}': {str(e)}")

        print(f"Aggregated {len(all_tools)} tools for LLM agent.")

        # D. LLM Reasoning Loop
        system_prompt = (
            "You are Aegis Platform's automated SRE and Shariah Compliance Auditor.\n"
            "Your duties:\n"
            "1. Check container health, RAM, and disk utilization of the system.\n"
            "2. Fetch the watchlist of stock tickers and run Shariah compliance checks on them.\n"
            "3. If container issues, anomalies, or compliance failures are detected, log them in your reasoning.\n"
            "4. Post a summary of your findings as comments on the Jira task.\n"
            "If you need to perform action overrides, rebuilds, or container restarts, execute them. "
            "Note that destructive commands require human approval; when you invoke them, they will pause and request permission.\n"
            "\n"
            "IMPORTANT: When using VCS/GitHub tools (such as get_file_from_api, create_branch, commit_file_change, create_pr, list_branches, and list_files_in_branch), the default repository is 'loud3stsil3nce/aegis-platform'. Make sure to pass 'loud3stsil3nce/aegis-platform' as the `repo_name` argument unless instructed otherwise. When creating a branch, make sure to specify both `repo_name` and `new_branch` values explicitly."
        )

        initial_prompt = f"Start the system monitoring sweep and watchlist audit for Jira issue: {issue_key}"
        if user_command:
            initial_prompt = f"The user has posted a command inside Jira issue {issue_key}: '{user_command}'. Please resolve this query using your tools."

        messages = [
            {"role": "user", "content": initial_prompt}
        ]

        jira.add_comment(issue_key, "🤖 *SRE Agent*: Starting autonomous audit sweep...")

        max_steps = 15
        run_openai_fallback = False
        for step in range(max_steps):
            print(f"Step {step+1}: Calling LLM...")
            try:
                response = anthropic.beta.messages.create(
                    model="claude-3-5-sonnet-20241022",
                    max_tokens=4000,
                    system=system_prompt,
                    messages=messages,
                    tools=all_tools
                )
            except Exception as api_err:
                print(f"Anthropic LLM API Call failed: {str(api_err)}.")
                if os.getenv("OPENAI_API_KEY"):
                    print("OPENAI_API_KEY detected. Transitioning to OpenAI fallback loop...")
                    run_openai_fallback = True
                else:
                    print("No OpenAI API key found. Triggering simulation fallback loop...")
                    await run_simulated_sweep_loop(
                        issue_key=issue_key,
                        user_command=user_command,
                        jira=jira,
                        local_mcp=local_mcp,
                        sessions=sessions,
                        tool_mappings=tool_mappings,
                        all_tools=all_tools
                    )
                break

            # Check if LLM wants to stop
            if response.stop_reason == "end_turn":
                summary_content = response.content[0].text
                jira.add_comment(issue_key, f"🤖 *SRE Agent Sweep Completed*:\n\n{summary_content}")
                jira.transition_issue(issue_key, transition="Done")
                print("Agent sweep finished.")
                break

            # Handle tool calls
            elif response.stop_reason == "tool_use":
                # Save agent response to message history
                messages.append({"role": "assistant", "content": response.content})

                tool_results_content = []
                for content_block in response.content:
                    if content_block.type == "tool_use":
                        tool_name = content_block.name
                        tool_input = content_block.input
                        tool_id = content_block.id

                        print(f"Agent calls tool: {tool_name} with parameters: {tool_input}")

                        # --- HITL SECURITY GATE ---
                        # Check if this tool requires human approval
                        base_tool_name = tool_name.split("_")[-1] if "_" in tool_name else tool_name
                        if base_tool_name in DESTRUCTIVE_ACTIONS:
                            # 1. Post to Jira and transition ticket to Pending
                            jira.add_comment(
                                issue_key,
                                f"⚠️ *SRE Agent* requests approval to run destructive action: `{tool_name}({json_str(tool_input)})`.\n"
                                "Please transition this Jira issue status to *'Approved'* or *'Rejected'* to proceed."
                            )
                            try:
                                jira.transition_issue(issue_key, transition="Pending SRE Approval")
                            except Exception:
                                pass # Transition might not be configured, proceed with comment check

                            # 2. Register in the server's pending map
                            from server import pending_approvals
                            event = asyncio.Event()
                            pending_approvals[issue_key] = {"event": event, "decision": None}

                            # 3. Wait for webhook update (timeout 5 minutes)
                            print(f"Blocking thread for Jira approval on issue {issue_key}...")
                            try:
                                await asyncio.wait_for(event.wait(), timeout=300)
                                decision = pending_approvals[issue_key]["decision"]
                            except asyncio.TimeoutError:
                                decision = "TIMEOUT"

                            # Clean up mapping
                            del pending_approvals[issue_key]

                            if decision != "APPROVED":
                                print(f"Action REJECTED or TIMED OUT: {decision}")
                                jira.add_comment(issue_key, f"❌ *SRE Agent*: Action rejected by operator (Decision: {decision}).")
                                tool_result = f"Action cancelled. Human operator denied the request. Reason: {decision}."
                                tool_results_content.append({
                                    "type": "tool_result",
                                    "tool_use_id": tool_id,
                                    "content": tool_result
                                })
                                continue

                            # Transition back to In Progress if approved
                            jira.add_comment(issue_key, "✅ *SRE Agent*: Action approved! Resuming execution...")
                            try:
                                jira.transition_issue(issue_key, transition="In Progress")
                            except Exception:
                                pass

                        # --- EXECUTE THE TOOL ---
                        tool_result = ""
                        try:
                            mapping = tool_mappings.get(tool_name)
                            if not mapping:
                                tool_result = f"Error: Tool '{tool_name}' not found."
                            else:
                                if mapping["source"] == "local":
                                    # Execute local tool directly via FastMCP helper
                                    local_res = await mapping["handler"].call_tool(tool_name, tool_input)
                                    tool_result = local_res.content[0].text
                                else:
                                    # Execute remote tool over ClientSession
                                    remote_name = mapping["original_name"]
                                    remote_res = await asyncio.wait_for(mapping["handler"].call_tool(remote_name, tool_input), timeout=30.0)
                                    # Remote result contains a content block
                                    tool_result = remote_res.content[0].text
                        except Exception as e:
                            tool_result = f"Execution Error: {str(e)}"

                        print(f"Tool execution output: {tool_result[:100]}...")
                        tool_results_content.append({
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": tool_result
                        })

                # Append tool result history for LLM
                messages.append({"role": "user", "content": tool_results_content})

        if run_openai_fallback:
            try:
                from openai import OpenAI
                openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

                # Convert tools to OpenAI format
                openai_tools = []
                for tool in all_tools:
                    openai_tools.append({
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool["description"],
                            "parameters": tool["input_schema"]
                        }
                    })

                openai_messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": initial_prompt}
                ]
                print(f"[DEBUG Fallback] openai_messages: {json.dumps(openai_messages)}", flush=True)
                print(f"[DEBUG Fallback] openai_tools count: {len(openai_tools)}", flush=True)
                for ot in openai_tools:
                    if "create_branch" in ot["function"]["name"]:
                        print(f"[DEBUG Fallback] create_branch schema: {json.dumps(ot)}", flush=True)

                for step in range(max_steps):
                    print(f"Step {step+1} (OpenAI Fallback): Calling OpenAI...", flush=True)
                    response = openai_client.chat.completions.create(
                        model="gpt-4o",
                        messages=openai_messages,
                        tools=openai_tools if openai_tools else None
                    )

                    choice = response.choices[0]
                    message = choice.message

                    # Convert OpenAI assistant message for history
                    assistant_msg = {"role": "assistant"}
                    if message.content:
                        assistant_msg["content"] = message.content
                    if message.tool_calls:
                        assistant_msg["tool_calls"] = [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments
                                }
                            }
                            for tc in message.tool_calls
                        ]
                    openai_messages.append(assistant_msg)

                    if choice.finish_reason == "stop" or not message.tool_calls:
                        summary_content = message.content or ""
                        jira.add_comment(issue_key, f"🤖 *SRE Agent Sweep Completed (OpenAI Fallback)*:\n\n{summary_content}")
                        try:
                            jira.transition_issue(issue_key, transition="Done")
                        except Exception:
                            pass
                        print("Agent sweep finished (OpenAI Fallback).")
                        break

                    elif choice.finish_reason == "tool_calls" or message.tool_calls:
                        tool_results = []
                        for tool_call in message.tool_calls:
                            tool_name = tool_call.function.name
                            tool_id = tool_call.id
                            tool_input = {}
                            if tool_call.function.arguments:
                                try:
                                    tool_input = json.loads(tool_call.function.arguments)
                                except Exception:
                                    pass

                            print(f"Agent (OpenAI) calls tool: {tool_name} with parameters: {tool_input}")

                            # --- HITL SECURITY GATE ---
                            base_tool_name = tool_name.split("_")[-1] if "_" in tool_name else tool_name
                            if base_tool_name in DESTRUCTIVE_ACTIONS:
                                jira.add_comment(
                                    issue_key,
                                    f"⚠️ *SRE Agent (OpenAI)* requests approval to run destructive action: `{tool_name}({json_str(tool_input)})`.\n"
                                    "Please transition this Jira issue status to *'Approved'* or *'Rejected'* to proceed."
                                )
                                try:
                                    jira.transition_issue(issue_key, transition="Pending SRE Approval")
                                except Exception:
                                    pass

                                from server import pending_approvals
                                event = asyncio.Event()
                                pending_approvals[issue_key] = {"event": event, "decision": None}

                                print(f"Blocking thread for Jira approval on issue {issue_key}...")
                                try:
                                    await asyncio.wait_for(event.wait(), timeout=300)
                                    decision = pending_approvals[issue_key]["decision"]
                                except asyncio.TimeoutError:
                                    decision = "TIMEOUT"

                                if issue_key in pending_approvals:
                                    del pending_approvals[issue_key]

                                if decision != "APPROVED":
                                    print(f"Action REJECTED or TIMED OUT: {decision}")
                                    jira.add_comment(issue_key, f"❌ *SRE Agent (OpenAI)*: Action rejected by operator (Decision: {decision}).")
                                    tool_results.append({
                                        "role": "tool",
                                        "tool_call_id": tool_id,
                                        "name": tool_name,
                                        "content": f"Action cancelled. Human operator denied the request. Reason: {decision}."
                                    })
                                    continue

                                jira.add_comment(issue_key, "✅ *SRE Agent (OpenAI)*: Action approved! Resuming execution...")
                                try:
                                    jira.transition_issue(issue_key, transition="In Progress")
                                except Exception:
                                    pass

                            # Execute the tool
                            tool_result = ""
                            mapping = tool_mappings.get(tool_name)
                            if not mapping:
                                tool_result = f"Error: Tool '{tool_name}' not found."
                            else:
                                try:
                                    if mapping["source"] == "local":
                                        local_res = await mapping["handler"].call_tool(tool_name, tool_input)
                                        tool_result = local_res.content[0].text
                                    else:
                                        remote_name = mapping["original_name"]
                                        remote_res = await asyncio.wait_for(mapping["handler"].call_tool(remote_name, tool_input), timeout=30.0)
                                        tool_result = remote_res.content[0].text
                                except Exception as e:
                                    tool_result = f"Execution Error: {str(e)}"

                            print(f"Tool execution output: {tool_result[:100]}...")
                            tool_results.append({
                                "role": "tool",
                                "tool_call_id": tool_id,
                                "name": tool_name,
                                "content": tool_result
                            })

                        # Append tool result history for OpenAI
                        for tr in tool_results:
                            openai_messages.append(tr)
            except Exception as openai_err:
                print(f"OpenAI fallback loop failed: {str(openai_err)}. Dropping to simulation fallback...")
                await run_simulated_sweep_loop(
                    issue_key=issue_key,
                    user_command=user_command,
                    jira=jira,
                    local_mcp=local_mcp,
                    sessions=sessions,
                    tool_mappings=tool_mappings,
                    all_tools=all_tools
                )

def json_str(obj):
    import json
    return json.dumps(obj)

async def run_simulated_sweep_loop(
    issue_key: str,
    user_command: str,
    jira,
    local_mcp,
    sessions,
    tool_mappings,
    all_tools
):
    print(f"[Simulated Loop] Starting sweep for issue {issue_key} with command: {user_command}")

    # helper to call tools
    async def call_any_tool(tool_name, tool_input):
        print(f"[Simulated Loop] Executing tool {tool_name} with {tool_input}")
        mapping = tool_mappings.get(tool_name)
        if not mapping:
            return f"Error: Tool '{tool_name}' not found."
        try:
            if mapping["source"] == "local":
                local_res = await mapping["handler"].call_tool(tool_name, tool_input)
                return local_res.content[0].text
            else:
                remote_name = mapping["original_name"]
                remote_res = await asyncio.wait_for(mapping["handler"].call_tool(remote_name, tool_input), timeout=30.0)
                return remote_res.content[0].text
        except Exception as e:
            return f"Execution Error: {str(e)}"

    # helper to run destructive tool with approval gate
    async def run_destructive_tool_with_approval(tool_name, tool_input):
        # 1. Post comment and transition status
        jira.add_comment(
            issue_key,
            f"⚠️ *SRE Agent (Simulated)* requests approval to run destructive action: `{tool_name}({json_str(tool_input)})`.\n"
            "Please transition this Jira issue status to *'Approved'* or *'Rejected'* to proceed."
        )
        try:
            jira.transition_issue(issue_key, transition="Pending SRE Approval")
        except Exception:
            pass

        # 2. Register in pending map
        from server import pending_approvals
        event = asyncio.Event()
        pending_approvals[issue_key] = {"event": event, "decision": None}

        # 3. Wait for approval
        print(f"[Simulated Loop] Blocking thread for Jira approval on issue {issue_key}...")
        try:
            await asyncio.wait_for(event.wait(), timeout=300)
            decision = pending_approvals[issue_key]["decision"]
        except asyncio.TimeoutError:
            decision = "TIMEOUT"

        if issue_key in pending_approvals:
            del pending_approvals[issue_key]

        if decision != "APPROVED":
            print(f"[Simulated Loop] Action REJECTED or TIMED OUT: {decision}")
            jira.add_comment(issue_key, f"❌ *SRE Agent (Simulated)*: Action rejected by operator (Decision: {decision}).")
            return f"Action cancelled. Human operator denied the request. Reason: {decision}."

        jira.add_comment(issue_key, "✅ *SRE Agent (Simulated)*: Action approved! Resuming execution...")
        try:
            jira.transition_issue(issue_key, transition="In Progress")
        except Exception:
            pass

        # Execute the tool
        return await call_any_tool(tool_name, tool_input)

    # A. If there is a user command:
    if user_command:
        cmd_lower = user_command.lower()
        if "restart" in cmd_lower:
            # Determine container target
            container_name = "shariahscreener"
            if "screener" in cmd_lower:
                container_name = "shariahscreener"
            elif "reeftracker" in cmd_lower or "reef" in cmd_lower:
                container_name = "reeftracker_app"
            elif "messenger" in cmd_lower:
                container_name = "e2ee_messenger"

            res = await run_destructive_tool_with_approval("restart_container", {"container_name": container_name})
            jira.add_comment(issue_key, f"🤖 *SRE Agent (Simulated) Restart Result*:\n\n{res}")
            try:
                jira.transition_issue(issue_key, transition="Done")
            except Exception:
                pass
            print("[Simulated Loop] Command execution finished.")
            return

        elif "health" in cmd_lower or "status" in cmd_lower or "check" in cmd_lower:
            # Check container health and logs
            containers_info = await call_any_tool("list_containers", {})
            screener_health = await call_any_tool("check_app_health", {"container_name": "shariahscreener"})
            reeftracker_health = await call_any_tool("check_app_health", {"container_name": "reeftracker_app"})

            report = (
                f"🤖 *SRE Agent (Simulated) Health Report*:\n\n"
                f"*Running Containers*:\n{containers_info}\n\n"
                f"*Screener Health*:\n{screener_health}\n\n"
                f"*ReefTracker Health*:\n{reeftracker_health}"
            )
            jira.add_comment(issue_key, report)
            try:
                jira.transition_issue(issue_key, transition="Done")
            except Exception:
                pass
            print("[Simulated Loop] Command execution finished.")
            return

        elif "watchlist" in cmd_lower or "compliance" in cmd_lower:
            watchlist_res = await call_any_tool("screener_get_screener_watchlist", {})
            # watchlist_res can be a list or string representation of a list
            import ast
            tickers = []
            try:
                if isinstance(watchlist_res, str):
                    tickers = ast.literal_eval(watchlist_res)
                elif isinstance(watchlist_res, list):
                    tickers = watchlist_res
            except Exception:
                tickers = ["AAPL", "TSLA"] # fallback mock

            scan_results = []
            for t in tickers[:3]: # Scan at most 3 tickers
                scan_res = await call_any_tool("screener_run_screener_scan", {"ticker": t})
                scan_results.append(f"*Ticker {t}*:\n{scan_res}")

            report = (
                f"🤖 *SRE Agent (Simulated) Watchlist Audit*:\n\n"
                f"*Watchlist tickers found*: {tickers}\n\n"
                + "\n\n".join(scan_results)
            )
            jira.add_comment(issue_key, report)
            try:
                jira.transition_issue(issue_key, transition="Done")
            except Exception:
                pass
            print("[Simulated Loop] Command execution finished.")
            return

    # B. Default Autonomous Audit Sweep:
    # 1. Check containers
    containers_info = await call_any_tool("list_containers", {})

    # 2. Get stock watchlist
    watchlist_res = await call_any_tool("screener_get_screener_watchlist", {})
    tickers = ["AAPL", "TSLA"] # default mock tickers
    try:
        if isinstance(watchlist_res, str) and watchlist_res.startswith("["):
            import ast
            tickers = ast.literal_eval(watchlist_res)
        elif isinstance(watchlist_res, list):
            tickers = watchlist_res
    except Exception:
        pass

    # 3. Scan first ticker in watchlist
    scan_results = []
    if tickers:
        t = tickers[0]
        scan_res = await call_any_tool("screener_run_screener_scan", {"ticker": t})
        scan_results.append(f"*Watchlist Stock Compliance Scan ({t})*:\n{scan_res}")

    # 4. Check ReefTracker logs
    reef_logs = await call_any_tool("reeftracker_get_aquarium_list", {})

    # 5. Check Messenger keys
    msg_keys = await call_any_tool("messenger_check_encryption_keys", {})

    report = (
        f"🤖 *SRE Agent (Simulated) Autonomous Sweep Completed*:\n\n"
        f"### 📋 System Health Check\n"
        f"```\n{containers_info}\n```\n\n"
        f"### 📈 Shariah Compliance Audit\n"
        + "\n\n".join(scan_results) + "\n\n"
        f"### 🐠 ReefTracker Metrics\n"
        f"```\n{reef_logs[:300]}...\n```\n\n"
        f"### 🔐 Messenger Security Checks\n"
        f"*{msg_keys}*"
    )

    jira.add_comment(issue_key, report)
    try:
        jira.transition_issue(issue_key, transition="Done")
    except Exception:
        pass
    print("[Simulated Loop] Autonomous sweep completed.")
