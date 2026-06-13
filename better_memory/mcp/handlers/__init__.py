"""MCP tool handlers, organised by domain.

Each domain module exposes one ``*Handlers`` class with a ``handlers()``
method returning ``list[Handler]`` for registration with the
``ToolDispatcher``. The :func:`all_handlers` helper assembles every
domain's contribution.
"""
from __future__ import annotations

from better_memory.mcp.dispatcher import Handler
from better_memory.mcp.handlers.episodes import EpisodeHandlers
from better_memory.mcp.handlers.knowledge import KnowledgeHandlers
from better_memory.mcp.handlers.observations import ObservationHandlers
from better_memory.mcp.handlers.ratings import RatingHandlers
from better_memory.mcp.handlers.reflections import ReflectionHandlers
from better_memory.mcp.handlers.retention import RetentionHandlers
from better_memory.mcp.handlers.semantics import SemanticHandlers


def all_handlers() -> list[Handler]:
    """Return the union of every domain's registered handlers.

    Filled in as handler modules land (Tasks 7-14).
    """
    return [
        *ObservationHandlers().handlers(),
        *SemanticHandlers().handlers(),
        *EpisodeHandlers().handlers(),
        *ReflectionHandlers().handlers(),
        *RetentionHandlers().handlers(),
        *KnowledgeHandlers().handlers(),
        *RatingHandlers().handlers(),
    ]
