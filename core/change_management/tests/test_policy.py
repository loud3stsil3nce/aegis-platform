import json
import unittest
from pathlib import Path

from core.change_management import ChangePolicy, ChangePolicyError


GOOD_DIFF = """diff --git a/core/example.py b/core/example.py
--- a/core/example.py
+++ b/core/example.py
@@ -1 +1 @@
-old
+new
"""


class ChangePolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = ChangePolicy(
            repositories=frozenset({"owner/aegis-platform"}),
            base_branches=frozenset({"main"}),
            allowed_path_prefixes=("core", "docs"),
            max_files=2, max_diff_bytes=4096, max_changed_lines=4,
        )

    def test_valid_change_is_normalized_and_hashed(self):
        result = self.policy.validate(
            repository="owner/aegis-platform", base_branch="main",
            proposed_branch="aegis/change-123", unified_diff=GOOD_DIFF,
        )
        self.assertEqual(result.paths, ("core/example.py",))
        self.assertEqual((result.additions, result.deletions), (1, 1))
        self.assertEqual(len(result.diff_sha256), 64)

    def test_repository_base_and_proposed_branch_are_exactly_scoped(self):
        cases = (
            {"repository": "owner/other", "base_branch": "main", "proposed_branch": "aegis/x"},
            {"repository": "owner/aegis-platform", "base_branch": "dev", "proposed_branch": "aegis/x"},
            {"repository": "owner/aegis-platform", "base_branch": "main", "proposed_branch": "feature/x"},
            {"repository": "owner/aegis-platform", "base_branch": "main", "proposed_branch": "main"},
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ChangePolicyError):
                self.policy.validate(unified_diff=GOOD_DIFF, **values)

    def test_out_of_scope_traversal_and_binary_changes_fail(self):
        for diff in (
            GOOD_DIFF.replace("core/example.py", "services/app.py"),
            GOOD_DIFF.replace("core/example.py", "core/../secrets.txt"),
            GOOD_DIFF + "GIT binary patch\n",
        ):
            with self.subTest(diff=diff), self.assertRaises(ChangePolicyError):
                self.policy.validate(
                    repository="owner/aegis-platform", base_branch="main",
                    proposed_branch="aegis/x", unified_diff=diff,
                )

    def test_file_byte_and_line_budgets_fail_closed(self):
        many_files = GOOD_DIFF + GOOD_DIFF.replace("example.py", "two.py") + GOOD_DIFF.replace("example.py", "three.py")
        many_lines = GOOD_DIFF + "+one\n+two\n+three\n"
        for diff in (many_files, many_lines, GOOD_DIFF + ("x" * 5000)):
            with self.subTest(size=len(diff)), self.assertRaises(ChangePolicyError):
                self.policy.validate(
                    repository="owner/aegis-platform", base_branch="main",
                    proposed_branch="aegis/x", unified_diff=diff,
                )

    def test_policy_cannot_start_without_explicit_paths(self):
        with self.assertRaises(ChangePolicyError):
            ChangePolicy(
                repositories=frozenset({"owner/aegis-platform"}),
                base_branches=frozenset({"main"}), allowed_path_prefixes=(),
            )

    def test_external_config_is_strict(self):
        policy = ChangePolicy.from_dict({
            "repositories": ["owner/aegis-platform"], "baseBranches": ["main"],
            "allowedPathPrefixes": ["core"], "maxFiles": 3,
        })
        self.assertEqual(policy.max_files, 3)
        with self.assertRaises(ChangePolicyError):
            ChangePolicy.from_dict({
                "repositories": ["owner/aegis-platform"], "baseBranches": ["main"],
                "allowedPathPrefixes": ["core"], "unexpected": True,
            })

    def test_repository_policy_scopes_privileged_service_exactly(self):
        policy_path = Path(__file__).parents[3] / "config" / "change-policy.json"
        policy = ChangePolicy.from_dict(json.loads(policy_path.read_text()))
        self.assertIn("services/docker_deployment", policy.allowed_path_prefixes)
        self.assertNotIn("services", policy.allowed_path_prefixes)
        diff = GOOD_DIFF.replace("core/example.py", "services/sre-agent/x.py")
        with self.assertRaises(ChangePolicyError):
            policy.validate(
                repository="loud3stsil3nce/aegis-platform", base_branch="main",
                proposed_branch="aegis/other-service", unified_diff=diff,
            )


if __name__ == "__main__":
    unittest.main()
