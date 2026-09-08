"""SQLite-backed plugin registry that stores declarations and token hashes only."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RegistryError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


class PluginRegistry:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.state_dir / "registry.sqlite3"
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(self.db_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS plugins (
              plugin_id TEXT PRIMARY KEY,
              version TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('enabled','disabled')),
              runtime_mode TEXT NOT NULL,
              manifest_json TEXT NOT NULL,
              identity TEXT NOT NULL UNIQUE,
              token_hash TEXT NOT NULL,
              installed_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS plugin_versions (
              plugin_id TEXT NOT NULL,
              version TEXT NOT NULL,
              manifest_json TEXT NOT NULL,
              recorded_at TEXT NOT NULL,
              PRIMARY KEY(plugin_id, version),
              FOREIGN KEY(plugin_id) REFERENCES plugins(plugin_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS registry_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              plugin_id TEXT NOT NULL,
              event_type TEXT NOT NULL,
              details_json TEXT NOT NULL,
              occurred_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()
        self.db_path.chmod(0o600)

    def close(self) -> None:
        self.connection.close()

    def _event(self, plugin_id: str, event_type: str, details: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO registry_events(plugin_id,event_type,details_json,occurred_at) VALUES(?,?,?,?)",
            (plugin_id, event_type, _canonical(details), _now()),
        )

    def install(self, manifest: dict[str, Any]) -> str:
        plugin_id = manifest["metadata"]["id"]
        with self._lock:
            if self.connection.execute("SELECT 1 FROM plugins WHERE plugin_id=?", (plugin_id,)).fetchone():
                raise RegistryError(f"plugin {plugin_id} is already installed")
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        identity = f"plugin:{plugin_id}"
        now = _now()
        serialized = _canonical(manifest)
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO plugins VALUES(?,?,?,?,?,?,?,?,?)",
                (plugin_id, manifest["metadata"]["version"], "enabled", manifest["spec"]["runtime"]["mode"], serialized, identity, token_hash, now, now),
            )
            self.connection.execute(
                "INSERT INTO plugin_versions VALUES(?,?,?,?)",
                (plugin_id, manifest["metadata"]["version"], serialized, now),
            )
            self._event(plugin_id, "installed", {"version": manifest["metadata"]["version"], "identity": identity})
        return token

    def get(self, plugin_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute("SELECT * FROM plugins WHERE plugin_id=?", (plugin_id,)).fetchone()
        if not row:
            raise RegistryError(f"plugin {plugin_id} is not installed")
        return {
            "id": row["plugin_id"], "version": row["version"], "status": row["status"],
            "runtimeMode": row["runtime_mode"], "identity": row["identity"],
            "manifest": json.loads(row["manifest_json"]), "installedAt": row["installed_at"],
            "updatedAt": row["updated_at"],
        }

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute("SELECT plugin_id FROM plugins ORDER BY plugin_id").fetchall()
        return [self.get(row["plugin_id"]) for row in rows]

    def verify_token(self, plugin_id: str, token: str) -> bool:
        with self._lock:
            row = self.connection.execute("SELECT token_hash FROM plugins WHERE plugin_id=?", (plugin_id,)).fetchone()
        return bool(row) and secrets.compare_digest(row["token_hash"], hashlib.sha256(token.encode()).hexdigest())

    def set_status(self, plugin_id: str, status: str) -> None:
        if status not in {"enabled", "disabled"}:
            raise RegistryError("invalid plugin status")
        self.get(plugin_id)
        with self._lock, self.connection:
            self.connection.execute("UPDATE plugins SET status=?,updated_at=? WHERE plugin_id=?", (status, _now(), plugin_id))
            self._event(plugin_id, status, {})

    def upgrade(self, manifest: dict[str, Any], permission_expansion: bool, approved: bool) -> None:
        plugin_id = manifest["metadata"]["id"]
        current = self.get(plugin_id)
        if current["version"] == manifest["metadata"]["version"]:
            raise RegistryError("candidate version is already installed")
        if permission_expansion and not approved:
            raise RegistryError("permission expansion requires explicit review")
        now, serialized = _now(), _canonical(manifest)
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO plugin_versions VALUES(?,?,?,?)",
                (plugin_id, manifest["metadata"]["version"], serialized, now),
            )
            self.connection.execute(
                "UPDATE plugins SET version=?,runtime_mode=?,manifest_json=?,updated_at=? WHERE plugin_id=?",
                (manifest["metadata"]["version"], manifest["spec"]["runtime"]["mode"], serialized, now, plugin_id),
            )
            self._event(plugin_id, "upgraded", {"from": current["version"], "to": manifest["metadata"]["version"], "permissionExpansionApproved": approved})

    def rollback(self, plugin_id: str, version: str) -> None:
        current = self.get(plugin_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT manifest_json FROM plugin_versions WHERE plugin_id=? AND version=?", (plugin_id, version)
            ).fetchone()
        if not row:
            raise RegistryError(f"no recorded version {version} for {plugin_id}")
        manifest = json.loads(row["manifest_json"])
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE plugins SET version=?,runtime_mode=?,manifest_json=?,updated_at=? WHERE plugin_id=?",
                (version, manifest["spec"]["runtime"]["mode"], row["manifest_json"], _now(), plugin_id),
            )
            self._event(plugin_id, "rolled_back", {"from": current["version"], "to": version})

    def remove(self, plugin_id: str) -> None:
        current = self.get(plugin_id)
        if current["status"] != "disabled":
            raise RegistryError("plugin must be disabled before removal")
        with self._lock, self.connection:
            self._event(plugin_id, "removed", {"version": current["version"]})
            self.connection.execute("DELETE FROM plugins WHERE plugin_id=?", (plugin_id,))
