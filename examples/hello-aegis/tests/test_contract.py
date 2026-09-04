import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class HelloAegisContractTests(unittest.TestCase):
    def test_build_inputs_are_version_and_digest_pinned(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        requirements = (ROOT / "requirements.txt").read_text().splitlines()
        self.assertRegex(dockerfile.splitlines()[0], r"^FROM python:3\.12-slim@sha256:[0-9a-f]{64}$")
        self.assertTrue(requirements)
        self.assertTrue(all("==" in line for line in requirements if line.strip()))

    def test_compose_has_no_host_port_or_external_network(self):
        text = (ROOT / "compose.yaml").read_text()
        self.assertNotIn("ports:", text)
        self.assertIn("internal: true", text)

    def test_container_is_hardened(self):
        text = (ROOT / "compose.yaml").read_text()
        self.assertIn("read_only: true", text)
        self.assertIn("no-new-privileges:true", text)
        self.assertIn("- ALL", text)

    def test_image_override_is_single_purpose_and_legacy_safe(self):
        text = (ROOT / "compose.yaml").read_text()
        self.assertIn(
            "image: ${AEGIS_HELLO_AEGIS_IMAGE:-aegis/hello-aegis:0.1.0}", text,
        )

    def test_manifest_and_runtime_versions_match(self):
        manifest = json.loads((ROOT / "aegis-plugin.yaml").read_text())
        server = (ROOT / "server.py").read_text()
        self.assertIn(f'"version": "{manifest["metadata"]["version"]}"', server)

    def test_runtime_requires_service_token(self):
        self.assertIn('if not TOKEN:', (ROOT / "server.py").read_text())

    def test_metrics_and_lifecycle_contracts_are_declared(self):
        manifest = json.loads((ROOT / "aegis-plugin.yaml").read_text())
        self.assertEqual(manifest["spec"]["health"]["metrics"], "/metrics")
        self.assertEqual(set(manifest["spec"]["lifecycle"]["supports"]), {"disable", "upgrade", "rollback", "remove", "restart"})
        self.assertEqual(manifest["spec"]["lifecycle"]["restart"]["action"], "plugin.restart")
        self.assertEqual(manifest["spec"]["health"]["logs"], "/logs")


if __name__ == "__main__":
    unittest.main()
