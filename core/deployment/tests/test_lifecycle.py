import io
import json
import unittest
from unittest.mock import patch

from core.deployment import HttpLifecycleAdapter, LifecycleAdapterError


class Response:
    def __init__(self, value):
        self.raw = json.dumps(value).encode() if not isinstance(value, bytes) else value
    def __enter__(self): return self
    def __exit__(self, *args): return None
    def read(self, _limit): return self.raw


class HttpLifecycleAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = HttpLifecycleAdapter("http://docker-lifecycle:8011", lambda: "token", poll_interval=0)

    def test_state_uses_exact_encoded_target_and_auth(self):
        with patch("urllib.request.urlopen", return_value=Response({"status": "running", "health": "healthy"})) as call:
            result = self.adapter.state("hello/container")
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, "http://docker-lifecycle:8011/v1/targets/hello%2Fcontainer/state")
        self.assertEqual(request.headers["Authorization"], "Bearer token")
        self.assertEqual(result["status"], "running")

    def test_restart_sends_only_idempotency_key(self):
        with patch("urllib.request.urlopen", return_value=Response({"status": "accepted"})) as call:
            self.adapter.restart("hello-container", "restart:proposal")
        request = call.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"idempotency_key": "restart:proposal"})

    def test_oversized_or_invalid_response_fails_closed(self):
        with patch("urllib.request.urlopen", return_value=Response(b"x" * 20_000)):
            with self.assertRaises(LifecycleAdapterError):
                self.adapter.state("hello-container")
        with patch("urllib.request.urlopen", return_value=Response(b"[]")):
            with self.assertRaises(LifecycleAdapterError):
                self.adapter.state("hello-container")

    def test_external_or_credential_bearing_url_is_rejected(self):
        for url in ("https://example.com", "http://user:pass@proxy:8011", "http://proxy:8011?x=1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                HttpLifecycleAdapter(url, lambda: "token")


if __name__ == "__main__":
    unittest.main()
