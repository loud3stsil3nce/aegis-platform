import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class LoggingPolicyTests(unittest.TestCase):
    def test_published_compose_services_bound_json_logs(self):
        expected_blocks = {
            "deploy/compose.docker-deployment.yaml": 2,
            "examples/hello-aegis/compose.yaml": 1,
        }
        for relative, count in expected_blocks.items():
            with self.subTest(compose=relative):
                content = (ROOT / relative).read_text(encoding="utf-8")
                self.assertEqual(content.count("logging:"), count)
                self.assertEqual(content.count("driver: json-file"), count)
                self.assertEqual(content.count('max-size: "10m"'), count)
                self.assertEqual(content.count('max-file: "5"'), count)


if __name__ == "__main__":
    unittest.main()
