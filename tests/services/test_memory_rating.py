"""Tests for MemoryRatingService.credit_one."""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

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


@pytest.fixture
def fixed_clock():
    fixed = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


def _seed_exposure(conn, session_id, kind, memory_id, exposed_at="2026-05-11T11:00:00+00:00"):
    conn.execute(
        "INSERT INTO session_memory_exposure "
        "(session_id, memory_kind, memory_id, exposed_at, source) "
        "VALUES (?, ?, ?, ?, 'bootstrap')",
        (session_id, kind, memory_id, exposed_at),
    )
    conn.commit()


def _seed_reflection(conn, rid="r1"):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at)
           VALUES (?, 't', 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01')""",
        (rid,),
    )
    conn.commit()


def _seed_semantic(conn, sid="s1"):
    conn.execute(
        """INSERT INTO semantic_memories
           (id, content, project, scope, created_at, updated_at)
           VALUES (?, 'fact', 'p', 'project', '2026-01-01', '2026-01-01')""",
        (sid,),
    )
    conn.commit()


class TestCreditOneClassEffects:
    def test_cited_bumps_useful_count_on_reflection(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        assert result == {"applied": "cited", "skipped": None}
        row = conn.execute(
            "SELECT useful_count, last_useful_at FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["useful_count"] == 1
        assert row["last_useful_at"] == "2026-05-11T12:00:00+00:00"

    def test_shaped_bumps_useful_count_on_semantic(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_semantic(conn, "s1")
        _seed_exposure(conn, "S1", "semantic", "s1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        svc.credit_one(
            session_id="S1", kind="semantic", id="s1", classification="shaped",
        )
        row = conn.execute(
            "SELECT useful_count FROM semantic_memories WHERE id='s1'"
        ).fetchone()
        assert row["useful_count"] == 1

    def test_misled_bumps_times_misled(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="misled",
        )
        row = conn.execute(
            "SELECT useful_count, times_misled, last_misled_at "
            "FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["useful_count"] == 0
        assert row["times_misled"] == 1
        assert row["last_misled_at"] == "2026-05-11T12:00:00+00:00"

    def test_credit_stamps_exposure_row(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        row = conn.execute(
            "SELECT rated_at, classification FROM session_memory_exposure "
            "WHERE session_id='S1'"
        ).fetchone()
        assert row["rated_at"] == "2026-05-11T12:00:00+00:00"
        assert row["classification"] == "cited"


class TestCreditOneSkips:
    def test_skip_not_exposed(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        # NO exposure row
        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        assert result == {"applied": None, "skipped": "not_exposed"}
        row = conn.execute(
            "SELECT useful_count FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["useful_count"] == 0

    def test_skip_already_rated(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        _seed_exposure(conn, "S1", "reflection", "r1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        # second call is the no-op
        result = svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        assert result == {"applied": None, "skipped": "already_rated"}
        row = conn.execute(
            "SELECT useful_count FROM reflections WHERE id='r1'"
        ).fetchone()
        assert row["useful_count"] == 1  # not double-bumped

    def test_skip_memory_missing(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        # exposure exists but reflection row does NOT
        _seed_exposure(conn, "S1", "reflection", "r-missing")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.credit_one(
            session_id="S1", kind="reflection", id="r-missing",
            classification="cited",
        )
        assert result == {"applied": None, "skipped": "memory_missing"}

    def test_skip_memory_retired(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        _seed_reflection(conn, "r1")
        conn.execute(
            "UPDATE reflections SET status='retired' WHERE id='r1'"
        )
        conn.commit()
        _seed_exposure(conn, "S1", "reflection", "r1")
        svc = MemoryRatingService(conn, clock=fixed_clock)
        result = svc.credit_one(
            session_id="S1", kind="reflection", id="r1", classification="cited",
        )
        assert result == {"applied": None, "skipped": "memory_retired"}


class TestCreditOneValidation:
    def test_ignored_class_rejected(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        svc = MemoryRatingService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="ignored"):
            svc.credit_one(
                session_id="S1", kind="reflection", id="r1", classification="ignored",
            )

    def test_unknown_class_rejected(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        svc = MemoryRatingService(conn, clock=fixed_clock)
        with pytest.raises(ValueError):
            svc.credit_one(
                session_id="S1", kind="reflection", id="r1", classification="bogus",
            )

    def test_unknown_kind_rejected(self, conn, fixed_clock):
        from better_memory.services.memory_rating import MemoryRatingService
        svc = MemoryRatingService(conn, clock=fixed_clock)
        with pytest.raises(ValueError):
            svc.credit_one(
                session_id="S1", kind="observation", id="o1", classification="cited",
            )
