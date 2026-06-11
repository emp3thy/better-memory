"""Per-domain MCP tool handler classes.

Each module in this package owns one tool domain and exposes a handler
class whose ``tools()`` method returns a ``{tool_name: coroutine}``
mapping. ``create_server`` (in :mod:`better_memory.mcp.server`)
constructs the services once, instantiates each handler class with the
services it needs, and merges the mappings into a single dispatch
registry — keeping ``_call_tool`` a pure lookup.

Handler classes hold no state beyond the injected services, so the
concurrency contract documented in ``create_server`` (one in-flight
tool call at a time over the shared sqlite connection) is unchanged.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.types import TextContent

from better_memory.mcp.handlers.episodes import EpisodeToolHandlers
from better_memory.mcp.handlers.knowledge import KnowledgeToolHandlers
from better_memory.mcp.handlers.observations import ObservationToolHandlers
from better_memory.mcp.handlers.reflections import ReflectionToolHandlers
from better_memory.mcp.handlers.semantics import SemanticToolHandlers
from better_memory.mcp.handlers.sessions import SessionToolHandlers

# One MCP tool invocation: JSON-decoded arguments in, TextContent out.
ToolHandler = Callable[[dict[str, Any]], Awaitable[list[TextContent]]]


def build_registry(
    *groups: Any,
) -> dict[str, ToolHandler]:
    """Merge handler groups into one ``{tool_name: handler}`` registry.

    Raises ``RuntimeError`` on a duplicate tool name so a wiring mistake
    fails at server construction, not at first dispatch.
    """
    registry: dict[str, ToolHandler] = {}
    for group in groups:
        for tool_name, handler in group.tools().items():
            if tool_name in registry:
                raise RuntimeError(f"duplicate tool handler: {tool_name}")
            registry[tool_name] = handler
    return registry


__all__ = [
    "EpisodeToolHandlers",
    "KnowledgeToolHandlers",
    "ObservationToolHandlers",
    "ReflectionToolHandlers",
    "SemanticToolHandlers",
    "SessionToolHandlers",
    "ToolHandler",
    "build_registry",
]
