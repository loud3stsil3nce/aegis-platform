import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO_ROOT), str(REPO_ROOT / "sdk" / "python")]

from aegis_plugin import load_manifest, permission_diff, validate_manifest
from core.registry import PluginRegistry, RegistryError


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.registry = PluginRegistry(self.temporary.name)
        self.addCleanup(self.registry.close)
        self.manifest = validate_manifest(load_manifest(REPO_ROOT / "examples/hello-aegis/aegis-plugin.yaml"))

    def test_token_is_hashed_and_identity_is_isolated(self):
        token = self.registry.install(self.manifest)
        self.assertTrue(self.registry.verify_token("hello-aegis", token))
        self.assertFalse(self.registry.verify_token("hello-aegis", "wrong"))
        row = self.registry.connection.execute("SELECT identity,token_hash FROM plugins").fetchone()
        self.assertEqual(row["identity"], "plugin:hello-aegis")
        self.assertNotEqual(row["token_hash"], token)

    def test_duplicate_install_fails(self):
        self.registry.install(self.manifest)
        with self.assertRaisesRegex(RegistryError, "already installed"):
            self.registry.install(self.manifest)

    def test_permission_expansion_requires_review(self):
        self.registry.install(self.manifest)
        candidate = copy.deepcopy(self.manifest)
        candidate["metadata"]["version"] = "0.2.0"
        candidate["spec"]["permissions"]["secrets"] = ["NEW_API_KEY"]
        expansion = bool(permission_diff(self.manifest, candidate)["added_secrets"])
        with self.assertRaisesRegex(RegistryError, "explicit review"):
            self.registry.upgrade(candidate, expansion, approved=False)
        self.assertEqual(self.registry.get("hello-aegis")["version"], "0.1.0")

    def test_upgrade_and_rollback_preserve_history(self):
        self.registry.install(self.manifest)
        candidate = copy.deepcopy(self.manifest)
        candidate["metadata"]["version"] = "0.2.0"
        self.registry.upgrade(candidate, permission_expansion=False, approved=False)
        self.assertEqual(self.registry.get("hello-aegis")["version"], "0.2.0")
        self.registry.rollback("hello-aegis", "0.1.0")
        self.assertEqual(self.registry.get("hello-aegis")["version"], "0.1.0")

    def test_remove_requires_disable_and_leaves_no_plugin(self):
        self.registry.install(self.manifest)
        with self.assertRaisesRegex(RegistryError, "disabled"):
            self.registry.remove("hello-aegis")
        self.registry.set_status("hello-aegis", "disabled")
        self.registry.remove("hello-aegis")
        self.assertEqual(self.registry.list(), [])


if __name__ == "__main__":
    unittest.main()
