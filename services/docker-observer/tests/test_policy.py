import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import main


class DockerObserverPolicyTests(unittest.TestCase):
    def test_rejects_unregistered_container_before_docker_access(self):
        with patch.dict(os.environ, {"AEGIS_ALLOWED_CONTAINERS": "app"}):
            with patch.object(main, "_client") as client:
                with self.assertRaises(HTTPException) as raised:
                    main._container("database")
                self.assertEqual(raised.exception.status_code, 404)
                client.assert_not_called()

    def test_redacts_and_bounds_logs(self):
        value = main._redact_and_bound("password=hunter2\n" + ("x" * 20_000))
        self.assertNotIn("hunter2", value)
        self.assertLessEqual(len(value.encode("utf-8")), main.MAX_RESULT_BYTES)


if __name__ == "__main__":
    unittest.main()
