"""ObservationHandlers: route the 4 observation tools + preserve invariants."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from better_memory.mcp.container import ServiceContainer
from better_memory.mcp.handlers.observations import ObservationHandlers


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


@pytest.mark.asyncio
async def test_observe_routes_to_observations_create() -> None:
    services = _stub_services()
    services.observations.create = AsyncMock(return_value="obs-123")
    handler = ObservationHandlers()
    result = await handler.observe(services, {"content": "hello"})
    payload = json.loads(result[0].text)
    assert payload == {"id": "obs-123"}
    services.observations.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_observe_falls_back_to_project_when_scope_is_null() -> None:
    services = _stub_services()
    services.observations.create = AsyncMock(return_value="x")
    handler = ObservationHandlers()
    await handler.observe(services, {"content": "x", "scope": None})
    assert services.observations.create.call_args.kwargs["scope"] == "project"


@pytest.mark.asyncio
async def test_record_use_calls_service_and_returns_ok() -> None:
    services = _stub_services()
    services.observations.record_use = MagicMock()
    handler = ObservationHandlers()
    result = await handler.record_use(services, {"id": "x", "outcome": "success"})
    assert json.loads(result[0].text) == {"ok": True}
    services.observations.record_use.assert_called_once_with("x", outcome="success")


def test_handlers_registers_four() -> None:
    handler = ObservationHandlers()
    names = [h.name for h in handler.handlers()]
    assert names == [
        "memory.observe",
        "memory.retrieve",
        "memory.retrieve_observations",
        "memory.record_use",
    ]
