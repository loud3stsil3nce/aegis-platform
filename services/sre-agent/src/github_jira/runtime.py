"""Fail-closed environment wiring for the GitHub–Jira workflow."""

from __future__ import annotations

import os
import stat
import sys
import threading
from pathlib import Path
from typing import Optional

from .github import GitHubReadClient, ReadOnlyInstallationTokenProvider
from .jira import JiraIncidentAdapter
from .service import GitHubJiraWorkflow
from .store import IncidentStore


PHASE_A_REPOSITORY = "loud3stsil3nce/aegis-platform"
_lock = threading.Lock()
_workflow: Optional[GitHubJiraWorkflow] = None
_configuration_error: Optional[str] = None


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not configured")
    return value


def _owner_only_secret_file(name: str) -> str:
    path = Path(_required(name))
    if not path.is_file():
        raise RuntimeError(f"{name} is unavailable")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError(f"{name} must be owner-only")
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 32:
        raise RuntimeError(f"{name} must contain at least 32 characters")
    return value


def build_workflow() -> GitHubJiraWorkflow:
    repository = _required("AEGIS_GITHUB_ALLOWED_REPOSITORIES")
    if repository != PHASE_A_REPOSITORY:
        raise RuntimeError("Phase A allows exactly loud3stsil3nce/aegis-platform")
    core_root = os.getenv("AEGIS_CORE_PYTHON_ROOT", "/app/code")
    if core_root not in sys.path:
        sys.path.insert(0, core_root)

    try:
        app_id = int(_required("AEGIS_GITHUB_READ_APP_ID"))
        installation_id = int(_required("AEGIS_GITHUB_READ_INSTALLATION_ID"))
    except ValueError as exc:
        raise RuntimeError("GitHub read App IDs must be integers") from exc
    private_key = _required("AEGIS_GITHUB_READ_PRIVATE_KEY_FILE")
    if not Path(private_key).is_file():
        raise RuntimeError("GitHub read App private key file is unavailable")
    if stat.S_IMODE(Path(private_key).stat().st_mode) & 0o077:
        raise RuntimeError("GitHub read App private key file must be owner-only")

    from jira import JIRA

    jira_client = JIRA(
        server=_required("JIRA_URL"),
        basic_auth=(_required("JIRA_USER_EMAIL"), _required("JIRA_API_TOKEN")),
        timeout=15,
        max_retries=0,
    )
    jira_adapter = JiraIncidentAdapter(jira_client, _required("JIRA_PROJECT_KEY"))
    token_provider = ReadOnlyInstallationTokenProvider(app_id, installation_id, private_key)
    github = GitHubReadClient(token_provider, {repository})
    store_path = Path(
        os.getenv("AEGIS_GITHUB_JIRA_STORE", "/app/state/github-jira-phase-a.sqlite3")
    )
    store_path.parent.mkdir(parents=True, exist_ok=True)
    return GitHubJiraWorkflow(
        IncidentStore(store_path),
        github,
        jira_adapter,
        _owner_only_secret_file("AEGIS_GITHUB_WEBHOOK_SECRET_FILE"),
        {repository},
    )


def get_workflow() -> Optional[GitHubJiraWorkflow]:
    global _workflow, _configuration_error
    if _workflow is not None:
        return _workflow
    with _lock:
        if _workflow is not None:
            return _workflow
        try:
            _workflow = build_workflow()
            _configuration_error = None
        except Exception as exc:
            _configuration_error = str(exc)
            return None
    return _workflow


def configuration_error() -> Optional[str]:
    return _configuration_error


def poll_ci_feedback() -> Optional[str]:
    """Disabled by default; only the reader App and metadata manifest enter SRE."""
    if os.getenv("AEGIS_GITHUB_CI_ENABLED", "0").casefold() not in {"1", "true", "yes"}:
        return None
    from .ci import CIFeedback, read_manifest

    records = read_manifest(_required("AEGIS_GITHUB_CI_MANIFEST"))
    workflow = get_workflow()
    if workflow is None:
        raise RuntimeError("read-only GitHub/Jira runtime unavailable")
    path = Path(_required("AEGIS_GITHUB_CI_STORE"))
    path.parent.mkdir(parents=True, exist_ok=True)
    provider = ReadOnlyInstallationTokenProvider(
        int(_required("AEGIS_GITHUB_READ_APP_ID")),
        int(_required("AEGIS_GITHUB_READ_INSTALLATION_ID")),
        _required("AEGIS_GITHUB_READ_PRIVATE_KEY_FILE"),
        include_statuses=True,
    )
    reader = GitHubReadClient(provider, {PHASE_A_REPOSITORY}, retries=0)
    feedback = CIFeedback(path)
    try:
        return feedback.poll(records, reader, workflow.jira)
    finally:
        feedback.close()
