"""Container bundling every long-lived service the MCP dispatcher uses.

Constructed once at startup by ``create_server``; passed by reference to
every tool handler. Frozen so handlers can never accidentally rebind a
service mid-call.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from better_memory.config import Config
    from better_memory.services.episode import EpisodeService
    from better_memory.services.knowledge import KnowledgeService
    from better_memory.services.memory_rating import MemoryRatingService
    from better_memory.services.observation import ObservationService
    from better_memory.services.reflection import ReflectionSynthesisService
    from better_memory.services.retention import RetentionService
    from better_memory.services.semantic import SemanticMemoryService
    from better_memory.services.session_bootstrap import SessionBootstrapService
    from better_memory.services.spool import SpoolService
    from better_memory.storage import StorageBackend


@dataclass(frozen=True)
class ServiceContainer:
    """All long-lived services + connections, built once in create_server."""
    config: Config
    memory_conn: sqlite3.Connection
    backend: StorageBackend
    episodes: EpisodeService
    observations: ObservationService
    reflections: ReflectionSynthesisService
    retention: RetentionService
    memory_rating: MemoryRatingService
    knowledge: KnowledgeService
    spool: SpoolService
    semantic: SemanticMemoryService
    session_bootstrap: SessionBootstrapService
