import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.github_jira.dispatch import dispatch_github_incident
from src.github_jira.jira import JiraIncidentAdapter
from src.github_jira.models import parse_failure_webhook
from src.github_jira.service import GitHubJiraWorkflow
from src.github_jira.store import IncidentStore
from test_github_jira_phase_a import FakeGitHub, FakeJira, FIXTURE, REPOSITORY


class ProposalJira(FakeJira):
    def __init__(self):
        super().__init__()
        self.labels = ["aegis:github-proposal"]
        self.updates = []

    def proposal_labels(self, issue_key):
        return self.labels

    def add_proposal_update(self, *args):
        self.updates.append(args)


class PhaseBRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "incidents.db"
        self.store = IncidentStore(self.path)
        event = parse_failure_webhook("workflow_run", json.loads(FIXTURE.read_text()), {REPOSITORY})
        self.jira, self.github = ProposalJira(), FakeGitHub(event)
        self.workflow = GitHubJiraWorkflow(self.store, self.github, self.jira, "secret", {REPOSITORY})
        self.workflow.intake_event(event, "phase-b-fixture")

    def test_request_is_durable_deduplicated_and_never_a_write_approval(self):
        message = self.workflow.handle_command("KAN-101")
        self.workflow.handle_command("KAN-101")
        reopened = IncidentStore(self.path)
        self.assertTrue(reopened.proposal_requested("KAN-101"))
        self.assertEqual(len(self.jira.updates), 1)
        self.assertEqual(self.jira.updates[0][2], "aegis:approval-required")
        self.assertNotIn("@sre-agent", message)
        self.assertIn("cannot approve", message)
        self.assertEqual(self.github.fetch_count, 0)
        fingerprint = reopened.get_by_issue("KAN-101").fingerprint
        self.assertEqual(reopened.audit_event_types(fingerprint)[-1], "PROPOSAL_REQUESTED")
        self.assertEqual(reopened.integrity_check(), "ok")

    def test_label_without_trigger_does_not_create_request(self):
        self.assertFalse(self.store.proposal_requested("KAN-101"))
        self.assertEqual(self.jira.updates, [])

    def test_without_exact_label_existing_read_only_diagnosis_is_preserved(self):
        self.jira.labels = ["aegis:github-proposal-forged"]
        self.workflow.handle_command("KAN-101")
        self.assertEqual(self.github.fetch_count, 1)
        self.assertEqual(self.jira.updates, [])
        self.assertFalse(self.store.proposal_requested("KAN-101"))

    def test_unknown_issue_cannot_request_a_proposal(self):
        with self.assertRaises(KeyError):
            self.workflow.handle_command("KAN-999")
        self.assertEqual(self.jira.updates, [])

    def test_dispatch_routes_to_label_aware_handler_and_does_not_fall_through(self):
        with patch("src.github_jira.dispatch.get_workflow", return_value=self.workflow):
            self.assertTrue(asyncio.run(dispatch_github_incident("KAN-101")))
            self.assertFalse(asyncio.run(dispatch_github_incident("KAN-999")))
        self.assertEqual(len(self.jira.updates), 1)

    def test_jira_timeout_does_not_duplicate_acknowledgement(self):
        with patch.object(self.jira, "add_proposal_update", side_effect=TimeoutError()):
            with self.assertRaises(TimeoutError):
                self.workflow.handle_command("KAN-101")
        self.workflow.handle_command("KAN-101")
        self.assertTrue(self.store.proposal_requested("KAN-101"))
        self.assertEqual(self.jira.updates, [])

    def test_jira_state_preserves_unrelated_labels(self):
        issue = Mock()
        issue.fields.labels = ["keep-me", "aegis:github-proposal", "aegis:approval-required"]
        client = Mock()
        client.issue.return_value = issue
        jira = JiraIncidentAdapter(client, "KAN")
        jira.add_proposal_update("KAN-101", "safe proposal link", "aegis:pr-open")
        self.assertEqual(
            issue.update.call_args.kwargs["fields"]["labels"],
            ["keep-me", "aegis:github-proposal", "aegis:pr-open"],
        )
        with self.assertRaises(ValueError):
            jira.add_proposal_update("KAN-101", "no", "approved")

    def test_operator_incident_lookup_is_read_only_and_requires_request(self):
        import importlib.util
        script = Path(__file__).resolve().parents[3] / "scripts/jira_change_proposal.py"
        spec = importlib.util.spec_from_file_location("phase_b_cli", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with self.assertRaises(ValueError):
            module.incident_request(str(self.path), "KAN-101")
        self.workflow.handle_command("KAN-101")
        before = self.path.read_bytes()
        record = module.incident_request(str(self.path), "KAN-101")
        self.assertEqual(record["repository"], REPOSITORY)
        self.assertEqual(before, self.path.read_bytes())


if __name__ == "__main__":
    unittest.main()