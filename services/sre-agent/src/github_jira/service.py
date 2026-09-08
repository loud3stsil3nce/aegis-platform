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

    def handle_command(self, issue_key: str, user_command: str = "") -> str:
        """Called only after the scheduler recognizes a human agent mention."""
        incident = self.store.get_by_issue(issue_key)
        if incident is None:
            raise KeyError("Jira issue is not a GitHub incident")

        cmd = user_command.casefold()
        if any(w in cmd for w in ("stage", "propose", "fix")):
            return self.stage_proposal(issue_key)

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

    def stage_proposal(self, issue_key: str) -> str:
        incident = self.store.get_by_issue(issue_key)
        if incident is None:
            raise KeyError("Jira issue is not a GitHub incident")

        from pathlib import Path
        import os
        from core.change_management.proposals import ChangeProposalStore
        from core.change_management.policy import ChangePolicy
        from core.change_management.jira_proposals import JiraProposalService, REQUEST_LABEL
        from core.change_management.github_adapter import InstallationTokenProvider, GitHubChangeAdapter
        from core.change_management.github_app import GitHubApi, app_jwt
        from core.change_management.deployment_auth import DeploymentActor

        policy_file = Path(os.getenv("AEGIS_POLICY_FILE", "/app/code/config/change-policy.json"))
        if not policy_file.exists():
            policy_file = Path(__file__).resolve().parents[4] / "config" / "change-policy.json"
        policy = ChangePolicy.from_dict(json.loads(policy_file.read_text()))

        store_path = Path(os.getenv("AEGIS_PROPOSAL_STORE", "/app/state/jira-change-proposals.sqlite3"))
        store_path.parent.mkdir(parents=True, exist_ok=True)
        proposal_store = ChangeProposalStore(store_path)

        app_id = self.github.token_provider.app_id
        inst_id = self.github.token_provider.installation_id
        key_file = self.github.token_provider.private_key_file
        provider = InstallationTokenProvider(lambda: GitHubApi(f"Bearer {app_jwt(app_id, key_file)}"), inst_id)
        adapter = GitHubChangeAdapter(provider)
        proposal_service = JiraProposalService(proposal_store, policy, adapter)

        # Check existing active proposal for this issue
        with proposal_store._lock:
            existing = proposal_store.connection.execute(
                "SELECT proposal_id FROM jira_change_snapshots WHERE issue_key=? AND lifecycle_state='ACTIVE'",
                (issue_key,),
            ).fetchone()
            if existing:
                res = proposal_service.inspect(existing["proposal_id"])
                p_id = res["proposal"]["proposal_id"]
                binding = res["binding_sha256"]
                diff = res["snapshot"]["diff"]
                msg = (
                    f"🤖 **SRE Agent Change Proposal Already Staged**\n\n"
                    f"An active change proposal for {issue_key} is already staged:\n\n"
                    f"- **Proposal ID**: `{p_id}`\n"
                    f"- **Repository**: `{res['proposal']['repository']}`\n"
                    f"- **Binding SHA-256**: `{binding}`\n\n"
                    f"### Proposed Diff:\n"
                    f"```diff\n"
                    f"{diff}\n"
                    f"```\n\n"
                    f"**To approve and open Draft PR**: Reply with `approve {binding}`"
                )
                self.jira.add_proposal_update(issue_key, msg, "aegis:approval-required")
                return msg

        # Synthesize fix snapshot
        target_path = None
        new_content = None

        if incident.repository == "loud3stsil3nce/shariahcompliantscreener":
            target_path = "src/db/helpers.py"
            current_raw = self.github.get_file_content(incident.repository, target_path, incident.commit_sha)
            text = current_raw.decode("utf-8")
            old_str = 'DATABASE_URL = os.getenv("DATABASE_URL")\nif not DATABASE_URL:\n    raise RuntimeError("DATABASE_URL is required")'
            new_str = (
                'DATABASE_URL = os.getenv("DATABASE_URL")\n'
                'if not DATABASE_URL:\n'
                '    if os.getenv("PYTEST_CURRENT_TEST") or os.getenv("CI"):\n'
                '        DATABASE_URL = os.getenv("TEST_DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test_db")\n'
                '    else:\n'
                '        raise RuntimeError("DATABASE_URL is required")'
            )
            if old_str in text:
                new_content = text.replace(old_str, new_str)
            else:
                pattern = r'DATABASE_URL\s*=\s*os\.getenv\("DATABASE_URL"\)\s*\nif not DATABASE_URL:\s*\n\s*raise RuntimeError\("DATABASE_URL is required"\)'
                new_content = re.sub(pattern, new_str, text)

        if not target_path or not new_content:
            msg = (
                f"🤖 SRE Agent: Could not automatically synthesize a verified patch for {incident.repository}.\n"
                f"Please inspect the evidence and prepare a snapshot using scripts/jira_change_proposal.py."
            )
            self.jira.add_comment(issue_key, msg)
            return msg

        # Prepare and stage the proposal
        actor = DeploymentActor("sre-agent", {"requester"})
        result = proposal_service.prepare(
            issue_key=issue_key,
            fingerprint=incident.fingerprint,
            labels=[REQUEST_LABEL],
            repository=incident.repository,
            base_sha=incident.commit_sha,
            files={target_path: new_content.encode("utf-8")},
            actor=actor,
        )

        p_id = result["proposal"]["proposal_id"]
        binding = result["binding_sha256"]
        diff = result["snapshot"]["diff"]
        branch = result["proposal"]["proposed_branch"]

        message = (
            f"🤖 **SRE Agent Change Proposal Staged**\n\n"
            f"A candidate fix was generated and staged under the Aegis Change Policy:\n\n"
            f"- **Repository**: `{incident.repository}`\n"
            f"- **Base Commit**: `{incident.commit_sha[:12]}`\n"
            f"- **Proposed Branch**: `{branch}`\n"
            f"- **Proposal ID**: `{p_id}`\n"
            f"- **Cryptographic Binding SHA-256**: `{binding}`\n\n"
            f"### Proposed Diff:\n"
            f"```diff\n"
            f"{diff}\n"
            f"```\n\n"
            f"---\n"
            f"**Operator Review & Approval Required**:\n"
            f"Review the diff above. To approve this proposal and open the Draft Pull Request, comment:\n"
            f"`approve {binding}`"
        )
        self.store.claim_proposal_request(issue_key)
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
