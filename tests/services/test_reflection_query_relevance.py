"""Verify the ``query`` argument makes retrieval task-relevant.

Without ``query``, ``retrieve_reflections`` orders by the popularity prior
alone (useful_count + 3*times_overlooked, confidence, recency), so every
caller gets the same rows regardless of what they are working on. With it, a
BM25 ranking over title/use_cases/hints is fused in via RRF.
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


def _seed(conn, rid, *, title, use_cases="uc", hints="[]", useful_count=0,
          confidence=0.5, polarity="do"):
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at, useful_count)
           VALUES (?, ?, 'p', 'general', ?, ?, ?, ?,
                   '2026-01-01', '2026-01-01', ?)""",
        (rid, title, polarity, use_cases, hints, confidence, useful_count),
    )
    conn.commit()


class TestQueryRelevance:
    def test_relevant_row_outranks_more_popular_irrelevant_row(self, conn):
        # The popular row wins on the prior alone; the relevant row only wins
        # once the query is supplied.
        _seed(conn, "r-popular-irrelevant",
              title="Always rebase before pushing", useful_count=10)
        _seed(conn, "r-unpopular-relevant",
              title="Retention archives reflections by confidence",
              use_cases="changing retention or pruning thresholds",
              useful_count=0)
        svc = ReflectionSynthesisService(conn)

        without = [r["id"] for r in svc.retrieve_reflections(project="p")["do"]]
        assert without[0] == "r-popular-irrelevant"

        with_query = [
            r["id"] for r in svc.retrieve_reflections(
                project="p", query="changing how retention prunes reflections",
            )["do"]
        ]
        assert with_query[0] == "r-unpopular-relevant"

    def test_query_promotes_but_never_discards(self, conn):
        _seed(conn, "r-a", title="Retention thresholds", useful_count=0)
        _seed(conn, "r-b", title="Unrelated advice", useful_count=1)
        svc = ReflectionSynthesisService(conn)
        ids = [
            r["id"] for r in svc.retrieve_reflections(
                project="p", query="retention",
            )["do"]
        ]
        assert set(ids) == {"r-a", "r-b"}, "non-matching rows must survive"

    def test_no_match_degrades_to_popularity_order(self, conn):
        _seed(conn, "r-popular", title="Alpha", useful_count=9)
        _seed(conn, "r-quiet", title="Beta", useful_count=0)
        svc = ReflectionSynthesisService(conn)
        baseline = [r["id"] for r in svc.retrieve_reflections(project="p")["do"]]
        queried = [
            r["id"] for r in svc.retrieve_reflections(
                project="p", query="zzzznothingmatchesthis",
            )["do"]
        ]
        assert queried == baseline

    def test_operator_characters_in_query_do_not_raise(self, conn):
        # 'better-memory' parses as a column-exclusion in raw FTS5 syntax.
        _seed(conn, "r-a", title="Alpha", useful_count=1)
        svc = ReflectionSynthesisService(conn)
        ids = [
            r["id"] for r in svc.retrieve_reflections(
                project="p", query='better-memory: "hooks" (windows)',
            )["do"]
        ]
        assert ids == ["r-a"]

    def test_short_tokens_alone_degrade_to_popularity_order(self, conn):
        _seed(conn, "r-popular", title="Alpha", useful_count=4)
        _seed(conn, "r-quiet", title="Beta", useful_count=0)
        svc = ReflectionSynthesisService(conn)
        baseline = [r["id"] for r in svc.retrieve_reflections(project="p")["do"]]
        queried = [
            r["id"] for r in svc.retrieve_reflections(project="p", query="a of to")["do"]
        ]
        assert queried == baseline
