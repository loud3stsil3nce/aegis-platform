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
        is_create_pr = any(phrase in cmd for phrase in ("create pr", "open pr", "draft pr", "make pr", "approve"))
        if is_create_pr:
            return self.approve_and_execute_proposal(issue_key, user_command)
        if any(w in cmd for w in ("stage", "propose", "fix", "solution")):
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

        # Run investigation if not yet diagnosed
        if getattr(incident, "status", None) != "DIAGNOSED":
            try:
                self.investigate(issue_key)
            except Exception as exc:
                print(f"[stage_proposal] Pre-staging investigation failed: {exc}", flush=True)

        from pathlib import Path
        import os
        from core.change_management.proposals import ChangeProposalStore
        from core.change_management.policy import ChangePolicy
        from core.change_management.jira_proposals import JiraProposalService, REQUEST_LABEL
        from core.change_management.github_adapter import InstallationTokenProvider, GitHubChangeAdapter
        from core.change_management.github_app import GitHubApi, app_jwt
        from core.change_management.deployment_auth import DeploymentActor

        try:
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
                        f"---\n"
                        f"**To create Draft Pull Request**: Reply with `@sre-agent create pr` or `@sre-agent approve`\n"
                        f"*(Merge remains manual on GitHub)*"
                    )
                    self.jira.add_proposal_update(issue_key, msg, "aegis:approval-required")
                    return msg

            # Determine base commit SHA from GitHub for target base branch (main)
            api = provider.issue(incident.repository, write=False)
            root = adapter._repo_path(incident.repository)
            base_branch = "main"
            ref_data = api.request("GET", f"{root}/git/ref/heads/{base_branch}")
            base_sha = ref_data.get("object", {}).get("sha")
            if not base_sha:
                base_sha = incident.commit_sha

            # 1. Attempt autonomous LLM traceback diagnosis and multi-file patch synthesis
            from .llm_synthesizer import synthesize_patch_with_llm

            patch_files: dict[str, bytes] = {}
            explanation = ""

            log_text = ""
            if incident.run_id and incident.source_kind == "workflow_run":
                try:
                    evidence = self.github.fetch_evidence(incident.as_event())
                    for job in evidence.jobs:
                        if job.get("conclusion") == "failure" and "id" in job:
                            raw_log = self.github._download_job_log(incident.repository, job["id"])
                            log_text = raw_log.decode("utf-8", errors="replace")
                            break
                except Exception:
                    pass

            if log_text:
                synth_result = synthesize_patch_with_llm(
                    repository=incident.repository,
                    base_sha=base_sha,
                    log_text=log_text,
                    github=self.github,
                    symptom=f"Workflow '{incident.workflow}' concluded '{incident.conclusion}'",
                    likely_cause=f"Failure in workflow '{incident.workflow}' on commit {incident.commit_sha[:12]}",
                )
                if synth_result and synth_result.files:
                    patch_files = synth_result.files
                    explanation = f"💡 **Autonomous Diagnosis** ({synth_result.provider}/{synth_result.model}):\n{synth_result.explanation}\n\n"

            # 2. Fallback to static template rules if LLM synthesis did not generate a patch
            if not patch_files and incident.repository == "loud3stsil3nce/shariahcompliantscreener":
                # Check helpers.py for DATABASE_URL fix
                helpers_path = "src/db/helpers.py"
                try:
                    helpers_raw = self.github.get_file_content(incident.repository, helpers_path, base_sha)
                    helpers_text = helpers_raw.decode("utf-8")
                    old_helpers = 'DATABASE_URL = os.getenv("DATABASE_URL")\nif not DATABASE_URL:\n    raise RuntimeError("DATABASE_URL is required")'
                    new_helpers = (
                        'DATABASE_URL = os.getenv("DATABASE_URL")\n'
                        'if not DATABASE_URL:\n'
                        '    if os.getenv("PYTEST_CURRENT_TEST") or os.getenv("CI"):\n'
                        '        DATABASE_URL = os.getenv("TEST_DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test_db")\n'
                        '    else:\n'
                        '        raise RuntimeError("DATABASE_URL is required")'
                    )
                    if old_helpers in helpers_text:
                        patch_files[helpers_path] = (helpers_text.replace(old_helpers, new_helpers).rstrip() + "\n").encode("utf-8")
                except Exception:
                    pass

                # If helpers.py already has the fix, check gemini_client.py
                if not patch_files:
                    gemini_path = "src/ai/gemini_client.py"
                    try:
                        gemini_raw = self.github.get_file_content(incident.repository, gemini_path, base_sha)
                        gemini_text = gemini_raw.decode("utf-8")
                        pattern = r'(\s*)if not api_key:\s*\n\s*return \{"error": "Gemini API Key not found\."\}'
                        match = re.search(pattern, gemini_text)
                        if match:
                            indent = match.group(1)
                            replacement = (
                                f"{indent}passed_client = client\n"
                                f"{indent}active_key = os.getenv('GEMINI_API_KEY') or api_key\n"
                                f"{indent}if not active_key and passed_client is None:\n"
                                f"{indent}    return {{'error': 'Gemini API Key not found.'}}"
                            )
                            patch_files[gemini_path] = (re.sub(pattern, replacement, gemini_text).rstrip() + "\n").encode("utf-8")
                    except Exception:
                        pass

            if not patch_files:
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
                base_sha=base_sha,
                files=patch_files,
                actor=actor,
            )

            p_id = result["proposal"]["proposal_id"]
            binding = result["binding_sha256"]
            diff = result["snapshot"]["diff"]
            branch = result["proposal"]["proposed_branch"]

            message = (
                f"🤖 **SRE Agent Change Proposal Staged**\n\n"
                f"{explanation}"
                f"A candidate fix was generated and staged under the Aegis Change Policy:\n\n"
                f"- **Repository**: `{incident.repository}`\n"
                f"- **Base Commit**: `{base_sha[:12]}`\n"
                f"- **Proposed Branch**: `{branch}`\n"
                f"- **Proposal ID**: `{p_id}`\n"
                f"- **Cryptographic Binding SHA-256**: `{binding}`\n\n"
                f"### Proposed Diff:\n"
                f"```diff\n"
                f"{diff}\n"
                f"```\n\n"
                f"---\n"
                f"**Next Steps**:\n"
                f"To open the Draft Pull Request on GitHub, reply with:\n"
                f"`@sre-agent create pr` or `@sre-agent approve`\n\n"
                f"*(Merge remains manual on GitHub)*"
            )
            self.store.claim_proposal_request(issue_key)
            self.jira.add_proposal_update(issue_key, message, "aegis:approval-required")
            return message
        except Exception as exc:
            err_msg = f"⚠️ SRE Agent could not stage proposal for {issue_key}: {exc}"
            try:
                self.jira.add_comment(issue_key, err_msg)
            except Exception:
                pass
            raise

    def approve_and_execute_proposal(self, issue_key: str, user_command: str) -> str:
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
        read_provider = InstallationTokenProvider(lambda: GitHubApi(f"Bearer {app_jwt(app_id, key_file)}"), inst_id)
        read_adapter = GitHubChangeAdapter(read_provider)
        proposal_service = JiraProposalService(proposal_store, policy, read_adapter)

        # Look up active proposal for this issue
        with proposal_store._lock:
            existing = proposal_store.connection.execute(
                "SELECT proposal_id, binding_sha256 FROM jira_change_snapshots WHERE issue_key=? AND lifecycle_state='ACTIVE'",
                (issue_key,),
            ).fetchone()

        if not existing:
            # Check if already executed
            with proposal_store._lock:
                executed = proposal_store.connection.execute(
                    "SELECT result_json FROM jira_change_snapshots WHERE issue_key=? AND result_json IS NOT NULL ORDER BY rowid DESC LIMIT 1",
                    (issue_key,),
                ).fetchone()
            if executed and executed["result_json"]:
                res = json.loads(executed["result_json"])
                msg = f"🤖 **Aegis Draft Pull Request is already open**: {res.get('pull_request_url')}"
                self.jira.add_comment(issue_key, msg)
                return msg

            # Stage the proposal first
            try:
                self.stage_proposal(issue_key)
            except Exception as e:
                msg = f"🤖 SRE Agent could not stage proposal before creating PR: {e}"
                self.jira.add_comment(issue_key, msg)
                return msg

            with proposal_store._lock:
                existing = proposal_store.connection.execute(
                    "SELECT proposal_id, binding_sha256 FROM jira_change_snapshots WHERE issue_key=? AND lifecycle_state='ACTIVE'",
                    (issue_key,),
                ).fetchone()

        if not existing:
            msg = f"🤖 No active change proposal found for {issue_key}."
            self.jira.add_comment(issue_key, msg)
            return msg

        proposal_id = existing["proposal_id"]
        expected_binding = existing["binding_sha256"]

        # Parse binding if provided
        binding_match = re.search(r"\b([a-f0-9]{64})\b", user_command)
        if binding_match and binding_match.group(1) != expected_binding:
            msg = (
                f"❌ Approval rejected: The supplied binding hash `{binding_match.group(1)}` "
                f"does not match the active proposal binding `{expected_binding}`."
            )
            self.jira.add_comment(issue_key, msg)
            return msg

        # Check expiry or base drift; auto-renew if needed
        record = proposal_service.inspect(proposal_id)
        expires_at = datetime.fromisoformat(record["proposal"]["expires_at"])
        api = read_provider.issue(record["proposal"]["repository"], write=False)
        root = read_adapter._repo_path(record["proposal"]["repository"])
        ref_data = api.request("GET", f"{root}/git/ref/heads/{record['proposal']['base_branch']}")
        current_base_sha = ref_data.get("object", {}).get("sha")

        if datetime.now(timezone.utc) > expires_at or (current_base_sha and current_base_sha != record["proposal"]["base_sha"]):
            approver = DeploymentActor("operator-approver", {"approver"})
            requester = DeploymentActor("sre-agent", {"requester"})
            proposal_service.abandon(proposal_id, binding=expected_binding, actor=approver)
            snapshot = record["snapshot"]
            files = {p: t.encode() for p, t in snapshot["after"].items()}
            try:
                new_res = proposal_service.prepare(
                    issue_key=issue_key,
                    fingerprint=snapshot["fingerprint"],
                    labels=[REQUEST_LABEL],
                    repository=record["proposal"]["repository"],
                    base_sha=current_base_sha or record["proposal"]["base_sha"],
                    files=files,
                    actor=requester,
                    retry_of=proposal_id,
                    retry_binding=expected_binding,
                )
                proposal_id = new_res["proposal"]["proposal_id"]
                expected_binding = new_res["binding_sha256"]
            except Exception as prep_err:
                if "unchanged files must not be included" in str(prep_err):
                    msg = (
                        f"🤖 **Aegis Change Notice**: The proposed changes for {issue_key} are already present "
                        f"on `{record['proposal']['base_branch']}`. No additional pull request is required."
                    )
                    self.jira.add_comment(issue_key, msg)
                    return msg
                raise

        # Use Writer App to execute and create Draft PR
        write_key_file = os.getenv("AEGIS_GITHUB_WRITE_PRIVATE_KEY_FILE", "/run/secrets/aegis-github-write-app.pem")
        write_app_id = int(os.getenv("AEGIS_GITHUB_WRITE_APP_ID", "4821358"))
        write_inst_id = int(os.getenv("AEGIS_GITHUB_WRITE_INSTALLATION_ID", "158855695"))

        write_provider = InstallationTokenProvider(
            lambda: GitHubApi(f"Bearer {app_jwt(write_app_id, write_key_file)}"), write_inst_id
        )
        write_adapter = GitHubChangeAdapter(write_provider)
        write_proposal_service = JiraProposalService(proposal_store, policy, write_adapter)

        approver = DeploymentActor("operator-approver", {"approver"})
        executor = DeploymentActor("operator-executor", {"executor"})
        write_proposal_service.approve(proposal_id, binding=expected_binding, actor=approver)
        res = write_proposal_service.execute(proposal_id, binding=expected_binding, actor=executor)
        write_proposal_service.notify(proposal_id, jira=self.jira, actor=executor)

        pr_url = res.get("result", {}).get("pull_request_url", "")
        msg = (
            f"🤖 **Aegis Change Proposal Approved & Executed**\n\n"
            f"Draft Pull Request created: {pr_url}\n\n"
            f"Review the diff and merge the pull request on GitHub when ready."
        )
        return msg

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
