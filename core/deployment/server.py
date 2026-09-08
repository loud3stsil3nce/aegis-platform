"""Environment-wired entrypoint for the internal deployment approval API."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .api import create_app
from .auth import ActorAuthenticator, MAX_TOKEN_BYTES
from .lifecycle import HttpLifecycleAdapter
from .restart import RestartExecutor, RestartProposalStore
from .service import DeploymentService


MAX_CONFIG_BYTES = 65_536


def _json_file(name: str) -> dict:
    path = Path(os.environ[name])
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_CONFIG_BYTES:
        raise RuntimeError(f"{name} is empty or oversized")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} must contain a JSON object")
    return value


def _token_provider(path: Path):
    def provide() -> str:
        raw = path.read_bytes()
        if not raw or len(raw) > MAX_TOKEN_BYTES:
            raise RuntimeError("lifecycle proxy token is empty or oversized")
        value = raw.decode("utf-8", errors="strict").strip()
        if not value:
            raise RuntimeError("lifecycle proxy token is empty")
        return value
    return provide


def build_app():
    actors_config = _json_file("AEGIS_DEPLOYMENT_ACTORS_FILE")
    manifest = _json_file("AEGIS_DEPLOYMENT_PLUGIN_MANIFEST")
    plugin_id = manifest["metadata"]["id"]
    restart = manifest["spec"].get("lifecycle", {}).get("restart")
    hooks = {plugin_id: restart} if isinstance(restart, dict) else {}
    store = RestartProposalStore(os.environ["AEGIS_DEPLOYMENT_STATE_PATH"])
    adapter = HttpLifecycleAdapter(
        os.environ.get("AEGIS_DOCKER_LIFECYCLE_URL", "http://docker-lifecycle:8011"),
        _token_provider(Path(os.environ["AEGIS_DOCKER_LIFECYCLE_TOKEN_FILE"])),
    )
    service = DeploymentService(store, RestartExecutor(store, adapter, hooks))
    return create_app(service, ActorAuthenticator(actors_config.get("actors", [])))


app = build_app()
