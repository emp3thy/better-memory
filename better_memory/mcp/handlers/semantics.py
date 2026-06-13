"""Semantic-memory MCP tool handlers (memory.semantic_*).

SemanticMemoryService is now built once in ``_build_services`` and lives
on the container at ``services.semantic`` — eliminating the 4x per-call
inline construction smell in the legacy ``_call_tool`` if-chain.
"""
from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent

from better_memory.config import project_name
from better_memory.mcp.container import ServiceContainer
from better_memory.mcp.dispatcher import Handler

_OBSERVE_SCHEMA: dict[str, Any] = {
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
}

_RETRIEVE_SCHEMA: dict[str, Any] = {
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
}

_UPDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id", "content"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string"},
        "content": {"type": "string"},
    },
}

_DELETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string"},
    },
}

_OBSERVE_DESCRIPTION = (
    "Record a user-stated fact or preference. Distinct from "
    "memory.observe (episodic): semantic memories are "
    "user-asserted current truths, retrieved at session "
    "startup. Set scope='general' for cross-project rules."
)
_RETRIEVE_DESCRIPTION = (
    "Return user-stated facts/preferences for the current "
    "project, merged with all general-scope semantic memories. "
    "Flat list ordered newest-first."
)
_UPDATE_DESCRIPTION = (
    "Edit a semantic memory's content in place. Bumps updated_at."
)
_DELETE_DESCRIPTION = (
    "Remove a semantic memory. Idempotent — no error if id absent."
)


class SemanticHandlers:
    """All memory.semantic_* tools (observe, retrieve, update, delete)."""

    async def observe(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        project = project_name()
        # `args.get("scope") or "project"` (not `, "project"` default) defends
        # against MCP clients sending {"scope": null} — dict.get returns the
        # default only when the key is absent, not when its value is None.
        memory_id = services.semantic.create(
            content=args["content"],
            project=project,
            scope=args.get("scope") or "project",
        )
        return [TextContent(type="text", text=json.dumps({"id": memory_id}))]

    async def retrieve(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        project = args.get("project") or project_name()
        memories = services.semantic.list_for_project(project=project)
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

    async def update(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        services.semantic.update_text(id=args["id"], content=args["content"])
        return [TextContent(type="text", text=json.dumps({"ok": True}))]

    async def delete(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        services.semantic.delete(id=args["id"])
        return [TextContent(type="text", text=json.dumps({"ok": True}))]

    def handlers(self) -> list[Handler]:
        return [
            Handler(
                name="memory.semantic_observe",
                description=_OBSERVE_DESCRIPTION,
                schema=_OBSERVE_SCHEMA,
                call=self.observe,
            ),
            Handler(
                name="memory.semantic_retrieve",
                description=_RETRIEVE_DESCRIPTION,
                schema=_RETRIEVE_SCHEMA,
                call=self.retrieve,
            ),
            Handler(
                name="memory.semantic_update",
                description=_UPDATE_DESCRIPTION,
                schema=_UPDATE_SCHEMA,
                call=self.update,
            ),
            Handler(
                name="memory.semantic_delete",
                description=_DELETE_DESCRIPTION,
                schema=_DELETE_SCHEMA,
                call=self.delete,
            ),
        ]
