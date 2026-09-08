"""Unit tests for Phase D: governed PR merge and immutable deployment."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from core.change_management.deployment_auth import DeploymentActor
from core.change_management.deployment_executor import OneServiceDeploymentExecutor
from core.change_management.deployment_policy import DeploymentPolicy, DeploymentTarget
from core.change_management.deployment_proposals import DeploymentProposalStore
from core.change_management.deployment_service import ImmutableDeploymentService
from core.change_management.github_adapter import GitHubChangeAdapter, GitHubPullRequestResult
from core.change_management.jira_proposals import JiraProposalService, canonical, digest
from core.change_management.policy import ChangePolicy
from core.change_management.proposals import ChangeApprovalError, ChangeProposalStore


class FakeGitHubApi:
    def __init__(self):
        self.pr_merged = False
        self.merge_sha = "f00dbabe" * 5
        self.reviews = [{"state": "APPROVED", "user": {"login": "human-owner"}}]

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        if method == "GET" and "/pulls/" in path and "/reviews" in path:
            return self.reviews
        if method == "GET" and "/pulls/" in path:
            return {
                "state": "open",
                "mergeable": True,
                "mergeable_state": "clean",
                "head": {"sha": "1111222233334444555566667777888899990000"},
                "base": {"ref": "main", "sha": "aaaabbbbccccddddeeeeffff0000111122223333"},
            }
        if method == "PUT" and "/merge" in path:
            self.pr_merged = True
            return {"merged": True, "sha": self.merge_sha, "message": "Pull Request successfully merged"}
        return {}


class FakeDeploymentAdapter:
    def __init__(self, current: str = "aegis/hello-aegis:0.1.0"):
        self._current = current
        self.deploy_calls = []

    def current_image(self, service: str) -> str:
        return self._current

    def deploy_image(self, service: str, image_reference: str, idempotency_key: str) -> None:
        self.deploy_calls.append((service, image_reference, idempotency_key))
        self._current = image_reference

    def wait_healthy(self, service: str, timeout_seconds: int) -> bool:
        return True


class PhaseDTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "proposals.sqlite3"
        self.store = ChangeProposalStore(self.db_path)
        self.policy = ChangePolicy(
            repositories=frozenset({"loud3stsil3nce/aegis-platform"}),
            base_branches=frozenset({"main"}),
            allowed_path_prefixes=("docs/",),
            max_files=5,
            max_diff_bytes=10000,
            max_changed_lines=100,
        )
        self.fake_api = FakeGitHubApi()
        self.adapter = MagicMock(spec=GitHubChangeAdapter)
        self.service = JiraProposalService(self.store, self.policy, self.adapter)

        self.requester = DeploymentActor("alice", frozenset({"requester"}))
        self.approver = DeploymentActor("bob", frozenset({"approver"}))
        self.executor = DeploymentActor("charlie", frozenset({"executor"}))
        self.merger = DeploymentActor("dan", frozenset({"merge-approver"}))
        self.deployer = DeploymentActor("eve", frozenset({"approver"}))
        self.deploy_exec = DeploymentActor("frank", frozenset({"executor"}))

    def tearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    def _setup_executed_proposal(self) -> tuple[str, str]:
        files = {"docs/test.md": b"# Test\n"}
        self.adapter.read_snapshot.return_value = {"docs/test.md": None}
        prep = self.service.prepare(
            issue_key="KAN-126",
            fingerprint="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            labels=("aegis:github-proposal",),
            repository="loud3stsil3nce/aegis-platform",
            base_sha="aaaabbbbccccddddeeeeffff0000111122223333",
            files=files,
            actor=self.requester,
            ttl_seconds=3600,
        )
        pid = prep["proposal"]["proposal_id"]
        binding = prep["binding_sha256"]
        self.service.approve(pid, binding=binding, actor=self.approver)

        self.adapter.create_pull_request.return_value = GitHubPullRequestResult(
            repository="loud3stsil3nce/aegis-platform",
            branch=prep["proposal"]["proposed_branch"],
            commit_sha="1111222233334444555566667777888899990000",
            pull_request_number=42,
            pull_request_url="https://github.com/loud3stsil3nce/aegis-platform/pull/42",
        )
        self.service.execute(pid, binding=binding, actor=self.executor)
        return pid, binding

    def test_merge_requires_merge_approver_role(self):
        pid, binding = self._setup_executed_proposal()
        with self.assertRaises(PermissionError):
            self.service.merge_proposal(pid, binding=binding, actor=self.requester, github_api=self.fake_api)

    def test_merge_rejects_self_approval_from_requester(self):
        pid, binding = self._setup_executed_proposal()
        dual_actor = DeploymentActor("alice", frozenset({"merge-approver"}))
        with self.assertRaises(ChangeApprovalError):
            self.service.merge_proposal(pid, binding=binding, actor=dual_actor, github_api=self.fake_api)

    def test_merge_rejects_wrong_binding(self):
        pid, _ = self._setup_executed_proposal()
        with self.assertRaises(ChangeApprovalError):
            self.service.merge_proposal(pid, binding="wrong_binding", actor=self.merger, github_api=self.fake_api)

    def test_merge_success_and_audit(self):
        pid, binding = self._setup_executed_proposal()
        result = self.service.merge_proposal(pid, binding=binding, actor=self.merger, github_api=self.fake_api)
        self.assertTrue(result["merged"])
        self.assertEqual(result["merge_commit_sha"], self.fake_api.merge_sha)

        inspected = self.service.inspect(pid)
        self.assertEqual(inspected["lifecycle_state"], "MERGED")
        self.assertEqual(inspected["merge_commit_sha"], self.fake_api.merge_sha)

    def test_cannot_merge_twice(self):
        pid, binding = self._setup_executed_proposal()
        self.service.merge_proposal(pid, binding=binding, actor=self.merger, github_api=self.fake_api)
        with self.assertRaises(ChangeApprovalError):
            self.service.merge_proposal(pid, binding=binding, actor=self.merger, github_api=self.fake_api)

    def test_deployment_integration(self):
        pid, binding = self._setup_executed_proposal()
        merge_res = self.service.merge_proposal(pid, binding=binding, actor=self.merger, github_api=self.fake_api)
        merge_sha = merge_res["merge_commit_sha"]

        deploy_policy = DeploymentPolicy(
            targets={
                "hello-aegis": DeploymentTarget(
                    "hello-aegis", "hello-aegis", "ghcr.io/loud3stsil3nce/aegis-hello-aegis",
                    bootstrap_current_image="aegis/hello-aegis:0.1.0",
                )
            },
        )
        deploy_store = DeploymentProposalStore(Path(self.temp_dir.name) / "deployment.sqlite3")
        deploy_adapter = FakeDeploymentAdapter(current="aegis/hello-aegis:0.1.0")
        deploy_executor = OneServiceDeploymentExecutor(store=deploy_store, adapter=deploy_adapter)
        deploy_service = ImmutableDeploymentService(policy=deploy_policy, store=deploy_store, executor=deploy_executor)

        prop = deploy_service.propose(
            plugin_id="hello-aegis",
            git_sha=merge_sha,
            image_digest="sha256:" + "beef" * 16,
            health_timeout_seconds=30,
            ttl_seconds=300,
            actor=DeploymentActor("requester-user", frozenset({"requester"})),
        )
        dpid = prop["proposal_id"]

        deploy_service.approve(dpid, actor=self.deployer)
        deploy_result = deploy_service.execute(dpid, actor=self.deploy_exec)
        self.assertEqual(deploy_result.status, "HEALTHY")
        deploy_store.close()


if __name__ == "__main__":
    unittest.main()
