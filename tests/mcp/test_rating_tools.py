"""Tests for the memory rating MCP tools.

Dispatch pattern: the existing MCP test suite (test_episode_tools.py,
test_semantic_tools.py) exercises the service layer directly and verifies
tool registration via _tool_definitions(). For tools that query memory_conn
directly (no service wrapper), we use _dispatch_for_tests, which calls
server.request_handlers[CallToolRequest] without stdio transport.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from tests.conftest import run_async


@pytest.fixture
def memory_db(tmp_path: Path, monkeypatch):
    """Yield a populated memory DB and configure env so create_server uses it."""
    home = tmp_path / "bm"
    home.mkdir()
    (home / "knowledge-base").mkdir()
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(home))
    db_path = home / "memory.db"
    c = connect(db_path)
    apply_migrations(c)
    try:
        yield c, db_path
    finally:
        c.close()


def _seed_exposure(c, sid, kind, mid):
    c.execute(
        """INSERT INTO session_memory_exposure
           (session_id, memory_kind, memory_id, exposed_at, source)
           VALUES (?, ?, ?, '2026-05-11T11:00:00+00:00', 'bootstrap')""",
        (sid, kind, mid),
    )
    c.commit()


def _seed_reflection(c, rid):
    c.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at)
           VALUES (?, ?, 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01')""",
        (rid, rid),
    )
    c.commit()


def _seed_semantic(c, sid, content="some semantic content"):
    c.execute(
        """INSERT INTO semantic_memories
           (id, content, project, scope, created_at, updated_at)
           VALUES (?, ?, 'p', 'project', '2026-01-01', '2026-01-01')""",
        (sid, content),
    )
    c.commit()


class TestListSessionExposuresRegistered:
    def test_tool_is_in_definitions(self):
        from better_memory.mcp.server import _tool_definitions
        names = {t.name for t in _tool_definitions()}
        assert "memory.list_session_exposures" in names

    def test_tool_schema_has_no_required_properties(self):
        from better_memory.mcp.server import _tool_definitions
        tool = next(
            t for t in _tool_definitions()
            if t.name == "memory.list_session_exposures"
        )
        assert tool.inputSchema["additionalProperties"] is False
        assert tool.inputSchema["properties"] == {}
        assert "required" not in tool.inputSchema


class TestListSessionExposures:
    def test_returns_unrated_for_current_session(self, memory_db, monkeypatch):
        """The tool reads CLAUDE_SESSION_ID from env and returns unrated rows.

        Note: _dispatch_for_tests opens its own DB connection (via create_server)
        separate from the fixture's conn. Both target the same on-disk file via
        BETTER_MEMORY_HOME. Commits in the fixture are required for the dispatch
        connection to see the seeded data.
        """
        from better_memory.mcp import server as srv_mod

        conn, _ = memory_db
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "S1")

        result = run_async(
            srv_mod._dispatch_for_tests("memory.list_session_exposures", {})
        )
        payload = json.loads(result[0].text)
        assert payload["session_id"] == "S1"
        assert len(payload["exposures"]) == 1
        assert payload["exposures"][0]["id"] == "r1"
        assert payload["exposures"][0]["kind"] == "reflection"
        assert "title" in payload["exposures"][0]
        assert "content" not in payload["exposures"][0]

    def test_returns_content_key_for_semantic_exposure(self, memory_db, monkeypatch):
        """Semantic exposures use the 'content' key, not 'title'."""
        from better_memory.mcp import server as srv_mod

        conn, _ = memory_db
        _seed_semantic(conn, "s1", content="prefer short filenames")
        _seed_exposure(conn, "S2", "semantic", "s1")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "S2")

        result = run_async(
            srv_mod._dispatch_for_tests("memory.list_session_exposures", {})
        )
        payload = json.loads(result[0].text)
        assert len(payload["exposures"]) == 1
        exp = payload["exposures"][0]
        assert exp["kind"] == "semantic"
        assert exp["id"] == "s1"
        assert exp["content"] == "prefer short filenames"
        assert "title" not in exp

    def test_empty_when_no_unrated(self, memory_db, monkeypatch):
        from better_memory.mcp import server as srv_mod

        monkeypatch.setenv("CLAUDE_SESSION_ID", "S1")
        result = run_async(
            srv_mod._dispatch_for_tests("memory.list_session_exposures", {})
        )
        payload = json.loads(result[0].text)
        assert payload["exposures"] == []

    def test_returns_null_session_when_env_missing(self, memory_db, monkeypatch):
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        from better_memory.mcp import server as srv_mod

        result = run_async(
            srv_mod._dispatch_for_tests("memory.list_session_exposures", {})
        )
        payload = json.loads(result[0].text)
        assert payload["session_id"] is None
        assert payload["exposures"] == []


class TestApplySessionRatingsTool:
    def test_applies_batch(self, memory_db, monkeypatch):
        from better_memory.mcp import server as srv_mod
        conn, _ = memory_db
        _seed_reflection(conn, "r1")
        _seed_reflection(conn, "r2")
        _seed_exposure(conn, "S1", "reflection", "r1")
        _seed_exposure(conn, "S1", "reflection", "r2")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "S1")

        result = run_async(srv_mod._dispatch_for_tests(
            "memory.apply_session_ratings",
            {"ratings": [
                {"kind": "reflection", "id": "r1", "class": "cited"},
                {"kind": "reflection", "id": "r2", "class": "ignored"},
            ]},
        ))
        payload = json.loads(result[0].text)
        assert payload["applied"]["cited"] == 1
        assert payload["applied"]["ignored"] == 1
        assert payload["session_id"] == "S1"

    def test_raises_value_error_when_env_missing(
        self, memory_db, monkeypatch,
    ):
        from better_memory.mcp import server as srv_mod
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        result = run_async(srv_mod._dispatch_for_tests(
            "memory.apply_session_ratings",
            {"ratings": [{"kind": "reflection", "id": "r1", "class": "cited"}]},
        ))
        # MCP framework catches exceptions and returns error text
        assert "session" in result[0].text.lower()
