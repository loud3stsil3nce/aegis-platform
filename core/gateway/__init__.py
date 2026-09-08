"""Core gateway identity and capability authorization."""

from .auth import GatewayAuthorizationError, authorize_plugin

__all__ = ["GatewayAuthorizationError", "authorize_plugin"]
