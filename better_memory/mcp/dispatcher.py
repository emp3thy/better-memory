"""ToolDispatcher: O(1) name to handler routing for MCP tool calls.

Replaces the 470-LOC ``if name == "..."`` chain in the legacy
``_call_tool`` closure. Handlers are registered as ``Handler`` dataclass
instances; the dispatcher owns the lookup, the capability gate, and the
"unknown tool" error contract.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mcp.types import TextContent, Tool

from better_memory.mcp.container import ServiceContainer

HandlerFn = Callable[[ServiceContainer, dict[str, Any]], Awaitable[list[TextContent]]]


@dataclass(frozen=True)
class Handler:
    """One MCP tool: name, JSON-Schema for inputs, async callable, capability flag."""
    name: str
    schema: dict[str, Any]
    call: HandlerFn
    description: str = ""
    requires_synthesis: bool = False


class ToolDispatcher:
    """Owns the {name: Handler} table plus the capability gate."""

    def __init__(
        self, services: ServiceContainer, handlers: list[Handler],
    ) -> None:
        self._services = services
        self._handlers: dict[str, Handler] = {h.name: h for h in handlers}

    def tool_definitions(self) -> list[Tool]:
        supports = self._services.backend.supports_synthesis
        return [
            Tool(name=h.name, description=h.description, inputSchema=h.schema)
            for h in self._handlers.values()
            if supports or not h.requires_synthesis
        ]

    async def call(
        self, name: str, args: dict[str, Any],
    ) -> list[TextContent]:
        handler = self._handlers.get(name)
        if handler is None:
            raise ValueError(f"Unknown tool: {name}")
        if (
            handler.requires_synthesis
            and not self._services.backend.supports_synthesis
        ):
            # Same error shape as today's fallthrough — clients depend on it.
            raise ValueError(f"Unknown tool: {name}")
        return await handler.call(self._services, args)
