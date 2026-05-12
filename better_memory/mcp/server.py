"""MCP stdio server exposing better-memory's tools.

The server wires together the existing service classes and presents them
as MCP tools over stdio. On startup, the knowledge-base is reindexed
(mtime-only, so this is cheap and idempotent) as a session-start step.

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
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, cast

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from better_memory.config import get_config, project_name
from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.embeddings.ollama import OllamaEmbedder
from better_memory.services import ui_launcher
from better_memory.services.episode import EpisodeService
from better_memory.services.knowledge import (
    KnowledgeDocument,
    KnowledgeSearchResult,
    KnowledgeService,
)
from better_memory.services.observation import ObservationService
from better_memory.services.reflection import (
    EpisodeContext,
    EpisodeQueueCounts,
    ReflectionSynthesisService,
    SynthesisStep,
)
from better_memory.services.memory_rating import MemoryRatingService
from better_memory.services.retention import RetentionService
from better_memory.services.retention_scheduler import RetentionScheduler
from better_memory.services.spool import SpoolService

# Module-level migration directories. Packaged alongside the code so
# ``python -m better_memory.mcp`` finds them without needing extra config.
_MEMORY_MIGRATIONS = Path(__file__).parent.parent / "db" / "migrations"
_KNOWLEDGE_MIGRATIONS = Path(__file__).parent.parent / "db" / "knowledge_migrations"

_OLLAMA_PROBE_TIMEOUT_SEC = 2.0

logger = logging.getLogger(__name__)


def _run_best_effort(operation: str, fn: Callable[[], Any]) -> None:
    """Run ``fn`` swallowing any ``Exception`` but logging it via the module logger.

    Used by best-effort hooks inside ``memory.retrieve`` (spool drain,
    retention scheduler) where a failure must NEVER block the call but
    must still produce a discoverable diagnostic. The previous behaviour
    silently dropped the exception, so a broken background path could
    fail invisibly for weeks.
    """
    try:
        fn()
    except Exception:  # noqa: BLE001 — best-effort wrapper
        logger.exception("best-effort %s failed", operation)


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
                    "scope": {
                        "type": "string",
                        "enum": ["project", "general"],
                        "description": (
                            "'project' (default) for project-scoped observations; "
                            "'general' for cross-project workflow rules that should "
                            "surface in every project's memory_retrieve."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="memory.semantic_observe",
            description=(
                "Record a user-stated fact or preference. Distinct from "
                "memory.observe (episodic): semantic memories are "
                "user-asserted current truths, retrieved at session "
                "startup. Set scope='general' for cross-project rules."
            ),
            inputSchema={
                "type": "object",
                "required": ["content"],
                "additionalProperties": False,
                "properties": {
                    "content": {"type": "string"},
                    "scope": {
                        "type": "string",
                        "enum": ["project", "general"],
                        "description": (
                            "'project' (default) for project-scoped rules; "
                            "'general' for cross-project workflow rules."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="memory.semantic_retrieve",
            description=(
                "Return user-stated facts/preferences for the current "
                "project, merged with all general-scope semantic memories. "
                "Flat list ordered newest-first."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "project": {
                        "type": "string",
                        "description": (
                            "Optional project override; "
                            "defaults to cwd-derived."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="memory.semantic_update",
            description=(
                "Edit a semantic memory's content in place. Bumps updated_at."
            ),
            inputSchema={
                "type": "object",
                "required": ["id", "content"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
        ),
        Tool(
            name="memory.semantic_delete",
            description=(
                "Remove a semantic memory. Idempotent — no error if id absent."
            ),
            inputSchema={
                "type": "object",
                "required": ["id"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
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
                "Spawn or reuse the better-memory management UI. Returns "
                '{"url": str, "reused": bool}. Reuses an existing live UI '
                "when one is already running on /healthz."
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
            name="memory.synthesize_next_get_context",
            description=(
                "Return the next pending episode's full context for "
                "consolidation: episode metadata, all observations on it, "
                "and tech-filtered existing reflections. The IDE-LLM "
                "consumes this, decides what new/augment/merge/ignore "
                "actions to take, and submits the decision via "
                "memory.synthesize_next_apply. Returns "
                '{"episode_id": null, "queue": {...}} when the queue is '
                "empty. See the better-memory-synthesize skill for the "
                "full workflow and decision schema."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "project": {
                        "type": "string",
                        "description": (
                            "Optional project override; defaults to "
                            "cwd-derived."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="memory.synthesize_next_apply",
            description=(
                "Apply a synthesis decision for one episode. Atomically "
                "creates new reflections, augments existing ones, merges "
                "duplicates, and marks observations as consumed (or "
                "ignored). Marks the episode synthesized. Returns a "
                "step summary {episode_id, counts, queue, failure}. "
                "decision shape: {new: [...], augment: [...], "
                "merge: [...], ignore: [...]} — see the "
                "better-memory-synthesize skill for the per-entry "
                "field schema."
            ),
            inputSchema={
                "type": "object",
                "required": ["episode_id", "decision"],
                "additionalProperties": False,
                "properties": {
                    "episode_id": {"type": "string"},
                    "decision": {
                        "type": "object",
                        "description": (
                            "Decision JSON; validation errors are "
                            "returned to the caller without stamping "
                            "the episode failed (caller can retry)."
                        ),
                    },
                    "project": {
                        "type": "string",
                        "description": (
                            "Optional project override; defaults to "
                            "cwd-derived."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="memory.session_bootstrap",
            description=(
                "Open or reuse a session episode and inject all project + "
                "general semantic memories and reflections as "
                "additionalContext markdown. Mirrors what the SessionStart "
                "hook does; callable manually for recovery, testing, or "
                "post-/clear re-injection."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": ["startup", "resume", "clear", "compact"],
                        "description": (
                            "SessionStart payload source. Unknown values "
                            "coerce to 'startup' inside the service."
                        ),
                    },
                    "session_id": {
                        "type": "string",
                        "description": (
                            "Optional SessionStart payload session_id. "
                            "Defaults to $CLAUDE_SESSION_ID env var, or a "
                            "fresh UUID."
                        ),
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Optional working directory. Defaults to "
                            "server's process cwd."
                        ),
                    },
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
        Tool(
            name="memory.list_session_exposures",
            description=(
                "Return the unrated session_memory_exposure rows for the "
                "current Claude session (resolved server-side from "
                "CLAUDE_SESSION_ID env). Read-only; no side effects. "
                "Used by the rate-session-memories skill as the "
                "authoritative anti-hallucination list."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        ),
        Tool(
            name="memory.apply_session_ratings",
            description=(
                "Atomic batch rating for the current Claude session "
                "(resolved server-side from CLAUDE_SESSION_ID). Called "
                "at session end by the rate-session-memories skill. "
                "Raises if CLAUDE_SESSION_ID is unset — call only inside "
                "an active Claude session."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "required": ["ratings"],
                "properties": {
                    "ratings": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["kind", "id", "class"],
                            "properties": {
                                "kind": {"enum": ["reflection", "semantic"]},
                                "id": {"type": "string"},
                                "class": {
                                    "enum": ["cited", "shaped", "ignored", "misled"]
                                },
                            },
                        },
                    },
                },
            },
        ),
        Tool(
            name="memory.credit",
            description=(
                "Per-tool-use credit. When you actively use a memory "
                "retrieved during this session (quote it, follow its "
                "guidance, or it misled you), call this immediately. "
                "Resolved server-side from CLAUDE_SESSION_ID. "
                "class must be 'cited', 'shaped', or 'misled' — NOT 'ignored'."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "id", "class"],
                "properties": {
                    "kind": {"enum": ["reflection", "semantic"]},
                    "id": {"type": "string"},
                    "class": {"enum": ["cited", "shaped", "misled"]},
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


def _serialize_queue(queue: EpisodeQueueCounts) -> dict[str, int]:
    return {
        "pending": queue.pending,
        "in_cooldown": queue.in_cooldown,
        "done": queue.done,
    }


def _serialize_synth_get_context(
    ctx: EpisodeContext | None,
    queue: EpisodeQueueCounts,
) -> dict[str, Any]:
    """Build the JSON payload for ``memory.synthesize_next_get_context``.

    Returns ``{"episode_id": null, "queue": {...}}`` when the queue is
    empty. Otherwise returns the full episode + observations + reflections
    bundle the IDE-LLM consumes to decide on synthesis actions.
    """
    queue_json = _serialize_queue(queue)
    if ctx is None:
        return {"episode_id": None, "queue": queue_json}
    return {
        "episode_id": ctx.episode.id,
        "queue": queue_json,
        "episode": {
            "id": ctx.episode.id,
            "project": ctx.episode.project,
            "goal": ctx.episode.goal,
            "tech": ctx.episode.tech,
            "outcome": ctx.episode.outcome,
        },
        "observations": [
            {
                "id": o.id,
                "content": o.content,
                "outcome": o.outcome,
                "component": o.component,
                "theme": o.theme,
                "tech": o.tech,
                "created_at": o.created_at,
                "status": o.status,
            }
            for o in ctx.observations
        ],
        "reflections": [
            {
                "id": r.id,
                "title": r.title,
                "tech": r.tech,
                "phase": r.phase,
                "polarity": r.polarity,
                "use_cases": r.use_cases,
                "hints": json.loads(r.hints) if r.hints else [],
                "confidence": r.confidence,
                "status": r.status,
            }
            for r in ctx.reflections
        ],
    }


def _serialize_synth_apply_ok(step: SynthesisStep) -> dict[str, Any]:
    """Build the JSON payload for a successful ``memory.synthesize_next_apply``."""
    return {
        "ok": True,
        "episode_id": step.episode_id,
        "counts": step.counts,
        "queue": _serialize_queue(step.queue),
    }


def _serialize_synth_apply_validation_error(message: str) -> dict[str, Any]:
    """Build the JSON payload for a decision-validation failure.

    Validation errors do NOT stamp ``synth_failed_at``; the caller can
    retry with a corrected payload.
    """
    return {
        "ok": False,
        "error": "validation",
        "message": message,
    }


def _serialize_synth_apply_state_error(message: str) -> dict[str, Any]:
    """Build the JSON payload for an episode-state failure.

    Surfaces apply-time precondition violations (episode not found,
    wrong project, already synthesized) without raising into the MCP
    framework's generic isError surface. Caller should NOT retry the
    same episode_id; pull fresh context via
    ``memory.synthesize_next_get_context`` first.
    """
    return {
        "ok": False,
        "error": "state",
        "message": message,
    }


# --------------------------------------------------------------------------- factory


def create_server() -> tuple[Server, Callable[[], Coroutine[Any, Any, None]]]:
    """Wire services and register tools.

    Returns a ``(server, cleanup)`` tuple where ``cleanup`` is an idempotent
    async function that closes the two SQLite connections and the embedder's
    HTTP client. Callers must await ``cleanup`` on shutdown (typically in a
    ``finally`` around ``server.run``).
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

    # Concurrency invariant: the five memory-side services below
    # (EpisodeService, ObservationService, ReflectionSynthesisService,
    # RetentionService, SpoolService) all share ``memory_conn``. Each
    # service's docstring documents that it owns the connection and the
    # caller must not share it with another service that has an open
    # transaction. That contract holds here only because the MCP stdio
    # transport serialises requests — _call_tool runs one tool invocation
    # at a time, so at most one service's SAVEPOINT is ever in flight.
    # If any future change introduces async fan-out, a background task,
    # or a worker thread that writes to ``memory_conn``, this assumption
    # breaks and the services must be reworked to use per-task
    # connections (or a connection pool with explicit checkout).
    episodes = EpisodeService(memory_conn)
    observations = ObservationService(memory_conn, embedder, episodes=episodes)

    # Reflection synthesis is driven by the IDE-LLM via two MCP tools
    # (memory.synthesize_next_get_context / _apply). The service no
    # longer holds a chat client.
    reflections = ReflectionSynthesisService(memory_conn)
    retention = RetentionService(conn=memory_conn)
    memory_rating = MemoryRatingService(memory_conn)

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
                # `or "project"` (not `, "project"` default) defends against
                # MCP clients sending {"scope": null} — dict.get returns the
                # default only when the key is absent, not when its value is
                # None. Without this, scope=None propagates to ObservationService
                # .create() which raises ValueError.
                scope=args.get("scope") or "project",
            )
            return [TextContent(type="text", text=json.dumps({"id": obs_id}))]

        if name == "memory.semantic_observe":
            from better_memory.services.semantic import SemanticMemoryService
            project = project_name()
            svc = SemanticMemoryService(memory_conn)
            # `args.get("scope") or "project"` (not `, "project"` default) defends
            # against MCP clients sending {"scope": null} — dict.get returns the
            # default only when the key is absent, not when its value is None.
            # Same fix as PR #25's BugBot finding on memory.observe.
            memory_id = svc.create(
                content=args["content"],
                project=project,
                scope=args.get("scope") or "project",
            )
            return [TextContent(type="text", text=json.dumps({"id": memory_id}))]

        if name == "memory.semantic_retrieve":
            from better_memory.services.semantic import SemanticMemoryService
            project = args.get("project") or project_name()
            svc = SemanticMemoryService(memory_conn)
            memories = svc.list_for_project(project=project)
            payload = [
                {
                    "id": m.id,
                    "content": m.content,
                    "project": m.project,
                    "scope": m.scope,
                    "created_at": m.created_at,
                    "updated_at": m.updated_at,
                }
                for m in memories
            ]
            return [TextContent(type="text", text=json.dumps(payload))]

        if name == "memory.semantic_update":
            from better_memory.services.semantic import SemanticMemoryService
            svc = SemanticMemoryService(memory_conn)
            svc.update_text(id=args["id"], content=args["content"])
            return [TextContent(type="text", text=json.dumps({"ok": True}))]

        if name == "memory.semantic_delete":
            from better_memory.services.semantic import SemanticMemoryService
            svc = SemanticMemoryService(memory_conn)
            svc.delete(id=args["id"])
            return [TextContent(type="text", text=json.dumps({"ok": True}))]

        if name == "memory.retrieve":
            # 1. Drain spool — must happen before any retrieval so fresh
            #    hook events (session_start, commit_close) are processed.
            #    SpoolService.drain is idempotent.
            _run_best_effort("spool.drain", spool.drain)

            # 2. Maybe run retention. Guard ensures at most once per 24h
            #    regardless of how often retrieve is called. Best-effort:
            #    a retention failure must NEVER block memory.retrieve.
            def _retention_step() -> None:
                cfg = get_config()
                RetentionScheduler(
                    memory_conn, auto_prune=cfg.auto_prune
                ).maybe_run(triggered_by="retrieve")

            _run_best_effort("retention scheduler", _retention_step)

            project = args.get("project") or project_name()
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
            project = args.get("project") or project_name()
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
            result = ui_launcher.start_ui()
            return [
                TextContent(type="text", text=json.dumps(result))
            ]

        if name == "memory.start_episode":
            project = project_name()
            episode_id = episodes.start_foreground(
                session_id=observations.session_id,
                project=project,
                goal=args["goal"],
                tech=args.get("tech"),
            )
            # Reflection synthesis is now interactive — driven by the IDE-LLM
            # via memory.synthesize_next_get_context / _apply. We surface the
            # current pending count so the LLM knows whether it should run
            # synthesis (typically before treating retrieved reflections as
            # canonical).
            queue = reflections._read_queue_counts(project=project)
            buckets = reflections.retrieve_reflections(
                project=project, tech=args.get("tech"),
            )
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "episode_id": episode_id,
                            "reflections": buckets,
                            "pending_synthesis": _serialize_queue(queue),
                        }
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
                    session_id=observations.session_id,
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
                exclude_session_ids={observations.session_id}
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

        if name == "memory.session_bootstrap":
            import uuid

            from better_memory.services.session_bootstrap import (
                SessionBootstrapService,
            )

            cwd_arg = args.get("cwd") or os.getcwd()
            session_id_arg = (
                args.get("session_id")
                or os.environ.get("CLAUDE_SESSION_ID")
                or uuid.uuid4().hex
            )
            svc = SessionBootstrapService(memory_conn)
            result = svc.bootstrap(
                source=args.get("source"),
                session_id=session_id_arg,
                cwd=Path(cwd_arg),
            )
            payload = {
                "additionalContext": result.additional_context,
                "project": result.project,
                "source": result.source,
                "episode": {
                    "id": result.episode_id,
                    "action": result.episode_action,
                },
                "counts": {
                    "semantic": result.semantic_count,
                    "reflections": result.reflections_counts,
                },
            }
            return [TextContent(type="text", text=json.dumps(payload))]

        if name == "memory.synthesize_next_get_context":
            project = args.get("project") or project_name()
            ctx = reflections.get_next_pending_context(project=project)
            queue = reflections._read_queue_counts(project=project)
            payload = _serialize_synth_get_context(ctx, queue)
            return [TextContent(type="text", text=json.dumps(payload))]

        if name == "memory.synthesize_next_apply":
            from better_memory.services.reflection import (
                SynthesisResponseError,
            )
            project = args.get("project") or project_name()
            episode_id = args["episode_id"]
            decision = args["decision"]
            try:
                # ``decision`` is already a parsed dict (the MCP framework
                # decoded it before dispatch). Use the dict-shape parser
                # directly to skip a redundant json.dumps → json.loads
                # round-trip.
                response = reflections.parse_response_dict(decision)
            except SynthesisResponseError as exc:
                payload = _serialize_synth_apply_validation_error(str(exc))
                return [TextContent(type="text", text=json.dumps(payload))]
            try:
                step = reflections.apply_decision(
                    episode_id=episode_id,
                    response=response,
                    project=project,
                )
            except ValueError as exc:
                # Episode-state preconditions: not found / wrong project /
                # already synthesized. Surface as structured error so the
                # IDE-LLM can refetch context instead of retrying the same
                # stale id.
                payload = _serialize_synth_apply_state_error(str(exc))
                return [TextContent(type="text", text=json.dumps(payload))]
            return [
                TextContent(
                    type="text",
                    text=json.dumps(_serialize_synth_apply_ok(step)),
                )
            ]

        if name == "memory.list_session_exposures":
            sid = os.environ.get("CLAUDE_SESSION_ID")
            if not sid:
                payload = {"session_id": None, "exposures": []}
            else:
                # Dedupe by (memory_kind, memory_id) — a memory can have two
                # exposure rows (bootstrap + retrieve) in one session. The
                # rating apply path stamps ALL unrated rows per (kind, id)
                # in one UPDATE, so the LLM must see one entry per unique
                # memory; otherwise apply_session_ratings rejects the batch
                # for duplicate (kind, id) pairs.
                rows = memory_conn.execute(
                    """
                    SELECT e.memory_kind, e.memory_id,
                           MIN(e.exposed_at) AS exposed_at,
                           MIN(e.source) AS source,
                           COALESCE(r.title, s.content) AS display
                      FROM session_memory_exposure e
                      LEFT JOIN reflections        r ON e.memory_kind='reflection'
                                                    AND e.memory_id = r.id
                      LEFT JOIN semantic_memories  s ON e.memory_kind='semantic'
                                                    AND e.memory_id = s.id
                     WHERE e.session_id = ? AND e.rated_at IS NULL
                     GROUP BY e.memory_kind, e.memory_id
                     ORDER BY exposed_at ASC
                    """,
                    (sid,),
                ).fetchall()
                payload = {
                    "session_id": sid,
                    "exposures": [
                        {
                            "kind": r["memory_kind"],
                            "id": r["memory_id"],
                            **({"title": r["display"]} if r["memory_kind"] == "reflection"
                               else {"content": r["display"]}),
                            "exposed_at": r["exposed_at"],
                            "source": r["source"],
                        }
                        for r in rows
                    ],
                }
            return [TextContent(type="text", text=json.dumps(payload))]

        if name == "memory.apply_session_ratings":
            sid = os.environ.get("CLAUDE_SESSION_ID")
            if not sid:
                raise ValueError("No active session: CLAUDE_SESSION_ID not set")
            payload = memory_rating.apply_session_ratings(
                session_id=sid,
                ratings=args["ratings"],
            )
            return [TextContent(type="text", text=json.dumps(payload))]

        if name == "memory.credit":
            sid = os.environ.get("CLAUDE_SESSION_ID")
            if not sid:
                payload = {"applied": None, "skipped": "no_session"}
            else:
                payload = memory_rating.credit_one(
                    session_id=sid,
                    kind=args["kind"],
                    id=args["id"],
                    classification=args["class"],
                )
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

    server, cleanup = create_server()
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
    server, cleanup = create_server()
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await cleanup()


if __name__ == "__main__":  # pragma: no cover — module entry-point shim
    asyncio.run(run())
