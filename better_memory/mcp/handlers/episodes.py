"""Handlers for the episode-lifecycle tools.

Tools: ``memory.start_episode``, ``memory.close_episode``,
``memory.reconcile_episodes``, ``memory.list_episodes``.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent

from better_memory.config import project_name
from better_memory.mcp.serializers import serialize_queue
from better_memory.services.episode import EpisodeService
from better_memory.services.observation import ObservationService
from better_memory.services.reflection import ReflectionSynthesisService
from better_memory.storage import StorageBackend


class EpisodeToolHandlers:
    """Episode open / close / reconcile / list."""

    def __init__(
        self,
        *,
        episodes: EpisodeService,
        observations: ObservationService,
        reflections: ReflectionSynthesisService,
        backend: StorageBackend,
        remote: StorageBackend | None = None,
    ) -> None:
        self._episodes = episodes
        self._observations = observations
        self._reflections = reflections
        self._backend = backend
        # agentcore-mode marker: episode tools are hidden from the
        # advertised list (supports_episodes=False) but stay registered
        # defensively; reconcile must not surface stale LOCAL episodes.
        self._remote = remote

    def tools(self) -> dict[str, Any]:
        return {
            "memory.start_episode": self.start_episode,
            "memory.close_episode": self.close_episode,
            "memory.reconcile_episodes": self.reconcile_episodes,
            "memory.list_episodes": self.list_episodes,
        }

    async def start_episode(self, args: dict[str, Any]) -> list[TextContent]:
        project = project_name()
        episode_id = self._episodes.start_foreground(
            session_id=self._observations.session_id,
            project=project,
            goal=args["goal"],
            tech=args.get("tech"),
        )
        # Reflection synthesis is now interactive — driven by the IDE-LLM
        # via memory.synthesize_next_get_context / _apply. We surface the
        # current pending count so the LLM knows whether it should run
        # synthesis (typically before treating retrieved reflections as
        # canonical).
        queue = self._reflections._read_queue_counts(project=project)
        buckets = self._backend.retrieve(
            project=project, tech=args.get("tech"),
        )
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "episode_id": episode_id,
                        "reflections": buckets,
                        "pending_synthesis": serialize_queue(queue),
                    }
                ),
            )
        ]

    async def close_episode(self, args: dict[str, Any]) -> list[TextContent]:
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
            closed_id = self._episodes.close_active(
                session_id=self._observations.session_id,
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
        self, args: dict[str, Any]
    ) -> list[TextContent]:
        if self._remote is not None:
            # AgentCore manages event grouping itself; the local episodes
            # table is not the source of truth in agentcore mode, so a
            # defensive caller gets an empty list rather than stale rows.
            return [TextContent(type="text", text=json.dumps([]))]
        open_episodes = self._episodes.unclosed_episodes(
            exclude_session_ids={self._observations.session_id}
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

    async def list_episodes(self, args: dict[str, Any]) -> list[TextContent]:
        rows = self._episodes.list_episodes(
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
