"""Knowledge-domain MCP tool handlers.

Tools: knowledge.search, knowledge.list.

Bodies lifted verbatim from the legacy ``_call_tool`` if-chain. The two
serializer helpers (``_serialize_knowledge_search_result`` /
``_serialize_knowledge_document``) flatten the dataclass shapes the
``KnowledgeService`` returns into the JSON payload the IDE-LLM
consumes. Neither tool is capability-gated.
"""
from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent

from better_memory.mcp.container import ServiceContainer
from better_memory.mcp.dispatcher import Handler
from better_memory.services.knowledge import (
    KnowledgeDocument,
    KnowledgeSearchResult,
)

_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["query"],
    "additionalProperties": False,
    "properties": {
        "query": {"type": "string"},
        "project": {"type": "string"},
    },
}

_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "project": {"type": "string"},
    },
}

_SEARCH_DESCRIPTION = (
    "BM25 search against the knowledge-base markdown corpus. "
    "Returns document paths and rank."
)
_LIST_DESCRIPTION = (
    "List indexed knowledge documents. When ``project`` is "
    "supplied, project-scoped rows are filtered to that project."
)


def _serialize_knowledge_search_result(
    result: KnowledgeSearchResult,
) -> dict[str, Any]:
    doc = result.document
    return {
        "path": doc.path,
        "scope": doc.scope,
        "project": doc.project,
        "language": doc.language,
        "rank": result.rank,
    }


def _serialize_knowledge_document(doc: KnowledgeDocument) -> dict[str, Any]:
    return {
        "path": doc.path,
        "scope": doc.scope,
        "project": doc.project,
        "language": doc.language,
    }


class KnowledgeHandlers:
    """The knowledge.search / knowledge.list tools."""

    async def search(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        results = services.knowledge.search(
            args["query"],
            project=args.get("project"),
        )
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    [_serialize_knowledge_search_result(r) for r in results]
                ),
            )
        ]

    async def list(
        self, services: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        docs = services.knowledge.list_documents(project=args.get("project"))
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    [_serialize_knowledge_document(d) for d in docs]
                ),
            )
        ]

    def handlers(self) -> list[Handler]:
        return [
            Handler(
                name="knowledge.search",
                description=_SEARCH_DESCRIPTION,
                schema=_SEARCH_SCHEMA,
                call=self.search,
            ),
            Handler(
                name="knowledge.list",
                description=_LIST_DESCRIPTION,
                schema=_LIST_SCHEMA,
                call=self.list,
            ),
        ]
