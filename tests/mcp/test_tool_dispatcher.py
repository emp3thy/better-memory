"""ToolDispatcher: register, list, call, capability-gate."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any
from unittest.mock import MagicMock

import pytest
from mcp.types import TextContent

from better_memory.mcp.container import ServiceContainer
from better_memory.mcp.dispatcher import Handler, ToolDispatcher


def _container(*, supports_synthesis: bool) -> ServiceContainer:
    backend = MagicMock()
    backend.supports_synthesis = supports_synthesis
    return ServiceContainer(
        config=MagicMock(), memory_conn=MagicMock(), backend=backend,
        episodes=MagicMock(), observations=MagicMock(), reflections=MagicMock(),
        retention=MagicMock(), memory_rating=MagicMock(), knowledge=MagicMock(),
        spool=MagicMock(), semantic=MagicMock(), session_bootstrap=MagicMock(),
    )


async def _stub_call(
    services: ServiceContainer, args: dict[str, Any],
) -> list[TextContent]:
    return []


def test_handler_is_frozen_dataclass() -> None:
    h = Handler(name="x", schema={"type": "object"}, call=_stub_call)
    assert h.name == "x"
    assert h.requires_synthesis is False
    with pytest.raises(FrozenInstanceError):
        h.name = "y"  # type: ignore[misc]


def test_dispatcher_lists_all_when_synthesis_supported() -> None:
    services = _container(supports_synthesis=True)
    handlers = [
        Handler("a", {}, _stub_call),
        Handler("b", {}, _stub_call, requires_synthesis=True),
    ]
    dispatcher = ToolDispatcher(services, handlers)
    names = [t.name for t in dispatcher.tool_definitions()]
    assert names == ["a", "b"]


def test_dispatcher_hides_synthesis_tools_when_unsupported() -> None:
    services = _container(supports_synthesis=False)
    handlers = [
        Handler("a", {}, _stub_call),
        Handler("b", {}, _stub_call, requires_synthesis=True),
    ]
    dispatcher = ToolDispatcher(services, handlers)
    names = [t.name for t in dispatcher.tool_definitions()]
    assert names == ["a"]


@pytest.mark.asyncio
async def test_dispatcher_call_unknown_raises_value_error() -> None:
    services = _container(supports_synthesis=True)
    dispatcher = ToolDispatcher(services, [])
    with pytest.raises(ValueError, match="Unknown tool: nope"):
        await dispatcher.call("nope", {})


@pytest.mark.asyncio
async def test_dispatcher_call_gated_tool_when_unsupported_raises_unknown() -> None:
    services = _container(supports_synthesis=False)
    handler = Handler("b", {}, _stub_call, requires_synthesis=True)
    dispatcher = ToolDispatcher(services, [handler])
    with pytest.raises(ValueError, match="Unknown tool: b"):
        await dispatcher.call("b", {})


@pytest.mark.asyncio
async def test_dispatcher_call_routes_to_handler() -> None:
    services = _container(supports_synthesis=True)
    seen: dict[str, Any] = {}
    async def recording_call(
        svc: ServiceContainer, args: dict[str, Any],
    ) -> list[TextContent]:
        seen["svc"] = svc
        seen["args"] = args
        return [TextContent(type="text", text="ok")]
    handler = Handler("x", {"type": "object"}, recording_call)
    dispatcher = ToolDispatcher(services, [handler])
    result = await dispatcher.call("x", {"k": 1})
    assert seen["svc"] is services
    assert seen["args"] == {"k": 1}
    assert len(result) == 1
