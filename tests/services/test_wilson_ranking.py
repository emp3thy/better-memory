"""Retrieval ranks by Wilson lower bound on (useful+overlooked)/rated.

Replaces the popularity + overlooked-weight + ignored-demotion stack.
Covers the old demotion scenarios too: proven dead weight sinks because
0 positive over many rated gives LB ~ 0, with no special-case code.
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


def _seed(conn, rid, *, useful=0, overlooked=0, ignored=0, confidence=0.5,
          polarity="do", updated_at="2026-01-01"):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at, useful_count,
            times_overlooked, times_ignored)
           VALUES (?, ?, 'p', 'general', ?, 'uc', '[]', ?,
                   '2026-01-01', ?, ?, ?, ?)""",
        (rid, rid, polarity, confidence, updated_at, useful, overlooked, ignored),
    )
    conn.commit()


def _ids(conn, **kw):
    svc = ReflectionSynthesisService(conn)
    return [r["id"] for r in svc.retrieve_reflections(project="p", **kw)["do"]]


class TestWilsonOrdering:
    def test_hit_rate_beats_raw_count(self, conn):
        _seed(conn, "r-workhorse", useful=67, ignored=125)      # 67/192 ~ 0.28
        _seed(conn, "r-newcomer", useful=3, ignored=1)          # 3/4  ~ 0.30
        assert _ids(conn)[:2] == ["r-newcomer", "r-workhorse"]

    def test_overlooked_counts_as_positive(self, conn):
        _seed(conn, "r-overlooked", overlooked=3, ignored=1)
        _seed(conn, "r-plain", useful=1, ignored=3)
        assert _ids(conn)[0] == "r-overlooked"

    def test_proven_dead_weight_sinks_below_modest_performer(self, conn):
        _seed(conn, "r-dead", useful=0, ignored=58)
        _seed(conn, "r-modest", useful=2, ignored=8)
        assert _ids(conn) == ["r-modest", "r-dead"]

    def test_confidence_breaks_wilson_ties(self, conn):
        _seed(conn, "r-low-conf", confidence=0.3)
        _seed(conn, "r-high-conf", confidence=0.9)
        assert _ids(conn) == ["r-high-conf", "r-low-conf"]

    def test_recency_breaks_confidence_ties(self, conn):
        _seed(conn, "r-old", updated_at="2026-01-01")
        _seed(conn, "r-new", updated_at="2026-06-01")
        assert _ids(conn) == ["r-new", "r-old"]

    def test_rows_expose_all_three_counters(self, conn):
        _seed(conn, "r-a", useful=1, overlooked=2, ignored=3)
        svc = ReflectionSynthesisService(conn)
        row = svc.retrieve_reflections(project="p")["do"][0]
        assert row["useful_count"] == 1
        assert row["times_overlooked"] == 2
        assert row["times_ignored"] == 3

    def test_demotion_constants_are_gone(self):
        import better_memory.services.memory_rating as mr
        for name in ("IGNORED_DEMOTION_FLOOR", "IGNORED_DEMOTION_WEIGHT",
                     "OVERLOOKED_RANKING_WEIGHT"):
            assert not hasattr(mr, name), f"{name} should be deleted"
