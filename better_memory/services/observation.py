"""Observation write-path service.

Handles creation of episodic ``observations`` rows plus the supporting
``observation_embeddings`` vector row and ``audit_log`` entry, and records
re-use / outcome signals that move ``reinforcement_score`` up (success) or
down (failure).

Retrieval lives in later phases.

Transactional behaviour
-----------------------
The SQLite connection uses Python's default deferred-transaction mode. A call
to :meth:`ObservationService.create`:

    1. Resolves the active episode via the injected :class:`EpisodeService`,
       opening a background episode if none is active. This call commits on
       success (background episode creation is its own transaction). See the
       caveat below.
    2. Calls the embedder. A slow / broken Ollama server causes this step to
       fail; steps 3+ do not run.
    3. Opens a SAVEPOINT, inserts the observation (AI trigger populates the
       FTS content-linked virtual table), inserts the embedding into the
       ``vec0`` table, and writes an audit row. All four statements succeed
       together or the SAVEPOINT rolls them all back.
    4. Commits the transaction.

Fail-fast caveat
----------------
If the embedder fails in step 2, a background episode may have been
committed in step 1 and left with zero observations attached. Subsequent
successful ``create`` calls on the same session will reuse that background
episode (via ``active_episode``), so the stranding is bounded to "one
orphan background episode per session that hit embed failure before any
successful write". Phase 2 accepts this trade-off; a future refactor of
``EpisodeService.open_background`` to support an "in-savepoint" mode
could restore the stricter guarantee.

If the SAVEPOINT is rolled back on error, the FTS trigger's side-effects are
undone along with the base-table row because SQLite FTS5 triggers participate
in the enclosing transaction.

Notes on the design spec
------------------------
The task brief originally said ``create`` should insert into
``observation_fts``. The shipped schema in ``0001_init.sql`` uses a
``content='observations'`` FTS5 external-content table with AFTER INSERT /
UPDATE / DELETE triggers that mirror writes automatically. Therefore
*no* direct insert into ``observation_fts`` is required — the trigger
handles it. The ``vec0`` ``observation_embeddings`` table, on the other
hand, is NOT trigger-populated and must be written manually.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import sqlite_vec

from better_memory.config import get_config
from better_memory.search.hybrid import (
    SearchFilters,
    SearchResult,
    hybrid_search,
)
from better_memory.search.query import sanitize_fts5_query
from better_memory.services import audit
from better_memory.services.episode import EpisodeService

Outcome = Literal["success", "failure", "neutral"]
UseOutcome = Literal["success", "failure"]


@dataclass(frozen=True)
class BucketedResults:
    """Outcome-filtered buckets returned by :meth:`ObservationService.retrieve`."""

    do: list[SearchResult]
    dont: list[SearchResult]
    neutral: list[SearchResult]


def _default_clock() -> datetime:
    """UTC-aware ``now``. Kept as a module-level function for clarity."""
    return datetime.now(UTC)


class ObservationService:
    """Service for creating observations and recording their reinforcement.

    Connection ownership
    --------------------
    ``ObservationService`` assumes it owns the provided :class:`sqlite3.Connection`
    and is free to call :meth:`~sqlite3.Connection.commit` and
    :meth:`~sqlite3.Connection.rollback` on it. Callers must not share a
    connection that already has an open transaction with other services: the
    ``commit()`` in :meth:`create` and the ``rollback()`` in
    :meth:`record_use` (unknown id path) would otherwise steal commit/rollback
    authority from the caller and either commit uncommitted work or discard it.
    This contract may be revisited when higher-level orchestration lands in a
    later phase.

    An injected :class:`EpisodeService` is expected to share the same
    connection as this service and use its own SAVEPOINT+commit envelope
    for episode writes (its ``open_background``, ``start_foreground``, and
    ``close_active`` methods all commit before returning).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        embedder: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        project_resolver: Callable[[], str] | None = None,
        scope_resolver: Callable[[], str | None] | None = None,
        session_id: str | None = None,
        audit_log_retrieved: bool | None = None,
        episodes: EpisodeService | None = None,
    ) -> None:
        self._conn = conn
        self._embedder = embedder
        self._clock: Callable[[], datetime] = clock or _default_clock
        self._project_resolver: Callable[[], str] = (
            project_resolver if project_resolver is not None else (lambda: Path.cwd().name)
        )
        self._scope_resolver: Callable[[], str | None] = (
            scope_resolver if scope_resolver is not None else (lambda: None)
        )
        # Resolution order: explicit kwarg > CLAUDE_SESSION_ID env var > uuid4().
        # The env var makes hook-written events (which read CLAUDE_SESSION_ID)
        # and MCP-written observations share the same session id.
        if session_id is not None:
            self._session_id = session_id
        else:
            self._session_id = (
                os.environ.get("CLAUDE_SESSION_ID") or uuid4().hex
            )
        # ``None`` defers to the resolved config value so tests can inject
        # ``False`` without having to monkeypatch the environment.
        self._audit_log_retrieved: bool = (
            audit_log_retrieved
            if audit_log_retrieved is not None
            else get_config().audit_log_retrieved
        )
        self._episodes = episodes

    # ------------------------------------------------------------------ public
    async def create(
        self,
        content: str,
        *,
        component: str | None = None,
        theme: str | None = None,
        trigger_type: str | None = None,
        outcome: Outcome = "neutral",
        scope_path: str | None = None,
        project: str | None = None,
        tech: str | None = None,
    ) -> str:
        """Insert a new observation, embedding and audit row; return its id."""
        obs_id = uuid4().hex

        resolved_project = project if project is not None else self._project_resolver()
        resolved_scope = scope_path if scope_path is not None else self._scope_resolver()
        tech_normalised = tech.lower() if tech else None

        # Resolve episode_id. ObservationService requires an EpisodeService
        # now that episode_id is NOT NULL on observations (Phase 1 schema).
        if self._episodes is None:
            raise RuntimeError(
                "ObservationService.create requires an EpisodeService "
                "(episodes=...). Wire one at construction time."
            )
        active = self._episodes.active_episode(self._session_id)
        if active is None:
            episode_id = self._episodes.open_background(
                session_id=self._session_id,
                project=resolved_project,
            )
        else:
            episode_id = active.id

        # Compute the embedding BEFORE opening the observation SAVEPOINT so a
        # slow / broken Ollama server does not hold an open SAVEPOINT. Note
        # that the episode lookup / background-open above has already
        # committed if a new background was created — see module docstring.
        vector = await self._embedder.embed(content)
        vec_blob = sqlite_vec.serialize_float32(vector)

        now = self._clock().isoformat()

        conn = self._conn
        conn.execute("SAVEPOINT observation_create")
        try:
            conn.execute(
                """
                INSERT INTO observations (
                    id, content, project, component, theme, session_id,
                    trigger_type, outcome, reinforcement_score, scope_path,
                    created_at, status_changed_at, episode_id, tech
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?, ?, ?, ?, ?)
                """,
                (
                    obs_id,
                    content,
                    resolved_project,
                    component,
                    theme,
                    self._session_id,
                    trigger_type,
                    outcome,
                    resolved_scope,
                    now,
                    now,
                    episode_id,
                    tech_normalised,
                ),
            )

            conn.execute(
                "INSERT INTO observation_embeddings (observation_id, embedding) "
                "VALUES (?, ?)",
                (obs_id, vec_blob),
            )

            self._write_audit(
                entity_id=obs_id,
                action="created",
                detail={
                    "outcome": outcome,
                    "scope_path": resolved_scope,
                    "component": component,
                    "episode_id": episode_id,
                    "tech": tech_normalised,
                },
            )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT observation_create")
            conn.execute("RELEASE SAVEPOINT observation_create")
            raise
        else:
            conn.execute("RELEASE SAVEPOINT observation_create")

        # Service owns the connection — see class docstring for the contract.
        conn.commit()
        return obs_id

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
    ) -> BucketedResults:
        """Run three outcome-filtered hybrid searches and return them bucketed.

        The query is embedded *once* and the vector is reused for all three
        sub-searches (success / failure / neutral), saving two embedder
        round-trips per retrieve.
        """
        resolved_project = project if project is not None else self._project_resolver()

        # Embed the query once, if any, so vector search is available to every
        # bucket without paying three embed calls.
        query_vector: list[float] | None = None
        if query is not None and query.strip():
            query_vector = await self._embedder.embed(query)

        # Sanitise before FTS5 MATCH: user queries like ``better-memory retrieval``
        # would otherwise be parsed by FTS5 as ``-memory`` column-exclusion and
        # resolve to [] (via the safety net in hybrid._fts_candidates), yielding
        # zero hits for any hyphenated term. The embedder still receives the
        # raw query — operator chars don't affect semantic similarity.
        fts_query_text = (
            sanitize_fts5_query(query) if query is not None else None
        ) or None

        base_kwargs: dict[str, Any] = {
            "project": resolved_project,
            "component": component,
            "status": status,
            "window_days": window_days,
            "scope_path": scope_path,
        }

        def _run(outcome: Outcome, limit: int) -> list[SearchResult]:
            filters = SearchFilters(outcome=outcome, **base_kwargs)
            return hybrid_search(
                self._conn,
                query_text=fts_query_text,
                query_vector=query_vector,
                filters=filters,
                limit=limit,
                candidate_k=candidate_k,
                reinforcement_alpha=reinforcement_alpha,
                clock=self._clock,
            )

        do_hits = _run("success", do_limit)
        dont_hits = _run("failure", dont_limit)
        neutral_hits = _run("neutral", neutral_limit)

        if self._audit_log_retrieved:
            self._record_retrieval(
                do=do_hits, dont=dont_hits, neutral=neutral_hits
            )

        return BucketedResults(do=do_hits, dont=dont_hits, neutral=neutral_hits)

    # ---------------------------------------------------------- retrieval audit
    def _record_retrieval(
        self,
        *,
        do: list[SearchResult],
        dont: list[SearchResult],
        neutral: list[SearchResult],
    ) -> None:
        """Bump ``retrieved_count`` and write one audit row per returned result.

        Gated by ``AUDIT_LOG_RETRIEVED`` — when disabled the whole path is
        skipped (no counter bump, no audit row). Run as a single batch so
        the counters and audit rows land atomically for the caller's
        retrieve call.
        """
        now = self._clock().isoformat()
        conn = self._conn

        buckets: tuple[tuple[str, list[SearchResult]], ...] = (
            ("do", do),
            ("dont", dont),
            ("neutral", neutral),
        )

        conn.execute("SAVEPOINT observation_retrieve_audit")
        try:
            for bucket_name, hits in buckets:
                for hit in hits:
                    conn.execute(
                        """
                        UPDATE observations
                           SET retrieved_count = retrieved_count + 1,
                               last_retrieved = ?
                         WHERE id = ?
                        """,
                        (now, hit.id),
                    )
                    self._write_audit(
                        entity_id=hit.id,
                        action="retrieved",
                        detail={
                            "outcome": hit.outcome,
                            "final_score": hit.final_score,
                            "bucket": bucket_name,
                        },
                    )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT observation_retrieve_audit")
            conn.execute("RELEASE SAVEPOINT observation_retrieve_audit")
            raise
        else:
            conn.execute("RELEASE SAVEPOINT observation_retrieve_audit")
        conn.commit()

    def record_use(
        self,
        observation_id: str,
        *,
        outcome: UseOutcome | None = None,
    ) -> None:
        """Bump ``used_count`` (and validation counters on outcome)."""
        now = self._clock().isoformat()
        conn = self._conn

        if outcome == "success":
            cursor = conn.execute(
                """
                UPDATE observations
                   SET used_count = used_count + 1,
                       last_used = ?,
                       validated_true = validated_true + 1,
                       reinforcement_score = reinforcement_score + 1.0,
                       last_validated = ?
                 WHERE id = ?
                """,
                (now, now, observation_id),
            )
        elif outcome == "failure":
            cursor = conn.execute(
                """
                UPDATE observations
                   SET used_count = used_count + 1,
                       last_used = ?,
                       validated_false = validated_false + 1,
                       reinforcement_score = reinforcement_score - 1.0,
                       last_validated = ?
                 WHERE id = ?
                """,
                (now, now, observation_id),
            )
        elif outcome is None:
            cursor = conn.execute(
                """
                UPDATE observations
                   SET used_count = used_count + 1,
                       last_used = ?
                 WHERE id = ?
                """,
                (now, observation_id),
            )
        else:  # defensive — typed as Literal so normally unreachable
            raise ValueError(f"Invalid outcome: {outcome!r}")

        if cursor.rowcount == 0:
            # No row updated — reject to give callers a clear error.
            # Service owns the connection — see class docstring for the contract.
            conn.rollback()
            raise ValueError(f"Observation not found: {observation_id}")

        self._write_audit(
            entity_id=observation_id,
            action="used",
            detail={"outcome": outcome},
        )
        conn.commit()

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
        """Phase 6 read-side API for ``memory.retrieve_observations``.

        Two modes:

        Filter-only mode (``query`` is None or empty):
        - Simple SQL filter on ``project``, ``episode_id``, ``component``,
          ``theme``, ``outcome``.
        - Order: ``created_at DESC, rowid DESC``. Cap at ``limit``.

        Query mode (``query`` is given):
        - Embed the query and route through :func:`hybrid_search`. FTS5 +
          sqlite-vec results, RRF-fused, ranked by relevance.
        - The simple filters that ``SearchFilters`` natively supports
          (``project``, ``component``, ``outcome``) are honoured; ``status``
          and ``window_days`` are disabled (drill-down should see all
          statuses, no time cap).
        - **Limitation:** ``episode_id`` and ``theme`` are NOT honoured in
          query mode (they are not in :class:`SearchFilters`). Pass them
          without ``query`` for those drill-down lookups.
        """
        resolved_project = project if project is not None else self._project_resolver()

        if query is not None and query.strip():
            return await self._list_observations_via_hybrid_search(
                project=resolved_project,
                component=component,
                outcome=outcome,
                query=query,
                limit=limit,
            )
        return self._list_observations_via_filter(
            project=resolved_project,
            episode_id=episode_id,
            component=component,
            theme=theme,
            outcome=outcome,
            limit=limit,
        )

    def _list_observations_via_filter(
        self,
        *,
        project: str,
        episode_id: str | None,
        component: str | None,
        theme: str | None,
        outcome: Outcome | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        clauses = ["project = ?"]
        params: list[Any] = [project]
        if episode_id is not None:
            clauses.append("episode_id = ?")
            params.append(episode_id)
        if component is not None:
            clauses.append("component = ?")
            params.append(component)
        if theme is not None:
            clauses.append("theme = ?")
            params.append(theme)
        if outcome is not None:
            clauses.append("outcome = ?")
            params.append(outcome)
        where = " AND ".join(clauses)
        sql = (
            "SELECT id, content, component, theme, outcome, "
            "reinforcement_score, created_at FROM observations "
            f"WHERE {where} "
            "ORDER BY created_at DESC, rowid DESC "
            "LIMIT ?"
        )
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "id": r["id"],
                "content": r["content"],
                "component": r["component"],
                "theme": r["theme"],
                "outcome": r["outcome"],
                "reinforcement_score": r["reinforcement_score"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    async def _list_observations_via_hybrid_search(
        self,
        *,
        project: str,
        component: str | None,
        outcome: Outcome | None,
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        # Embed the query; reuse the same embedder used for writes.
        vector = await self._embedder.embed(query)
        fts_query_text = sanitize_fts5_query(query) or None

        # Drill-down should see ALL statuses and have no time cap.
        filters = SearchFilters(
            project=project,
            component=component,
            outcome=outcome,
            status=None,
            window_days=None,
        )
        results = hybrid_search(
            self._conn,
            query_text=fts_query_text,
            query_vector=vector,
            filters=filters,
            limit=limit,
            clock=self._clock,
        )
        return [
            {
                "id": r.id,
                "content": r.content,
                "component": r.component,
                "theme": r.theme,
                "outcome": r.outcome,
                "reinforcement_score": r.reinforcement_score,
                "created_at": r.created_at,
            }
            for r in results
        ]

    # ----------------------------------------------------------------- helpers
    def _write_audit(
        self,
        *,
        entity_id: str,
        action: str,
        detail: dict[str, Any],
    ) -> None:
        """Insert an observation audit row via :func:`audit.log`.

        Thin adapter that fills in the observation-service defaults
        (``entity_type``, ``actor``, ``session_id``) so call sites stay
        concise. ``audit_log.created_at`` is populated by the schema's
        ``DEFAULT CURRENT_TIMESTAMP`` — callers should not override it.
        """
        audit.log(
            self._conn,
            entity_type="observation",
            entity_id=entity_id,
            action=action,
            actor="ai",
            session_id=self._session_id,
            detail=detail,
        )
