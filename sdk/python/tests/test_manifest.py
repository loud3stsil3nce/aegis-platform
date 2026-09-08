import copy
import sys
import unittest
from pathlib import Path

SDK_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(SDK_ROOT))

from aegis_plugin import ManifestError, is_core_compatible, load_manifest, permission_diff, validate_manifest


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.path = REPO_ROOT / "examples" / "hello-aegis" / "aegis-plugin.yaml"
        self.manifest = load_manifest(self.path)

    def test_hello_manifest_validates_without_optional_dependencies(self):
        self.assertEqual(validate_manifest(self.manifest)["metadata"]["id"], "hello-aegis")

    def test_legacy_sse_is_rejected(self):
        changed = copy.deepcopy(self.manifest)
        changed["spec"]["mcp"]["transport"] = "sse"
        with self.assertRaisesRegex(ManifestError, "streamable-http"):
            validate_manifest(changed)

    def test_runtime_specific_fields_are_required(self):
        changed = copy.deepcopy(self.manifest)
        changed["spec"]["runtime"] = {"mode": "managed-container"}
        with self.assertRaisesRegex(ManifestError, "composeFile"):
            validate_manifest(changed)

    def test_duplicate_secret_declarations_are_rejected(self):
        changed = copy.deepcopy(self.manifest)
        changed["spec"]["permissions"]["secrets"] = ["API_KEY", "API_KEY"]
        with self.assertRaisesRegex(ManifestError, "duplicates"):
            validate_manifest(changed)

    def test_permission_expansion_is_reported(self):
        changed = copy.deepcopy(self.manifest)
        changed["spec"]["permissions"]["secrets"] = ["EXAMPLE_API_KEY"]
        changed["spec"]["permissions"]["capabilities"].append({"name": "hello.write", "risk": "R3"})
        diff = permission_diff(self.manifest, changed)
        self.assertEqual(diff["added_secrets"], ["EXAMPLE_API_KEY"])
        self.assertEqual(diff["added_capabilities"], ["hello.write:R3"])

    def test_core_compatibility_grammar(self):
        self.assertTrue(is_core_compatible("^0.1.0", "0.1.4"))
        self.assertFalse(is_core_compatible("^0.1.0", "1.0.0"))
        self.assertTrue(is_core_compatible(">=0.1.0", "2.0.0"))
        self.assertFalse(is_core_compatible("~0.1.0", "0.2.0"))


if __name__ == "__main__":
    unittest.main()
