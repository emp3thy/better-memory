"""Integration tests for the memory.session_bootstrap MCP tool.

The handler is a thin passthrough over ``SessionBootstrapService``. We
verify three things at the contract boundary (no MCP framework
internals — those vary across SDK versions):

1. ``memory.session_bootstrap`` is registered as a Tool by name with the
   expected inputSchema (source enum, session_id, cwd).
2. The dispatch path produces the documented JSON wire shape:
   ``{additionalContext, project, source, episode: {id, action},
   counts: {semantic, reflections}}``.
3. ``create_server()`` wires the tool end-to-end without raising.

Why mirror instead of invoke ``_call_tool``: that function is a closure
over the per-server SQLite connections + service singletons. Lifting it
to a module-level helper would touch every existing tool. Mirroring the
handler body is the established pattern (see test_start_ui_tool.py).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo so ``project_name(cwd)`` resolves to its name."""
    repo = tmp_path / "demo-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=str(repo), check=True)
    return repo


class TestSessionBootstrapToolRegistration:
    def test_tool_is_registered_in_factory(self) -> None:
        """memory.session_bootstrap appears in the tool list."""
        from better_memory.mcp.server import _tool_definitions

        tool_names = {t.name for t in _tool_definitions()}
        assert "memory.session_bootstrap" in tool_names

    def test_tool_inputschema_shape(self) -> None:
        """Schema mirrors the spec: source enum, session_id, cwd; no required."""
        from better_memory.mcp.server import _tool_definitions

        tool = next(
            t for t in _tool_definitions()
            if t.name == "memory.session_bootstrap"
        )
        schema = tool.inputSchema
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        props = schema["properties"]
        assert "source" in props
        assert set(props["source"]["enum"]) == {
            "startup", "resume", "clear", "compact",
        }
        assert "session_id" in props
        assert "cwd" in props
        # All optional — service supplies defaults.
        assert "required" not in schema or schema["required"] == []

    def test_tool_description_mentions_additional_context(self) -> None:
        from better_memory.mcp.server import _tool_definitions

        tool = next(
            t for t in _tool_definitions()
            if t.name == "memory.session_bootstrap"
        )
        assert tool.description is not None
        assert "additionalContext" in tool.description


class TestSessionBootstrapToolDispatch:
    def test_handler_body_returns_structured_payload(
        self, conn, git_repo: Path
    ) -> None:
        """Mirror the handler body to verify the wire JSON shape.

        Drives the same service code path with the same arg-defaulting
        the handler uses, then asserts the JSON envelope keys.
        """
        from better_memory.services.session_bootstrap import (
            SessionBootstrapService,
        )

        svc = SessionBootstrapService(conn)
        result = svc.bootstrap(
            source="startup",
            session_id="s-1",
            cwd=git_repo,
        )
        payload = {
            "additionalContext": result.additional_context,
            "project": result.project,
            "source": result.source,
            "episode": {
                "id": result.episode_id,
                "action": result.episode_action,
            },
            "counts": {
                "semantic": result.semantic_count,
                "reflections": result.reflections_counts,
            },
        }
        # Round-trip through JSON to mimic TextContent serialisation.
        wire = json.loads(json.dumps(payload))

        assert wire["project"] == "demo-repo"
        assert wire["source"] == "startup"
        assert wire["episode"]["action"] == "opened"
        assert wire["episode"]["id"]  # non-empty
        assert "additionalContext" in wire
        assert "## better-memory: session bootstrap" in wire["additionalContext"]
        assert wire["counts"]["semantic"] == 0
        assert wire["counts"]["reflections"] == {
            "do": 0, "dont": 0, "neutral": 0,
        }

    def test_unknown_source_coerces_to_startup(
        self, conn, git_repo: Path
    ) -> None:
        from better_memory.services.session_bootstrap import (
            SessionBootstrapService,
        )

        svc = SessionBootstrapService(conn)
        result = svc.bootstrap(
            source="bogus",
            session_id="s-2",
            cwd=git_repo,
        )
        assert result.source == "startup"

    def test_none_source_coerces_to_startup(
        self, conn, git_repo: Path
    ) -> None:
        """Service must accept None source (handler default fallback)."""
        from better_memory.services.session_bootstrap import (
            SessionBootstrapService,
        )

        svc = SessionBootstrapService(conn)
        result = svc.bootstrap(
            source=None,
            session_id="s-3",
            cwd=git_repo,
        )
        assert result.source == "startup"


class TestSessionBootstrapToolWiring:
    def test_create_server_wires_session_bootstrap_tool(
        self, tmp_path, monkeypatch
    ) -> None:
        """create_server() succeeds and the tool is exposed.

        Catches import errors / typos that would break server startup
        before any tool call lands.
        """
        from tests.conftest import run_async

        home = tmp_path / "bm"
        home.mkdir()
        (home / "knowledge-base").mkdir()
        monkeypatch.setenv("BETTER_MEMORY_HOME", str(home))

        from better_memory.mcp.server import create_server, _tool_definitions

        server, cleanup = create_server()
        try:
            assert server is not None
            names = {t.name for t in _tool_definitions()}
            assert "memory.session_bootstrap" in names
        finally:
            run_async(cleanup())
