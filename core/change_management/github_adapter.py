"""Exact Git-object adapter for approved Aegis proposal branches and PRs."""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
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

    def __init__(
        self, app_api_provider: Callable[[], GitHubApi], installation_id: int,
        *, require_expiry: bool = False,
    ):
        if installation_id <= 0:
            raise ValueError("installation ID must be positive")
        self.app_api_provider, self.installation_id = app_api_provider, installation_id
        self.require_expiry = require_expiry

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
        if self.require_expiry:
            try:
                expiry = datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00"))
                remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
            except (KeyError, TypeError, ValueError) as exc:
                raise GitHubChangeError("installation token expiry is invalid") from exc
            if not 30 < remaining <= 3600:
                raise GitHubChangeError("installation token lifetime must be at most one hour")
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

    def read_snapshot(
        self, change: ValidatedChange, *, expected_base_sha: str,
    ) -> dict[str, bytes | None]:
        """Read exact ordinary-file bytes; reject symlinks, submodules and truncation."""
        preflight = self.preflight(change, expected_base_sha=expected_base_sha)
        api = self.token_provider.issue(change.repository, write=False)
        root = self._repo_path(change.repository)
        trees: dict[str, list] = {}
        result: dict[str, bytes | None] = {}
        total = 0
        for path in change.paths:
            parts = path.split("/")
            if len(parts) > 8 or any(part in {"", ".", "..", ".git", ".github"} for part in parts):
                raise GitHubChangeError("snapshot path is invalid")
            tree_sha = preflight.base_tree_sha
            for index, part in enumerate(parts):
                if tree_sha not in trees:
                    tree = api.request("GET", f"{root}/git/trees/{tree_sha}")
                    if tree.get("truncated") or not isinstance(tree.get("tree"), list):
                        raise GitHubChangeError("base tree is missing or truncated")
                    trees[tree_sha] = tree["tree"]
                matches = [entry for entry in trees[tree_sha] if entry.get("path") == part]
                if not matches:
                    result[path] = None
                    break
                if len(matches) != 1:
                    raise GitHubChangeError("base tree path is ambiguous")
                entry = matches[0]
                if index < len(parts) - 1:
                    if entry.get("type") != "tree" or entry.get("mode") != "040000":
                        raise GitHubChangeError("path parent is not an ordinary directory")
                    tree_sha = entry["sha"]
                    continue
                if entry.get("type") != "blob" or entry.get("mode") != "100644":
                    raise GitHubChangeError("only ordinary non-executable files may be proposed")
                if type(entry.get("size")) is not int or not 0 <= entry["size"] <= 131_072 - total:
                    raise GitHubChangeError("base snapshot exceeds byte budget")
                blob = api.request("GET", f"{root}/git/blobs/{entry['sha']}")
                if blob.get("encoding") != "base64":
                    raise GitHubChangeError("base blob encoding is invalid")
                try:
                    content = base64.b64decode("".join(blob["content"].split()), validate=True)
                except (KeyError, ValueError, TypeError) as exc:
                    raise GitHubChangeError("base blob content is invalid") from exc
                object_sha = hashlib.sha1(f"blob {len(content)}".encode() + bytes([0]) + content).hexdigest()
                if len(content) != entry["size"] or object_sha != entry["sha"]:
                    raise GitHubChangeError("base blob does not match its Git object")
                total += len(content)
                result[path] = content
        return result

    def create_pull_request(
        self, change: ValidatedChange, *, expected_base_sha: str,
        files: dict[str, bytes], commit_message: str, title: str, body: str,
        rollback_on_pr_failure: bool = True,
        before_write: Callable[[], None] | None = None,
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
        if before_write is not None:
            before_write()
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
            if not rollback_on_pr_failure:
                # A timeout may mean the PR was created. Phase B preserves the
                # branch for inspection instead of deleting potentially live state.
                raise GitHubChangeError(
                    "draft PR outcome is indeterminate; preserve branch for manual recovery"
                ) from exc
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
