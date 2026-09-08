import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.deployment import ApprovalError, RestartExecutor, RestartProposalStore


class FakeAdapter:
    def __init__(self, healthy=True):
        self.healthy, self.restarts = healthy, []
        self.states = {"hello-container": {"status": "running", "started_at": "before"}}

    def state(self, target):
        if target not in self.states:
            raise RuntimeError("target unavailable")
        return dict(self.states[target])

    def restart(self, target, idempotency_key):
        self.restarts.append((target, idempotency_key))

    def wait_ready(self, target, timeout_seconds):
        return {"status": "running", "health": "healthy" if self.healthy else "unhealthy", "started_at": "after"}


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = RestartProposalStore(Path(self.temp.name) / "deployment.sqlite3"); self.addCleanup(self.store.close)
        self.adapter = FakeAdapter()
        self.executor = RestartExecutor(self.store, self.adapter, {"hello-aegis": {"action": "plugin.restart", "target": "hello-container"}})
        self.expected = {"status": "running", "started_at": "before"}
        self.args = {"timeout_seconds": 30}

    def proposal(self, requester="requester"):
        return self.store.propose(plugin_id="hello-aegis", target="hello-container", requested_by=requester, expected_state=self.expected, timeout_seconds=30)

    def approve(self, proposal):
        self.store.approve(proposal, approved_by="approver")

    def test_healthy_restart_consumes_once_and_audits(self):
        proposal = self.proposal(); self.approve(proposal)
        result = self.executor.execute(proposal, plugin_id="hello-aegis", arguments=self.args, actor="executor")
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(self.adapter.restarts, [("hello-container", f"restart:{proposal}")])
        event = self.store.connection.execute("select * from restart_events where status='SUCCESS'").fetchone()
        self.assertEqual((event["plugin_id"], event["status"]), ("hello-aegis", "SUCCESS"))
        with self.assertRaises(ApprovalError):
            self.executor.execute(proposal, plugin_id="hello-aegis", arguments=self.args, actor="executor")

    def test_self_approval_fails(self):
        proposal = self.proposal("same")
        with self.assertRaisesRegex(ApprovalError, "own action"):
            self.store.approve(proposal, approved_by="same")

    def test_expiry_fails(self):
        proposal = self.proposal()
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.store.connection.execute("update restart_proposals set expires_at=? where proposal_id=?", (expired, proposal)); self.store.connection.commit()
        with self.assertRaisesRegex(ApprovalError, "expired"):
            self.store.approve(proposal, approved_by="approver")
        self.assertEqual(self.store.get(proposal)["status"], "EXPIRED")
        self.assertEqual(
            self.store.connection.execute(
                "select status from restart_events where proposal_id=? order by created_at desc limit 1",
                (proposal,),
            ).fetchone()[0],
            "EXPIRED",
        )

    def test_changed_arguments_and_state_fail(self):
        proposal = self.proposal(); self.approve(proposal)
        with self.assertRaises(ApprovalError):
            self.executor.execute(proposal, plugin_id="hello-aegis", arguments={"timeout_seconds": 31}, actor="executor")
        proposal = self.proposal(); self.approve(proposal)
        self.adapter.states["hello-container"]["started_at"] = "changed"
        with self.assertRaises(ApprovalError):
            self.executor.execute(proposal, plugin_id="hello-aegis", arguments=self.args, actor="executor")

    def test_undeclared_or_other_plugin_cannot_restart(self):
        proposal = self.proposal(); self.approve(proposal)
        with self.assertRaisesRegex(ApprovalError, "no declared"):
            self.executor.execute(proposal, plugin_id="other", arguments=self.args, actor="executor")
        self.assertEqual(self.adapter.restarts, [])

    def test_failed_readiness_has_actionable_recovery_and_audit(self):
        self.adapter.healthy = False
        proposal = self.proposal(); self.approve(proposal)
        result = self.executor.execute(proposal, plugin_id="hello-aegis", arguments=self.args, actor="executor")
        self.assertEqual(result.status, "FAILED")
        self.assertIn("inspect hello-aegis", result.recovery)
        statuses = [row[0] for row in self.store.connection.execute("select status from restart_events order by created_at")]
        self.assertIn("FAILED", statuses)

    def test_audit_failure_blocks_restart_dispatch(self):
        proposal = self.proposal(); self.approve(proposal)
        original_record = self.store.record
        self.store.record = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable"))
        self.addCleanup(setattr, self.store, "record", original_record)
        with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
            self.executor.execute(proposal, plugin_id="hello-aegis", arguments=self.args, actor="executor")
        self.assertEqual(self.adapter.restarts, [])


if __name__ == "__main__":
    unittest.main()
