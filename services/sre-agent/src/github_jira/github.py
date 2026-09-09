"""Repository-scoped, GET-only GitHub evidence adapter."""

from __future__ import annotations

import base64
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .models import FAILURE_CONCLUSIONS, SHA_RE, FailureEvent
from .security import MAX_LOG_BYTES, redact_and_bound_logs


API_VERSION = "2022-11-28"
MAX_JSON_BYTES = 1_048_576
MAX_JOBS = 10
MAX_PULL_REQUESTS = 10
MAX_CHECKS = 20
READ_PERMISSIONS = {
    "actions": "read",
    "checks": "read",
    "contents": "read",
    "metadata": "read",
    "pull_requests": "read",
}
MAX_KEY_BYTES = 16_384


class GitHubReadError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _app_jwt(app_id: int, private_key_file: str) -> str:
    path = Path(private_key_file)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GitHubReadError("GitHub App private key is unavailable") from exc
    if app_id <= 0 or not raw or len(raw) > MAX_KEY_BYTES:
        raise GitHubReadError("GitHub App signing configuration is invalid")
    timestamp = int(time.time())
    header = _b64url(b'{"alg":"RS256","typ":"JWT"}')
    payload = _b64url(
        json.dumps(
            {"iat": timestamp - 60, "exp": timestamp + 540, "iss": str(app_id)},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    try:
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(path)],
            input=signing_input,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitHubReadError("GitHub App JWT signing failed") from exc
    if result.returncode or not result.stdout:
        raise GitHubReadError("GitHub App JWT signing failed")
    return f"{header}.{payload}.{_b64url(result.stdout)}"


def _mint_installation_token(
    app_id: int, installation_id: int, private_key_file: str, repository: str,
    permissions: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    name = repository.split("/", 1)[1]
    body = json.dumps(
        {"repositories": [name], "permissions": permissions if permissions is not None else READ_PERMISSIONS},
        separators=(",", ":"),
    ).encode()
    request = urllib.request.Request(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        data=body,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + _app_jwt(app_id, private_key_file),
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "aegis-github-jira-read/0.1",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = _read_limited(response, MAX_JSON_BYTES)
    except urllib.error.HTTPError as exc:
        raise GitHubReadError(
            f"GitHub rejected installation token request with HTTP {exc.code}", exc.code
        ) from exc
    except urllib.error.URLError as exc:
        raise GitHubReadError("GitHub installation token request failed") from exc
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GitHubReadError("GitHub returned an invalid installation token response") from exc
    if not isinstance(result, dict):
        raise GitHubReadError("GitHub returned an invalid installation token response")
    return result


@dataclass(frozen=True)
class GitHubEvidence:
    run: dict[str, Any]
    jobs: tuple[dict[str, Any], ...]
    checks: tuple[dict[str, Any], ...]
    commit: dict[str, Any]
    pull_requests: tuple[dict[str, Any], ...]
    logs: str

    def stable_payload(self) -> dict[str, Any]:
        return {
            "run": self.run,
            "jobs": list(self.jobs),
            "checks": list(self.checks),
            "commit": self.commit,
            "pull_requests": list(self.pull_requests),
            "logs": self.logs,
        }


class ReadOnlyInstallationTokenProvider:
    """Mint only an exact repository-scoped read token from a GitHub App."""

    def __init__(self, app_id: int, installation_id: int, private_key_file: str, *, include_statuses: bool = False):
        if app_id <= 0 or installation_id <= 0 or not private_key_file:
            raise ValueError("GitHub read App configuration is invalid")
        self.app_id = app_id
        self.installation_id = installation_id
        self.private_key_file = private_key_file
        self._cached: dict[str, tuple[str, float]] = {}
        self.permissions = dict(READ_PERMISSIONS)
        self.include_statuses = include_statuses
        if include_statuses:
            self.permissions["statuses"] = "read"

    def __call__(self, repository: str) -> str:
        cached = self._cached.get(repository)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        args = (self.app_id, self.installation_id, self.private_key_file, repository)
        result = (
            _mint_installation_token(*args, permissions=self.permissions)
            if self.include_statuses else _mint_installation_token(*args)
        )
        token = result.get("token")
        permissions = result.get("permissions", {})
        repositories = {item.get("full_name") for item in result.get("repositories", [])}
        if not token or permissions != self.permissions or repositories != {repository}:
            raise GitHubReadError("GitHub read token scope or permissions are not exact")
        expires_at = result.get("expires_at")
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp()
        except (AttributeError, ValueError):
            if self.include_statuses:
                raise GitHubReadError("CI read token expiry is invalid")
            expiry = time.time() + 300
        if self.include_statuses and not 30 < expiry - time.time() <= 3600:
            raise GitHubReadError("CI read token lifetime is outside policy")
        self._cached[repository] = (token, expiry)
        return token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _read_limited(response: Any, max_bytes: int) -> bytes:
    raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise GitHubReadError("GitHub response exceeds policy limit")
    return raw


def _read_tail(response: Any, max_bytes: int) -> bytes:
    """Read a response and bound to max_bytes, keeping the tail where tracebacks are."""
    raw = response.read()
    if len(raw) > max_bytes:
        return raw[-max_bytes:]
    return raw


class GitHubReadClient:
    """Expose only the GitHub GET operations required for incident diagnosis."""

    def __init__(
        self,
        token_provider: Callable[[str], str],
        allowed_repositories: Iterable[str],
        base_url: str = "https://api.github.com",
        retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.token_provider = token_provider
        self.allowed_repositories = frozenset(allowed_repositories)
        self.base_url = base_url.rstrip("/")
        self.retries = max(0, min(int(retries), 3))
        self.sleep = sleep

    def _root(self, repository: str) -> str:
        if repository not in self.allowed_repositories:
            raise GitHubReadError("repository is not allowlisted")
        owner, name = repository.split("/", 1)
        return "/repos/{}/{}".format(
            urllib.parse.quote(owner, safe=""), urllib.parse.quote(name, safe="")
        )

    def _request(self, repository: str, path: str, max_bytes: int = MAX_JSON_BYTES) -> bytes:
        if not path.startswith("/") or path.startswith("//"):
            raise GitHubReadError("GitHub API path is invalid")
        authorization = "Bearer " + self.token_provider(repository)
        request = urllib.request.Request(
            self.base_url + path,
            method="GET",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": authorization,
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "aegis-github-jira-read/0.1",
            },
        )
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    return _read_limited(response, max_bytes)
            except urllib.error.HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt >= self.retries:
                    raise GitHubReadError(
                        f"GitHub read failed with HTTP {exc.code}", status_code=exc.code
                    ) from exc
            except urllib.error.URLError as exc:
                if attempt >= self.retries:
                    raise GitHubReadError("GitHub read failed") from exc
            self.sleep(0.1 * (2**attempt))
        raise GitHubReadError("GitHub read retry budget exhausted")

    def _download_job_log(self, repository: str, job_id: int, max_bytes: int = MAX_LOG_BYTES) -> bytes:
        """Follow only GitHub's HTTPS log redirect and never forward authorization."""

        root = self._root(repository)
        request = urllib.request.Request(
            self.base_url + f"{root}/actions/jobs/{job_id}/logs",
            method="GET",
            headers={
                "Authorization": "Bearer " + self.token_provider(repository),
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "aegis-github-jira-read/0.1",
            },
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=15) as response:
                return _read_limited(response, max_bytes)
        except urllib.error.HTTPError as exc:
            if exc.code not in {301, 302, 303, 307, 308}:
                raise GitHubReadError(
                    f"GitHub job-log read failed with HTTP {exc.code}", status_code=exc.code
                ) from exc
            location = exc.headers.get("Location")
        parsed = urllib.parse.urlsplit(location or "")
        host = (parsed.hostname or "").casefold()
        allowed_redirect = parsed.scheme == "https" and any(
            host == suffix.lstrip(".") or host.endswith(suffix)
            for suffix in (
                ".actions.githubusercontent.com",
                ".githubusercontent.com",
                ".blob.core.windows.net",
            )
        )
        if not allowed_redirect:
            raise GitHubReadError("GitHub job-log redirect target is not allowlisted")
        # The signed redirect URL authorizes the download. Deliberately omit the
        # GitHub App token so it cannot leak to an object-storage host.
        redirected = urllib.request.Request(
            location, method="GET", headers={"User-Agent": "aegis-github-jira-read/0.1"}
        )
        try:
            with urllib.request.urlopen(redirected, timeout=15) as response:
                return _read_tail(response, max_bytes)
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise GitHubReadError("GitHub job-log download failed") from exc

    def _json(self, repository: str, path: str) -> Any:
        raw = self._request(repository, path)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GitHubReadError("GitHub returned invalid JSON") from exc

    def get_file_content(self, repository: str, path: str, ref: str) -> bytes:
        encoded_path = urllib.parse.quote(path.strip("/"))
        data = self._json(repository, f"{self._root(repository)}/contents/{encoded_path}?ref={urllib.parse.quote(ref)}")
        if isinstance(data, dict) and data.get("encoding") == "base64" and "content" in data:
            return base64.b64decode(data["content"])
        raise GitHubReadError(f"unable to read file content for {path} at {ref}")

    def get_tree(self, repository: str, ref: str) -> list[str]:
        """Fetch all blob file paths in the repository at ref using Git Trees API."""
        try:
            data = self._json(repository, f"{self._root(repository)}/git/trees/{urllib.parse.quote(ref)}?recursive=1")
            if isinstance(data, dict) and "tree" in data and isinstance(data["tree"], list):
                return [
                    item["path"]
                    for item in data["tree"]
                    if isinstance(item, dict) and item.get("type") == "blob" and isinstance(item.get("path"), str)
                ]
        except Exception:
            pass
        return []

    def get_multiple_files(
        self, repository: str, paths: Iterable[str], ref: str, max_total_bytes: int = 262_144
    ) -> dict[str, str]:
        """Fetch contents of multiple project files, up to a bounded total byte budget."""
        results: dict[str, str] = {}
        total_bytes = 0
        for path in paths:
            if total_bytes >= max_total_bytes:
                break
            try:
                content = self.get_file_content(repository, path, ref)
                if total_bytes + len(content) > max_total_bytes:
                    content = content[: max_total_bytes - total_bytes]
                results[path] = content.decode("utf-8", errors="replace")
                total_bytes += len(content)
            except Exception:
                continue
        return results

    @staticmethod
    def _summary(item: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
        return {key: item.get(key) for key in keys if item.get(key) is not None}

    def fetch_evidence(self, event: FailureEvent) -> GitHubEvidence:
        root = self._root(event.repository)
        if event.source_kind == "workflow_run":
            run_raw = self._json(event.repository, f"{root}/actions/runs/{event.run_id}")
            jobs_raw = self._json(
                event.repository, f"{root}/actions/runs/{event.run_id}/jobs?per_page={MAX_JOBS}"
            )
            run = self._summary(
                run_raw,
                ("id", "name", "status", "conclusion", "head_branch", "head_sha", "html_url"),
            )
            summarized_jobs = []
            for job in jobs_raw.get("jobs", [])[:MAX_JOBS]:
                summary = self._summary(
                    job, ("id", "name", "status", "conclusion", "html_url")
                )
                summary["steps"] = [
                    self._summary(step, ("number", "name", "status", "conclusion"))
                    for step in job.get("steps", [])[:50]
                ]
                summarized_jobs.append(summary)
            jobs = tuple(summarized_jobs)
        else:
            check = self._json(event.repository, f"{root}/check-runs/{event.run_id}")
            run = self._summary(
                check, ("id", "name", "status", "conclusion", "head_sha", "html_url")
            )
            output = check.get("output", {})
            run["output"] = {
                "title": str(output.get("title", ""))[:500],
                "summary": str(output.get("summary", ""))[:2_000],
            }
            jobs = ()

        commit_raw = self._json(event.repository, f"{root}/commits/{event.commit_sha}")
        checks_raw = self._json(
            event.repository, f"{root}/commits/{event.commit_sha}/check-runs?per_page={MAX_CHECKS}"
        )
        pulls_raw = self._json(
            event.repository, f"{root}/commits/{event.commit_sha}/pulls?per_page={MAX_PULL_REQUESTS}"
        )
        commit = {
            "sha": commit_raw.get("sha"),
            "html_url": commit_raw.get("html_url"),
            "message": str(commit_raw.get("commit", {}).get("message", ""))[:500],
        }
        checks = tuple(
            self._summary(item, ("id", "name", "status", "conclusion", "html_url"))
            for item in checks_raw.get("check_runs", [])[:MAX_CHECKS]
        )
        pull_requests = tuple(
            self._summary(item, ("number", "title", "state", "html_url"))
            for item in pulls_raw[:MAX_PULL_REQUESTS]
        )
        log_lines = []
        raw_log_bytes = bytearray()
        for job in jobs:
            if job.get("conclusion") in FAILURE_CONCLUSIONS:
                log_lines.append("Job {!r} failed.".format(job.get("name", "unknown")))
                for step in job.get("steps", [])[:50]:
                    if step.get("conclusion") in FAILURE_CONCLUSIONS:
                        log_lines.append("Step {!r} failed.".format(step.get("name", "unknown")))
                remaining = MAX_LOG_BYTES - len(raw_log_bytes)
                job_id = job.get("id")
                if remaining > 0 and isinstance(job_id, int):
                    try:
                        raw_log_bytes.extend(
                            self._download_job_log(event.repository, job_id, remaining)
                        )
                    except GitHubReadError:
                        log_lines.append("[bounded job log unavailable]")
        if event.source_kind == "check_run":
            output = run.get("output") or {}
            log_lines.extend([str(output.get("title", "")), str(output.get("summary", ""))])
        logs = redact_and_bound_logs(
            "\n".join(line for line in log_lines if line).encode() + b"\n" + bytes(raw_log_bytes)
        )
        return GitHubEvidence(run, jobs, checks, commit, pull_requests, logs)

    def list_recent_failures(
        self, repository: str, since: datetime, limit: int = 20
    ) -> tuple[FailureEvent, ...]:
        root = self._root(repository)
        limit = max(1, min(int(limit), 20))
        raw = self._json(repository, f"{root}/actions/runs?status=completed&per_page={limit}")
        events = []
        threshold = since.astimezone(timezone.utc)
        for run in raw.get("workflow_runs", [])[:limit]:
            if run.get("conclusion") not in FAILURE_CONCLUSIONS:
                continue
            try:
                updated_at = datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
            except (KeyError, AttributeError, ValueError):
                continue
            if updated_at < threshold:
                continue
            sha = str(run.get("head_sha", "")).lower()
            run_id = run.get("id")
            if not SHA_RE.fullmatch(sha) or not isinstance(run_id, int):
                continue
            events.append(
                FailureEvent(
                    repository=repository,
                    workflow=str(run.get("name") or "unknown")[:200],
                    branch=str(run.get("head_branch") or "unknown")[:200],
                    commit_sha=sha,
                    run_id=run_id,
                    source_kind="workflow_run",
                    conclusion=str(run["conclusion"]),
                )
            )
        return tuple(events)
