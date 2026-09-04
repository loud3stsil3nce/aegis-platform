"""Durable, single-use approvals for immutable deployment plans."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .deployment_policy import ImmutableDeploymentPlan


POLICY_VERSION = "deployment-proposal-v1"


class DeploymentApprovalError(ValueError):
    pass


def _require_actor(actor: str) -> None:
    if not isinstance(actor, str) or not actor.strip() or len(actor.encode()) > 200:
        raise DeploymentApprovalError("a bounded attributable actor is required")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class DeploymentProposalStore:
    def __init__(self, path: str | Path):
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(Path(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS deployment_proposals (
              proposal_id TEXT PRIMARY KEY, plugin_id TEXT NOT NULL,
              service TEXT NOT NULL, git_sha TEXT NOT NULL,
              image_reference TEXT NOT NULL, expected_current_image TEXT NOT NULL,
              rollback_image TEXT NOT NULL, health_timeout_seconds INTEGER NOT NULL,
              requested_by TEXT NOT NULL, approved_by TEXT, status TEXT NOT NULL,
              policy_version TEXT NOT NULL, created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL, decided_at TEXT, consumed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS deployment_events (
              event_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL,
              plugin_id TEXT NOT NULL, status TEXT NOT NULL,
              actor TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def _event(self, proposal_id: str, plugin_id: str, status: str, actor: str, detail: str) -> None:
        self.connection.execute(
            "INSERT INTO deployment_events VALUES(?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), proposal_id, plugin_id, status, actor, detail[:1000], _iso(_now())),
        )

    def propose(
        self, *, plan: ImmutableDeploymentPlan, requested_by: str,
        ttl_seconds: int = 900,
    ) -> dict[str, Any]:
        _require_actor(requested_by)
        if not 30 <= ttl_seconds <= 3600:
            raise DeploymentApprovalError("approval TTL must be between 30 and 3600 seconds")
        proposal_id, now = str(uuid.uuid4()), _now()
        values = asdict(plan)
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO deployment_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (proposal_id, values["plugin_id"], values["service"], values["git_sha"],
                 values["image_reference"], values["expected_current_image"],
                 values["rollback_image"], values["health_timeout_seconds"],
                 requested_by, None, "PENDING", POLICY_VERSION, _iso(now),
                 _iso(now + timedelta(seconds=ttl_seconds)), None, None),
            )
            self._event(proposal_id, plan.plugin_id, "PROPOSED", requested_by, "exact deployment proposed")
        return self.get(proposal_id)

    def get(self, proposal_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM deployment_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        if row is None:
            raise DeploymentApprovalError("deployment proposal not found")
        return dict(row)

    def approve(
        self, proposal_id: str, *, approved_by: str, current_image: str,
    ) -> dict[str, Any]:
        _require_actor(approved_by)
        now, error = _now(), None
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT * FROM deployment_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise DeploymentApprovalError("deployment proposal not found")
            if row["status"] != "PENDING":
                error = f"deployment proposal is already {row['status']}"
            elif datetime.fromisoformat(row["expires_at"]) <= now:
                self.connection.execute(
                    "UPDATE deployment_proposals SET status='EXPIRED',decided_at=? WHERE proposal_id=?",
                    (_iso(now), proposal_id),
                )
                self._event(proposal_id, row["plugin_id"], "EXPIRED", approved_by, "deployment approval expired")
                error = "deployment proposal expired"
            elif row["requested_by"] == approved_by:
                error = "requester cannot approve their own deployment"
            elif row["expected_current_image"] != current_image:
                self.connection.execute(
                    "UPDATE deployment_proposals SET status='STALE',decided_at=? WHERE proposal_id=?",
                    (_iso(now), proposal_id),
                )
                self._event(proposal_id, row["plugin_id"], "STALE", approved_by, "current image precondition changed")
                error = "current image changed after proposal"
            else:
                self.connection.execute(
                    "UPDATE deployment_proposals SET status='APPROVED',approved_by=?,decided_at=? WHERE proposal_id=?",
                    (approved_by, _iso(now), proposal_id),
                )
                self._event(proposal_id, row["plugin_id"], "APPROVED", approved_by, "exact deployment approved")
        if error:
            raise DeploymentApprovalError(error)
        return self.get(proposal_id)

    def consume(
        self, proposal_id: str, *, plan: ImmutableDeploymentPlan,
        current_image: str, actor: str,
    ) -> dict[str, Any]:
        _require_actor(actor)
        now, values, denied = _now(), asdict(plan), False
        with self._lock, self.connection:
            result = self.connection.execute(
                """UPDATE deployment_proposals SET status='CONSUMED',consumed_at=?
                   WHERE proposal_id=? AND status='APPROVED' AND expires_at>?
                     AND plugin_id=? AND service=? AND git_sha=? AND image_reference=?
                     AND expected_current_image=? AND rollback_image=?
                     AND health_timeout_seconds=? AND expected_current_image=?
                     AND policy_version=?""",
                (_iso(now), proposal_id, _iso(now), values["plugin_id"], values["service"],
                 values["git_sha"], values["image_reference"], values["expected_current_image"],
                 values["rollback_image"], values["health_timeout_seconds"], current_image,
                 POLICY_VERSION),
            )
            row = self.connection.execute(
                "SELECT plugin_id FROM deployment_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if result.rowcount != 1:
                if row:
                    self._event(proposal_id, row["plugin_id"], "EXECUTION_DENIED", actor, "deployment execution rejected by policy")
                denied = True
            else:
                self._event(proposal_id, row["plugin_id"], "CONSUMED", actor, "deployment approval consumed")
        if denied:
            raise DeploymentApprovalError("deployment approval is expired, altered, stale, replayed, or invalid")
        return self.get(proposal_id)

    def record_execution(self, proposal_id: str, *, status: str, actor: str, detail: str) -> None:
        _require_actor(actor)
        if status not in {"HEALTHY", "ROLLED_BACK", "FAILED"}:
            raise DeploymentApprovalError("invalid deployment execution status")
        proposal = self.get(proposal_id)
        with self._lock, self.connection:
            self._event(proposal_id, proposal["plugin_id"], status, actor, detail)
