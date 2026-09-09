"""Autonomous LLM Traceback Diagnosis and Multi-File Patch Synthesizer for Aegis SRE Agent."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .github import GitHubReadClient

logger = logging.getLogger(__name__)

GEMINI_OPENAI_COMPATIBLE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
MAX_FILE_FETCH_BYTES = 262_144
MAX_PROMPT_FILES = 8


@dataclass(frozen=True)
class SynthesizedPatch:
    explanation: str
    files: dict[str, bytes]
    provider: str
    model: str


def parse_traceback_context(log_text: str) -> dict[str, Any]:
    """Extract failing test names, stack frame paths, and error messages from logs."""
    failed_tests: list[str] = []
    referenced_files: list[str] = []
    error_clues: list[str] = []

    # 1. Pytest failure lines: e.g. "FAILED tests/test_screener.py::test_foo - Error: ..."
    for line in log_text.splitlines():
        line_clean = line.strip()
        if line_clean.startswith("FAILED ") or " FAILED " in line_clean:
            failed_tests.append(line_clean)
        # Extract files mentioned in pytest summary
        m_pytest = re.search(r"\b([a-zA-Z0-9_\-./]+\.py)::(\w+)", line_clean)
        if m_pytest:
            f_path = m_pytest.group(1).lstrip("./")
            if f_path not in referenced_files:
                referenced_files.append(f_path)

    def _clean_path(raw_p: str) -> str:
        norm_path = raw_p.replace("\\", "/")
        for prefix in ("/home/runner/work/", "/app/", "./"):
            if prefix in norm_path:
                norm_path = norm_path.split(prefix, 1)[-1]
                parts = norm_path.split("/", 1)
                if len(parts) == 2 and not parts[0].endswith(".py"):
                    norm_path = parts[1]
        return norm_path.lstrip("/")

    # 2. Python standard traceback frame paths: File "path/to/file.py", line 123
    frame_matches = re.findall(r'File "([^"]+\.py)", line \d+', log_text)
    for frame in frame_matches:
        cleaned = _clean_path(frame)
        if cleaned.endswith(".py") and not cleaned.startswith("<") and cleaned not in referenced_files:
            referenced_files.append(cleaned)

    # 3. Pytest traceback frame paths: path/to/file.py:123: in function_name
    pytest_frames = re.findall(r"(?:^|\s)([a-zA-Z0-9_\-./]+\.py):\d+:\s+in\s+", log_text, re.MULTILINE)
    for frame in pytest_frames:
        cleaned = _clean_path(frame)
        if cleaned.endswith(".py") and not cleaned.startswith("<") and cleaned not in referenced_files:
            referenced_files.append(cleaned)

    # 3. Stack trace lines / exception summaries
    for line in log_text.splitlines():
        line_clean = line.strip()
        if any(
            err in line_clean
            for err in (
                "Exception:",
                "Error:",
                "HTTPException",
                "Connect call failed",
                "RuntimeError",
                "AssertionError",
            )
        ):
            if line_clean not in error_clues:
                error_clues.append(line_clean)

    # 4. Extract pytest FAILURES block if present
    failures_block = ""
    if "=== FAILURES ===" in log_text or "FAILURES" in log_text:
        match = re.search(r"(=+\s*FAILURES\s*=+.+?)(?:=+\s*short test summary info|\Z)", log_text, re.DOTALL)
        if match:
            failures_block = match.group(1)[:10_000]

    return {
        "failed_tests": failed_tests[:5],
        "referenced_files": referenced_files[:15],
        "error_clues": error_clues[:10],
        "failures_block": failures_block or "\n".join(error_clues[:15]),
    }


def discover_relevant_files(
    tree: list[str], referenced_files: Iterable[str]
) -> list[str]:
    """Select the most relevant project files to provide to the LLM for diagnosis."""
    tree_set = set(tree)
    selected: list[str] = []

    # Priority 1: Files explicitly referenced in the traceback that exist in the repo
    for rf in referenced_files:
        if rf in tree_set and rf not in selected:
            selected.append(rf)
        else:
            # Check if any path in tree ends with rf
            matches = [p for p in tree if p.endswith(rf) or rf.endswith(p)]
            for m in matches:
                if m not in selected:
                    selected.append(m)

    # Priority 2: Key test fixtures and configuration files
    conftest_candidates = [
        "tests/conftest.py",
        "conftest.py",
        "tests/e2e/conftest.py",
        "pytest.ini",
        "setup.cfg",
        "pyproject.toml",
    ]
    for c in conftest_candidates:
        if c in tree_set and c not in selected:
            selected.append(c)

    return selected[:MAX_PROMPT_FILES]


def _get_client_and_model() -> tuple[Any, str, str]:
    """Initialize OpenAI or Gemini client using available environment credentials."""
    primary = os.getenv("SRE_LLM_PROVIDER", "auto").strip().casefold()
    fallback = os.getenv("SRE_LLM_FALLBACK_PROVIDER", "gemini").strip().casefold()

    candidates = [primary, fallback] if primary in {"openai", "gemini"} else ["openai", fallback, "gemini"]
    keys = {"openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}

    for provider in candidates:
        api_key = os.getenv(keys.get(provider, ""))
        if not api_key:
            continue
        from openai import OpenAI

        if provider == "openai":
            model = os.getenv("SRE_OPENAI_MODEL", "gpt-4.1-mini")
            return OpenAI(api_key=api_key), model, "openai"
        if provider == "gemini":
            model = os.getenv("SRE_GEMINI_MODEL", "gemini-2.5-flash")
            return (
                OpenAI(api_key=api_key, base_url=GEMINI_OPENAI_COMPATIBLE_BASE_URL),
                model,
                "gemini",
            )

    raise RuntimeError("No LLM credentials configured (OPENAI_API_KEY or GEMINI_API_KEY required)")


def synthesize_patch_with_llm(
    repository: str,
    base_sha: str,
    log_text: str,
    github: GitHubReadClient,
    symptom: str = "",
    likely_cause: str = "",
) -> Optional[SynthesizedPatch]:
    """Autonomously diagnose failure, read project files, and synthesize a code patch."""
    try:
        client, model, provider = _get_client_and_model()
    except Exception as e:
        logger.warning(f"Cannot initialize LLM client for patch synthesis: {e}")
        return None

    # 1. Parse traceback and find failing files
    diag = parse_traceback_context(log_text)

    # 2. Query full repository tree to discover all project files
    tree = github.get_tree(repository, base_sha)
    if not tree:
        # Fallback to referenced files if tree cannot be retrieved
        tree = list(diag["referenced_files"])

    # 3. Read relevant project files
    files_to_read = discover_relevant_files(tree, diag["referenced_files"])
    project_files = github.get_multiple_files(
        repository, files_to_read, base_sha, max_total_bytes=MAX_FILE_FETCH_BYTES
    )

    if not project_files:
        logger.warning("No project files could be fetched for LLM synthesis")
        return None

    # 4. Construct prompt for LLM
    files_payload = ""
    for path, content in project_files.items():
        files_payload += f"\n--- BEGIN FILE: {path} ---\n{content}\n--- END FILE: {path} ---\n"

    tree_summary = "\n".join(tree[:100])
    if len(tree) > 100:
        tree_summary += f"\n... ({len(tree) - 100} more files)"

    system_prompt = (
        "You are the Aegis Autonomous SRE Agent. A continuous integration test suite failed.\n"
        "Your task is to analyze the failure, review the repository code and project standards, and produce\n"
        "a minimal, high-quality, verified code patch that resolves the issue.\n\n"
        "Guidelines:\n"
        "1. Identify the root cause from the failure logs and traceback.\n"
        "2. Review the provided project files (including conftest / fixtures) to follow established patterns.\n"
        "   - If an isolated unit test failed because of an unmocked/external connection (e.g. Postgres DB,\n"
        "     external API), inspect how other tests or conftest fixtures isolate it (e.g. monkeypatching\n"
        "     get_db or using isolated sqlite), and adhere to that standard.\n"
        "   - If production code needs defensive delegation or fallback, implement it cleanly.\n"
        "3. Output MUST be strict JSON in the following schema:\n"
        "{\n"
        '  "explanation": "concise description of the root cause and why the fix works",\n'
        '  "files": {\n'
        '    "path/to/modified_file.py": "COMPLETE full file content of modified file with fix applied"\n'
        "  }\n"
        "}\n"
        "Do NOT return markdown code blocks around the JSON. Return raw parseable JSON only.\n"
        "Provide the complete modified file content so it can be committed directly to Git."
    )

    user_prompt = (
        f"Repository: {repository}\n"
        f"Base Commit: {base_sha}\n"
        f"Symptom: {symptom}\n"
        f"Likely Cause: {likely_cause}\n\n"
        f"Failed Tests:\n{json.dumps(diag['failed_tests'], indent=2)}\n\n"
        f"Traceback / Failure Logs:\n{diag['failures_block']}\n\n"
        f"Repository Tree (sample):\n{tree_summary}\n\n"
        f"Current Project Files:\n{files_payload}\n"
    )

    try:
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
        )
        response_text = completion.choices[0].message.content or ""
        response_text = response_text.strip()
        # Clean any markdown fences if present
        if response_text.startswith("```json"):
            response_text = response_text[7:]
        elif response_text.startswith("```"):
            response_text = response_text[3:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]
        response_text = response_text.strip()

        data = json.loads(response_text)
        explanation = str(data.get("explanation", "Autonomous patch synthesized by SRE Agent"))
        files_dict = data.get("files", {})

        if not isinstance(files_dict, dict) or not files_dict:
            logger.warning(f"LLM returned no valid files dictionary: {data}")
            return None

        clean_files: dict[str, bytes] = {}
        for path, code in files_dict.items():
            if not isinstance(code, str) or not code.strip():
                continue
            # Ensure proper LF and trailing newline
            code_clean = code.replace("\r\n", "\n").rstrip() + "\n"
            clean_files[path] = code_clean.encode("utf-8")

        if not clean_files:
            return None

        return SynthesizedPatch(
            explanation=explanation,
            files=clean_files,
            provider=provider,
            model=model,
        )
    except Exception as e:
        logger.error(f"Error during LLM patch synthesis: {e}", exc_info=True)
        return None
