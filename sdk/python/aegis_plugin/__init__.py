"""Public, dependency-light Aegis plugin contract helpers."""

from .manifest import ManifestError, is_core_compatible, load_manifest, permission_diff, validate_manifest

__all__ = ["ManifestError", "is_core_compatible", "load_manifest", "permission_diff", "validate_manifest"]
