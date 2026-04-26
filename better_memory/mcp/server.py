"""MCP stdio server exposing better-memory's six tools.

The server wires together the existing service classes and presents them
as MCP tools over stdio. On startup, the knowledge-base is reindexed
(mtime-only, so this is cheap and idempotent) as a session-start step.

Tools
-----
* ``memory.observe``       — create a new observation; returns ``{"id": ...}``.
* ``memory.retrieve``      — reflections bucketed by polarity (do/dont/neutral),
                             filtered by project/tech/phase/polarity, capped 20 per bucket.
                             Drains the spool before retrieving.
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
from better_memory.llm.ollama import OllamaChat
from better_memory.services.episode import EpisodeService
from better_memory.services.knowledge import (
    KnowledgeDocument,
    KnowledgeSearchResult,
    KnowledgeService,
)
from better_memory.services.observation import ObservationService
from better_memory.services.reflection import ReflectionSynthesisService
from better_memory.services.retention import RetentionService
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
                "Retrieve reflections (do / dont / neutral lessons distilled "
                "from prior observations) bucketed by polarity. Filter by "
                "project, tech, phase, and polarity. For raw observation "
                "lookup, use memory.retrieve_observations."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "project": {"type": "string"},
                    "tech": {"type": "string"},
                    "phase": {
                        "type": "string",
                        "enum": ["planning", "implementation", "general"],
                    },
                    "polarity": {
                        "type": "string",
                        "enum": ["do", "dont", "neutral"],
                    },
                    "limit_per_bucket": {"type": "integer"},
                },
            },
        ),
        Tool(
            name="memory.retrieve_observations",
            description=(
                "Retrieve raw observations matching given filters. Drill-down "
                "tool — use memory.retrieve for the distilled-reflections "
                "default. With ``query``, results are ranked by hybrid "
                "FTS5 + sqlite-vec relevance; without, ordered created_at "
                "DESC. ``episode_id`` and ``theme`` filters are ignored "
                "in query mode."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "project": {"type": "string"},
                    "episode_id": {"type": "string"},
                    "component": {"type": "string"},
                    "theme": {"type": "string"},
                    "outcome": {
                        "type": "string",
                        "enum": ["success", "failure", "neutral"],
                    },
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
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
        Tool(
            name="memory.run_retention",
            description=(
                "Apply spec §9 retention rules — flip eligible "
                "observations to status='archived' and optionally "
                "hard-delete archived rows older than prune_age_days."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "retention_days": {
                        "type": "integer",
                        "default": 90,
                        "description": (
                            "Age threshold for the three archive "
                            "rules. Default 90 (per spec §9)."
                        ),
                    },
                    "prune": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "If true, also hard-delete archived rows "
                            "older than prune_age_days."
                        ),
                    },
                    "prune_age_days": {
                        "type": "integer",
                        "default": 365,
                        "description": (
                            "Age threshold for prune mode. Default 365."
                        ),
                    },
                    "dry_run": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "If true, return the counts without "
                            "writing any changes to the DB."
                        ),
                    },
                },
            },
        ),
    ]


# --------------------------------------------------------------------------- helpers


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

    # LLM client for reflection synthesis.
    chat = OllamaChat(host=config.ollama_host, model=config.consolidate_model)
    reflections = ReflectionSynthesisService(memory_conn, chat=chat)
    retention = RetentionService(conn=memory_conn)

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
            # 1. Drain spool — must happen before any retrieval so fresh
            #    hook events (session_start, commit_close) are processed.
            #    SpoolService.drain is idempotent.
            try:
                spool.drain()
            except Exception:  # noqa: BLE001 — drain is best-effort
                pass

            project = args.get("project") or Path.cwd().name
            limit_per_bucket = args.get("limit_per_bucket", 20)
            buckets = reflections.retrieve_reflections(
                project=project,
                tech=args.get("tech"),
                phase=args.get("phase"),
                polarity=args.get("polarity"),
                limit_per_bucket=limit_per_bucket,
            )
            return [TextContent(type="text", text=json.dumps(buckets))]

        if name == "memory.retrieve_observations":
            project = args.get("project") or Path.cwd().name
            results = await observations.list_observations(
                project=project,
                episode_id=args.get("episode_id"),
                component=args.get("component"),
                theme=args.get("theme"),
                outcome=args.get("outcome"),
                query=args.get("query"),
                limit=args.get("limit", 50),
            )
            return [TextContent(type="text", text=json.dumps(results))]

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
            project = Path.cwd().name
            episode_id = episodes.start_foreground(
                session_id=observations._session_id,
                project=project,
                goal=args["goal"],
                tech=args.get("tech"),
            )
            buckets = await reflections.synthesize(
                goal=args["goal"],
                tech=args.get("tech"),
                project=project,
            )
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"episode_id": episode_id, "reflections": buckets}
                    ),
                )
            ]

        if name == "memory.close_episode":
            outcome = args["outcome"]
            # Default close_reason: match outcome for the common paths.
            default_reasons = {
                "success": "goal_complete",
                "partial": "plan_complete",
                "abandoned": "abandoned",
                "no_outcome": "session_end_reconciled",
            }
            close_reason = args.get("close_reason") or default_reasons[outcome]
            try:
                closed_id = episodes.close_active(
                    session_id=observations._session_id,
                    outcome=outcome,
                    close_reason=close_reason,
                    summary=args.get("summary"),
                )
            except ValueError:
                # No active episode — already closed (e.g. by a prior commit-
                # trailer drain) or never opened. Matches the "safe no-op"
                # contract documented in the CLAUDE snippet's plan-complete
                # section.
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {"closed_episode_id": None, "already_closed": True}
                        ),
                    )
                ]
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"closed_episode_id": closed_id, "already_closed": False}
                    ),
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

        if name == "memory.run_retention":
            report = retention.run(
                retention_days=args.get("retention_days", 90),
                prune=args.get("prune", False),
                prune_age_days=args.get("prune_age_days", 365),
                dry_run=args.get("dry_run", False),
            )
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "archived_via_retired_reflection":
                            report.archived_via_retired_reflection,
                        "archived_via_consumed_without_reflection":
                            report.archived_via_consumed_without_reflection,
                        "archived_via_no_outcome_episode":
                            report.archived_via_no_outcome_episode,
                        "pruned": report.pruned,
                    }),
                )
            ]

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
        try:
            await chat.aclose()
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
