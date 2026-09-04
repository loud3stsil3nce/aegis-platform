import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class ImmutableDeploymentApiContractTests(unittest.TestCase):
    def test_api_is_narrow_internal_and_strict(self):
        api = (ROOT / "deployment_api.py").read_text()
        self.assertIn('ConfigDict(extra="forbid")', api)
        self.assertIn('@app.post("/v1/image-deployments")', api)
        self.assertIn('@app.post("/v1/image-deployments/{proposal_id}/approve")', api)
        self.assertIn('@app.post("/v1/image-deployments/{proposal_id}/execute")', api)
        for forbidden in (
            "container", "rollback_image", "expected_current_image",
            '@app.post("/exec', '@app.post("/shell', '@app.post("/containers',
        ):
            self.assertNotIn(forbidden, api)

    def test_server_uses_file_credentials_and_bounded_config(self):
        server = (ROOT / "deployment_server.py").read_text()
        self.assertIn("AEGIS_IMAGE_DEPLOYMENT_PROXY_TOKEN_FILE", server)
        self.assertIn("MAX_CONFIG_BYTES = 16_384", server)
        self.assertNotIn('os.getenv("AEGIS_IMAGE_DEPLOYMENT_PROXY_TOKEN")', server)

    def test_container_and_compose_are_hardened(self):
        dockerfile = (ROOT / "Dockerfile.deployment-api").read_text()
        self.assertRegex(dockerfile.splitlines()[0], r"^FROM python:3\.12-slim@sha256:[0-9a-f]{64}$")
        self.assertIn("USER deployment-api", dockerfile)
        compose = (ROOT.parents[1] / "deploy" / "compose.docker-deployment.yaml").read_text()
        self.assertIn("image-deployment-api:", compose)
        self.assertNotIn("8014:8014", compose)
        self.assertIn("image-deployment-state:/var/lib/aegis-image-deployment", compose)


if __name__ == "__main__": unittest.main()
