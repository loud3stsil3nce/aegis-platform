"""Durable, exact-action approval and execution for one plugin restart."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol


POLICY_VERSION = "restart-v1"
ACTION_TYPE = "plugin.restart"


class ApprovalError(ValueError):
    """The proposed action is not currently authorized."""


class RestartAdapter(Protocol):
    def state(self, target: str) -> dict[str, Any]: ...
    def restart(self, target: str, idempotency_key: str) -> None: ...
    def wait_ready(self, target: str, timeout_seconds: int) -> dict[str, Any]: ...


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _canonical(value: dict[str, Any]) -> str:
    if not isinstance(value, dict):
        raise TypeError("restart arguments must be an object")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class RestartResult:
    proposal_id: str
    plugin_id: str
    target: str
    status: str
    before: dict[str, Any]
    after: dict[str, Any] | None
    recovery: str | None


class RestartProposalStore:
    """SQLite store whose approval consumption is a single atomic update."""

    def __init__(self, path: str | Path):
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(Path(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS restart_proposals (
              proposal_id TEXT PRIMARY KEY, plugin_id TEXT NOT NULL,
              target TEXT NOT NULL, action_type TEXT NOT NULL,
              arguments_json TEXT NOT NULL, arguments_hash TEXT NOT NULL,
              expected_state_json TEXT NOT NULL, expected_state_hash TEXT NOT NULL,
              requested_by TEXT NOT NULL, approved_by TEXT,
              status TEXT NOT NULL, nonce_hash TEXT NOT NULL UNIQUE,
              policy_version TEXT NOT NULL, created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL, decided_at TEXT, consumed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS restart_events (
              event_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL,
              plugin_id TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL,
              actor TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def get(self, proposal_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM restart_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        if row is None:
            raise ApprovalError("proposal not found")
        value = dict(row)
        value["arguments"] = json.loads(value.pop("arguments_json"))
        value["expected_state"] = json.loads(value.pop("expected_state_json"))
        value.pop("nonce_hash", None)
        return value

    def propose(
        self, *, plugin_id: str, target: str, requested_by: str,
        expected_state: dict[str, Any], timeout_seconds: int = 60,
        ttl_seconds: int = 900,
    ) -> str:
        if not (30 <= ttl_seconds <= 3600):
            raise ApprovalError("approval TTL must be between 30 and 3600 seconds")
        if not (5 <= timeout_seconds <= 300):
            raise ApprovalError("health timeout must be between 5 and 300 seconds")
        arguments = {"timeout_seconds": timeout_seconds}
        now, proposal_id = _now(), str(uuid.uuid4())
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO restart_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (proposal_id, plugin_id, target, ACTION_TYPE, _canonical(arguments),
                 _hash(arguments), _canonical(expected_state), _hash(expected_state),
                 requested_by, None, "PENDING",
                 hashlib.sha256(secrets.token_bytes(32)).hexdigest(), POLICY_VERSION,
                 _iso(now), _iso(now + timedelta(seconds=ttl_seconds)), None, None),
            )
            self._insert_event(
                proposal_id, plugin_id, target, "PROPOSED", requested_by,
                "restart proposal created",
            )
        return proposal_id

    def approve(self, proposal_id: str, *, approved_by: str) -> None:
        now = _now()
        expired = False
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT * FROM restart_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise ApprovalError("proposal not found")
            if row["status"] != "PENDING":
                raise ApprovalError(f"proposal is already {row['status']}")
            if datetime.fromisoformat(row["expires_at"]) <= now:
                self.connection.execute(
                    "UPDATE restart_proposals SET status='EXPIRED',decided_at=? WHERE proposal_id=?",
                    (_iso(now), proposal_id),
                )
                self._insert_event(
                    proposal_id, row["plugin_id"], row["target"], "EXPIRED",
                    approved_by, "approval rejected because proposal expired",
                )
                expired = True
            elif secrets.compare_digest(row["requested_by"], approved_by):
                raise ApprovalError("requester cannot approve their own action")
            else:
                self.connection.execute(
                    "UPDATE restart_proposals SET status='APPROVED',approved_by=?,decided_at=? WHERE proposal_id=?",
                    (approved_by, _iso(now), proposal_id),
                )
                self._insert_event(
                    proposal_id, row["plugin_id"], row["target"], "APPROVED",
                    approved_by, "restart proposal approved",
                )
        if expired:
            raise ApprovalError("proposal expired")

    def consume(
        self, proposal_id: str, *, plugin_id: str, target: str,
        arguments: dict[str, Any], expected_state: dict[str, Any],
    ) -> sqlite3.Row:
        now = _now()
        with self._lock, self.connection:
            result = self.connection.execute(
                """UPDATE restart_proposals SET status='CONSUMED',consumed_at=?
                   WHERE proposal_id=? AND status='APPROVED' AND expires_at>?
                     AND action_type=? AND plugin_id=? AND target=?
                     AND arguments_hash=? AND expected_state_hash=? AND policy_version=?""",
                (_iso(now), proposal_id, _iso(now), ACTION_TYPE, plugin_id, target,
                 _hash(arguments), _hash(expected_state), POLICY_VERSION),
            )
            if result.rowcount != 1:
                raise ApprovalError("approval is expired, altered, replayed, or invalid")
            return self.connection.execute(
                "SELECT * FROM restart_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()

    def record(self, proposal_id: str, plugin_id: str, target: str, status: str, actor: str, detail: str) -> None:
        with self._lock, self.connection:
            self._insert_event(proposal_id, plugin_id, target, status, actor, detail)

    def _insert_event(self, proposal_id: str, plugin_id: str, target: str, status: str, actor: str, detail: str) -> None:
        self.connection.execute(
            "INSERT INTO restart_events VALUES(?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), proposal_id, plugin_id, target, status, actor,
             detail[:1000], _iso(_now())),
        )


class RestartExecutor:
    def __init__(self, store: RestartProposalStore, adapter: RestartAdapter, hooks: dict[str, dict[str, Any]]):
        self.store, self.adapter, self.hooks = store, adapter, hooks

    def execute(self, proposal_id: str, *, plugin_id: str, arguments: dict[str, Any], actor: str) -> RestartResult:
        hook = self.hooks.get(plugin_id)
        if not hook or hook.get("action") != ACTION_TYPE:
            raise ApprovalError("plugin has no declared restart lifecycle hook")
        target = hook["target"]
        before = self.adapter.state(target)
        expected = {"status": before.get("status"), "started_at": before.get("started_at")}
        self.store.consume(
            proposal_id, plugin_id=plugin_id, target=target,
            arguments=arguments, expected_state=expected,
        )
        key = f"restart:{proposal_id}"
        # R3 actions fail closed if the durable audit boundary is unavailable.
        # This event is intentionally written after one-time consumption and
        # before the lifecycle adapter receives the mutation.
        self.store.record(
            proposal_id, plugin_id, target, "EXECUTING", actor,
            "approval consumed; restart dispatch pending",
        )
        try:
            self.adapter.restart(target, key)
            after = self.adapter.wait_ready(target, int(arguments["timeout_seconds"]))
            if after.get("health") not in {"healthy", "ready"}:
                raise RuntimeError(f"target did not become ready: {after.get('health', 'unknown')}")
        except Exception as exc:
            recovery = f"Restart consumed but readiness failed; inspect {plugin_id} and target {target}: {type(exc).__name__}"
            self.store.record(proposal_id, plugin_id, target, "FAILED", actor, recovery)
            return RestartResult(proposal_id, plugin_id, target, "FAILED", before, None, recovery)
        self.store.record(proposal_id, plugin_id, target, "SUCCESS", actor, "restart completed and readiness passed")
        return RestartResult(proposal_id, plugin_id, target, "SUCCESS", before, after, None)
