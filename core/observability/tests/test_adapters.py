import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO_ROOT), str(REPO_ROOT / "sdk/python")]

from core.observability import ContractAdapterError, HttpContractAdapter, MonitoredProjectAdapter


class FakeResponse:
    def __init__(self, value): self.raw = json.dumps(value).encode()
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def read(self, _limit): return self.raw


def plugin(mode="remote"):
    return {"id": "sample", "manifest": {"spec": {
        "runtime": {"mode": mode, "endpoint": "https://sample.example"},
        "health": {"readiness": "/ready", "version": "/version", "metrics": "/metrics", "logs": "/logs"},
    }}}


class HttpAdapterTests(unittest.TestCase):
    @patch("core.observability.adapters.urllib.request.urlopen")
    def test_contract_paths_token_and_log_bound_are_applied(self, open_url):
        open_url.return_value = FakeResponse({"logs": "ok"})
        adapter = HttpContractAdapter(lambda plugin_id: "scoped-token")
        self.assertEqual(adapter.logs(plugin(), 200), "ok")
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://sample.example/logs?lines=200")
        self.assertEqual(request.get_header("Authorization"), "Bearer scoped-token")

    @patch("core.observability.adapters.urllib.request.urlopen")
    def test_oversized_contract_response_fails(self, open_url):
        response = FakeResponse({})
        response.raw = b"x" * 16_385
        open_url.return_value = response
        with self.assertRaisesRegex(ContractAdapterError, "exceeds"):
            HttpContractAdapter(lambda _: "token").health(plugin())


class MonitoredAdapterTests(unittest.TestCase):
    def test_reads_only_declared_files_and_tails_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, value in (("status.json", {"status": "ready"}), ("version.json", {"version": "1.0.0"}), ("metrics.json", {"requests": 2})):
                (root / name).write_text(json.dumps(value))
            (root / "app.log").write_text("one\ntwo\nthree\n")
            item = plugin("monitored-project")
            item["manifest"]["spec"]["runtime"].update({
                "projectPath": str(root), "statusFile": "status.json", "versionFile": "version.json",
                "metricsFile": "metrics.json", "logFile": "app.log",
            })
            adapter = MonitoredProjectAdapter()
            self.assertEqual(adapter.health(item)["status"], "ready")
            self.assertEqual(adapter.logs(item, 2), "two\nthree")

    def test_runtime_traversal_is_rechecked(self):
        with tempfile.TemporaryDirectory() as directory:
            item = plugin("monitored-project")
            item["manifest"]["spec"]["runtime"].update({"projectPath": directory, "statusFile": "../outside"})
            with self.assertRaisesRegex(ContractAdapterError, "escaped"):
                MonitoredProjectAdapter().health(item)


if __name__ == "__main__": unittest.main()
