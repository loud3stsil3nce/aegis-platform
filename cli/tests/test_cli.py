import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "cli" / "aegis.py"
EXAMPLE = REPO_ROOT / "examples" / "hello-aegis"


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), *args], text=True, capture_output=True)

    def test_validate_example(self):
        result = self.run_cli("plugin", "validate", str(EXAMPLE))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hello-aegis@0.1.0", result.stdout)

    def test_inspect_exposes_names_not_secret_values(self):
        result = self.run_cli("plugin", "inspect", str(EXAMPLE))
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["secretNames"], [])
        self.assertNotIn("secretValues", summary)

    def test_init_does_not_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self.run_cli("plugin", "init", "sample", "--directory", directory)
            second = self.run_cli("plugin", "init", "sample", "--directory", directory)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 1)
            self.assertIn("refusing to overwrite", second.stderr)

    def test_permission_expansion_uses_distinct_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "aegis-plugin.yaml"
            manifest = json.loads((EXAMPLE / "aegis-plugin.yaml").read_text())
            manifest["spec"]["permissions"]["secrets"] = ["NEW_API_KEY"]
            candidate.write_text(json.dumps(manifest))
            result = self.run_cli("plugin", "diff", str(EXAMPLE), str(candidate))
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertTrue(json.loads(result.stdout)["permissionExpansion"])

    def test_install_disable_remove_lifecycle(self):
        with tempfile.TemporaryDirectory() as state_dir:
            install = self.run_cli("plugin", "install", str(EXAMPLE), "--state-dir", state_dir)
            self.assertEqual(install.returncode, 0, install.stderr)
            result = json.loads(install.stdout)
            self.assertEqual(result["installed"], "hello-aegis")
            self.assertGreater(len(result["serviceToken"]), 30)

            premature = self.run_cli("plugin", "remove", "hello-aegis", "--state-dir", state_dir)
            self.assertEqual(premature.returncode, 1)
            self.assertIn("must be disabled", premature.stderr)

            disable = self.run_cli("plugin", "disable", "hello-aegis", "--state-dir", state_dir)
            remove = self.run_cli("plugin", "remove", "hello-aegis", "--state-dir", state_dir)
            listing = self.run_cli("plugin", "list", "--state-dir", state_dir)
            self.assertEqual(disable.returncode, 0, disable.stderr)
            self.assertEqual(remove.returncode, 0, remove.stderr)
            self.assertEqual(json.loads(listing.stdout), [])

    def test_install_rejects_incompatible_core(self):
        with tempfile.TemporaryDirectory() as state_dir:
            result = self.run_cli(
                "plugin", "install", str(EXAMPLE), "--state-dir", state_dir,
                "--core-version", "1.0.0",
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("installed Core is 1.0.0", result.stderr)

    def test_install_rejects_undeclared_secret_grant_before_registry_write(self):
        with tempfile.TemporaryDirectory() as state_dir:
            result = self.run_cli(
                "plugin", "install", str(EXAMPLE), "--state-dir", state_dir,
                "--grant-secret", "STOLEN_KEY",
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("not declared", result.stderr)
            listing = self.run_cli("plugin", "list", "--state-dir", state_dir)
            self.assertEqual(json.loads(listing.stdout), [])


if __name__ == "__main__":
    unittest.main()
