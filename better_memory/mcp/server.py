"""MCP stdio server exposing better-memory's tools.

The server wires together the existing service classes and presents them
as MCP tools over stdio. On startup, the knowledge-base is reindexed
(mtime-only, so this is cheap and idempotent) as a session-start step.

Module layout
-------------
This module owns wiring only: connection + service construction, the
handler registry, and the stdio run loop. The per-tool behaviour lives in
:mod:`better_memory.mcp.handlers` (one module per tool domain), the tool
name/schema declarations in :mod:`better_memory.mcp.tools`, the JSON
payload builders in :mod:`better_memory.mcp.serializers`, and the
synthesize audit log in :mod:`better_memory.mcp.synth_audit`.

Tools
-----
* ``memory.observe``                      — create an observation.
* ``memory.retrieve``                     — reflections bucketed by polarity.
* ``memory.retrieve_observations``        — raw observation drill-down.
* ``memory.record_use``                   — record re-use of a memory.
* ``memory.semantic_observe`` / _retrieve / _update / _delete — user-stated facts.
* ``memory.start_ui``                     — spawn or reuse the management UI.
* ``memory.start_episode`` / _close_episode / _list_episodes / _reconcile_episodes
                                          — episode lifecycle.
* ``memory.synthesize_next_get_context``  — episode-context fetch for synthesis.
* ``memory.synthesize_next_apply``        — apply IDE-LLM's decision JSON.
* ``memory.session_bootstrap``            — open/reuse a session episode + inject memories.
* ``memory.run_retention``                — apply spec §9 retention rules.
* ``knowledge.search`` / ``knowledge.list`` — knowledge-base introspection.

Connection ownership
--------------------
The server owns both SQLite connections (memory.db + knowledge.db) for the
duration of the process. They are not shared with any other component;
every service writes to them under its documented transaction contract.

Error surfaces
--------------
All tool handlers return JSON-encoded ``TextContent``. Exceptions raised
inside a handler are caught by the MCP framework and re-surfaced as a
``CallToolResult`` with ``isError=True`` and a plain-text error message.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from better_memory.config import get_config, project_name
from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.embeddings.ollama import OllamaEmbedder
from better_memory.embeddings.sync_embed import SyncEmbedder
from better_memory.mcp._util import resolve_session_id as _resolve_session_id
from better_memory.mcp.handlers import (
    EpisodeToolHandlers,
    KnowledgeToolHandlers,
    ObservationToolHandlers,
    ReflectionToolHandlers,
    SemanticToolHandlers,
    SessionToolHandlers,
    build_registry,
)
from better_memory.mcp.tools import tool_definitions as _tool_definitions
from better_memory.services.episode import EpisodeService
from better_memory.services.knowledge import KnowledgeService
from better_memory.services.memory_rating import MemoryRatingService
from better_memory.services.observation import ObservationService
from better_memory.services.reflection import ReflectionSynthesisService
from better_memory.services.retention import RetentionService
from better_memory.services.semantic import SemanticMemoryService
from better_memory.services.session_bootstrap import SessionBootstrapService
from better_memory.services.spool import SpoolService
from better_memory.storage import StorageBackend, build_backend

# Module-level migration directories. Packaged alongside the code so
# ``python -m better_memory.mcp`` finds them without needing extra config.
_MEMORY_MIGRATIONS = Path(__file__).parent.parent / "db" / "migrations"
_KNOWLEDGE_MIGRATIONS = Path(__file__).parent.parent / "db" / "knowledge_migrations"

_OLLAMA_PROBE_TIMEOUT_SEC = 2.0


def _probe_ollama(host: str) -> None:
    """Log a clear stderr warning if Ollama isn't reachable. Never raise.

    Called once at startup; purely informational. The server continues in
    either case — knowledge-only tools (``knowledge.search``, ``knowledge.list``)
    don't need Ollama, and embedding-dependent tools raise a clean
    ``EmbeddingError`` the first time they're invoked against a down host.
    """
    url = host.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(  # noqa: S310 — local-only URL
            url, timeout=_OLLAMA_PROBE_TIMEOUT_SEC
        ) as response:
            if response.status == 200:
                return
            print(
                f"[better-memory] WARNING: Ollama probe at {url} returned "
                f"HTTP {response.status}; memory.observe / memory.retrieve "
                "may fail until this is resolved.",
                file=sys.stderr,
                flush=True,
            )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(
            f"[better-memory] WARNING: Ollama unreachable at {host} "
            f"({type(exc).__name__}: {exc}). memory.observe and memory.retrieve "
            "will fail until Ollama is running; knowledge.* tools still work.",
            file=sys.stderr,
            flush=True,
        )


# --------------------------------------------------------------------------- factory


@dataclass
class ServerContext:
    """Bundle of long-lived runtime objects exposed by ``create_server``.

    Returned alongside the ``Server`` and ``cleanup`` callable so callers
    (tests, future hooks) can introspect or reuse the wired-up backend
    without rebuilding it. ``backend`` is the live ``StorageBackend``;
    ``memory_conn`` is the underlying sqlite connection (None for non-sqlite
    backends in future plans); ``embedder`` is whatever was passed to the
    backend (None for the sqlite/FTS5 embeddings backend, which indexes via
    DB triggers and needs no Python embedder).
    """

    backend: StorageBackend
    memory_conn: sqlite3.Connection | None = None
    embedder: Any = None


def create_server() -> tuple[
    Server,
    Callable[[], Coroutine[Any, Any, None]],
    ServerContext,
]:
    """Wire services and register tools.

    Returns a ``(server, cleanup, ctx)`` triple where ``cleanup`` is an
    idempotent async function that closes the two SQLite connections and
    the embedder's HTTP client, and ``ctx`` is a :class:`ServerContext`
    bundling the live :class:`StorageBackend`, its underlying memory
    connection, and the embedder (if any). Callers must await ``cleanup``
    on shutdown (typically in a ``finally`` around ``server.run``).
    """
    config = get_config()

    memory_conn = connect(config.memory_db)
    apply_migrations(memory_conn, migrations_dir=_MEMORY_MIGRATIONS)
    knowledge_conn = connect(config.knowledge_db)
    apply_migrations(knowledge_conn, migrations_dir=_KNOWLEDGE_MIGRATIONS)

    # Embedder is only built for the ollama backend. For sqlite, FTS5
    # triggers handle indexing automatically (see migration 0011) and no
    # embedder is needed.
    embedder: OllamaEmbedder | None = None
    if config.embeddings_backend == "ollama":
        # One embedder per server. Construction is cheap and does NOT contact
        # Ollama (see OllamaEmbedder.__init__); the first embed() call does.
        embedder = OllamaEmbedder()
        # Cheap reachability probe against Ollama. Warn (to stderr) if it's
        # down but do not block startup — knowledge.* tools still work
        # without Ollama, and if Ollama comes up later, memory.observe /
        # memory.retrieve will succeed on their next call without a restart.
        _probe_ollama(config.ollama_host)

    # Fresh short-timeout embedder per bridge call (loop-bound client);
    # shared instance so the breaker state is process-wide.
    sync_embedder: SyncEmbedder | None = None
    if embedder is not None:
        sync_embedder = SyncEmbedder(
            lambda: OllamaEmbedder(timeout=5.0, max_retries=1)
        )

    # Concurrency invariant: the memory-side services below
    # (EpisodeService, ObservationService, ReflectionSynthesisService,
    # RetentionService, SpoolService, SemanticMemoryService,
    # SessionBootstrapService, MemoryRatingService) all share
    # ``memory_conn``. Each service's docstring documents that it owns the
    # connection and the caller must not share it with another service that
    # has an open transaction. That contract holds here only because the MCP
    # stdio transport serialises requests — _call_tool runs one tool
    # invocation at a time, so at most one service's SAVEPOINT is ever in
    # flight. If any future change introduces async fan-out, a background
    # task, or a worker thread that writes to ``memory_conn``, this
    # assumption breaks and the services must be reworked to use per-task
    # connections (or a connection pool with explicit checkout).
    episodes = EpisodeService(memory_conn)
    observations = ObservationService(memory_conn, embedder=embedder, episodes=episodes)

    # Resolve project + session id ONCE at startup. Per-handler code can
    # still override per-call (handlers continue to read args.get("project")
    # / call project_name() for backwards compatibility), but the backend
    # construction needs concrete defaults to lock in the SqliteBackend's
    # write-path resolution. Pass ``None`` (not ``""``) when the session id
    # can't be resolved at startup — ObservationService's session_id
    # resolution (services/observation.py:147-155) only falls back to the
    # env-var / marker file path when ``session_id is None``. An empty
    # string silently writes observations with session_id='' and breaks
    # the rating tools.
    startup_project = project_name()
    startup_session_id: str | None = _resolve_session_id(config.home) or None
    backend: StorageBackend = build_backend(
        config=config,
        memory_conn=memory_conn,
        embedder=embedder,
        session_id=startup_session_id,
        project=startup_project,
    )

    # Reflection synthesis is driven by the IDE-LLM via two MCP tools
    # (memory.synthesize_next_get_context / _apply). The service no
    # longer holds a chat client.
    reflections = ReflectionSynthesisService(memory_conn, sync_embedder=sync_embedder)
    retention = RetentionService(conn=memory_conn)
    memory_rating = MemoryRatingService(memory_conn)
    semantic = SemanticMemoryService(memory_conn)
    session_bootstrap = SessionBootstrapService(memory_conn)

    knowledge = KnowledgeService(
        knowledge_conn,
        knowledge_base=config.knowledge_base,
    )
    spool = SpoolService(memory_conn, config.spool_dir, episodes=episodes)

    # Session-start behaviour: reindex knowledge at startup. mtime-only, so
    # the cost is O(files) stat calls on an already-indexed corpus. We
    # swallow any exception so a missing / unreachable knowledge base does
    # not block the server from serving memory tools.
    if config.knowledge_base.is_dir():
        try:
            knowledge.reindex()
        except Exception:  # noqa: BLE001 — best-effort startup hook
            pass

    # Remote dispatch (agentcore mode): the data-tool handlers receive the
    # backend as ``remote`` iff the CONFIG string says agentcore. The
    # predicate is deliberately the config value — never backend truthiness
    # or isinstance — so the sqlite default path keeps ``remote=None`` and
    # its service-stack behaviour is byte-identical (session-id / project
    # resolution semantics differ between the standalone services and
    # SqliteBackend; routing sqlite through the backend would change them).
    remote: StorageBackend | None = (
        backend if config.storage_backend == "agentcore" else None
    )

    # All tool behaviour lives in the per-domain handler classes; this
    # registry is the single dispatch surface for _call_tool. The
    # synthesize handlers stay registered even when the backend does not
    # support synthesis — only the *advertised* tool list is gated (see
    # _list_tools), matching the pre-extraction dispatcher. The same holds
    # for the episode/retention handlers in agentcore mode.
    registry = build_registry(
        ObservationToolHandlers(
            observations=observations, retention=retention, remote=remote
        ),
        ReflectionToolHandlers(
            backend=backend,
            reflections=reflections,
            spool=spool,
            memory_conn=memory_conn,
            home=config.home,
            remote=remote,
        ),
        EpisodeToolHandlers(
            episodes=episodes,
            observations=observations,
            reflections=reflections,
            backend=backend,
            remote=remote,
        ),
        SemanticToolHandlers(semantic=semantic, remote=remote),
        KnowledgeToolHandlers(knowledge=knowledge),
        SessionToolHandlers(
            session_bootstrap=session_bootstrap,
            memory_rating=memory_rating,
            home=config.home,
            remote=remote,
        ),
    )

    server: Server = Server(name="better-memory")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return _tool_definitions(
            supports_synthesis=backend.supports_synthesis,
            supports_episodes=backend.supports_episodes,
        )

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[TextContent]:
        handler = registry.get(name)
        if handler is None:
            raise ValueError(f"Unknown tool: {name}")
        return await handler(arguments or {})

    cleaned = False

    async def cleanup() -> None:
        """Close SQLite connections and the embedder HTTP client.

        Idempotent: safe to call multiple times. SQLite ``Connection.close``
        is a no-op after the first call, and we guard the embedder close with
        a local flag so we don't double-close its httpx client either.
        """
        nonlocal cleaned
        if cleaned:
            return
        cleaned = True
        try:
            memory_conn.close()
        except Exception:  # noqa: BLE001 — best-effort shutdown
            pass
        try:
            knowledge_conn.close()
        except Exception:  # noqa: BLE001 — best-effort shutdown
            pass
        # In the sqlite backend no embedder is built (FTS5 triggers handle
        # indexing). Guard against the None case so this cleanup stays
        # idempotent across both backends.
        if embedder is not None:
            try:
                await embedder.aclose()
            except Exception:  # noqa: BLE001 — best-effort shutdown
                pass

    return server, cleanup, ServerContext(
        backend=backend,
        memory_conn=memory_conn,
        embedder=embedder,
    )


async def _dispatch_for_tests(name: str, arguments: dict) -> list[TextContent]:
    """Test-only entry point that runs one tool invocation against a fresh
    server instance. NOT used by production code.

    The MCP SDK catches exceptions inside handlers and surfaces them as
    CallToolResult(isError=True). To make tests ergonomic, this helper
    re-raises any error as ValueError so callers can use
    `pytest.raises(ValueError, match="...")` instead of inspecting
    result text manually.
    """
    from mcp.types import CallToolRequest, CallToolRequestParams

    server, cleanup, _ = create_server()
    try:
        handler = server.request_handlers[CallToolRequest]
        req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(name=name, arguments=arguments),
        )
        result = await handler(req)
        # The SDK's ServerResult is a discriminated union; we know this
        # handler is wired to CallTool and returns CallToolResult.
        from mcp.types import CallToolResult
        assert isinstance(result.root, CallToolResult), (
            f"Expected CallToolResult, got {type(result.root).__name__}"
        )
        if getattr(result.root, "isError", False):
            # Re-raise as ValueError; preserve the framework's error text.
            text = ""
            if result.root.content:
                first = result.root.content[0]
                text = getattr(first, "text", "") or ""
            raise ValueError(text)
        # Tests inspect .text on TextContent entries — runtime is correct;
        # cast through Any to satisfy Pyright's list invariance (the SDK
        # types .content as list[ContentBlock]; our tools only emit
        # TextContent so the cast is sound).
        return cast(list[TextContent], result.root.content)
    finally:
        await cleanup()


async def run() -> None:
    """Start the server on stdio and run until the client disconnects."""
    server, cleanup, _ = create_server()
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await cleanup()


if __name__ == "__main__":  # pragma: no cover — module entry-point shim
    asyncio.run(run())
