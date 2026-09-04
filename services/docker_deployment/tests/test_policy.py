import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from services.docker_deployment.policy import (
    DeploymentIdempotencyStore,
    DeploymentProxyPolicy,
    DeploymentProxyPolicyError,
)


REPOSITORY = "ghcr.io/loud3stsil3nce/aegis-hello-aegis"
IMAGE = REPOSITORY + "@sha256:" + "a" * 64


class DeploymentProxyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.policy = DeploymentProxyPolicy.from_dict({"targets": [{
            "service": "hello-aegis", "container": "hello-aegis-hello-aegis-1",
            "imageRepository": REPOSITORY,
            "composeProject": "hello-aegis", "projectDirectory": "/srv/hello-aegis",
            "composeFile": "/srv/hello-aegis/compose.yaml",
            "imageVariable": "AEGIS_HELLO_AEGIS_IMAGE",
        }]})
        self.store = DeploymentIdempotencyStore(Path(self.temp.name) / "idempotency.sqlite3")
        self.key = f"{uuid.uuid4()}:deploy"

    def request(self, **overrides):
        values = {"service": "hello-aegis", "image_reference": IMAGE, "idempotency_key": self.key}
        values.update(overrides)
        return self.policy.validate(**values)

    def test_exact_target_digest_and_proposal_operation_are_bound(self):
        request = self.request()
        self.assertEqual(request.target.container, "hello-aegis-hello-aegis-1")
        self.assertEqual(request.operation, "deploy")

    def test_unknown_mutable_cross_repository_and_bad_keys_fail(self):
        cases = (
            {"service": "database"},
            {"image_reference": REPOSITORY + ":latest"},
            {"image_reference": "ghcr.io/other/image@sha256:" + "a" * 64},
            {"idempotency_key": "not-a-proposal:deploy"},
            {"idempotency_key": f"{uuid.uuid4()}:remove"},
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(DeploymentProxyPolicyError):
                self.request(**values)

    def test_external_policy_rejects_identifiers_variables_and_paths(self):
        base = {
            "service": "hello-aegis", "container": "hello-aegis-hello-aegis-1",
            "imageRepository": REPOSITORY, "composeProject": "hello-aegis",
            "projectDirectory": "/srv/hello-aegis",
            "composeFile": "/srv/hello-aegis/compose.yaml",
            "imageVariable": "AEGIS_HELLO_AEGIS_IMAGE",
        }
        for changed in (
            {"service": "../other"}, {"imageRepository": "UPPER/repo"},
            {"imageVariable": "BAD-NAME"}, {"projectDirectory": "relative"},
            {"composeFile": "/srv/other/compose.yaml"},
            {"composeFile": "/srv/hello-aegis/../compose.yaml"},
        ):
            value = {**base, **changed}
            with self.subTest(changed=changed), self.assertRaises(DeploymentProxyPolicyError):
                DeploymentProxyPolicy.from_dict({"targets": [value]})

    def test_applied_replay_is_safe_and_changed_binding_fails(self):
        request = self.request()
        self.assertEqual(self.store.claim(request), "CLAIMED")
        self.store.finish(self.key, "APPLIED")
        self.assertEqual(self.store.claim(request), "APPLIED")
        changed = self.request(image_reference=REPOSITORY + "@sha256:" + "b" * 64)
        with self.assertRaisesRegex(DeploymentProxyPolicyError, "another operation"):
            self.store.claim(changed)

    def test_in_progress_and_failed_are_fail_closed_across_connections(self):
        request = self.request()
        self.store.claim(request)
        with self.assertRaisesRegex(DeploymentProxyPolicyError, "indeterminate"):
            DeploymentIdempotencyStore(self.store.path).claim(request)
        self.store.finish(self.key, "FAILED")
        with self.assertRaisesRegex(DeploymentProxyPolicyError, "indeterminate"):
            DeploymentIdempotencyStore(self.store.path).claim(request)

    def test_concurrent_claim_has_only_one_winner(self):
        request = self.request()
        def claim(_):
            try:
                return self.store.claim(request)
            except DeploymentProxyPolicyError:
                return "DENIED"
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim, range(16)))
        self.assertEqual(results.count("CLAIMED"), 1)
        self.assertEqual(results.count("DENIED"), 15)


if __name__ == "__main__":
    unittest.main()
