"""KnowledgeHandlers: search + list."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from better_memory.mcp.container import ServiceContainer
from better_memory.mcp.handlers.knowledge import KnowledgeHandlers
from better_memory.services.knowledge import (
    KnowledgeDocument,
    KnowledgeSearchResult,
)


def _stub_services() -> ServiceContainer:
    return ServiceContainer(
        config=MagicMock(),
        memory_conn=MagicMock(),
        backend=MagicMock(),
        episodes=MagicMock(),
        observations=MagicMock(),
        reflections=MagicMock(),
        retention=MagicMock(),
        memory_rating=MagicMock(),
        knowledge=MagicMock(),
        spool=MagicMock(),
        semantic=MagicMock(),
        session_bootstrap=MagicMock(),
    )


def _make_doc(
    *,
    path: str = "standards/ralph-runtime.md",
    scope: str = "standard",
    project: str | None = None,
    language: str | None = None,
) -> KnowledgeDocument:
    return KnowledgeDocument(
        id="doc-1",
        path=path,
        scope=scope,
        project=project,
        language=language,
        content="# body",
        last_indexed="2026-06-01T00:00:00+00:00",
        file_mtime="2026-06-01T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_search_serializes_results() -> None:
    services = _stub_services()
    doc = _make_doc(path="standards/foo.md", scope="standard")
    services.knowledge.search = MagicMock(
        return_value=[KnowledgeSearchResult(document=doc, rank=-1.5)]
    )
    handler = KnowledgeHandlers()
    result = await handler.search(
        services, {"query": "foo", "project": "better-memory"}
    )
    payload = json.loads(result[0].text)
    assert payload == [
        {
            "path": "standards/foo.md",
            "scope": "standard",
            "project": None,
            "language": None,
            "rank": -1.5,
        }
    ]
    services.knowledge.search.assert_called_once_with(
        "foo", project="better-memory",
    )


@pytest.mark.asyncio
async def test_search_forwards_missing_project_as_none() -> None:
    services = _stub_services()
    services.knowledge.search = MagicMock(return_value=[])
    handler = KnowledgeHandlers()
    result = await handler.search(services, {"query": "bar"})
    payload = json.loads(result[0].text)
    assert payload == []
    services.knowledge.search.assert_called_once_with("bar", project=None)


@pytest.mark.asyncio
async def test_list_serializes_docs() -> None:
    services = _stub_services()
    docs = [
        _make_doc(
            path="standards/ralph-runtime.md",
            scope="standard",
        ),
        _make_doc(
            path="projects/better-memory/architecture.md",
            scope="project",
            project="better-memory",
        ),
    ]
    services.knowledge.list_documents = MagicMock(return_value=docs)
    handler = KnowledgeHandlers()
    result = await handler.list(services, {"project": "better-memory"})
    payload = json.loads(result[0].text)
    assert payload == [
        {
            "path": "standards/ralph-runtime.md",
            "scope": "standard",
            "project": None,
            "language": None,
        },
        {
            "path": "projects/better-memory/architecture.md",
            "scope": "project",
            "project": "better-memory",
            "language": None,
        },
    ]
    services.knowledge.list_documents.assert_called_once_with(
        project="better-memory",
    )


@pytest.mark.asyncio
async def test_list_forwards_missing_project_as_none() -> None:
    services = _stub_services()
    services.knowledge.list_documents = MagicMock(return_value=[])
    handler = KnowledgeHandlers()
    result = await handler.list(services, {})
    payload = json.loads(result[0].text)
    assert payload == []
    services.knowledge.list_documents.assert_called_once_with(project=None)


def test_handlers_registers_two() -> None:
    handler = KnowledgeHandlers()
    assert [h.name for h in handler.handlers()] == [
        "knowledge.search",
        "knowledge.list",
    ]


def test_handlers_are_not_capability_gated() -> None:
    handler = KnowledgeHandlers()
    assert all(not h.requires_synthesis for h in handler.handlers())
