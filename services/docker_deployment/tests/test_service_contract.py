import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class DeploymentProxyServiceContractTests(unittest.TestCase):
    def test_routes_are_narrow_and_schema_forbids_extra_fields(self):
        text = (ROOT / "main.py").read_text()
        self.assertIn('@app.get("/v1/targets/{service}/state"', text)
        self.assertIn('@app.post("/v1/targets/{service}/image"', text)
        self.assertIn('ConfigDict(extra="forbid")', text)
        for route in ("/exec", "/containers", "/remove", "/volumes", "/networks"):
            self.assertNotIn(f'@app.post("{route}', text)

    def test_auth_is_file_only_and_bodies_outputs_are_bounded(self):
        text = (ROOT / "main.py").read_text()
        self.assertIn("AEGIS_DEPLOYMENT_PROXY_TOKEN_FILE", text)
        self.assertNotIn('os.getenv("AEGIS_DEPLOYMENT_PROXY_TOKEN")', text)
        self.assertIn("MAX_BODY_BYTES = 4_096", text)
        backend = (ROOT / "compose_backend.py").read_text()
        self.assertIn("MAX_COMMAND_OUTPUT_BYTES = 16_384", backend)
        self.assertIn('"--no-deps"', backend)
        self.assertIn('"--no-build"', backend)

    def test_container_is_pinned_non_root_and_has_no_entrypoint_shell(self):
        text = (ROOT / "Dockerfile").read_text()
        self.assertRegex(text.splitlines()[0], r"^FROM python:3\.12-slim@sha256:[0-9a-f]{64}$")
        self.assertIn("USER deployment", text)
        self.assertIn('CMD ["uvicorn"', text)

    def test_disposable_fixture_is_not_the_production_project(self):
        policy = (ROOT / "integration" / "policy.json").read_text()
        self.assertIn('"composeProject": "aegis-deployment-disposable"', policy)
        self.assertNotIn("hello-aegis-hello-aegis-1", policy)


if __name__ == "__main__":
    unittest.main()
