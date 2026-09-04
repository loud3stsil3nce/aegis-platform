import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.change_management import (
    DeploymentApprovalError,
    DeploymentPolicy,
    DeploymentProposalStore,
)


CURRENT = "ghcr.io/loud3stsil3nce/aegis-core@sha256:" + "a" * 64


class DeploymentProposalTests(unittest.TestCase):
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

    def propose(self, requester="requester"):
        return self.store.propose(plan=self.plan, requested_by=requester)

    def test_exact_plan_is_approved_consumed_once_and_audited(self):
        proposal = self.propose()
        self.store.approve(proposal["proposal_id"], approved_by="approver", current_image=CURRENT)
        consumed = self.store.consume(
            proposal["proposal_id"], plan=self.plan, current_image=CURRENT, actor="executor",
        )
        self.assertEqual(consumed["status"], "CONSUMED")
        with self.assertRaises(DeploymentApprovalError):
            self.store.consume(
                proposal["proposal_id"], plan=self.plan, current_image=CURRENT, actor="executor",
            )
        statuses = {row[0] for row in self.store.connection.execute("select status from deployment_events")}
        self.assertTrue({"PROPOSED", "APPROVED", "CONSUMED", "EXECUTION_DENIED"} <= statuses)

    def test_self_approval_and_changed_current_image_fail(self):
        own = self.propose("same")
        with self.assertRaisesRegex(DeploymentApprovalError, "own deployment"):
            self.store.approve(own["proposal_id"], approved_by="same", current_image=CURRENT)
        stale = self.propose()
        with self.assertRaisesRegex(DeploymentApprovalError, "current image changed"):
            self.store.approve(stale["proposal_id"], approved_by="approver", current_image=CURRENT.replace("a", "d"))
        self.assertEqual(self.store.get(stale["proposal_id"])["status"], "STALE")

    def test_all_actor_boundaries_require_attribution(self):
        with self.assertRaisesRegex(DeploymentApprovalError, "attributable actor"):
            self.propose("   ")
        proposal = self.propose()
        with self.assertRaisesRegex(DeploymentApprovalError, "attributable actor"):
            self.store.approve(proposal["proposal_id"], approved_by="", current_image=CURRENT)
        with self.assertRaisesRegex(DeploymentApprovalError, "attributable actor"):
            self.store.record_execution(proposal["proposal_id"], status="FAILED", actor="", detail="x")

    def test_expiry_is_durable(self):
        proposal = self.propose()
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.store.connection.execute(
            "update deployment_proposals set expires_at=? where proposal_id=?",
            (expired, proposal["proposal_id"]),
        ); self.store.connection.commit()
        with self.assertRaisesRegex(DeploymentApprovalError, "expired"):
            self.store.approve(proposal["proposal_id"], approved_by="approver", current_image=CURRENT)
        self.assertEqual(self.store.get(proposal["proposal_id"])["status"], "EXPIRED")

    def test_altered_plan_stale_image_and_replay_cannot_consume(self):
        for plan, current in (
            (replace(self.plan, git_sha="d" * 40), CURRENT),
            (replace(self.plan, health_timeout_seconds=61), CURRENT),
            (self.plan, CURRENT.replace("a", "d")),
        ):
            proposal = self.propose()
            self.store.approve(proposal["proposal_id"], approved_by="approver", current_image=CURRENT)
            with self.assertRaises(DeploymentApprovalError):
                self.store.consume(proposal["proposal_id"], plan=plan, current_image=current, actor="executor")

    def test_execution_events_are_bounded(self):
        proposal = self.propose()
        self.store.record_execution(proposal["proposal_id"], status="ROLLED_BACK", actor="executor", detail="x" * 2000)
        detail = self.store.connection.execute("select detail from deployment_events where status='ROLLED_BACK'").fetchone()[0]
        self.assertEqual(len(detail), 1000)
        with self.assertRaises(DeploymentApprovalError):
            self.store.record_execution(proposal["proposal_id"], status="UNKNOWN", actor="executor", detail="bad")


if __name__ == "__main__":
    unittest.main()
