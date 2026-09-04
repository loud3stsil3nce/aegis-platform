"""Exact Git-object adapter for approved Aegis proposal branches and PRs."""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

from .github_app import GitHubApi, GitHubAppError
from .policy import ValidatedChange


class GitHubChangeError(RuntimeError):
    pass


def content_manifest_sha256(files: dict[str, bytes]) -> str:
    manifest = [
        {"path": path, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        for path, content in sorted(files.items())
    ]
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class GitHubPreflight:
    repository: str
    base_branch: str
    base_sha: str
    base_tree_sha: str


@dataclass(frozen=True)
class GitHubPullRequestResult:
    repository: str
    branch: str
    commit_sha: str
    pull_request_number: int
    pull_request_url: str


class InstallationTokenProvider:
    """Mints a repository- and permission-narrowed token for one operation."""

    def __init__(self, app_api_provider: Callable[[], GitHubApi], installation_id: int):
        if installation_id <= 0:
            raise ValueError("installation ID must be positive")
        self.app_api_provider, self.installation_id = app_api_provider, installation_id

    def issue(self, repository: str, *, write: bool) -> GitHubApi:
        owner, name = repository.split("/", 1)
        level = "write" if write else "read"
        result = self.app_api_provider().request(
            "POST", f"/app/installations/{self.installation_id}/access_tokens",
            {"repositories": [name], "permissions": {
                "contents": level, "pull_requests": level, "metadata": "read",
            }},
        )
        token = result.get("token")
        permissions = result.get("permissions", {})
        repositories = result.get("repositories", [])
        names = {item.get("full_name") for item in repositories}
        if not token or permissions != {"contents": level, "metadata": "read", "pull_requests": level}:
            raise GitHubChangeError("installation token permissions are not exact")
        if names != {repository}:
            raise GitHubChangeError("installation token repository scope is not exact")
        return GitHubApi(f"Bearer {token}")


class GitHubChangeAdapter:
    def __init__(self, token_provider: InstallationTokenProvider):
        self.token_provider = token_provider

    @staticmethod
    def _repo_path(repository: str) -> str:
        owner, name = repository.split("/", 1)
        return f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"

    def preflight(self, change: ValidatedChange, *, expected_base_sha: str) -> GitHubPreflight:
        api = self.token_provider.issue(change.repository, write=False)
        root = self._repo_path(change.repository)
        branch = urllib.parse.quote(change.base_branch, safe="")
        reference = api.request("GET", f"{root}/git/ref/heads/{branch}")
        actual_sha = reference.get("object", {}).get("sha")
        if actual_sha != expected_base_sha:
            raise GitHubChangeError("GitHub base head changed after approval")
        commit = api.request("GET", f"{root}/git/commits/{actual_sha}")
        tree_sha = commit.get("tree", {}).get("sha")
        if not tree_sha:
            raise GitHubChangeError("GitHub base commit has no tree")
        return GitHubPreflight(change.repository, change.base_branch, actual_sha, tree_sha)

    def create_pull_request(
        self, change: ValidatedChange, *, expected_base_sha: str,
        files: dict[str, bytes], commit_message: str, title: str, body: str,
    ) -> GitHubPullRequestResult:
        if set(files) != set(change.paths):
            raise GitHubChangeError("file snapshot does not exactly match approved paths")
        if not commit_message.strip() or not title.strip():
            raise GitHubChangeError("commit message and pull request title are required")
        preflight = self.preflight(change, expected_base_sha=expected_base_sha)
        api = self.token_provider.issue(change.repository, write=True)
        root = self._repo_path(change.repository)
        branch = urllib.parse.quote(change.base_branch, safe="")
        write_head = api.request("GET", f"{root}/git/ref/heads/{branch}")
        if write_head.get("object", {}).get("sha") != preflight.base_sha:
            raise GitHubChangeError("GitHub base head changed before mutation")
        proposal_ref = urllib.parse.quote(change.proposed_branch, safe="")
        if api.request_optional("GET", f"{root}/git/ref/heads/{proposal_ref}") is not None:
            raise GitHubChangeError("governed proposal branch already exists")
        tree_entries = []
        for path, content in sorted(files.items()):
            blob = api.request("POST", f"{root}/git/blobs", {
                "content": base64.b64encode(content).decode("ascii"), "encoding": "base64",
            })
            tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = api.request("POST", f"{root}/git/trees", {
            "base_tree": preflight.base_tree_sha, "tree": tree_entries,
        })
        commit = api.request("POST", f"{root}/git/commits", {
            "message": commit_message, "tree": tree["sha"], "parents": [preflight.base_sha],
        })
        api.request("POST", f"{root}/git/refs", {
            "ref": f"refs/heads/{change.proposed_branch}", "sha": commit["sha"],
        })
        try:
            pull = api.request("POST", f"{root}/pulls", {
                "title": title, "body": body, "head": change.proposed_branch,
                "base": change.base_branch, "draft": True,
            })
        except Exception as exc:
            # Roll back only our exact branch and only while it still points to
            # the commit created by this invocation. Never delete changed state.
            current = api.request("GET", f"{root}/git/ref/heads/{proposal_ref}")
            if current.get("object", {}).get("sha") != commit["sha"]:
                raise GitHubChangeError(
                    "draft PR failed and proposal branch changed; manual recovery required"
                ) from exc
            try:
                api.request("DELETE", f"{root}/git/refs/heads/{proposal_ref}")
            except Exception as rollback_exc:
                raise GitHubChangeError(
                    "draft PR failed and exact branch rollback failed; manual recovery required"
                ) from rollback_exc
            raise GitHubChangeError("draft PR creation failed; exact proposal branch rolled back") from exc
        return GitHubPullRequestResult(
            change.repository, change.proposed_branch, commit["sha"],
            int(pull["number"]), str(pull["html_url"]),
        )
