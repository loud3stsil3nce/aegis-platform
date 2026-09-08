"""Phase C lifecycle regression tests, including Phase B database migration."""
import hashlib
import unittest
from unittest.mock import patch

import test_jira_proposals as fixtures
from core.change_management.jira_proposals import JiraProposalService
from core.change_management.proposals import ChangeApprovalError, ChangeProposalStore


class LifecycleTests(unittest.TestCase):
    setUp = fixtures.JiraProposalTests.setUp
    prepare = fixtures.JiraProposalTests.prepare
    approve = fixtures.JiraProposalTests.approve
    execute = fixtures.JiraProposalTests.execute

    def abandon(self, record, actor=fixtures.APPROVER):
        return self.service.abandon(
            record["proposal"]["proposal_id"], binding=record["binding_sha256"], actor=actor,
        )

    def retry(self, record, **kwargs):
        return self.prepare(retry_of=record["proposal"]["proposal_id"],
                            retry_binding=record["binding_sha256"], **kwargs)

    def test_abandon_revokes_approval_and_preserves_exact_snapshot(self):
        original = self.prepare()
        self.approve(original)
        abandoned = self.abandon(original)
        self.assertEqual(abandoned["lifecycle_state"], "ABANDONED")
        self.assertEqual(abandoned["binding_sha256"], original["binding_sha256"])
        self.assertEqual(abandoned["snapshot"], original["snapshot"])
        with self.assertRaises(ChangeApprovalError):
            self.execute(original)
        with self.assertRaises(ChangeApprovalError):
            self.approve(original)
        self.assertEqual(self.adapter.writes, [])

    def test_retry_has_new_id_branch_binding_and_requires_new_approval(self):
        original = self.prepare()
        self.approve(original)
        self.execute(original)
        self.abandon(original)
        new = self.retry(original, files={"docs/test.md": b"second\n"})
        for key in ("proposal_id", "proposed_branch", "content_manifest_sha256"):
            self.assertNotEqual(new["proposal"][key], original["proposal"][key])
        self.assertNotEqual(new["binding_sha256"], original["binding_sha256"])
        self.assertIsNone(new["approval_sha256"])
        self.assertEqual(new["proposal"]["status"], "PENDING")
        with self.assertRaises(ChangeApprovalError):
            self.execute(new)
        self.approve(new)
        self.execute(new)
        self.assertEqual(len(self.adapter.writes), 2)
        self.assertEqual(self.service.inspect(original["proposal"]["proposal_id"])["snapshot"], original["snapshot"])
        with self.assertRaises(ChangeApprovalError):
            self.retry(original)

    def test_retry_rejects_active_wrong_incident_and_wrong_binding(self):
        original = self.prepare()
        with self.assertRaises(ChangeApprovalError):
            self.retry(original)
        self.abandon(original)
        with self.assertRaises(ChangeApprovalError):
            self.retry(original, issue_key="KAN-102")
        with self.assertRaises(ChangeApprovalError):
            self.retry(original, fingerprint="d" * 64)
        with self.assertRaises(ChangeApprovalError):
            self.prepare(retry_of=original["proposal"]["proposal_id"], retry_binding="0" * 64)
        with self.assertRaises(ChangeApprovalError):
            self.prepare()  # Cannot evade explicit retry history.

    def test_roles_and_repeated_abandonment(self):
        record = self.prepare()
        with self.assertRaises(PermissionError):
            self.abandon(record, fixtures.REQUESTER)
        with self.assertRaises(PermissionError):
            self.service.ci_manifest(actor=fixtures.APPROVER)
        self.abandon(record)
        with self.assertRaises(ChangeApprovalError):
            self.abandon(record)

    def test_indeterminate_execution_cannot_be_abandoned_or_retried(self):
        record = self.prepare()
        self.approve(record)
        self.adapter.failure = True
        with self.assertRaises(RuntimeError):
            self.execute(record)
        with self.assertRaises(ChangeApprovalError):
            self.abandon(record)
        with self.assertRaises(ChangeApprovalError):
            self.retry(record)
        self.assertEqual(len(self.adapter.writes), 1)

    def test_abandon_audit_failure_rolls_back_revocation(self):
        record = self.prepare()
        with patch.object(self.store, "_event", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                self.abandon(record)
        self.assertEqual(self.service.inspect(record["proposal"]["proposal_id"])["lifecycle_state"], "ACTIVE")

    def test_export_contains_exact_git_blob_hashes_not_code_or_credentials(self):
        record = self.prepare()
        self.assertEqual(self.service.ci_manifest(actor=fixtures.EXECUTOR)["proposals"], [])
        self.approve(record)
        self.execute(record)
        manifest = self.service.ci_manifest(actor=fixtures.EXECUTOR)
        item = manifest["proposals"][0]
        self.assertEqual(item["files"], {"docs/test.md": hashlib.sha1(b"blob 4" + bytes([0]) + b"new\n").hexdigest()})
        self.assertNotIn("after", item)
        self.assertNotIn("approval_sha256", item)
        self.abandon(record)
        self.assertEqual(self.service.ci_manifest(actor=fixtures.EXECUTOR)["proposals"][0]["lifecycle_state"], "ABANDONED")

    def test_legacy_unique_issue_schema_migrates_without_rebinding(self):
        record = self.prepare()
        self.approve(record)
        before = self.service.inspect(record["proposal"]["proposal_id"])
        with self.store.connection:
            self.store.connection.executescript("""
                DROP INDEX jira_one_active_proposal;
                ALTER TABLE jira_change_snapshots RENAME TO snapshots_new;
                CREATE TABLE jira_change_snapshots (
                    proposal_id TEXT PRIMARY KEY, issue_key TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL, binding_sha256 TEXT NOT NULL,
                    approval_sha256 TEXT, approval_actor TEXT, result_json TEXT,
                    notification_state TEXT NOT NULL DEFAULT 'PENDING'
                );
                INSERT INTO jira_change_snapshots SELECT
                    proposal_id,issue_key,payload,binding_sha256,approval_sha256,
                    approval_actor,result_json,notification_state FROM snapshots_new;
                DROP TABLE snapshots_new;
            """)
        reopened = ChangeProposalStore(self.path)
        self.addCleanup(reopened.close)
        migrated = JiraProposalService(reopened, self.policy, self.adapter)
        self.assertEqual(migrated.inspect(record["proposal"]["proposal_id"]), before)
        self.assertEqual(reopened.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.execute(record, migrated)

    def test_only_one_successor_even_after_successor_abandoned(self):
        record = self.prepare()
        self.abandon(record)
        next_record = self.retry(record)
        self.abandon(next_record)
        with self.assertRaises(ChangeApprovalError):
            self.retry(record)
        third = self.retry(next_record)
        self.assertEqual(third["snapshot"]["retry_of"], next_record["proposal"]["proposal_id"])


if __name__ == "__main__":
    unittest.main()