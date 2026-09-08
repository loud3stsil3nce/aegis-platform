from apscheduler.schedulers.asyncio import AsyncIOScheduler
from src.agent.orchestrator import execute_agent_sweep, jira
from src.github_jira.dispatch import dispatch_github_incident
from src.github_jira.runtime import get_workflow, poll_ci_feedback
import os
import json
import re
import asyncio

scheduler = AsyncIOScheduler()

PROCESSED_COMMENTS_FILE = "/app/processed_comments.json"
PROCESSED_ISSUES_FILE = "/app/processed_issue_triggers.json"
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "KAN")


def _load_processed(path):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return set(json.load(f))
    except Exception as e:
        print(f"[Polling] Error loading processed state {path}: {e}", flush=True)
    return set()


def _save_processed(path, processed):
    try:
        with open(path, "w") as f:
            json.dump(list(processed), f)
    except Exception as e:
        print(f"[Polling] Error saving processed state {path}: {e}", flush=True)

def load_processed_comments():
    return _load_processed(PROCESSED_COMMENTS_FILE)

def save_processed_comments(processed_set):
    _save_processed(PROCESSED_COMMENTS_FILE, processed_set)


def _jira_text(value):
    """Return plain text from Jira's string or Atlassian Document Format value."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        return "".join(_jira_text(child) for child in value.get("content", []))
    if isinstance(value, list):
        return "".join(_jira_text(child) for child in value)
    return ""


def _extract_marker_command(text):
    match = re.search(r"@sre-agent\b", text or "", flags=re.IGNORECASE)
    if not match:
        return None
    return re.sub(r"@sre-agent\b", "", text[match.start():], count=1, flags=re.IGNORECASE).strip()

async def run_agent_command_safe(issue_key: str, user_command: str):
    try:
        print(f"[Polling] Launching sweep for issue {issue_key} with command: '{user_command}'", flush=True)
        if await dispatch_github_incident(issue_key):
            return
        await execute_agent_sweep(issue_key=issue_key, user_command=user_command)
    except Exception as e:
        print(f"[Polling] Error executing sweep for {issue_key}: {e}", flush=True)

async def poll_jira_comments_job():
    if not jira:
        return
    
    processed = load_processed_comments()
    processed_issues = _load_processed(PROCESSED_ISSUES_FILE)
    
    try:
        # Search for recent issues modified in project KAN
        # We wrap in asyncio.to_thread because the jira client library makes synchronous HTTP calls
        issues = await asyncio.to_thread(
            jira.search_issues,
            f"project = {JIRA_PROJECT_KEY} order by updated desc",
            maxResults=10,
        )
        
        updated_any = False
        for issue in issues:
            # 1. Skip issues that are already done or closed
            try:
                status = issue.fields.status.name.upper()
            except AttributeError:
                status = "UNKNOWN"
                
            if status in ["DONE", "RESOLVED", "CLOSED"]:
                continue

            # A marker in the issue title or description is a one-time trigger.
            # Jira Cloud descriptions may be plain text or Atlassian Document Format.
            issue_trigger_id = f"issue:{issue.key}"
            issue_text = "\n".join(
                _jira_text(getattr(issue.fields, field, ""))
                for field in ("summary", "description")
            )
            if issue_trigger_id not in processed_issues:
                issue_command = _extract_marker_command(issue_text)
                if issue_command is not None:
                    print(f"[Polling] Found issue trigger on {issue.key}", flush=True)
                    processed_issues.add(issue_trigger_id)
                    updated_any = True
                    asyncio.create_task(run_agent_command_safe(issue.key, issue_command))
                
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
            _save_processed(PROCESSED_ISSUES_FILE, processed_issues)
            
    except Exception as e:
        print(f"[Polling] Error during polling: {e}", flush=True)


async def poll_github_failures_job():
    if os.getenv("AEGIS_GITHUB_POLL_ENABLED", "0").casefold() not in {"1", "true", "yes"}:
        return
    workflow = get_workflow()
    if workflow is None:
        return
    raw_repos = os.getenv("AEGIS_GITHUB_ALLOWED_REPOSITORIES", "")
    repos = [r.strip() for r in raw_repos.split(",") if r.strip()]
    lookback = int(os.getenv("AEGIS_GITHUB_POLL_LOOKBACK_MINUTES", "15"))
    for repository in repos:
        try:
            await asyncio.to_thread(workflow.poll_repository, repository, lookback)
        except Exception as e:
            print(f"[GitHub Polling] Recovery poll failed for {repository}: {type(e).__name__}", flush=True)

async def poll_github_ci_job():
    try:
        await asyncio.to_thread(poll_ci_feedback)
    except Exception as exc:
        print(f"[GitHub CI] Feedback blocked: {type(exc).__name__}", flush=True)


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

    scheduler.add_job(
        poll_github_failures_job,
        "interval",
        seconds=max(30, int(os.getenv("AEGIS_GITHUB_POLL_INTERVAL_SECONDS", "60"))),
        id="github_failure_poll_job",
    )
    
    scheduler.add_job(
        poll_github_ci_job,
        "interval",
        seconds=max(30, int(os.getenv("AEGIS_GITHUB_CI_INTERVAL_SECONDS", "60"))),
        id="github_ci_feedback_job",
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
