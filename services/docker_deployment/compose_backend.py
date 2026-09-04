"""Fixed-argument Docker Compose backend for one policy-validated target."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Callable

from .policy import ProxyTarget


MAX_COMMAND_OUTPUT_BYTES = 16_384


class ComposeBackendError(RuntimeError):
    pass


class ComposeBackend:
    def __init__(
        self, *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ):
        self.runner = runner

    def _run(self, args: list[str], *, env: dict[str, str] | None = None) -> str:
        try:
            result = self.runner(
                args, check=True, capture_output=True, text=True, timeout=300, env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ComposeBackendError("bounded Docker Compose operation failed") from exc
        output = result.stdout
        if len(output.encode()) > MAX_COMMAND_OUTPUT_BYTES:
            raise ComposeBackendError("Docker Compose output exceeds policy limit")
        return output.strip()

    def state(self, target: ProxyTarget) -> dict[str, object]:
        output = self._run([
            "docker", "inspect", "--format",
            "{{json .State}}|{{json .Config.Image}}", target.container,
        ])
        try:
            raw_state, raw_image = output.split("|", 1)
            state, image = json.loads(raw_state), json.loads(raw_image)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ComposeBackendError("Docker returned invalid target state") from exc
        health = state.get("Health") or {}
        return {
            "target": target.service,
            "status": state.get("Status", "unknown"),
            "health": health.get("Status", "not-configured"),
            "image": image,
        }

    def apply(self, target: ProxyTarget, image_reference: str) -> None:
        self._run(["docker", "pull", image_reference])
        environment = os.environ.copy()
        environment[target.image_variable] = image_reference
        self._run([
            "docker-compose", "--project-name", target.compose_project,
            "--project-directory", target.project_directory,
            "-f", target.compose_file, "up", "-d", "--no-deps", "--no-build",
            "--force-recreate", target.service,
        ], env=environment)
