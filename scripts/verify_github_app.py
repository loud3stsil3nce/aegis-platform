#!/usr/bin/env python3
"""Verify GitHub App installation scope without printing credentials."""

from __future__ import annotations

import argparse
import json

from core.change_management.github_app import GitHubApi, GitHubAppError, app_jwt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-id", type=int, required=True)
    parser.add_argument("--private-key-file", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()

    jwt = app_jwt(args.app_id, args.private_key_file)
    app_api = GitHubApi(f"Bearer {jwt}")
    app = app_api.request("GET", "/app")
    installations = app_api.request("GET", "/app/installations?per_page=100")
    matches = [item for item in installations if item.get("account", {}).get("login", "").lower() == args.account.lower()]
    if len(matches) != 1:
        raise GitHubAppError("expected exactly one installation for configured account")
    installation = matches[0]
    installation_id = int(installation["id"])
    token_result = app_api.request("POST", f"/app/installations/{installation_id}/access_tokens", {
        "repositories": [args.repository],
        "permissions": {"contents": "read", "pull_requests": "read", "metadata": "read"},
    })
    token = token_result.pop("token", None)
    if not token:
        raise GitHubAppError("GitHub did not issue an installation token")
    installation_api = GitHubApi(f"Bearer {token}")
    repositories = installation_api.request("GET", "/installation/repositories?per_page=100")
    names = sorted(item["full_name"] for item in repositories.get("repositories", []))
    expected = f"{args.account}/{args.repository}"
    if names != [expected]:
        raise GitHubAppError("installation token repository scope is not exact")
    output = {
        "appId": args.app_id,
        "appSlug": app.get("slug"),
        "installationId": installation_id,
        "account": installation.get("account", {}).get("login"),
        "repositorySelection": installation.get("repository_selection"),
        "installedPermissions": installation.get("permissions", {}),
        "tokenPermissions": token_result.get("permissions", {}),
        "tokenRepositories": names,
        "tokenExpiresAt": token_result.get("expires_at"),
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GitHubAppError as exc:
        raise SystemExit(f"verification failed: {exc}")
