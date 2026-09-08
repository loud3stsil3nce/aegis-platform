"""Small structured audit store; unrestricted prompts and tool results are excluded."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditStore:
    def __init__(self, path: str | Path):
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(Path(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS audit_events (
            event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, trace_id TEXT NOT NULL,
            actor TEXT NOT NULL, interface TEXT NOT NULL, plugin_id TEXT NOT NULL,
            capability TEXT NOT NULL, risk TEXT NOT NULL, arguments_hash TEXT NOT NULL,
            started_at TEXT NOT NULL, ended_at TEXT NOT NULL, status TEXT NOT NULL,
            error_class TEXT, result_bytes INTEGER NOT NULL, policy_version TEXT NOT NULL
            )"""
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def record(
        self, *, request_id: str, trace_id: str, actor: str, interface: str,
        plugin_id: str, capability: str, risk: str, arguments: dict[str, Any],
        started_at: str, status: str, error_class: str | None, result_bytes: int,
        policy_version: str = "observability-v1",
    ) -> None:
        normalized = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), request_id, trace_id, actor, interface, plugin_id,
                 capability, risk, hashlib.sha256(normalized.encode()).hexdigest(),
                 started_at, _now(), status, error_class, result_bytes, policy_version),
            )

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.connection.execute("SELECT * FROM audit_events ORDER BY rowid")]
