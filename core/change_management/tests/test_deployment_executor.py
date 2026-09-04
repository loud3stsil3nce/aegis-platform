import tempfile
import unittest
from pathlib import Path

from core.change_management import (
    DeploymentExecutionError,
    DeploymentPolicy,
    DeploymentProposalStore,
    OneServiceDeploymentExecutor,
)


CURRENT = "ghcr.io/loud3stsil3nce/aegis-core@sha256:" + "a" * 64
NEW = "ghcr.io/loud3stsil3nce/aegis-core@sha256:" + "c" * 64


class FakeAdapter:
    def __init__(self, *, healthy=True, rollback_healthy=True, fail_rollback=False):
        self.image = CURRENT
        self.healthy = healthy
        self.rollback_healthy = rollback_healthy
        self.fail_rollback = fail_rollback
        self.deployments = []

    def current_image(self, service):
        return self.image

    def deploy_image(self, service, image_reference, idempotency_key):
        self.deployments.append((service, image_reference, idempotency_key))
        if image_reference == CURRENT and self.fail_rollback:
            raise RuntimeError("rollback failed")
        self.image = image_reference

    def wait_healthy(self, service, timeout_seconds):
        return self.rollback_healthy if self.image == CURRENT else self.healthy


class DeploymentExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = DeploymentProposalStore(Path(self.temp.name) / "deployments.sqlite3")
        self.addCleanup(self.store.close)
        policy = DeploymentPolicy.from_dict({"targets": [{
            "pluginId": "hello-aegis", "service": "hello-aegis",
            "imageRepository": "ghcr.io/loud3stsil3nce/aegis-core",
        }]})
        self.plan = policy.plan(
            plugin_id="hello-aegis", git_sha="b" * 40,
            image_digest="sha256:" + "c" * 64,
            expected_current_image=CURRENT, health_timeout_seconds=60,
        )

    def approved(self):
        proposal = self.store.propose(plan=self.plan, requested_by="requester")
        self.store.approve(proposal["proposal_id"], approved_by="approver", current_image=CURRENT)
        return proposal["proposal_id"]

    def statuses(self):
        return [row[0] for row in self.store.connection.execute("select status from deployment_events")]

    def test_healthy_exact_image_succeeds(self):
        adapter, proposal_id = FakeAdapter(), self.approved()
        result = OneServiceDeploymentExecutor(store=self.store, adapter=adapter).execute(
            proposal_id=proposal_id, plan=self.plan, actor="executor",
        )
        self.assertEqual(result.status, "HEALTHY")
        self.assertEqual(adapter.deployments, [("hello-aegis", NEW, f"{proposal_id}:deploy")])
        self.assertIn("HEALTHY", self.statuses())

    def test_unhealthy_deployment_rolls_back_and_verifies(self):
        adapter, proposal_id = FakeAdapter(healthy=False), self.approved()
        with self.assertRaisesRegex(DeploymentExecutionError, "rolled back"):
            OneServiceDeploymentExecutor(store=self.store, adapter=adapter).execute(
                proposal_id=proposal_id, plan=self.plan, actor="executor",
            )
        self.assertEqual(adapter.deployments, [
            ("hello-aegis", NEW, f"{proposal_id}:deploy"),
            ("hello-aegis", CURRENT, f"{proposal_id}:rollback"),
        ])
        self.assertEqual(adapter.image, CURRENT)
        self.assertIn("ROLLED_BACK", self.statuses())

    def test_unverified_or_failed_rollback_requires_manual_recovery(self):
        for adapter in (FakeAdapter(healthy=False, rollback_healthy=False), FakeAdapter(healthy=False, fail_rollback=True)):
            with self.subTest(adapter=adapter):
                proposal_id = self.approved()
                with self.assertRaisesRegex(DeploymentExecutionError, "manual recovery"):
                    OneServiceDeploymentExecutor(store=self.store, adapter=adapter).execute(
                        proposal_id=proposal_id, plan=self.plan, actor="executor",
                    )
                self.assertEqual(self.statuses()[-1], "FAILED")

    def test_stale_image_blocks_before_any_deployment(self):
        adapter, proposal_id = FakeAdapter(), self.approved()
        adapter.image = CURRENT.replace("a" * 64, "d" * 64)
        with self.assertRaises(Exception):
            OneServiceDeploymentExecutor(store=self.store, adapter=adapter).execute(
                proposal_id=proposal_id, plan=self.plan, actor="executor",
            )
        self.assertEqual(adapter.deployments, [])


if __name__ == "__main__":
    unittest.main()
