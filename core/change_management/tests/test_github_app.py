import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.change_management.github_app import GitHubApi, GitHubAppError, app_jwt


class GitHubAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.key = Path(self.temp.name) / "key.pem"; self.key.write_text("test-key")

    def test_jwt_has_bounded_claims_and_string_issuer(self):
        completed = type("Result", (), {"returncode": 0, "stdout": b"signature"})()
        with patch("subprocess.run", return_value=completed) as run:
            token = app_jwt(4821358, self.key, now=1_000)
        parts = token.split(".")
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
        self.assertEqual(payload, {"iat": 940, "exp": 1540, "iss": "4821358"})
        self.assertNotIn("test-key", token)
        self.assertEqual(run.call_args.kwargs["input"], ".".join(parts[:2]).encode())

    def test_missing_or_oversized_key_fails_before_signing(self):
        with self.assertRaises(GitHubAppError): app_jwt(1, self.key.with_name("missing"))
        self.key.write_bytes(b"x" * 20_000)
        with self.assertRaises(GitHubAppError): app_jwt(1, self.key)

    def test_api_rejects_unscoped_paths(self):
        api = GitHubApi("Bearer token")
        for path in ("repos/x", "//attacker.example/path"):
            with self.subTest(path=path), self.assertRaises(GitHubAppError):
                api.request("GET", path)


if __name__ == "__main__":
    unittest.main()
