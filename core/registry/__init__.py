"""Durable plugin registry and lifecycle state."""

from .store import PluginRegistry, RegistryError

__all__ = ["PluginRegistry", "RegistryError"]
