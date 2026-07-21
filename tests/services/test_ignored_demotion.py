"""Memories that keep being served and keep not mattering must sink.

Before migration 0013 the ranking key had no negative term, so a reflection
served 142 times that was useful zero times sorted level with one that had
never been served at all. On a live DB, 55 such memories accounted for 27.5%
of every rated exposure.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.memory_rating import (
    IGNORED_DEMOTION_FLOOR,
    MemoryRatingService,
)
from better_memory.services.reflection import ReflectionSynthesisService


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed(conn, rid, *, useful_count=0, times_ignored=0, confidence=0.5):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at, useful_count, times_ignored)
           VALUES (?, ?, 'p', 'general', 'do', 'uc', '[]', ?,
                   '2026-01-01', '2026-01-01', ?, ?)""",
        (rid, rid, confidence, useful_count, times_ignored),
    )
    conn.commit()


def _ids(conn, **kw):
    svc = ReflectionSynthesisService(conn)
    return [r["id"] for r in svc.retrieve_reflections(project="p", **kw)["do"]]


class TestIgnoredDemotion:
    def test_chronically_ignored_sinks_below_never_served(self, conn):
        _seed(conn, "r-proven-useless", useful_count=0, times_ignored=60)
        _seed(conn, "r-untested", useful_count=0, times_ignored=0)
        assert _ids(conn) == ["r-untested", "r-proven-useless"]

    def test_below_floor_is_not_demoted(self, conn):
        # A handful of ignores says nothing — the task simply wasn't relevant.
        _seed(conn, "r-a", useful_count=0, times_ignored=IGNORED_DEMOTION_FLOOR,
              confidence=0.9)
        _seed(conn, "r-b", useful_count=0, times_ignored=0, confidence=0.1)
        assert _ids(conn) == ["r-a", "r-b"], "at the floor, confidence still decides"

    def test_memory_with_useful_history_is_never_demoted(self, conn):
        # The best-performing memory on the live DB is ignored far more often
        # than it is used; a hit rate below 50% is normal and fine.
        _seed(conn, "r-useful-but-often-ignored", useful_count=18, times_ignored=55)
        _seed(conn, "r-untested", useful_count=0, times_ignored=0)
        assert _ids(conn)[0] == "r-useful-but-often-ignored"

    def test_apply_session_ratings_bumps_times_ignored(self, conn, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
        _seed(conn, "r-a")
        conn.execute(
            "INSERT INTO session_memory_exposure "
            "(session_id, memory_kind, memory_id, exposed_at, source) "
            "VALUES ('s1', 'reflection', 'r-a', '2026-01-01', 'retrieve')"
        )
        conn.commit()

        MemoryRatingService(conn).apply_session_ratings(
            session_id="s1",
            ratings=[{"kind": "reflection", "id": "r-a", "class": "ignored"}],
        )
        row = conn.execute(
            "SELECT times_ignored, last_ignored_at FROM reflections WHERE id = 'r-a'"
        ).fetchone()
        assert row["times_ignored"] == 1
        assert row["last_ignored_at"] is not None

    def test_repeat_exposures_in_one_session_count_once(self, conn, monkeypatch):
        # A memory retrieved five times in one session that lands nowhere
        # failed once, not five times.
        monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
        _seed(conn, "r-a")
        for i in range(5):
            conn.execute(
                "INSERT INTO session_memory_exposure "
                "(session_id, memory_kind, memory_id, exposed_at, source) "
                "VALUES ('s1', 'reflection', 'r-a', ?, 'retrieve')",
                (f"2026-01-01T00:0{i}:00",),
            )
        conn.commit()

        MemoryRatingService(conn).apply_session_ratings(
            session_id="s1",
            ratings=[{"kind": "reflection", "id": "r-a", "class": "ignored"}],
        )
        assert conn.execute(
            "SELECT times_ignored FROM reflections WHERE id = 'r-a'"
        ).fetchone()["times_ignored"] == 1
