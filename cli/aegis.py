#!/usr/bin/env python3
"""A dependency-light CLI for Aegis plugin contract workflows."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SDK_ROOT = REPO_ROOT / "sdk" / "python"
sys.path.insert(0, str(SDK_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from aegis_plugin import ManifestError, is_core_compatible, load_manifest, permission_diff, validate_manifest
from core.registry import PluginRegistry, RegistryError
from core.runtime import RuntimePolicyError, build_runtime_plan


def _manifest_path(value: str) -> Path:
    path = Path(value)
    return path / "aegis-plugin.yaml" if path.is_dir() else path


def command_init(args: argparse.Namespace) -> int:
    destination = Path(args.directory).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    manifest_path = destination / "aegis-plugin.yaml"
    if manifest_path.exists() and not args.force:
        raise ManifestError(f"refusing to overwrite existing {manifest_path}; pass --force explicitly")
    template_path = REPO_ROOT / "examples" / "hello-aegis" / "aegis-plugin.yaml"
    template = load_manifest(template_path)
    template["metadata"].update({"id": args.plugin_id, "name": args.name or args.plugin_id})
    manifest_path.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")
    print(manifest_path)
    return 0


def command_validate(args: argparse.Namespace) -> int:
    path = _manifest_path(args.path)
    manifest = validate_manifest(load_manifest(path))
    print(f"valid: {manifest['metadata']['id']}@{manifest['metadata']['version']}")
    return 0


def _summary(manifest: dict) -> dict:
    spec = manifest["spec"]
    permissions = spec["permissions"]
    return {
        "id": manifest["metadata"]["id"],
        "version": manifest["metadata"]["version"],
        "coreCompatibility": spec["coreCompatibility"],
        "runtimeMode": spec["runtime"]["mode"],
        "mcpTransport": spec["mcp"]["transport"],
        "capabilities": permissions["capabilities"],
        "secretNames": permissions["secrets"],
        "networks": permissions["networks"],
        "storage": permissions["storage"],
    }


def command_inspect(args: argparse.Namespace) -> int:
    manifest = validate_manifest(load_manifest(_manifest_path(args.path)))
    print(json.dumps(_summary(manifest), indent=2, sort_keys=True))
    return 0


def command_diff(args: argparse.Namespace) -> int:
    old = load_manifest(_manifest_path(args.installed))
    new = load_manifest(_manifest_path(args.candidate))
    diff = permission_diff(old, new)
    expansion = any(values for key, values in diff.items() if key.startswith("added_"))
    print(json.dumps({"permissionExpansion": expansion, "changes": diff}, indent=2, sort_keys=True))
    return 2 if expansion else 0


def _registry(args: argparse.Namespace) -> PluginRegistry:
    return PluginRegistry(args.state_dir)


def command_install(args: argparse.Namespace) -> int:
    manifest_path = _manifest_path(args.path)
    manifest = validate_manifest(load_manifest(manifest_path))
    _require_compatibility(manifest, args.core_version)
    build_runtime_plan(
        manifest, manifest_path.parent, args.state_dir, set(args.grant_secret),
        tuple(args.allow_project_root),
    )
    registry = _registry(args)
    try:
        token = registry.install(manifest)
    finally:
        registry.close()
    print(json.dumps({"installed": manifest["metadata"]["id"], "serviceToken": token, "warning": "store this token now; Aegis stores only its hash"}, indent=2))
    return 0


def command_list(args: argparse.Namespace) -> int:
    registry = _registry(args)
    try:
        plugins = [{key: item[key] for key in ("id", "version", "status", "runtimeMode", "identity")} for item in registry.list()]
    finally:
        registry.close()
    print(json.dumps(plugins, indent=2))
    return 0


def command_status(args: argparse.Namespace) -> int:
    registry = _registry(args)
    try:
        registry.set_status(args.plugin_id, args.status)
    finally:
        registry.close()
    print(f"{args.plugin_id}: {args.status}")
    return 0


def command_upgrade(args: argparse.Namespace) -> int:
    manifest_path = _manifest_path(args.path)
    candidate = validate_manifest(load_manifest(manifest_path))
    _require_compatibility(candidate, args.core_version)
    build_runtime_plan(
        candidate, manifest_path.parent, args.state_dir, set(args.grant_secret),
        tuple(args.allow_project_root),
    )
    registry = _registry(args)
    try:
        current = registry.get(candidate["metadata"]["id"])["manifest"]
        changes = permission_diff(current, candidate)
        expansion = any(values for key, values in changes.items() if key.startswith("added_"))
        registry.upgrade(candidate, expansion, args.approve_permission_expansion)
    finally:
        registry.close()
    print(json.dumps({"upgraded": candidate["metadata"]["id"], "permissionChanges": changes}, indent=2))
    return 0


def command_upgrade_plan(args: argparse.Namespace) -> int:
    candidate = validate_manifest(load_manifest(_manifest_path(args.path)))
    _require_compatibility(candidate, args.core_version)
    registry = _registry(args)
    try:
        current = registry.get(candidate["metadata"]["id"])["manifest"]
    finally:
        registry.close()
    changes = permission_diff(current, candidate)
    expansion = any(values for key, values in changes.items() if key.startswith("added_"))
    print(json.dumps({"plugin": candidate["metadata"]["id"], "from": current["metadata"]["version"], "to": candidate["metadata"]["version"], "permissionExpansion": expansion, "changes": changes}, indent=2))
    return 2 if expansion else 0


def _require_compatibility(manifest: dict, core_version: str) -> None:
    constraint = manifest["spec"]["coreCompatibility"]
    if not is_core_compatible(constraint, core_version):
        raise ManifestError(f"plugin requires Core {constraint}; installed Core is {core_version}")


def command_rollback(args: argparse.Namespace) -> int:
    registry = _registry(args)
    try:
        registry.rollback(args.plugin_id, args.version)
    finally:
        registry.close()
    print(f"{args.plugin_id}: rolled back to {args.version}")
    return 0


def command_remove(args: argparse.Namespace) -> int:
    registry = _registry(args)
    try:
        registry.remove(args.plugin_id)
    finally:
        registry.close()
    print(f"{args.plugin_id}: removed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aegis", description="Aegis Core and plugin management")
    subcommands = parser.add_subparsers(dest="command", required=True)
    plugin = subcommands.add_parser("plugin", help="manage an independently installable plugin")
    plugin_commands = plugin.add_subparsers(dest="plugin_command", required=True)
    state_default = os.getenv("AEGIS_STATE_DIR", str(Path.cwd() / ".aegis-state"))
    core_version_default = os.getenv("AEGIS_CORE_VERSION", (REPO_ROOT / "core" / "VERSION").read_text().strip())

    def state_argument(command):
        command.add_argument("--state-dir", default=state_default)

    def runtime_policy_arguments(command):
        command.add_argument("--grant-secret", action="append", default=[])
        command.add_argument("--allow-project-root", action="append", default=[])

    initialize = plugin_commands.add_parser("init", help="create a minimal plugin manifest")
    initialize.add_argument("plugin_id")
    initialize.add_argument("--name")
    initialize.add_argument("--directory", default=".")
    initialize.add_argument("--force", action="store_true")
    initialize.set_defaults(handler=command_init)

    validate = plugin_commands.add_parser("validate", help="validate a plugin manifest")
    validate.add_argument("path", nargs="?", default=".")
    validate.set_defaults(handler=command_validate)

    inspect = plugin_commands.add_parser("inspect", help="print declared runtime and permissions")
    inspect.add_argument("path", nargs="?", default=".")
    inspect.set_defaults(handler=command_inspect)

    diff = plugin_commands.add_parser("diff", help="compare installed and candidate permissions")
    diff.add_argument("installed")
    diff.add_argument("candidate")
    diff.set_defaults(handler=command_diff)

    install = plugin_commands.add_parser("install", help="validate and register a plugin")
    install.add_argument("path", nargs="?", default=".")
    install.add_argument("--core-version", default=core_version_default)
    state_argument(install)
    runtime_policy_arguments(install)
    install.set_defaults(handler=command_install)

    listing = plugin_commands.add_parser("list", help="list installed plugins")
    state_argument(listing)
    listing.set_defaults(handler=command_list)

    for name in ("enable", "disable"):
        status = plugin_commands.add_parser(name, help=f"{name} an installed plugin")
        status.add_argument("plugin_id")
        state_argument(status)
        status.set_defaults(handler=command_status, status="enabled" if name == "enable" else "disabled")

    upgrade = plugin_commands.add_parser("upgrade", help="upgrade with permission expansion protection")
    upgrade.add_argument("path")
    upgrade.add_argument("--core-version", default=core_version_default)
    upgrade.add_argument("--approve-permission-expansion", action="store_true")
    state_argument(upgrade)
    runtime_policy_arguments(upgrade)
    upgrade.set_defaults(handler=command_upgrade)

    upgrade_plan = plugin_commands.add_parser("upgrade-plan", help="validate and preview an upgrade without changing state")
    upgrade_plan.add_argument("path")
    upgrade_plan.add_argument("--core-version", default=core_version_default)
    state_argument(upgrade_plan)
    upgrade_plan.set_defaults(handler=command_upgrade_plan)

    rollback = plugin_commands.add_parser("rollback", help="restore a recorded plugin manifest version")
    rollback.add_argument("plugin_id")
    rollback.add_argument("version")
    state_argument(rollback)
    rollback.set_defaults(handler=command_rollback)

    remove = plugin_commands.add_parser("remove", help="remove a disabled plugin registration")
    remove.add_argument("plugin_id")
    state_argument(remove)
    remove.set_defaults(handler=command_remove)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return args.handler(args)
    except (ManifestError, RegistryError, RuntimePolicyError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
