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
                     polarity="do", updated_at="2026-01-01",
                     times_overlooked=0):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at, useful_count,
            times_overlooked)
           VALUES (?, ?, 'p', 'general', ?, 'uc', '[]', ?,
                   '2026-01-01', ?, ?, ?)""",
        (rid, rid, polarity, confidence, updated_at, useful_count,
         times_overlooked),
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


def _seed_semantic(conn, sid, *, useful_count=0, created_at="2026-01-01",
                   times_overlooked=0):
    conn.execute(
        """INSERT INTO semantic_memories
           (id, content, project, scope, created_at, updated_at,
            useful_count, times_overlooked)
           VALUES (?, 'fact', 'p', 'project', ?, ?, ?, ?)""",
        (sid, created_at, created_at, useful_count, times_overlooked),
    )
    conn.commit()


class TestSemanticRanking:
    def test_useful_count_beats_created_at(self, conn):
        """High useful_count surfaces first even when older."""
        from better_memory.services.semantic import SemanticMemoryService
        _seed_semantic(conn, "s-older-but-useful",
                       useful_count=5, created_at="2026-01-01")
        _seed_semantic(conn, "s-newer-unused",
                       useful_count=0, created_at="2026-05-01")
        svc = SemanticMemoryService(conn)
        results = svc.list_for_project(project="p", track_exposure=False)
        ids = [m.id for m in results]
        assert ids[0] == "s-older-but-useful"
        assert ids[1] == "s-newer-unused"

    def test_created_at_tiebreaks_when_useful_count_equal(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        _seed_semantic(conn, "s-older", useful_count=0, created_at="2026-01-01")
        _seed_semantic(conn, "s-newer", useful_count=0, created_at="2026-05-01")
        svc = SemanticMemoryService(conn)
        results = svc.list_for_project(project="p", track_exposure=False)
        ids = [m.id for m in results]
        assert ids[0] == "s-newer"


class TestOverlookedRanking:
    def test_overlooked_outranks_lower_useful_count(self, conn):
        """One overlooked (weight 3) beats useful_count=2 (score 2 < 3)."""
        from better_memory.services.reflection import ReflectionSynthesisService
        _seed_reflection(conn, "r-useful-2", useful_count=2,
                         times_overlooked=0)
        _seed_reflection(conn, "r-overlooked-1", useful_count=0,
                         times_overlooked=1)
        svc = ReflectionSynthesisService(conn)
        result = svc.retrieve_reflections(project="p")
        ids = [r["id"] for r in result["do"]]
        assert ids.index("r-overlooked-1") < ids.index("r-useful-2")

    def test_high_useful_count_still_beats_one_overlooked(self, conn):
        """useful_count=4 (score 4) beats one overlooked (score 3)."""
        from better_memory.services.reflection import ReflectionSynthesisService
        _seed_reflection(conn, "r-useful-4", useful_count=4,
                         times_overlooked=0)
        _seed_reflection(conn, "r-overlooked-1", useful_count=0,
                         times_overlooked=1)
        svc = ReflectionSynthesisService(conn)
        result = svc.retrieve_reflections(project="p")
        ids = [r["id"] for r in result["do"]]
        assert ids.index("r-useful-4") < ids.index("r-overlooked-1")

    def test_semantic_overlooked_outranks_lower_useful_count(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        _seed_semantic(conn, "s-useful-2", useful_count=2,
                       times_overlooked=0)
        _seed_semantic(conn, "s-overlooked-1", useful_count=0,
                       times_overlooked=1)
        svc = SemanticMemoryService(conn)
        results = svc.list_for_project(project="p", track_exposure=False)
        ids = [m.id for m in results]
        assert ids.index("s-overlooked-1") < ids.index("s-useful-2")

    def test_semantic_high_useful_count_still_beats_one_overlooked(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        _seed_semantic(conn, "s-useful-4", useful_count=4,
                       times_overlooked=0)
        _seed_semantic(conn, "s-overlooked-1", useful_count=0,
                       times_overlooked=1)
        svc = SemanticMemoryService(conn)
        results = svc.list_for_project(project="p", track_exposure=False)
        ids = [m.id for m in results]
        assert ids.index("s-useful-4") < ids.index("s-overlooked-1")

    def test_semantic_read_model_carries_times_overlooked(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        _seed_semantic(conn, "s1", times_overlooked=5)
        svc = SemanticMemoryService(conn)
        results = svc.list_for_project(project="p", track_exposure=False)
        assert results[0].times_overlooked == 5
