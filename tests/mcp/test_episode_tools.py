"""Integration tests for the episode MCP tools.

Rather than invoking the registered handlers via MCP framework internals
(which vary across SDK versions), these tests exercise the same code path
by constructing the services directly and also verifying the factory
registers each tool by name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.episode import EpisodeService


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


class TestStartEpisodeTool:
    def test_start_episode_via_service(self, conn):
        """The tool is a thin wrapper; verify the service call shape."""
        svc = EpisodeService(conn)
        episode_id = svc.start_foreground(
            session_id="sess-1",
            project="proj",
            goal="test goal",
            tech="python",
        )
        row = conn.execute(
            "SELECT goal, tech FROM episodes WHERE id = ?",
            (episode_id,),
        ).fetchone()
        assert row["goal"] == "test goal"
        assert row["tech"] == "python"

    def test_tool_is_registered_in_factory(self):
        """The MCP server registers memory.start_episode by name."""
        from better_memory.mcp.server import _tool_definitions

        tool_names = {t.name for t in _tool_definitions()}
        assert "memory.start_episode" in tool_names


class TestCloseEpisodeTool:
    def test_close_via_service(self, conn):
        """The MCP tool is a thin wrapper; verify the service call shape."""
        svc = EpisodeService(conn)
        svc.start_foreground(
            session_id="sess-1", project="p", goal="g"
        )
        closed_id = svc.close_active(
            session_id="sess-1",
            outcome="abandoned",
            close_reason="abandoned",
            summary="stopped by user",
        )
        row = conn.execute(
            "SELECT outcome, summary FROM episodes WHERE id = ?",
            (closed_id,),
        ).fetchone()
        assert row["outcome"] == "abandoned"
        assert row["summary"] == "stopped by user"

    def test_tool_is_registered_in_factory(self):
        from better_memory.mcp.server import _tool_definitions

        tool_names = {t.name for t in _tool_definitions()}
        assert "memory.close_episode" in tool_names


class TestReconcileEpisodesTool:
    def test_returns_unclosed_from_other_sessions(self, conn):
        svc = EpisodeService(conn)
        svc.open_background(session_id="sess-prior", project="p")
        svc.open_background(session_id="sess-current", project="p")

        unclosed = svc.unclosed_episodes(
            exclude_session_ids={"sess-current"}
        )
        assert len(unclosed) == 1
        assert unclosed[0].project == "p"

    def test_tool_is_registered_in_factory(self):
        from better_memory.mcp.server import _tool_definitions

        tool_names = {t.name for t in _tool_definitions()}
        assert "memory.reconcile_episodes" in tool_names


class TestListEpisodesTool:
    def test_filters_work_via_service(self, conn):
        svc = EpisodeService(conn)
        svc.open_background(session_id="s1", project="proj-a")
        svc.open_background(session_id="s2", project="proj-b")

        result = svc.list_episodes(project="proj-a")
        assert len(result) == 1
        assert result[0].project == "proj-a"

    def test_tool_is_registered_in_factory(self):
        from better_memory.mcp.server import _tool_definitions

        tool_names = {t.name for t in _tool_definitions()}
        assert "memory.list_episodes" in tool_names


class TestServerStartupBackgroundEpisode:
    async def test_background_episode_opens_on_create_server(
        self, tmp_path, monkeypatch
    ):
        """Constructing the MCP server opens a background episode.

        `get_config()` reads env vars each call (not cached) per
        better_memory/config.py, so setting BETTER_MEMORY_HOME via
        monkeypatch is sufficient — no cache-reset needed.
        """
        home = tmp_path / "bm"
        home.mkdir()
        (home / "knowledge-base").mkdir()
        monkeypatch.setenv("BETTER_MEMORY_HOME", str(home))

        from better_memory import config
        from better_memory.mcp.server import create_server

        server, cleanup = create_server()
        try:
            from better_memory.db.connection import connect

            resolved = config.get_config()
            conn = connect(resolved.memory_db)
            try:
                rows = conn.execute(
                    "SELECT id, goal, ended_at FROM episodes"
                ).fetchall()
                assert len(rows) == 1
                assert rows[0]["goal"] is None
                assert rows[0]["ended_at"] is None
            finally:
                conn.close()
        finally:
            await cleanup()
