"""SemanticHandlers: 4 semantic tools + scope-null fallback."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from better_memory.mcp.container import ServiceContainer
from better_memory.mcp.handlers.semantics import SemanticHandlers


def _services_with_semantic(semantic: MagicMock) -> ServiceContainer:
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
        semantic=semantic,
        session_bootstrap=MagicMock(),
    )


@pytest.mark.asyncio
async def test_semantic_observe_falls_back_to_project_when_scope_null() -> None:
    semantic = MagicMock()
    semantic.create.return_value = "sem-1"
    services = _services_with_semantic(semantic)
    handler = SemanticHandlers()
    await handler.observe(services, {"content": "x", "scope": None})
    assert semantic.create.call_args.kwargs["scope"] == "project"


@pytest.mark.asyncio
async def test_semantic_observe_returns_id() -> None:
    semantic = MagicMock()
    semantic.create.return_value = "sem-42"
    services = _services_with_semantic(semantic)
    handler = SemanticHandlers()
    result = await handler.observe(services, {"content": "hello"})
    assert json.loads(result[0].text) == {"id": "sem-42"}


@pytest.mark.asyncio
async def test_semantic_retrieve_serializes_memories() -> None:
    memory = MagicMock()
    memory.id = "sem-1"
    memory.content = "user prefers dark mode"
    memory.project = "my-project"
    memory.scope = "project"
    memory.created_at = "2026-06-01T00:00:00Z"
    memory.updated_at = "2026-06-01T00:00:00Z"
    semantic = MagicMock()
    semantic.list_for_project.return_value = [memory]
    services = _services_with_semantic(semantic)
    handler = SemanticHandlers()
    result = await handler.retrieve(services, {"project": "my-project"})
    payload = json.loads(result[0].text)
    assert payload == [
        {
            "id": "sem-1",
            "content": "user prefers dark mode",
            "project": "my-project",
            "scope": "project",
            "created_at": "2026-06-01T00:00:00Z",
            "updated_at": "2026-06-01T00:00:00Z",
        },
    ]
    semantic.list_for_project.assert_called_once_with(project="my-project")


@pytest.mark.asyncio
async def test_semantic_retrieve_defaults_project_to_cwd_derived() -> None:
    semantic = MagicMock()
    semantic.list_for_project.return_value = []
    services = _services_with_semantic(semantic)
    handler = SemanticHandlers()
    await handler.retrieve(services, {})
    # project= kwarg present and non-empty (defaulted via project_name()).
    call = semantic.list_for_project.call_args
    assert "project" in call.kwargs
    assert call.kwargs["project"]


@pytest.mark.asyncio
async def test_semantic_update_calls_update_text_and_returns_ok() -> None:
    semantic = MagicMock()
    services = _services_with_semantic(semantic)
    handler = SemanticHandlers()
    result = await handler.update(
        services, {"id": "sem-1", "content": "new text"},
    )
    assert json.loads(result[0].text) == {"ok": True}
    semantic.update_text.assert_called_once_with(id="sem-1", content="new text")


@pytest.mark.asyncio
async def test_semantic_delete_returns_ok() -> None:
    semantic = MagicMock()
    services = _services_with_semantic(semantic)
    handler = SemanticHandlers()
    result = await handler.delete(services, {"id": "sem-1"})
    assert json.loads(result[0].text) == {"ok": True}
    semantic.delete.assert_called_once_with(id="sem-1")


def test_handlers_registers_four() -> None:
    handler = SemanticHandlers()
    names = [h.name for h in handler.handlers()]
    assert names == [
        "memory.semantic_observe",
        "memory.semantic_retrieve",
        "memory.semantic_update",
        "memory.semantic_delete",
    ]
