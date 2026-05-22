"""End-to-end tests for :mod:`better_memory.search.tfidf_search`."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.embeddings.tfidf import TfidfRetriever
from better_memory.search.hybrid import SearchFilters
from better_memory.search.tfidf_search import tfidf_search


@pytest.fixture
def conn(tmp_memory_db: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_memory_db)
    try:
        apply_migrations(c)
        yield c
    finally:
        c.close()


def _seed(conn: sqlite3.Connection, *docs: tuple[str, str, str]) -> None:
    """Insert (id, content, outcome) rows + episodes."""
    for obs_id, content, outcome in docs:
        conn.execute(
            "INSERT INTO episodes (id, project, started_at, outcome) "
            "VALUES (?, 'p', '2026-01-01T00:00:00+00:00', NULL)",
            (f"ep-{obs_id}",),
        )
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id, "
            "status, outcome, created_at, status_changed_at) "
            "VALUES (?, ?, 'p', ?, 'active', ?, "
            "'2026-04-01T00:00:00+00:00', '2026-04-01T00:00:00+00:00')",
            (obs_id, content, f"ep-{obs_id}", outcome),
        )
    conn.commit()


def _clock() -> datetime:
    return datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC)


def test_tfidf_search_returns_relevant_doc_first(conn: sqlite3.Connection) -> None:
    _seed(
        conn,
        ("hit", "pytest junit-xml output capture on windows", "success"),
        ("miss", "completely unrelated config setting docs", "success"),
    )
    r = TfidfRetriever(conn)
    r.fit_from_db()

    filters = SearchFilters(
        project="p", status="active", window_days=None, outcome="success"
    )
    results = tfidf_search(
        conn, r, query_text="pytest windows", filters=filters,
        limit=2, clock=_clock,
    )

    assert [hit.id for hit in results][0] == "hit"


def test_tfidf_search_empty_query_returns_empty(conn: sqlite3.Connection) -> None:
    _seed(conn, ("o1", "anything", "neutral"))
    r = TfidfRetriever(conn)
    r.fit_from_db()
    filters = SearchFilters(project="p", status="active", window_days=None)
    assert tfidf_search(conn, r, query_text=None, filters=filters,
                        limit=10, clock=_clock) == []


def test_tfidf_search_respects_outcome_filter(conn: sqlite3.Connection) -> None:
    _seed(
        conn,
        ("s", "alpha beta gamma", "success"),
        ("f", "alpha beta gamma", "failure"),
    )
    r = TfidfRetriever(conn)
    r.fit_from_db()

    filters = SearchFilters(
        project="p", status="active", window_days=None, outcome="success"
    )
    results = tfidf_search(
        conn, r, query_text="alpha", filters=filters,
        limit=10, clock=_clock,
    )
    assert {hit.id for hit in results} == {"s"}
