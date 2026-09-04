import re
import unittest
from pathlib import Path


class BuildWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (
            Path(__file__).parents[3] / ".github" / "workflows" / "build-hello-aegis.yml"
        ).read_text()

    def test_workflow_has_narrow_trigger_permissions_and_context(self):
        self.assertIn("branches: [main]", self.text)
        self.assertIn("examples/hello-aegis/**", self.text)
        self.assertRegex(self.text, r"permissions:\n  contents: read\n  packages: write\n")
        self.assertNotIn("pull_request_target", self.text)
        self.assertNotIn("contents: write", self.text)
        self.assertIn("context: examples/hello-aegis", self.text)
        self.assertIn("platforms: linux/amd64", self.text)

    def test_actions_are_commit_pinned_and_output_is_immutable(self):
        uses = re.findall(r"^\s*uses:\s+([^\s#]+)", self.text, re.MULTILINE)
        self.assertGreaterEqual(len(uses), 4)
        for action in uses:
            self.assertRegex(action, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")
        self.assertIn(":git-${{ github.sha }}", self.text)
        self.assertIn("${{ steps.build.outputs.digest }}", self.text)
        self.assertIn("provenance: mode=max", self.text)
        self.assertIn("sbom: true", self.text)
        self.assertNotRegex(self.text, r"(?m)^\s*tags:\s+.*:(?:latest|main)\s*$")


if __name__ == "__main__":
    unittest.main()
