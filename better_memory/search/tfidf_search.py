"""TF-IDF + FTS5 BM25 hybrid search for the TF-IDF embeddings backend.

Mirrors :func:`better_memory.search.hybrid.hybrid_search` but uses an
in-memory :class:`TfidfRetriever` for the vector half instead of
sqlite-vec. Reuses private helpers from ``hybrid`` for FTS5 candidates,
RRF fusion, row hydration, and the reinforcement+recency finalisation.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from better_memory.embeddings.tfidf import TfidfRetriever
from better_memory.search.hybrid import (
    SearchFilters,
    SearchResult,
    _add_rrf_ranks,
    _build_where,
    _Candidate,
    _fetch_rows,
    _finalize,
    _fts_candidates,
)


_DEFAULT_FILTERS = SearchFilters()


def _default_clock() -> datetime:
    return datetime.now(UTC)


def tfidf_search(
    conn: sqlite3.Connection,
    retriever: TfidfRetriever,
    *,
    query_text: str | None = None,
    filters: SearchFilters = _DEFAULT_FILTERS,
    limit: int = 10,
    candidate_k: int = 50,
    rrf_k: int = 60,
    reinforcement_alpha: float = 0.1,
    recency_half_life_days: float = 14.0,
    clock: Callable[[], datetime] | None = None,
) -> list[SearchResult]:
    """Run FTS5 BM25 + TF-IDF cosine, fuse via RRF, return top ``limit``."""
    if query_text is None or not query_text.strip():
        return []

    now = (clock or _default_clock)()
    where_sql, where_params = _build_where(filters, now=now)

    # --- FTS5 candidates (SQL) -------------------------------------------
    fts_ids = _fts_candidates(
        conn,
        query_text=query_text,
        where_sql=where_sql,
        where_params=where_params,
        candidate_k=candidate_k,
    )

    # --- TF-IDF candidates (Python over filter-matched ids) --------------
    sql = "SELECT o.id AS id FROM observations o"
    params: list[Any] = []
    if where_sql:
        sql += " WHERE " + where_sql
        params.extend(where_params)
    filter_ids = [r["id"] for r in conn.execute(sql, params).fetchall()]
    tfidf_scored = retriever.score(query_text, filter_ids)
    tfidf_ids = [doc_id for doc_id, score in tfidf_scored[:candidate_k] if score > 0.0]

    if not fts_ids and not tfidf_ids:
        return []

    # --- RRF fuse --------------------------------------------------------
    candidates: dict[str, _Candidate] = {}
    _add_rrf_ranks(candidates, fts_ids, source="fts", rrf_k=rrf_k)
    _add_rrf_ranks(candidates, tfidf_ids, source="vec", rrf_k=rrf_k)
    if not candidates:
        return []

    # --- Hydrate + finalise ----------------------------------------------
    rows = _fetch_rows(conn, list(candidates.keys()))
    for row in rows:
        candidates[row["id"]].row = row

    results = [
        _finalize(c, now=now, alpha=reinforcement_alpha, half_life=recency_half_life_days)
        for c in candidates.values()
        if c.row is not None
    ]
    results.sort(key=lambda r: (-r.final_score, r.id))
    return results[:limit]
