"""MCP server selects the right StorageBackend and gates synthesis tools.

Also covers the agentcore dispatch wiring (spec Task 2): ``create_server``
passes ``remote=backend`` to the data-tool handlers iff
``config.storage_backend == "agentcore"`` — never on backend truthiness —
and gates the episode/retention tool surface via ``supports_episodes``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

EPISODE_GATED_TOOLS = {
    "memory.start_episode",
    "memory.close_episode",
    "memory.reconcile_episodes",
    "memory.list_episodes",
    "memory.run_retention",
}


def test_synthesis_tools_registered_when_capability_true() -> None:
    from better_memory.mcp.server import _tool_definitions

    tools = _tool_definitions(supports_synthesis=True)
    tool_names = {t.name for t in tools}
    assert "memory.synthesize_next_get_context" in tool_names
    assert "memory.synthesize_next_apply" in tool_names


def test_synthesis_tools_skipped_when_capability_false() -> None:
    from better_memory.mcp.server import _tool_definitions

    tools = _tool_definitions(supports_synthesis=False)
    tool_names = {t.name for t in tools}
    assert "memory.synthesize_next_get_context" not in tool_names
    assert "memory.synthesize_next_apply" not in tool_names


def test_create_server_returns_three_tuple_with_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """create_server returns (server, cleanup, ctx) with ctx.backend set."""
    home = tmp_path / "bm"
    home.mkdir()
    (home / "knowledge-base").mkdir()
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(home))
    monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "sqlite")
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)

    from better_memory.mcp.server import create_server
    from better_memory.storage import SqliteBackend

    result = create_server()
    try:
        assert isinstance(result, tuple) and len(result) == 3
        server, cleanup, ctx = result
        assert isinstance(ctx.backend, SqliteBackend)
    finally:
        asyncio.run(result[1]())


# ---------------------------------------------------------------------------
# supports_episodes tool-list gate (UD-1)
# ---------------------------------------------------------------------------


def test_episode_and_retention_tools_registered_by_default() -> None:
    """The new supports_episodes param must default True — every existing
    registration test calls tool_definitions() with defaults."""
    from better_memory.mcp.server import _tool_definitions

    names = {t.name for t in _tool_definitions()}
    assert EPISODE_GATED_TOOLS <= names


def test_episode_and_retention_tools_hidden_when_capability_false() -> None:
    from better_memory.mcp.server import _tool_definitions

    names = {t.name for t in _tool_definitions(supports_episodes=False)}
    assert not (EPISODE_GATED_TOOLS & names)
    # The rest of the surface is untouched by the episode gate.
    assert {"memory.observe", "memory.retrieve", "memory.semantic_observe"} <= names


def test_episode_gate_is_independent_of_synthesis_gate() -> None:
    from better_memory.mcp.server import _tool_definitions

    names = {
        t.name
        for t in _tool_definitions(supports_synthesis=False, supports_episodes=False)
    }
    assert not (EPISODE_GATED_TOOLS & names)
    assert "memory.synthesize_next_get_context" not in names
    assert "memory.observe" in names


# ---------------------------------------------------------------------------
# create_server dispatch wiring (agentcore remote vs sqlite None)
# ---------------------------------------------------------------------------


class _StubRemoteBackend:
    """Records data-tool calls; agentcore-shaped capability flags."""

    supports_synthesis = False
    supports_episodes = False

    def __init__(self) -> None:
        self.observe_calls: list[dict[str, Any]] = []
        self.bootstrap_calls: list[dict[str, Any]] = []

    async def observe(self, **kwargs: Any) -> str:
        self.observe_calls.append(kwargs)
        return "stub-evt-0001"

    def retrieve(self, **kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        return {"do": [], "dont": [], "neutral": []}

    def session_bootstrap(self, **kwargs: Any) -> dict[str, Any]:
        self.bootstrap_calls.append(kwargs)
        return {
            "additional_context": "stub-ctx",
            "project": "stub-proj",
            "source": kwargs.get("source") or "",
            "episode_id": kwargs.get("session_id") or "",
            "episode_action": "opened",
            "semantic_count": 0,
            "reflections_counts": {"do": 0, "dont": 0, "neutral": 0},
        }


def _server_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "bm"
    (home / "knowledge-base").mkdir(parents=True)
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(home))
    monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "sqlite")
    return home


async def _dispatch(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    """Drive one tool call through the wired server's CallTool handler."""
    from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult

    handler = server.request_handlers[CallToolRequest]
    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments),
    )
    result = await handler(req)
    assert isinstance(result.root, CallToolResult)
    return result.root


def _observation_row_count(home: Path) -> int:
    with closing(sqlite3.connect(home / "memory.db")) as conn:
        return conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]


async def test_agentcore_mode_wires_remote_into_data_handlers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """storage_backend == 'agentcore' → handlers receive the backend as
    ``remote``: memory.observe hits the backend and writes ZERO local rows."""
    home = _server_home(tmp_path, monkeypatch)
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")

    import better_memory.mcp.server as server_mod

    stub = _StubRemoteBackend()
    monkeypatch.setattr(server_mod, "build_backend", lambda **kwargs: stub)

    server, cleanup, ctx = server_mod.create_server()
    try:
        assert ctx.backend is stub
        result = await _dispatch(server, "memory.observe", {"content": "wired"})
        assert not result.isError
        assert json.loads(result.content[0].text) == {"id": "stub-evt-0001"}
        assert len(stub.observe_calls) == 1
        assert stub.observe_calls[0]["content"] == "wired"
        assert _observation_row_count(home) == 0
    finally:
        await cleanup()


async def test_agentcore_mode_session_bootstrap_unwraps_dict_over_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _server_home(tmp_path, monkeypatch)
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")

    import better_memory.mcp.server as server_mod

    stub = _StubRemoteBackend()
    monkeypatch.setattr(server_mod, "build_backend", lambda **kwargs: stub)

    server, cleanup, _ctx = server_mod.create_server()
    try:
        result = await _dispatch(
            server,
            "memory.session_bootstrap",
            {"source": "startup", "session_id": "sess-77"},
        )
        assert not result.isError, result
        payload = json.loads(result.content[0].text)
        assert payload["additionalContext"] == "stub-ctx"
        assert payload["episode"] == {"id": "sess-77", "action": "opened"}
        assert payload["counts"]["semantic"] == 0
        assert len(stub.bootstrap_calls) == 1
    finally:
        await cleanup()


async def test_agentcore_mode_hides_episode_and_synthesis_tools_at_list_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from mcp.types import ListToolsRequest, ListToolsResult

    _server_home(tmp_path, monkeypatch)
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")

    import better_memory.mcp.server as server_mod

    monkeypatch.setattr(
        server_mod, "build_backend", lambda **kwargs: _StubRemoteBackend()
    )
    server, cleanup, _ctx = server_mod.create_server()
    try:
        handler = server.request_handlers[ListToolsRequest]
        result = await handler(ListToolsRequest(method="tools/list"))
        assert isinstance(result.root, ListToolsResult)
        names = {t.name for t in result.root.tools}
        assert not (EPISODE_GATED_TOOLS & names)
        assert "memory.synthesize_next_get_context" not in names
        assert {"memory.observe", "memory.retrieve", "memory.record_use"} <= names
    finally:
        await cleanup()


async def test_sqlite_mode_dispatch_never_routes_to_backend_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The branch predicate is the config STRING, not backend truthiness:
    in sqlite mode a (stubbed) backend object must NOT receive observe —
    the sqlite service path writes the local row exactly as before."""
    home = _server_home(tmp_path, monkeypatch)
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)

    import better_memory.mcp.server as server_mod

    stub = _StubRemoteBackend()
    monkeypatch.setattr(server_mod, "build_backend", lambda **kwargs: stub)

    server, cleanup, ctx = server_mod.create_server()
    try:
        assert ctx.backend is stub
        result = await _dispatch(server, "memory.observe", {"content": "local-row"})
        assert not result.isError, result
        assert stub.observe_calls == []
        assert _observation_row_count(home) == 1
    finally:
        await cleanup()
