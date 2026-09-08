"""Strict data contracts for GitHub failure incidents."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional


FAILURE_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
)
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
MAX_EVENT_BYTES = 262_144


class EventValidationError(ValueError):
    pass


def _bounded_text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise EventValidationError(f"{field} must be text")
    value = " ".join(value.split())
    if not value or len(value) > limit:
        raise EventValidationError(f"{field} is empty or exceeds its limit")
    return value


@dataclass(frozen=True)
class FailureEvent:
    repository: str
    workflow: str
    branch: str
    commit_sha: str
    run_id: int
    source_kind: str
    conclusion: str

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(
            [self.repository, self.workflow, self.commit_sha.lower(), self.run_id],
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(canonical).hexdigest()

    @property
    def evidence_url(self) -> str:
        return f"https://github.com/{self.repository}/actions/runs/{self.run_id}"

    @property
    def fingerprint_label(self) -> str:
        return f"aegis-fp-{self.fingerprint[:24]}"


@dataclass(frozen=True)
class IncidentRecord:
    fingerprint: str
    repository: str
    workflow: str
    branch: str
    commit_sha: str
    run_id: int
    source_kind: str
    conclusion: str
    status: str
    issue_key: Optional[str]
    event_count: int

    def as_event(self) -> FailureEvent:
        return FailureEvent(
            repository=self.repository,
            workflow=self.workflow,
            branch=self.branch,
            commit_sha=self.commit_sha,
            run_id=self.run_id,
            source_kind=self.source_kind,
            conclusion=self.conclusion,
        )


def parse_failure_webhook(
    event_name: str,
    payload: dict[str, Any],
    allowed_repositories: Iterable[str],
) -> Optional[FailureEvent]:
    """Normalize a failed workflow/check webhook; ignore non-failure events."""

    allowed = frozenset(allowed_repositories)
    repository = _bounded_text(
        payload.get("repository", {}).get("full_name"), "repository", 201
    )
    if not REPOSITORY_RE.fullmatch(repository) or repository not in allowed:
        raise EventValidationError("repository is not allowlisted")

    if event_name == "workflow_run":
        raw = payload.get("workflow_run")
        source_kind = "workflow_run"
        if not isinstance(raw, dict):
            raise EventValidationError("workflow_run payload is missing")
        if raw.get("status") != "completed":
            return None
        workflow = raw.get("name")
        branch = raw.get("head_branch") or "unknown"
        commit_sha = raw.get("head_sha")
    elif event_name == "check_run":
        raw = payload.get("check_run")
        source_kind = "check_run"
        if not isinstance(raw, dict):
            raise EventValidationError("check_run payload is missing")
        if raw.get("status") != "completed":
            return None
        workflow = raw.get("name")
        branch = raw.get("check_suite", {}).get("head_branch") or "unknown"
        commit_sha = raw.get("head_sha")
    else:
        return None

    conclusion = _bounded_text(raw.get("conclusion"), "conclusion", 40).casefold()
    if conclusion not in FAILURE_CONCLUSIONS:
        return None
    workflow = _bounded_text(workflow, "workflow", 200)
    branch = _bounded_text(branch, "branch", 200)
    commit_sha = _bounded_text(commit_sha, "commit SHA", 40).lower()
    if not SHA_RE.fullmatch(commit_sha):
        raise EventValidationError("commit SHA must be exactly 40 hexadecimal characters")
    run_id = raw.get("id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
        raise EventValidationError("run ID must be a positive integer")
    return FailureEvent(
        repository=repository,
        workflow=workflow,
        branch=branch,
        commit_sha=commit_sha,
        run_id=run_id,
        source_kind=source_kind,
        conclusion=conclusion,
    )
