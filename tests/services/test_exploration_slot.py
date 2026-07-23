"""One shortlist slot per bucket is reserved for an untested memory.

Untested = fewer than 3 rated exposures. Wilson scores them 0.0, so
without the slot they would never be served and never earn a rating.
Rating coverage is ~100% (sync Stop hook), so one serve = one rating.
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


def _seed(conn, rid, *, useful=0, ignored=0, updated_at="2026-01-01"):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at, useful_count, times_ignored)
           VALUES (?, ?, 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', ?, ?, ?)""",
        (rid, rid, updated_at, useful, ignored),
    )
    conn.commit()


def _ids(conn, cap=3):
    svc = ReflectionSynthesisService(conn)
    return [r["id"] for r in
            svc.retrieve_reflections(project="p", limit_per_bucket=cap)["do"]]


class TestExplorationSlot:
    def test_last_slot_goes_to_best_untested(self, conn):
        for i in range(4):                       # four proven memories
            _seed(conn, f"r-proven-{i}", useful=5 - i, ignored=5)
        _seed(conn, "r-untested-old", updated_at="2026-02-01")
        _seed(conn, "r-untested-new", updated_at="2026-06-01")
        ids = _ids(conn, cap=3)
        assert len(ids) == 3
        assert ids[:2] == ["r-proven-0", "r-proven-1"]      # cap-1 proven
        assert ids[2] == "r-untested-new"                    # best untested

    def test_no_untested_fills_all_slots_with_proven(self, conn):
        for i in range(4):
            _seed(conn, f"r-proven-{i}", useful=5 - i, ignored=5)
        ids = _ids(conn, cap=3)
        assert ids == ["r-proven-0", "r-proven-1", "r-proven-2"]

    def test_all_untested_fills_normally(self, conn):
        for i in range(4):
            _seed(conn, f"r-untested-{i}")
        assert len(_ids(conn, cap=3)) == 3

    def test_two_ratings_is_still_untested_three_is_not(self, conn):
        _seed(conn, "r-two", useful=1, ignored=1)     # rated == 2: untested
        _seed(conn, "r-three", useful=1, ignored=2)   # rated == 3: tested
        for i in range(3):
            _seed(conn, f"r-proven-{i}", useful=9, ignored=1)
        ids = _ids(conn, cap=3)
        assert ids[2] == "r-two"

    def test_unlimited_cap_reserves_nothing(self, conn):
        _seed(conn, "r-proven", useful=5, ignored=5)
        _seed(conn, "r-untested")
        svc = ReflectionSynthesisService(conn)
        rows = svc.retrieve_reflections(project="p", limit_per_bucket=None)["do"]
        assert len(rows) == 2                        # everything returned anyway
