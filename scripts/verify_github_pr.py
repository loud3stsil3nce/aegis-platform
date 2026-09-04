#!/usr/bin/env python3
"""Read-only verification of one governed GitHub draft pull request."""

from __future__ import annotations

import argparse
import json
import sqlite3

from core.change_management.github_adapter import InstallationTokenProvider
from core.change_management.github_app import GitHubApi, app_jwt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-id", type=int, required=True)
    parser.add_argument("--installation-id", type=int, required=True)
    parser.add_argument("--private-key-file", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pull-request", type=int, required=True)
    parser.add_argument("--proposal-id", required=True)
    parser.add_argument("--state", required=True)
    args = parser.parse_args()
    provider = InstallationTokenProvider(
        lambda: GitHubApi(f"Bearer {app_jwt(args.app_id, args.private_key_file)}"),
        args.installation_id,
    )
    api = provider.issue(args.repository, write=False)
    root = "/repos/" + args.repository
    pull = api.request("GET", f"{root}/pulls/{args.pull_request}")
    files = api.request("GET", f"{root}/pulls/{args.pull_request}/files?per_page=100")
    branch = pull.get("head", {}).get("ref", "")
    ref = api.request("GET", f"{root}/git/ref/heads/{branch.replace('/', '%2F')}")
    connection = sqlite3.connect(args.state)
    connection.row_factory = sqlite3.Row
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        events = [dict(row) for row in connection.execute(
            "SELECT status,actor,detail FROM change_events WHERE proposal_id=? ORDER BY created_at,event_id",
            (args.proposal_id,),
        )]
    finally:
        connection.close()
    print(json.dumps({
        "number": pull.get("number"), "state": pull.get("state"),
        "draft": pull.get("draft"), "url": pull.get("html_url"),
        "base": pull.get("base", {}).get("ref"),
        "baseSha": pull.get("base", {}).get("sha"),
        "head": branch, "headSha": pull.get("head", {}).get("sha"),
        "refSha": ref.get("object", {}).get("sha"),
        "files": [{
            "filename": item.get("filename"), "status": item.get("status"),
            "additions": item.get("additions"), "deletions": item.get("deletions"),
        } for item in files],
        "auditIntegrity": integrity, "auditEvents": events,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
