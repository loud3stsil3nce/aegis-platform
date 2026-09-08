"""Authorize a plugin service token against durable registry grants."""

from __future__ import annotations

from core.registry import PluginRegistry, RegistryError


class GatewayAuthorizationError(PermissionError):
    pass


def authorize_plugin(registry: PluginRegistry, plugin_id: str, token: str, capability: str) -> dict:
    try:
        plugin = registry.get(plugin_id)
    except RegistryError as exc:
        raise GatewayAuthorizationError("unknown plugin identity") from exc
    if plugin["status"] != "enabled":
        raise GatewayAuthorizationError("plugin identity is disabled")
    if not registry.verify_token(plugin_id, token):
        raise GatewayAuthorizationError("invalid plugin service token")
    grants = {item["name"]: item for item in plugin["manifest"]["spec"]["permissions"]["capabilities"]}
    if capability not in grants:
        raise GatewayAuthorizationError("capability is not declared or granted")
    return {"identity": plugin["identity"], "pluginId": plugin_id, "capability": capability, "risk": grants[capability]["risk"]}
