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


async def test_create_server_wires_no_sync_embedder_into_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Task 2 (remove-ollama-embeddings): create_server no longer builds any
    SyncEmbedder at all — mcp/server.py no longer even imports SyncEmbedder,
    let alone constructs one — regardless of
    BETTER_MEMORY_EMBEDDINGS_BACKEND. The backend's internal _synthesis
    service receives ``sync_embedder=None``, same as every other caller
    (formerly PR #83 pinned that _synthesis and _semantic shared ONE
    process-wide instance; that instance no longer exists, and Task 4
    removed the parameter from SemanticMemoryService entirely)."""
    home = tmp_path / "bm"
    home.mkdir()
    (home / "knowledge-base").mkdir()
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(home))
    monkeypatch.delenv("BETTER_MEMORY_STORAGE_BACKEND", raising=False)

    import better_memory.mcp.server as server_mod
    from better_memory.storage import SqliteBackend

    server, cleanup, ctx = server_mod.create_server()
    try:
        assert isinstance(ctx.backend, SqliteBackend)
        assert ctx.backend._synthesis._sync_embedder is None
    finally:
        await cleanup()


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


# --- sqlite payload-parity contracts (pinned shapes) -----------------------
# The AgentCoreBackend must hand the dispatch layer the SAME payload shapes
# sqlite mode produces; the dispatch layer must transmit them to the MCP
# wire unmodified. Sources of truth:
#   - reflection bucket item: ReflectionSynthesisService.retrieve_reflections
#     (services/reflection.py bucket.append(...)) — internal ranking helpers
#     (_overlooked_count / _updated_at_ts) are backend-private and must never
#     reach the wire.
#   - observation row: ObservationService._list_observations_via_filter
#     SELECT list (services/observation.py).
#   - apply_session_ratings: MemoryRatingService.apply_session_ratings
#     documented return (services/memory_rating.py) — NOT the legacy
#     agentcore {"applied": int, "failed": int} pair.

SQLITE_REFLECTION_ITEM_KEYS = frozenset({
    "id", "title", "phase", "use_cases", "hints", "confidence", "tech",
    "evidence_count", "useful_count", "times_misled", "updated_at",
})

_PARITY_REFLECTION_ITEM: dict[str, Any] = {
    "id": "mem-" + "0" * 36,
    "title": "Use uv run for pytest",
    "phase": "general",
    "use_cases": "running the unit gate",
    "hints": ["always uv run pytest"],
    "confidence": 0.8,
    "tech": "python",
    "evidence_count": 3,
    "useful_count": 2,
    "times_misled": 0,
    "updated_at": "2026-07-12T10:00:00+00:00",
}

SQLITE_OBSERVATION_ROW_KEYS = frozenset({
    "id", "content", "component", "theme", "outcome",
    "reinforcement_score", "created_at",
})

_PARITY_OBSERVATION_ROW: dict[str, Any] = {
    "id": "evt-parity-1",
    "content": "observation body",
    "component": None,
    "theme": "bug",
    "outcome": "failure",
    "reinforcement_score": None,
    "created_at": "2026-07-12T09:00:00+00:00",
}

SQLITE_RATINGS_RESULT: dict[str, Any] = {
    "session_id": "sid-parity-1",
    "applied": {
        "cited": 1, "shaped": 0, "ignored": 0, "misled": 0, "overlooked": 0,
    },
    "skipped": {
        "not_exposed": 0, "already_rated": 0,
        "memory_missing": 0, "memory_retired": 0,
    },
}


class _StubRemoteBackend:
    """Records data-tool calls; agentcore-shaped capability flags.

    The payload-returning methods hand back the SQLITE-PARITY shapes above
    (the contract AgentCoreBackend implements) so the parity tests can pin
    that the dispatch layer transmits them to the MCP wire unmodified.
    """

    supports_synthesis = False
    supports_episodes = False

    def __init__(self) -> None:
        self.observe_calls: list[dict[str, Any]] = []
        self.bootstrap_calls: list[dict[str, Any]] = []
        self.list_observation_calls: list[dict[str, Any]] = []
        self.ratings_calls: list[dict[str, Any]] = []
        # Per-test overridable returns (defaults keep the older tests
        # in this module byte-identical in behavior).
        self.retrieve_buckets: dict[str, list[dict[str, Any]]] = {
            "do": [], "dont": [], "neutral": [],
        }
        self.observation_rows: list[dict[str, Any]] = []
        self.ratings_result: dict[str, Any] = json.loads(
            json.dumps(SQLITE_RATINGS_RESULT)
        )

    async def observe(self, **kwargs: Any) -> str:
        self.observe_calls.append(kwargs)
        return "stub-evt-0001"

    def retrieve(self, **kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        return self.retrieve_buckets

    async def list_observations(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.list_observation_calls.append(kwargs)
        return self.observation_rows

    def apply_session_ratings(self, **kwargs: Any) -> dict[str, Any]:
        self.ratings_calls.append(kwargs)
        return self.ratings_result

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


# ---------------------------------------------------------------------------
# Payload parity with sqlite mode (agentcore dispatch → MCP wire pins)
# ---------------------------------------------------------------------------


def _agentcore_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, Any, _StubRemoteBackend]:
    _server_home(tmp_path, monkeypatch)
    monkeypatch.setenv("BETTER_MEMORY_STORAGE_BACKEND", "agentcore")

    import better_memory.mcp.server as server_mod

    stub = _StubRemoteBackend()
    monkeypatch.setattr(server_mod, "build_backend", lambda **kwargs: stub)
    server, cleanup, _ctx = server_mod.create_server()
    return server, cleanup, stub


async def test_agentcore_retrieve_payload_matches_sqlite_item_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """memory.retrieve items reach the wire with EXACTLY the sqlite bucket
    item key set — in particular no backend-internal ranking helpers
    (_overlooked_count / _updated_at_ts) — and the dispatch layer transmits
    the backend's items unmodified (no reshaping, no added keys)."""
    server, cleanup, stub = _agentcore_server(tmp_path, monkeypatch)
    stub.retrieve_buckets = {
        "do": [dict(_PARITY_REFLECTION_ITEM)],
        "dont": [],
        "neutral": [],
    }
    try:
        result = await _dispatch(server, "memory.retrieve", {})
        assert not result.isError, result
        buckets = json.loads(result.content[0].text)
        assert set(buckets) == {"do", "dont", "neutral"}
        assert buckets["dont"] == [] and buckets["neutral"] == []
        (item,) = buckets["do"]
        assert set(item) == SQLITE_REFLECTION_ITEM_KEYS
        assert not any(
            key.startswith("_")
            for bucket in buckets.values()
            for row in bucket
            for key in row
        )
        assert item == _PARITY_REFLECTION_ITEM  # unmangled passthrough
    finally:
        await cleanup()


async def test_agentcore_retrieve_observations_payload_matches_sqlite_row_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """memory.retrieve_observations rows carry the sqlite SELECT-list key
    set (id, content, component, theme, outcome, reinforcement_score,
    created_at — None where agentcore has no value) and pass through the
    dispatch layer unmodified."""
    server, cleanup, stub = _agentcore_server(tmp_path, monkeypatch)
    stub.observation_rows = [dict(_PARITY_OBSERVATION_ROW)]
    try:
        result = await _dispatch(
            server, "memory.retrieve_observations", {"project": "projx"}
        )
        assert not result.isError, result
        rows = json.loads(result.content[0].text)
        assert len(rows) == 1
        assert set(rows[0]) == SQLITE_OBSERVATION_ROW_KEYS
        assert rows[0] == _PARITY_OBSERVATION_ROW
        assert len(stub.list_observation_calls) == 1
        assert stub.list_observation_calls[0]["project"] == "projx"
    finally:
        await cleanup()


async def test_agentcore_apply_session_ratings_payload_matches_sqlite_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """memory.apply_session_ratings in agentcore mode returns the sqlite
    ApplySessionRatingsResult shape — {session_id, applied: {per-class},
    skipped: {per-reason}} — NOT the legacy {"applied": int, "failed": int}
    pair, and the dispatch layer transmits it verbatim."""
    server, cleanup, stub = _agentcore_server(tmp_path, monkeypatch)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-parity-1")
    ratings = [
        {"kind": "semantic", "id": "mem-" + "1" * 36, "class": "cited"},
    ]
    try:
        result = await _dispatch(
            server, "memory.apply_session_ratings", {"ratings": ratings}
        )
        assert not result.isError, result
        payload = json.loads(result.content[0].text)
        assert payload == SQLITE_RATINGS_RESULT
        assert set(payload) == {"session_id", "applied", "skipped"}
        assert isinstance(payload["applied"], dict)  # per-class counts
        assert set(payload["applied"]) == {
            "cited", "shaped", "ignored", "misled", "overlooked",
        }
        assert set(payload["skipped"]) == {
            "not_exposed", "already_rated", "memory_missing", "memory_retired",
        }
        assert "failed" not in payload  # legacy agentcore-only key is gone
        assert stub.ratings_calls == [
            {"session_id": "sid-parity-1", "ratings": ratings}
        ]
    finally:
        await cleanup()
