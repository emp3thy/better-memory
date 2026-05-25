"""MCP server selects the right StorageBackend and gates synthesis tools."""

from __future__ import annotations

import asyncio

import pytest


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
    monkeypatch.setenv("BETTER_MEMORY_EMBEDDINGS_BACKEND", "tfidf")
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
