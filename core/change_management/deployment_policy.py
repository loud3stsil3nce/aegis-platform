"""Immutable, single-service deployment proposal policy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_REPOSITORY = re.compile(r"^[a-z0-9]+(?:[._/-][a-z0-9]+)*$")


class DeploymentPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class DeploymentTarget:
    plugin_id: str
    service: str
    image_repository: str


@dataclass(frozen=True)
class ImmutableDeploymentPlan:
    plugin_id: str
    service: str
    git_sha: str
    image_reference: str
    expected_current_image: str
    rollback_image: str
    health_timeout_seconds: int


class DeploymentPolicy:
    def __init__(self, targets: dict[str, DeploymentTarget]):
        if not targets or any(key != target.plugin_id for key, target in targets.items()):
            raise DeploymentPolicyError("deployment targets must be explicit and keyed by plugin ID")
        if any(not _IMAGE_REPOSITORY.fullmatch(target.image_repository) for target in targets.values()):
            raise DeploymentPolicyError("deployment image repository is invalid")
        self.targets = dict(targets)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DeploymentPolicy":
        if not isinstance(value, dict) or set(value) != {"targets"} or not isinstance(value["targets"], list):
            raise DeploymentPolicyError("deployment policy must contain only a target list")
        targets = {}
        for item in value["targets"]:
            if not isinstance(item, dict) or set(item) != {"pluginId", "service", "imageRepository"}:
                raise DeploymentPolicyError("deployment target fields are invalid")
            if any(not isinstance(item[field], str) for field in item):
                raise DeploymentPolicyError("deployment target values must be strings")
            target = DeploymentTarget(item["pluginId"], item["service"], item["imageRepository"])
            if not target.plugin_id or not target.service or target.plugin_id in targets:
                raise DeploymentPolicyError("deployment target identity is invalid or duplicated")
            targets[target.plugin_id] = target
        return cls(targets)

    def plan(
        self, *, plugin_id: str, git_sha: str, image_digest: str,
        expected_current_image: str, health_timeout_seconds: int,
    ) -> ImmutableDeploymentPlan:
        target = self.targets.get(plugin_id)
        if target is None:
            raise DeploymentPolicyError("plugin is not an allowlisted deployment target")
        if not _GIT_SHA.fullmatch(git_sha):
            raise DeploymentPolicyError("deployment requires an exact Git commit SHA")
        if not _DIGEST.fullmatch(image_digest):
            raise DeploymentPolicyError("deployment requires an immutable image digest")
        prefix = target.image_repository + "@"
        if not expected_current_image.startswith(prefix) or not _DIGEST.fullmatch(expected_current_image[len(prefix):]):
            raise DeploymentPolicyError("current image is not an immutable expected-state reference")
        image_reference = prefix + image_digest
        if image_reference == expected_current_image:
            raise DeploymentPolicyError("new image must differ from current image")
        if not 10 <= health_timeout_seconds <= 300:
            raise DeploymentPolicyError("health timeout must be between 10 and 300 seconds")
        return ImmutableDeploymentPlan(
            target.plugin_id, target.service, git_sha, image_reference,
            expected_current_image, expected_current_image, health_timeout_seconds,
        )
