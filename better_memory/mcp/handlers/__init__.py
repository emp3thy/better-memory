"""MCP tool handlers, organised by domain.

Each domain module exposes one ``*Handlers`` class with a ``handlers()``
method returning ``list[Handler]`` for registration with the
``ToolDispatcher``. The :func:`all_handlers` helper assembles every
domain's contribution.
"""
from __future__ import annotations

from better_memory.mcp.dispatcher import Handler
from better_memory.mcp.handlers.observations import ObservationHandlers


def all_handlers() -> list[Handler]:
    """Return the union of every domain's registered handlers.

    Filled in as handler modules land (Tasks 7-14).
    """
    return [
        *ObservationHandlers().handlers(),
    ]
