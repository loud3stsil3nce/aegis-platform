import json
import unittest
from unittest.mock import patch

from core.change_management import HttpDeploymentAdapter, RuntimeAdapterError


IMAGE = "ghcr.io/loud3stsil3nce/aegis-hello-aegis@sha256:" + "a" * 64


class Response:
    def __init__(self, value):
        self.raw = json.dumps(value).encode() if not isinstance(value, bytes) else value
    def __enter__(self): return self
    def __exit__(self, *args): return None
    def read(self, _limit): return self.raw


class HttpDeploymentAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = HttpDeploymentAdapter(
            "http://docker-deployment:8013", lambda: "token", poll_interval=0,
        )

    def test_state_uses_encoded_target_auth_and_bounded_image(self):
        value = {"status": "running", "health": "healthy", "image": IMAGE}
        with patch("urllib.request.urlopen", return_value=Response(value)) as call:
            self.assertEqual(self.adapter.current_image("hello/aegis"), IMAGE)
        request = call.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://docker-deployment:8013/v1/targets/hello%2Faegis/state",
        )
        self.assertEqual(request.headers["Authorization"], "Bearer token")
        with patch("urllib.request.urlopen", return_value=Response({"image": "aegis/hello-aegis:0.1.0"})):
            self.assertEqual(self.adapter.current_image("hello-aegis"), "aegis/hello-aegis:0.1.0")

    def test_deploy_sends_only_exact_image_and_idempotency_key(self):
        with patch("urllib.request.urlopen", return_value=Response({"status": "accepted"})) as call:
            self.adapter.deploy_image("hello-aegis", IMAGE, "proposal:deploy")
        self.assertEqual(json.loads(call.call_args.args[0].data), {
            "image_reference": IMAGE, "idempotency_key": "proposal:deploy",
        })

    def test_bad_image_response_body_auth_or_origin_fails_closed(self):
        for value in ({"image": ""}, {"image": "bad\nimage"}, [], b"x" * 20_000):
            with self.subTest(value=value), patch("urllib.request.urlopen", return_value=Response(value)):
                with self.assertRaises(RuntimeAdapterError):
                    self.adapter.current_image("hello-aegis")
        with self.assertRaises(RuntimeAdapterError):
            HttpDeploymentAdapter("http://proxy:8013", lambda: "").state("hello-aegis")
        for url in ("https://example.com", "http://user:pass@proxy:8013", "http://proxy:8013?x=1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                HttpDeploymentAdapter(url, lambda: "token")


if __name__ == "__main__":
    unittest.main()
