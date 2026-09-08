import tempfile
import unittest
from pathlib import Path

from core.deployment import (
    Actor, ActorAuthenticator, ActorAuthorizationError, ApprovalError,
    DeploymentService, RestartExecutor, RestartProposalStore,
)


class FakeAdapter:
    def __init__(self):
        self.restarts = []
        self.current = {"status": "running", "health": "healthy", "started_at": "before"}
    def state(self, target): return dict(self.current)
    def restart(self, target, idempotency_key):
        self.restarts.append((target, idempotency_key)); self.current["started_at"] = "after"
    def wait_ready(self, target, timeout_seconds): return dict(self.current)


class DeploymentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = RestartProposalStore(Path(self.temp.name) / "deployment.sqlite3")
        self.addCleanup(self.store.close)
        self.adapter = FakeAdapter()
        hooks = {"hello-aegis": {"action": "plugin.restart", "target": "hello-container", "healthTimeoutSeconds": 30}}
        self.service = DeploymentService(self.store, RestartExecutor(self.store, self.adapter, hooks))
        self.requester = Actor("requester", frozenset({"requester"}))
        self.approver = Actor("approver", frozenset({"approver"}))
        self.executor = Actor("executor", frozenset({"executor"}))

    def test_authorized_end_to_end_flow_records_actor_evidence(self):
        proposal = self.service.propose(plugin_id="hello-aegis", actor=self.requester)
        self.assertEqual(proposal["status"], "PENDING")
        approved = self.service.approve(proposal["proposal_id"], actor=self.approver)
        self.assertEqual(approved["approved_by"], "approver")
        result = self.service.execute(proposal["proposal_id"], actor=self.executor)
        self.assertEqual(result.status, "SUCCESS")
        events = list(self.store.connection.execute("select status,actor from restart_events order by created_at,event_id"))
        evidence = {(row["status"], row["actor"]) for row in events}
        self.assertTrue({("PROPOSED", "requester"), ("APPROVED", "approver"), ("EXECUTING", "executor"), ("SUCCESS", "executor")} <= evidence)

    def test_roles_are_enforced_at_every_boundary(self):
        with self.assertRaises(ActorAuthorizationError):
            self.service.propose(plugin_id="hello-aegis", actor=self.approver)
        proposal = self.service.propose(plugin_id="hello-aegis", actor=self.requester)
        with self.assertRaises(ActorAuthorizationError):
            self.service.approve(proposal["proposal_id"], actor=self.requester)
        with self.assertRaises(ActorAuthorizationError):
            self.service.execute(proposal["proposal_id"], actor=self.approver)
        self.assertEqual(self.adapter.restarts, [])

    def test_self_approval_still_fails_for_multi_role_actor(self):
        actor = Actor("same", frozenset({"requester", "approver"}))
        proposal = self.service.propose(plugin_id="hello-aegis", actor=actor)
        with self.assertRaisesRegex(ApprovalError, "own action"):
            self.service.approve(proposal["proposal_id"], actor=actor)
        event = self.store.connection.execute(
            "select status from restart_events where proposal_id=? order by created_at desc limit 1",
            (proposal["proposal_id"],),
        ).fetchone()
        self.assertEqual(event[0], "APPROVAL_DENIED")

    def test_replay_denial_is_audited(self):
        proposal = self.service.propose(plugin_id="hello-aegis", actor=self.requester)
        self.service.approve(proposal["proposal_id"], actor=self.approver)
        self.service.execute(proposal["proposal_id"], actor=self.executor)
        with self.assertRaises(ApprovalError):
            self.service.execute(proposal["proposal_id"], actor=self.executor)
        statuses = [row[0] for row in self.store.connection.execute(
            "select status from restart_events where proposal_id=?", (proposal["proposal_id"],)
        )]
        self.assertIn("EXECUTION_DENIED", statuses)

    def test_undeclared_plugin_fails_before_state_access(self):
        with self.assertRaisesRegex(ApprovalError, "no declared"):
            self.service.propose(plugin_id="other", actor=self.requester)


class ActorAuthenticatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.token = Path(self.temp.name) / "requester.token"
        self.token.write_text("secret-token\n")

    def test_file_backed_token_maps_to_actor_without_exposing_token(self):
        auth = ActorAuthenticator([{"id": "alice", "roles": ["requester"], "tokenFile": str(self.token)}])
        actor = auth.authenticate("Bearer secret-token")
        self.assertEqual((actor.actor_id, actor.roles), ("alice", frozenset({"requester"})))
        with self.assertRaises(ActorAuthorizationError):
            auth.authenticate("Bearer wrong")
        self.assertNotIn("secret-token", repr(auth.__dict__))

    def test_invalid_roles_and_oversized_tokens_fail_startup(self):
        with self.assertRaises(ValueError):
            ActorAuthenticator([{"id": "alice", "roles": ["admin"], "tokenFile": str(self.token)}])
        self.token.write_bytes(b"x" * 5000)
        with self.assertRaises(ValueError):
            ActorAuthenticator([{"id": "alice", "roles": ["requester"], "tokenFile": str(self.token)}])


if __name__ == "__main__":
    unittest.main()
