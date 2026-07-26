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
from better_memory.storage import StorageBackend


class SemanticToolHandlers:
    """User-stated facts/preferences CRUD.

    ``remote`` (agentcore mode) routes the tools to the StorageBackend;
    ``None`` (the sqlite default) keeps the original service path
    unchanged.
    """

    def __init__(
        self,
        *,
        semantic: SemanticMemoryService,
        remote: StorageBackend | None = None,
    ) -> None:
        self._semantic = semantic
        self._remote = remote

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
        if self._remote is not None:
            memory_id = self._remote.semantic_observe(
                content=args["content"],
                project=project,
                scope=args.get("scope") or "project",
            )
            return [
                TextContent(type="text", text=json.dumps({"id": memory_id}))
            ]
        memory_id = self._semantic.create(
            content=args["content"],
            project=project,
            scope=args.get("scope") or "project",
        )
        return [TextContent(type="text", text=json.dumps({"id": memory_id}))]

    async def semantic_retrieve(self, args: dict[str, Any]) -> list[TextContent]:
        project = args.get("project") or project_name()
        if self._remote is not None:
            # Sqlite parity (fix plan UD-2): AgentCoreBackend.semantic_list's
            # scope_filter=None view now performs the project+general merge
            # itself (fan out over both namespaces, dedup by id, project
            # wins — see storage/agentcore.py::semantic_list). A single call
            # here is sufficient; this handler used to do its own manual
            # merge over (None, "general") to work around semantic_list's
            # old project-only default, which now double-queries the
            # general namespace on top of semantic_list's own fan-out.
            # AgentCore records carry no project/created_at/updated_at —
            # keep the payload keys stable with None so downstream consumers
            # (rate-session-memories skill, management UI) don't KeyError.
            merged = [
                # semantic_list now returns SemanticMemory objects (§6.3),
                # matching the sqlite path below — attribute access, not
                # dict subscripting. created_at/updated_at stay None: the
                # stable UD-2 payload contract keeps agentcore semantic
                # rows key-identical to sqlite with placeholder timestamps.
                {
                    "id": record.id,
                    "content": record.content,
                    "project": None,
                    "scope": record.scope,
                    "created_at": None,
                    "updated_at": None,
                }
                for record in self._remote.semantic_list(
                    project=project, scope_filter=None
                )
            ]
            return [TextContent(type="text", text=json.dumps(merged))]
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
        if self._remote is not None:
            self._remote.semantic_update_text(
                id=args["id"], content=args["content"]
            )
            return [TextContent(type="text", text=json.dumps({"ok": True}))]
        self._semantic.update_text(id=args["id"], content=args["content"])
        return [TextContent(type="text", text=json.dumps({"ok": True}))]

    async def semantic_delete(self, args: dict[str, Any]) -> list[TextContent]:
        if self._remote is not None:
            self._remote.semantic_delete(id=args["id"])
            return [TextContent(type="text", text=json.dumps({"ok": True}))]
        self._semantic.delete(id=args["id"])
        return [TextContent(type="text", text=json.dumps({"ok": True}))]
