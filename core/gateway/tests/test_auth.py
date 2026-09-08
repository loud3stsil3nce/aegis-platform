import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO_ROOT), str(REPO_ROOT / "sdk/python")]

from aegis_plugin import load_manifest, validate_manifest
from core.gateway import GatewayAuthorizationError, authorize_plugin
from core.registry import PluginRegistry


class GatewayAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.registry = PluginRegistry(self.temporary.name)
        self.addCleanup(self.registry.close)
        manifest = validate_manifest(load_manifest(REPO_ROOT / "examples/hello-aegis/aegis-plugin.yaml"))
        self.token = self.registry.install(manifest)

    def test_exact_identity_token_and_capability_are_authorized(self):
        context = authorize_plugin(self.registry, "hello-aegis", self.token, "hello.read")
        self.assertEqual(context, {"identity": "plugin:hello-aegis", "pluginId": "hello-aegis", "capability": "hello.read", "risk": "R0"})

    def test_wrong_token_and_cross_plugin_identity_fail(self):
        with self.assertRaisesRegex(GatewayAuthorizationError, "invalid"):
            authorize_plugin(self.registry, "hello-aegis", "wrong", "hello.read")
        with self.assertRaisesRegex(GatewayAuthorizationError, "unknown"):
            authorize_plugin(self.registry, "other-plugin", self.token, "hello.read")

    def test_undeclared_capability_fails(self):
        with self.assertRaisesRegex(GatewayAuthorizationError, "not declared"):
            authorize_plugin(self.registry, "hello-aegis", self.token, "docker.restart")

    def test_disabled_identity_fails(self):
        self.registry.set_status("hello-aegis", "disabled")
        with self.assertRaisesRegex(GatewayAuthorizationError, "disabled"):
            authorize_plugin(self.registry, "hello-aegis", self.token, "hello.read")


if __name__ == "__main__":
    unittest.main()
