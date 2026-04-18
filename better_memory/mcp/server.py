"""MCP stdio server exposing better-memory's six tools.

The server wires together the existing service classes and presents them
as MCP tools over stdio. On startup, the knowledge-base is reindexed
(mtime-only, so this is cheap and idempotent) as a session-start step.

Tools
-----
* ``memory.observe``       — create a new observation; returns ``{"id": ...}``.
* ``memory.retrieve``      — three outcome buckets + insights + knowledge.
                             Drains the spool before searching.
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
from better_memory.services.insight import InsightService
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
                },
            },
        ),
        Tool(
            name="memory.retrieve",
            description=(
                "Retrieve observations, insights and knowledge relevant to "
                "the current task, bucketed by outcome (do / dont / neutral)."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string"},
                    "component": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": ["observation", "insight", "all"],
                        "default": "all",
                    },
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
        return int(raw[:-1])
    if raw.endswith("h"):
        hours = int(raw[:-1])
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


def _serialize_insight(result: Any) -> dict[str, Any]:
    insight = result.insight
    return {
        "id": insight.id,
        "title": insight.title,
        "content": insight.content,
        "polarity": insight.polarity,
        "status": insight.status,
        "rank": result.rank,
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


def create_server() -> Server:
    """Wire services and register tools; return an unstarted ``Server``."""
    config = get_config()

    memory_conn = connect(config.memory_db)
    apply_migrations(memory_conn, migrations_dir=_MEMORY_MIGRATIONS)
    knowledge_conn = connect(config.knowledge_db)
    apply_migrations(knowledge_conn, migrations_dir=_KNOWLEDGE_MIGRATIONS)

    # One embedder per server. Construction is cheap and does NOT contact
    # Ollama (see OllamaEmbedder.__init__); the first embed() call does.
    embedder = OllamaEmbedder()

    observations = ObservationService(memory_conn, embedder)
    insights = InsightService(memory_conn, embedder=embedder)
    knowledge = KnowledgeService(
        knowledge_conn,
        knowledge_base=config.knowledge_base,
    )
    spool = SpoolService(memory_conn, config.spool_dir)

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

            insight_hits: list[dict[str, Any]] = []
            knowledge_hits: list[dict[str, Any]] = []
            if query:
                insight_hits = [
                    _serialize_insight(r)
                    for r in insights.search(query, limit=5)
                ]
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

        raise ValueError(f"Unknown tool: {name}")

    return server


async def run() -> None:
    """Start the server on stdio and run until the client disconnects."""
    server = create_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":  # pragma: no cover — module entry-point shim
    asyncio.run(run())
