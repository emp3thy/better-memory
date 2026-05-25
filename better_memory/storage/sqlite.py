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
from better_memory.services.memory_rating import MemoryRatingService
from better_memory.services.observation import ObservationService
from better_memory.services.reflection import (
    ReflectionService,
    ReflectionSynthesisService,
)
from better_memory.services.semantic import SemanticMemoryService
from better_memory.services.session_bootstrap import SessionBootstrapService
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
        self._semantic = SemanticMemoryService(memory_conn)
        self._reflection = ReflectionService(memory_conn)
        self._memory_rating = MemoryRatingService(memory_conn)
        self._session_bootstrap = SessionBootstrapService(memory_conn)
        self._synthesis = ReflectionSynthesisService(memory_conn)

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

    # ----- Semantic memories -----

    def semantic_observe(
        self,
        *,
        content: str,
        project: str | None = None,
        scope: str = "project",
    ) -> str:
        return self._semantic.create(
            content=content,
            project=project or self._project,
            scope=scope,
        )

    def semantic_list(
        self,
        *,
        project: str | None = None,
        scope_filter: str | None = None,
        search: str | None = None,
        track_exposure: bool = True,
    ) -> list[Any]:
        return self._semantic.list_for_project(
            project=project or self._project,
            scope_filter=scope_filter,
            search=search,
            track_exposure=track_exposure,
        )

    def semantic_update_text(self, *, id: str, content: str) -> None:
        self._semantic.update_text(id=id, content=content)

    def semantic_set_scope(self, *, id: str, scope: str) -> None:
        self._semantic.set_scope(id=id, scope=scope)

    def semantic_delete(self, *, id: str) -> None:
        self._semantic.delete(id=id)

    # ----- Episodes -----

    def open_background_episode(
        self,
        *,
        session_id: str,
        project: str,
    ) -> str:
        return self._episodes.open_background(
            session_id=session_id, project=project,
        )

    def start_foreground_episode(
        self,
        *,
        session_id: str,
        project: str,
        goal: str,
        tech: str | None = None,
    ) -> str:
        return self._episodes.start_foreground(
            session_id=session_id, project=project, goal=goal, tech=tech,
        )

    def close_active_episode(
        self,
        *,
        session_id: str,
        outcome: str,
        close_reason: str,
        summary: str | None = None,
    ) -> str:
        return self._episodes.close_active(
            session_id=session_id,
            outcome=outcome,
            close_reason=close_reason,
            summary=summary,
        )

    def close_episode_by_id(
        self,
        *,
        episode_id: str,
        outcome: str,
        close_reason: str,
        summary: str | None = None,
    ) -> str:
        return self._episodes.close_by_id(
            episode_id=episode_id,
            outcome=outcome,
            close_reason=close_reason,
            summary=summary,
        )

    def list_episodes(
        self,
        *,
        project: str | None = None,
        outcome: str | None = None,
        only_open: bool = False,
    ) -> list[Any]:
        return self._episodes.list_episodes(
            project=project,
            outcome=outcome,
            only_open=only_open,
        )

    # ----- Reflection lifecycle -----

    def promote_reflection(self, *, reflection_id: str) -> None:
        self._reflection.promote_to_general(reflection_id=reflection_id)

    def retire_reflection(self, *, reflection_id: str) -> None:
        self._reflection.retire(reflection_id=reflection_id)

    # ----- Session lifecycle -----

    def session_bootstrap(
        self,
        *,
        session_id: str,
        source: str | None = None,
        cwd: Any | None = None,
        project: str | None = None,
    ) -> Any:
        return self._session_bootstrap.bootstrap(
            session_id=session_id,
            source=source,
            cwd=cwd,
            project=project or self._project,
        )

    def list_session_exposures(self, *, session_id: str) -> dict[str, Any]:
        return self._session_bootstrap.list_session_exposures(
            session_id=session_id,
        )

    def apply_session_ratings(
        self,
        *,
        session_id: str,
        ratings: list[dict[str, str]],
    ) -> Any:
        return self._memory_rating.apply_session_ratings(
            session_id=session_id, ratings=ratings,
        )

    def credit_one(
        self,
        *,
        session_id: str,
        kind: str,
        id: str,
        classification: str,
    ) -> Any:
        return self._memory_rating.credit_one(
            session_id=session_id, kind=kind, id=id, classification=classification,
        )

    # ----- Synthesis -----

    def synthesize_next_get_context(self, *, project: str) -> Any:
        return self._synthesis.get_next_pending_context(project=project)

    def synthesize_next_apply(
        self,
        *,
        episode_id: str,
        response: Any,
        project: str,
    ) -> Any:
        return self._synthesis.apply_decision(
            episode_id=episode_id, response=response, project=project,
        )
