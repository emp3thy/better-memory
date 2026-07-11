"""Verify retrieve_reflections carries times_misled + updated_at.

Task 4's contextual relevance scorer consumes both fields to apply the
misled penalty and to weight by recency.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.reflection import ReflectionSynthesisService


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed_reflection(conn, rid, *, useful_count=0, confidence=0.5,
                     polarity="do", updated_at="2026-01-01",
                     times_overlooked=0, times_misled=0):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at, useful_count,
            times_overlooked, times_misled)
           VALUES (?, ?, 'proj', 'general', ?, 'uc', '[]', ?,
                   '2026-01-01', ?, ?, ?, ?)""",
        (rid, rid, polarity, confidence, updated_at, useful_count,
         times_overlooked, times_misled),
    )
    conn.commit()


def test_retrieve_reflections_includes_misled_and_updated_at(conn):
    """Every bucket dict must carry times_misled and updated_at so the
    contextual relevance scorer can apply the misled penalty and age."""
    _seed_reflection(conn, "r1")
    svc = ReflectionSynthesisService(conn)
    buckets = svc.retrieve_reflections(project="proj", track_exposure=False)
    item = (buckets["do"] + buckets["dont"] + buckets["neutral"])[0]
    assert item["times_misled"] == 0
    assert isinstance(item["updated_at"], str) and item["updated_at"]
