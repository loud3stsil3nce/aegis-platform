"""Fail-closed validation for proposal-only Git changes."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any


class ChangePolicyError(ValueError):
    pass


_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,126}[A-Za-z0-9])?$")
_DIFF_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")


def _path(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise ChangePolicyError("diff contains an invalid path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ChangePolicyError("diff path escapes repository scope")
    return str(path)


@dataclass(frozen=True)
class ValidatedChange:
    repository: str
    base_branch: str
    proposed_branch: str
    paths: tuple[str, ...]
    additions: int
    deletions: int
    diff_bytes: int
    diff_sha256: str


@dataclass(frozen=True)
class ChangePolicy:
    repositories: frozenset[str]
    base_branches: frozenset[str]
    allowed_path_prefixes: tuple[str, ...]
    proposed_branch_prefix: str = "aegis/"
    max_files: int = 20
    max_diff_bytes: int = 131_072
    max_changed_lines: int = 1_000

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ChangePolicy":
        expected = {
            "repositories", "baseBranches", "allowedPathPrefixes",
            "proposedBranchPrefix", "maxFiles", "maxDiffBytes", "maxChangedLines",
        }
        if not isinstance(value, dict) or set(value) - expected:
            raise ChangePolicyError("change policy contains unknown fields")
        try:
            return cls(
                repositories=frozenset(value["repositories"]),
                base_branches=frozenset(value["baseBranches"]),
                allowed_path_prefixes=tuple(value["allowedPathPrefixes"]),
                proposed_branch_prefix=value.get("proposedBranchPrefix", "aegis/"),
                max_files=int(value.get("maxFiles", 20)),
                max_diff_bytes=int(value.get("maxDiffBytes", 131_072)),
                max_changed_lines=int(value.get("maxChangedLines", 1_000)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ChangePolicyError("change policy is incomplete or invalid") from exc

    def __post_init__(self) -> None:
        if not self.repositories or not all(_REPOSITORY.fullmatch(item) for item in self.repositories):
            raise ChangePolicyError("at least one valid repository must be explicitly allowed")
        if not self.base_branches or not all(_BRANCH.fullmatch(item) for item in self.base_branches):
            raise ChangePolicyError("at least one valid base branch must be explicitly allowed")
        if not self.allowed_path_prefixes:
            raise ChangePolicyError("at least one repository path prefix must be explicitly allowed")
        for prefix in self.allowed_path_prefixes:
            normalized = _path(prefix.rstrip("/"))
            if normalized != prefix.rstrip("/"):
                raise ChangePolicyError("allowed path prefix is not normalized")
        if not _BRANCH.fullmatch(self.proposed_branch_prefix.rstrip("/")):
            raise ChangePolicyError("proposed branch prefix is invalid")
        if min(self.max_files, self.max_diff_bytes, self.max_changed_lines) <= 0:
            raise ChangePolicyError("change budgets must be positive")

    def validate(
        self, *, repository: str, base_branch: str,
        proposed_branch: str, unified_diff: str,
    ) -> ValidatedChange:
        if repository not in self.repositories:
            raise ChangePolicyError("repository is not allowlisted")
        if base_branch not in self.base_branches:
            raise ChangePolicyError("base branch is not allowlisted")
        if not _BRANCH.fullmatch(proposed_branch) or not proposed_branch.startswith(self.proposed_branch_prefix):
            raise ChangePolicyError("proposed branch is outside the governed namespace")
        if proposed_branch in self.base_branches or proposed_branch == base_branch:
            raise ChangePolicyError("writes to a protected base branch are prohibited")
        raw = unified_diff.encode("utf-8")
        if not raw or len(raw) > self.max_diff_bytes:
            raise ChangePolicyError("diff is empty or exceeds its byte budget")
        if "GIT binary patch" in unified_diff or "Binary files " in unified_diff:
            raise ChangePolicyError("binary changes are prohibited")

        paths: list[str] = []
        additions = deletions = 0
        for line in unified_diff.splitlines():
            match = _DIFF_HEADER.fullmatch(line)
            if match:
                old_path, new_path = _path(match.group(1)), _path(match.group(2))
                for path in (old_path, new_path):
                    if not any(path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/") for prefix in self.allowed_path_prefixes):
                        raise ChangePolicyError(f"path is outside the allowlist: {path}")
                if new_path not in paths:
                    paths.append(new_path)
                continue
            if line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1
        if not paths:
            raise ChangePolicyError("diff has no recognized file changes")
        if len(paths) > self.max_files:
            raise ChangePolicyError("diff exceeds its file budget")
        if additions + deletions > self.max_changed_lines:
            raise ChangePolicyError("diff exceeds its changed-line budget")
        return ValidatedChange(
            repository, base_branch, proposed_branch, tuple(paths), additions,
            deletions, len(raw), hashlib.sha256(raw).hexdigest(),
        )
