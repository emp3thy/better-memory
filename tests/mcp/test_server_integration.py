"""Integration tests for :mod:`better_memory.mcp.server`.

These drive the real MCP stdio server as a subprocess via the ``mcp``
client SDK. They use per-test tmpfs for all paths (memory DB, knowledge
DB, spool, knowledge base). Task 2 (remove-ollama-embeddings) deleted
create_server's embedder construction and Ollama probe entirely, so this
module no longer talks to Ollama at all and no longer needs a live
Ollama daemon to run — it works identically regardless of
``BETTER_MEMORY_EMBEDDINGS_BACKEND``.

Beyond the happy-path round-trips, this module covers the tool-handler
error paths that are only reachable through the real dispatch layer:
missing required arguments, invalid enum values, DB CHECK-constraint
violations, malformed synthesis decision dicts, and stale / already
synthesized episode ids.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from better_memory.mcp.server import _tool_definitions

# Generous timeout to absorb slow CI subprocess startup and migrations.
_CLIENT_TIMEOUT = timedelta(seconds=60)

# A structurally valid synthesize_next_apply decision that takes no action.
_EMPTY_DECISION: dict[str, list[Any]] = {
    "new": [],
    "augment": [],
    "merge": [],
    "ignore": [],
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def server_params(tmp_path: Path) -> StdioServerParameters:
    """Per-test stdio server parameters with isolated paths.

    Each test gets its own memory DB, knowledge DB, spool directory and
    knowledge-base root. The server is spawned by the SDK's ``stdio_client``
    using these params; this prevents cross-test contamination of the
    shared ``~/.better-memory`` location. ``CLAUDE_SESSION_ID`` is pinned
    so episode/session binding is deterministic regardless of the
    invoking environment.
    """
    env = {
        **os.environ,
        "BETTER_MEMORY_HOME": str(tmp_path),
        "CLAUDE_SESSION_ID": "itest-session",
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
# Happy-path tests
# ---------------------------------------------------------------------------


async def test_initialize_then_tools_list(
    server_params: StdioServerParameters,
) -> None:
    """The server boots and advertises exactly the factory's tool list.

    The default sqlite backend supports synthesis, so the wire-visible
    set must match ``_tool_definitions(supports_synthesis=True)``.
    """
    expected = {
        t.name for t in _tool_definitions(supports_synthesis=True)
    }
    # Spot-check the core surface so a bug in _tool_definitions itself
    # can't make this test vacuously true.
    assert {
        "memory.observe",
        "memory.retrieve",
        "memory.record_use",
        "knowledge.search",
        "knowledge.list",
        "memory.start_ui",
        "memory.synthesize_next_apply",
    } <= expected

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=_CLIENT_TIMEOUT
        ) as session:
            await session.initialize()

            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            assert names == expected


async def test_memory_observe_then_retrieve_observations_roundtrip(
    server_params: StdioServerParameters,
) -> None:
    """Observe success + failure, then drill down via retrieve_observations."""
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
            fail_id = _single_json_dict(fail_resp)["id"]

            success_resp = await session.call_tool(
                "memory.observe",
                {
                    "content": "success-abc probemarker",
                    "outcome": "success",
                },
            )
            success_id = _single_json_dict(success_resp)["id"]

            drill_resp = await session.call_tool(
                "memory.retrieve_observations",
                {"query": "probemarker"},
            )
            rows = _single_json_list(drill_resp)
            by_id = {row["id"]: row for row in rows}
            assert fail_id in by_id, f"failure id {fail_id} missing: {rows}"
            assert success_id in by_id, (
                f"success id {success_id} missing: {rows}"
            )
            assert by_id[fail_id]["outcome"] == "failure"
            assert by_id[success_id]["outcome"] == "success"


async def test_memory_retrieve_returns_reflection_buckets(
    server_params: StdioServerParameters,
) -> None:
    """``memory.retrieve`` returns the do/dont/neutral reflection buckets."""
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=_CLIENT_TIMEOUT
        ) as session:
            await session.initialize()

            retrieve_resp = await session.call_tool("memory.retrieve", {})
            payload = _single_json_dict(retrieve_resp)
            assert isinstance(payload["do"], list)
            assert isinstance(payload["dont"], list)
            assert isinstance(payload["neutral"], list)


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
            obs_id = _single_json_dict(observe_resp)["id"]

            record_resp = await session.call_tool(
                "memory.record_use",
                {"id": obs_id, "outcome": "success"},
            )
            assert _single_json_dict(record_resp) == {"ok": True}


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
            docs = _single_json_list(list_resp)
            paths = {d["path"] for d in docs}
            assert "standards/testing.md" in paths

            search_resp = await session.call_tool(
                "knowledge.search", {"query": "probemarker"}
            )
            hits = _single_json_list(search_resp)
            assert any(h["path"] == "standards/testing.md" for h in hits)


async def test_spool_drain_on_retrieve(
    server_params: StdioServerParameters,
    tmp_path: Path,
) -> None:
    """A file dropped into the spool is consumed by ``memory.retrieve``."""
    assert server_params.env is not None, "server_params fixture must populate env"
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
            await session.call_tool("memory.retrieve", {})

    # After retrieve, the spool file must no longer sit at the top level of
    # the spool directory — either drained into hook_events or quarantined.
    assert not spool_file.exists(), (
        f"Spool file was not consumed: {spool_file}"
    )


# ---------------------------------------------------------------------------
# Handler error paths
# ---------------------------------------------------------------------------


async def test_observe_missing_required_content_is_error(
    server_params: StdioServerParameters,
) -> None:
    """``memory.observe`` without ``content`` surfaces an MCP tool error."""
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=_CLIENT_TIMEOUT
        ) as session:
            await session.initialize()

            resp = await session.call_tool("memory.observe", {})
            text = _error_text(resp)
            assert "content" in text


async def test_observe_invalid_scope_is_error(
    server_params: StdioServerParameters,
) -> None:
    """``memory.observe`` with an invalid ``scope`` enum value errors.

    The MCP SDK validates arguments against the tool's inputSchema before
    dispatch, so this surfaces as an "Input validation error" rather than
    the handler's own ValueError — either way the caller sees isError.
    """
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=_CLIENT_TIMEOUT
        ) as session:
            await session.initialize()

            resp = await session.call_tool(
                "memory.observe",
                {"content": "bad scope probe", "scope": "bogus"},
            )
            text = _error_text(resp)
            assert "'bogus' is not one of" in text


async def test_record_use_unknown_observation_id_is_error(
    server_params: StdioServerParameters,
) -> None:
    """``memory.record_use`` with a nonexistent id errors with 'not found'."""
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=_CLIENT_TIMEOUT
        ) as session:
            await session.initialize()

            resp = await session.call_tool(
                "memory.record_use",
                {"id": "does-not-exist", "outcome": "success"},
            )
            text = _error_text(resp)
            assert "not found" in text.lower()


async def test_record_use_invalid_outcome_is_error(
    server_params: StdioServerParameters,
) -> None:
    """``memory.record_use`` rejects an outcome outside its enum."""
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=_CLIENT_TIMEOUT
        ) as session:
            await session.initialize()

            observe_resp = await session.call_tool(
                "memory.observe",
                {"content": "invalid outcome probe", "outcome": "neutral"},
            )
            obs_id = _single_json_dict(observe_resp)["id"]

            resp = await session.call_tool(
                "memory.record_use",
                {"id": obs_id, "outcome": "bogus"},
            )
            text = _error_text(resp)
            assert "'bogus' is not one of" in text


async def test_close_episode_constraint_violation_is_error(
    server_params: StdioServerParameters,
) -> None:
    """An outcome that violates the episodes CHECK constraint errors.

    ``close_reason`` is passed explicitly so the handler's
    ``default_reasons[outcome]`` lookup doesn't mask the DB-level
    constraint path.
    """
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=_CLIENT_TIMEOUT
        ) as session:
            await session.initialize()

            start_resp = await session.call_tool(
                "memory.start_episode", {"goal": "constraint probe"}
            )
            assert _single_json_dict(start_resp)["episode_id"]

            resp = await session.call_tool(
                "memory.close_episode",
                {"outcome": "bogus", "close_reason": "goal_complete"},
            )
            _error_text(resp)


async def test_synthesize_apply_malformed_decision_is_validation_error(
    server_params: StdioServerParameters,
) -> None:
    """A decision dict missing required top-level keys → structured error.

    Validation failures must come back as ``{"ok": false, "error":
    "validation"}`` (not the MCP isError surface) so the calling LLM can
    retry with a corrected payload.
    """
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=_CLIENT_TIMEOUT
        ) as session:
            await session.initialize()

            resp = await session.call_tool(
                "memory.synthesize_next_apply",
                {
                    "episode_id": "irrelevant",
                    "decision": {"garbage": True},
                },
            )
            payload = _single_json_dict(resp)
            assert payload["ok"] is False
            assert payload["error"] == "validation"
            assert "missing required top-level key" in payload["message"]


async def test_synthesize_apply_stale_episode_id_is_state_error(
    server_params: StdioServerParameters,
) -> None:
    """A well-formed decision against a nonexistent episode → state error."""
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=_CLIENT_TIMEOUT
        ) as session:
            await session.initialize()

            resp = await session.call_tool(
                "memory.synthesize_next_apply",
                {
                    "episode_id": "no-such-episode",
                    "decision": _EMPTY_DECISION,
                },
            )
            payload = _single_json_dict(resp)
            assert payload["ok"] is False
            assert payload["error"] == "state"
            assert "not found" in payload["message"]


async def test_synthesize_apply_already_synthesized_is_state_error(
    server_params: StdioServerParameters,
) -> None:
    """Applying twice against the same episode → state error on the retry."""
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=_CLIENT_TIMEOUT
        ) as session:
            await session.initialize()

            start_resp = await session.call_tool(
                "memory.start_episode", {"goal": "double-apply probe"}
            )
            episode_id = _single_json_dict(start_resp)["episode_id"]

            close_resp = await session.call_tool(
                "memory.close_episode", {"outcome": "success"}
            )
            closed = _single_json_dict(close_resp)
            assert closed["closed_episode_id"] == episode_id

            first = await session.call_tool(
                "memory.synthesize_next_apply",
                {"episode_id": episode_id, "decision": _EMPTY_DECISION},
            )
            first_payload = _single_json_dict(first)
            assert first_payload["ok"] is True, first_payload

            second = await session.call_tool(
                "memory.synthesize_next_apply",
                {"episode_id": episode_id, "decision": _EMPTY_DECISION},
            )
            second_payload = _single_json_dict(second)
            assert second_payload["ok"] is False
            assert second_payload["error"] == "state"
            assert "already synthesized" in second_payload["message"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_single_json(result: object) -> object:
    """Extract the single TextContent and parse its JSON payload.

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
    return json.loads(block.text)


def _single_json_dict(result: object) -> dict[str, Any]:
    """Parse the single TextContent payload, asserting it's a JSON object."""
    parsed = _parse_single_json(result)
    assert isinstance(parsed, dict), (
        f"expected JSON object, got {type(parsed).__name__}"
    )
    return parsed


def _single_json_list(result: object) -> list[Any]:
    """Parse the single TextContent payload, asserting it's a JSON array."""
    parsed = _parse_single_json(result)
    assert isinstance(parsed, list), (
        f"expected JSON array, got {type(parsed).__name__}"
    )
    return parsed


def _error_text(result: object) -> str:
    """Assert the call failed at the MCP layer and return the error text."""
    content = result.content  # type: ignore[attr-defined]
    is_error = getattr(result, "isError", False)
    assert is_error, f"expected isError result, got success: {content!r}"
    assert len(content) >= 1, "error result carried no content blocks"
    block = content[0]
    assert getattr(block, "type", None) == "text", f"not a text block: {block!r}"
    return block.text
