import copy
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO_ROOT), str(REPO_ROOT / "sdk/python")]

from aegis_plugin import load_manifest, validate_manifest
from core.runtime import RuntimePolicyError, build_runtime_plan


class RuntimePlannerTests(unittest.TestCase):
    def setUp(self):
        self.hello_root = REPO_ROOT / "examples/hello-aegis"
        self.manifest = validate_manifest(load_manifest(self.hello_root / "aegis-plugin.yaml"))
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)

    def test_managed_plan_is_plugin_scoped(self):
        plan = build_runtime_plan(self.manifest, self.hello_root, self.state.name, set())
        self.assertEqual(plan.network, "aegis-plugin-hello-aegis")
        self.assertTrue(plan.compose_file.endswith("hello-aegis/compose.yaml"))
        self.assertEqual(plan.identity, "plugin:hello-aegis")

    def test_compose_traversal_is_rejected(self):
        changed = copy.deepcopy(self.manifest)
        changed["spec"]["runtime"]["composeFile"] = "../docker-compose.yml"
        with self.assertRaisesRegex(RuntimePolicyError, "inside the plugin package"):
            build_runtime_plan(changed, self.hello_root, self.state.name, set())

    def test_undeclared_and_missing_secret_grants_fail(self):
        with self.assertRaisesRegex(RuntimePolicyError, "not declared"):
            build_runtime_plan(self.manifest, self.hello_root, self.state.name, {"STOLEN_KEY"})
        changed = copy.deepcopy(self.manifest)
        changed["spec"]["permissions"]["secrets"] = ["REQUIRED_KEY"]
        with self.assertRaisesRegex(RuntimePolicyError, "missing"):
            build_runtime_plan(changed, self.hello_root, self.state.name, set())

    def test_remote_requires_https_and_declared_host(self):
        changed = copy.deepcopy(self.manifest)
        changed["spec"]["runtime"] = {"mode": "remote", "endpoint": "http://127.0.0.1:9000"}
        with self.assertRaisesRegex(RuntimePolicyError, "HTTPS"):
            build_runtime_plan(changed, self.hello_root, self.state.name, set())
        changed["spec"]["runtime"]["endpoint"] = "https://plugin.example.test"
        with self.assertRaisesRegex(RuntimePolicyError, "not declared"):
            build_runtime_plan(changed, self.hello_root, self.state.name, set())
        changed["spec"]["permissions"]["networks"] = ["plugin.example.test"]
        plan = build_runtime_plan(changed, self.hello_root, self.state.name, set())
        self.assertEqual(plan.endpoint, "https://plugin.example.test/mcp")

    def test_monitored_project_is_read_only_and_root_bounded(self):
        changed = copy.deepcopy(self.manifest)
        changed["spec"]["runtime"] = {
            "mode": "monitored-project", "projectPath": str(self.hello_root), "readOnly": True,
            "statusFile": "status.json", "versionFile": "version.json",
            "metricsFile": "metrics.json", "logFile": "logs/app.log",
        }
        changed["spec"]["mcp"] = None
        validate_manifest(changed)
        with self.assertRaisesRegex(RuntimePolicyError, "outside"):
            build_runtime_plan(changed, self.hello_root, self.state.name, set(), ("/tmp",))
        plan = build_runtime_plan(changed, self.hello_root, self.state.name, set(), (REPO_ROOT,))
        self.assertEqual(plan.project_path, str(self.hello_root.resolve()))


if __name__ == "__main__":
    unittest.main()
