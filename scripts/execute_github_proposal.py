#!/usr/bin/env python3
"""Execute one explicitly human-approved, policy-validated draft PR."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from core.change_management import ChangePolicy, ChangeProposalStore
from core.change_management.github_adapter import (
    GitHubChangeAdapter, InstallationTokenProvider,
)
from core.change_management.github_app import GitHubApi, app_jwt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--app-id", type=int, required=True)
    parser.add_argument("--installation-id", type=int, required=True)
    parser.add_argument("--private-key-file", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--base-branch", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--proposed-branch", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--requested-by", required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--commit-message", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", required=True)
    args = parser.parse_args()

    policy = ChangePolicy.from_dict(json.loads(Path(args.policy).read_text()))
    file_path = Path(args.file)
    content = file_path.read_bytes()
    diff_process = subprocess.run(
        ["git", "diff", "--no-index", "--", "/dev/null", args.file],
        capture_output=True, check=False, text=True,
    )
    if diff_process.returncode not in {0, 1} or not diff_process.stdout:
        raise RuntimeError("could not produce exact proposal diff")
    change = policy.validate(
        repository=args.repository, base_branch=args.base_branch,
        proposed_branch=args.proposed_branch, unified_diff=diff_process.stdout,
    )
    files = {change.paths[0]: content}
    store = ChangeProposalStore(args.state)
    try:
        proposal = store.propose(
            policy=policy, repository=args.repository, base_branch=args.base_branch,
            base_sha=args.base_sha, proposed_branch=args.proposed_branch,
            unified_diff=diff_process.stdout, file_contents=files,
            requested_by=args.requested_by,
        )
        app_api = lambda: GitHubApi(f"Bearer {app_jwt(args.app_id, args.private_key_file)}")
        adapter = GitHubChangeAdapter(InstallationTokenProvider(app_api, args.installation_id))
        adapter.preflight(change, expected_base_sha=args.base_sha)
        store.approve(
            proposal["proposal_id"], approved_by=args.approved_by,
            current_base_sha=args.base_sha,
        )
        store.consume(
            proposal["proposal_id"], diff_sha256=change.diff_sha256,
            content_manifest_sha256=proposal["content_manifest_sha256"],
            current_base_sha=args.base_sha, actor="github-executor",
        )
        try:
            result = adapter.create_pull_request(
                change, expected_base_sha=args.base_sha, files=files,
                commit_message=args.commit_message, title=args.title, body=args.body,
            )
        except Exception as exc:
            store.record_execution(
                proposal["proposal_id"], status="FAILED", actor="github-executor",
                detail=f"draft PR creation failed: {type(exc).__name__}",
            )
            raise
        store.record_execution(
            proposal["proposal_id"], status="SUCCESS", actor="github-executor",
            detail=f"draft pull request created: {result.pull_request_url}",
        )
        print(json.dumps({
            "proposalId": proposal["proposal_id"],
            "diffSha256": change.diff_sha256,
            "contentManifestSha256": proposal["content_manifest_sha256"],
            "branch": result.branch, "commitSha": result.commit_sha,
            "pullRequestNumber": result.pull_request_number,
            "pullRequestUrl": result.pull_request_url,
        }, sort_keys=True))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
