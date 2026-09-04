import json
import unittest
from pathlib import Path

from core.change_management.deployment_policy import DeploymentPolicy, DeploymentPolicyError


CURRENT = "ghcr.io/loud3stsil3nce/aegis-core@sha256:" + ("a" * 64)
LEGACY = "aegis/hello-aegis:0.1.0"


class DeploymentPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = DeploymentPolicy.from_dict({"targets": [{
            "pluginId": "hello-aegis", "service": "hello-aegis",
            "imageRepository": "ghcr.io/loud3stsil3nce/aegis-core",
            "bootstrapCurrentImage": LEGACY,
        }]})

    def test_plan_binds_one_service_commit_digest_and_rollback(self):
        plan = self.policy.plan(
            plugin_id="hello-aegis", git_sha="b" * 40,
            image_digest="sha256:" + ("c" * 64), expected_current_image=CURRENT,
            health_timeout_seconds=60,
        )
        self.assertEqual(plan.service, "hello-aegis")
        self.assertEqual(plan.rollback_image, CURRENT)
        self.assertTrue(plan.image_reference.endswith("c" * 64))

    def test_exact_bootstrap_current_image_is_the_only_mutable_exception(self):
        plan = self.policy.plan(
            plugin_id="hello-aegis", git_sha="b" * 40,
            image_digest="sha256:" + "c" * 64,
            expected_current_image=LEGACY, health_timeout_seconds=60,
        )
        self.assertEqual(plan.rollback_image, LEGACY)
        for image in ("aegis/hello-aegis:latest", "other/image:0.1.0"):
            with self.subTest(image=image), self.assertRaises(DeploymentPolicyError):
                self.policy.plan(
                    plugin_id="hello-aegis", git_sha="b" * 40,
                    image_digest="sha256:" + "c" * 64,
                    expected_current_image=image, health_timeout_seconds=60,
                )

    def test_unknown_service_and_mutable_images_fail_closed(self):
        cases = (
            {"plugin_id": "other", "git_sha": "b" * 40, "image_digest": "sha256:" + "c" * 64, "expected_current_image": CURRENT},
            {"plugin_id": "hello-aegis", "git_sha": "short", "image_digest": "sha256:" + "c" * 64, "expected_current_image": CURRENT},
            {"plugin_id": "hello-aegis", "git_sha": "b" * 40, "image_digest": "latest", "expected_current_image": CURRENT},
            {"plugin_id": "hello-aegis", "git_sha": "b" * 40, "image_digest": "sha256:" + "c" * 64, "expected_current_image": "ghcr.io/loud3stsil3nce/aegis-core:latest"},
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(DeploymentPolicyError):
                self.policy.plan(health_timeout_seconds=60, **values)

    def test_same_image_and_unbounded_health_timeout_fail(self):
        with self.assertRaises(DeploymentPolicyError):
            self.policy.plan(
                plugin_id="hello-aegis", git_sha="b" * 40,
                image_digest="sha256:" + "a" * 64,
                expected_current_image=CURRENT, health_timeout_seconds=60,
            )
        for timeout in (9, 301):
            with self.assertRaises(DeploymentPolicyError):
                self.policy.plan(
                    plugin_id="hello-aegis", git_sha="b" * 40,
                    image_digest="sha256:" + "c" * 64,
                    expected_current_image=CURRENT, health_timeout_seconds=timeout,
                )

    def test_policy_schema_rejects_unknown_fields_and_duplicates(self):
        for value in (
            {"targets": [], "extra": True},
            {"targets": [{"pluginId": "x", "service": "x", "imageRepository": "repo", "extra": True}]},
            {"targets": [{"pluginId": "x", "service": "x", "imageRepository": 7}]},
            {"targets": [{"pluginId": "x", "service": "x", "imageRepository": "repo", "bootstrapCurrentImage": "bad"}]},
            {"targets": [
                {"pluginId": "x", "service": "x", "imageRepository": "repo"},
                {"pluginId": "x", "service": "y", "imageRepository": "repo"},
            ]},
        ):
            with self.subTest(value=value), self.assertRaises(DeploymentPolicyError):
                DeploymentPolicy.from_dict(value)

    def test_repository_policy_file_is_strict_and_loadable(self):
        path = Path(__file__).parents[3] / "config" / "deployment-policy.json"
        policy = DeploymentPolicy.from_dict(json.loads(path.read_text()))
        self.assertEqual(
            policy.targets["hello-aegis"].image_repository,
            "ghcr.io/loud3stsil3nce/aegis-hello-aegis",
        )
        self.assertEqual(policy.targets["hello-aegis"].bootstrap_current_image, LEGACY)


if __name__ == "__main__":
    unittest.main()
