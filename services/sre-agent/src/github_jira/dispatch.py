"""Route Jira commands for GitHub incidents before the general SRE sweep."""

from __future__ import annotations

import asyncio

from .runtime import get_workflow


async def dispatch_github_incident(issue_key: str, user_command: str = "") -> bool:
    workflow = get_workflow()
    if workflow is None or not workflow.has_incident(issue_key):
        return False
    await asyncio.to_thread(workflow.handle_command, issue_key, user_command)
    return True
