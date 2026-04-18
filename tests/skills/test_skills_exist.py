"""Tests that the Phase 9 skill markdown files exist and contain key concepts."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parents[2] / "better_memory" / "skills"

SKILL_FILES = [
    "memory-retrieve.md",
    "memory-write.md",
    "memory-feedback.md",
    "session-close.md",
    "CLAUDE.snippet.md",
]


def _strip_fences(text: str) -> str:
    """Remove fenced code blocks and YAML front matter so body-only assertions are clean."""
    # YAML front matter at start of file.
    text = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.DOTALL)
    # Triple-backtick fenced blocks.
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    return text


@pytest.mark.parametrize("name", SKILL_FILES)
def test_skill_file_exists(name: str) -> None:
    path = SKILLS_DIR / name
    assert path.is_file(), f"missing skill file: {path}"


@pytest.mark.parametrize("name", SKILL_FILES)
def test_skill_file_non_trivial(name: str) -> None:
    path = SKILLS_DIR / name
    assert path.stat().st_size > 500, f"skill file too short (<=500 bytes): {path}"


def test_memory_retrieve_mentions_buckets() -> None:
    body = _strip_fences((SKILLS_DIR / "memory-retrieve.md").read_text(encoding="utf-8"))
    for term in ("do", "dont", "neutral"):
        assert term in body, f"memory-retrieve.md body missing term: {term}"


def test_memory_write_mentions_outcomes() -> None:
    body = _strip_fences((SKILLS_DIR / "memory-write.md").read_text(encoding="utf-8"))
    for term in ("outcome", "success", "failure"):
        assert term in body, f"memory-write.md body missing term: {term}"


def test_memory_feedback_mentions_record_use_and_outcome() -> None:
    body = _strip_fences((SKILLS_DIR / "memory-feedback.md").read_text(encoding="utf-8"))
    assert "record_use" in body, "memory-feedback.md body missing 'record_use'"
    assert "outcome" in body, "memory-feedback.md body missing 'outcome'"


def test_session_close_mentions_outcome() -> None:
    body = _strip_fences((SKILLS_DIR / "session-close.md").read_text(encoding="utf-8"))
    assert "outcome" in body, "session-close.md body missing 'outcome'"


def test_claude_snippet_references_all_skill_files() -> None:
    body = _strip_fences((SKILLS_DIR / "CLAUDE.snippet.md").read_text(encoding="utf-8"))
    for name in (
        "memory-retrieve.md",
        "memory-write.md",
        "memory-feedback.md",
        "session-close.md",
    ):
        assert name in body, f"CLAUDE.snippet.md missing reference to {name}"
