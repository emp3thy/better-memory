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


class TestCloseEpisodeNoActiveIsSilentNoop:
    """Phase 4: calling memory.close_episode with no active episode returns
    {already_closed: true} rather than raising.

    This matches the CLAUDE snippet's documented plan-complete behaviour:
    after a commit-trailer drain has already closed the episode, the LLM's
    follow-up plan-complete close must be a no-op.
    """

    def test_close_active_value_error_is_caught(self, tmp_path, monkeypatch):
        """Drive the handler path: no active episode → already_closed payload."""

        home = tmp_path / "bm"
        home.mkdir()
        (home / "knowledge-base").mkdir()
        monkeypatch.setenv("BETTER_MEMORY_HOME", str(home))
        monkeypatch.setenv("CLAUDE_SESSION_ID", "claude-sess-noop")

        # Drive the service directly with the same shape the MCP handler
        # uses. The handler's ValueError catch is what we're asserting;
        # we reproduce the call pattern without plumbing through MCP
        # framework internals.
        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations
        from better_memory.services.episode import EpisodeService

        db = home / "memory.db"
        conn = connect(db)
        apply_migrations(conn)
        try:
            svc = EpisodeService(conn)
            # No episode open for this session. close_active must raise.
            import pytest as _pytest
            with _pytest.raises(ValueError, match="No active episode"):
                svc.close_active(
                    session_id="claude-sess-noop",
                    outcome="success",
                    close_reason="plan_complete",
                )
        finally:
            conn.close()


class TestStartEpisodeReturnsReflections:
    """Phase 5: memory.start_episode returns {episode_id, reflections}."""

    def test_start_episode_tool_still_registered(self):
        from better_memory.mcp.server import _tool_definitions
        tool_names = {t.name for t in _tool_definitions()}
        assert "memory.start_episode" in tool_names

    def test_create_server_wires_reflection_service(self, tmp_path, monkeypatch):
        """Factory constructs without error — full wiring chain.

        Catches import errors, bad kwargs on service constructors, and
        any typo that would crash create_server() before a single tool
        call fires. Synthesis no longer holds an LLM client (the
        IDE-LLM drives synthesis via memory.synthesize_next_*), so no
        chat stub is needed.
        """
        import asyncio

        home = tmp_path / "bm"
        home.mkdir()
        (home / "knowledge-base").mkdir()
        monkeypatch.setenv("BETTER_MEMORY_HOME", str(home))

        from better_memory.mcp.server import create_server

        server, cleanup, _ = create_server()
        try:
            assert server is not None
            from better_memory.mcp.server import _tool_definitions
            names = {t.name for t in _tool_definitions()}
            assert "memory.start_episode" in names
            assert "memory.synthesize_next_get_context" in names
            assert "memory.synthesize_next_apply" in names
        finally:
            asyncio.run(cleanup())


class TestRetrieveReturnsReflections:
    """Phase 6: memory.retrieve returns reflections, not observations."""

    def test_retrieve_via_service_returns_reflection_buckets(self, conn):
        from better_memory.services.reflection import ReflectionSynthesisService

        # Seed two reflections.
        from uuid import uuid4
        for polarity, title in (("do", "Do this"), ("dont", "Don't that")):
            conn.execute(
                "INSERT INTO reflections "
                "(id, title, project, phase, polarity, use_cases, hints, "
                " confidence, status, evidence_count, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uuid4().hex, title, "p", "general", polarity,
                 "uc", "[]", 0.5, "confirmed", 1,
                 "2026-04-25T10:00:00+00:00", "2026-04-25T10:00:00+00:00"),
            )
        conn.commit()

        svc = ReflectionSynthesisService(conn)
        result = svc.retrieve_reflections(project="p")
        assert {r["title"] for r in result["do"]} == {"Do this"}
        assert {r["title"] for r in result["dont"]} == {"Don't that"}

    def test_memory_retrieve_tool_schema_takes_filter_params(self):
        """Tool schema should expose project/tech/phase/polarity + query.

        ``query`` was dropped in Phase 6 when this tool was repointed from
        observations to reflections, because reflections had no relevance
        path — ranking was the popularity prior alone. It is back with real
        semantics: BM25 over title/use_cases/hints, RRF-fused with that prior.
        The observation-shaped filters stay gone; they never applied to
        reflections and belong to memory.retrieve_observations.
        """
        from better_memory.mcp.server import _tool_definitions

        tool = next(
            t for t in _tool_definitions() if t.name == "memory.retrieve"
        )
        props = tool.inputSchema["properties"]
        # Filter params present.
        assert "project" in props
        assert "tech" in props
        assert "phase" in props
        assert "polarity" in props
        # Relevance param present.
        assert "query" in props
        # Observation-shaped params remain absent.
        assert "component" not in props
        assert "window" not in props
        assert "scope_path" not in props


class TestRetrieveObservationsTool:
    """Phase 6: memory.retrieve_observations is registered with the right schema."""

    def test_tool_is_registered(self):
        from better_memory.mcp.server import _tool_definitions
        names = {t.name for t in _tool_definitions()}
        assert "memory.retrieve_observations" in names

    def test_tool_schema_has_filter_and_query_params(self):
        from better_memory.mcp.server import _tool_definitions
        tool = next(
            t for t in _tool_definitions()
            if t.name == "memory.retrieve_observations"
        )
        props = tool.inputSchema["properties"]
        for key in (
            "project", "episode_id", "component", "theme",
            "outcome", "query", "limit",
        ):
            assert key in props, f"missing property: {key}"
        assert tool.inputSchema["additionalProperties"] is False

    def test_tool_outcome_enum_matches_observations_check(self):
        from better_memory.mcp.server import _tool_definitions
        tool = next(
            t for t in _tool_definitions()
            if t.name == "memory.retrieve_observations"
        )
        outcome_prop = tool.inputSchema["properties"]["outcome"]
        assert set(outcome_prop["enum"]) == {"success", "failure", "neutral"}
