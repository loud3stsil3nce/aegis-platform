"""Minimal GitHub App authentication with bounded, non-printing credentials."""

from __future__ import annotations

import base64
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


MAX_KEY_BYTES = 16_384
MAX_RESPONSE_BYTES = 1_048_576
API_VERSION = "2026-03-10"


class GitHubAppError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def app_jwt(app_id: int, private_key_file: str | Path, *, now: int | None = None) -> str:
    if app_id <= 0:
        raise GitHubAppError("GitHub App ID must be positive")
    path = Path(private_key_file)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GitHubAppError("GitHub App private key is unavailable") from exc
    if not raw or len(raw) > MAX_KEY_BYTES:
        raise GitHubAppError("GitHub App private key is empty or oversized")
    timestamp = int(time.time() if now is None else now)
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps(
        {"iat": timestamp - 60, "exp": timestamp + 540, "iss": str(app_id)},
        separators=(",", ":"),
    ).encode())
    signing_input = f"{header}.{payload}".encode()
    result = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(path)],
        input=signing_input, capture_output=True, check=False, timeout=10,
    )
    if result.returncode or not result.stdout:
        raise GitHubAppError("GitHub App JWT signing failed")
    return f"{header}.{payload}.{_b64url(result.stdout)}"


class GitHubApi:
    def __init__(self, authorization: str, *, base_url: str = "https://api.github.com"):
        if not authorization:
            raise GitHubAppError("GitHub authorization is required")
        self.authorization, self.base_url = authorization, base_url.rstrip("/")

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        if not path.startswith("/") or path.startswith("//"):
            raise GitHubAppError("GitHub API path is invalid")
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        request = urllib.request.Request(
            self.base_url + path, data=body, method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": self.authorization,
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "aegis-change-proposals/0.1",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise GitHubAppError(
                f"GitHub API rejected request with HTTP {exc.code}", status_code=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise GitHubAppError("GitHub API request failed") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise GitHubAppError("GitHub API response exceeds policy limit")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GitHubAppError("GitHub API returned invalid JSON") from exc

    def request_optional(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any | None:
        try:
            return self.request(method, path, payload)
        except GitHubAppError as exc:
            if exc.status_code == 404:
                return None
            raise
