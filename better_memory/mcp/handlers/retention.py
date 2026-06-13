"""Retention-domain MCP tool handler.

Tools: memory.run_retention.

Body lifted verbatim from the legacy ``_call_tool`` if-chain. The
serialized payload exposes the four ``RetentionReport`` fields the
spec §9 audit pipeline consumes (three ``archived_via_*`` counters
plus ``pruned``).
"""
from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent

from better_memory.mcp.container import ServiceContainer
from better_memory.mcp.dispatcher import Handler

_RUN_RETENTION_SCHEMA: dict[str, Any] = {
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
}

_RUN_RETENTION_DESCRIPTION = (
    "Apply spec §9 retention rules — flip eligible "
    "observations to status='archived' and optionally "
    "hard-delete archived rows older than prune_age_days."
)


class RetentionHandlers:
    """The memory.run_retention tool."""

    async def run_retention(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        report = services.retention.run(
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

    def handlers(self) -> list[Handler]:
        return [
            Handler(
                name="memory.run_retention",
                description=_RUN_RETENTION_DESCRIPTION,
                schema=_RUN_RETENTION_SCHEMA,
                call=self.run_retention,
            ),
        ]
