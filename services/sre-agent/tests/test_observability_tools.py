import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from src.observability_tools import (
    DEFAULT_MAX_LOG_LINES,
    get_bounded_logs,
    get_container_health,
    list_registered_containers,
    search_bounded_logs,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, limit):
        return self.payload[:limit]


class ObservabilityToolsTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "AEGIS_DOCKER_OBSERVER_URL": "http://docker-observer:8010",
                "AEGIS_DOCKER_OBSERVER_TOKEN": "observer-token",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    @patch("src.observability_tools.urllib.request.urlopen")
    def test_lists_registered_containers(self, open_url):
        open_url.return_value = FakeResponse({"containers": [{"name": "app"}]})
        result = json.loads(list_registered_containers())
        self.assertEqual(result["containers"][0]["name"], "app")
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer observer-token")

    @patch("src.observability_tools.urllib.request.urlopen")
    def test_encodes_container_name_as_one_path_segment(self, open_url):
        open_url.return_value = FakeResponse({"name": "app", "status": "running"})
        get_container_health("../app")
        request = open_url.call_args.args[0]
        self.assertIn("..%2Fapp", request.full_url)

    @patch("src.observability_tools.urllib.request.urlopen")
    def test_caps_log_line_count(self, open_url):
        open_url.return_value = FakeResponse({"logs": "ok"})
        get_bounded_logs("app", 50_000)
        request = open_url.call_args.args[0]
        self.assertTrue(request.full_url.endswith(f"?lines={DEFAULT_MAX_LOG_LINES}"))

    @patch("src.observability_tools.urllib.request.urlopen")
    def test_log_search_is_literal_and_case_insensitive(self, open_url):
        open_url.return_value = FakeResponse({"logs": "first ERROR line\nsecond line\n"})
        self.assertEqual(search_bounded_logs("app", "error"), "first ERROR line")

    @patch("src.observability_tools.urllib.request.urlopen")
    def test_maps_unregistered_container_to_safe_error(self, open_url):
        open_url.side_effect = urllib.error.HTTPError(
            "http://observer", 404, "not found", {}, io.BytesIO()
        )
        with self.assertRaisesRegex(ValueError, "not registered"):
            get_container_health("unknown")

    def test_fails_closed_without_observer_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                list_registered_containers()


if __name__ == "__main__":
    unittest.main()
