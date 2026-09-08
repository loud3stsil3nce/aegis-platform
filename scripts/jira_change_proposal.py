#!/usr/bin/env python3
"""Separate operator entry point for Phases B/C; never registered with SRE/MCP."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services/sre-agent"))

from core.change_management.deployment_auth import DeploymentActorAuthenticator
from core.change_management.github_adapter import GitHubChangeAdapter, InstallationTokenProvider
from core.change_management.github_app import GitHubApi, app_jwt
from core.change_management.jira_proposals import JiraProposalService, REPOSITORY, require_role
from core.change_management.policy import ChangePolicy
from core.change_management.proposals import ChangeProposalStore


def protected_file(path: str, limit: int = 16_384) -> bytes:
    target = Path(path)
    info = target.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.getuid():
        raise ValueError("operator files must be owned by the current user and owner-only")
    with target.open("rb") as handle:
        raw = handle.read(limit + 1)
    if not raw.strip() or len(raw) > limit:
        raise ValueError("operator file is empty or oversized")
    return raw


def incident_request(path: str, issue_key: str) -> dict:
    # Read-only connection does not migrate or mutate the deployed incident DB.
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT i.* FROM github_incidents i JOIN github_proposal_requests r "
            "ON r.issue_key=i.issue_key AND r.fingerprint=i.fingerprint WHERE i.issue_key=?",
            (issue_key,),
        ).fetchone()
    if row is None or row["repository"] != REPOSITORY:
        raise ValueError("a durable labeled, human-triggered platform incident request is required")
    return dict(row)


def jira_adapter():
    from jira import JIRA
    from src.github_jira.jira import JiraIncidentAdapter

    client = JIRA(
        server=os.environ["JIRA_URL"],
        basic_auth=(os.environ["JIRA_USER_EMAIL"], protected_file(os.environ["JIRA_API_TOKEN_FILE"], 4096).decode().strip()),
        timeout=15,
        max_retries=0,
    )
    return JiraIncidentAdapter(client, os.environ["JIRA_PROJECT_KEY"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="owner-only operator configuration JSON")
    parser.add_argument("--actor-token-file", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--issue", required=True)
    prepare.add_argument("--base-sha", required=True)
    prepare.add_argument("--snapshot", required=True, help='JSON {"docs/file.md": "exact text\\n"}')
    prepare.add_argument("--ttl", type=int, default=900)
    prepare.add_argument("--retry-of", help="exact abandoned predecessor ID")
    prepare.add_argument("--retry-binding", help="reviewed predecessor binding hash")
    sub.add_parser("export-ci", help="emit metadata-only manifest for an operator-controlled read-only SRE mount")
    for name in ("inspect", "approve", "execute", "notify", "abandon"):
        command = sub.add_parser(name)
        command.add_argument("--proposal", required=True)
        if name in {"approve", "execute", "abandon"}:
            command.add_argument("--binding", required=True)
    args = parser.parse_args()
    config = json.loads(protected_file(args.config))
    expected = {"state", "incidentStore", "policy", "actors", "appId", "installationId", "privateKeyFile"}
    if not isinstance(config, dict) or set(config) != expected:
        raise ValueError("operator configuration must contain exactly the documented fields")
    hashes = set()
    for record in config["actors"]:
        token = protected_file(record["tokenFile"], 4096).strip()
        if len(token) < 32 or token in hashes:
            raise ValueError("operator identities require distinct tokens of at least 32 bytes")
        hashes.add(token)
        if record.get("roles") not in (["requester"], ["approver"], ["executor"]):
            raise ValueError("each Phase B identity must have exactly one role")
    actor = DeploymentActorAuthenticator(config["actors"]).authenticate(
        "Bearer " + protected_file(args.actor_token_file, 4096).decode().strip()
    )
    role = {
        "prepare": "requester", "approve": "approver", "execute": "executor",
        "notify": "executor", "abandon": "approver", "export-ci": "executor",
    }
    if args.command in role:
        require_role(actor, role[args.command])
    state = Path(config["state"])
    if not state.parent.is_dir() or stat.S_IMODE(state.parent.stat().st_mode) & 0o077:
        raise ValueError("proposal state requires an existing owner-only directory")
    if state.exists():
        protected_file(str(state), 16_777_216)
    os.umask(0o077)
    policy = ChangePolicy.from_dict(json.loads(Path(config["policy"]).read_text()))
    # App keys are needed only for GitHub operations, never inspect/notify.
    if args.command in {"prepare", "approve", "execute"}:
        protected_file(config["privateKeyFile"])
    provider = InstallationTokenProvider(
        lambda: GitHubApi(f"Bearer {app_jwt(config['appId'], config['privateKeyFile'])}"),
        config["installationId"], require_expiry=True,
    )
    store = ChangeProposalStore(state)
    try:
        service = JiraProposalService(store, policy, GitHubChangeAdapter(provider))
        if args.command == "prepare":
            incident = incident_request(config["incidentStore"], args.issue)
            jira = jira_adapter()
            with Path(args.snapshot).open("rb") as handle:
                raw = handle.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ValueError("snapshot JSON exceeds byte limit")
            snapshot = json.loads(raw)
            if not isinstance(snapshot, dict) or not all(isinstance(v, str) for v in snapshot.values()):
                raise ValueError("snapshot must map repository paths to exact text")
            result = service.prepare(
                issue_key=args.issue, fingerprint=incident["fingerprint"],
                labels=jira.proposal_labels(args.issue), repository=incident["repository"],
                base_sha=args.base_sha, files={p: v.encode() for p, v in snapshot.items()},
                actor=actor, ttl_seconds=args.ttl,
                retry_of=args.retry_of, retry_binding=args.retry_binding,
            )
            proposal_id = result["proposal"]["proposal_id"]
            jira.add_proposal_update(
                args.issue,
                f"Aegis exact proposal prepared: {proposal_id}\n"
                f"Binding SHA-256: {result['binding_sha256']}\n"
                f"Diff SHA-256: {result['proposal']['diff_sha256']}\n"
                f"Content manifest SHA-256: {result['proposal']['content_manifest_sha256']}\n"
                f"Expires: {result['proposal']['expires_at']}\n"
                "Review the exact snapshot using the separate operator inspect command. "
                "No approval or GitHub write has occurred.",
                "aegis:approval-required",
            )
        elif args.command == "export-ci":
            result = service.ci_manifest(actor=actor)
        elif args.command == "abandon":
            result = service.abandon(args.proposal, binding=args.binding, actor=actor)
        elif args.command == "inspect":
            result = service.inspect(args.proposal)
        elif args.command == "approve":
            result = service.approve(args.proposal, binding=args.binding, actor=actor)
        elif args.command == "execute":
            result = service.execute(args.proposal, binding=args.binding, actor=actor)
        else:
            service.notify(args.proposal, jira=jira_adapter(), actor=actor)
            result = service.inspect(args.proposal)
        print(json.dumps(result, sort_keys=True, indent=2))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Never print external exception bodies, authorization headers or keys.
        print(f"Phase B/C command failed ({type(exc).__name__}); inspect protected audit state.", file=sys.stderr)
        raise SystemExit(1)