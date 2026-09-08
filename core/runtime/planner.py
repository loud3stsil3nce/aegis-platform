"""Translate a validated manifest into a bounded, non-executing runtime plan."""

from __future__ import annotations

import ipaddress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class RuntimePolicyError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimePlan:
    plugin_id: str
    mode: str
    identity: str
    network: str | None
    endpoint: str | None
    compose_file: str | None
    service: str | None
    project_path: str | None
    secret_names: tuple[str, ...]
    data_paths: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_remote_endpoint(endpoint: str, declared_networks: set[str]) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimePolicyError("remote MCP endpoint must be credential-free HTTPS")
    if parsed.query or parsed.fragment:
        raise RuntimePolicyError("remote MCP endpoint cannot contain query or fragment")
    host = parsed.hostname.lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified):
        raise RuntimePolicyError("remote MCP endpoint targets a prohibited address")
    if host not in declared_networks:
        raise RuntimePolicyError(f"remote MCP host {host} is not declared in permissions.networks")
    return endpoint.rstrip("/")


def _safe_internal_endpoint(endpoint: str, declared_networks: set[str]) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimePolicyError("managed endpoint must be credential-free internal HTTP")
    if parsed.query or parsed.fragment or parsed.hostname.lower() not in declared_networks:
        raise RuntimePolicyError("managed endpoint host must be exactly declared in permissions.networks")
    return endpoint.rstrip("/")


def build_runtime_plan(
    manifest: dict[str, Any],
    plugin_root: str | Path,
    state_root: str | Path,
    granted_secrets: set[str],
    allowed_project_roots: tuple[str | Path, ...] = (),
) -> RuntimePlan:
    plugin_id = manifest["metadata"]["id"]
    spec, runtime = manifest["spec"], manifest["spec"]["runtime"]
    permissions = spec["permissions"]
    declared_secrets = set(permissions["secrets"])
    if not granted_secrets <= declared_secrets:
        undeclared = sorted(granted_secrets - declared_secrets)
        raise RuntimePolicyError(f"secret grant was not declared: {undeclared}")
    if granted_secrets != declared_secrets:
        missing = sorted(declared_secrets - granted_secrets)
        raise RuntimePolicyError(f"required secret grants are missing: {missing}")

    root = Path(plugin_root).resolve()
    state = Path(state_root).resolve() / "plugins" / plugin_id
    data_paths = tuple(str(state / "data" / name) for name in permissions["storage"])
    common = {
        "plugin_id": plugin_id,
        "mode": runtime["mode"],
        "identity": f"plugin:{plugin_id}",
        "secret_names": tuple(sorted(granted_secrets)),
        "data_paths": data_paths,
    }

    if runtime["mode"] == "remote":
        endpoint = _safe_remote_endpoint(runtime["endpoint"], set(permissions["networks"]))
        return RuntimePlan(network=None, endpoint=endpoint + spec["mcp"]["path"], compose_file=None, service=None, project_path=None, **common)

    if runtime["mode"] == "managed-container":
        compose_file = (root / runtime["composeFile"]).resolve()
        if not _within(compose_file, root) or not compose_file.is_file():
            raise RuntimePolicyError("composeFile must exist inside the plugin package")
        endpoint = _safe_internal_endpoint(runtime.get("endpoint", ""), set(permissions["networks"]))
        return RuntimePlan(
            network=f"aegis-plugin-{plugin_id}", endpoint=endpoint, compose_file=str(compose_file),
            service=runtime["service"], project_path=None, **common,
        )

    project = Path(runtime["projectPath"]).resolve()
    roots = tuple(Path(item).resolve() for item in allowed_project_roots)
    if not roots or not any(_within(project, root_item) for root_item in roots):
        raise RuntimePolicyError("monitored project is outside configured read-only roots")
    return RuntimePlan(network=None, endpoint=None, compose_file=None, service=None, project_path=str(project), **common)
