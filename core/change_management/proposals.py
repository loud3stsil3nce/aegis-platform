"""Durable exact approvals for proposed Git changes; no GitHub mutation code."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .github_adapter import content_manifest_sha256
from .policy import ChangePolicy, ValidatedChange


POLICY_VERSION = "change-proposal-v1"
_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class ChangeApprovalError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class ChangeProposalStore:
    def __init__(self, path: str | Path):
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(Path(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS change_proposals (
              proposal_id TEXT PRIMARY KEY, repository TEXT NOT NULL,
              base_branch TEXT NOT NULL, base_sha TEXT NOT NULL,
              proposed_branch TEXT NOT NULL, paths_json TEXT NOT NULL,
              additions INTEGER NOT NULL, deletions INTEGER NOT NULL,
              diff_bytes INTEGER NOT NULL, diff_sha256 TEXT NOT NULL,
              content_manifest_sha256 TEXT NOT NULL,
              requested_by TEXT NOT NULL, approved_by TEXT,
              status TEXT NOT NULL, policy_version TEXT NOT NULL,
              created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
              decided_at TEXT, consumed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS change_events (
              event_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL,
              repository TEXT NOT NULL, status TEXT NOT NULL,
              actor TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def _event(self, proposal_id: str, repository: str, status: str, actor: str, detail: str) -> None:
        self.connection.execute(
            "INSERT INTO change_events VALUES(?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), proposal_id, repository, status, actor, detail[:1000], _iso(_now())),
        )

    def propose(
        self, *, policy: ChangePolicy, repository: str, base_branch: str,
        base_sha: str, proposed_branch: str, unified_diff: str,
        file_contents: dict[str, bytes], requested_by: str, ttl_seconds: int = 900,
    ) -> dict[str, Any]:
        if not requested_by:
            raise ChangeApprovalError("requesting actor is required")
        if not _COMMIT_SHA.fullmatch(base_sha):
            raise ChangeApprovalError("base commit SHA must be an exact lowercase hash")
        if not 30 <= ttl_seconds <= 3600:
            raise ChangeApprovalError("approval TTL must be between 30 and 3600 seconds")
        change = policy.validate(
            repository=repository, base_branch=base_branch,
            proposed_branch=proposed_branch, unified_diff=unified_diff,
        )
        if set(file_contents) != set(change.paths):
            raise ChangeApprovalError("file snapshot does not exactly match validated diff paths")
        content_hash = content_manifest_sha256(file_contents)
        proposal_id, now = str(uuid.uuid4()), _now()
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO change_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (proposal_id, change.repository, change.base_branch, base_sha,
                 change.proposed_branch, json.dumps(change.paths), change.additions,
                 change.deletions, change.diff_bytes, change.diff_sha256,
                 content_hash, requested_by, None, "PENDING", POLICY_VERSION, _iso(now),
                 _iso(now + timedelta(seconds=ttl_seconds)), None, None),
            )
            self._event(proposal_id, repository, "PROPOSED", requested_by, "exact change proposal created")
        return self.get(proposal_id)

    def get(self, proposal_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM change_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        if row is None:
            raise ChangeApprovalError("change proposal not found")
        result = dict(row)
        result["paths"] = json.loads(result.pop("paths_json"))
        return result

    def record_execution(self, proposal_id: str, *, status: str, actor: str, detail: str) -> None:
        if status not in {"SUCCESS", "FAILED"}:
            raise ChangeApprovalError("invalid change execution status")
        proposal = self.get(proposal_id)
        with self._lock, self.connection:
            self._event(proposal_id, proposal["repository"], status, actor, detail)

    def approve(self, proposal_id: str, *, approved_by: str, current_base_sha: str) -> dict[str, Any]:
        now, error = _now(), None
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT * FROM change_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise ChangeApprovalError("change proposal not found")
            if row["status"] != "PENDING":
                error = f"change proposal is already {row['status']}"
            elif datetime.fromisoformat(row["expires_at"]) <= now:
                self.connection.execute(
                    "UPDATE change_proposals SET status='EXPIRED',decided_at=? WHERE proposal_id=?",
                    (_iso(now), proposal_id),
                )
                self._event(proposal_id, row["repository"], "EXPIRED", approved_by, "change approval expired")
                error = "change proposal expired"
            elif row["requested_by"] == approved_by:
                error = "requester cannot approve their own change"
            elif row["base_sha"] != current_base_sha:
                self.connection.execute(
                    "UPDATE change_proposals SET status='STALE',decided_at=? WHERE proposal_id=?",
                    (_iso(now), proposal_id),
                )
                self._event(proposal_id, row["repository"], "STALE", approved_by, "base commit precondition changed")
                error = "base commit changed after proposal"
            else:
                self.connection.execute(
                    "UPDATE change_proposals SET status='APPROVED',approved_by=?,decided_at=? WHERE proposal_id=?",
                    (approved_by, _iso(now), proposal_id),
                )
                self._event(proposal_id, row["repository"], "APPROVED", approved_by, "exact change approved")
        if error:
            raise ChangeApprovalError(error)
        return self.get(proposal_id)

    def consume(
        self, proposal_id: str, *, diff_sha256: str,
        content_manifest_sha256: str, current_base_sha: str, actor: str,
    ) -> dict[str, Any]:
        now, denied = _now(), False
        with self._lock, self.connection:
            result = self.connection.execute(
                """UPDATE change_proposals SET status='CONSUMED',consumed_at=?
                   WHERE proposal_id=? AND status='APPROVED' AND expires_at>?
                     AND base_sha=? AND diff_sha256=?
                     AND content_manifest_sha256=? AND policy_version=?""",
                (_iso(now), proposal_id, _iso(now), current_base_sha, diff_sha256,
                 content_manifest_sha256, POLICY_VERSION),
            )
            if result.rowcount != 1:
                row = self.connection.execute(
                    "SELECT repository FROM change_proposals WHERE proposal_id=?", (proposal_id,)
                ).fetchone()
                if row:
                    self._event(proposal_id, row["repository"], "EXECUTION_DENIED", actor, "change execution rejected by policy")
                denied = True
            else:
                row = self.connection.execute(
                    "SELECT * FROM change_proposals WHERE proposal_id=?", (proposal_id,)
                ).fetchone()
                self._event(proposal_id, row["repository"], "CONSUMED", actor, "change approval consumed")
        if denied:
            raise ChangeApprovalError("change approval is expired, altered, stale, replayed, or invalid")
        return self.get(proposal_id)
