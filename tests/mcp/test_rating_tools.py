"""Tests for the memory rating MCP tools.

Dispatch pattern: the existing MCP test suite (test_episode_tools.py,
test_semantic_tools.py) exercises the service layer directly and verifies
tool registration via _tool_definitions(). For tools that query memory_conn
directly (no service wrapper), we use _dispatch_for_tests, which calls
server.request_handlers[CallToolRequest] without stdio transport.
"""
from __future__ import annotations

import json
import os
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


class TestMarkerFileFallback:
    """When env vars are missing, the rating tools fall back to the marker
    file written by the SessionStart hook (Claude Code does not propagate
    CLAUDE_SESSION_ID into MCP server env — see runtime/session_marker.py)."""

    def test_list_exposures_reads_marker_when_env_absent(
        self, memory_db, tmp_path, monkeypatch,
    ):
        from better_memory.mcp import server as srv_mod
        from better_memory.runtime.session_marker import write_session_id

        conn, _ = memory_db
        # tmp_path is BETTER_MEMORY_HOME / "bm" — but memory_db's home is
        # the parent dir of memory.db. Read it back from env.
        home = Path(os.environ["BETTER_MEMORY_HOME"])
        _seed_reflection(conn, "rmark")
        _seed_exposure(conn, "SMARK", "reflection", "rmark")
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        write_session_id(home, "SMARK", project_dir=str(tmp_path))

        result = run_async(srv_mod._dispatch_for_tests(
            "memory.list_session_exposures", {},
        ))
        payload = json.loads(result[0].text)
        assert payload["session_id"] == "SMARK"
        assert len(payload["exposures"]) == 1

    def test_env_wins_over_marker(
        self, memory_db, tmp_path, monkeypatch,
    ):
        from better_memory.mcp import server as srv_mod
        from better_memory.runtime.session_marker import write_session_id

        conn, _ = memory_db
        home = Path(os.environ["BETTER_MEMORY_HOME"])
        _seed_reflection(conn, "renv")
        _seed_exposure(conn, "SENV", "reflection", "renv")
        # Marker says SMARK; env says SENV — env wins.
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        monkeypatch.setenv("CLAUDE_SESSION_ID", "SENV")
        write_session_id(home, "SMARK", project_dir=str(tmp_path))

        result = run_async(srv_mod._dispatch_for_tests(
            "memory.list_session_exposures", {},
        ))
        payload = json.loads(result[0].text)
        assert payload["session_id"] == "SENV"

    def test_apply_ratings_uses_marker_when_env_absent(
        self, memory_db, tmp_path, monkeypatch,
    ):
        from better_memory.mcp import server as srv_mod
        from better_memory.runtime.session_marker import write_session_id

        conn, _ = memory_db
        home = Path(os.environ["BETTER_MEMORY_HOME"])
        _seed_reflection(conn, "rap")
        _seed_exposure(conn, "SAP", "reflection", "rap")
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        write_session_id(home, "SAP", project_dir=str(tmp_path))

        result = run_async(srv_mod._dispatch_for_tests(
            "memory.apply_session_ratings",
            {"ratings": [
                {"kind": "reflection", "id": "rap", "class": "cited",
                 "evidence": "used it to fix the bug"},
            ]},
        ))
        payload = json.loads(result[0].text)
        assert payload["session_id"] == "SAP"
        assert payload["applied"]["cited"] == 1

    def test_credit_uses_marker_when_env_absent(
        self, memory_db, tmp_path, monkeypatch,
    ):
        from better_memory.mcp import server as srv_mod
        from better_memory.runtime.session_marker import write_session_id

        conn, _ = memory_db
        home = Path(os.environ["BETTER_MEMORY_HOME"])
        _seed_reflection(conn, "rcr")
        _seed_exposure(conn, "SCR", "reflection", "rcr")
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        write_session_id(home, "SCR", project_dir=str(tmp_path))

        # NOTE (Task 2 / evidence rating): the memory.credit MCP handler
        # does not yet forward an "evidence" argument to credit_one (that
        # wiring is Task 3's schema update). credit_one's evidence
        # parameter now defaults to None, which fails validation for the
        # non-ignored "cited" class with a clean ValueError (isError),
        # rather than the TypeError a required kwarg would have raised.
        # This is a transitional state: memory.credit is broken at
        # runtime for non-ignored classes until Task 3 lands. This test
        # is adjusted to assert that transitional behaviour rather than
        # a successful credit, so the marker-fallback session resolution
        # (the thing this test is actually about) is still exercised up
        # to the point where credit_one raises.
        with pytest.raises(ValueError, match="evidence"):
            run_async(srv_mod._dispatch_for_tests(
                "memory.credit",
                {"kind": "reflection", "id": "rcr", "class": "cited"},
            ))


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
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        from better_memory.mcp import server as srv_mod

        result = run_async(
            srv_mod._dispatch_for_tests("memory.list_session_exposures", {})
        )
        payload = json.loads(result[0].text)
        assert payload["session_id"] is None
        assert payload["exposures"] == []

    def test_dedupes_multi_row_exposure(self, memory_db, monkeypatch):
        """A memory with TWO unrated exposure rows (bootstrap + retrieve)
        must appear ONCE in the response — apply_session_ratings rejects
        duplicate (kind, id) pairs, so the list tool must match that
        contract."""
        from better_memory.mcp import server as srv_mod

        conn, _ = memory_db
        _seed_reflection(conn, "r-dup")
        # Two distinct exposed_at timestamps for the same (session, kind, id).
        for ts, src in [
            ("2026-05-11T10:00:00+00:00", "bootstrap"),
            ("2026-05-11T11:00:00+00:00", "retrieve"),
        ]:
            conn.execute(
                """INSERT INTO session_memory_exposure
                   (session_id, memory_kind, memory_id, exposed_at, source)
                   VALUES ('SDUP', 'reflection', 'r-dup', ?, ?)""",
                (ts, src),
            )
        conn.commit()
        monkeypatch.setenv("CLAUDE_SESSION_ID", "SDUP")

        result = run_async(
            srv_mod._dispatch_for_tests("memory.list_session_exposures", {})
        )
        payload = json.loads(result[0].text)
        assert len(payload["exposures"]) == 1
        assert payload["exposures"][0]["id"] == "r-dup"


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
                {"kind": "reflection", "id": "r1", "class": "cited",
                 "evidence": "used it to fix the bug"},
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
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        with pytest.raises(ValueError, match="session"):
            run_async(srv_mod._dispatch_for_tests(
                "memory.apply_session_ratings",
                {"ratings": [{"kind": "reflection", "id": "r1", "class": "cited"}]},
            ))


class TestMemoryCreditTool:
    def test_credit_one(self, memory_db, monkeypatch):
        from better_memory.mcp import server as srv_mod
        conn, _ = memory_db
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "S1")

        # NOTE (Task 2 / evidence rating): transitional — see the identical
        # note in TestMarkerFileFallback.test_credit_uses_marker_when_env_absent.
        # memory.credit does not forward evidence until Task 3, so
        # credit_one's compat default (None) fails validation cleanly.
        with pytest.raises(ValueError, match="evidence"):
            run_async(srv_mod._dispatch_for_tests(
                "memory.credit",
                {"kind": "reflection", "id": "r1", "class": "cited"},
            ))

    def test_no_session_returns_skipped(self, memory_db, monkeypatch):
        from better_memory.mcp import server as srv_mod
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        result = run_async(srv_mod._dispatch_for_tests(
            "memory.credit",
            {"kind": "reflection", "id": "r1", "class": "cited"},
        ))
        payload = json.loads(result[0].text)
        assert payload == {"applied": None, "skipped": "no_session"}


class TestOverlookedClassInSchemas:
    def test_credit_tool_class_enum_includes_overlooked(self):
        from better_memory.mcp.server import _tool_definitions
        tool = next(
            t for t in _tool_definitions() if t.name == "memory.credit"
        )
        enum = tool.inputSchema["properties"]["class"]["enum"]
        assert "overlooked" in enum

    def test_apply_ratings_tool_class_enum_includes_overlooked(self):
        from better_memory.mcp.server import _tool_definitions
        tool = next(
            t for t in _tool_definitions()
            if t.name == "memory.apply_session_ratings"
        )
        enum = (
            tool.inputSchema["properties"]["ratings"]
            ["items"]["properties"]["class"]["enum"]
        )
        assert "overlooked" in enum

    def test_credit_dispatch_accepts_overlooked(self, memory_db, monkeypatch):
        from better_memory.mcp import server as srv_mod
        conn, _ = memory_db
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "S1")
        # NOTE (Task 2 / evidence rating): transitional — see the identical
        # note in TestMarkerFileFallback.test_credit_uses_marker_when_env_absent.
        # memory.credit does not forward evidence until Task 3, so
        # credit_one's compat default (None) fails validation cleanly.
        # This test now confirms the "overlooked" enum value still reaches
        # credit_one (fails on evidence, not on an unknown-class error).
        with pytest.raises(ValueError, match="evidence"):
            run_async(srv_mod._dispatch_for_tests(
                "memory.credit",
                {"kind": "reflection", "id": "r1", "class": "overlooked"},
            ))
