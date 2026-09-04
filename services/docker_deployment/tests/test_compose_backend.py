import json
import subprocess
import unittest

from services.docker_deployment.compose_backend import ComposeBackend, ComposeBackendError
from services.docker_deployment.policy import ProxyTarget


IMAGE = "ghcr.io/loud3stsil3nce/aegis-hello-aegis@sha256:" + "a" * 64
LEGACY = "aegis/hello-aegis:0.1.0"
LEGACY_ID = "sha256:" + "e" * 64
TARGET = ProxyTarget(
    "hello-aegis", "hello-aegis-hello-aegis-1",
    "ghcr.io/loud3stsil3nce/aegis-hello-aegis", "hello-aegis",
    "/srv/hello-aegis", "/srv/hello-aegis/compose.yaml",
    "AEGIS_HELLO_AEGIS_IMAGE", LEGACY, LEGACY_ID,
)


class Runner:
    def __init__(self, outputs=()):
        self.outputs = list(outputs); self.calls = []
    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout=self.outputs.pop(0) if self.outputs else "", stderr="")


class ComposeBackendTests(unittest.TestCase):
    def test_state_uses_only_exact_container_and_parses_bounded_fields(self):
        state = {"Status": "running", "Health": {"Status": "healthy"}}
        runner = Runner([json.dumps(state) + "|" + json.dumps(IMAGE)])
        result = ComposeBackend(runner=runner).state(TARGET)
        self.assertEqual(result, {
            "target": "hello-aegis", "status": "running", "health": "healthy", "image": IMAGE,
        })
        self.assertEqual(runner.calls[0][0][-1], "hello-aegis-hello-aegis-1")

    def test_apply_pulls_exact_digest_and_recreates_only_bound_service(self):
        runner = Runner()
        ComposeBackend(runner=runner).apply(TARGET, IMAGE, "deploy")
        self.assertEqual(runner.calls[0][0], ["docker", "pull", IMAGE])
        compose, options = runner.calls[1]
        self.assertEqual(
            compose[-5:], ["-d", "--no-deps", "--no-build", "--force-recreate", "hello-aegis"],
        )
        self.assertEqual(compose[0], "docker-compose")
        self.assertEqual(options["env"]["AEGIS_HELLO_AEGIS_IMAGE"], IMAGE)
        self.assertNotIn("shell", options)

    def test_bootstrap_rollback_verifies_local_image_id_without_pull(self):
        runner = Runner([LEGACY_ID])
        ComposeBackend(runner=runner).apply(TARGET, LEGACY, "rollback")
        self.assertEqual(
            runner.calls[0][0],
            ["docker", "image", "inspect", "--format", "{{.Id}}", LEGACY],
        )
        self.assertEqual(runner.calls[1][0][0], "docker-compose")
        with self.assertRaisesRegex(ComposeBackendError, "identity changed"):
            ComposeBackend(runner=Runner(["sha256:" + "f" * 64])).apply(
                TARGET, LEGACY, "rollback",
            )

    def test_bad_or_oversized_state_fails_closed(self):
        for output in ("bad", "x" * 20_000):
            with self.subTest(output=output[:10]), self.assertRaises(ComposeBackendError):
                ComposeBackend(runner=Runner([output])).state(TARGET)


if __name__ == "__main__":
    unittest.main()
