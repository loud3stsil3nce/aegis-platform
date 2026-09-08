from fastapi import FastAPI, Request, HTTPException, BackgroundTasks                                                                                                                                                                                                                            
from fastapi.responses import HTMLResponse                                                                                                                                                                                                                                                      
try:
    from fastmcp import FastMCP
except ImportError:
    from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport                                                                                                                                                                                                                                                   
from src.health import register_health_tools                                                                                                                                                                                                                                                    
from src.observability_tools import register_observability_tools
from src.auth import (
    enforce_body_limit,
    read_bounded_body,
    require_jira_webhook_auth,
    require_mcp_auth,
)
from src.github_jira.dispatch import dispatch_github_incident
from src.github_jira.models import EventValidationError
from src.github_jira.runtime import configuration_error, get_workflow
from src.github_jira.security import WebhookAuthenticationError
from src.db.database import engine                                                                                                                                                                                                                                                              
from src.db.models import Base                                                                                                                                                                                                                                                                  
import uvicorn                                                                                                                                                                                                                                                                                  
import asyncio                                                                                                                                                                                                                                                                                  
import os                                                                                                                                                                                                                                                                                       
import json                                                                                                                                                                                                                                                                                     
                                                                                                                                                                                                                                                                                                
from src.agent.orchestrator import execute_agent_sweep                                                                                                                                                                                                                                          
from src.agent.scheduler import start_scheduler                                                                                                                                                                                                                                                 
                                                                                                                                                                                                                                                                                                
app = FastAPI(title="SRE Agent Control Center (Jira Mode)")                                                                                                                                                                                                                                     


def _jira_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        return "".join(_jira_text(child) for child in value.get("content", []))
    if isinstance(value, list):
        return "".join(_jira_text(child) for child in value)
    return ""
                                                                                                                                                                                                                                                                                                
# 1. Initialize and register SRE FastMCP tools                                                                                                                                                                                                                                                  
mcp = FastMCP("SRE Bug Hunter")                                                                                                                                                                                                                                                                 
register_health_tools(mcp)                                                                                                                                                                                                                                                                      
register_observability_tools(mcp)
                                                                                                                                                                                                                                                                                                
# 2. Database initialization helper                                                                                                                                                                                                                                                             
async def init_db():                                                                                                                                                                                                                                                                            
    async with engine.begin() as conn:                                                                                                                                                                                                                                                          
        await conn.run_sync(Base.metadata.create_all)                                                                                                                                                                                                                                           
                                                                                                                                                                                                                                                                                                
# 3. Startup lifecycle: run init_db and start_scheduler                                                                                                                                                                                                                                         
@app.on_event("startup")                                                                                                                                                                                                                                                                        
async def startup_event():                                                                                                                                                                                                                                                                      
    await init_db()                                                                                                                                                                                                                                                                             
    workflow = get_workflow()
    if workflow is None:
        print(f"GitHub-Jira Phase A disabled: {configuration_error()}", flush=True)
    else:
        print("GitHub-Jira Phase A read-only workflow enabled.", flush=True)
    start_scheduler()                                                                                                                                                                                                                                                                           
                                                                                                                                                                                                                                                                                                
# 4. Mount SRE local MCP SSE transport endpoints                                                                                                                                                                                                                                                
mcp_transport = SseServerTransport("/mcp/messages/")                                                                                                                                                                                                                                            
                                                                                                                                                                                                                                                                                                
@app.get("/mcp/sse")                                                                                                                                                                                                                                                                            
async def handle_mcp_sse(request: Request):                                                                                                                                                                                                                                                     
    require_mcp_auth(request)
    async with mcp_transport.connect_sse(                                                                                                                                                                                                                                                       
        request.scope, request.receive, request._send                                                                                                                                                                                                                                           
    ) as (in_stream, out_stream):                                                                                                                                                                                                                                                               
        await mcp._mcp_server.run(                                                                                                                                                                                                                                                              
            in_stream,                                                                                                                                                                                                                                                                          
            out_stream,                                                                                                                                                                                                                                                                         
            mcp._mcp_server.create_initialization_options()                                                                                                                                                                                                                                     
        )                                                                                                                                                                                                                                                                                       
                                                                                                                                                                                                                                                                                                
@app.post("/mcp/messages/")                                                                                                                                                                                                                                                                     
async def handle_mcp_messages(request: Request):                                                                                                                                                                                                                                                
    require_mcp_auth(request)
    enforce_body_limit(request)
    return await mcp_transport.handle_post_message(                                                                                                                                                                                                                                             
        request.scope, request.receive, request._send                                                                                                                                                                                                                                           
    )                                                                                                                                                                                                                                                                                           
                                                                                                                                                                                                                                                                                                
# --- 5. Authenticated Jira Webhook Listener ---
                                                                                                                                                                                                                                                                                                
async def run_agent_command(issue_key: str, comment_body: str):
    # Clean the @SRE-Agent tag out case-insensitively
    import re
    user_command = re.sub(r"@sre-agent", "", comment_body, flags=re.IGNORECASE).strip()
    # Run the agent sweep with the specific user command context
    try:
        if await dispatch_github_incident(issue_key):
            return
        await execute_agent_sweep(issue_key=issue_key, user_command=user_command)
    except Exception as e:
        print(f"Error running background command for issue {issue_key}: {str(e)}")                                                                                                                                                                                                              
                                                                                                                                                                                                                                                                                                
@app.post("/api/jira/webhook")                                                                                                                                                                                                                                                                  
async def handle_jira_webhook(request: Request, background_tasks: BackgroundTasks):                                                                                                                                                                                                             
    """                                                                                                                                                                                                                                                                                         
    Receives events from Jira Cloud (issue created, updated, commented, transitioned).                                                                                                                                                                                                          
    """                                                                                                                                                                                                                                                                                         
    require_jira_webhook_auth(request)
    try:                                                                                                                                                                                                                                                                                        
        body = json.loads(await read_bounded_body(request))
    except HTTPException:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON body.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object.")
                                                                                                                                                                                                                                                                                                
    issue = body.get("issue", {})                                                                                                                                                                                                                                                               
    issue_key = issue.get("key")                                                                                                                                                                                                                                                                
    if not issue_key:                                                                                                                                                                                                                                                                           
        return {"status": "ignored", "reason": "No issue key found in payload."}                                                                                                                                                                                                                
                                                                                                                                                                                                                                                                                                
    event_type = body.get("webhookEvent")                                                                                                                                                                                                                                                       
    print(f"Received Jira webhook event: '{event_type}' for issue {issue_key}")                                                                                                                                                                                                                 
                                                                                                                                                                                                                                                                                                
    # Jira workflow transitions are not authorization. Durable exact-action
    # approvals will be handled by the policy service introduced in Phase 2.
    if event_type == "comment_created":
        comment_body = _jira_text(body.get("comment", {}).get("body", ""))
        if "@sre-agent" in comment_body.lower():
            print(f"Triggering background command for issue {issue_key}")
            background_tasks.add_task(run_agent_command, issue_key, comment_body)
                                                                                                                                                                                                                                                                                                
    return {"status": "processed"}


@app.post("/api/github/webhook")
async def handle_github_webhook(request: Request):
    """Accept signed failure facts; this route exposes no GitHub mutation."""

    workflow = get_workflow()
    if workflow is None:
        raise HTTPException(status_code=503, detail="GitHub-Jira workflow is not configured")
    body = await read_bounded_body(request)
    try:
        result = await asyncio.to_thread(workflow.handle_webhook, body, dict(request.headers))
    except WebhookAuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except EventValidationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": "accepted" if result.accepted else "ignored",
        "replayed": result.replayed,
        "fingerprint": result.fingerprint,
        "issue_key": result.issue_key,
        "reason": result.reason,
    }

# Simple Status landing page
@app.get("/", response_class=HTMLResponse)
async def get_root():
    return """
    <html>
        <head><title>SRE Agent</title></head>
        <body style="font-family: sans-serif; text-align: center; padding-top: 10%; background: #0f172a; color: #f1f5f9;">
            <h1>🛡️ SRE Agent Jira Webhook Listener Online</h1>
            <p style="color: #94a3b8;">Port: 8002 | Running in Jira-Native Orchestration Mode</p>
            <div style="margin-top: 20px; padding: 15px; border-radius: 8px; border: 1px solid #334155; display: inline-block; background: #1e293b;">
                <strong>Local MCP Endpoint:</strong> <code style="color: #22d3ee;">http://sre-agent:8002/mcp/sse</code>
            </div>
        </body>
    </html>
    """

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8002, reload=False)
