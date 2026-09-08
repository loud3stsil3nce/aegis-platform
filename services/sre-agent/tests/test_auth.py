import unittest

try:
    import fastapi  # noqa: F401
except ModuleNotFoundError:
    FASTAPI_AVAILABLE = False
else:
    FASTAPI_AVAILABLE = True

from src.auth import (
    AuthenticationError,
    enforce_body_limit,
    validate_bearer_header,
    validate_shared_secret,
)


class FakeRequest:
    def __init__(self, content_length=None):
        self.headers = {}
        if content_length is not None:
            self.headers["content-length"] = content_length


class AuthenticationTests(unittest.TestCase):
    def test_bearer_accepts_exact_token(self):
        validate_bearer_header("Bearer correct", "correct")

    def test_bearer_rejects_missing_configuration(self):
        with self.assertRaisesRegex(AuthenticationError, "not configured"):
            validate_bearer_header("Bearer anything", None)

    def test_bearer_rejects_missing_header(self):
        with self.assertRaisesRegex(AuthenticationError, "missing"):
            validate_bearer_header(None, "correct")

    def test_bearer_rejects_wrong_token(self):
        with self.assertRaisesRegex(AuthenticationError, "invalid"):
            validate_bearer_header("Bearer wrong", "correct")

    def test_webhook_accepts_exact_secret(self):
        validate_shared_secret("correct", "correct")

    def test_webhook_fails_closed_without_configuration(self):
        with self.assertRaisesRegex(AuthenticationError, "not configured"):
            validate_shared_secret("anything", None)

    def test_webhook_rejects_wrong_secret(self):
        with self.assertRaisesRegex(AuthenticationError, "invalid"):
            validate_shared_secret("wrong", "correct")



@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI is installed in the service image")
class BodyLimitTests(unittest.TestCase):
    def test_body_limit_accepts_missing_header(self):
        enforce_body_limit(FakeRequest())

    def test_body_limit_accepts_boundary(self):
        enforce_body_limit(FakeRequest("262144"))

    def test_body_limit_rejects_oversized_request(self):
        with self.assertRaisesRegex(Exception, "exceeds policy limit") as context:
            enforce_body_limit(FakeRequest("262145"))
        self.assertEqual(context.exception.status_code, 413)

    def test_body_limit_rejects_invalid_length(self):
        with self.assertRaisesRegex(Exception, "invalid content-length") as context:
            enforce_body_limit(FakeRequest("not-a-number"))
        self.assertEqual(context.exception.status_code, 400)

    def test_body_limit_rejects_negative_length(self):
        with self.assertRaisesRegex(Exception, "exceeds policy limit") as context:
            enforce_body_limit(FakeRequest("-1"))
        self.assertEqual(context.exception.status_code, 413)


if __name__ == "__main__":
    unittest.main()
