import os
import threading
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


class FakeContainer:
    def __init__(self, name="hello-container"):
        self.name = name
        self.attrs = {
            "State": {"Status": "running", "Health": {"Status": "healthy"}, "StartedAt": "before"},
            "RestartCount": 0,
        }
        self.restart_calls = 0
        self.lock = threading.Lock()

    def reload(self):
        return None

    def restart(self):
        with self.lock:
            self.restart_calls += 1


class LifecycleProxyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.container = FakeContainer()
        self.env = patch.dict(os.environ, {
            "AEGIS_DOCKER_LIFECYCLE_TOKEN": "correct-token",
            "AEGIS_LIFECYCLE_ALLOWED_TARGETS": "hello-container",
            "AEGIS_LIFECYCLE_STATE_PATH": os.path.join(self.temp.name, "idempotency.sqlite3"),
        })
        self.lookup = patch.object(main, "_container", side_effect=self._lookup)
        self.env.start(); self.lookup.start()
        self.addCleanup(self.env.stop); self.addCleanup(self.lookup.stop)
        self.client = TestClient(main.app)
        self.headers = {"Authorization": "Bearer correct-token"}

    def _lookup(self, target):
        if target != "hello-container":
            raise main.HTTPException(status_code=404, detail="lifecycle target not registered")
        return self.container

    def test_missing_and_wrong_auth_fail_closed(self):
        path = "/v1/targets/hello-container/state"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(path, headers={"Authorization": "Bearer wrong"}).status_code, 401)

    def test_unknown_target_is_rejected(self):
        response = self.client.get("/v1/targets/database/state", headers=self.headers)
        self.assertEqual(response.status_code, 404)

    def test_body_is_bounded_and_schema_is_exact(self):
        path = "/v1/targets/hello-container/restart"
        oversized = self.client.post(path, content=b"x" * (main.MAX_BODY_BYTES + 1), headers=self.headers)
        self.assertEqual(oversized.status_code, 413)
        extra = self.client.post(path, json={"idempotency_key": "one", "command": "remove"}, headers=self.headers)
        self.assertEqual(extra.status_code, 422)
        self.assertEqual(self.container.restart_calls, 0)

    def test_streamed_body_without_content_length_is_bounded(self):
        path = "/v1/targets/hello-container/restart"
        def chunks():
            yield b"x" * 3000
            yield b"y" * 3000
        response = self.client.post(path, content=chunks(), headers=self.headers)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.container.restart_calls, 0)

    def test_duplicate_restart_is_idempotent(self):
        path = "/v1/targets/hello-container/restart"
        first = self.client.post(path, json={"idempotency_key": "restart:one"}, headers=self.headers)
        second = self.client.post(path, json={"idempotency_key": "restart:one"}, headers=self.headers)
        self.assertEqual(first.json()["status"], "accepted")
        self.assertEqual(second.json()["status"], "already_applied")
        self.assertEqual(self.container.restart_calls, 1)

    def test_idempotency_survives_new_database_connections(self):
        path = "/v1/targets/hello-container/restart"
        first = self.client.post(path, json={"idempotency_key": "restart:persistent"}, headers=self.headers)
        self.assertEqual(first.json()["status"], "accepted")
        # Every operation opens a fresh SQLite connection, matching a proxy
        # process restart against the same persistent state file.
        second = self.client.post(path, json={"idempotency_key": "restart:persistent"}, headers=self.headers)
        self.assertEqual(second.json()["status"], "already_applied")
        self.assertEqual(self.container.restart_calls, 1)

    def test_concurrent_duplicates_restart_once(self):
        path = "/v1/targets/hello-container/restart"
        def invoke(_):
            with TestClient(main.app) as client:
                return client.post(path, json={"idempotency_key": "restart:concurrent"}, headers=self.headers)
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(invoke, range(16)))
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(self.container.restart_calls, 1)

    def test_no_generic_docker_environment_or_filesystem_routes_exist(self):
        for path in ("/v1/containers", "/v1/docker", "/v1/exec", "/v1/env", "/v1/files"):
            self.assertEqual(self.client.get(path, headers=self.headers).status_code, 404)


if __name__ == "__main__":
    unittest.main()
