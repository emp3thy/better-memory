"""Tests for ObservationService with the TF-IDF retriever backend."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.embeddings.tfidf import TfidfRetriever
from better_memory.services.episode import EpisodeService
from better_memory.services.observation import ObservationService


@pytest.fixture
def conn(tmp_memory_db: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_memory_db)
    try:
        apply_migrations(c)
        yield c
    finally:
        c.close()


@pytest.fixture
def fixed_clock():
    fixed = datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


@pytest.fixture
def service(conn: sqlite3.Connection, fixed_clock) -> ObservationService:
    retriever = TfidfRetriever(conn)
    retriever.fit_from_db()
    episodes = EpisodeService(conn, clock=fixed_clock)
    return ObservationService(
        conn,
        embedder=None,
        retriever=retriever,
        clock=fixed_clock,
        project_resolver=lambda: "p",
        scope_resolver=lambda: None,
        session_id="sess",
        episodes=episodes,
    )


async def test_create_skips_vec0_insert_in_tfidf_mode(
    service: ObservationService, conn: sqlite3.Connection
) -> None:
    obs_id = await service.create(content="hello tfidf world", outcome="success")

    row = conn.execute(
        "SELECT id FROM observations WHERE id = ?", (obs_id,)
    ).fetchone()
    assert row is not None

    vec_count = conn.execute(
        "SELECT COUNT(*) FROM observation_embeddings WHERE observation_id = ?",
        (obs_id,),
    ).fetchone()[0]
    assert vec_count == 0


async def test_create_indexes_into_retriever(
    service: ObservationService,
) -> None:
    obs_id = await service.create(content="unique-marker-xyz", outcome="neutral")
    assert obs_id in service._retriever._doc_vectors  # type: ignore[union-attr]


async def test_create_requires_exactly_one_of_embedder_retriever(
    conn: sqlite3.Connection, fixed_clock
) -> None:
    episodes = EpisodeService(conn, clock=fixed_clock)
    with pytest.raises(ValueError, match="exactly one of embedder/retriever"):
        ObservationService(
            conn, embedder=None, retriever=None,
            clock=fixed_clock, episodes=episodes,
        )
