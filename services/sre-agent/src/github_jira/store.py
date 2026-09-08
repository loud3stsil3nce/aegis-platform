"""Durable idempotency and incident state for the Phase A workflow."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from .models import FailureEvent, IncidentRecord


class IncidentStore:
    def __init__(self, path: Union[str, Path]):
        self.path = str(path)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS github_incidents (
                    fingerprint TEXT PRIMARY KEY,
                    repository TEXT NOT NULL,
                    workflow TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    commit_sha TEXT NOT NULL,
                    run_id INTEGER NOT NULL,
                    source_kind TEXT NOT NULL,
                    conclusion TEXT NOT NULL,
                    status TEXT NOT NULL,
                    issue_key TEXT UNIQUE,
                    event_count INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    diagnosed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS github_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    received_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS github_proposal_requests (
                    issue_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL, requested_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS github_jira_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    details TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _record(row: Optional[sqlite3.Row]) -> Optional[IncidentRecord]:
        if row is None:
            return None
        return IncidentRecord(
            fingerprint=row["fingerprint"],
            repository=row["repository"],
            workflow=row["workflow"],
            branch=row["branch"],
            commit_sha=row["commit_sha"],
            run_id=row["run_id"],
            source_kind=row["source_kind"],
            conclusion=row["conclusion"],
            status=row["status"],
            issue_key=row["issue_key"],
            event_count=row["event_count"],
        )

    def record_event(self, event: FailureEvent, delivery_id: str) -> tuple[IncidentRecord, bool]:
        """Atomically claim a delivery and upsert its stable incident fingerprint."""

        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT fingerprint FROM github_deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if prior:
                row = connection.execute(
                    "SELECT * FROM github_incidents WHERE fingerprint = ?", (prior["fingerprint"],)
                ).fetchone()
                if row is None:
                    raise RuntimeError("delivery references a missing incident")
                return self._record(row), True  # type: ignore[return-value]
            connection.execute(
                "INSERT INTO github_deliveries(delivery_id, fingerprint, received_at) VALUES (?, ?, ?)",
                (delivery_id, event.fingerprint, now),
            )
            connection.execute(
                """
                INSERT INTO github_incidents(
                    fingerprint, repository, workflow, branch, commit_sha, run_id,
                    source_kind, conclusion, status, issue_key, event_count,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'DETECTED', NULL, 1, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    event_count = event_count + 1,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    event.fingerprint, event.repository, event.workflow, event.branch,
                    event.commit_sha, event.run_id, event.source_kind, event.conclusion, now, now,
                ),
            )
            connection.execute(
                "INSERT INTO github_jira_audit(occurred_at, event_type, fingerprint, actor, details) VALUES (?, 'DETECTED', ?, 'github-intake', ?)",
                (now, event.fingerprint, f"delivery={delivery_id}"),
            )
            row = connection.execute(
                "SELECT * FROM github_incidents WHERE fingerprint = ?", (event.fingerprint,)
            ).fetchone()
            return self._record(row), False  # type: ignore[return-value]

    def attach_issue(self, fingerprint: str, issue_key: str) -> IncidentRecord:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT issue_key FROM github_incidents WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if row is None:
                raise KeyError("incident does not exist")
            if row["issue_key"] and row["issue_key"] != issue_key:
                raise RuntimeError("incident is already bound to a different Jira issue")
            connection.execute(
                "UPDATE github_incidents SET issue_key = ?, status = 'TRIAGED' WHERE fingerprint = ?",
                (issue_key, fingerprint),
            )
            connection.execute(
                "INSERT INTO github_jira_audit(occurred_at, event_type, fingerprint, actor, details) VALUES (?, 'TRIAGED', ?, 'jira-intake', ?)",
                (now, fingerprint, f"issue={issue_key}"),
            )
            updated = connection.execute(
                "SELECT * FROM github_incidents WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            return self._record(updated)  # type: ignore[return-value]

    def get_by_fingerprint(self, fingerprint: str) -> Optional[IncidentRecord]:
        with self._connect() as connection:
            return self._record(
                connection.execute(
                    "SELECT * FROM github_incidents WHERE fingerprint = ?", (fingerprint,)
                ).fetchone()
            )

    def get_by_issue(self, issue_key: str) -> Optional[IncidentRecord]:
        with self._connect() as connection:
            return self._record(
                connection.execute(
                    "SELECT * FROM github_incidents WHERE issue_key = ?", (issue_key,)
                ).fetchone()
            )

    def mark_diagnosed(self, fingerprint: str, evidence_hash: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE github_incidents SET status = 'DIAGNOSED', diagnosed_at = ? WHERE fingerprint = ?",
                (now, fingerprint),
            ).rowcount
            if changed != 1:
                raise KeyError("incident does not exist")
            connection.execute(
                "INSERT INTO github_jira_audit(occurred_at, event_type, fingerprint, actor, details) VALUES (?, 'DIAGNOSED', ?, 'sre-agent', ?)",
                (now, fingerprint, f"evidence_sha256={evidence_hash}"),
            )

    def claim_proposal_request(self, issue_key: str) -> bool:
        """Claim once before posting to Jira; crashes require manual reconciliation."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            incident = connection.execute(
                "SELECT fingerprint FROM github_incidents WHERE issue_key=?", (issue_key,)
            ).fetchone()
            if incident is None:
                raise KeyError("proposal requires a durable GitHub incident")
            claimed = connection.execute(
                "INSERT OR IGNORE INTO github_proposal_requests VALUES(?,?,'REQUESTED',?)",
                (issue_key, incident["fingerprint"], now),
            ).rowcount
            if claimed:
                connection.execute(
                    "INSERT INTO github_jira_audit(occurred_at,event_type,fingerprint,actor,details) "
                    "VALUES(?,'PROPOSAL_REQUESTED',?,'sre-agent',?)",
                    (now, incident["fingerprint"], f"issue={issue_key}; request only, no approval"),
                )
            return claimed == 1

    def proposal_requested(self, issue_key: str) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM github_proposal_requests WHERE issue_key=?", (issue_key,)
            ).fetchone() is not None

    def count_incidents(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM github_incidents").fetchone()[0])

    def audit_event_types(self, fingerprint: str) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_type FROM github_jira_audit WHERE fingerprint = ? ORDER BY id",
                (fingerprint,),
            ).fetchall()
            return tuple(row["event_type"] for row in rows)

    def integrity_check(self) -> str:
        with self._connect() as connection:
            return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
