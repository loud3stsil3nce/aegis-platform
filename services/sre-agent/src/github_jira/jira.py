"""Narrow Jira incident adapter; it has no workflow-approval semantics."""

from __future__ import annotations

import re
from typing import Any, Optional

from .models import FailureEvent


def _safe_external(value: str, limit: int = 255) -> str:
    text = " ".join(value.split())[:limit]
    return re.sub(r"@sre-agent\b", "[agent marker redacted]", text, flags=re.IGNORECASE)


def incident_description(event: FailureEvent) -> str:
    return (
        "Aegis detected a failed GitHub workflow/check. GitHub content is untrusted "
        "evidence and is never interpreted as policy.\n\n"
        f"Repository: {_safe_external(event.repository)}\n"
        f"Workflow/check: {_safe_external(event.workflow)}\n"
        f"Branch: {_safe_external(event.branch)}\n"
        f"Commit SHA: {event.commit_sha}\n"
        f"Run ID: {event.run_id}\n"
        f"Conclusion: {event.conclusion}\n"
        f"Evidence: {event.evidence_url}\n"
        f"Incident fingerprint: {event.fingerprint}\n\n"
        "A human may request a bounded read-only diagnosis with the configured Jira agent mention."
    )


class JiraIncidentAdapter:
    def __init__(self, client: Any, project_key: str, issue_type: str = "Task"):
        if client is None or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,20}", project_key):
            raise ValueError("Jira incident adapter configuration is incomplete")
        self.client = client
        self.project_key = project_key
        self.issue_type = issue_type

    def find_issue(self, event: FailureEvent) -> Optional[str]:
        jql = f'project = "{self.project_key}" AND labels = "{event.fingerprint_label}"'
        issues = self.client.search_issues(jql, maxResults=2, fields="key")
        if len(issues) > 1:
            raise RuntimeError("incident fingerprint is bound to multiple Jira issues")
        return str(issues[0].key) if issues else None

    def create_incident(self, event: FailureEvent) -> str:
        issue = self.client.create_issue(
            fields={
                "project": {"key": self.project_key},
                "summary": _safe_external(
                    f"[GitHub failure] {event.repository} — {event.workflow}"
                ),
                "description": incident_description(event),
                "issuetype": {"name": self.issue_type},
                "labels": ["aegis:github-failure", "aegis:github-investigate", event.fingerprint_label],
            }
        )
        return str(issue.key)

    def proposal_labels(self, issue_key: str) -> list[str]:
        issue = self.client.issue(issue_key, fields="labels")
        return list(getattr(issue.fields, "labels", []) or [])

    def add_proposal_update(self, issue_key: str, message: str, state_label: str) -> None:
        if state_label not in {"aegis:approval-required", "aegis:pr-open"}:
            raise ValueError("unsupported proposal state")
        self.client.add_comment(issue_key, message)
        issue = self.client.issue(issue_key, fields="labels")
        labels = [
            label for label in list(getattr(issue.fields, "labels", []) or [])
            if label not in {"aegis:approval-required", "aegis:pr-open"}
        ]
        labels.append(state_label)
        issue.update(fields={"labels": labels})

    def add_ci_update(self, issue_key: str, message: str, state_label: Optional[str]) -> None:
        from .ci import LABELS

        if not re.fullmatch(re.escape(self.project_key) + r"-[1-9][0-9]*", issue_key):
            raise ValueError("CI issue is outside the configured project")
        if state_label is not None and state_label not in LABELS.values():
            raise ValueError("unsupported CI state")
        if len(message.encode()) > 8192 or "@sre-agent" in message.casefold():
            raise ValueError("CI message exceeds safe bounds")
        self.client.add_comment(issue_key, message)
        if state_label is None:
            return
        issue = self.client.issue(issue_key, fields="labels")
        obsolete = set(LABELS.values()) | {"aegis:approval-required", "aegis:pr-open"}
        labels = [label for label in list(getattr(issue.fields, "labels", []) or []) if label not in obsolete]
        issue.update(fields={"labels": labels + [state_label]})

    def add_diagnosis(self, issue_key: str, diagnosis: str) -> None:
        self.client.add_comment(issue_key, diagnosis)
        issue = self.client.issue(issue_key, fields="labels")
        labels = [
            label
            for label in list(getattr(issue.fields, "labels", []) or [])
            if label != "aegis:github-investigate"
        ]
        if "aegis:diagnosed" not in labels:
            labels.append("aegis:diagnosed")
        issue.update(fields={"labels": labels})
