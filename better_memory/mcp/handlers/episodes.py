"""Episode-lifecycle MCP tool handlers.

Tools: memory.start_episode, memory.close_episode, memory.reconcile_episodes,
memory.list_episodes. Bodies lifted verbatim from the legacy ``_call_tool``
if-chain to preserve every documented invariant — notably the close try/except
fork that turns a ValueError ("no active episode") into the
``already_closed=True`` no-op payload, and the 10-field list_episodes
serializer the UI depends on.
"""
from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent

from better_memory.config import project_name
from better_memory.mcp.container import ServiceContainer
from better_memory.mcp.dispatcher import Handler
from better_memory.services.reflection import EpisodeQueueCounts


def _serialize_queue(queue: EpisodeQueueCounts) -> dict[str, int]:
    return {
        "pending": queue.pending,
        "in_cooldown": queue.in_cooldown,
        "done": queue.done,
    }


_START_EPISODE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["goal"],
    "additionalProperties": False,
    "properties": {
        "goal": {"type": "string"},
        "tech": {"type": "string"},
    },
}

_CLOSE_EPISODE_SCHEMA: dict[str, Any] = {
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
}

_RECONCILE_EPISODES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
}

_LIST_EPISODES_SCHEMA: dict[str, Any] = {
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
}


_START_EPISODE_DESCRIPTION = (
    "Declare a goal for the current session. Opens a new "
    "foreground episode or hardens the existing background "
    "episode. Returns the active episode id."
)
_CLOSE_EPISODE_DESCRIPTION = (
    "Close the current session's active episode. outcome is one "
    "of success / partial / abandoned / no_outcome."
)
_RECONCILE_EPISODES_DESCRIPTION = (
    "List episodes that are still open from prior sessions, "
    "for the LLM to prompt the user about."
)
_LIST_EPISODES_DESCRIPTION = (
    "List episodes with optional filters. For UI and LLM "
    "introspection."
)


class EpisodeHandlers:
    """All memory.start_episode / .close_episode / .reconcile_episodes / .list_episodes tools."""

    async def start_episode(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        project = project_name()
        episode_id = services.episodes.start_foreground(
            session_id=services.observations.session_id,
            project=project,
            goal=args["goal"],
            tech=args.get("tech"),
        )
        # Reflection synthesis is now interactive — driven by the IDE-LLM via
        # memory.synthesize_next_get_context / _apply. We surface the current
        # pending count so the LLM knows whether it should run synthesis
        # (typically before treating retrieved reflections as canonical).
        queue = services.reflections.read_queue_counts(project=project)
        buckets = services.backend.retrieve(
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

    async def close_episode(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
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
            closed_id = services.episodes.close_active(
                session_id=services.observations.session_id,
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

    async def reconcile_episodes(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        open_episodes = services.episodes.unclosed_episodes(
            exclude_session_ids={services.observations.session_id}
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

    async def list_episodes(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        rows = services.episodes.list_episodes(
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

    def handlers(self) -> list[Handler]:
        return [
            Handler(
                name="memory.start_episode",
                description=_START_EPISODE_DESCRIPTION,
                schema=_START_EPISODE_SCHEMA,
                call=self.start_episode,
            ),
            Handler(
                name="memory.close_episode",
                description=_CLOSE_EPISODE_DESCRIPTION,
                schema=_CLOSE_EPISODE_SCHEMA,
                call=self.close_episode,
            ),
            Handler(
                name="memory.reconcile_episodes",
                description=_RECONCILE_EPISODES_DESCRIPTION,
                schema=_RECONCILE_EPISODES_SCHEMA,
                call=self.reconcile_episodes,
            ),
            Handler(
                name="memory.list_episodes",
                description=_LIST_EPISODES_DESCRIPTION,
                schema=_LIST_EPISODES_SCHEMA,
                call=self.list_episodes,
            ),
        ]
