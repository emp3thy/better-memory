"""Unit tests for storage/session.py — identity resolution helpers."""

from __future__ import annotations

import pytest

from better_memory.storage import session as sess


def test_resolve_actor_id_returns_project() -> None:
    assert sess.resolve_actor_id("better-memory") == "better-memory"


def test_resolve_actor_id_returns_general_for_none() -> None:
    assert sess.resolve_actor_id(None) == "general"


def test_resolve_actor_id_returns_general_for_empty_string() -> None:
    assert sess.resolve_actor_id("") == "general"


def test_resolve_session_id_uses_env_first(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_SESSION_ID", "env-session-xyz")
    assert sess.resolve_session_id() == "env-session-xyz"


def test_resolve_session_id_falls_back_to_claude_code_env(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "code-env-session")
    assert sess.resolve_session_id() == "code-env-session"


def test_resolve_session_id_generates_when_no_env(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    sid = sess.resolve_session_id()
    assert isinstance(sid, str) and len(sid) >= 16


def test_resolve_namespace_project_reflections() -> None:
    assert sess.resolve_namespace("better-memory", "reflections") == "projects/better-memory/reflections/"


def test_resolve_namespace_project_episodes_nests_under_reflections() -> None:
    # Per spec line 123: reflectionConfiguration.namespaceTemplates must be a
    # prefix of (or equal to) the strategy's namespaceTemplates. Episodes nest
    # under reflections to satisfy that constraint.
    assert sess.resolve_namespace("better-memory", "episodes") == "projects/better-memory/reflections/episodes/"


def test_resolve_namespace_general_semantic() -> None:
    assert sess.resolve_namespace("general", "semantic") == "general/semantic/"


def test_resolve_namespace_project_retired() -> None:
    assert sess.resolve_namespace("better-memory", "retired") == "projects/better-memory/retired/"


def test_resolve_namespace_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        sess.resolve_namespace("better-memory", "bogus")  # type: ignore[arg-type]


def test_closure_event_payload_uses_role_other() -> None:
    """OTHER is the right enum value for a system-emitted closure marker
    (Conversational.role enum: ASSISTANT | USER | TOOL | OTHER per
    verified boto3 surface)."""
    payload = sess.closure_event_payload()
    assert isinstance(payload, list)
    assert len(payload) == 1
    block = payload[0]["conversational"]
    assert block["role"] == "OTHER"
    assert "Session complete" in block["content"]["text"]


def test_parse_hints_prose_splits_on_markdown_bullets() -> None:
    prose = "First hint here.\n- Second hint here.\n- Third hint."
    assert sess.parse_hints_prose(prose) == [
        "First hint here.",
        "Second hint here.",
        "Third hint.",
    ]


def test_parse_hints_prose_returns_single_element_when_no_bullets() -> None:
    prose = "Just one prose paragraph with no bullets at all."
    assert sess.parse_hints_prose(prose) == [prose]


def test_parse_hints_prose_handles_empty_string() -> None:
    assert sess.parse_hints_prose("") == []
