"""Load and validate the stable fields of an Aegis v1alpha1 plugin manifest."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PLUGIN_ID = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")
SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
COMPAT = re.compile(r"^(?:\^|~|>=)?[0-9]+\.[0-9]+\.[0-9]+$")
CAPABILITY = re.compile(r"^[a-z][a-z0-9_.-]*$")
SECRET = re.compile(r"^[A-Z][A-Z0-9_]*$")
RISKS = {"R0", "R1", "R2", "R3", "R4", "R5"}
MODES = {"remote", "managed-container", "monitored-project"}


class ManifestError(ValueError):
    """Raised when a plugin manifest is malformed or unsafe."""


def load_manifest(path: str | Path) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore
        except ModuleNotFoundError as exc:
            raise ManifestError(
                "ordinary YAML requires PyYAML; JSON syntax is valid YAML and works without dependencies"
            ) from exc
        data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ManifestError("manifest root must be an object")
    return data


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{field} must be an object")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ManifestError(f"{field} must be a list")
    return value


def validate_manifest(data: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    if data.get("apiVersion") != "aegis.dev/v1alpha1":
        errors.append("apiVersion must be aegis.dev/v1alpha1")
    if data.get("kind") != "Plugin":
        errors.append("kind must be Plugin")

    metadata = _object(data.get("metadata"), "metadata")
    plugin_id = metadata.get("id", "")
    if not isinstance(plugin_id, str) or not PLUGIN_ID.fullmatch(plugin_id) or len(plugin_id) > 63:
        errors.append("metadata.id is not a valid stable plugin id")
    if not SEMVER.fullmatch(str(metadata.get("version", ""))):
        errors.append("metadata.version must be semantic version X.Y.Z")
    if not str(metadata.get("name", "")).strip():
        errors.append("metadata.name is required")

    spec = _object(data.get("spec"), "spec")
    if not COMPAT.fullmatch(str(spec.get("coreCompatibility", ""))):
        errors.append("spec.coreCompatibility must be a supported semantic-version constraint")
    runtime = _object(spec.get("runtime"), "spec.runtime")
    mode = runtime.get("mode")
    if mode not in MODES:
        errors.append(f"spec.runtime.mode must be one of {sorted(MODES)}")
    if mode == "remote":
        endpoint = str(runtime.get("endpoint", ""))
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            errors.append("remote runtime requires an http(s) endpoint")
    elif mode == "managed-container":
        if not runtime.get("composeFile") or not runtime.get("service"):
            errors.append("managed-container runtime requires composeFile and service")
    elif mode == "monitored-project":
        if not str(runtime.get("projectPath", "")).startswith("/"):
            errors.append("monitored-project runtime requires an absolute projectPath")
        if runtime.get("readOnly") is not True:
            errors.append("monitored-project runtime must declare readOnly: true")
        for field in ("statusFile", "versionFile", "metricsFile", "logFile"):
            value = runtime.get(field)
            if not isinstance(value, str) or not value or value.startswith("/") or ".." in value.split("/"):
                errors.append(f"monitored-project runtime requires a safe relative {field}")

    mcp_value = spec.get("mcp")
    if mode == "monitored-project":
        if mcp_value is not None:
            errors.append("monitored-project must not declare an MCP transport")
    else:
        mcp = _object(mcp_value, "spec.mcp")
        if mcp.get("transport") != "streamable-http":
            errors.append("spec.mcp.transport must be streamable-http")
        if not str(mcp.get("path", "")).startswith("/"):
            errors.append("spec.mcp.path must be absolute")

    permissions = _object(spec.get("permissions"), "spec.permissions")
    capabilities = _list(permissions.get("capabilities"), "spec.permissions.capabilities")
    names: set[str] = set()
    for index, item in enumerate(capabilities):
        capability = _object(item, f"capabilities[{index}]")
        name, risk = capability.get("name"), capability.get("risk")
        if not isinstance(name, str) or not CAPABILITY.fullmatch(name):
            errors.append(f"capabilities[{index}].name is invalid")
        elif name in names:
            errors.append(f"duplicate capability: {name}")
        else:
            names.add(name)
        if risk not in RISKS:
            errors.append(f"capabilities[{index}].risk must be R0-R5")
    for field, pattern in (("secrets", SECRET), ("networks", CAPABILITY), ("storage", CAPABILITY)):
        values = _list(permissions.get(field), f"spec.permissions.{field}")
        if len(values) != len(set(values)):
            errors.append(f"spec.permissions.{field} contains duplicates")
        for value in values:
            if not isinstance(value, str) or not pattern.fullmatch(value):
                errors.append(f"invalid {field} declaration: {value!r}")

    health = _object(spec.get("health"), "spec.health")
    for field in ("liveness", "readiness", "version", "metrics", "logs"):
        if not str(health.get(field, "")).startswith("/"):
            errors.append(f"spec.health.{field} must be an absolute path")

    lifecycle = _object(spec.get("lifecycle"), "spec.lifecycle")
    if lifecycle.get("manager") != "aegis-cli":
        errors.append("spec.lifecycle.manager must be aegis-cli")
    required_lifecycle = {"disable", "upgrade", "rollback", "remove"}
    supported_lifecycle = set(_list(lifecycle.get("supports"), "spec.lifecycle.supports"))
    if not required_lifecycle <= supported_lifecycle or supported_lifecycle - required_lifecycle - {"restart"}:
        errors.append("spec.lifecycle.supports must contain disable, upgrade, rollback, and remove")
    restart = lifecycle.get("restart")
    if "restart" in supported_lifecycle:
        restart = _object(restart, "spec.lifecycle.restart")
        if restart.get("action") != "plugin.restart":
            errors.append("spec.lifecycle.restart.action must be plugin.restart")
        target = restart.get("target")
        if not isinstance(target, str) or not target or len(target) > 128 or not CAPABILITY.fullmatch(target):
            errors.append("spec.lifecycle.restart.target is invalid")
        timeout = restart.get("healthTimeoutSeconds")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 5 <= timeout <= 300:
            errors.append("spec.lifecycle.restart.healthTimeoutSeconds must be 5-300")
    elif restart is not None:
        errors.append("spec.lifecycle.restart requires restart support")

    if errors:
        raise ManifestError("; ".join(errors))
    return data


def permission_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, list[str]]:
    """Return permission additions/removals; any addition requires human review."""
    validate_manifest(old)
    validate_manifest(new)
    old_permissions = old["spec"]["permissions"]
    new_permissions = new["spec"]["permissions"]
    old_caps = {f"{item['name']}:{item['risk']}" for item in old_permissions["capabilities"]}
    new_caps = {f"{item['name']}:{item['risk']}" for item in new_permissions["capabilities"]}
    result: dict[str, list[str]] = {}
    for field, before, after in (
        ("capabilities", old_caps, new_caps),
        ("secrets", set(old_permissions["secrets"]), set(new_permissions["secrets"])),
        ("networks", set(old_permissions["networks"]), set(new_permissions["networks"])),
        ("storage", set(old_permissions["storage"]), set(new_permissions["storage"])),
    ):
        result[f"added_{field}"] = sorted(after - before)
        result[f"removed_{field}"] = sorted(before - after)
    return result


def is_core_compatible(constraint: str, core_version: str) -> bool:
    """Evaluate the deliberately small v1 compatibility grammar."""
    if not COMPAT.fullmatch(constraint) or not SEMVER.fullmatch(core_version):
        raise ManifestError("invalid compatibility constraint or Core version")
    operator = next((prefix for prefix in (">=", "^", "~") if constraint.startswith(prefix)), "")
    required = constraint[len(operator):]
    current_parts = tuple(int(part) for part in core_version.split("-")[0].split("."))
    required_parts = tuple(int(part) for part in required.split("-")[0].split("."))
    if operator == ">=":
        return current_parts >= required_parts
    if operator == "^":
        return current_parts >= required_parts and current_parts[0] == required_parts[0]
    if operator == "~":
        return current_parts >= required_parts and current_parts[:2] == required_parts[:2]
    return current_parts == required_parts
