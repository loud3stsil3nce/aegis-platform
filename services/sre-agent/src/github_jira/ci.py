"""Phase C: bounded GET-only exact-PR verification and durable Jira feedback.

The manifest is an operator-owned read-only export, never Jira/model input.
This module has no proposal approval, GitHub mutation or deployment capability.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .github import GitHubReadError

REPOSITORY = "loud3stsil3nce/aegis-platform"
SHA = re.compile(r"[a-f0-9]{40}")
HASH = re.compile(r"[a-f0-9]{64}")
ID = re.compile(r"[a-f0-9-]{36}")
ISSUE = re.compile(r"[A-Z][A-Z0-9_]{1,20}-[1-9][0-9]*")
LABELS = {
    "CI_VERIFIED": "aegis:ci-verified", "CI_FAILED": "aegis:ci-failed",
    "CI_PENDING": "aegis:ci-pending", "REVIEW_REQUIRED": "aegis:review-required",
    "ABANDONED": "aegis:proposal-abandoned",
}


def encoded(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def validate_manifest(value: Any) -> list[dict]:
    if not isinstance(value, dict) or set(value) != {"version", "proposals"} or value["version"] != 1:
        raise ValueError("unsupported CI manifest")
    records = value["proposals"]
    if not isinstance(records, list) or len(records) > 100:
        raise ValueError("CI manifest exceeds proposal budget")
    seen, active = set(), set()
    for row in records:
        if not isinstance(row, dict) or set(row) != {
            "proposal_id", "issue_key", "repository", "binding_sha256", "base_sha",
            "base_branch", "branch", "lifecycle_state", "result", "files",
        }:
            raise ValueError("invalid CI manifest fields")
        if (
            not ID.fullmatch(row["proposal_id"]) or row["proposal_id"] in seen
            or not ISSUE.fullmatch(row["issue_key"]) or row["repository"] != REPOSITORY
            or not HASH.fullmatch(row["binding_sha256"]) or not SHA.fullmatch(row["base_sha"])
            or row["base_branch"] != "main"
            or not re.fullmatch(r"aegis/jira-[a-z0-9_-]+-[a-f0-9]{12}(?:-[a-f0-9]{12})?", row["branch"])
            or row["lifecycle_state"] not in {"ACTIVE", "ABANDONED"}
        ):
            raise ValueError("invalid CI manifest binding")
        seen.add(row["proposal_id"])
        if row["lifecycle_state"] == "ACTIVE":
            if row["issue_key"] in active:
                raise ValueError("multiple active proposals for an issue")
            active.add(row["issue_key"])
        files = row["files"]
        if not isinstance(files, dict) or not 1 <= len(files) <= 20:
            raise ValueError("invalid CI file manifest")
        for path, sha in files.items():
            if (
                not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+){1,7}", path)
                or any(p in {".", "..", ".git", ".github"} for p in path.split("/"))
                or not SHA.fullmatch(sha)
            ):
                raise ValueError("invalid CI file binding")
        result = row["result"]
        if result is None and row["lifecycle_state"] == "ABANDONED":
            continue
        if not isinstance(result, dict) or set(result) != {
            "repository", "branch", "commit_sha", "pull_request_number", "pull_request_url",
        }:
            raise ValueError("invalid CI PR result")
        number = result["pull_request_number"]
        if (
            result["repository"] != REPOSITORY or result["branch"] != row["branch"]
            or not SHA.fullmatch(result["commit_sha"]) or type(number) is not int or number <= 0
            or result["pull_request_url"] != f"https://github.com/{REPOSITORY}/pull/{number}"
        ):
            raise ValueError("invalid CI PR target")
    return records


def read_manifest(path: str) -> list[dict]:
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise ValueError("CI manifest must be a regular operator-owned mount")
    if target.stat().st_mode & 0o022:
        raise ValueError("CI manifest must not be group/world writable")
    with target.open("rb") as handle:
        raw = handle.read(262_145)
    if len(raw) > 262_144:
        raise ValueError("CI manifest exceeds byte budget")
    return validate_manifest(json.loads(raw))


def observe(github: Any, row: dict) -> dict:
    """At most five bounded JSON reads; no external names/logs/URLs are copied."""
    if row["lifecycle_state"] == "ABANDONED":
        return {"state": "ABANDONED", "reason": "Authorization retired; remote PR/branch preserved."}
    repository = row["repository"]
    root = github._root(repository)
    result = row["result"]
    number, head = result["pull_request_number"], result["commit_sha"]
    pr_path = f"{root}/pulls/{number}"

    def checked_pr(pr):
        if (
            pr.get("number") != number or pr.get("state") != "open"
            or pr.get("draft") is not True or pr.get("merged") is not False
            or pr.get("html_url") != result["pull_request_url"]
            or pr.get("head", {}).get("sha") != head
            or pr.get("head", {}).get("ref") != row["branch"]
            or pr.get("head", {}).get("repo", {}).get("full_name") != repository
            or pr.get("base", {}).get("sha") != row["base_sha"]
            or pr.get("base", {}).get("ref") != "main"
            or pr.get("base", {}).get("repo", {}).get("full_name") != repository
        ):
            raise GitHubReadError("PR identity, lifecycle or exact head/base changed")

    checked_pr(github._json(repository, pr_path))
    commit = github._json(repository, f"{root}/commits/{head}?per_page=100")
    files = commit.get("files")
    if (
        commit.get("sha") != head
        or [p.get("sha") for p in commit.get("parents", [])] != [row["base_sha"]]
        or not isinstance(files, list) or len(files) != len(row["files"])
        or len({f.get("filename") for f in files}) != len(files)
        or {f.get("filename"): f.get("sha") for f in files} != row["files"]
        or any(f.get("status") not in {"added", "modified"} for f in files)
    ):
        raise GitHubReadError("exact approved parent or file hashes differ")
    checks = github._json(repository, f"{root}/commits/{head}/check-runs?filter=latest&per_page=100")
    statuses = github._json(repository, f"{root}/commits/{head}/status?per_page=100")
    runs, contexts = checks.get("check_runs"), statuses.get("statuses")
    if (
        not isinstance(runs, list) or not isinstance(contexts, list)
        or type(checks.get("total_count")) is not int or checks["total_count"] != len(runs)
        or type(statuses.get("total_count")) is not int or statuses["total_count"] != len(contexts)
        or len(runs) >= 100 or len(contexts) >= 100 or statuses.get("sha") != head
    ):
        raise GitHubReadError("CI evidence is missing or truncated")
    counts = {"passed": 0, "failed": 0, "pending": 0, "other": 0}
    evidence = []
    for check in runs:
        if (
            type(check.get("id")) is not int or check["id"] <= 0
            or check.get("head_sha") != head
            or check.get("status") not in {"queued", "in_progress", "completed", "waiting", "pending", "requested"}
        ):
            raise GitHubReadError("check identity or status is invalid")
        status, conclusion = check["status"], check.get("conclusion")
        if status != "completed":
            category = "pending"
        elif conclusion == "success":
            category = "passed"
        elif conclusion in {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}:
            category = "failed"
        else:
            category = "other"
        counts[category] += 1
        evidence.append(("check", check["id"], category))
    for context in contexts:
        state = context.get("state")
        if type(context.get("id")) is not int or context["id"] <= 0 or state not in {"success", "failure", "error", "pending"}:
            raise GitHubReadError("commit status evidence is invalid")
        category = {"success": "passed", "failure": "failed", "error": "failed", "pending": "pending"}[state]
        counts[category] += 1
        evidence.append(("status", context["id"], category))
    # A head/base change during collection invalidates all preceding evidence.
    checked_pr(github._json(repository, pr_path))
    if counts["failed"]:
        state = "CI_FAILED"
    elif counts["other"]:
        state = "REVIEW_REQUIRED"
    elif counts["pending"] or not evidence:
        state = "CI_PENDING"
    else:
        state = "CI_VERIFIED"
    return {"state": state, "counts": counts, "evidence": sorted(evidence)}


class CIFeedback:
    """One proposal per tick, fair rotation; one durable claim per state change.

    A SENDING record blocks subsequent delivery for that issue on ambiguous
    Jira failure. It is deliberately not reset/retried without reconciliation.
    """
    def __init__(self, path: str | Path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS ci_tracking (
                proposal_id TEXT PRIMARY KEY, binding TEXT NOT NULL,
                identity TEXT NOT NULL, abandoned INTEGER NOT NULL DEFAULT 0,
                last_checked TEXT NOT NULL DEFAULT '', last_payload TEXT
            );
            CREATE TABLE IF NOT EXISTS ci_deliveries (
                id INTEGER PRIMARY KEY, proposal_id TEXT NOT NULL, issue_key TEXT NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT 'sre-agent-ci'
            );
        """)

    def close(self):
        self.db.close()

    def poll(self, records: list[dict], github: Any, jira: Any) -> str | None:
        validate_manifest({"version": 1, "proposals": records})
        if not records:
            return None
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for row in records:
                identity = encoded({k: v for k, v in row.items() if k != "lifecycle_state"})
                existing = self.db.execute("SELECT * FROM ci_tracking WHERE proposal_id=?", (row["proposal_id"],)).fetchone()
                abandoned = row["lifecycle_state"] == "ABANDONED"
                if existing and (existing["identity"] != identity or (existing["abandoned"] and not abandoned)):
                    raise ValueError("CI manifest rewrote a bound proposal or revived abandonment")
                self.db.execute(
                    "INSERT INTO ci_tracking(proposal_id,binding,identity,abandoned) VALUES(?,?,?,?) "
                    "ON CONFLICT(proposal_id) DO UPDATE SET abandoned=excluded.abandoned",
                    (row["proposal_id"], row["binding_sha256"], identity, abandoned),
                )
            by_id = {r["proposal_id"]: r for r in records}
            candidates = self.db.execute("SELECT * FROM ci_tracking ORDER BY last_checked,proposal_id").fetchall()
            selected = next(r for r in candidates if r["proposal_id"] in by_id)
            row = by_id[selected["proposal_id"]]
            checked_at = datetime.now(timezone.utc).isoformat()
            self.db.execute("UPDATE ci_tracking SET last_checked=? WHERE proposal_id=?",
                            (checked_at, row["proposal_id"]))
        try:
            payload = observe(github, row)
        except Exception:
            # No raw exception bodies, logs, check names or external URLs.
            payload = {"state": "REVIEW_REQUIRED", "reason": "Exact PR/CI evidence unavailable or changed; human inspection required."}
        serialized = encoded(payload)
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            current = self.db.execute("SELECT * FROM ci_tracking WHERE proposal_id=?", (row["proposal_id"],)).fetchone()
            if current["last_checked"] != checked_at or bool(current["abandoned"]) != (row["lifecycle_state"] == "ABANDONED"):
                return "SUPERSEDED"
            if current["last_payload"] == serialized:
                return payload["state"]
            if self.db.execute("SELECT 1 FROM ci_deliveries WHERE issue_key=? AND status='SENDING'", (row["issue_key"],)).fetchone():
                return "DELIVERY_BLOCKED"
            cursor = self.db.execute(
                "INSERT INTO ci_deliveries(proposal_id,issue_key,payload,status,created_at) VALUES(?,?,?,'SENDING',?)",
                (row["proposal_id"], row["issue_key"], serialized, datetime.now(timezone.utc).isoformat()),
            )
            delivery_id = cursor.lastrowid
        state = payload["state"]
        result = row["result"]
        lines = [
            f"Aegis CI feedback: {state}",
            f"Proposal/audit ID: {row['proposal_id']}",
            f"Binding SHA-256: {row['binding_sha256']}",
            f"CI delivery ID: {delivery_id}",
        ]
        if result:
            lines += [f"Draft PR: {result['pull_request_url']}", f"Exact head: {result['commit_sha']}",
                      f"Checks: {result['pull_request_url']}/checks",
                      f"Commit status: https://github.com/{REPOSITORY}/commit/{result['commit_sha']}/checks"]
        if "counts" in payload:
            lines.append("Observed CI: " + ", ".join(f"{k}={v}" for k, v in payload["counts"].items()))
        if "reason" in payload:
            lines.append(payload["reason"])
        lines.append({
            "CI_VERIFIED": "Observed checks passed at this exact head. Human review is still required; this is not merge authorization or proof of required-check coverage.",
            "CI_FAILED": "CI failed. Human review required; abandon and prepare a fresh independently approved proposal if another fix is needed.",
            "CI_PENDING": "CI is pending or no checks have reported. No success claim is made.",
            "REVIEW_REQUIRED": "Do not act on incomplete, stale, skipped or unknown CI evidence. Request human review.",
            "ABANDONED": "Do not execute this proposal. Any retry needs a new ID, branch, snapshot and separate approval.",
        }[state])
        lines.append("No GitHub mutation, merge, workflow dispatch or deployment was performed by this watcher.")
        label = LABELS[state]
        if state == "ABANDONED" and any(r["issue_key"] == row["issue_key"] and r["lifecycle_state"] == "ACTIVE" for r in records):
            label = None  # Historical notice must not replace the successor's state.
        jira.add_ci_update(row["issue_key"], "\n".join(lines), label)
        with self.lock, self.db:
            self.db.execute("UPDATE ci_deliveries SET status='SENT' WHERE id=?", (delivery_id,))
            self.db.execute("UPDATE ci_tracking SET last_payload=? WHERE proposal_id=?", (serialized, row["proposal_id"]))
        return state