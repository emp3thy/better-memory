"""MCP stdio server exposing better-memory's six tools.

The server wires together the existing service classes and presents them
as MCP tools over stdio. On startup, the knowledge-base is reindexed
(mtime-only, so this is cheap and idempotent) as a session-start step.

Tools
-----
* ``memory.observe``       — create a new observation; returns ``{"id": ...}``.
* ``memory.retrieve``      — three outcome buckets + knowledge. Drains
                             the spool before searching. (Reflection
                             retrieval is planned for Phase 6.)
* ``memory.record_use``    — record re-use (optionally with outcome).
* ``knowledge.search``     — BM25 search against the knowledge-base FTS.
* ``knowledge.list``       — list indexed knowledge documents.
* ``memory.start_ui``      — Plan 2 stub; returns an explanatory error.

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
The ``memory.start_ui`` stub is *not* an error — it returns a normal
``{"error": "UI not yet implemented ..."}`` JSON payload so clients can
display the message without treating it as a tool crash.
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from better_memory.config import get_config
from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.embeddings.ollama import OllamaEmbedder
from better_memory.search.hybrid import SearchResult
from better_memory.services.episode import EpisodeService
from better_memory.services.knowledge import (
    KnowledgeDocument,
    KnowledgeSearchResult,
    KnowledgeService,
)
from better_memory.services.observation import ObservationService
from better_memory.services.spool import SpoolService

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


# --------------------------------------------------------------------------- tools


def _tool_definitions() -> list[Tool]:
    """Return the static list of tools exposed over MCP."""
    return [
        Tool(
            name="memory.observe",
            description=(
                "Record an observation about the current session (a fact, "
                "decision, bug fix, or outcome). Returns the new observation id."
            ),
            inputSchema={
                "type": "object",
                "required": ["content"],
                "additionalProperties": False,
                "properties": {
                    "content": {"type": "string"},
                    "component": {"type": "string"},
                    "theme": {"type": "string"},
                    "trigger_type": {"type": "string"},
                    "outcome": {
                        "type": "string",
                        "enum": ["success", "failure", "neutral"],
                    },
                    "tech": {"type": "string"},
                },
            },
        ),
        Tool(
            name="memory.retrieve",
            description=(
                "Retrieve observations and knowledge relevant to the current "
                "task, bucketed by outcome (do / dont / neutral). Reflection "
                "retrieval is planned for Phase 6."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string"},
                    "component": {"type": "string"},
                    "window": {
                        "type": "string",
                        "default": "30d",
                        "description": (
                            "Lookback window, e.g. '30d', '24h', or 'none'."
                        ),
                    },
                    "scope_path": {"type": "string"},
                },
            },
        ),
        Tool(
            name="memory.record_use",
            description=(
                "Record that an observation was used; optionally mark the "
                "outcome as success or failure to reinforce the memory."
            ),
            inputSchema={
                "type": "object",
                "required": ["id"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "outcome": {
                        "type": "string",
                        "enum": ["success", "failure"],
                    },
                },
            },
        ),
        Tool(
            name="knowledge.search",
            description=(
                "BM25 search against the knowledge-base markdown corpus. "
                "Returns document paths and rank."
            ),
            inputSchema={
                "type": "object",
                "required": ["query"],
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string"},
                    "project": {"type": "string"},
                },
            },
        ),
        Tool(
            name="knowledge.list",
            description=(
                "List indexed knowledge documents. When ``project`` is "
                "supplied, project-scoped rows are filtered to that project."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "project": {"type": "string"},
                },
            },
        ),
        Tool(
            name="memory.start_ui",
            description=(
                "Launch the better-memory review UI. Stub — the UI ships in "
                "Plan 2; this tool currently returns an explanatory error."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        ),
        Tool(
            name="memory.start_episode",
            description=(
                "Declare a goal for the current session. Opens a new "
                "foreground episode or hardens the existing background "
                "episode. Returns the active episode id."
            ),
            inputSchema={
                "type": "object",
                "required": ["goal"],
                "additionalProperties": False,
                "properties": {
                    "goal": {"type": "string"},
                    "tech": {"type": "string"},
                },
            },
        ),
        Tool(
            name="memory.close_episode",
            description=(
                "Close the current session's active episode. outcome is one "
                "of success / partial / abandoned / no_outcome."
            ),
            inputSchema={
                "type": "object",
                "required": ["outcome"],
                "additionalProperties": False,
                "properties": {
                    "outcome": {
                        "type": "string",
                        "enum": [
                            "success",
                            "partial",
                            "abandoned",
                            "no_outcome",
                        ],
                    },
                    "close_reason": {
                        "type": "string",
                        "enum": [
                            "goal_complete",
                            "plan_complete",
                            "abandoned",
                            "superseded",
                            "session_end_reconciled",
                        ],
                    },
                    "summary": {"type": "string"},
                },
            },
        ),
        Tool(
            name="memory.reconcile_episodes",
            description=(
                "List episodes that are still open from prior sessions, "
                "for the LLM to prompt the user about."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        ),
        Tool(
            name="memory.list_episodes",
            description=(
                "List episodes with optional filters. For UI and LLM "
                "introspection."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "project": {"type": "string"},
                    "outcome": {
                        "type": "string",
                        "enum": [
                            "success",
                            "partial",
                            "abandoned",
                            "no_outcome",
                        ],
                    },
                    "only_open": {"type": "boolean"},
                },
            },
        ),
    ]


# --------------------------------------------------------------------------- helpers


def _parse_window(value: str | None) -> int | None:
    """Parse a window like ``"30d"`` or ``"24h"`` into integer days.

    ``None`` / ``"none"`` disables windowing entirely. ``"Xd"`` returns ``X``,
    ``"Xh"`` rounds up to at least one day. Anything else raises ``ValueError``
    so callers see a clear message rather than silent mis-filtering.
    """
    if value is None:
        return 30
    raw = value.strip().lower()
    if raw in {"", "none"}:
        return None
    if raw.endswith("d"):
        magnitude = raw[:-1]
        if not magnitude.isdigit():
            raise ValueError(f"Unrecognised window: {value!r}")
        return int(magnitude)
    if raw.endswith("h"):
        magnitude = raw[:-1]
        if not magnitude.isdigit():
            raise ValueError(f"Unrecognised window: {value!r}")
        hours = int(magnitude)
        # Any sub-day window collapses to a single day because the underlying
        # hybrid_search windowing is day-granular. We round up so a request of
        # "1h" still matches same-day rows.
        days = max(1, hours // 24 + (1 if hours % 24 else 0))
        return days
    raise ValueError(f"Unrecognised window: {value!r}")


def _serialize_result(result: SearchResult) -> dict[str, Any]:
    return {
        "id": result.id,
        "content": result.content,
        "component": result.component,
        "theme": result.theme,
        "outcome": result.outcome,
        "reinforcement_score": result.reinforcement_score,
        "created_at": result.created_at,
        "final_score": result.final_score,
    }


def _serialize_knowledge_search(result: KnowledgeSearchResult) -> dict[str, Any]:
    doc = result.document
    return {
        "path": doc.path,
        "scope": doc.scope,
        "project": doc.project,
        "language": doc.language,
        "rank": result.rank,
    }


def _serialize_knowledge_doc(doc: KnowledgeDocument) -> dict[str, Any]:
    return {
        "path": doc.path,
        "scope": doc.scope,
        "project": doc.project,
        "language": doc.language,
    }


# --------------------------------------------------------------------------- factory


def create_server() -> tuple[Server, Callable[[], Awaitable[None]]]:
    """Wire services and register tools.

    Returns a ``(server, cleanup)`` tuple where ``cleanup`` is an idempotent
    async function that closes the two SQLite connections and the Ollama
    embedder's HTTP client. Callers must await ``cleanup`` on shutdown
    (typically in a ``finally`` around ``server.run``).
    """
    config = get_config()

    memory_conn = connect(config.memory_db)
    apply_migrations(memory_conn, migrations_dir=_MEMORY_MIGRATIONS)
    knowledge_conn = connect(config.knowledge_db)
    apply_migrations(knowledge_conn, migrations_dir=_KNOWLEDGE_MIGRATIONS)

    # One embedder per server. Construction is cheap and does NOT contact
    # Ollama (see OllamaEmbedder.__init__); the first embed() call does.
    embedder = OllamaEmbedder()

    # Cheap reachability probe against Ollama. Warn (to stderr) if it's down
    # but do not block startup — knowledge.* tools still work without Ollama,
    # and if Ollama comes up later, memory.observe / memory.retrieve will
    # succeed on their next call without a restart.
    _probe_ollama(config.ollama_host)

    episodes = EpisodeService(memory_conn)
    observations = ObservationService(memory_conn, embedder, episodes=episodes)
    knowledge = KnowledgeService(
        knowledge_conn,
        knowledge_base=config.knowledge_base,
    )
    spool = SpoolService(memory_conn, config.spool_dir)

    # Session-start behaviour: open a background episode for this server's
    # session so observations written before the LLM declares a goal still
    # bind to an episode. Phase 3's session-start hook will eventually
    # trigger this externally; Phase 2 does it at factory time.
    try:
        episodes.open_background(
            session_id=observations._session_id,
            project=Path.cwd().name,
        )
    except Exception:  # noqa: BLE001 — best-effort startup hook
        # Don't block server startup; lazy-open in ObservationService.create
        # catches the gap.
        pass

    # Session-start behaviour: reindex knowledge at startup. mtime-only, so
    # the cost is O(files) stat calls on an already-indexed corpus. We
    # swallow any exception so a missing / unreachable knowledge base does
    # not block the server from serving memory tools.
    if config.knowledge_base.is_dir():
        try:
            knowledge.reindex()
        except Exception:  # noqa: BLE001 — best-effort startup hook
            pass

    server: Server = Server(name="better-memory")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return _tool_definitions()

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[TextContent]:
        args = arguments or {}

        if name == "memory.observe":
            obs_id = await observations.create(
                content=args["content"],
                component=args.get("component"),
                theme=args.get("theme"),
                trigger_type=args.get("trigger_type"),
                outcome=args.get("outcome", "neutral"),
                tech=args.get("tech"),
            )
            return [TextContent(type="text", text=json.dumps({"id": obs_id}))]

        if name == "memory.retrieve":
            # 1. Drain spool — must happen before any search so fresh hook
            #    events are searchable. SpoolService.drain is idempotent.
            try:
                spool.drain()
            except Exception:  # noqa: BLE001 — drain is best-effort
                # A drain failure should not prevent retrieval.
                pass

            window_days = _parse_window(args.get("window", "30d"))
            query = args.get("query")
            component = args.get("component")
            scope_path = args.get("scope_path")

            buckets = await observations.retrieve(
                query=query,
                component=component,
                window_days=window_days,
                scope_path=scope_path,
            )

            # Insights table was dropped in Phase 1. Reflection retrieval
            # replaces this path in Phase 6; for now, return [] so clients
            # continue to receive the payload shape they expect.
            insight_hits: list[dict[str, Any]] = []
            knowledge_hits: list[dict[str, Any]] = []
            if query:
                knowledge_hits = [
                    _serialize_knowledge_search(r)
                    for r in knowledge.search(query, limit=5)
                ]

            payload = {
                "do": [_serialize_result(r) for r in buckets.do],
                "dont": [_serialize_result(r) for r in buckets.dont],
                "neutral": [_serialize_result(r) for r in buckets.neutral],
                "insights": insight_hits,
                "knowledge": knowledge_hits,
            }
            return [TextContent(type="text", text=json.dumps(payload))]

        if name == "memory.record_use":
            observations.record_use(
                args["id"],
                outcome=args.get("outcome"),
            )
            return [TextContent(type="text", text=json.dumps({"ok": True}))]

        if name == "knowledge.search":
            results = knowledge.search(
                args["query"],
                project=args.get("project"),
            )
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        [_serialize_knowledge_search(r) for r in results]
                    ),
                )
            ]

        if name == "knowledge.list":
            docs = knowledge.list_documents(project=args.get("project"))
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        [_serialize_knowledge_doc(d) for d in docs]
                    ),
                )
            ]

        if name == "memory.start_ui":
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "error": (
                                "UI not yet implemented — planned for Plan 2."
                            )
                        }
                    ),
                )
            ]

        if name == "memory.start_episode":
            # Phase 2 scope: open/harden foreground episode only — reflection
            # synthesis is Phase 5. Session id is resolved from the
            # ObservationService's session (same id the observation path uses).
            episode_id = episodes.start_foreground(
                session_id=observations._session_id,
                project=Path.cwd().name,
                goal=args["goal"],
                tech=args.get("tech"),
            )
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"episode_id": episode_id}),
                )
            ]

        if name == "memory.close_episode":
            outcome = args["outcome"]
            # Default close_reason: match outcome for the common paths.
            default_reasons = {
                "success": "goal_complete",
                "partial": "superseded",
                "abandoned": "abandoned",
                "no_outcome": "session_end_reconciled",
            }
            close_reason = args.get("close_reason") or default_reasons[outcome]
            closed_id = episodes.close_active(
                session_id=observations._session_id,
                outcome=outcome,
                close_reason=close_reason,
                summary=args.get("summary"),
            )
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"closed_episode_id": closed_id}),
                )
            ]

        if name == "memory.reconcile_episodes":
            open_episodes = episodes.unclosed_episodes(
                exclude_session_ids={observations._session_id}
            )
            payload = [
                {
                    "episode_id": e.id,
                    "project": e.project,
                    "tech": e.tech,
                    "goal": e.goal,
                    "started_at": e.started_at,
                }
                for e in open_episodes
            ]
            return [TextContent(type="text", text=json.dumps(payload))]

        if name == "memory.list_episodes":
            rows = episodes.list_episodes(
                project=args.get("project"),
                outcome=args.get("outcome"),
                only_open=args.get("only_open", False),
            )
            payload = [
                {
                    "episode_id": e.id,
                    "project": e.project,
                    "tech": e.tech,
                    "goal": e.goal,
                    "started_at": e.started_at,
                    "hardened_at": e.hardened_at,
                    "ended_at": e.ended_at,
                    "close_reason": e.close_reason,
                    "outcome": e.outcome,
                    "summary": e.summary,
                }
                for e in rows
            ]
            return [TextContent(type="text", text=json.dumps(payload))]

        raise ValueError(f"Unknown tool: {name}")

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
        try:
            await embedder.aclose()
        except Exception:  # noqa: BLE001 — best-effort shutdown
            pass

    return server, cleanup


async def run() -> None:
    """Start the server on stdio and run until the client disconnects."""
    server, cleanup = create_server()
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await cleanup()


if __name__ == "__main__":  # pragma: no cover — module entry-point shim
    asyncio.run(run())
