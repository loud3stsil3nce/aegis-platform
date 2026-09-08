import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from core.deployment import ActorAuthenticator, DeploymentService, RestartExecutor, RestartProposalStore
from core.deployment.api import create_app


class FakeAdapter:
    def __init__(self): self.restarts = []
    def state(self, target): return {"status": "running", "health": "healthy", "started_at": "before"}
    def restart(self, target, idempotency_key): self.restarts.append((target, idempotency_key))
    def wait_ready(self, target, timeout_seconds): return {"status": "running", "health": "healthy", "started_at": "after"}


class DeploymentApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        records = []
        for actor_id, role in (("alice", "requester"), ("bob", "approver"), ("runtime", "executor")):
            path = root / f"{actor_id}.token"; path.write_text(f"{actor_id}-token")
            records.append({"id": actor_id, "roles": [role], "tokenFile": str(path)})
        store = RestartProposalStore(root / "deployment.sqlite3"); self.addCleanup(store.close)
        self.adapter = FakeAdapter()
        executor = RestartExecutor(store, self.adapter, {"hello-aegis": {
            "action": "plugin.restart", "target": "hello-container", "healthTimeoutSeconds": 30,
        }})
        self.client = TestClient(create_app(DeploymentService(store, executor), ActorAuthenticator(records)))

    @staticmethod
    def auth(actor_id): return {"Authorization": f"Bearer {actor_id}-token"}

    def test_missing_and_wrong_tokens_are_rejected(self):
        self.assertEqual(self.client.post("/v1/restart-proposals", json={"plugin_id": "hello-aegis"}).status_code, 401)
        self.assertEqual(self.client.post("/v1/restart-proposals", json={"plugin_id": "hello-aegis"}, headers=self.auth("wrong")).status_code, 401)

    def test_http_flow_requires_three_authorized_actors(self):
        proposed = self.client.post(
            "/v1/restart-proposals", json={"plugin_id": "hello-aegis"}, headers=self.auth("alice"),
        )
        self.assertEqual(proposed.status_code, 200)
        proposal_id = proposed.json()["proposal_id"]
        self.assertEqual(self.client.post(
            f"/v1/restart-proposals/{proposal_id}/approve", headers=self.auth("alice"),
        ).status_code, 403)
        self.assertEqual(self.client.post(
            f"/v1/restart-proposals/{proposal_id}/execute", headers=self.auth("bob"),
        ).status_code, 403)
        self.assertEqual(self.client.post(
            f"/v1/restart-proposals/{proposal_id}/approve", headers=self.auth("bob"),
        ).status_code, 200)
        executed = self.client.post(
            f"/v1/restart-proposals/{proposal_id}/execute", headers=self.auth("runtime"),
        )
        self.assertEqual((executed.status_code, executed.json()["status"]), (200, "SUCCESS"))
        self.assertEqual(len(self.adapter.restarts), 1)

    def test_extra_fields_and_undeclared_plugins_fail_closed(self):
        response = self.client.post(
            "/v1/restart-proposals", json={"plugin_id": "hello-aegis", "target": "database"},
            headers=self.auth("alice"),
        )
        self.assertEqual(response.status_code, 422)
        response = self.client.post(
            "/v1/restart-proposals", json={"plugin_id": "other"}, headers=self.auth("alice"),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.adapter.restarts, [])

    def test_streamed_oversized_body_is_rejected(self):
        def chunks():
            yield b"x" * 5000
            yield b"y" * 5000
        response = self.client.post("/v1/restart-proposals", content=chunks(), headers=self.auth("alice"))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.adapter.restarts, [])

    def test_no_public_restart_or_generic_mutation_routes(self):
        for path in ("/restart", "/v1/restart", "/v1/docker", "/v1/exec", "/v1/deploy"):
            self.assertEqual(self.client.post(path, headers=self.auth("runtime")).status_code, 404)


if __name__ == "__main__":
    unittest.main()
