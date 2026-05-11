"""Verify useful_count is the primary sort key in retrieval."""
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed_reflection(conn, rid, *, useful_count=0, confidence=0.5,
                     polarity="do", updated_at="2026-01-01"):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at, useful_count)
           VALUES (?, ?, 'p', 'general', ?, 'uc', '[]', ?,
                   '2026-01-01', ?, ?)""",
        (rid, rid, polarity, confidence, updated_at, useful_count),
    )
    conn.commit()


class TestReflectionRanking:
    def test_useful_count_beats_confidence(self, conn):
        from better_memory.services.reflection import ReflectionSynthesisService
        _seed_reflection(conn, "r-low-confidence-but-useful",
                          useful_count=5, confidence=0.3)
        _seed_reflection(conn, "r-high-confidence-unused",
                          useful_count=0, confidence=0.9)
        svc = ReflectionSynthesisService(conn)
        result = svc.retrieve_reflections(project="p")
        ids = [r["id"] for r in result["do"]]
        assert ids[0] == "r-low-confidence-but-useful"
        assert ids[1] == "r-high-confidence-unused"

    def test_confidence_tiebreaks_when_useful_count_equal(self, conn):
        from better_memory.services.reflection import ReflectionSynthesisService
        _seed_reflection(conn, "r-mid-confidence", useful_count=0, confidence=0.5)
        _seed_reflection(conn, "r-high-confidence", useful_count=0, confidence=0.9)
        svc = ReflectionSynthesisService(conn)
        result = svc.retrieve_reflections(project="p")
        ids = [r["id"] for r in result["do"]]
        assert ids[0] == "r-high-confidence"

    def test_updated_at_tiebreaks_when_both_equal(self, conn):
        from better_memory.services.reflection import ReflectionSynthesisService
        _seed_reflection(conn, "r-older",
                          useful_count=2, confidence=0.5, updated_at="2026-01-01")
        _seed_reflection(conn, "r-newer",
                          useful_count=2, confidence=0.5, updated_at="2026-05-01")
        svc = ReflectionSynthesisService(conn)
        result = svc.retrieve_reflections(project="p")
        ids = [r["id"] for r in result["do"]]
        assert ids[0] == "r-newer"
