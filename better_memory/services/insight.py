"""Insight CRUD + source/relation service.

Insights are higher-level knowledge entries distilled from observations
(e.g. "always escape quoted arguments in shell subprocesses"). Each insight
carries a ``polarity`` — ``'do'``, ``'dont'``, or ``'neutral'`` — that
determines which retrieval bucket it surfaces from. Polarity is enforced
by the ``insights.polarity`` CHECK constraint (see ``0001_init.sql``) and
defensively validated at this service layer before the DB sees the value.

Phase 6 scope
-------------
This module supports *manual* CRUD for insights so tests and fixtures can
seed data. AI-driven consolidation — the process that actually creates
insights from clustered observations — ships in Plan 2, Phase 3. The MCP
layer intentionally does NOT expose an ``insight.create`` tool at this
phase.

Embedding is *optional*. ``create(..., embed=True)`` will use an injected
:class:`~better_memory.embeddings.ollama.OllamaEmbedder` to populate the
``insight_embeddings`` vec0 table; by default (``embed=False``) no vector
is produced. Without an embedder present, ``embed=True`` is a programming
error and raises :class:`RuntimeError`.

Connection ownership
--------------------
``InsightService`` assumes it owns the provided :class:`sqlite3.Connection`
and is free to call :meth:`~sqlite3.Connection.commit` and
:meth:`~sqlite3.Connection.rollback` on it. Callers must not share a
connection that already has an open transaction with other services: the
``commit()`` calls in write methods would otherwise steal commit authority
from the caller. Same contract as :class:`ObservationService`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

import sqlite_vec

from better_memory.embeddings.ollama import OllamaEmbedder
from better_memory.search.query import sanitize_fts5_query
from better_memory.services import audit

Polarity = Literal["do", "dont", "neutral"]
RelationType = Literal["related", "contradicts", "supersedes"]

_VALID_POLARITIES: frozenset[str] = frozenset({"do", "dont", "neutral"})
_VALID_RELATION_TYPES: frozenset[str] = frozenset(
    {"related", "contradicts", "supersedes"}
)


@dataclass(frozen=True)
class Insight:
    """A row from the ``insights`` table."""

    id: str
    title: str
    content: str
    project: str | None
    component: str | None
    status: str  # 'pending_review' | 'confirmed' | 'contradicted' | 'promoted' | 'retired'
    confidence: str  # 'low' | 'medium' | 'high'
    polarity: str  # 'do' | 'dont' | 'neutral'
    evidence_count: int
    last_validated: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class InsightSearchResult:
    """A single search hit with its bm25 rank (lower is better).

    When :meth:`InsightService.search` is called without a query, results
    are ordered by ``created_at`` descending and ``rank`` is set to
    ``0.0`` — callers should treat ``rank`` as meaningful only when a
    full-text query was supplied.
    """

    insight: Insight
    rank: float


def _default_clock() -> datetime:
    return datetime.now(UTC)


def row_to_insight(row: sqlite3.Row) -> Insight:
    return Insight(
        id=row["id"],
        title=row["title"],
        content=row["content"],
        project=row["project"],
        component=row["component"],
        status=row["status"],
        confidence=row["confidence"],
        polarity=row["polarity"],
        evidence_count=row["evidence_count"],
        last_validated=row["last_validated"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class InsightService:
    """Service for CRUD, source/relation wiring, and search of insights.

    See module docstring for the connection-ownership contract.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
        session_id: str | None = None,
        embedder: OllamaEmbedder | None = None,
    ) -> None:
        self._conn = conn
        self._clock: Callable[[], datetime] = clock or _default_clock
        self._session_id = session_id if session_id is not None else uuid4().hex
        self._embedder = embedder

    # ------------------------------------------------------------------ create
    async def create(
        self,
        *,
        title: str,
        content: str,
        project: str | None = None,
        component: str | None = None,
        status: str = "pending_review",
        confidence: str = "low",
        polarity: Polarity = "neutral",
        embed: bool = False,
    ) -> str:
        """Insert a new insight row; return its id.

        When ``embed=True`` a vector is computed from ``title`` + ``content``
        using the injected embedder and stored in ``insight_embeddings``.
        Otherwise no vector row is written — Phase 6 does not embed insights
        by default; consolidation (Plan 2) will.
        """
        if polarity not in _VALID_POLARITIES:
            raise ValueError(
                f"Invalid polarity: {polarity!r}; "
                f"expected one of {sorted(_VALID_POLARITIES)}"
            )

        insight_id = uuid4().hex

        # Compute the embedding BEFORE opening a write transaction so a slow
        # or broken embedder does not leave a pending SAVEPOINT.
        vec_blob: bytes | None = None
        if embed:
            if self._embedder is None:
                raise RuntimeError(
                    "InsightService.create(embed=True) requires an embedder; "
                    "none was provided to the constructor."
                )
            vector = await self._embedder.embed(f"{title}\n\n{content}")
            vec_blob = sqlite_vec.serialize_float32(vector)

        now = self._clock().isoformat()
        conn = self._conn
        conn.execute("SAVEPOINT insight_create")
        try:
            conn.execute(
                """
                INSERT INTO insights (
                    id, title, content, project, component, status,
                    confidence, polarity, evidence_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    insight_id,
                    title,
                    content,
                    project,
                    component,
                    status,
                    confidence,
                    polarity,
                    now,
                    now,
                ),
            )
            if vec_blob is not None:
                conn.execute(
                    "INSERT INTO insight_embeddings (insight_id, embedding) "
                    "VALUES (?, ?)",
                    (insight_id, vec_blob),
                )
            self._write_audit(
                entity_id=insight_id,
                action="created",
                detail={
                    "title": title,
                    "polarity": polarity,
                    "status": status,
                    "confidence": confidence,
                    "project": project,
                    "component": component,
                },
            )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT insight_create")
            conn.execute("RELEASE SAVEPOINT insight_create")
            raise
        else:
            conn.execute("RELEASE SAVEPOINT insight_create")

        conn.commit()
        return insight_id

    # --------------------------------------------------------------------- get
    def get(self, insight_id: str) -> Insight | None:
        row = self._conn.execute(
            "SELECT * FROM insights WHERE id = ?", (insight_id,)
        ).fetchone()
        if row is None:
            return None
        return row_to_insight(row)

    # ------------------------------------------------------------------ update
    def update(
        self,
        insight_id: str,
        *,
        title: str | None = None,
        content: str | None = None,
        project: str | None = None,
        component: str | None = None,
        status: str | None = None,
        confidence: str | None = None,
        polarity: Polarity | None = None,
        evidence_count: int | None = None,
        last_validated: str | None = None,
    ) -> None:
        """Update non-``None`` fields, bump ``updated_at``, audit.

        Writes an audit row with ``action='status_changed'`` if the status
        changed, otherwise ``action='updated'``. No-op (no audit row) when
        no fields are supplied.
        """
        if polarity is not None and polarity not in _VALID_POLARITIES:
            raise ValueError(
                f"Invalid polarity: {polarity!r}; "
                f"expected one of {sorted(_VALID_POLARITIES)}"
            )

        # Collect candidate updates preserving insertion order.
        candidate: dict[str, Any] = {}
        if title is not None:
            candidate["title"] = title
        if content is not None:
            candidate["content"] = content
        if project is not None:
            candidate["project"] = project
        if component is not None:
            candidate["component"] = component
        if status is not None:
            candidate["status"] = status
        if confidence is not None:
            candidate["confidence"] = confidence
        if polarity is not None:
            candidate["polarity"] = polarity
        if evidence_count is not None:
            candidate["evidence_count"] = evidence_count
        if last_validated is not None:
            candidate["last_validated"] = last_validated

        # Always verify the row exists — required whether or not there are
        # fields to set, so callers get a consistent "not found" error.
        existing = self._conn.execute(
            "SELECT status FROM insights WHERE id = ?", (insight_id,)
        ).fetchone()
        if existing is None:
            raise ValueError(f"Insight not found: {insight_id}")

        if not candidate:
            # Nothing to change — do not emit an audit row.
            return

        now = self._clock().isoformat()
        old_status = existing["status"]
        status_changed = "status" in candidate and candidate["status"] != old_status

        set_clauses = [f"{col} = ?" for col in candidate]
        set_clauses.append("updated_at = ?")
        params: list[Any] = list(candidate.values())
        params.append(now)
        params.append(insight_id)

        conn = self._conn
        conn.execute("SAVEPOINT insight_update")
        try:
            conn.execute(
                f"UPDATE insights SET {', '.join(set_clauses)} WHERE id = ?",
                params,
            )
            if status_changed:
                self._write_audit(
                    entity_id=insight_id,
                    action="status_changed",
                    detail={"fields": list(candidate.keys())},
                    from_status=old_status,
                    to_status=candidate["status"],
                )
            else:
                self._write_audit(
                    entity_id=insight_id,
                    action="updated",
                    detail={"fields": list(candidate.keys())},
                )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT insight_update")
            conn.execute("RELEASE SAVEPOINT insight_update")
            raise
        else:
            conn.execute("RELEASE SAVEPOINT insight_update")
        conn.commit()

    # ------------------------------------------------------------------ delete
    def delete(self, insight_id: str) -> None:
        """Hard delete insight + its sources/relations; audit ``retired``.

        Plan 2 introduces a soft-delete/promotion workflow; Phase 6 keeps
        this as a plain row removal, with Phase 10's audit trail preserving
        the lifecycle record.
        """
        existing = self._conn.execute(
            "SELECT title FROM insights WHERE id = ?", (insight_id,)
        ).fetchone()
        if existing is None:
            raise ValueError(f"Insight not found: {insight_id}")

        title = existing["title"]
        conn = self._conn
        conn.execute("SAVEPOINT insight_delete")
        try:
            conn.execute(
                "DELETE FROM insight_sources WHERE insight_id = ?", (insight_id,)
            )
            conn.execute(
                "DELETE FROM insight_relations "
                "WHERE from_insight_id = ? OR to_insight_id = ?",
                (insight_id, insight_id),
            )
            # The vec0 table is keyed by insight_id; remove any vector too.
            conn.execute(
                "DELETE FROM insight_embeddings WHERE insight_id = ?",
                (insight_id,),
            )
            conn.execute("DELETE FROM insights WHERE id = ?", (insight_id,))
            self._write_audit(
                entity_id=insight_id,
                action="retired",
                detail={"title": title},
            )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT insight_delete")
            conn.execute("RELEASE SAVEPOINT insight_delete")
            raise
        else:
            conn.execute("RELEASE SAVEPOINT insight_delete")
        conn.commit()

    # ------------------------------------------------------------- add_source
    def add_source(self, insight_id: str, observation_id: str) -> None:
        """Link an observation as evidence for an insight; bump evidence_count.

        Raises :class:`ValueError` if either the insight or the observation
        does not exist, or if this pair has already been linked.
        """
        # Validate foreign keys up-front so we can raise clean messages. The
        # schema also has FK constraints, but catching IntegrityError gives a
        # generic message that loses the "which side is missing" signal.
        insight_exists = (
            self._conn.execute(
                "SELECT 1 FROM insights WHERE id = ?", (insight_id,)
            ).fetchone()
            is not None
        )
        if not insight_exists:
            raise ValueError(f"Insight not found: {insight_id}")
        observation_exists = (
            self._conn.execute(
                "SELECT 1 FROM observations WHERE id = ?", (observation_id,)
            ).fetchone()
            is not None
        )
        if not observation_exists:
            raise ValueError(f"Observation not found: {observation_id}")

        now = self._clock().isoformat()
        conn = self._conn
        conn.execute("SAVEPOINT insight_add_source")
        try:
            try:
                conn.execute(
                    "INSERT INTO insight_sources (insight_id, observation_id) "
                    "VALUES (?, ?)",
                    (insight_id, observation_id),
                )
            except sqlite3.IntegrityError as exc:
                # Composite PK collision — pair already exists.
                raise ValueError(
                    f"Source already linked: insight={insight_id} "
                    f"observation={observation_id}"
                ) from exc
            conn.execute(
                "UPDATE insights SET evidence_count = evidence_count + 1, "
                "updated_at = ? WHERE id = ?",
                (now, insight_id),
            )
            self._write_audit(
                entity_id=insight_id,
                action="evidence_added",
                detail={"observation_id": observation_id},
            )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT insight_add_source")
            conn.execute("RELEASE SAVEPOINT insight_add_source")
            raise
        else:
            conn.execute("RELEASE SAVEPOINT insight_add_source")
        conn.commit()

    # ----------------------------------------------------------- add_relation
    def add_relation(
        self,
        from_insight_id: str,
        to_insight_id: str,
        relation_type: RelationType,
    ) -> None:
        """Link two insights with a typed relation; audit on the ``from`` id.

        The migration does not constrain ``relation_type`` at the DB layer —
        enforce it here. Self-relations are rejected as a semantic error.
        """
        if relation_type not in _VALID_RELATION_TYPES:
            raise ValueError(
                f"Invalid relation_type: {relation_type!r}; "
                f"expected one of {sorted(_VALID_RELATION_TYPES)}"
            )
        if from_insight_id == to_insight_id:
            raise ValueError(
                f"Self-relation is not allowed (from == to == {from_insight_id})"
            )

        # Validate both ends exist so callers see a clean error, not an FK
        # integrity trap.
        for label, iid in (
            ("from", from_insight_id),
            ("to", to_insight_id),
        ):
            exists = (
                self._conn.execute(
                    "SELECT 1 FROM insights WHERE id = ?", (iid,)
                ).fetchone()
                is not None
            )
            if not exists:
                raise ValueError(f"Insight not found ({label}): {iid}")

        conn = self._conn
        conn.execute("SAVEPOINT insight_add_relation")
        try:
            try:
                conn.execute(
                    "INSERT INTO insight_relations "
                    "(from_insight_id, to_insight_id, relation_type) "
                    "VALUES (?, ?, ?)",
                    (from_insight_id, to_insight_id, relation_type),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"Relation already exists: {from_insight_id} -> "
                    f"{to_insight_id}"
                ) from exc
            self._write_audit(
                entity_id=from_insight_id,
                action="relation_added",
                detail={
                    "to_insight_id": to_insight_id,
                    "relation_type": relation_type,
                },
            )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT insight_add_relation")
            conn.execute("RELEASE SAVEPOINT insight_add_relation")
            raise
        else:
            conn.execute("RELEASE SAVEPOINT insight_add_relation")
        conn.commit()

    # ------------------------------------------------------------------ search
    def search(
        self,
        query: str | None = None,
        *,
        polarity: Polarity | None = None,
        project: str | None = None,
        component: str | None = None,
        status: str | None = None,
        limit: int = 10,
    ) -> list[InsightSearchResult]:
        """FTS5 MATCH search (bm25-ordered) or browsing by ``created_at``.

        When ``query`` is ``None`` or empty, returns rows ordered by
        ``created_at`` descending with ``rank=0.0``. When ``query`` is given,
        uses the ``insight_fts`` virtual table and orders by bm25 ascending
        (lower is better).
        """
        if polarity is not None and polarity not in _VALID_POLARITIES:
            raise ValueError(
                f"Invalid polarity: {polarity!r}; "
                f"expected one of {sorted(_VALID_POLARITIES)}"
            )

        params: list[Any] = []
        filter_clauses: list[str] = []
        if polarity is not None:
            filter_clauses.append("i.polarity = ?")
            params.append(polarity)
        if project is not None:
            filter_clauses.append("i.project = ?")
            params.append(project)
        if component is not None:
            filter_clauses.append("i.component = ?")
            params.append(component)
        if status is not None:
            filter_clauses.append("i.status = ?")
            params.append(status)

        if query is not None and query.strip():
            # Sanitise before MATCH: raw user text containing FTS5 operator
            # chars (``-``, ``:``, ``"``) would otherwise crash the query
            # (e.g. ``better-memory`` → ``no such column: memory``).
            sanitized = sanitize_fts5_query(query)
            if not sanitized:
                return []

            sql = (
                "SELECT i.*, bm25(insight_fts) AS rank "
                "FROM insight_fts "
                "JOIN insights i ON i.rowid = insight_fts.rowid "
                "WHERE insight_fts MATCH ? "
            )
            # Prepend the MATCH param before the filter params.
            match_params: list[Any] = [sanitized]
            if filter_clauses:
                sql += "AND " + " AND ".join(filter_clauses) + " "
            sql += "ORDER BY rank LIMIT ?"
            final_params = match_params + params + [limit]
            try:
                rows = self._conn.execute(sql, final_params).fetchall()
            except sqlite3.OperationalError:
                # Safety net for any FTS5 operator the sanitiser missed.
                return []
            return [
                InsightSearchResult(
                    insight=row_to_insight(row), rank=row["rank"]
                )
                for row in rows
            ]

        # No query — browse mode.
        sql = "SELECT i.* FROM insights i"
        if filter_clauses:
            sql += " WHERE " + " AND ".join(filter_clauses)
        sql += " ORDER BY i.created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            InsightSearchResult(insight=row_to_insight(row), rank=0.0)
            for row in rows
        ]

    # ----------------------------------------------------------------- helpers
    def _write_audit(
        self,
        *,
        entity_id: str,
        action: str,
        detail: dict[str, Any],
        from_status: str | None = None,
        to_status: str | None = None,
    ) -> None:
        """Insert an insight audit row via :func:`audit.log`.

        Thin adapter that fills in the insight-service defaults
        (``entity_type``, ``actor``, ``session_id``) so call sites stay
        concise. ``audit_log.created_at`` is populated by the schema's
        ``DEFAULT CURRENT_TIMESTAMP`` — callers should not override it.
        """
        audit.log(
            self._conn,
            entity_type="insight",
            entity_id=entity_id,
            action=action,
            actor="ai",
            from_status=from_status,
            to_status=to_status,
            session_id=self._session_id,
            detail=detail,
        )
