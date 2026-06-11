"""Handlers for the semantic-memory CRUD tools.

Tools: ``memory.semantic_observe``, ``memory.semantic_retrieve``,
``memory.semantic_update``, ``memory.semantic_delete``.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent

from better_memory.config import project_name
from better_memory.services.semantic import SemanticMemoryService


class SemanticToolHandlers:
    """User-stated facts/preferences CRUD."""

    def __init__(self, *, semantic: SemanticMemoryService) -> None:
        self._semantic = semantic

    def tools(self) -> dict[str, Any]:
        return {
            "memory.semantic_observe": self.semantic_observe,
            "memory.semantic_retrieve": self.semantic_retrieve,
            "memory.semantic_update": self.semantic_update,
            "memory.semantic_delete": self.semantic_delete,
        }

    async def semantic_observe(self, args: dict[str, Any]) -> list[TextContent]:
        project = project_name()
        # `args.get("scope") or "project"` (not `, "project"` default) defends
        # against MCP clients sending {"scope": null} — dict.get returns the
        # default only when the key is absent, not when its value is None.
        # Same fix as PR #25's BugBot finding on memory.observe.
        memory_id = self._semantic.create(
            content=args["content"],
            project=project,
            scope=args.get("scope") or "project",
        )
        return [TextContent(type="text", text=json.dumps({"id": memory_id}))]

    async def semantic_retrieve(self, args: dict[str, Any]) -> list[TextContent]:
        project = args.get("project") or project_name()
        memories = self._semantic.list_for_project(project=project)
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

    async def semantic_update(self, args: dict[str, Any]) -> list[TextContent]:
        self._semantic.update_text(id=args["id"], content=args["content"])
        return [TextContent(type="text", text=json.dumps({"ok": True}))]

    async def semantic_delete(self, args: dict[str, Any]) -> list[TextContent]:
        self._semantic.delete(id=args["id"])
        return [TextContent(type="text", text=json.dumps({"ok": True}))]
