"""Observation-domain MCP tool handlers.

Tools: memory.observe, memory.retrieve, memory.retrieve_observations,
memory.record_use. Bodies lifted verbatim from the legacy
``_call_tool`` if-chain to preserve every documented invariant —
notably the ``spool.drain -> retention.maybe_schedule -> backend.retrieve``
ordering inside ``memory.retrieve``.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from mcp.types import TextContent

from better_memory import _diag
from better_memory.config import get_config, project_name
from better_memory.mcp._best_effort import _run_best_effort
from better_memory.mcp.container import ServiceContainer
from better_memory.mcp.dispatcher import Handler
from better_memory.services.retention_scheduler import RetentionScheduler

_OBSERVE_SCHEMA: dict[str, Any] = {
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
}

_RETRIEVE_SCHEMA: dict[str, Any] = {
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
}

_RETRIEVE_OBSERVATIONS_SCHEMA: dict[str, Any] = {
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
}

_RECORD_USE_SCHEMA: dict[str, Any] = {
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
}


_OBSERVE_DESCRIPTION = (
    "Record an observation about the current session (a fact, "
    "decision, bug fix, or outcome). Returns the new observation id."
)
_RETRIEVE_DESCRIPTION = (
    "Retrieve reflections (do / dont / neutral lessons distilled "
    "from prior observations) bucketed by polarity. Filter by "
    "project, tech, phase, and polarity. For raw observation "
    "lookup, use memory.retrieve_observations."
)
_RETRIEVE_OBSERVATIONS_DESCRIPTION = (
    "Retrieve raw observations matching given filters. Drill-down "
    "tool — use memory.retrieve for the distilled-reflections "
    "default. With ``query``, results are ranked by hybrid "
    "FTS5 + sqlite-vec relevance; without, ordered created_at "
    "DESC. ``episode_id`` and ``theme`` filters are ignored "
    "in query mode."
)
_RECORD_USE_DESCRIPTION = (
    "Record that an observation was used; optionally mark the "
    "outcome as success or failure to reinforce the memory."
)


class ObservationHandlers:
    """All memory.observe / .retrieve / .retrieve_observations / .record_use tools."""

    async def observe(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        with _diag.trace(
            "mcp.memory.observe",
            content_len=len(args.get("content") or ""),
            scope=args.get("scope") or "project",
            component=args.get("component"),
        ):
            _diag.step("mcp.memory.observe", "calling_observations_create")
            obs_id = await services.observations.create(
                content=args["content"],
                component=args.get("component"),
                theme=args.get("theme"),
                trigger_type=args.get("trigger_type"),
                outcome=args.get("outcome", "neutral"),
                tech=args.get("tech"),
                # `or "project"` (not `, "project"` default) defends against
                # MCP clients sending {"scope": null} — dict.get returns the
                # default only when the key is absent, not when its value is
                # None. Without this, scope=None propagates to
                # ObservationService.create() which raises ValueError.
                scope=args.get("scope") or "project",
            )
            _diag.step("mcp.memory.observe", "create_returned", obs_id=obs_id)
            return [TextContent(type="text", text=json.dumps({"id": obs_id}))]

    async def retrieve(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        with _diag.trace(
            "mcp.memory.retrieve",
            project=args.get("project") or "<auto>",
            tech=args.get("tech"),
            phase=args.get("phase"),
            polarity=args.get("polarity"),
        ):
            diag_cid: str | None = None
            t_retrieve = 0.0
            if _diag.enabled():
                diag_cid = uuid.uuid4().hex[:8]
                t_retrieve = time.monotonic()
                _diag.log(f"[bm-retrieve start cid={diag_cid}]")

            # 1. Drain spool — must happen before any retrieval so fresh
            #    hook events (session_start, commit_close) are processed.
            #    SpoolService.drain is idempotent.
            _diag.step("mcp.memory.retrieve", "before_spool_drain")
            _run_best_effort(
                "spool.drain", services.spool.drain, diag_cid=diag_cid,
            )
            _diag.step("mcp.memory.retrieve", "after_spool_drain")

            # 2. Maybe run retention. Guard ensures at most once per 24h
            #    regardless of how often retrieve is called. Best-effort:
            #    a retention failure must NEVER block memory.retrieve.
            def _retention_step() -> None:
                cfg = get_config()
                RetentionScheduler(
                    services.memory_conn, auto_prune=cfg.auto_prune,
                ).maybe_run(triggered_by="retrieve")

            _diag.step("mcp.memory.retrieve", "before_retention_scheduler")
            _run_best_effort(
                "retention scheduler", _retention_step, diag_cid=diag_cid,
            )
            _diag.step("mcp.memory.retrieve", "after_retention_scheduler")

            project = args.get("project") or project_name()
            limit_per_bucket = args.get("limit_per_bucket", 20)
            t_reflections = time.monotonic() if diag_cid else 0.0
            _diag.step(
                "mcp.memory.retrieve",
                "before_retrieve_reflections",
                project=project,
            )
            buckets = services.backend.retrieve(
                project=project,
                tech=args.get("tech"),
                phase=args.get("phase"),
                polarity=args.get("polarity"),
                limit_per_bucket=limit_per_bucket,
            )
            _diag.step(
                "mcp.memory.retrieve",
                "after_retrieve_reflections",
                do=len(buckets.get("do", [])),
                dont=len(buckets.get("dont", [])),
                neutral=len(buckets.get("neutral", [])),
            )
            if diag_cid is not None:
                refl_ms = int((time.monotonic() - t_reflections) * 1000)
                total_ms = int((time.monotonic() - t_retrieve) * 1000)
                _diag.log(
                    f"[bm-retrieve step=reflections cid={diag_cid} "
                    f"ms={refl_ms} status=ok]"
                )
                _diag.log(
                    f"[bm-retrieve done cid={diag_cid} total_ms={total_ms}]"
                )
            return [TextContent(type="text", text=json.dumps(buckets))]

    async def retrieve_observations(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        project = args.get("project") or project_name()
        results = await services.observations.list_observations(
            project=project,
            episode_id=args.get("episode_id"),
            component=args.get("component"),
            theme=args.get("theme"),
            outcome=args.get("outcome"),
            query=args.get("query"),
            limit=args.get("limit", 50),
        )
        return [TextContent(type="text", text=json.dumps(results))]

    async def record_use(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        services.observations.record_use(
            args["id"],
            outcome=args.get("outcome"),
        )
        return [TextContent(type="text", text=json.dumps({"ok": True}))]

    def handlers(self) -> list[Handler]:
        return [
            Handler(
                name="memory.observe",
                description=_OBSERVE_DESCRIPTION,
                schema=_OBSERVE_SCHEMA,
                call=self.observe,
            ),
            Handler(
                name="memory.retrieve",
                description=_RETRIEVE_DESCRIPTION,
                schema=_RETRIEVE_SCHEMA,
                call=self.retrieve,
            ),
            Handler(
                name="memory.retrieve_observations",
                description=_RETRIEVE_OBSERVATIONS_DESCRIPTION,
                schema=_RETRIEVE_OBSERVATIONS_SCHEMA,
                call=self.retrieve_observations,
            ),
            Handler(
                name="memory.record_use",
                description=_RECORD_USE_DESCRIPTION,
                schema=_RECORD_USE_SCHEMA,
                call=self.record_use,
            ),
        ]
