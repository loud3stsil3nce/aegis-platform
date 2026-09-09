"""Unit tests for Autonomous LLM Traceback Diagnosis and Patch Synthesizer."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sre_root = Path(__file__).resolve().parents[1]
if str(sre_root) not in sys.path:
    sys.path.insert(0, str(sre_root))

from src.github_jira.llm_synthesizer import (
    parse_traceback_context,
    discover_relevant_files,
    synthesize_patch_with_llm,
)


SAMPLE_PYTEST_LOG = """
============================= test session starts ==============================
platform linux -- Python 3.10.21, pytest-9.0.3, pluggy-1.6.0
rootdir: /home/runner/work/shariahcompliantscreener/shariahcompliantscreener
configfile: pytest.ini
testpaths: tests
plugins: anyio-4.13.0
collected 97 items
tests/test_screener.py ..F. [100%]
=================================== FAILURES ===================================
_______________________ test_multi_source_custom_sec_url _______________________
tests/test_screener.py:255: in test_multi_source_custom_sec_url
    response = asyncio.run(run_ai_audit("LIME", audit_input))
src/api.py:772: in run_ai_audit
    await loop.run_in_executor(None, lambda: run_screener(use_current_market_cap=False))
src/analysis/screener.py:34: in run_screener
    conn = get_db()
src/db/helpers.py:168: in execute
    records = run_sync(_execute())
E   fastapi.exceptions.HTTPException: 500: Multiple exceptions: [Errno 111] Connect call failed ('127.0.0.1', 5432)
=========================== short test summary info ============================
FAILED tests/test_screener.py::test_multi_source_custom_sec_url - fastapi.exceptions.HTTPException: 500
"""


class LLMSynthesizerTests(unittest.TestCase):
    def test_parse_traceback_context(self):
        diag = parse_traceback_context(SAMPLE_PYTEST_LOG)
        self.assertEqual(len(diag["failed_tests"]), 1)
        self.assertIn("test_multi_source_custom_sec_url", diag["failed_tests"][0])

        ref_files = diag["referenced_files"]
        self.assertIn("tests/test_screener.py", ref_files)
        self.assertIn("src/api.py", ref_files)
        self.assertIn("src/analysis/screener.py", ref_files)
        self.assertIn("src/db/helpers.py", ref_files)

        self.assertTrue(any("Connect call failed" in err for err in diag["error_clues"]))

    def test_discover_relevant_files(self):
        tree = [
            "README.md",
            "pytest.ini",
            "tests/conftest.py",
            "tests/test_screener.py",
            "src/api.py",
            "src/analysis/screener.py",
            "src/db/helpers.py",
            "src/utils.py",
        ]
        ref_files = ["tests/test_screener.py", "src/analysis/screener.py"]
        discovered = discover_relevant_files(tree, ref_files)

        self.assertIn("tests/test_screener.py", discovered)
        self.assertIn("src/analysis/screener.py", discovered)
        self.assertIn("tests/conftest.py", discovered)
        self.assertIn("pytest.ini", discovered)

    def test_synthesize_patch_with_llm_mocked(self):
        mock_github = MagicMock()
        mock_github.get_tree.return_value = [
            "tests/test_screener.py",
            "src/analysis/screener.py",
            "tests/conftest.py",
        ]
        mock_github.get_multiple_files.return_value = {
            "tests/test_screener.py": "# test screener file content\n",
            "src/analysis/screener.py": "# screener file content\n",
        }

        mock_llm_response = {
            "explanation": "Fix unmocked database connection by monkeypatching screener get_db",
            "files": {
                "tests/test_screener.py": "# patched test screener file content\n",
            },
        }

        mock_client = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps(mock_llm_response)
        mock_client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])

        with patch(
            "src.github_jira.llm_synthesizer._get_client_and_model",
            return_value=(mock_client, "gpt-4.1-mini", "openai"),
        ):
            patch_result = synthesize_patch_with_llm(
                repository="loud3stsil3nce/shariahcompliantscreener",
                base_sha="a7aee7a61590",
                log_text=SAMPLE_PYTEST_LOG,
                github=mock_github,
                symptom="CI failed",
                likely_cause="Run tests failed",
            )

        self.assertIsNotNone(patch_result)
        self.assertEqual(patch_result.provider, "openai")
        self.assertIn("tests/test_screener.py", patch_result.files)
        self.assertEqual(
            patch_result.files["tests/test_screener.py"],
            b"# patched test screener file content\n",
        )
        self.assertIn("Fix unmocked database connection", patch_result.explanation)


if __name__ == "__main__":
    unittest.main()
