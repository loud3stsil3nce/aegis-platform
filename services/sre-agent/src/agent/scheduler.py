from apscheduler.schedulers.asyncio import AsyncIOScheduler
from src.agent.orchestrator import execute_agent_sweep, jira
import os
import json
import re
import asyncio

scheduler = AsyncIOScheduler()

PROCESSED_COMMENTS_FILE = "/app/processed_comments.json"

def load_processed_comments():
    try:
        if os.path.exists(PROCESSED_COMMENTS_FILE):
            with open(PROCESSED_COMMENTS_FILE, 'r') as f:
                return set(json.load(f))
    except Exception as e:
        print(f"[Polling] Error loading processed comments: {e}", flush=True)
    return set()

def save_processed_comments(processed_set):
    try:
        with open(PROCESSED_COMMENTS_FILE, 'w') as f:
            json.dump(list(processed_set), f)
    except Exception as e:
        print(f"[Polling] Error saving processed comments: {e}", flush=True)

async def run_agent_command_safe(issue_key: str, user_command: str):
    try:
        print(f"[Polling] Launching sweep for issue {issue_key} with command: '{user_command}'", flush=True)
        await execute_agent_sweep(issue_key=issue_key, user_command=user_command)
    except Exception as e:
        print(f"[Polling] Error executing sweep for {issue_key}: {e}", flush=True)

async def poll_jira_comments_job():
    if not jira:
        return
    
    processed = load_processed_comments()
    
    try:
        # Search for recent issues modified in project KAN
        # We wrap in asyncio.to_thread because the jira client library makes synchronous HTTP calls
        issues = await asyncio.to_thread(jira.search_issues, "project=KAN order by updated desc", maxResults=10)
        
        updated_any = False
        for issue in issues:
            # 1. Skip issues that are already done or closed
            try:
                status = issue.fields.status.name.upper()
            except AttributeError:
                status = "UNKNOWN"
                
            if status in ["DONE", "RESOLVED", "CLOSED"]:
                continue
                
            # 2. Retrieve comments
            comments = await asyncio.to_thread(jira.comments, issue.key)
            for comment in comments:
                comment_id = str(comment.id)
                if comment_id in processed:
                    continue
                
                body = comment.body
                if body and "@sre-agent" in body.lower():
                    print(f"[Polling] Found new command in comment {comment_id} on issue {issue.key}: '{body}'", flush=True)
                    
                    # Mark it as processed before running to avoid double triggers
                    processed.add(comment_id)
                    updated_any = True
                    
                    # Parse command case-insensitively
                    user_command = re.sub(r"@sre-agent", "", body, flags=re.IGNORECASE).strip()
                    
                    # Run command in the background
                    asyncio.create_task(run_agent_command_safe(issue.key, user_command))
                    
        if updated_any:
            save_processed_comments(processed)
            
    except Exception as e:
        print(f"[Polling] Error during polling: {e}", flush=True)

def start_scheduler():
    interval_minutes = int(os.getenv("CRON_INTERVAL_MINUTES", "30"))
    print(f"Scheduling SRE Agent sweep job to run every {interval_minutes} minutes...", flush=True)
    scheduler.add_job(
        execute_agent_sweep,
        "interval",
        minutes=interval_minutes,
        id="sre_agent_sweep_job"
    )
    
    # Add comment polling job as a fallback to run every 10 seconds
    print("Scheduling Jira comment polling fallback job to run every 10 seconds...", flush=True)
    scheduler.add_job(
        poll_jira_comments_job,
        "interval",
        seconds=10,
        id="jira_comments_poll_job"
    )
    
    scheduler.start()