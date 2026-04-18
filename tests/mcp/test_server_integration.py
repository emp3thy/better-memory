"""Integration tests for :mod:`better_memory.mcp.server`.

These drive the real MCP stdio server as a subprocess via the ``mcp``
client SDK. They use per-test tmpfs for all paths (memory DB, knowledge
DB, spool, knowledge base) and talk to the local Ollama instance on
``http://localhost:11434`` — Phase 8 is explicit about running against
the real embedder on this dev machine.

Why not mark them ``integration``?
----------------------------------
The task brief states these should run in the default test suite so the
``memory.observe`` → ``memory.retrieve`` round-trip is part of the
default verify. Ollama is expected to be reachable on this machine.
If you run this suite on a machine without Ollama, use the
``OLLAMA_HOST`` env var to point at one before invoking pytest.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# Allow plenty of room for Ollama's first-call warm-up on a cold model.
_CLIENT_TIMEOUT = timedelta(seconds=60)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def server_params(tmp_path: Path) -> StdioServerParameters:
    """Per-test stdio server parameters with isolated paths.

    Each test gets its own memory DB, knowledge DB, spool directory and
    knowledge-base root. The server is spawned by the SDK's ``stdio_client``
    using these params; this prevents cross-test contamination of the
    shared ``~/.better-memory`` location.
    """
    env = {
        **os.environ,
        "BETTER_MEMORY_HOME": str(tmp_path),
    }
    # Ensure knowledge-base exists so the startup reindex has something to
    # walk; otherwise the reindex path would silently no-op.
    (tmp_path / "knowledge-base").mkdir(parents=True, exist_ok=True)

    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "better_memory.mcp"],
        env=env,
    )


@pytest.fixture
def seed_knowledge(tmp_path: Path) -> Path:
    """Seed a single standards markdown doc before the server starts."""
    kb = tmp_path / "knowledge-base"
    (kb / "standards").mkdir(parents=True, exist_ok=True)
    (kb / "standards" / "testing.md").write_text(
        "# Testing Standard\n\nAlways write probemarker assertions.\n",
        encoding="utf-8",
    )
    return kb


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_initialize_then_tools_list(
    server_params: StdioServerParameters,
) -> None:
    """The server boots, exposes all 6 tools, and advertises capabilities."""
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=_CLIENT_TIMEOUT
        ) as session:
            await session.initialize()

            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            assert names == {
                "memory.observe",
                "memory.retrieve",
                "memory.record_use",
                "knowledge.search",
                "knowledge.list",
                "memory.start_ui",
            }


async def test_memory_observe_and_retrieve_roundtrip(
    server_params: StdioServerParameters,
) -> None:
    """Observe success + failure, then retrieve — assert bucket routing."""
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=_CLIENT_TIMEOUT
        ) as session:
            await session.initialize()

            fail_resp = await session.call_tool(
                "memory.observe",
                {
                    "content": "failure-xyz probemarker",
                    "outcome": "failure",
                },
            )
            fail_id = _single_json(fail_resp)["id"]

            success_resp = await session.call_tool(
                "memory.observe",
                {
                    "content": "success-abc probemarker",
                    "outcome": "success",
                },
            )
            success_id = _single_json(success_resp)["id"]

            retrieve_resp = await session.call_tool(
                "memory.retrieve",
                {"query": "probemarker"},
            )
            payload = _single_json(retrieve_resp)

            # All three buckets + insights + knowledge present as lists.
            assert isinstance(payload["do"], list)
            assert isinstance(payload["dont"], list)
            assert isinstance(payload["neutral"], list)
            assert isinstance(payload["insights"], list)
            assert isinstance(payload["knowledge"], list)

            dont_ids = {row["id"] for row in payload["dont"]}
            do_ids = {row["id"] for row in payload["do"]}
            assert fail_id in dont_ids, (
                f"failure id {fail_id} missing from 'dont': {payload['dont']}"
            )
            assert success_id in do_ids, (
                f"success id {success_id} missing from 'do': {payload['do']}"
            )


async def test_memory_record_use_returns_ok(
    server_params: StdioServerParameters,
) -> None:
    """``memory.record_use`` with an existing id returns ``{"ok": true}``."""
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=_CLIENT_TIMEOUT
        ) as session:
            await session.initialize()

            observe_resp = await session.call_tool(
                "memory.observe",
                {"content": "record-use probe", "outcome": "neutral"},
            )
            obs_id = _single_json(observe_resp)["id"]

            record_resp = await session.call_tool(
                "memory.record_use",
                {"id": obs_id, "outcome": "success"},
            )
            assert _single_json(record_resp) == {"ok": True}


async def test_knowledge_search_and_list_return_arrays(
    server_params: StdioServerParameters,
    seed_knowledge: Path,
) -> None:
    """Startup reindexes the seeded markdown; search + list surface it."""
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=_CLIENT_TIMEOUT
        ) as session:
            await session.initialize()

            list_resp = await session.call_tool("knowledge.list", {})
            docs = _single_json(list_resp)
            assert isinstance(docs, list)
            paths = {d["path"] for d in docs}
            assert "standards/testing.md" in paths

            search_resp = await session.call_tool(
                "knowledge.search", {"query": "probemarker"}
            )
            hits = _single_json(search_resp)
            assert isinstance(hits, list)
            assert any(h["path"] == "standards/testing.md" for h in hits)


async def test_memory_start_ui_returns_stub_error(
    server_params: StdioServerParameters,
) -> None:
    """``memory.start_ui`` is a stub until Plan 2 lands."""
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=_CLIENT_TIMEOUT
        ) as session:
            await session.initialize()

            resp = await session.call_tool("memory.start_ui", {})
            payload = _single_json(resp)
            assert "error" in payload
            assert "UI not yet implemented" in payload["error"]


async def test_spool_drain_on_retrieve(
    server_params: StdioServerParameters,
    tmp_path: Path,
) -> None:
    """A file dropped into the spool is consumed by ``memory.retrieve``."""
    spool_dir = Path(server_params.env["BETTER_MEMORY_HOME"]) / "spool"
    spool_dir.mkdir(parents=True, exist_ok=True)

    spool_file = spool_dir / "20260418T120000-abc.json"
    spool_file.write_text(
        json.dumps(
            {
                "event_type": "tool_use",
                "tool": "Edit",
                "file": "foo.py",
                "content_snippet": "drained-by-retrieve",
                "cwd": str(tmp_path),
                "session_id": "sess-spool",
                "timestamp": "2026-04-18T12:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=_CLIENT_TIMEOUT
        ) as session:
            await session.initialize()
            await session.call_tool("memory.retrieve", {"query": "unused"})

    # After retrieve, the spool file must no longer sit at the top level of
    # the spool directory — either drained into hook_events or quarantined.
    assert not spool_file.exists(), (
        f"Spool file was not consumed: {spool_file}"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _single_json(result: object) -> dict | list:
    """Extract the single TextContent from a call_tool response and parse it.

    The MCP SDK returns a ``CallToolResult`` with ``.content`` as a list of
    content blocks. Our tools always emit exactly one ``TextContent`` whose
    ``text`` is JSON-encoded. When ``isError`` is set the SDK surfaces the
    error message as plain text — we raise instead of trying to parse.
    """
    content = result.content  # type: ignore[attr-defined]
    is_error = getattr(result, "isError", False)
    assert len(content) == 1, f"expected one content block, got {len(content)}"
    block = content[0]
    assert getattr(block, "type", None) == "text", f"not a text block: {block!r}"
    if is_error:
        raise AssertionError(f"tool returned error: {block.text}")
    return json.loads(block.text)  # type: ignore[no-any-return]
