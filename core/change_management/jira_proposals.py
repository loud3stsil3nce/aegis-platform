"""Operator-only Jira change proposals. No model-facing execution surface.

Jira labels request preparation, never approval. File snapshots are read at an
exact Git tree; the review diff is derived from those bytes, not caller text.
The existing durable change store remains the one-time consumption boundary.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from .deployment_auth import DeploymentActor
from .github_adapter import GitHubChangeAdapter, content_manifest_sha256
from .policy import ChangePolicy
from .proposals import ChangeApprovalError, ChangeProposalStore

REPOSITORY = "loud3stsil3nce/aegis-platform"
REQUEST_LABEL = "aegis:github-proposal"
MAX_SNAPSHOT_BYTES = 131_072
ROLLBACK = "On failure inspect the exact proposal branch; never reset the base or retry writes."
IMMUTABLE_FIELDS = (
    "proposal_id", "repository", "base_branch", "base_sha", "proposed_branch",
    "paths", "additions", "deletions", "diff_bytes", "diff_sha256",
    "content_manifest_sha256", "requested_by", "policy_version", "created_at", "expires_at",
)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def require_role(actor: DeploymentActor, role: str) -> None:
    if not actor.actor_id.strip() or len(actor.actor_id.encode()) > 200 or role not in actor.roles:
        raise PermissionError("operator role is not authorized")


def snapshot_text(files: dict[str, bytes], *, allow_missing_final_newline: bool = False) -> dict[str, str]:
    if not files or sum(len(value) for value in files.values()) > MAX_SNAPSHOT_BYTES:
        raise ChangeApprovalError("snapshot is empty or exceeds the byte budget")
    result = {}
    for path, content in files.items():
        if (
            not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+){1,7}", path)
            or any(part in {".", "..", ".git", ".github"} for part in path.split("/"))
        ):
            raise ChangeApprovalError("snapshot path is not a normalized permitted file")
        text = content.decode("utf-8")
        if "\x00" in text or "\r" in text:
            raise ChangeApprovalError("only UTF-8 LF text is supported")
        if text and not text.endswith("\n"):
            if not allow_missing_final_newline:
                raise ChangeApprovalError("only UTF-8 LF text with a final newline is supported")
        result[path] = text
    return result


def review_diff(before: dict[str, str | None], after: dict[str, str]) -> str:
    pieces = []
    for path, text in sorted(after.items()):
        old = before[path]
        if old == text:
            raise ChangeApprovalError("unchanged files must not be included")
        pieces.append(f"diff --git a/{path} b/{path}\n")
        # Empty added files need an explicit header; difflib emits no hunk.
        if old is None and text == "":
            pieces.append(f"new file mode 100644\n--- /dev/null\n+++ b/{path}\n")
        else:
            pieces.extend(difflib.unified_diff(
                (old or "").splitlines(keepends=True), text.splitlines(keepends=True),
                fromfile="/dev/null" if old is None else f"a/{path}", tofile=f"b/{path}",
            ))
    return "".join(pieces)


class JiraProposalService:
    def __init__(self, store: ChangeProposalStore, policy: ChangePolicy, adapter: GitHubChangeAdapter):
        if not policy.repositories or policy.base_branches != frozenset({"main"}):
            raise ChangeApprovalError("Phase B requires an allowlisted repository set and main base")
        self.store, self.policy, self.adapter = store, policy, adapter
        with store._lock, store.connection:
            store.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jira_change_snapshots (
                    proposal_id TEXT PRIMARY KEY, issue_key TEXT NOT NULL,
                    payload TEXT NOT NULL, binding_sha256 TEXT NOT NULL,
                    approval_sha256 TEXT, approval_actor TEXT, result_json TEXT,
                    notification_state TEXT NOT NULL DEFAULT 'PENDING'
                );
                """
            )
        # Migrate the Phase B one-issue/one-snapshot constraint without changing
        # any bound payloads, receipts, results or proposal IDs.
        with store._lock, store.connection:
            store.connection.execute("BEGIN IMMEDIATE")
            columns = {r["name"] for r in store.connection.execute("PRAGMA table_info(jira_change_snapshots)")}
            if "lifecycle_state" not in columns:
                store.connection.execute(
                    "CREATE TABLE jira_change_snapshots_c ("
                    "proposal_id TEXT PRIMARY KEY, issue_key TEXT NOT NULL, payload TEXT NOT NULL,"
                    "binding_sha256 TEXT NOT NULL, approval_sha256 TEXT, approval_actor TEXT,"
                    "result_json TEXT, notification_state TEXT NOT NULL DEFAULT 'PENDING',"
                    "lifecycle_state TEXT NOT NULL DEFAULT 'ACTIVE', retry_of TEXT UNIQUE)"
                )
                store.connection.execute(
                    "INSERT INTO jira_change_snapshots_c "
                    "(proposal_id,issue_key,payload,binding_sha256,approval_sha256,approval_actor,result_json,notification_state) "
                    "SELECT proposal_id,issue_key,payload,binding_sha256,approval_sha256,approval_actor,result_json,notification_state "
                    "FROM jira_change_snapshots"
                )
                store.connection.execute("DROP TABLE jira_change_snapshots")
                store.connection.execute("ALTER TABLE jira_change_snapshots_c RENAME TO jira_change_snapshots")
            if "merge_commit_sha" not in columns:
                store.connection.execute("ALTER TABLE jira_change_snapshots ADD COLUMN merge_commit_sha TEXT")
            store.connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS jira_one_active_proposal "
                "ON jira_change_snapshots(issue_key) WHERE lifecycle_state='ACTIVE'"
            )

    def prepare(
        self, *, issue_key: str, fingerprint: str, labels: list[str],
        repository: str, base_sha: str, files: dict[str, bytes],
        actor: DeploymentActor, ttl_seconds: int = 900,
        retry_of: str | None = None, retry_binding: str | None = None,
    ) -> dict[str, Any]:
        require_role(actor, "requester")
        if (
            REQUEST_LABEL not in labels or repository not in self.policy.repositories
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,20}-[1-9][0-9]*", issue_key)
            or not re.fullmatch(r"[a-f0-9]{64}", fingerprint)
            or not re.fullmatch(r"[a-f0-9]{40}", base_sha)
        ):
            raise ChangeApprovalError("an exact labeled Jira incident is required")
        after = snapshot_text(files)
        branch = f"aegis/jira-{issue_key.lower()}-{fingerprint[:12]}"
        if retry_of:
            previous, _ = self._checked(retry_of, retry_binding)
            if (
                previous["lifecycle_state"] != "ABANDONED"
                or previous["snapshot"]["issue_key"] != issue_key
                or previous["snapshot"]["fingerprint"] != fingerprint
            ):
                raise ChangeApprovalError("retry requires the exact abandoned incident proposal")
            branch += "-" + uuid.uuid4().hex[:12]
        elif retry_binding is not None:
            raise ChangeApprovalError("retry binding requires a predecessor")
        # Validate target and budgets before making any GitHub read.
        probe = self.policy.validate(
            repository=repository, base_branch="main", proposed_branch=branch,
            unified_diff=review_diff(dict.fromkeys(after), after),
        )
        with self.store._lock:
            if self.store.connection.execute(
                "SELECT 1 FROM jira_change_snapshots WHERE issue_key=? "
                "AND (lifecycle_state='ACTIVE' OR ? IS NULL OR retry_of=?)",
                (issue_key, retry_of, retry_of),
            ).fetchone():
                raise ChangeApprovalError("issue already has a proposal; inspect it, never silently rewrite")
            before_bytes = self.adapter.read_snapshot(probe, expected_base_sha=base_sha)
            before = {
                path: None if value is None else snapshot_text({path: value}, allow_missing_final_newline=True)[path]
                for path, value in before_bytes.items()
            }
            diff = review_diff(before, after)
            proposal = self.store.propose(
                policy=self.policy, repository=repository, base_branch="main",
                base_sha=base_sha, proposed_branch=branch, unified_diff=diff,
                file_contents=files, requested_by=actor.actor_id, ttl_seconds=ttl_seconds,
            )
            payload = {
                "issue_key": issue_key, "fingerprint": fingerprint,
                "before": before, "after": after, "diff": diff,
                "rollback": {"base_sha": base_sha, "branch": branch, "instructions": ROLLBACK},
            }
            if retry_of:
                payload["retry_of"] = retry_of
                payload["retry_binding"] = retry_binding
            binding = digest({"proposal": {key: proposal[key] for key in IMMUTABLE_FIELDS}, "snapshot": payload})
            with self.store.connection:
                self.store.connection.execute(
                    "INSERT INTO jira_change_snapshots(proposal_id,issue_key,payload,binding_sha256,retry_of) VALUES(?,?,?,?,?)",
                    (proposal["proposal_id"], issue_key, canonical(payload), binding, retry_of),
                )
                if retry_of:
                    self.store._event(proposal["proposal_id"], repository, "RETRY_PREPARED",
                                      actor.actor_id, f"new approval required; predecessor {retry_of}")
        return self.inspect(proposal["proposal_id"])

    def inspect(self, proposal_id: str) -> dict[str, Any]:
        with self.store._lock:
            proposal = self.store.get(proposal_id)
            row = self.store.connection.execute(
                "SELECT * FROM jira_change_snapshots WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise ChangeApprovalError("proposal has no bound Jira snapshot")
            payload = json.loads(row["payload"])
            binding = digest({"proposal": {key: proposal[key] for key in IMMUTABLE_FIELDS}, "snapshot": payload})
            if (
                binding != row["binding_sha256"] or payload["issue_key"] != row["issue_key"]
                or payload.get("retry_of") != row["retry_of"]
            ):
                raise ChangeApprovalError("proposal target, content, rollback, or expiry binding changed")
            files = {path: text.encode() for path, text in payload["after"].items()}
            snapshot_text(files)
            change = self.policy.validate(
                repository=proposal["repository"], base_branch=proposal["base_branch"],
                proposed_branch=proposal["proposed_branch"], unified_diff=payload["diff"],
            )
            if (
                review_diff(payload["before"], payload["after"]) != payload["diff"]
                or change.diff_sha256 != proposal["diff_sha256"]
                or list(change.paths) != proposal["paths"]
                or content_manifest_sha256(files) != proposal["content_manifest_sha256"]
            ):
                raise ChangeApprovalError("review diff and exact file snapshot disagree")
            return {
                "proposal": proposal, "snapshot": payload, "binding_sha256": binding,
                "approval_sha256": row["approval_sha256"], "approval_actor": row["approval_actor"],
                "result": json.loads(row["result_json"]) if row["result_json"] else None,
                "notification_state": row["notification_state"],
                "lifecycle_state": row["lifecycle_state"],
                "merge_commit_sha": row["merge_commit_sha"] if "merge_commit_sha" in row.keys() else None,
            }

    def _checked(self, proposal_id: str, binding: str):
        record = self.inspect(proposal_id)
        if record["binding_sha256"] != binding:
            raise ChangeApprovalError("operator must supply the exact reviewed binding hash")
        proposal, payload = record["proposal"], record["snapshot"]
        change = self.policy.validate(
            repository=proposal["repository"], base_branch=proposal["base_branch"],
            proposed_branch=proposal["proposed_branch"], unified_diff=payload["diff"],
        )
        return record, change

    def abandon(self, proposal_id: str, *, binding: str, actor: DeploymentActor) -> dict[str, Any]:
        """Retire local authorization only; never close/delete a remote PR/branch."""
        require_role(actor, "approver")
        with self.store._lock, self.store.connection:
            self.store.connection.execute("BEGIN IMMEDIATE")
            record, _ = self._checked(proposal_id, binding)
            if record["lifecycle_state"] != "ACTIVE":
                raise ChangeApprovalError("proposal already abandoned")
            # A dispatched write without a durable result may still be in flight.
            if record["proposal"]["status"] == "CONSUMED" and record["result"] is None:
                raise ChangeApprovalError("indeterminate execution requires manual reconciliation; retry blocked")
            self.store.connection.execute(
                "UPDATE change_proposals SET status='ABANDONED' WHERE proposal_id=?", (proposal_id,),
            )
            self.store.connection.execute(
                "UPDATE jira_change_snapshots SET lifecycle_state='ABANDONED' WHERE proposal_id=?", (proposal_id,),
            )
            repo = record["proposal"]["repository"]
            self.store._event(proposal_id, repo, "ABANDONED", actor.actor_id,
                              "authorization retired; remote PR and branch preserved; retry requires fresh approval")
        return self.inspect(proposal_id)

    def ci_manifest(self, *, actor: DeploymentActor) -> dict[str, Any]:
        """Export only trusted machine metadata, not code or operator credentials."""
        require_role(actor, "executor")
        with self.store._lock:
            ids = self.store.connection.execute(
                "SELECT proposal_id FROM jira_change_snapshots ORDER BY rowid LIMIT 101"
            ).fetchall()
            if len(ids) > 100:
                raise ChangeApprovalError("CI manifest exceeds the 100-proposal budget; archive under review")
            records = []
            for row in ids:
                record = self.inspect(row["proposal_id"])
                proposal, snapshot, result = record["proposal"], record["snapshot"], record["result"]
                if result is None and record["lifecycle_state"] != "ABANDONED":
                    continue
                records.append({
                    "proposal_id": proposal["proposal_id"], "issue_key": snapshot["issue_key"],
                    "repository": proposal["repository"], "binding_sha256": record["binding_sha256"],
                    "base_sha": proposal["base_sha"], "base_branch": proposal["base_branch"],
                    "branch": proposal["proposed_branch"], "lifecycle_state": record["lifecycle_state"],
                    "result": result,
                    "files": {path: hashlib.sha1(
                        f"blob {len(text.encode())}".encode() + bytes([0]) + text.encode()
                    ).hexdigest() for path, text in snapshot["after"].items()},
                })
        return {"version": 1, "proposals": records}

    def approve(self, proposal_id: str, *, binding: str, actor: DeploymentActor) -> dict[str, Any]:
        require_role(actor, "approver")
        with self.store._lock, self.store.connection:
            self.store.connection.execute("BEGIN IMMEDIATE")
            record, change = self._checked(proposal_id, binding)
            proposal = record["proposal"]
            if record["lifecycle_state"] != "ACTIVE":
                raise ChangeApprovalError("abandoned proposal cannot be approved")
            self.adapter.preflight(change, expected_base_sha=proposal["base_sha"])
            approved = self.store.approve(
                proposal_id, approved_by=actor.actor_id, current_base_sha=proposal["base_sha"],
            )
            receipt = digest({"binding": binding, "actor": actor.actor_id, "decided_at": approved["decided_at"]})
            with self.store.connection:
                self.store.connection.execute(
                    "UPDATE jira_change_snapshots SET approval_sha256=?,approval_actor=? WHERE proposal_id=?",
                    (receipt, actor.actor_id, proposal_id),
                )
        return self.inspect(proposal_id)

    def _before_write(self, proposal_id: str, binding: str, receipt: str) -> None:
        record, _ = self._checked(proposal_id, binding)
        proposal = record["proposal"]
        if (
            proposal["status"] != "CONSUMED" or record["lifecycle_state"] != "ACTIVE"
            or datetime.fromisoformat(proposal["expires_at"]) <= datetime.now(timezone.utc)
            or record["approval_sha256"] != receipt
            or digest({"binding": binding, "actor": proposal["approved_by"],
                       "decided_at": proposal["decided_at"]}) != receipt
        ):
            raise ChangeApprovalError("approval changed or expired during GitHub preflight")

    def execute(self, proposal_id: str, *, binding: str, actor: DeploymentActor) -> dict[str, Any]:
        require_role(actor, "executor")
        try:
            with self.store._lock:
                record, change = self._checked(proposal_id, binding)
                proposal, payload = record["proposal"], record["snapshot"]
                receipt = digest({
                    "binding": binding, "actor": proposal["approved_by"], "decided_at": proposal["decided_at"],
                })
                if (
                    proposal["status"] != "APPROVED" or record["lifecycle_state"] != "ACTIVE" or not proposal["approved_by"]
                    or proposal["approved_by"] == proposal["requested_by"]
                    or record["approval_actor"] != proposal["approved_by"]
                    or record["approval_sha256"] != receipt
                ):
                    raise ChangeApprovalError("exact independent approval is missing or already consumed")
                self.adapter.preflight(change, expected_base_sha=proposal["base_sha"])
                self.store.consume(
                    proposal_id, diff_sha256=change.diff_sha256,
                    content_manifest_sha256=proposal["content_manifest_sha256"],
                    current_base_sha=proposal["base_sha"], actor=actor.actor_id,
                )
        except Exception:
            # Denials must commit separately from failed validation transactions.
            with self.store._lock, self.store.connection:
                try:
                    repo = self.store.get(proposal_id)["repository"]
                except Exception:
                    repo = "unknown"
                self.store._event(proposal_id, repo, "EXECUTION_DENIED", actor.actor_id, "Phase B exact binding rejected")
            raise

        # A consumed proposal is never retried, even if the process dies or a
        # GitHub response is lost. Jira notification is a separate operation.
        try:
            repo = proposal["repository"]
            result = self.adapter.create_pull_request(
                change, expected_base_sha=proposal["base_sha"],
                files={path: text.encode() for path, text in payload["after"].items()},
                rollback_on_pr_failure=False,
                before_write=lambda: self._before_write(proposal_id, binding, receipt),
                commit_message=f"{payload['issue_key']}: approved proposal {proposal_id}",
                title=f"{payload['issue_key']}: approved change proposal",
                body=f"Proposal: {proposal_id}\nBinding SHA-256: {binding}\nIncident: {payload['fingerprint']}\n"
                     "Draft only. Human review and merge are separate; no deployment is authorized.",
            )
            expected_url = f"https://github.com/{repo}/pull/{result.pull_request_number}"
            if (
                result.repository != repo or result.branch != change.proposed_branch
                or not re.fullmatch(r"[a-f0-9]{40}", result.commit_sha)
                or type(result.pull_request_number) is not int or result.pull_request_number <= 0
                or result.pull_request_url != expected_url
            ):
                raise ChangeApprovalError("GitHub returned an unexpected proposal result; manual recovery required")
            with self.store._lock, self.store.connection:
                self.store.connection.execute(
                    "UPDATE jira_change_snapshots SET result_json=? WHERE proposal_id=?",
                    (canonical(asdict(result)), proposal_id),
                )
                self.store._event(
                    proposal_id, repo, "SUCCESS", actor.actor_id, canonical(asdict(result)),
                )
        except Exception:
            self.store.record_execution(
                proposal_id, status="FAILED", actor=actor.actor_id,
                detail="Phase B execution failed or is indeterminate; inspect audit and branch, never retry writes",
            )
            raise
        return self.inspect(proposal_id)

    def notify(self, proposal_id: str, *, jira: Any, actor: DeploymentActor) -> None:
        """At-most-once Jira write claim. Ambiguous responses need human inspection."""
        require_role(actor, "executor")
        record = self.inspect(proposal_id)
        result = record["result"]
        if result is None or record["lifecycle_state"] != "ACTIVE":
            raise ChangeApprovalError("no active draft PR result to notify")
        with self.store._lock, self.store.connection:
            claimed = self.store.connection.execute(
                "UPDATE jira_change_snapshots SET notification_state='SENDING' "
                "WHERE proposal_id=? AND notification_state='PENDING'", (proposal_id,),
            ).rowcount
        if not claimed:
            raise ChangeApprovalError("notification already sent or indeterminate; do not repeat automatically")
        jira.add_proposal_update(
            record["snapshot"]["issue_key"],
            f"Aegis draft PR: {result['pull_request_url']}\nCommit: {result['commit_sha']}\n"
            f"Proposal/audit ID: {proposal_id}\nBinding SHA-256: {record['binding_sha256']}\n"
            "Human review/merge required. No merge or deployment was performed.",
            "aegis:pr-open",
        )
        with self.store._lock, self.store.connection:
            self.store.connection.execute(
                "UPDATE jira_change_snapshots SET notification_state='SENT' WHERE proposal_id=?", (proposal_id,),
            )
            repo = record["proposal"]["repository"]
            self.store._event(proposal_id, repo, "JIRA_NOTIFIED", actor.actor_id, "draft PR linked to bound issue")

    def merge_proposal(
        self, proposal_id: str, *, binding: str, actor: DeploymentActor, github_api: Any = None,
    ) -> dict[str, Any]:
        require_role(actor, "merge-approver")
        record = self.inspect(proposal_id)
        if record["binding_sha256"] != binding:
            raise ChangeApprovalError("operator must supply the exact reviewed binding hash")
        proposal = record["proposal"]
        if actor.actor_id == proposal["requested_by"]:
            raise ChangeApprovalError("separation of duties: merge approver must differ from requester")
        if record["result"] is None:
            raise ChangeApprovalError("cannot merge proposal without an active PR execution result")
        if record["lifecycle_state"] == "MERGED":
            raise ChangeApprovalError("proposal has already been merged")
        if record["lifecycle_state"] != "ACTIVE":
            raise ChangeApprovalError("cannot merge inactive or abandoned proposal")

        repo = proposal["repository"]
        pr_num = record["result"]["pull_request_number"]
        root = f"/repos/{repo}"
        api = github_api
        if api is None:
            api = self.adapter.token_provider.issue(repo, write=True)

        # 1. Check PR state and mergeable status
        pr = api.request("GET", f"{root}/pulls/{pr_num}")
        if pr.get("state") != "open":
            raise ChangeApprovalError("PR is not open on GitHub")
        if pr.get("mergeable") is False:
            raise ChangeApprovalError("PR has conflicts and is not mergeable")

        # 2. Check branch protection review rule (at least one approved review)
        reviews = api.request("GET", f"{root}/pulls/{pr_num}/reviews")
        approved_reviews = [r for r in reviews if r.get("state") == "APPROVED"]
        if not approved_reviews:
            raise ChangeApprovalError("PR must have an approving review before merge")

        # 3. Merge PR via GitHub API
        merge_payload = {
            "commit_title": f"{record['snapshot']['issue_key']}: merge proposal {proposal_id}",
            "commit_message": f"Proposal ID: {proposal_id}\nBinding SHA-256: {binding}\nApproved by: {actor.actor_id}\nDraft PR #{pr_num}",
            "merge_method": "squash",
        }
        merge_res = api.request("PUT", f"{root}/pulls/{pr_num}/merge", merge_payload)
        if not merge_res.get("merged"):
            raise ChangeApprovalError(f"GitHub merge failed: {merge_res.get('message', 'unknown')}")

        merge_sha = merge_res["sha"]
        with self.store._lock, self.store.connection:
            self.store.connection.execute(
                "UPDATE jira_change_snapshots SET lifecycle_state='MERGED', merge_commit_sha=? WHERE proposal_id=?",
                (merge_sha, proposal_id),
            )
            self.store._event(
                proposal_id, repo, "MERGED", actor.actor_id,
                canonical({"pull_request_number": pr_num, "merge_commit_sha": merge_sha}),
            )
        return {
            "proposal_id": proposal_id,
            "pull_request_number": pr_num,
            "merged": True,
            "merge_commit_sha": merge_sha,
            "actor": actor.actor_id,
        }

    def notify_merge(self, proposal_id: str, *, jira: Any, actor: DeploymentActor) -> None:
        require_role(actor, "merge-approver")
        record = self.inspect(proposal_id)
        if record["lifecycle_state"] != "MERGED" or not record.get("merge_commit_sha"):
            raise ChangeApprovalError("proposal must be in MERGED state to notify")
        jira.add_proposal_update(
            record["snapshot"]["issue_key"],
            f"Aegis PR merged: {record['result']['pull_request_url']}\n"
            f"Merge commit: {record['merge_commit_sha']}\n"
            f"Proposal ID: {proposal_id}\n"
            f"Binding SHA-256: {record['binding_sha256']}\n"
            f"Approved by merge-approver: {actor.actor_id}\n"
            "Ready for governed immutable deployment.",
            "aegis:merged",
        )
        with self.store._lock, self.store.connection:
            repo = record["proposal"]["repository"]
            self.store._event(proposal_id, repo, "JIRA_MERGE_NOTIFIED", actor.actor_id, "PR merge posted to Jira")