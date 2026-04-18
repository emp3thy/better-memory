"""Hybrid search over observations (FTS5 BM25 + sqlite-vec kNN + RRF fusion).

This module is deliberately **pure SQLite**: it never calls the embedder.
Callers that want semantic search must supply a ``query_vector`` themselves.
This keeps the layer unit-testable without mocking Ollama and lets the caller
reuse one embedding across multiple search calls (e.g. three bucket searches
per retrieve).

Algorithm
---------
1. Build a base ``WHERE`` clause from :class:`SearchFilters`.
2. Run FTS5 BM25 top-K (if ``query_text`` supplied) and sqlite-vec kNN top-K
   (if ``query_vector`` supplied). Both are cheap serial queries — the "parallel"
   in the plan means "independently scored", not concurrent execution.
3. Fuse candidate lists with Reciprocal Rank Fusion:
   ``score(d) = Σ 1 / (rrf_k + rank_source(d))`` across sources.
4. Multiply by the reinforcement multiplier
   ``(1 + α · reinforcement_score)`` so positively reinforced rows rise.
5. Apply exponential recency decay with a 14-day half-life (configurable).
6. Sort descending, truncate to ``limit``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import sqlite_vec

Outcome = Literal["success", "failure", "neutral"]


@dataclass(frozen=True)
class SearchFilters:
    """Structural filters applied to every sub-query."""

    project: str | None = None
    component: str | None = None
    status: str | None = "active"
    window_days: int | None = 30
    scope_path: str | None = None
    outcome: Outcome | None = None


@dataclass(frozen=True)
class SearchResult:
    """A single hybrid-search hit."""

    id: str
    content: str
    component: str | None
    theme: str | None
    outcome: str
    reinforcement_score: float
    created_at: str
    final_score: float


@dataclass
class _Candidate:
    """Internal scratchpad used during RRF fusion."""

    row: sqlite3.Row
    rrf_score: float = 0.0
    ranks: dict[str, int] = field(default_factory=dict)


# Module-level singleton so the function signature doesn't call SearchFilters()
# on every import (ruff B008). It's a frozen dataclass so sharing is safe.
_DEFAULT_FILTERS = SearchFilters()


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------


def hybrid_search(
    conn: sqlite3.Connection,
    *,
    query_text: str | None = None,
    query_vector: list[float] | None = None,
    filters: SearchFilters = _DEFAULT_FILTERS,
    limit: int = 10,
    candidate_k: int = 50,
    rrf_k: int = 60,
    reinforcement_alpha: float = 0.1,
    recency_half_life_days: float = 14.0,
    clock: Callable[[], datetime] | None = None,
) -> list[SearchResult]:
    """Run hybrid search and return the top ``limit`` results."""
    if query_text is None and query_vector is None:
        return []

    now = (clock or _default_clock)()
    where_sql, where_params = _build_where(filters, now=now)

    # Gather candidate rowids from each active source.
    fts_ids: list[str] = []
    vec_ids: list[str] = []

    if query_text is not None and query_text.strip():
        fts_ids = _fts_candidates(
            conn,
            query_text=query_text,
            where_sql=where_sql,
            where_params=where_params,
            candidate_k=candidate_k,
        )
    if query_vector is not None:
        vec_ids = _vec_candidates(
            conn,
            query_vector=query_vector,
            where_sql=where_sql,
            where_params=where_params,
            candidate_k=candidate_k,
        )

    if not fts_ids and not vec_ids:
        return []

    # Fuse with Reciprocal Rank Fusion.
    candidates: dict[str, _Candidate] = {}
    _add_rrf_ranks(candidates, fts_ids, source="fts", rrf_k=rrf_k)
    _add_rrf_ranks(candidates, vec_ids, source="vec", rrf_k=rrf_k)

    if not candidates:
        return []

    # Hydrate rows (single SELECT with an IN list) so the caller only pays one
    # additional round-trip to SQLite.
    rows = _fetch_rows(conn, list(candidates.keys()))
    for row in rows:
        candidates[row["id"]].row = row

    results = [
        _finalize(c, now=now, alpha=reinforcement_alpha, half_life=recency_half_life_days)
        for c in candidates.values()
        if c.row is not None  # defensive; always True after hydrate
    ]

    # Secondary sort key on id keeps ordering stable for identical scores.
    results.sort(key=lambda r: (-r.final_score, r.id))
    return results[:limit]


# ---------------------------------------------------------------------------
# Filter building
# ---------------------------------------------------------------------------


def _build_where(filters: SearchFilters, *, now: datetime) -> tuple[str, list[Any]]:
    """Return a SQL snippet like ``"o.project = ? AND ..."`` and its params.

    ``now`` is the injected clock's current time (already tz-aware UTC); we use
    it to compute the window cutoff so tests with a ``fixed_clock`` are
    deterministic and don't depend on SQLite's wall clock.
    """
    clauses: list[str] = []
    params: list[Any] = []

    if filters.project is not None:
        clauses.append("o.project = ?")
        params.append(filters.project)
    if filters.component is not None:
        clauses.append("o.component = ?")
        params.append(filters.component)
    if filters.status is not None:
        clauses.append("o.status = ?")
        params.append(filters.status)
    if filters.scope_path is not None:
        clauses.append("o.scope_path = ?")
        params.append(filters.scope_path)
    if filters.outcome is not None:
        clauses.append("o.outcome = ?")
        params.append(filters.outcome)
    if filters.window_days is not None:
        cutoff = now - timedelta(days=int(filters.window_days))
        # Store tz-naive ISO so string comparison matches the format produced by
        # ObservationService.create() (which uses datetime.isoformat() with tz).
        # SQLite's string comparison is lexicographic on ISO-8601, so both forms
        # sort correctly against one another.
        clauses.append("o.created_at >= ?")
        params.append(cutoff.isoformat())

    return (" AND ".join(clauses), params)


# ---------------------------------------------------------------------------
# Source queries
# ---------------------------------------------------------------------------


def _fts_candidates(
    conn: sqlite3.Connection,
    *,
    query_text: str,
    where_sql: str,
    where_params: list[Any],
    candidate_k: int,
) -> list[str]:
    """Return observation ids ordered by BM25 (best first), honouring filters."""
    sql = (
        "SELECT o.id AS id, bm25(observation_fts) AS bm "
        "FROM observation_fts "
        "JOIN observations o ON o.rowid = observation_fts.rowid "
        "WHERE observation_fts MATCH ?"
    )
    params: list[Any] = [query_text]
    if where_sql:
        sql += " AND " + where_sql
        params.extend(where_params)
    # BM25 in SQLite's FTS5 returns negative numbers where *lower* is better
    # (i.e. more negative = better match). ASC sort puts best matches first.
    sql += " ORDER BY bm ASC LIMIT ?"
    params.append(candidate_k)

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        # Malformed user query text (e.g. unbalanced quotes in FTS syntax):
        # treat as no matches rather than propagating.
        return []
    return [r["id"] for r in rows]


def _vec_candidates(
    conn: sqlite3.Connection,
    *,
    query_vector: list[float],
    where_sql: str,
    where_params: list[Any],
    candidate_k: int,
) -> list[str]:
    """Return observation ids ordered by vec distance (closest first)."""
    # sqlite-vec's kNN operator only accepts ``embedding MATCH ? AND k = ?``
    # without extra predicates, so we fetch the top candidates first and then
    # filter/order by joining to observations afterwards.
    blob = sqlite_vec.serialize_float32(query_vector)
    knn_rows = conn.execute(
        "SELECT observation_id, distance "
        "FROM observation_embeddings "
        "WHERE embedding MATCH ? AND k = ? "
        "ORDER BY distance",
        (blob, candidate_k),
    ).fetchall()
    if not knn_rows:
        return []

    # Build the filtered set by joining to observations.
    ids_in_order = [r["observation_id"] for r in knn_rows]
    placeholders = ",".join("?" for _ in ids_in_order)
    sql = f"SELECT o.id AS id FROM observations o WHERE o.id IN ({placeholders})"
    params: list[Any] = list(ids_in_order)
    if where_sql:
        sql += " AND " + where_sql
        params.extend(where_params)

    allowed = {r["id"] for r in conn.execute(sql, params).fetchall()}
    # Preserve kNN order while filtering.
    return [i for i in ids_in_order if i in allowed]


# ---------------------------------------------------------------------------
# RRF + hydration
# ---------------------------------------------------------------------------


def _add_rrf_ranks(
    candidates: dict[str, _Candidate],
    ids_in_rank_order: list[str],
    *,
    source: str,
    rrf_k: int,
) -> None:
    """Fold ``ids_in_rank_order`` into ``candidates`` by adding RRF scores."""
    for rank, obs_id in enumerate(ids_in_rank_order, start=1):
        entry = candidates.get(obs_id)
        if entry is None:
            entry = _Candidate(row=None)  # type: ignore[arg-type]
            candidates[obs_id] = entry
        entry.ranks[source] = rank
        entry.rrf_score += 1.0 / (rrf_k + rank)


def _fetch_rows(
    conn: sqlite3.Connection, ids: list[str]
) -> list[sqlite3.Row]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    sql = (
        "SELECT id, content, component, theme, outcome, "
        "reinforcement_score, created_at "
        f"FROM observations WHERE id IN ({placeholders})"
    )
    return conn.execute(sql, ids).fetchall()


# ---------------------------------------------------------------------------
# Finalization: multiply by reinforcement & recency
# ---------------------------------------------------------------------------


def _finalize(
    candidate: _Candidate,
    *,
    now: datetime,
    alpha: float,
    half_life: float,
) -> SearchResult:
    row = candidate.row
    reinforcement = float(row["reinforcement_score"])
    reinforcement_mult = 1.0 + alpha * reinforcement

    age_days = _age_in_days(row["created_at"], now=now)
    if half_life > 0:
        recency_mult = 0.5 ** (max(age_days, 0.0) / half_life)
    else:
        recency_mult = 1.0

    final = candidate.rrf_score * reinforcement_mult * recency_mult

    return SearchResult(
        id=row["id"],
        content=row["content"],
        component=row["component"],
        theme=row["theme"],
        outcome=row["outcome"],
        reinforcement_score=reinforcement,
        created_at=row["created_at"],
        final_score=final,
    )


def _age_in_days(created_at: str, *, now: datetime) -> float:
    """Return the age in days (as a float) between ``created_at`` and ``now``."""
    created = _parse_sqlite_datetime(created_at)
    # Normalise both sides to UTC-aware for safe subtraction.
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    delta = now - created
    return delta.total_seconds() / 86400.0


def _parse_sqlite_datetime(value: str) -> datetime:
    """Parse a timestamp as SQLite stores it (space- or T-separated)."""
    # SQLite's ``datetime('now')`` uses a space separator; ``.isoformat()``
    # uses 'T'. ``fromisoformat`` on 3.12 accepts both, but be defensive
    # against older sub-second/no-tz forms.
    normalized = value.replace(" ", "T")
    return datetime.fromisoformat(normalized)


def _default_clock() -> datetime:
    return datetime.now(UTC)
