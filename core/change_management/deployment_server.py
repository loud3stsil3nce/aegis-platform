"""Environment-wired server for the internal immutable deployment API."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .deployment_api import create_app
from .deployment_auth import DeploymentActorAuthenticator
from .deployment_executor import OneServiceDeploymentExecutor
from .deployment_policy import DeploymentPolicy
from .deployment_proposals import DeploymentProposalStore
from .deployment_service import ImmutableDeploymentService
from .runtime_adapter import HttpDeploymentAdapter


MAX_CONFIG_BYTES = 16_384
MAX_TOKEN_BYTES = 4_096


def _json_file(name: str) -> object:
    raw = Path(os.environ[name]).read_bytes()
    if not raw or len(raw) > MAX_CONFIG_BYTES:
        raise RuntimeError(f"{name} is empty or oversized")
    return json.loads(raw)


def _token() -> str:
    raw = Path(os.environ["AEGIS_IMAGE_DEPLOYMENT_PROXY_TOKEN_FILE"]).read_bytes()
    if not raw or len(raw) > MAX_TOKEN_BYTES:
        raise RuntimeError("deployment proxy token is empty or oversized")
    token = raw.decode("utf-8", errors="strict").strip()
    if not token:
        raise RuntimeError("deployment proxy token is empty")
    return token


policy = DeploymentPolicy.from_dict(_json_file("AEGIS_IMAGE_DEPLOYMENT_POLICY"))
store = DeploymentProposalStore(os.environ["AEGIS_IMAGE_DEPLOYMENT_STATE_PATH"])
adapter = HttpDeploymentAdapter(
    os.environ["AEGIS_IMAGE_DEPLOYMENT_PROXY_URL"], _token,
    request_timeout=float(os.getenv("AEGIS_IMAGE_DEPLOYMENT_PROXY_TIMEOUT", "5")),
)
executor = OneServiceDeploymentExecutor(store=store, adapter=adapter)
service = ImmutableDeploymentService(policy=policy, store=store, executor=executor)
authenticator = DeploymentActorAuthenticator(_json_file("AEGIS_IMAGE_DEPLOYMENT_ACTORS_FILE"))
app = create_app(service, authenticator)

