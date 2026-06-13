"""MCP stdio server exposing better-memory's tools.

The server wires together the existing service classes and presents them
as MCP tools over stdio. On startup, the knowledge-base is reindexed
(mtime-only, so this is cheap and idempotent) as a session-start step.

Tool registration and dispatch are owned by
:class:`better_memory.mcp.dispatcher.ToolDispatcher`. Each tool's schema,
description, and async handler live in the per-domain modules under
``better_memory.mcp.handlers``. ``create_server`` is now a thin wiring
seam: build services, build the dispatcher, register two MCP callbacks
that delegate to it.

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
import logging
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

from better_memory import _diag
from better_memory.config import get_config, project_name
from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.embeddings.ollama import OllamaEmbedder

# Re-exported for tests that still import ``_run_best_effort`` from
# ``better_memory.mcp.server`` (test_best_effort_logging). The canonical
# definition lives in ``_best_effort`` so handler modules can import it
# without creating a server <-> handlers import cycle.
from better_memory.mcp._best_effort import _run_best_effort  # noqa: F401
from better_memory.mcp._session import resolve_session_id
from better_memory.mcp.container import ServiceContainer
from better_memory.mcp.dispatcher import ToolDispatcher
from better_memory.mcp.handlers import all_handlers

# Re-exported for tests that import the synth serializers from server.py
# (test_synthesize_tools). The canonical definitions live alongside the
# ReflectionHandlers; we re-export here only to keep the existing test
# import paths working.
from better_memory.mcp.handlers.reflections import (  # noqa: F401
    _serialize_queue,
    _serialize_synth_apply_ok,
    _serialize_synth_apply_state_error,
    _serialize_synth_apply_validation_error,
    _serialize_synth_get_context,
)
from better_memory.services.episode import EpisodeService
from better_memory.services.knowledge import KnowledgeService
from better_memory.services.memory_rating import MemoryRatingService
from better_memory.services.observation import ObservationService
from better_memory.services.reflection import ReflectionSynthesisService
from better_memory.services.retention import RetentionService
from better_memory.services.spool import SpoolService
from better_memory.storage import StorageBackend, build_backend

# Module-level migration directories. Packaged alongside the code so
# ``python -m better_memory.mcp`` finds them without needing extra config.
_MEMORY_MIGRATIONS = Path(__file__).parent.parent / "db" / "migrations"
_KNOWLEDGE_MIGRATIONS = Path(__file__).parent.parent / "db" / "knowledge_migrations"

_OLLAMA_PROBE_TIMEOUT_SEC = 2.0

logger = logging.getLogger(__name__)


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


def _tool_definitions(*, supports_synthesis: bool = True) -> list[Tool]:
    """Return the static list of tools exposed over MCP, gated by capability.

    Used by tests that want to assert tool registration without standing
    up a full :class:`ToolDispatcher`. In production, ``create_server``
    builds a dispatcher and calls its own ``tool_definitions()`` against
    the live backend's capability flag — this helper is the same list,
    derived from the same ``all_handlers()`` source.
    """
    return [
        Tool(name=h.name, description=h.description, inputSchema=h.schema)
        for h in all_handlers()
        if supports_synthesis or not h.requires_synthesis
    ]


@dataclass
class ServerContext:
    """Bundle of long-lived runtime objects exposed by ``create_server``.

    Returned alongside the ``Server`` and ``cleanup`` callable so callers
    (tests, future hooks) can introspect or reuse the wired-up backend
    without rebuilding it. ``backend`` is the live ``StorageBackend``;
    ``memory_conn`` is the underlying sqlite connection (None for non-sqlite
    backends in future plans); ``embedder`` is whatever was passed to the
    backend (None for the sqlite/FTS5 embeddings backend, which indexes via
    DB triggers and needs no Python embedder). ``dispatcher`` is the live
    :class:`ToolDispatcher` so tests can route tool calls without going
    through the SDK request stack.
    """

    backend: StorageBackend
    memory_conn: sqlite3.Connection | None = None
    embedder: Any = None
    dispatcher: ToolDispatcher | None = None


def _build_services(
    config: Any,  # better_memory.config.Config
    memory_conn: sqlite3.Connection,
    knowledge_conn: sqlite3.Connection,
    embedder: OllamaEmbedder | None,
    *,
    startup_project: str,
    startup_session_id: str | None,
) -> ServiceContainer:
    """Construct every service exactly once.

    Replaces the inline 4x ``SemanticMemoryService(memory_conn)`` and the
    inline 2x ``SessionBootstrapService(...)`` smells in the legacy
    ``_call_tool`` body.
    """
    from better_memory.services.semantic import SemanticMemoryService
    from better_memory.services.session_bootstrap import SessionBootstrapService

    episodes = EpisodeService(memory_conn)
    observations = ObservationService(
        memory_conn, embedder=embedder, episodes=episodes,
    )
    backend = build_backend(
        config=config,
        memory_conn=memory_conn,
        embedder=embedder,
        session_id=startup_session_id,
        project=startup_project,
    )
    reflections = ReflectionSynthesisService(memory_conn)
    retention = RetentionService(conn=memory_conn)
    memory_rating = MemoryRatingService(memory_conn)
    knowledge = KnowledgeService(
        knowledge_conn, knowledge_base=config.knowledge_base,
    )
    spool = SpoolService(memory_conn, config.spool_dir, episodes=episodes)
    semantic = SemanticMemoryService(memory_conn)
    session_bootstrap = SessionBootstrapService(memory_conn)

    return ServiceContainer(
        config=config,
        memory_conn=memory_conn,
        backend=backend,
        episodes=episodes,
        observations=observations,
        reflections=reflections,
        retention=retention,
        memory_rating=memory_rating,
        knowledge=knowledge,
        spool=spool,
        semantic=semantic,
        session_bootstrap=session_bootstrap,
    )


def _make_cleanup(
    memory_conn: sqlite3.Connection,
    knowledge_conn: sqlite3.Connection,
    embedder: OllamaEmbedder | None,
) -> Callable[[], Coroutine[Any, Any, None]]:
    """Build the idempotent shutdown closure.

    Closes both SQLite connections and the embedder's HTTP client.
    Safe to call multiple times: SQLite ``Connection.close`` is a no-op
    after the first call, and a local ``cleaned`` flag guards against
    double-closing the embedder's httpx client.
    """
    cleaned = False

    async def cleanup() -> None:
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

    return cleanup


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
    connection, the embedder (if any), and the :class:`ToolDispatcher`.
    Callers must await ``cleanup`` on shutdown (typically in a ``finally``
    around ``server.run``).

    Concurrency invariant: the memory-side services share ``memory_conn``.
    Each service's docstring documents that it owns the connection and the
    caller must not share it with another service that has an open
    transaction. That contract holds here only because the MCP stdio
    transport serialises requests — ``_call_tool`` runs one tool
    invocation at a time, so at most one service's SAVEPOINT is ever in
    flight. If any future change introduces async fan-out, a background
    task, or a worker thread that writes to ``memory_conn``, this
    assumption breaks and the services must be reworked to use per-task
    connections (or a connection pool with explicit checkout).
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
    services = _build_services(
        config, memory_conn, knowledge_conn, embedder,
        startup_project=project_name(),
        startup_session_id=resolve_session_id(config.home) or None,
    )

    # Session-start behaviour: reindex knowledge at startup. mtime-only, so
    # the cost is O(files) stat calls on an already-indexed corpus. We
    # swallow any exception so a missing / unreachable knowledge base does
    # not block the server from serving memory tools.
    if config.knowledge_base.is_dir():
        try:
            services.knowledge.reindex()
        except Exception:  # noqa: BLE001 — best-effort startup hook
            pass

    dispatcher = ToolDispatcher(services, all_handlers())

    server: Server = Server(name="better-memory")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return dispatcher.tool_definitions()

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any] | None,
    ) -> list[TextContent]:
        with _diag.trace(f"mcp.{name}"):
            return await dispatcher.call(name, arguments or {})

    cleanup = _make_cleanup(memory_conn, knowledge_conn, embedder)
    return server, cleanup, ServerContext(
        backend=services.backend,
        memory_conn=memory_conn,
        embedder=embedder,
        dispatcher=dispatcher,
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
