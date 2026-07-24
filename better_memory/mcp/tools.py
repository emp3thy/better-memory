"""MCP tool definitions (names, descriptions, input schemas).

Pure data: nothing here touches a database or a service. The server's
``list_tools`` handler calls :func:`tool_definitions`; the actual
behaviour behind each name lives in :mod:`better_memory.mcp.handlers`.
"""

from __future__ import annotations

from mcp.types import Tool

#: Tools gated on ``supports_episodes``: the episode lifecycle plus local
#: retention are sqlite-only concepts. In agentcore mode AgentCore manages
#: event grouping via sessionId and applies its own event expiry, so these
#: are hidden from the advertised list (handlers stay registered
#: defensively — see ``create_server``).
_EPISODE_GATED_TOOLS: frozenset[str] = frozenset(
    {
        "memory.start_episode",
        "memory.close_episode",
        "memory.reconcile_episodes",
        "memory.list_episodes",
        "memory.run_retention",
    }
)


def tool_definitions(
    *,
    supports_synthesis: bool = True,
    supports_episodes: bool = True,
) -> list[Tool]:
    """Return the list of tools exposed over MCP.

    When ``supports_synthesis`` is False, the
    ``memory.synthesize_next_get_context`` and ``memory.synthesize_next_apply``
    tools are omitted. This gates the synthesis surface on the active
    StorageBackend's capability flag — backends without a local episode
    queue (e.g. AgentCoreBackend in Plan 2) do not expose these tools.

    When ``supports_episodes`` is False, the four episode-lifecycle tools
    and ``memory.run_retention`` are omitted too (same capability-flag
    pattern; False for AgentCoreBackend).
    """
    tools: list[Tool] = [
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
                "from prior observations) bucketed by polarity. ALWAYS pass "
                "`query` — a plain-language description of the task you are "
                "about to do — or you get the same generic top-ranked lessons "
                "every session regardless of what you are working on. Filter "
                "further by project, tech, phase, and polarity. For raw "
                "observation lookup, use memory.retrieve_observations."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Plain-language description of the task at hand, "
                            "e.g. 'changing how retention archives reflections'. "
                            "Ranks results by relevance to this text fused with "
                            "the usefulness prior."
                        ),
                    },
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
                # `query` is deliberately NOT required. Making it mandatory
                # would break every existing caller (and the start_episode /
                # bootstrap internal paths) for a param whose absence degrades
                # gracefully to the Wilson-prior. The description urges it
                # instead; the real usefulness gains come from the shortlist
                # default and the Wilson-score confidence boost, not from coercing
                # a query on every call.
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
                                    "enum": [
                                        "cited", "shaped", "ignored",
                                        "misled", "overlooked",
                                    ]
                                },
                                # Optional here at the wire-schema level;
                                # MemoryRatingService.apply_session_ratings
                                # enforces the real contract (required +
                                # non-empty for every non-ignored class,
                                # <=EVIDENCE_MAX_CHARS). Full schema
                                # description/required wiring is Task 3.
                                "evidence": {"type": "string"},
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
                "class must be 'cited', 'shaped', 'misled', or "
                "'overlooked' — NOT 'ignored'. Use 'overlooked' when the "
                "user pointed you back to a memory you already had but "
                "had not applied."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "id", "class"],
                "properties": {
                    "kind": {"enum": ["reflection", "semantic"]},
                    "id": {"type": "string"},
                    "class": {"enum": ["cited", "shaped", "misled", "overlooked"]},
                },
            },
        ),
    ]

    if supports_synthesis:
        tools.extend([
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
        ])

    if not supports_episodes:
        tools = [t for t in tools if t.name not in _EPISODE_GATED_TOOLS]

    return tools
