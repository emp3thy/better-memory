"""Handlers for the knowledge-base introspection tools.

Tools: ``knowledge.search``, ``knowledge.list``.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent

from better_memory.mcp.serializers import (
    serialize_knowledge_doc,
    serialize_knowledge_search,
)
from better_memory.services.knowledge import KnowledgeService


class KnowledgeToolHandlers:
    """BM25 search + listing over the knowledge-base corpus."""

    def __init__(self, *, knowledge: KnowledgeService) -> None:
        self._knowledge = knowledge

    def tools(self) -> dict[str, Any]:
        return {
            "knowledge.search": self.search,
            "knowledge.list": self.list_documents,
        }

    async def search(self, args: dict[str, Any]) -> list[TextContent]:
        results = self._knowledge.search(
            args["query"],
            project=args.get("project"),
        )
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    [serialize_knowledge_search(r) for r in results]
                ),
            )
        ]

    async def list_documents(self, args: dict[str, Any]) -> list[TextContent]:
        docs = self._knowledge.list_documents(project=args.get("project"))
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    [serialize_knowledge_doc(d) for d in docs]
                ),
            )
        ]
