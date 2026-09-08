"""Closed-loop Phase A orchestration without repository or host mutation."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from .github import GitHubEvidence, GitHubReadClient
from .models import MAX_EVENT_BYTES, FailureEvent, IncidentRecord, parse_failure_webhook
from .security import (
    WebhookAuthenticationError,
    validate_delivery_id,
    verify_github_signature,
)
from .store import IncidentStore


@dataclass(frozen=True)
class IntakeResult:
    accepted: bool
    replayed: bool
    fingerprint: Optional[str] = None
    issue_key: Optional[str] = None
    reason: Optional[str] = None


def _safe_label(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "unknown").split())[:limit]
    text = re.sub(r"@sre-agent\b", "[agent marker redacted]", text, flags=re.IGNORECASE)
    return text.translate(str.maketrans({"`": "'", "*": "'", "_": "-"}))


def render_diagnosis(event: FailureEvent, evidence: GitHubEvidence) -> str:
    failed_jobs = [job for job in evidence.jobs if job.get("conclusion") != "success"]
    failed_steps = []
    for job in failed_jobs:
        failed_steps.extend(
            step for step in job.get("steps", []) if step.get("conclusion") != "success"
        )
    if failed_steps:
        cause = "The first failed Actions step is {!r}.".format(
            _safe_label(failed_steps[0].get("name"))
        )
        confidence = "high"
    elif failed_jobs:
        cause = "The first failed Actions job is {!r}; step-level failure metadata is unavailable.".format(
            _safe_label(failed_jobs[0].get("name"))
        )
        confidence = "medium"
    elif evidence.run.get("output", {}).get("title"):
        cause = "The failed check reports {!r}.".format(
            _safe_label(evidence.run["output"].get("title"))
        )
        confidence = "medium"
    else:
        cause = "GitHub confirms the failure, but its bounded metadata does not identify a failed step."
        confidence = "low"

    log_clue = None
    for line in reversed(evidence.logs.splitlines()):
        if re.search(r"\b(error|failed|failure|exception|traceback)\b", line, re.IGNORECASE):
            log_clue = _safe_label(line, 240)
            break

    links = [
        event.evidence_url,
        f"https://github.com/{event.repository}/commit/{event.commit_sha}",
    ]
    if evidence.pull_requests:
        pull_number = evidence.pull_requests[0].get("number")
        if isinstance(pull_number, int) and pull_number > 0:
            links.append(f"https://github.com/{event.repository}/pull/{pull_number}")
    evidence_lines = "\n".join(f"- {link}" for link in links)
    return (
        "🤖 *SRE Agent GitHub diagnosis (read-only)*\n\n"
        f"*Symptom:* `{_safe_label(event.workflow)}` concluded `{event.conclusion}` on "
        f"`{event.commit_sha[:12]}`.\n"
        f"*Likely cause:* {cause}\n"
        + (f"*Bounded log clue (untrusted):* `{log_clue}`\n" if log_clue else "")
        +
        "*Impact:* The affected CI signal is not green; no deployment or repository change was attempted.\n"
        "*Recommended next action:* Review the failed job/step at the run link, reproduce it on the "
        "same commit, and request a separately governed proposal only after the cause is confirmed.\n"
        f"*Confidence:* {confidence}.\n"
        "*Uncertainty:* Logs, commit text, PR text, and check output are treated as untrusted data; "
        "this diagnosis uses only bounded structured evidence.\n"
        f"*Evidence:*\n{evidence_lines}\n\n"
        f"Audit fingerprint: `{event.fingerprint}`"
    )


class GitHubJiraWorkflow:
    def __init__(
        self,
        store: IncidentStore,
        github: GitHubReadClient,
        jira: Any,
        webhook_secret: str,
        allowed_repositories: Iterable[str],
    ):
        self.store = store
        self.github = github
        self.jira = jira
        if not webhook_secret:
            raise ValueError("GitHub webhook secret is required")
        self.webhook_secret = webhook_secret
        self.allowed_repositories = frozenset(allowed_repositories)
        self._issue_lock = threading.RLock()

    def _ensure_issue(self, incident: IncidentRecord) -> str:
        if incident.issue_key:
            return incident.issue_key
        event = incident.as_event()
        with self._issue_lock:
            refreshed = self.store.get_by_fingerprint(event.fingerprint)
            if refreshed and refreshed.issue_key:
                return refreshed.issue_key
            issue_key = self.jira.find_issue(event)
            if not issue_key:
                issue_key = self.jira.create_incident(event)
            self.store.attach_issue(event.fingerprint, issue_key)
            return issue_key

    def intake_event(self, event: FailureEvent, delivery_id: str) -> IntakeResult:
        incident, replayed = self.store.record_event(event, delivery_id)
        issue_key = self._ensure_issue(incident)
        return IntakeResult(True, replayed, event.fingerprint, issue_key)

    def handle_webhook(self, body: bytes, headers: Mapping[str, str]) -> IntakeResult:
        if len(body) > MAX_EVENT_BYTES:
            raise ValueError("GitHub webhook body exceeds policy limit")
        normalized_headers = {str(key).casefold(): value for key, value in headers.items()}
        verify_github_signature(
            body, normalized_headers.get("x-hub-signature-256"), self.webhook_secret
        )
        delivery_id = validate_delivery_id(normalized_headers.get("x-github-delivery"))
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError("GitHub webhook body is invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("GitHub webhook body must be an object")
        event = parse_failure_webhook(
            normalized_headers.get("x-github-event", ""), payload, self.allowed_repositories
        )
        if event is None:
            return IntakeResult(False, False, reason="event is not an allowlisted completed failure")
        return self.intake_event(event, delivery_id)

    def poll_repository(self, repository: str, lookback_minutes: int = 15) -> tuple[IntakeResult, ...]:
        if repository not in self.allowed_repositories:
            raise ValueError("repository is not allowlisted")
        lookback_minutes = max(1, min(int(lookback_minutes), 1_440))
        since = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
        return tuple(
            self.intake_event(event, f"poll:{event.repository}:{event.run_id}")
            for event in self.github.list_recent_failures(repository, since)
        )

    def has_incident(self, issue_key: str) -> bool:
        return self.store.get_by_issue(issue_key) is not None

    def handle_command(self, issue_key: str) -> str:
        """Called only after the scheduler recognizes a human agent mention."""
        incident = self.store.get_by_issue(issue_key)
        if incident is None:
            raise KeyError("Jira issue is not a GitHub incident")
        if "aegis:github-proposal" not in self.jira.proposal_labels(issue_key):
            return self.investigate(issue_key)
        message = (
            "Aegis change proposal requested; no GitHub write is authorized.\n"
            f"Incident fingerprint: {incident.fingerprint}\n"
            f"Repository: {incident.repository}\n"
            "An operator must prepare an exact file snapshot using the Phase B command, "
            "review its diff and binding hash, then obtain separate durable approval. "
            "Jira comments and labels cannot approve, execute, merge, or deploy. "
            "See docs/GITHUB_JIRA_PHASE_B.md."
        )
        if self.store.claim_proposal_request(issue_key):
            self.jira.add_proposal_update(issue_key, message, "aegis:approval-required")
        return message

    def investigate(self, issue_key: str) -> str:
        incident = self.store.get_by_issue(issue_key)
        if incident is None:
            raise KeyError("Jira issue is not a GitHub incident")
        event = incident.as_event()
        evidence = self.github.fetch_evidence(event)
        diagnosis = render_diagnosis(event, evidence)
        stable = json.dumps(evidence.stable_payload(), sort_keys=True, separators=(",", ":"))
        evidence_hash = hashlib.sha256(stable.encode()).hexdigest()
        self.jira.add_diagnosis(issue_key, diagnosis)
        self.store.mark_diagnosed(event.fingerprint, evidence_hash)
        return diagnosis
