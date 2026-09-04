import tempfile
import unittest
from pathlib import Path

from core.change_management.deployment_auth import (
    DeploymentActor, DeploymentActorAuthenticator, DeploymentActorAuthorizationError,
)
from core.change_management.deployment_executor import OneServiceDeploymentExecutor
from core.change_management.deployment_policy import DeploymentPolicy, DeploymentPolicyError
from core.change_management.deployment_proposals import DeploymentProposalStore
from core.change_management.deployment_proposals import DeploymentApprovalError
from core.change_management.deployment_service import ImmutableDeploymentService


LEGACY = "aegis/hello-aegis:0.1.0"
DIGEST = "sha256:" + "a" * 64
IMAGE = "ghcr.io/loud3stsil3nce/aegis-hello-aegis@" + DIGEST


class Adapter:
    def __init__(self):
        self.image = LEGACY
        self.state_calls = []
        self.deployments = []

    def current_image(self, service):
        self.state_calls.append(service)
        return self.image

    def deploy_image(self, service, image_reference, idempotency_key):
        self.deployments.append((service, image_reference, idempotency_key))
        self.image = image_reference

    def wait_healthy(self, service, timeout_seconds): return True


class ImmutableDeploymentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = DeploymentProposalStore(Path(self.temp.name) / "state.sqlite3")
        self.addCleanup(self.store.close)
        self.adapter = Adapter()
        policy = DeploymentPolicy.from_dict({"targets": [{
            "pluginId": "hello-aegis", "service": "hello-aegis",
            "imageRepository": "ghcr.io/loud3stsil3nce/aegis-hello-aegis",
            "bootstrapCurrentImage": LEGACY,
        }]})
        executor = OneServiceDeploymentExecutor(store=self.store, adapter=self.adapter)
        self.service = ImmutableDeploymentService(policy=policy, store=self.store, executor=executor)
        self.requester = DeploymentActor("requester", frozenset({"requester"}))
        self.approver = DeploymentActor("approver", frozenset({"approver"}))
        self.executor = DeploymentActor("executor", frozenset({"executor"}))

    def propose(self, actor=None):
        return self.service.propose(
            plugin_id="hello-aegis", git_sha="b" * 40, image_digest=DIGEST,
            health_timeout_seconds=60, ttl_seconds=900,
            actor=actor or self.requester,
        )

    def test_proposal_derives_current_image_and_roles_are_separate(self):
        proposal = self.propose()
        self.assertEqual(proposal["expected_current_image"], LEGACY)
        self.assertEqual(self.adapter.state_calls, ["hello-aegis"])
        with self.assertRaises(DeploymentActorAuthorizationError):
            self.propose(self.approver)
        self.service.approve(proposal["proposal_id"], actor=self.approver)
        result = self.service.execute(proposal["proposal_id"], actor=self.executor)
        self.assertEqual(result.image_reference, IMAGE)
        self.assertEqual(self.adapter.deployments[0][2], proposal["proposal_id"] + ":deploy")

    def test_unknown_target_never_reaches_runtime_adapter(self):
        with self.assertRaises(DeploymentPolicyError):
            self.service.propose(
                plugin_id="other", git_sha="b" * 40, image_digest=DIGEST,
                health_timeout_seconds=60, ttl_seconds=900, actor=self.requester,
            )
        self.assertEqual(self.adapter.state_calls, [])

    def test_requester_cannot_inspect_another_requesters_proposal(self):
        proposal = self.propose()
        other = DeploymentActor("other", frozenset({"requester"}))
        with self.assertRaises(DeploymentActorAuthorizationError):
            self.service.get(proposal["proposal_id"], actor=other)

    def test_self_approval_is_rejected_and_attributed(self):
        dual = DeploymentActor("dual", frozenset({"requester", "approver"}))
        proposal = self.propose(dual)
        with self.assertRaises(DeploymentApprovalError):
            self.service.approve(proposal["proposal_id"], actor=dual)
        event = self.store.connection.execute(
            "SELECT status,actor FROM deployment_events WHERE proposal_id=? ORDER BY rowid DESC LIMIT 1",
            (proposal["proposal_id"],),
        ).fetchone()
        self.assertEqual(tuple(event), ("APPROVAL_DENIED", "dual"))


class DeploymentActorAuthenticatorTests(unittest.TestCase):
    def test_file_tokens_authenticate_without_retaining_plaintext(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "token"; path.write_text("secret-token\n")
            auth = DeploymentActorAuthenticator([{
                "id": "requester", "roles": ["requester"], "tokenFile": str(path),
            }])
            self.assertEqual(auth.authenticate("Bearer secret-token").actor_id, "requester")
            self.assertNotIn("secret-token", repr(auth._actors))
            for header in (None, "Bearer wrong", "Bearer "):
                with self.subTest(header=header), self.assertRaises(DeploymentActorAuthorizationError):
                    auth.authenticate(header)


if __name__ == "__main__": unittest.main()
