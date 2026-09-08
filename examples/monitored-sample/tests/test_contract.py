import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parents[1]
sys.path.insert(0, str(REPO_ROOT / "sdk/python"))

from aegis_plugin import load_manifest, validate_manifest


class MonitoredSampleTests(unittest.TestCase):
    def test_manifest_has_no_mcp_or_secrets(self):
        manifest = validate_manifest(load_manifest(ROOT / "aegis-plugin.yaml"))
        self.assertIsNone(manifest["spec"]["mcp"])
        self.assertEqual(manifest["spec"]["permissions"]["secrets"], [])
        self.assertTrue(manifest["spec"]["runtime"]["readOnly"])

    def test_all_declared_files_exist_inside_sample(self):
        runtime = json.loads((ROOT / "aegis-plugin.yaml").read_text())["spec"]["runtime"]
        for field in ("statusFile", "versionFile", "metricsFile", "logFile"):
            self.assertTrue((ROOT / runtime[field]).is_file())


if __name__ == "__main__": unittest.main()
