"""SqliteBackend — wraps existing services to satisfy the StorageBackend Protocol.

Behaviour-preserving wrapper. Existing service tests continue to exercise
service business logic directly; tests in tests/storage/ only verify the
protocol delegation surface.

Held state:
- ``memory_conn`` — open sqlite3 Connection
- ``embedder`` — forwarded to ObservationService. May be ``None`` for the
  sqlite (FTS5) embeddings backend, which indexes via DB triggers instead
  of a Python embedder.
- ``sync_embedder`` — caller-owned ``SyncEmbedder`` forwarded to
  ``SemanticMemoryService`` / ``ReflectionSynthesisService`` as-is. The
  backend does NOT construct its own — it must be the same process-wide
  instance the caller (e.g. ``mcp/server.py``) built, so its circuit
  breaker state is shared across the write-path tools and this backend's
  ``retrieve``/``semantic`` methods rather than split into two breakers.
- ``session_id`` — used for episode lookups, ratings, exposures. ``None``
  means "defer resolution to env-var fallback at first write"
  (ObservationService re-resolves from ``CLAUDE_SESSION_ID`` /
  ``CLAUDE_CODE_SESSION_ID`` / marker file when ``session_id is None``)
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
        embedder: Any = None,
        sync_embedder: Any = None,
        session_id: str | None,
        project: str,
    ) -> None:
        self._conn = memory_conn
        self._embedder = embedder
        self._sync_embedder = sync_embedder
        self._session_id: str | None = session_id
        self._project = project
        self._project_resolver = lambda: self._project
        self._episodes = EpisodeService(memory_conn)
        self._observations = ObservationService(
            memory_conn,
            embedder=embedder,
            session_id=session_id,
            project_resolver=self._project_resolver,
            episodes=self._episodes,
        )
        self._reflection = ReflectionService(memory_conn)
        self._memory_rating = MemoryRatingService(memory_conn)
        self._session_bootstrap = SessionBootstrapService(memory_conn)
        self._semantic = SemanticMemoryService(memory_conn, sync_embedder=sync_embedder)
        self._synthesis = ReflectionSynthesisService(
            memory_conn, sync_embedder=sync_embedder
        )

    # ----- Capability flags -----

    @property
    def supports_synthesis(self) -> bool:
        return True

    @property
    def supports_episodes(self) -> bool:
        return True

    @property
    def supports_observations(self) -> bool:
        return True

    @property
    def supports_provenance(self) -> bool:
        return True

    @property
    def supports_retention_runs(self) -> bool:
        return True

    @property
    def supports_reflection_review(self) -> bool:
        return True

    @property
    def supports_reflection_text_edit(self) -> bool:
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

    def retrieve(
        self,
        *,
        project: str | None = None,
        tech: str | None = None,
        phase: str | None = None,
        polarity: str | None = None,
        limit_per_bucket: int | None = 20,
        track_exposure: bool = True,
        query: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        return self._synthesis.retrieve_reflections(
            project=project or self._project,
            tech=tech,
            phase=phase,
            polarity=polarity,
            limit_per_bucket=limit_per_bucket,
            track_exposure=track_exposure,
            query=query,
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

    def semantic_get(self, *, id: str) -> Any | None:
        return self._semantic.get(id=id)

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

    # ----- Contextual relevance -----

    def relevance_ranks(
        self,
        *,
        query: str,
        kinds: tuple[str, ...] = ("reflection", "semantic"),
        top_k: int = 50,
    ) -> dict[tuple[str, str], int]:
        """Thin protocol-completeness wrapper over the existing BM25
        (``reflection_fts``) + vector legs (``services/relevant.py``'s
        ``_bm25_qualifiers`` / ``_vec_qualifiers``), RRF-merged per kind.
        Always returns a dict, never ``None`` -- its underlying legs
        already degrade internally (a missing conn, no query vector, or a
        malformed FTS query all resolve to empty results, not an error) so
        there is no failure mode to signal here (see the Protocol
        docstring's ``None`` vs ``{}`` contract, which only agentcore's
        AWS-error path actually exercises).

        NOT consumed by ``retrieve_relevant``'s own sqlite path -- that
        function keeps calling those helpers directly against its own
        ``conn`` parameter (see its ``agentcore_mode`` gate, which is
        False for any backend reporting ``supports_synthesis=True``), so
        this method's existence changes zero sqlite contextual-gate
        behavior. It exists purely so SqliteBackend satisfies the full
        ``StorageBackend.relevance_ranks`` contract.
        """
        if not (query or "").strip():
            return {}
        # Local import: services/relevant.py is the contextual-gate module;
        # importing its private ranking helpers here (rather than
        # duplicating the BM25/vec SQL) keeps the two legs byte-identical
        # without creating a module-load-time cycle (relevant.py itself
        # never imports storage).
        from better_memory.services.relevant import (
            _RRF_K,
            _bm25_qualifiers,
            _vec_qualifiers,
        )

        q = query.strip()
        qvec = (
            self._sync_embedder.embed_text(q)
            if self._sync_embedder is not None else None
        )

        def _rrf_merge(rank_maps: list[dict[str, int]]) -> dict[str, int]:
            scores: dict[str, float] = {}
            for rm in rank_maps:
                for rid, rank in rm.items():
                    scores[rid] = scores.get(rid, 0.0) + 1.0 / (_RRF_K + rank)
            ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
            return {rid: i for i, (rid, _) in enumerate(ordered[:top_k])}

        out: dict[tuple[str, str], int] = {}
        if "reflection" in kinds:
            bm = _bm25_qualifiers(self._conn, q)
            vec_r = _vec_qualifiers(
                self._conn, "reflection_embeddings", "reflection_id", qvec, 0.55,
            )
            for rid, rank in _rrf_merge([bm, vec_r]).items():
                out[("reflection", rid)] = rank
        if "semantic" in kinds:
            vec_s = _vec_qualifiers(
                self._conn, "semantic_embeddings", "memory_id", qvec, 0.55,
            )
            for rid, rank in _rrf_merge([vec_s]).items():
                out[("semantic", rid)] = rank
        return out

    # ----- Reflection lifecycle -----

    def promote_reflection(self, *, reflection_id: str) -> None:
        self._reflection.promote_to_general(reflection_id=reflection_id)

    def retire_reflection(self, *, reflection_id: str) -> None:
        self._reflection.retire(reflection_id=reflection_id)

    def reflection_get(self, *, reflection_id: str) -> dict[str, Any] | None:
        from dataclasses import asdict

        from better_memory.ui import queries

        row = queries.reflection_row(self._conn, reflection_id=reflection_id)
        return None if row is None else asdict(row)

    def reflection_list(
        self,
        *,
        project: str | None = None,
        tech: str | None = None,
        phase: str | None = None,
        polarity: str | None = None,
        status: str | None = None,
        min_confidence: float = 0.0,
        useful_only: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        from dataclasses import asdict

        from better_memory.ui import queries

        rows = queries.reflection_list_for_ui(
            self._conn, project=project or self._project, tech=tech, phase=phase,
            polarity=polarity, status=status, min_confidence=min_confidence,
            useful_only=useful_only, limit=limit,
        )
        return [asdict(r) for r in rows]

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

    def record_exposures(
        self,
        *,
        session_id: str,
        items: list[tuple[str, str]],
        source: str,
    ) -> None:
        self._session_bootstrap.record_exposures(
            session_id=session_id, items=items, source=source,
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
        evidence: str | None = None,
    ) -> Any:
        return self._memory_rating.credit_one(
            session_id=session_id, kind=kind, id=id, classification=classification,
            evidence=evidence,
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
