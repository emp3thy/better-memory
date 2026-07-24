"""Exploration-slot serves are tagged so the headline metric can exclude them.

Exploration is an investment the ranker makes, not a relevance claim;
counting it in useful% punishes the system for learning (measured ~2-4pts
drag in the PR-A A/B). Rating flow is unchanged — explorers still rated.
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


def _seed(conn, rid, *, useful=0, ignored=0):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at, useful_count, times_ignored)
           VALUES (?, ?, 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01', ?, ?)""",
        (rid, rid, useful, ignored),
    )
    conn.commit()


def _flags(conn):
    return {
        r[0]: r[1] for r in conn.execute(
            "SELECT memory_id, via_exploration FROM session_memory_exposure")
    }


class TestExplorationTagging:
    def test_slot_serve_tagged_others_not(self, conn, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
        for i in range(3):
            _seed(conn, f"r-proven-{i}", useful=5, ignored=5)
        _seed(conn, "r-untested")
        svc = ReflectionSynthesisService(conn)
        svc.retrieve_reflections(project="p", limit_per_bucket=3)
        flags = _flags(conn)
        assert flags["r-untested"] == 1
        assert flags["r-proven-0"] == 0

    def test_dedup_wins_over_tag(self, conn, monkeypatch):
        # Memory already exposed normally: a later exploration serve writes
        # nothing, so the flag stays 0 (first-source-wins).
        monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
        _seed(conn, "r-x")
        svc = ReflectionSynthesisService(conn)
        svc.retrieve_reflections(project="p", limit_per_bucket=None)   # normal serve
        for i in range(3):
            _seed(conn, f"r-proven-{i}", useful=5, ignored=5)
        svc.retrieve_reflections(project="p", limit_per_bucket=3)      # r-x now explorer
        assert _flags(conn)["r-x"] == 0

    def test_unlimited_cap_never_tags(self, conn, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
        _seed(conn, "r-untested")
        svc = ReflectionSynthesisService(conn)
        svc.retrieve_reflections(project="p", limit_per_bucket=None)
        assert _flags(conn)["r-untested"] == 0
