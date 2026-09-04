import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.change_management import ChangeApprovalError, ChangePolicy, ChangeProposalStore


DIFF = """diff --git a/core/a.py b/core/a.py
--- a/core/a.py
+++ b/core/a.py
@@ -1 +1 @@
-old
+new
"""
BASE = "a" * 40
FILES = {"core/a.py": b"new\n"}


class ChangeProposalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = ChangeProposalStore(Path(self.temp.name) / "changes.sqlite3"); self.addCleanup(self.store.close)
        self.policy = ChangePolicy(
            repositories=frozenset({"owner/repo"}), base_branches=frozenset({"main"}),
            allowed_path_prefixes=("core",),
        )

    def propose(self, requester="requester"):
        return self.store.propose(
            policy=self.policy, repository="owner/repo", base_branch="main",
            base_sha=BASE, proposed_branch="aegis/change", unified_diff=DIFF,
            file_contents=FILES, requested_by=requester,
        )

    def test_exact_change_approval_consumes_once_and_audits(self):
        proposal = self.propose()
        approved = self.store.approve(proposal["proposal_id"], approved_by="approver", current_base_sha=BASE)
        self.assertEqual(approved["status"], "APPROVED")
        consumed = self.store.consume(
            proposal["proposal_id"], diff_sha256=proposal["diff_sha256"],
            content_manifest_sha256=proposal["content_manifest_sha256"], current_base_sha=BASE, actor="executor",
        )
        self.assertEqual(consumed["status"], "CONSUMED")
        with self.assertRaises(ChangeApprovalError):
            self.store.consume(
                proposal["proposal_id"], diff_sha256=proposal["diff_sha256"],
                content_manifest_sha256=proposal["content_manifest_sha256"], current_base_sha=BASE, actor="executor",
            )
        statuses = [row[0] for row in self.store.connection.execute("select status from change_events")]
        self.assertTrue({"PROPOSED", "APPROVED", "CONSUMED", "EXECUTION_DENIED"} <= set(statuses))

    def test_self_approval_fails(self):
        proposal = self.propose("same")
        with self.assertRaisesRegex(ChangeApprovalError, "own change"):
            self.store.approve(proposal["proposal_id"], approved_by="same", current_base_sha=BASE)

    def test_changed_base_is_durably_stale(self):
        proposal = self.propose()
        with self.assertRaisesRegex(ChangeApprovalError, "base commit changed"):
            self.store.approve(proposal["proposal_id"], approved_by="approver", current_base_sha="b" * 40)
        self.assertEqual(self.store.get(proposal["proposal_id"])["status"], "STALE")

    def test_expiry_is_durable(self):
        proposal = self.propose()
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.store.connection.execute("update change_proposals set expires_at=? where proposal_id=?", (expired, proposal["proposal_id"])); self.store.connection.commit()
        with self.assertRaisesRegex(ChangeApprovalError, "expired"):
            self.store.approve(proposal["proposal_id"], approved_by="approver", current_base_sha=BASE)
        self.assertEqual(self.store.get(proposal["proposal_id"])["status"], "EXPIRED")

    def test_altered_diff_or_base_cannot_consume(self):
        for diff_hash, content_hash, base in (
            ("0" * 64, None, BASE),
            (None, "0" * 64, BASE),
            (None, None, "b" * 40),
        ):
            proposal = self.propose(); self.store.approve(proposal["proposal_id"], approved_by="approver", current_base_sha=BASE)
            with self.assertRaises(ChangeApprovalError):
                self.store.consume(
                    proposal["proposal_id"], diff_sha256=diff_hash or proposal["diff_sha256"],
                    content_manifest_sha256=content_hash or proposal["content_manifest_sha256"],
                    current_base_sha=base, actor="executor",
                )

    def test_file_snapshot_must_match_diff_paths(self):
        with self.assertRaisesRegex(ChangeApprovalError, "file snapshot"):
            self.store.propose(
                policy=self.policy, repository="owner/repo", base_branch="main",
                base_sha=BASE, proposed_branch="aegis/change", unified_diff=DIFF,
                file_contents={"core/other.py": b"new\n"}, requested_by="requester",
            )


if __name__ == "__main__":
    unittest.main()
