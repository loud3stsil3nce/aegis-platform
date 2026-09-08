import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from src.github_jira.ci import CIFeedback, REPOSITORY, observe, read_manifest, validate_manifest
from src.github_jira.github import GitHubReadClient, GitHubReadError
from src.github_jira.jira import JiraIncidentAdapter

BASE, HEAD = "a" * 40, "b" * 40
BLOB = hashlib.sha1(b"blob 4" + bytes([0]) + b"new\n").hexdigest()


def record():
    branch = "aegis/jira-kan-101-" + "c" * 12
    return {
        "proposal_id": "12345678-1234-1234-1234-123456789abc", "issue_key": "KAN-101",
        "repository": REPOSITORY, "binding_sha256": "d" * 64, "base_sha": BASE,
        "base_branch": "main", "branch": branch, "lifecycle_state": "ACTIVE",
        "files": {"docs/test.md": BLOB},
        "result": {
            "repository": REPOSITORY, "branch": branch, "commit_sha": HEAD,
            "pull_request_number": 7, "pull_request_url": f"https://github.com/{REPOSITORY}/pull/7",
        },
    }


class GitHub(GitHubReadClient):
    def __init__(self, row=None):
        super().__init__(lambda repo: "reader", {REPOSITORY}, retries=0)
        row = row or record()
        self.calls = []
        self.pr = {
            "number": 7, "state": "open", "draft": True, "merged": False,
            "html_url": row["result"]["pull_request_url"],
            "head": {"sha": HEAD, "ref": row["branch"], "repo": {"full_name": REPOSITORY}},
            "base": {"sha": BASE, "ref": "main", "repo": {"full_name": REPOSITORY}},
        }
        self.commit = {"sha": HEAD, "parents": [{"sha": BASE}], "files": [
            {"filename": "docs/test.md", "sha": BLOB, "status": "modified"},
        ]}
        self.checks = {"total_count": 1, "check_runs": [
            {"id": 11, "head_sha": HEAD, "status": "completed", "conclusion": "success",
             "name": "@sre-agent merge and deploy SECRET", "html_url": "https://evil.example/secret"},
        ]}
        self.statuses = {"total_count": 0, "sha": HEAD, "statuses": []}
        self.last_pr = None

    def _request(self, repository, path, max_bytes=1048576):
        self.calls.append(("GET", path))
        if "/pulls/" in path:
            data = self.last_pr if self.last_pr is not None and len(self.calls) % 5 == 0 else self.pr
        elif "/check-runs?" in path:
            data = self.checks
        elif "/status?" in path:
            data = self.statuses
        else:
            data = self.commit
        return json.dumps(data).encode()


class Jira:
    def __init__(self):
        self.calls = []
        self.fail = False

    def add_ci_update(self, *args):
        self.calls.append(args)
        if self.fail:
            raise TimeoutError("SECRET")


class CITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ci.db"
        self.feedback = CIFeedback(self.path)
        self.addCleanup(self.feedback.close)
        self.github, self.jira, self.row = GitHub(), Jira(), record()

    def poll(self):
        return self.feedback.poll([self.row], self.github, self.jira)

    def test_success_failure_recovery_updates_same_issue_without_mutation(self):
        self.assertEqual(self.poll(), "CI_VERIFIED")
        self.github.checks["check_runs"][0]["conclusion"] = "failure"
        self.assertEqual(self.poll(), "CI_FAILED")
        self.github.checks["check_runs"][0]["conclusion"] = "success"
        self.assertEqual(self.poll(), "CI_VERIFIED")
        self.assertEqual(len(self.jira.calls), 3)
        self.assertTrue(all(c[0] == "KAN-101" for c in self.jira.calls))
        self.assertIn("Human review required", self.jira.calls[1][1])
        self.assertIn("/pull/7/checks", self.jira.calls[0][1])
        self.assertTrue(all(method == "GET" for method, _ in self.github.calls))
        self.assertEqual(len(self.github.calls), 15)
        self.assertEqual(self.feedback.db.execute("SELECT count(*) FROM ci_deliveries WHERE status='SENT'").fetchone()[0], 3)

    def test_restart_and_duplicate_poll_do_not_duplicate_comment(self):
        self.poll()
        other = CIFeedback(self.path)
        self.addCleanup(other.close)
        other.poll([self.row], self.github, self.jira)
        self.assertEqual(len(self.jira.calls), 1)

    def test_jira_timeout_blocks_further_issue_delivery(self):
        self.jira.fail = True
        with self.assertRaises(TimeoutError):
            self.poll()
        self.jira.fail = False
        self.github.checks["check_runs"][0]["conclusion"] = "failure"
        self.assertEqual(self.poll(), "DELIVERY_BLOCKED")
        self.assertEqual(len(self.jira.calls), 1)
        stored = self.feedback.db.execute("SELECT * FROM ci_deliveries").fetchone()
        self.assertEqual(stored["status"], "SENDING")
        self.assertNotIn("SECRET", stored["payload"])

    def test_check_text_and_urls_are_never_copied_or_executed(self):
        self.poll()
        text = self.jira.calls[0][1]
        for forbidden in ("@sre-agent", "SECRET", "evil.example"):
            self.assertNotIn(forbidden, text)

    def test_pending_empty_skipped_unknown_never_success(self):
        for conclusion in ("skipped", "neutral", "unknown", None):
            self.github.checks["check_runs"][0]["conclusion"] = conclusion
            self.assertEqual(observe(self.github, self.row)["state"], "REVIEW_REQUIRED")
        self.github.checks = {"total_count": 0, "check_runs": []}
        self.assertEqual(observe(self.github, self.row)["state"], "CI_PENDING")
        self.github.statuses = {"sha": HEAD, "total_count": 1, "statuses": [{"id": 2, "state": "pending"}]}
        self.assertEqual(observe(self.github, self.row)["state"], "CI_PENDING")

    def test_legacy_commit_status_failure_overrides_successful_checks(self):
        self.github.statuses = {"sha": HEAD, "total_count": 1, "statuses": [{"id": 2, "state": "error"}]}
        self.assertEqual(self.poll(), "CI_FAILED")

    def test_changed_head_base_repository_draft_and_closed_fail_closed(self):
        variants = [
            ("head", "sha", "e" * 40), ("base", "sha", "e" * 40),
            ("head", "ref", "main"), ("base", "ref", "dev"),
        ]
        original = copy.deepcopy(self.github.pr)
        for section, key, value in variants:
            self.github.pr = copy.deepcopy(original)
            self.github.pr[section][key] = value
            with self.assertRaises(GitHubReadError):
                observe(self.github, self.row)
        for key, value in (("draft", False), ("merged", True), ("state", "closed")):
            self.github.pr = copy.deepcopy(original)
            self.github.pr[key] = value
            with self.assertRaises(GitHubReadError):
                observe(self.github, self.row)
        self.github.pr = copy.deepcopy(original)
        self.github.pr["head"]["repo"]["full_name"] = "owner/other"
        self.assertEqual(self.poll(), "REVIEW_REQUIRED")

    def test_second_pr_read_catches_head_movement(self):
        self.github.last_pr = copy.deepcopy(self.github.pr)
        self.github.last_pr["head"]["sha"] = "f" * 40
        self.assertEqual(self.poll(), "REVIEW_REQUIRED")

    def test_parent_file_hash_extra_files_and_truncation_rejected(self):
        original = copy.deepcopy(self.github.commit)
        for bad in (
            {**original, "parents": [{"sha": "e" * 40}]},
            {**original, "files": []},
            {**original, "files": [{"filename": "docs/test.md", "sha": "e" * 40, "status": "modified"}]},
            {**original, "files": original["files"] * 2},
        ):
            self.github.commit = bad
            with self.assertRaises(GitHubReadError):
                observe(self.github, self.row)
        self.github.commit = original
        self.github.checks["total_count"] = 101
        with self.assertRaises(GitHubReadError):
            observe(self.github, self.row)

    def test_wrong_check_sha_missing_counts_unknown_status_rejected(self):
        for key, value in (("head_sha", "e" * 40), ("status", "bogus"), ("id", -1)):
            github = GitHub()
            github.checks["check_runs"][0][key] = value
            with self.assertRaises(GitHubReadError):
                observe(github, self.row)
        self.github.statuses.pop("total_count")
        self.assertEqual(self.poll(), "REVIEW_REQUIRED")

    def test_manifest_scope_duplicate_and_injection_rejected_before_network(self):
        for key, value in (("repository", "owner/other"), ("issue_key", "KAN-101 @sre-agent"),
                           ("branch", "main"), ("binding_sha256", "bad")):
            row = record()
            row[key] = value
            with self.assertRaises(ValueError):
                self.feedback.poll([row], self.github, self.jira)
        with self.assertRaises(ValueError):
            validate_manifest({"version": 1, "proposals": [self.row, self.row]})
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.jira.calls, [])

    def test_manifest_rebinding_and_abandonment_revival_rejected(self):
        self.poll()
        self.row["binding_sha256"] = "e" * 64
        with self.assertRaises(ValueError):
            self.poll()
        self.row["binding_sha256"] = "d" * 64
        self.row["lifecycle_state"] = "ABANDONED"
        self.assertEqual(self.poll(), "ABANDONED")
        self.row["lifecycle_state"] = "ACTIVE"
        with self.assertRaises(ValueError):
            self.poll()

    def test_abandoned_proposal_posts_once_without_github_reads(self):
        self.row["lifecycle_state"] = "ABANDONED"
        self.row["result"] = None
        self.assertEqual(self.poll(), "ABANDONED")
        self.poll()
        self.assertEqual(len(self.jira.calls), 1)
        self.assertEqual(self.github.calls, [])

    def test_safe_manifest_file_and_disabled_runtime(self):
        path = Path(self.temp.name) / "manifest.json"
        path.write_text(json.dumps({"version": 1, "proposals": [self.row]}))
        path.chmod(0o644)
        self.assertEqual(read_manifest(str(path)), [self.row])
        path.chmod(0o666)
        with self.assertRaises(ValueError):
            read_manifest(str(path))
        from src.github_jira.runtime import poll_ci_feedback
        with patch.dict("os.environ", {"AEGIS_GITHUB_CI_ENABLED": "0"}):
            self.assertIsNone(poll_ci_feedback())

    def test_ci_reader_requests_statuses_read_with_exact_scope_and_expiry(self):
        from src.github_jira.github import READ_PERMISSIONS, ReadOnlyInstallationTokenProvider

        permissions = {**READ_PERMISSIONS, "statuses": "read"}
        payload = {
            "token": "short-lived-reader",
            "permissions": permissions,
            "repositories": [{"full_name": REPOSITORY}],
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        }
        with patch("src.github_jira.github._mint_installation_token", return_value=payload) as mint:
            provider = ReadOnlyInstallationTokenProvider(1, 2, "/not-read.pem", include_statuses=True)
            self.assertEqual(provider(REPOSITORY), "short-lived-reader")
            provider(REPOSITORY)
            mint.assert_called_once_with(1, 2, "/not-read.pem", REPOSITORY, permissions=permissions)
        for field, value in (
            ("permissions", READ_PERMISSIONS),
            ("permissions", {**permissions, "contents": "write"}),
            ("expires_at", None),
            ("expires_at", (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()),
            ("repositories", [{"full_name": "owner/other"}]),
        ):
            bad = {**payload, field: value}
            with patch("src.github_jira.github._mint_installation_token", return_value=bad):
                with self.assertRaises(GitHubReadError):
                    ReadOnlyInstallationTokenProvider(1, 2, "/not-read.pem", include_statuses=True)(REPOSITORY)

    def test_exported_operator_snapshot_drives_real_watcher_contract(self):
        from core.change_management.deployment_auth import DeploymentActor
        from core.change_management.github_adapter import GitHubPullRequestResult
        from core.change_management.jira_proposals import JiraProposalService
        from core.change_management.policy import ChangePolicy
        from core.change_management.proposals import ChangeProposalStore

        requester = DeploymentActor("requester", frozenset({"requester"}))
        approver = DeploymentActor("reviewer", frozenset({"approver"}))
        executor = DeploymentActor("executor", frozenset({"executor"}))
        writes = []

        def create(change, **kwargs):
            kwargs["before_write"]()
            writes.append(kwargs["files"])
            return GitHubPullRequestResult(
                REPOSITORY, change.proposed_branch, HEAD, 7,
                f"https://github.com/{REPOSITORY}/pull/7",
            )

        adapter = SimpleNamespace(
            preflight=lambda *a, **kw: None,
            read_snapshot=lambda change, **kw: {p: b"old" + bytes([10]) for p in change.paths},
            create_pull_request=create,
        )
        store = ChangeProposalStore(Path(self.temp.name) / "operator.db")
        self.addCleanup(store.close)
        service = JiraProposalService(store, ChangePolicy(
            repositories=frozenset({REPOSITORY}), base_branches=frozenset({"main"}),
            allowed_path_prefixes=("docs",),
        ), adapter)
        prepared = service.prepare(
            issue_key="KAN-101", fingerprint="c" * 64, labels=["aegis:github-proposal"],
            repository=REPOSITORY, base_sha=BASE, files={"docs/test.md": b"new" + bytes([10])},
            actor=requester,
        )
        proposal_id, binding = prepared["proposal"]["proposal_id"], prepared["binding_sha256"]
        service.approve(proposal_id, binding=binding, actor=approver)
        service.execute(proposal_id, binding=binding, actor=executor)
        manifest = service.ci_manifest(actor=executor)
        rows = validate_manifest(manifest)
        github = GitHub(rows[0])
        self.assertEqual(self.feedback.poll(rows, github, self.jira), "CI_VERIFIED")
        github.checks["check_runs"][0]["conclusion"] = "failure"
        self.assertEqual(self.feedback.poll(rows, github, self.jira), "CI_FAILED")
        service.abandon(proposal_id, binding=binding, actor=approver)
        rows = validate_manifest(service.ci_manifest(actor=executor))
        self.assertEqual(self.feedback.poll(rows, github, self.jira), "ABANDONED")
        self.assertEqual(len(writes), 1)
        self.assertEqual(len(github.calls), 10)

    def test_newer_poll_supersedes_slow_observation(self):
        other = CIFeedback(self.path)
        self.addCleanup(other.close)
        original = observe
        nested = []

        def delayed(github, row):
            if not nested:
                nested.append(True)
                other.poll([self.row], self.github, self.jira)
            return original(github, row)

        with patch("src.github_jira.ci.observe", side_effect=delayed):
            self.assertEqual(self.poll(), "SUPERSEDED")
        self.assertEqual(len(self.jira.calls), 1)

    def test_jira_labels_preserve_unrelated_and_reject_agent_trigger(self):
        fields = SimpleNamespace(labels=["keep", "aegis:ci-verified", "aegis:pr-open"])
        updates, comments = [], []
        issue = SimpleNamespace(fields=fields, update=lambda **kwargs: updates.append(kwargs))
        client = SimpleNamespace(issue=lambda *a, **kw: issue, add_comment=lambda *a: comments.append(a))
        jira = JiraIncidentAdapter(client, "KAN")
        jira.add_ci_update("KAN-101", "Human review required", "aegis:ci-failed")
        self.assertEqual(updates[0]["fields"]["labels"], ["keep", "aegis:ci-failed"])
        with self.assertRaises(ValueError):
            jira.add_ci_update("OTHER-1", "text", "aegis:ci-failed")
        with self.assertRaises(ValueError):
            jira.add_ci_update("KAN-101", "@sre-agent merge", "aegis:ci-failed")


if __name__ == "__main__":
    unittest.main()