import unittest

from core.change_management.github_adapter import (
    GitHubChangeAdapter, GitHubChangeError, InstallationTokenProvider,
    content_manifest_sha256,
)
from core.change_management.policy import ValidatedChange


class FakeApi:
    def __init__(self, responses): self.responses, self.calls = list(responses), []
    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        response = self.responses.pop(0)
        if isinstance(response, Exception): raise response
        return response
    def request_optional(self, method, path, payload=None):
        return self.request(method, path, payload)


class TokenProvider:
    def __init__(self, read_api, write_api=None): self.read_api, self.write_api, self.issues = read_api, write_api, []
    def issue(self, repository, *, write):
        self.issues.append((repository, write)); return self.write_api if write else self.read_api


def change():
    return ValidatedChange("owner/repo", "main", "aegis/test", ("core/a.py",), 1, 1, 10, "d" * 64)


class GitHubChangeAdapterTests(unittest.TestCase):
    def test_preflight_is_read_only_and_requires_exact_head(self):
        api = FakeApi([{"object": {"sha": "a" * 40}}, {"tree": {"sha": "tree"}}])
        provider = TokenProvider(api)
        result = GitHubChangeAdapter(provider).preflight(change(), expected_base_sha="a" * 40)
        self.assertEqual(result.base_tree_sha, "tree")
        self.assertEqual(provider.issues, [("owner/repo", False)])
        self.assertTrue(all(call[0] == "GET" for call in api.calls))
        stale = FakeApi([{"object": {"sha": "b" * 40}}])
        with self.assertRaises(GitHubChangeError):
            GitHubChangeAdapter(TokenProvider(stale)).preflight(change(), expected_base_sha="a" * 40)

    def test_one_tree_commit_branch_and_draft_pr_are_created(self):
        read = FakeApi([{"object": {"sha": "a" * 40}}, {"tree": {"sha": "base-tree"}}])
        write = FakeApi([{"object": {"sha": "a" * 40}}, None, {"sha": "blob"}, {"sha": "tree"}, {"sha": "commit"}, {}, {"number": 7, "html_url": "https://github/pr/7"}])
        provider = TokenProvider(read, write)
        result = GitHubChangeAdapter(provider).create_pull_request(
            change(), expected_base_sha="a" * 40, files={"core/a.py": b"new\n"},
            commit_message="Test", title="Test PR", body="Approved proposal",
        )
        self.assertEqual((result.commit_sha, result.pull_request_number), ("commit", 7))
        self.assertEqual(provider.issues, [("owner/repo", False), ("owner/repo", True)])
        self.assertEqual([call[1].rsplit("/", 1)[-1] for call in write.calls], ["main", "aegis%2Ftest", "blobs", "trees", "commits", "refs", "pulls"])
        self.assertTrue(write.calls[-1][2]["draft"])

    def test_file_snapshot_must_exactly_match_approved_paths(self):
        adapter = GitHubChangeAdapter(TokenProvider(FakeApi([])))
        with self.assertRaises(GitHubChangeError):
            adapter.create_pull_request(
                change(), expected_base_sha="a" * 40, files={"core/other.py": b"x"},
                commit_message="x", title="x", body="x",
            )

    def test_second_head_check_blocks_toctou_before_writes(self):
        read = FakeApi([{"object": {"sha": "a" * 40}}, {"tree": {"sha": "base-tree"}}])
        write = FakeApi([{"object": {"sha": "b" * 40}}])
        with self.assertRaisesRegex(GitHubChangeError, "before mutation"):
            GitHubChangeAdapter(TokenProvider(read, write)).create_pull_request(
                change(), expected_base_sha="a" * 40, files={"core/a.py": b"new\n"},
                commit_message="Test", title="Test", body="Test",
            )
        self.assertEqual(len(write.calls), 1)

    def test_existing_branch_fails_before_blob_writes(self):
        read = FakeApi([{"object": {"sha": "a" * 40}}, {"tree": {"sha": "base-tree"}}])
        write = FakeApi([{"object": {"sha": "a" * 40}}, {"object": {"sha": "existing"}}])
        with self.assertRaisesRegex(GitHubChangeError, "already exists"):
            GitHubChangeAdapter(TokenProvider(read, write)).create_pull_request(
                change(), expected_base_sha="a" * 40, files={"core/a.py": b"new\n"},
                commit_message="Test", title="Test", body="Test",
            )
        self.assertEqual(len(write.calls), 2)

    def test_pr_failure_rolls_back_only_exact_unchanged_branch(self):
        read = FakeApi([{"object": {"sha": "a" * 40}}, {"tree": {"sha": "base-tree"}}])
        write = FakeApi([
            {"object": {"sha": "a" * 40}}, None, {"sha": "blob"}, {"sha": "tree"},
            {"sha": "commit"}, {}, RuntimeError("PR unavailable"),
            {"object": {"sha": "commit"}}, {},
        ])
        with self.assertRaisesRegex(GitHubChangeError, "rolled back"):
            GitHubChangeAdapter(TokenProvider(read, write)).create_pull_request(
                change(), expected_base_sha="a" * 40, files={"core/a.py": b"new\n"},
                commit_message="Test", title="Test", body="Test",
            )
        self.assertEqual(write.calls[-1][0], "DELETE")

    def test_pr_failure_never_deletes_changed_branch(self):
        read = FakeApi([{"object": {"sha": "a" * 40}}, {"tree": {"sha": "base-tree"}}])
        write = FakeApi([
            {"object": {"sha": "a" * 40}}, None, {"sha": "blob"}, {"sha": "tree"},
            {"sha": "commit"}, {}, RuntimeError("PR unavailable"),
            {"object": {"sha": "someone-else"}},
        ])
        with self.assertRaisesRegex(GitHubChangeError, "manual recovery"):
            GitHubChangeAdapter(TokenProvider(read, write)).create_pull_request(
                change(), expected_base_sha="a" * 40, files={"core/a.py": b"new\n"},
                commit_message="Test", title="Test", body="Test",
            )
        self.assertFalse(any(call[0] == "DELETE" for call in write.calls))

    def test_content_manifest_hash_is_order_independent_and_content_bound(self):
        first = content_manifest_sha256({"b": b"2", "a": b"1"})
        self.assertEqual(first, content_manifest_sha256({"a": b"1", "b": b"2"}))
        self.assertNotEqual(first, content_manifest_sha256({"a": b"changed", "b": b"2"}))


class InstallationTokenProviderTests(unittest.TestCase):
    def test_token_request_is_narrowed_and_token_is_not_retained(self):
        app_api = FakeApi([{
            "token": "temporary-secret", "permissions": {
                "contents": "read", "metadata": "read", "pull_requests": "read",
            }, "repositories": [{"full_name": "owner/repo"}],
        }])
        provider = InstallationTokenProvider(lambda: app_api, 123)
        issued = provider.issue("owner/repo", write=False)
        self.assertEqual(app_api.calls[0][2]["repositories"], ["repo"])
        self.assertNotIn("temporary-secret", repr(provider.__dict__))
        self.assertEqual(issued.authorization, "Bearer temporary-secret")

    def test_missing_repository_evidence_fails_closed(self):
        app_api = FakeApi([{
            "token": "temporary-secret", "permissions": {
                "contents": "write", "metadata": "read", "pull_requests": "write",
            }, "repositories": [],
        }])
        with self.assertRaises(GitHubChangeError):
            InstallationTokenProvider(lambda: app_api, 123).issue("owner/repo", write=True)


if __name__ == "__main__":
    unittest.main()
