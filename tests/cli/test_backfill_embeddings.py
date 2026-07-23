from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.cli.backfill_embeddings import backfill
from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from tests.services._embedding_fakes import FakeEmbedder


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed_reflection(conn, rid, status="pending_review"):
    conn.execute(
        """INSERT INTO reflections (id, title, project, phase, polarity,
           use_cases, hints, confidence, created_at, updated_at, status)
           VALUES (?, 'T', 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01', ?)""", (rid, status))
    conn.commit()


def _seed_semantic(conn, sid):
    conn.execute(
        """INSERT INTO semantic_memories (id, content, project, scope,
           created_at, updated_at)
           VALUES (?, 'fact', 'p', 'project', '2026-01-01', '2026-01-01')""",
        (sid,))
    conn.commit()


def test_backfills_reflections_and_semantics(conn):
    _seed_reflection(conn, "r1")
    _seed_semantic(conn, "s1")
    stats = backfill(conn, FakeEmbedder())
    assert stats == {"reflections": 1, "semantics": 1, "skipped": 0}
    assert conn.execute("SELECT COUNT(*) FROM reflection_embeddings").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM semantic_embeddings").fetchone()[0] == 1


def test_idempotent(conn):
    _seed_reflection(conn, "r1")
    fake = FakeEmbedder()
    backfill(conn, fake)
    assert backfill(conn, fake) == {"reflections": 0, "semantics": 0, "skipped": 0}


def test_retired_reflections_skipped(conn):
    _seed_reflection(conn, "r1", status="retired")
    assert backfill(conn, FakeEmbedder()) == {
        "reflections": 0, "semantics": 0, "skipped": 0}


def test_embed_failure_counted_as_skipped(conn):
    _seed_reflection(conn, "r1")
    stats = backfill(conn, FakeEmbedder(fail=True))
    assert stats == {"reflections": 0, "semantics": 0, "skipped": 1}
