"""Handlers for the observation-lifecycle tools.

Tools: ``memory.observe``, ``memory.retrieve_observations``,
``memory.record_use``, ``memory.run_retention``.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent

from better_memory import _diag
from better_memory.config import project_name
from better_memory.services.observation import ObservationService
from better_memory.services.retention import RetentionService
from better_memory.storage import StorageBackend

#: AgentCore memoryRecordIds are >= 40 chars (enforced client-side by
#: botocore). memory.observe returns EVENT ids (shorter, different id
#: domain) which are not ratable records — without this floor, a
#: record_use on an event id stalls ~20s in the backend's transient-404
#: retry loop inside the serialized MCP dispatch loop.
_MIN_AGENTCORE_RECORD_ID_LEN = 40


class ObservationToolHandlers:
    """Observation create / drill-down / reinforcement / retention.

    ``remote`` (agentcore mode) routes the data tools to the
    StorageBackend; ``None`` (the sqlite default) keeps the original
    service path unchanged.
    """

    def __init__(
        self,
        *,
        observations: ObservationService,
        retention: RetentionService,
        remote: StorageBackend | None = None,
    ) -> None:
        self._observations = observations
        self._retention = retention
        self._remote = remote

    def tools(self) -> dict[str, Any]:
        return {
            "memory.observe": self.observe,
            "memory.retrieve_observations": self.retrieve_observations,
            "memory.record_use": self.record_use,
            "memory.run_retention": self.run_retention,
        }

    async def observe(self, args: dict[str, Any]) -> list[TextContent]:
        with _diag.trace(
            "mcp.memory.observe",
            content_len=len(args.get("content") or ""),
            scope=args.get("scope") or "project",
            component=args.get("component"),
        ):
            if self._remote is not None:
                _diag.step("mcp.memory.observe", "calling_remote_observe")
                obs_id = await self._remote.observe(
                    content=args["content"],
                    component=args.get("component"),
                    theme=args.get("theme"),
                    trigger_type=args.get("trigger_type"),
                    outcome=args.get("outcome", "neutral"),
                    tech=args.get("tech"),
                    # Same {"scope": null} defence as the sqlite path below.
                    scope=args.get("scope") or "project",
                )
                _diag.step(
                    "mcp.memory.observe", "remote_returned", obs_id=obs_id
                )
                return [
                    TextContent(type="text", text=json.dumps({"id": obs_id}))
                ]
            _diag.step("mcp.memory.observe", "calling_observations_create")
            obs_id = await self._observations.create(
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
            _diag.step("mcp.memory.observe", "create_returned", obs_id=obs_id)
            return [TextContent(type="text", text=json.dumps({"id": obs_id}))]

    async def retrieve_observations(
        self, args: dict[str, Any]
    ) -> list[TextContent]:
        project = args.get("project") or project_name()
        if self._remote is not None:
            remote_results = await self._remote.list_observations(
                project=project,
                episode_id=args.get("episode_id"),
                component=args.get("component"),
                theme=args.get("theme"),
                outcome=args.get("outcome"),
                query=args.get("query"),
                limit=args.get("limit", 50),
            )
            # AgentCore events carry datetime event_timestamps
            # (botocore-parsed); default=str keeps the payload JSON-safe.
            return [
                TextContent(
                    type="text", text=json.dumps(remote_results, default=str)
                )
            ]
        results = await self._observations.list_observations(
            project=project,
            episode_id=args.get("episode_id"),
            component=args.get("component"),
            theme=args.get("theme"),
            outcome=args.get("outcome"),
            query=args.get("query"),
            limit=args.get("limit", 50),
        )
        return [TextContent(type="text", text=json.dumps(results))]

    async def record_use(self, args: dict[str, Any]) -> list[TextContent]:
        if self._remote is not None:
            record_id = args["id"]
            if len(record_id) < _MIN_AGENTCORE_RECORD_ID_LEN:
                raise ValueError(
                    "memory.record_use in agentcore mode takes an AgentCore "
                    "memory RECORD id (>= 40 chars, e.g. from "
                    f"memory.retrieve); got {record_id!r}. Event ids "
                    "returned by memory.observe are not ratable records."
                )
            self._remote.record_use(record_id, outcome=args.get("outcome"))
            return [TextContent(type="text", text=json.dumps({"ok": True}))]
        self._observations.record_use(
            args["id"],
            outcome=args.get("outcome"),
        )
        return [TextContent(type="text", text=json.dumps({"ok": True}))]

    async def run_retention(self, args: dict[str, Any]) -> list[TextContent]:
        report = self._retention.run(
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
