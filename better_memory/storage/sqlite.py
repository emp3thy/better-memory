"""SqliteBackend — wraps existing services to satisfy the StorageBackend Protocol.

Behaviour-preserving wrapper. Existing service tests continue to exercise
service business logic directly; tests in tests/storage/ only verify the
protocol delegation surface.

Held state:
- ``memory_conn`` — open sqlite3 Connection
- ``embedder`` — passed to ObservationService
- ``session_id`` — used for episode lookups, ratings, exposures
- ``project`` — default project for any method whose project kwarg is omitted

Services are cached on ``__init__``. They are light objects over the same
connection; caching avoids duplicate connection-ownership claims and
N×-multiplied allocations on hot paths.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from better_memory.services.episode import EpisodeService
from better_memory.services.observation import ObservationService
from better_memory.storage.protocol import Outcome, UseOutcome


class SqliteBackend:
    """Wraps existing services to satisfy the StorageBackend Protocol."""

    def __init__(
        self,
        *,
        memory_conn: sqlite3.Connection,
        embedder: Any,
        session_id: str,
        project: str,
    ) -> None:
        self._conn = memory_conn
        self._embedder = embedder
        self._session_id = session_id
        self._project = project
        self._project_resolver = lambda: self._project
        self._episodes = EpisodeService(memory_conn)
        self._observations = ObservationService(
            memory_conn,
            embedder,
            session_id=session_id,
            project_resolver=self._project_resolver,
            episodes=self._episodes,
        )

    # ----- Capability flags -----

    @property
    def supports_synthesis(self) -> bool:
        return True

    # ----- Observations -----

    async def observe(
        self,
        *,
        content: str,
        component: str | None = None,
        theme: str | None = None,
        trigger_type: str | None = None,
        outcome: Outcome = "neutral",
        scope_path: str | None = None,
        project: str | None = None,
        tech: str | None = None,
        scope: str = "project",
    ) -> str:
        return await self._observations.create(
            content=content,
            component=component,
            theme=theme,
            trigger_type=trigger_type,
            outcome=outcome,
            scope_path=scope_path,
            project=project or self._project,
            tech=tech,
            scope=scope,
        )

    async def retrieve(
        self,
        query: str | None = None,
        *,
        component: str | None = None,
        status: str | None = "active",
        window_days: int | None = 30,
        scope_path: str | None = None,
        project: str | None = None,
        do_limit: int = 10,
        dont_limit: int = 10,
        neutral_limit: int = 5,
        candidate_k: int = 50,
        reinforcement_alpha: float = 0.1,
    ) -> Any:
        return await self._observations.retrieve(
            query,
            component=component,
            status=status,
            window_days=window_days,
            scope_path=scope_path,
            project=project or self._project,
            do_limit=do_limit,
            dont_limit=dont_limit,
            neutral_limit=neutral_limit,
            candidate_k=candidate_k,
            reinforcement_alpha=reinforcement_alpha,
        )

    async def list_observations(
        self,
        *,
        project: str | None = None,
        episode_id: str | None = None,
        component: str | None = None,
        theme: str | None = None,
        outcome: Outcome | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._observations.list_observations(
            project=project or self._project,
            episode_id=episode_id,
            component=component,
            theme=theme,
            outcome=outcome,
            query=query,
            limit=limit,
        )

    def record_use(
        self,
        observation_id: str,
        *,
        outcome: UseOutcome | None = None,
    ) -> None:
        self._observations.record_use(observation_id, outcome=outcome)
